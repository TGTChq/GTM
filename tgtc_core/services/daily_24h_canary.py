"""Daily 24h Fantastic acquisition canary. Feature flag ``FANTASTIC_DAILY_24H_CANARY=1``; OFF by default.

Tests the provider's recommendation -- one daily ``time_frame=24h`` pass, paginating while a
page returns the full limit and stopping at the first shorter page -- WITHOUT touching
production state.

Changed relative to production acquisition (``services/acquisition.py``):

* window: ``time_frame=24h`` and NO ``date_created_gte/lt``. The provider documents that
  ``time_frame`` filters on ``date_created`` and a live count probe proved it intersects the
  two, so an extra bound is redundant at best and narrowing at worst;
* pagination: one traversal inside this run, from ``offset=0``; the next offset is the
  previous offset plus the rows received; a full page always requests another page, the first
  page shorter than ``limit`` ends the partition. No offset is read from or written to any
  persisted cursor.

Unchanged: endpoints, profile filters, ``exclude_ats_duplicate`` / ``include_basic_organization_details``,
``location``, ``description_format``, the priority-profile ``exclude_organization_slug`` rule and the
query partitions (both feeds x priority/discovery profiles).

Never: a database connection, cursors, watermarks, a write to any seen registry (it is read-only
input), Apollo, Airtable, Instantly, email verification or a model call. The only output is a new
evidence directory.

Provider-confirmed strategy (Remco, and developer.fantastic.jobs "Recommended Strategy", read
2026-09-19): one ``time_frame=24h`` pass at the same hour every day (the window is ``date_created``
with a one-hour enrichment delay), ``limit=1000`` from ``offset=0``, the offset advancing by 1,000 after
every full page, stopping at the first page shorter than 1,000. Ids and ordering are stable across
requests and partitions. A failed day is recovered with ``7d`` (offset) or ``6m`` (cursor) and EXACT
``date_created_gte/lt`` bounds. ``ai_taxonomies_a`` matches ANY assigned taxonomy;
``ai_taxonomies_a_primary`` matches only the first (most relevant) one. Headcount and employment filters
drop rows whose value is missing, so Wellfound / Y Combinator and ATS jobs without a mapped company
profile need their own requests. ``STRATEGY_ARMS`` renders those requests; no title filter is used.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from ..domain.acquisition_query import (
    DISCOVERY_PROFILE, PRIORITY_PROFILE, count_query, feed_covers_window, profile_filters, provider_list, recent_size_exclusions,
)
from ..domain.candidate_qualification import approved_industry_query_labels
from ..domain.classification import classify_posting
from ..domain.employer_attribution import employer_attribution_conflict
from ..domain.facts import resolve_company_size, size_reject_reason
from ..domain.identity import employer_anchors, employer_key, posting_canonical_key
from ..policy.requirements import excluded_industry, rule
from ..providers.fantastic import (
    FantasticAuthError, FantasticClient, FantasticQuotaError, FantasticRequestError, build_window_params, read_quota,
)
from ..providers.http import TransportTimeout
from .acquisition import DEFAULT_SOURCES, SOURCE_ATS, SOURCE_JOB_BOARDS, SOURCE_SPECS, org_block

FLAG_ENV = "FANTASTIC_DAILY_24H_CANARY"
DAILY_TIME_FRAME = "24h"
#: The four frames the provider documents (docs read 2026-09-05); offset paging applies to 1h/24h/7d.
DOCUMENTED_TIME_FRAMES = ("1h", "24h", "7d", "6m")
#: The production query partitions in production slot order (runner._acquire_balanced / balanced_slots).
EXISTING_PARTITIONS: Tuple[Tuple[str, str], ...] = (
    (SOURCE_JOB_BOARDS, PRIORITY_PROFILE), (SOURCE_ATS, PRIORITY_PROFILE),
    (SOURCE_JOB_BOARDS, DISCOVERY_PROFILE), (SOURCE_ATS, DISCOVERY_PROFILE),
)
#: Safety ceilings for a live canary; the CLI refuses anything larger.
HARD_MAX_RECORDS = 5000
HARD_MAX_REQUESTS = 25
#: Same reserve the production quota guard keeps (config defaults).
MIN_JOBS_QUOTA_REMAINING = 90
MIN_REQUESTS_QUOTA_REMAINING = 20
#: Informational split only; the canary applies the CURRENT deterministic policy unchanged.
FOUR_GROUP_FUNCTIONS = ("engineering", "gtm_revenue", "marketing", "customer_success", "customer_support")
BUCKETS = ("within_page_duplicates", "cross_page_duplicates", "cross_query_duplicates", "historical_previously_seen",
           "canonical_url_duplicates", "canonical_fingerprint_duplicates", "net_new")


#: Provider-confirmed page size and the documented request-length limit.
STRATEGY_LIMIT = 1000
MAX_QUERY_CHARS = 10_000
#: The 24h feed serves jobs with a one-hour enrichment delay (docs, "Recommended Strategy").
ENRICHMENT_DELAY = timedelta(hours=1)
#: First (most relevant) taxonomy only: the recommended acquisition filter.
PRIMARY_TAXONOMIES = ("Technology", "Sales", "Marketing", "Customer Service & Support", "Creative & Media")
#: Every job-board parameter that could make acquisition depend on a title catalogue.
TITLE_PARAMETERS = ("title", "title_advanced", "description", "description_advanced", "exclude_title")
HEADCOUNT = {"organization_headcount_gte": 25, "organization_headcount_lt": 1001}
FULL_TIME = {"ai_employment_type": "FULL_TIME"}
NON_FULL_TIME = "PART_TIME,CONTRACTOR,TEMPORARY,PER_DIEM,INTERN,VOLUNTEER,OTHER"


@dataclass(frozen=True)
class Arm:
    """One query partition of the provider-confirmed strategy: endpoint + static filters (no window, no paging)."""

    key: str
    source: str                      # SOURCE_JOB_BOARDS or SOURCE_ATS
    filters: Tuple[Tuple[str, Any], ...]
    purpose: str
    recovers: str = ""

    @property
    def endpoint(self) -> str:
        return SOURCE_SPECS[self.source].endpoint

    def params(self) -> Dict[str, Any]:
        return dict(self.filters)

    def request(self, *, offset: int, limit: int = STRATEGY_LIMIT) -> Tuple[str, Dict[str, Any]]:
        """The normal daily request: provider-defined ``24h`` window, no ``date_created`` bounds."""
        params = dict(self.filters, time_frame=DAILY_TIME_FRAME, limit=int(limit), offset=int(offset))
        return self.endpoint, params


def _arm(key: str, source: str, params: Mapping[str, Any], purpose: str, recovers: str = "") -> Arm:
    return Arm(key, source, tuple(sorted((k, v) for k, v in params.items() if v not in (None, ""))), purpose, recovers)


def _common(source: str, *, jb_source: str = "", exclude_ats_duplicate: bool = False,
            location: str = "United States") -> Dict[str, Any]:
    params: Dict[str, Any] = {"description_format": "text", "location": location}
    if source == SOURCE_ATS:
        params["include_basic_organization_details"] = "true"
    if jb_source:
        params["source"] = jb_source
    if exclude_ats_duplicate:
        params["exclude_ats_duplicate"] = "true"
    return params


def strategy_arms(*, slug_exclusions: Sequence[str] = (), wf_yc_industry: bool = True, wf_yc_agency: bool = True,
                  ats_missing_profile_industry: bool = True, ats_missing_profile_agency: bool = True,
                  ats_missing_profile_full_time: bool = False) -> Tuple[Arm, ...]:
    """The five record arms of the provider-confirmed canary, in run order (A..E)."""
    corrected = provider_list(approved_industry_query_labels())
    primary = provider_list(PRIMARY_TAXONOMIES)
    _, control = daily_request(SOURCE_JOB_BOARDS, PRIORITY_PROFILE, offset=0, limit=STRATEGY_LIMIT,
                               slug_exclusions=slug_exclusions)
    control = {k: v for k, v in control.items() if k not in {"offset", "limit", "time_frame"}}
    standard = {"organization_agency": "exclude", "exclude_organization_industry": corrected, "ai_taxonomies_a_primary": primary,
                **HEADCOUNT, **FULL_TIME}
    wf_yc = {"ai_taxonomies_a_primary": primary}
    if wf_yc_industry:
        wf_yc["exclude_organization_industry"] = corrected
    if wf_yc_agency:
        wf_yc["organization_agency"] = "exclude"
    missing = {"ai_taxonomies_a_primary": primary}
    if ats_missing_profile_industry:
        missing["exclude_organization_industry"] = corrected
    if ats_missing_profile_agency:
        missing["organization_agency"] = "exclude"
    if ats_missing_profile_full_time:
        missing.update(FULL_TIME)
    return (
        _arm("A_broad_control_jb", SOURCE_JOB_BOARDS, control,
             "current production priority query (ai_taxonomies_a ANY of 16, old industry labels, headcount, FULL_TIME)"),
        _arm("B_primary_ats", SOURCE_ATS, {**_common(SOURCE_ATS), **standard},
             "primary taxonomy on the ATS feed with the standard firmographic and employment filters"),
        _arm("C_primary_linkedin", SOURCE_JOB_BOARDS,
             {**_common(SOURCE_JOB_BOARDS, jb_source="linkedin", exclude_ats_duplicate=True), **standard},
             "primary taxonomy on LinkedIn, ATS twins removed at the provider"),
        _arm("D_wellfound_yc", SOURCE_JOB_BOARDS, {**_common(SOURCE_JOB_BOARDS, jb_source="wellfound,ycombinator"), **wf_yc},
             "Wellfound + Y Combinator without headcount/employment filters (their rows carry no firmographics)",
             "sources the firmographic filters remove entirely; NOT in the provider expired-jobs feed"),
        _arm("E_ats_missing_profile", SOURCE_ATS, {**_common(SOURCE_ATS), **missing},
             "primary-taxonomy ATS without headcount (and, by default, without the employment filter)",
             "ATS jobs whose company profile or employment type is not mapped"),
    )


def count_probes(*, slug_exclusions: Sequence[str] = ()) -> List[Tuple[str, Arm]]:
    """Zero-job-credit count requests for the full 24h inventory of every arm, plus the probes that
    quantify missing-data populations and test whether a filter silently drops rows with missing values."""
    arms = {a.key: a for a in strategy_arms(slug_exclusions=slug_exclusions)}
    primary = provider_list(PRIMARY_TAXONOMIES)
    seven = provider_list(PRIMARY_TAXONOMIES + ("Software", "Data & Analytics"))

    def variant(base: Arm, key: str, *, add: Mapping[str, Any] = (), drop: Sequence[str] = (), purpose: str = "") -> Tuple[str, Arm]:
        params = {k: v for k, v in base.params().items() if k not in set(drop)}
        params.update(dict(add))
        return key, _arm(key, base.source, params, purpose or base.purpose)

    _, ats_control = daily_request(SOURCE_ATS, PRIORITY_PROFILE, offset=0, limit=STRATEGY_LIMIT)
    ats_control = {k: v for k, v in ats_control.items() if k not in {"offset", "limit", "time_frame"}}
    a_ats = _arm("A_broad_control_ats", SOURCE_ATS, ats_control, "current production priority query on the ATS feed")
    b, e = arms["B_primary_ats"], arms["E_ats_missing_profile"]
    d = arms["D_wellfound_yc"]
    return [
        ("A_broad_control_jb", arms["A_broad_control_jb"]),
        ("A_broad_control_ats", a_ats),
        variant(arms["A_broad_control_jb"], "A_jb_primary_only", add={"ai_taxonomies_a_primary": primary},
                purpose="control query restricted to the five primary taxonomies (A ∩ primary)"),
        variant(a_ats, "A_ats_primary_only", add={"ai_taxonomies_a_primary": primary},
                purpose="ATS control restricted to the five primary taxonomies"),
        ("B_primary_ats", b),
        variant(b, "B_headcount_below_25", add={"organization_headcount_lt": 25}, drop=("organization_headcount_gte",),
                purpose="known headcount < 25 (to separate MISSING headcount from out-of-range headcount)"),
        variant(b, "B_headcount_above_1000", add={"organization_headcount_gte": 1001}, drop=("organization_headcount_lt",),
                purpose="known headcount > 1000"),
        ("C_primary_linkedin", arms["C_primary_linkedin"]),
        ("D_wellfound_yc", d),
        variant(d, "D_without_industry_exclusion", drop=("exclude_organization_industry",),
                purpose="tests the claim that the industry exclusion is safe when industry is missing"),
        variant(d, "D_without_agency_filter", drop=("organization_agency",),
                purpose="tests whether organization_agency=exclude drops rows with no agency value"),
        ("E_ats_missing_profile", e),
        variant(e, "E_without_industry_exclusion", drop=("exclude_organization_industry",),
                purpose="tests industry-exclusion safety on ATS rows without a company profile"),
        variant(e, "E_without_agency_filter", drop=("organization_agency",),
                purpose="tests agency-filter safety on ATS rows without a company profile"),
        variant(e, "E_full_time_only", add=dict(FULL_TIME), purpose="E with the employment filter"),
        variant(e, "E_non_full_time_labels", add={"ai_employment_type": NON_FULL_TIME},
                purpose="E rows carrying any non-full-time label (to bound MISSING employment type)"),
        # organization filters that may drop MISSING values: measured, not assumed
        variant(b, "B_without_industry_exclusion", drop=("exclude_organization_industry",),
                purpose="known-profile rows the corrected industry exclusion removes"),
        variant(d, "D_location_and_taxonomy_only", drop=("exclude_organization_industry", "organization_agency"),
                purpose="Wellfound/YC with no organization filter at all"),
        variant(e, "E_no_org_filters", drop=("exclude_organization_industry", "organization_agency"),
                purpose="ATS primary taxonomy with no organization filter (every missing-profile row)"),
        variant(e, "E_no_org_filters_headcount_known", drop=("exclude_organization_industry", "organization_agency"),
                add={"organization_headcount_gte": 1}, purpose="the same rows WITH a known headcount"),
        variant(e, "E_no_org_filters_full_time", drop=("exclude_organization_industry", "organization_agency"),
                add=dict(FULL_TIME), purpose="every missing-profile row, full-time label only"),
        variant(e, "E_no_org_filters_full_time_headcount_known", drop=("exclude_organization_industry", "organization_agency"),
                add={**FULL_TIME, "organization_headcount_gte": 1}, purpose="full-time rows WITH a known headcount"),
        # recall check: engineering jobs whose FIRST taxonomy is Software or Data & Analytics
        variant(b, "B_primary_plus_software_data", add={"ai_taxonomies_a_primary": seven},
                purpose="B with Software and Data & Analytics added to the primary taxonomies"),
        variant(arms["C_primary_linkedin"], "C_primary_plus_software_data", add={"ai_taxonomies_a_primary": seven},
                purpose="C with Software and Data & Analytics added to the primary taxonomies"),
        variant(arms["A_broad_control_jb"], "A_jb_primary_plus_software_data", add={"ai_taxonomies_a_primary": seven},
                purpose="control query restricted to the seven primary taxonomies"),
        variant(a_ats, "A_ats_primary_plus_software_data", add={"ai_taxonomies_a_primary": seven},
                purpose="ATS control restricted to the seven primary taxonomies"),
    ]


def daily_count_window(now: datetime) -> Tuple[datetime, datetime]:
    """The date_created interval the 24h feed serves at ``now``: 24 whole hours ending one hour
    (the enrichment delay) before the current hour."""
    upper = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) - ENRICHMENT_DELAY
    return upper - timedelta(hours=24), upper


def recovery_request(arm: Arm, *, lower: datetime, upper: datetime, now: datetime, offset: int = 0,
                     cursor: Optional[int] = None, limit: int = STRATEGY_LIMIT) -> Tuple[str, Dict[str, Any]]:
    """Failed-run recovery: EXACT ``date_created`` bounds on ``7d`` (offset paging) when it covers the window,
    otherwise ``6m`` with ``cursor`` paging (the provider's recommendation for 6m); the two are never mixed."""
    if not lower < upper:
        raise ValueError("recovery window must be non-empty")
    params = dict(arm.filters, date_created_gte=_iso(lower), date_created_lt=_iso(upper), limit=int(limit))
    if feed_covers_window({"time_frame": "7d"}, lower, upper, now):
        if cursor is not None:
            raise ValueError("7d recovery pages by offset; a cursor would change the ordering")
        params.update(time_frame="7d", offset=int(offset))
    elif feed_covers_window({"time_frame": "6m"}, lower, upper, now):
        if offset:
            raise ValueError("6m recovery pages by cursor; never resume an offset run with a cursor")
        params["time_frame"] = "6m"
        if cursor is not None:
            params["cursor"] = int(cursor)
    else:
        raise ValueError("window is outside the 6m feed")
    return arm.endpoint, params


def query_length(endpoint: str, params: Mapping[str, Any]) -> int:
    """Characters of the percent-encoded query string the provider receives (limit 10,000)."""
    from urllib.parse import urlencode
    return len(endpoint) + 1 + len(urlencode({k: str(v) for k, v in params.items()}))


def flag_enabled(env: Optional[Mapping[str, str]]) -> bool:
    return str((env or {}).get(FLAG_ENV, "") or "").strip() == "1"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def daily_request(source: str, profile: str, *, offset: int, limit: int, sources: Sequence[str] = DEFAULT_SOURCES,
                  location: Optional[str] = "United States", slug_exclusions: Sequence[str] = ()) -> Tuple[str, Dict[str, Any]]:
    """The production request for one query partition with only the window replaced."""
    spec = SOURCE_SPECS[source]
    exclude = spec.supports_exclude_ats_duplicate and SOURCE_ATS in tuple(sources)
    params = build_window_params(lower_iso="", upper_iso="", limit=limit, offset=offset, time_frame=DAILY_TIME_FRAME,
                                 location=location, exclude_ats_duplicate=exclude,
                                 include_basic_organization_details=spec.supports_basic_organization_details)
    params.pop("date_created_gte", None)
    params.pop("date_created_lt", None)
    params.update(profile_filters(profile))
    if slug_exclusions and profile != DISCOVERY_PROFILE:
        params["exclude_organization_slug"] = ",".join(slug_exclusions)
    return spec.endpoint, params


def canonical_url(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def row_fingerprint(row: Mapping[str, Any]) -> Tuple[str, str]:
    """(posting canonical key, employer key) exactly as identity resolution computes them."""
    org = org_block(dict(row))
    domain, slug, nk = employer_anchors({**org, "organization": org.get("organization") or row.get("org_linkedin_name") or ""})
    ekey = employer_key(domain, slug, nk)
    if not ekey:
        return "", ""
    return posting_canonical_key(ekey, row.get("title"), row.get("description_text")), ekey


@dataclass(frozen=True)
class HistoricalRegistry:
    """Read-only snapshot of already-acquired postings. Never written by the canary."""

    provider_keys: frozenset = frozenset()
    urls: frozenset = frozenset()
    canonical_keys: frozenset = frozenset()
    label: str = "none"

    @classmethod
    def empty(cls) -> "HistoricalRegistry":
        return cls()

    @classmethod
    def from_rows(cls, rows: Iterable[Sequence[Any]], *, label: str = "rows") -> "HistoricalRegistry":
        keys, urls, fps = set(), set(), set()
        for source, provider_id, url, ckey in rows:
            if source and provider_id:
                keys.add((str(source), str(provider_id)))
            if canonical_url(url):
                urls.add(canonical_url(url))
            if ckey:
                fps.add(str(ckey))
        return cls(frozenset(keys), frozenset(urls), frozenset(fps), label)

    @classmethod
    def from_file(cls, path: Path) -> "HistoricalRegistry":
        raw = Path(path).read_bytes()
        data = json.loads(raw.decode("utf-8"))
        reg = cls.from_rows(data.get("postings") or [], label=f"{Path(path).name} sha256={hashlib.sha256(raw).hexdigest()}")
        return reg

    def describe(self) -> Dict[str, Any]:
        return {"label": self.label, "provider_keys": len(self.provider_keys), "urls": len(self.urls),
                "canonical_keys": len(self.canonical_keys)}


def count_total(transport, *, base_url: str, api_key: str, endpoint: str, params: Mapping[str, Any],
                timeout: float = 60.0) -> Tuple[Optional[int], Dict[str, Any]]:
    """Zero-job-credit count for one partition. The count endpoint has its own frame set, so
    ``time_frame`` is removed; the caller supplies explicit ``date_created`` bounds."""
    count_endpoint, cparams = count_query(endpoint, {"endpoint": endpoint, **dict(params)})
    cparams.pop("time_frame", None)
    resp = transport.request("GET", base_url.rstrip("/") + count_endpoint,
                             headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                             params=cparams, timeout=timeout)
    meta = {"status": resp.status, "count_endpoint": count_endpoint, "quota": read_quota(resp).to_dict()}
    if resp.status != 200:
        return None, meta
    body = resp.json()
    if isinstance(body, bool):
        return None, meta
    if isinstance(body, (int, float)):
        return int(body), meta
    candidates = [body] if isinstance(body, dict) else ([body[0]] if isinstance(body, list) and body and isinstance(body[0], dict) else [])
    for item in candidates:
        for key in ("count", "total", "jobs", "result"):
            if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool):
                return int(item[key]), meta
    return None, meta


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def qualify_row(row: Mapping[str, Any], *, now: datetime) -> Dict[str, Any]:
    """The current deterministic job + company rules. ``inference=None``: no model is consulted, so
    a posting the rules cannot place is AMBIGUOUS, never approved. Contact/email/approval stages are
    not run, so ``qualified_pre_contact`` is an upper bound on approvals."""
    org = org_block(dict(row))
    employer_name = str(row.get("organization") or row.get("org_linkedin_name") or "")
    fp, ekey = row_fingerprint(row)
    domain = employer_anchors({**org, "organization": org.get("organization") or employer_name})[0]
    out: Dict[str, Any] = {"function": "", "employer_key": ekey}
    valid_through = _parse_dt(row.get("date_valid_through"))
    if valid_through and valid_through < now:
        return {**out, "outcome": "rejected:posting_expired"}
    if employer_attribution_conflict(row.get("description_text"), employer_name=employer_name, employer_domain=domain):
        return {**out, "outcome": "rejected:employer_attribution_conflict"}
    if not ekey:
        return {**out, "outcome": "rejected:employer_identity_unresolved"}
    locations = row.get("locations_derived") or []
    location_text = ", ".join(str(x) for x in locations[:3]) if isinstance(locations, list) else str(locations or "")
    result = classify_posting(
        description=row.get("description_text"), title=row.get("title"), employment_type=row.get("employment_type"),
        ai_employment_type=row.get("ai_employment_type"), location_type=row.get("location_type"),
        countries=[str(c) for c in (row.get("countries_derived") or []) if c], location_text=location_text,
        employer_name=employer_name, agency_flag=_bool(row.get("org_linkedin_recruitment_agency_derived")),
        org_industry=row.get("org_linkedin_industry"), content_hash="", inference=None,
    )
    if result.excluded:
        return {**out, "outcome": f"rejected:{result.exclusion_reason}"}
    if not result.compatible_functions:
        note = next((n for n in result.notes if n.startswith("insufficient_evidence:description_too_short")), "")
        return {**out, "outcome": "rejected:insufficient_evidence:description_too_short" if note
                else "ambiguous:needs_semantic_classifier"}
    function = result.compatible_functions[0]
    out["function"] = function
    anchor = min(d for d in (_parse_dt(row.get("date_posted")), _parse_dt(row.get("date_created")), now) if d is not None)
    if (now - anchor).days > int(rule("approval_max_age_days")):
        return {**out, "outcome": "rejected:posting_too_old"}
    if _bool(row.get("org_linkedin_recruitment_agency_derived")) is True:
        return {**out, "outcome": "rejected:employer_is_agency"}
    industry = excluded_industry(str(row.get("org_linkedin_industry") or ""))
    if industry:
        return {**out, "outcome": f"rejected:employer_excluded_industry:{industry}"}
    # Final whole-branch review, CANARY (2026-09-20): the SAME company-size
    # predicate the live gates use (services/opportunity._size_gate and
    # domain/approval.build_approved_lead both call resolve_company_size), on
    # the SAME rule() bounds. This read org_linkedin_headcount alone while every
    # other stage of this same function had already moved to the three-state
    # policy -- one canary running two different policies, which is worse than
    # either. A conflict is a review bucket, never a reject (Decision 2:
    # never discard a potentially eligible company merely because two sources
    # disagree); only a verdict every populated source agrees on rejects.
    min_employees, max_employees = int(rule("min_employees")), int(rule("max_employees"))
    size_state_value, _size_excerpt, effective = resolve_company_size(
        row.get("org_linkedin_headcount"), row.get("org_linkedin_size"),
        description=str(row.get("description_text") or ""),
        min_employees=min_employees, max_employees=max_employees,
    )
    if size_state_value == "out_of_range":
        return {**out, "outcome": f"rejected:{size_reject_reason(effective, min_employees=min_employees)}"}
    if size_state_value == "unknown_firmographics":
        return {**out, "outcome": "ambiguous:company_size_unknown"}
    if size_state_value == "firmographic_conflict":
        return {**out, "outcome": "ambiguous:firmographic_conflict"}
    return {**out, "outcome": "qualified_pre_contact"}


@dataclass
class _Seen:
    by_key: Dict[Tuple[str, str], str] = field(default_factory=dict)   # (source, id) -> partition label
    urls: set = field(default_factory=set)
    fingerprints: set = field(default_factory=set)


class Daily24hCanary:
    """One in-run traversal of every configured partition. Pure: no database, no persisted cursor."""

    def __init__(self, client: FantasticClient, *, registry: HistoricalRegistry,
                 partitions: Sequence[Tuple[str, str]] = EXISTING_PARTITIONS, limit: int = 100,
                 max_records: int = HARD_MAX_RECORDS, max_requests: int = HARD_MAX_REQUESTS,
                 slug_exclusions: Sequence[str] = (), sources: Sequence[str] = DEFAULT_SOURCES,
                 location: Optional[str] = "United States", requests_already_used: int = 0,
                 count_totals: Optional[Mapping[str, Optional[int]]] = None, last_quota: Optional[Mapping[str, Any]] = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 max_records_per_partition: Optional[int] = None):
        if limit < 1 or max_records < 1 or max_requests < 1:
            raise ValueError("limit and ceilings must be positive")
        client.require_single_physical_attempt()   # one request = one physical call; no hidden retries
        self.client, self.registry = client, registry
        self.partitions = tuple(partitions)
        self.limit, self.max_records, self.max_requests = int(limit), int(max_records), int(max_requests)
        self.slug_exclusions, self.sources, self.location = tuple(slug_exclusions), tuple(sources), location
        self.requests_used = int(requests_already_used)
        self.count_totals = dict(count_totals or {})
        self.last_quota: Dict[str, Any] = dict(last_quota or {})
        self.now = now
        self.records_billed = 0
        self.records_billed_header = 0
        self.seen = _Seen()
        self.new_rows: List[Dict[str, Any]] = []
        self.billed_rows: List[Dict[str, Any]] = []          # EVERY returned row, for per-arm overlap analysis
        self.request_log: List[Dict[str, Any]] = []
        self.max_records_per_partition = max_records_per_partition

    # ------------------------------------------------------------------ guards
    def _ceiling(self) -> str:
        if self.requests_used >= self.max_requests:
            return "request_ceiling"
        if self.records_billed + self.limit > self.max_records:
            return "record_ceiling"
        jr, rr = self.last_quota.get("jobs_remaining"), self.last_quota.get("requests_remaining")
        if rr is not None and rr - 1 < MIN_REQUESTS_QUOTA_REMAINING:
            return "quota_reserve:requests"
        if jr is not None and jr - self.limit < MIN_JOBS_QUOTA_REMAINING:
            return "quota_reserve:jobs"
        return ""

    # ------------------------------------------------------------------ accounting
    def _account(self, source: str, label: str, rows: List[Dict[str, Any]], partition_keys: set) -> Dict[str, int]:
        c = Counter({b: 0 for b in BUCKETS})
        page_ids: set = set()
        for r in rows:
            pid = str(r.get("id") or "")
            key = (source, pid)
            owner = self.seen.by_key.get(key)
            if owner is not None and owner != label:
                c["cross_query_duplicates"] += 1
            elif pid in page_ids:
                c["within_page_duplicates"] += 1
            elif key in partition_keys:
                c["cross_page_duplicates"] += 1
            elif key in self.registry.provider_keys:
                c["historical_previously_seen"] += 1
            else:
                url = canonical_url(r.get("url"))
                fp, _ = row_fingerprint(r)
                if url and (url in self.registry.urls or url in self.seen.urls):
                    c["canonical_url_duplicates"] += 1
                elif fp and (fp in self.registry.canonical_keys or fp in self.seen.fingerprints):
                    c["canonical_fingerprint_duplicates"] += 1
                else:
                    c["net_new"] += 1
                    self.new_rows.append({"source": source, "partition": label, "row": r})
                if url:
                    self.seen.urls.add(url)
                if fp:
                    self.seen.fingerprints.add(fp)
            page_ids.add(pid)
            partition_keys.add(key)
            self.seen.by_key.setdefault(key, label)
        c["ats_duplicate_true"] = sum(1 for r in rows if _bool(r.get("ats_duplicate")) is True)
        return dict(c)

    # ------------------------------------------------------------------ traversal
    def _traverse(self, source: str, profile: str, arm: Optional[Arm] = None) -> Tuple[Dict[str, Any], bool]:
        if arm is not None:
            label, source, profile = arm.key, arm.source, arm.key
            endpoint, frozen = arm.request(offset=0, limit=self.limit)
        else:
            label = f"{source}|{profile}"
            endpoint, frozen = daily_request(source, profile, offset=0, limit=self.limit, sources=self.sources,
                                             location=self.location, slug_exclusions=self.slug_exclusions)
        part: Dict[str, Any] = {"label": label, "source": source, "profile": profile, "endpoint": endpoint,
                                "params": {k: v for k, v in frozen.items() if k != "offset"}, "pages": [],
                                "stop_reason": "", "completed": False, "count_total": self.count_totals.get(label),
                                "purpose": arm.purpose if arm else "", "recovers": arm.recovers if arm else ""}
        signatures: set = set()
        partition_keys: set = set()
        offset = 0
        fatal = False
        part_billed = 0
        while True:
            stop = self._ceiling()
            if stop:
                part["stop_reason"] = stop
                fatal = True
                break
            if self.max_records_per_partition is not None and part_billed + self.limit > self.max_records_per_partition:
                part["stop_reason"] = "partition_record_ceiling"   # this arm only; the next arm still runs
                break
            params = dict(frozen, offset=offset, limit=self.limit)
            self.requests_used += 1
            entry = {"partition": label, "endpoint": endpoint, "params": params, "sent_at": _iso(self.now())}
            self.request_log.append(entry)
            try:
                page = self.client.fetch_page(endpoint, params)
            except TransportTimeout:
                part["stop_reason"], fatal = "provider_error:timeout_uncertain", True
            except FantasticAuthError:
                part["stop_reason"], fatal = "provider_error:auth", True
            except FantasticQuotaError:
                part["stop_reason"], fatal = "provider_error:quota", True
            except FantasticRequestError as exc:
                part["stop_reason"], fatal = f"provider_error:request_error:{exc.code}", True
                entry["response_summary"] = exc.response_summary
            if fatal:
                entry["status"] = part["stop_reason"]
                break
            rows = page.rows
            part_billed += len(rows)
            self.billed_rows.extend({"partition": label, "source": source, "offset": offset, "row": r} for r in rows)
            ids = [str(r.get("id") or "") for r in rows]
            signature = hashlib.sha256("\x1f".join(ids).encode()).hexdigest()
            header = page.quota.jobs_this_request
            self.records_billed += len(rows)
            self.records_billed_header += int(header) if header is not None else 0
            self.last_quota = {k: v for k, v in page.quota.to_dict().items() if v is not None} or self.last_quota
            repeated = bool(ids) and signature in signatures
            signatures.add(signature)
            metrics = self._account(source, label, rows, partition_keys)
            record = {"page_index": len(part["pages"]), "offset": offset, "limit": self.limit, "rows": len(rows),
                      "full_page": len(rows) >= self.limit, "jobs_this_request": header, "signature": signature,
                      "repeated_signature": repeated, "quota": page.quota.to_dict(), **metrics}
            part["pages"].append(record)
            entry.update(status=f"http_{page.status}", rows=len(rows), jobs_this_request=header)
            if repeated:
                part["stop_reason"] = "repeated_page_signature"
                break
            if len(rows) < self.limit:
                total = part["count_total"]
                if total is not None and offset + len(rows) < 0.5 * total:
                    part["stop_reason"] = "short_page_inconsistent_with_count"
                else:
                    part["stop_reason"], part["completed"] = "short_page", True
                break
            offset += len(rows)
        return part, fatal

    def run(self) -> Dict[str, Any]:
        started = self.now()
        parts: List[Dict[str, Any]] = []
        for i, item in enumerate(self.partitions):
            if isinstance(item, Arm):
                part, fatal = self._traverse(item.source, item.key, arm=item)
            else:
                part, fatal = self._traverse(*item)
            parts.append(part)
            if fatal:
                for rest in self.partitions[i + 1:]:
                    key = rest.key if isinstance(rest, Arm) else f"{rest[0]}|{rest[1]}"
                    parts.append({"label": key, "source": rest.source if isinstance(rest, Arm) else rest[0],
                                  "profile": key if isinstance(rest, Arm) else rest[1], "pages": [], "completed": False,
                                  "stop_reason": "not_started", "count_total": self.count_totals.get(key)})
                break
        totals = Counter()
        for part in parts:
            pages = part["pages"]
            part["rows"] = sum(p["rows"] for p in pages)
            part["net_new"] = sum(p["net_new"] for p in pages)
            part["full_pages"] = sum(1 for p in pages if p["full_page"])
            part["partial_pages"] = len(pages) - part["full_pages"]
            for p in pages:
                totals["rows_returned"] += p["rows"]
                for b in BUCKETS + ("ats_duplicate_true",):
                    totals[b] += p[b]
            totals["full_pages"] += part["full_pages"]
            totals["partial_pages"] += part["partial_pages"]
        totals = dict(totals)
        totals["net_new_unique_jobs"] = totals.pop("net_new", 0)
        for b in BUCKETS[:-1] + ("rows_returned", "ats_duplicate_true", "full_pages", "partial_pages"):
            totals.setdefault(b, 0)
        qualification = self._qualify()
        billed = self.records_billed
        report = {
            "mode": "fantastic_daily_24h_canary", "flag": FLAG_ENV, "time_frame": DAILY_TIME_FRAME,
            "started_at": _iso(started), "finished_at": _iso(self.now()), "limit": self.limit,
            "ceilings": {"max_records": self.max_records, "max_requests": self.max_requests},
            "provider_requests": self.requests_used, "records_billed": billed,
            "records_billed_by_header": self.records_billed_header, "last_quota": self.last_quota,
            "partitions": parts, "totals": totals, "qualification": qualification,
            "traversal_complete": len(parts) == len(self.partitions) and all(p.get("completed") for p in parts),
            "production_state_written": False, "registry": self.registry.describe(),
            "slug_exclusions": len(self.slug_exclusions),
            "rates_per_1000_billed": {
                "net_new_unique_jobs": round(1000.0 * totals["net_new_unique_jobs"] / billed, 1) if billed else None,
                "qualified_pre_contact_jobs": round(1000.0 * qualification["qualified_pre_contact_jobs"] / billed, 1) if billed else None,
            },
            "_request_log": self.request_log,
            "_new_rows": self.new_rows,
            "_billed_rows": self.billed_rows,
        }
        return report

    def _qualify(self) -> Dict[str, Any]:
        moment = self.now()
        outcomes: Counter = Counter()
        functions: Counter = Counter()
        opportunities: set = set()
        details: List[Dict[str, Any]] = []
        for item in self.new_rows:
            r = item["row"]
            q = qualify_row(r, now=moment)
            outcomes[q["outcome"]] += 1
            if q["outcome"] == "qualified_pre_contact":
                functions[q["function"]] += 1
                opportunities.add((q["employer_key"], q["function"]))
            details.append({"source": item["source"], "partition": item["partition"], "provider_job_id": str(r.get("id") or ""),
                            "title": str(r.get("title") or "")[:140], "organization": str(r.get("organization") or "")[:100],
                            "url": canonical_url(r.get("url")), "org_linkedin_headcount": r.get("org_linkedin_headcount"),
                            "org_linkedin_industry": r.get("org_linkedin_industry"), "outcome": q["outcome"],
                            "function": q["function"]})
        qualified = outcomes.get("qualified_pre_contact", 0)
        return {
            "definition": "current deterministic job rules (classify_posting, inference=None) + known-fact company gates; "
                          "no contact, email or approval stage, so this is an UPPER BOUND on approvals",
            "reviewed": len(self.new_rows), "qualified_pre_contact_jobs": qualified,
            "qualified_pre_contact_opportunities": len(opportunities),
            "ambiguous": sum(n for k, n in outcomes.items() if k.startswith("ambiguous:")),
            "rejected": sum(n for k, n in outcomes.items() if k.startswith("rejected:")),
            "by_outcome": dict(outcomes.most_common()), "qualified_by_function": dict(functions.most_common()),
            "qualified_in_four_group_functions": sum(n for f, n in functions.items() if f in FOUR_GROUP_FUNCTIONS),
            "model_calls": 0, "_details": details,
        }


def write_artifacts(report: Mapping[str, Any], state_dir: Path) -> Path:
    """Write the evidence into a NEW directory. Refuses to overwrite an earlier canary."""
    state_dir = Path(state_dir)
    if (state_dir / "report.json").exists():
        raise FileExistsError(f"{state_dir} already holds a canary report")
    state_dir.mkdir(parents=True, exist_ok=True)
    public = {k: v for k, v in report.items() if not k.startswith("_")}
    qual = dict(public.get("qualification") or {})
    details = qual.pop("_details", [])
    public["qualification"] = qual
    (state_dir / "report.json").write_text(json.dumps(public, indent=2, default=str), encoding="utf-8")
    with (state_dir / "pages.jsonl").open("w", encoding="utf-8") as fh:
        for part in public.get("partitions") or []:
            for page in part.get("pages") or []:
                fh.write(json.dumps({"partition": part["label"], **page}, default=str) + "\n")
    with (state_dir / "requests_redacted.jsonl").open("w", encoding="utf-8") as fh:
        for entry in report.get("_request_log") or []:
            fh.write(json.dumps(entry, default=str) + "\n")
    with (state_dir / "qualification.jsonl").open("w", encoding="utf-8") as fh:
        for d in details:
            fh.write(json.dumps(d, default=str) + "\n")
    with gzip.open(state_dir / "net_new_rows.jsonl.gz", "wt", encoding="utf-8") as fh:
        for item in report.get("_new_rows") or []:
            fh.write(json.dumps(item, default=str) + "\n")
    if report.get("_billed_rows"):
        with gzip.open(state_dir / "billed_rows.jsonl.gz", "wt", encoding="utf-8") as fh:
            for item in report["_billed_rows"]:
                fh.write(json.dumps(item, default=str) + "\n")
    return state_dir


def render_plan(*, partitions: Sequence[Tuple[str, str]] = EXISTING_PARTITIONS, limit: int, max_records: int,
                max_requests: int, slug_exclusions: Sequence[str] = (), preflight_requests: int = 0) -> Dict[str, Any]:
    """Exact redacted payloads and the offset sequence a traversal would request, assuming full pages."""
    page_budget = max(0, min(max_requests - preflight_requests, max_records // max(1, limit)))
    out: Dict[str, Any] = {"mode": "fantastic_daily_24h_canary (dry run: no request is sent)",
                           "http": {"method": "GET", "auth": "bearer token from FANTASTIC_JOBS_API_KEY (never rendered)"},
                           "time_frame": DAILY_TIME_FRAME, "limit": limit, "max_records": max_records,
                           "max_requests": max_requests, "preflight_count_requests": preflight_requests,
                           "page_budget_if_all_pages_full": page_budget,
                           "order": "partitions are traversed one after another; a later partition receives pages "
                                    "only after the previous one returned a page shorter than limit",
                           "partitions": []}
    for source, profile in partitions:
        endpoint, params = daily_request(source, profile, offset=0, limit=limit, slug_exclusions=slug_exclusions)
        shown = dict(params)
        if "exclude_organization_slug" in shown:
            shown["exclude_organization_slug"] = f"<{len(slug_exclusions)} LinkedIn company slugs>"
        out["partitions"].append({
            "label": f"{source}|{profile}", "endpoint": endpoint, "params": shown,
            "planned_pages": [{"params": {"offset": i * limit, "limit": limit}} for i in range(page_budget)],
        })
    return out


def _load_slug_exclusions(path: str, now: datetime) -> List[str]:
    if not path:
        return []
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    employers = [{"employee_count": r.get("employee_count"), "linkedin_slug": r.get("linkedin_slug"),
                  "created_at": _parse_dt(r.get("created_at"))} for r in rows]
    return recent_size_exclusions(employers, now=now, minimum=int(rule("min_employees")), maximum=int(rule("max_employees")))


def preflight_counts(transport, *, base_url: str, api_key: str, partitions: Sequence[Tuple[str, str]], now: datetime,
                     slug_exclusions: Sequence[str] = ()) -> Tuple[Dict[str, Optional[int]], Dict[str, Any], List[Dict[str, Any]]]:
    """One zero-job-credit count per partition over the last 24h of date_created."""
    totals: Dict[str, Optional[int]] = {}
    log: List[Dict[str, Any]] = []
    last_quota: Dict[str, Any] = {}
    lower, upper = _iso(now - timedelta(hours=24)), _iso(now)
    for source, profile in partitions:
        endpoint, params = daily_request(source, profile, offset=0, limit=100, slug_exclusions=slug_exclusions)
        params = dict(params, date_created_gte=lower, date_created_lt=upper)
        total, meta = count_total(transport, base_url=base_url, api_key=api_key, endpoint=endpoint, params=params)
        label = f"{source}|{profile}"
        totals[label] = total
        quota = {k: v for k, v in (meta.get("quota") or {}).items() if v is not None}
        last_quota = quota or last_quota
        log.append({"partition": label, "count_endpoint": meta["count_endpoint"], "status": meta["status"],
                    "window": [lower, upper], "total": total, "quota": quota})
    return totals, last_quota, log


def cli(args: Any, env: Mapping[str, str]) -> int:
    """``python -m tgtc_core canary-24h``. Requires the flag; live mode also needs --i-understand-spend."""
    if not flag_enabled(env):
        raise SystemExit(f"canary-24h is disabled: set {FLAG_ENV}=1 (off by default)")
    if not 100 <= args.limit <= 1000:
        raise SystemExit("--limit must be within the documented 100-1000 range")
    if not (1 <= args.max_records <= HARD_MAX_RECORDS and 1 <= args.max_requests <= HARD_MAX_REQUESTS):
        raise SystemExit(f"ceilings must be within {HARD_MAX_RECORDS} records and {HARD_MAX_REQUESTS} requests")
    now = datetime.now(timezone.utc)
    slugs = _load_slug_exclusions(args.size_exclusion_employers, now)
    preflight = len(EXISTING_PARTITIONS) if args.preflight_counts else 0
    if args.dry_run:
        print(json.dumps(render_plan(limit=args.limit, max_records=args.max_records, max_requests=args.max_requests,
                                     slug_exclusions=slugs, preflight_requests=preflight), indent=2))
        return 0
    if not args.i_understand_spend:
        raise SystemExit("refusing to call the provider without --i-understand-spend")
    api_key = str(env.get("FANTASTIC_JOBS_API_KEY", "") or "")
    if not api_key:
        raise SystemExit("FANTASTIC_JOBS_API_KEY is not set")
    state_dir = Path(args.state_dir)
    if state_dir.exists() and any(state_dir.iterdir()):
        raise SystemExit(f"{state_dir} is not empty: every canary needs a new evidence directory")
    registry = HistoricalRegistry.from_file(Path(args.registry)) if args.registry else HistoricalRegistry.empty()
    from ..providers.http import RequestsTransport

    transport = RequestsTransport()
    base_url = str(env.get("FANTASTIC_JOBS_BASE_URL", "") or "https://data.fantastic.jobs")
    totals: Dict[str, Optional[int]] = {}
    last_quota: Dict[str, Any] = {}
    count_log: List[Dict[str, Any]] = []
    if args.preflight_counts:
        totals, last_quota, count_log = preflight_counts(transport, base_url=base_url, api_key=api_key,
                                                         partitions=EXISTING_PARTITIONS, now=now, slug_exclusions=slugs)
    # 250-row pages over a whole 24h window can answer slower than production's 100-row hourly pages.
    client = FantasticClient(transport, base_url=base_url, api_key=api_key, timeout=90.0)
    report = Daily24hCanary(client, registry=registry, limit=args.limit, max_records=args.max_records,
                            max_requests=args.max_requests, slug_exclusions=slugs, requests_already_used=len(count_log),
                            count_totals=totals, last_quota=last_quota).run()
    report["preflight_counts"] = count_log
    write_artifacts(report, state_dir)
    summary = {k: report[k] for k in ("provider_requests", "records_billed", "records_billed_by_header",
                                      "traversal_complete", "totals", "rates_per_1000_billed")}
    summary["stops"] = {p["label"]: p["stop_reason"] for p in report["partitions"]}
    summary["qualified_pre_contact_jobs"] = report["qualification"]["qualified_pre_contact_jobs"]
    summary["evidence_dir"] = str(state_dir)
    print(json.dumps(summary, indent=2, default=str))
    return 0 if report["traversal_complete"] else 3


def run_count_matrix(transport, *, base_url: str, api_key: str, now: datetime,
                     probes: Sequence[Tuple[str, Arm]]) -> Dict[str, Any]:
    """Phase 2: zero-job-credit counts over the exact 24h feed window. No job record is requested."""
    lower, upper = daily_count_window(now)
    rows: List[Dict[str, Any]] = []
    for key, arm in probes:
        params = dict(arm.params(), date_created_gte=_iso(lower), date_created_lt=_iso(upper))
        total, meta = count_total(transport, base_url=base_url, api_key=api_key, endpoint=arm.endpoint, params=params)
        shown = {k: (f"<{len(str(v).split(','))} values>" if k in {"exclude_organization_slug", "exclude_organization_industry"}
                     else v) for k, v in params.items()}
        rows.append({"probe": key, "endpoint": meta["count_endpoint"], "status": meta["status"], "total": total,
                     "purpose": arm.purpose, "recovers": arm.recovers, "params": shown,
                     "quota": {k: v for k, v in (meta.get("quota") or {}).items() if v is not None}})
    return {"window": {"date_created_gte": _iso(lower), "date_created_lt": _iso(upper),
                       "rule": "24 whole hours ending one hour before the current hour (enrichment delay)"},
            "measured_at": _iso(now), "probes": rows, "job_records_requested": 0}


def strategy_cli(args: Any, env: Mapping[str, str]) -> int:
    """``python -m tgtc_core canary-24h-strategy``: the provider-confirmed canary. Flag-gated; the records
    phase also needs --i-understand-spend. Counts cost no job credits; records are billed."""
    if not flag_enabled(env):
        raise SystemExit(f"canary-24h-strategy is disabled: set {FLAG_ENV}=1 (off by default)")
    if not (1 <= args.max_records <= HARD_MAX_RECORDS and 1 <= args.max_requests <= HARD_MAX_REQUESTS):
        raise SystemExit(f"ceilings must be within {HARD_MAX_RECORDS} records and {HARD_MAX_REQUESTS} requests")
    if not 1 <= args.max_records_per_arm <= STRATEGY_LIMIT:
        raise SystemExit("--max-records-per-arm must be within 1..1000")
    now = datetime.now(timezone.utc)
    slugs = _load_slug_exclusions(args.size_exclusion_employers, now)
    arms = strategy_arms(slug_exclusions=slugs, wf_yc_industry=args.wf_yc_industry == "on",
                         wf_yc_agency=args.wf_yc_agency == "on", ats_missing_profile_industry=args.ats_missing_industry == "on",
                         ats_missing_profile_agency=args.ats_missing_agency == "on",
                         ats_missing_profile_full_time=args.ats_missing_full_time == "on")
    for arm in arms:
        endpoint, params = arm.request(offset=0)
        if query_length(endpoint, params) > MAX_QUERY_CHARS:
            raise SystemExit(f"{arm.key}: request exceeds {MAX_QUERY_CHARS} characters")
        if any(k in params for k in TITLE_PARAMETERS):
            raise SystemExit(f"{arm.key}: title-dependent acquisition is not allowed")
    if args.dry_run:
        plan = {"mode": "provider-confirmed strategy (dry run: no request is sent)", "limit": STRATEGY_LIMIT,
                "count_window": [_iso(x) for x in daily_count_window(now)],
                "arms": [{"key": a.key, "endpoint": a.endpoint, "purpose": a.purpose, "recovers": a.recovers,
                          "params": {k: (f"<{len(str(v).split(','))} values>" if k in {"exclude_organization_slug",
                                         "exclude_organization_industry"} else v) for k, v in a.request(offset=0)[1].items()},
                          "query_chars": query_length(*a.request(offset=0))} for a in arms],
                "count_probes": [k for k, _ in count_probes(slug_exclusions=slugs)]}
        print(json.dumps(plan, indent=2))
        return 0
    api_key = str(env.get("FANTASTIC_JOBS_API_KEY", "") or "")
    if not api_key:
        raise SystemExit("FANTASTIC_JOBS_API_KEY is not set")
    state_dir = Path(args.state_dir)
    if state_dir.exists() and any(state_dir.iterdir()):
        raise SystemExit(f"{state_dir} is not empty: every canary needs a new evidence directory")
    from ..providers.http import RequestsTransport

    transport = RequestsTransport()
    base_url = str(env.get("FANTASTIC_JOBS_BASE_URL", "") or "https://data.fantastic.jobs")
    if args.phase == "counts":
        matrix = run_count_matrix(transport, base_url=base_url, api_key=api_key, now=now,
                                  probes=count_probes(slug_exclusions=slugs))
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "count_matrix.json").write_text(json.dumps(matrix, indent=2, default=str), encoding="utf-8")
        print(json.dumps({p["probe"]: p["total"] for p in matrix["probes"]}, indent=2))
        return 0 if all(p["total"] is not None for p in matrix["probes"]) else 3
    if not args.i_understand_spend:
        raise SystemExit("refusing to request billed records without --i-understand-spend")
    registry = HistoricalRegistry.from_file(Path(args.registry)) if args.registry else HistoricalRegistry.empty()
    client = FantasticClient(transport, base_url=base_url, api_key=api_key, timeout=120.0)
    selected = [a for a in arms if not args.arms or a.key in set(args.arms.split(","))]
    report = Daily24hCanary(client, registry=registry, partitions=selected, limit=STRATEGY_LIMIT,
                            max_records=args.max_records, max_requests=args.max_requests, slug_exclusions=slugs,
                            max_records_per_partition=args.max_records_per_arm).run()
    report["strategy"] = {"arms": [{"key": a.key, "purpose": a.purpose, "recovers": a.recovers} for a in selected],
                          "limit": STRATEGY_LIMIT, "time_frame": DAILY_TIME_FRAME, "date_created_bounds": False}
    write_artifacts(report, state_dir)
    summary = {k: report[k] for k in ("provider_requests", "records_billed", "records_billed_by_header", "totals")}
    summary["arms"] = {p["label"]: {"rows": p["rows"], "stop": p["stop_reason"]} for p in report["partitions"]}
    summary["evidence_dir"] = str(state_dir)
    print(json.dumps(summary, indent=2, default=str))
    return 0


__all__ = [
    "FLAG_ENV", "DAILY_TIME_FRAME", "DOCUMENTED_TIME_FRAMES", "EXISTING_PARTITIONS", "HistoricalRegistry",
    "Daily24hCanary", "daily_request", "count_total", "qualify_row", "write_artifacts", "render_plan",
    "preflight_counts", "flag_enabled", "cli", "Arm", "STRATEGY_LIMIT", "PRIMARY_TAXONOMIES", "strategy_arms",
    "count_probes", "daily_count_window", "recovery_request", "query_length", "run_count_matrix", "strategy_cli",
]
