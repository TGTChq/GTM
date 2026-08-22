"""Fantastic.jobs additive acquisition adapter.

Isolated source: it fetches `/v1/active-ats` and `/v1/active-jb` (Wellfound, Y
Combinator, LinkedIn segments), maps records into the existing canonical job
schema, and returns a `SourceResult`. It NEVER connects raw responses to
downstream stages, NEVER emits contact PII, NEVER logs the API key or headers,
and fails OPEN at the source level (a source failure skips Fantastic.jobs and the
pipeline continues) while failing CLOSED at the record level (an unsafe record is
rejected, never accepted merely to preserve volume).

Disabled by default: when ``config.FANTASTIC_JOBS_ENABLED`` is falsey no request
is issued and production behaviour is the unchanged baseline.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

import config
from free_job_sources import SourceResult

logger = logging.getLogger(__name__)

_CONTINUATION_SCHEMA = "fantastic-continuation/1"


def _load_continuation_state() -> Dict[str, Any]:
    """Cross-run continuation cursor state (best-effort; a missing/corrupt file
    starts fresh so the first run acquires the newest window)."""
    path = str(getattr(config, "FANTASTIC_JOBS_CONTINUATION_STATE_PATH", "") or "")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data.get("schema") == _CONTINUATION_SCHEMA:
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_continuation_state(state: Dict[str, Any]) -> None:
    path = str(getattr(config, "FANTASTIC_JOBS_CONTINUATION_STATE_PATH", "") or "")
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)  # atomic
    except OSError as exc:  # never fail the run on a state-write error
        logger.warning("Could not persist Fantastic continuation state: %s", type(exc).__name__)


def _advance_iso_second(value: str, seconds: int = 1) -> str:
    """Return ``value`` shifted later by ``seconds`` (used to re-include the
    boundary second in the next ``date_posted_lt`` window)."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return str(value or "")
    return (dt + timedelta(seconds=seconds)).isoformat()


def _parse_time_frame_hours(time_frame: str) -> float:
    """Parse a Fantastic ``time_frame`` like ``24h`` / ``3d`` into hours."""
    m = re.match(r"^\s*(\d+)\s*([hd])\s*$", str(time_frame or "").lower())
    if not m:
        return 24.0
    n = int(m.group(1))
    return n * 24.0 if m.group(2) == "d" else float(n)


def _cursor_is_stale(cursor_date_iso: str, time_frame: str) -> bool:
    """True when a persisted continuation cursor is older than the current
    acquisition window. Resuming from such a cursor (date_posted_lt = cursor+1s)
    would fall entirely below the feed's ``time_frame`` lower bound and silently
    return zero jobs -- so the caller must RESET to a fresh window instead of
    letting stale state suppress a new acquisition window. An unparseable cursor
    is treated as NOT stale (best-effort: never force a reset on a parse glitch)."""
    try:
        dt = datetime.fromisoformat(str(cursor_date_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    hours = _parse_time_frame_hours(time_frame)
    return dt < datetime.now(timezone.utc) - timedelta(hours=hours)


def _family_id(term: str) -> str:
    """Stable slug identifying a title-query family in continuation state."""
    return "".join(c if c.isalnum() else "_" for c in str(term or "").lower()).strip("_")


def _ids_at(jobs: List[Dict[str, Any]], when: str) -> List[str]:
    """Stable IDs of the jobs whose date_posted equals ``when`` (a timestamp
    boundary set, used to dedupe the boundary second on the next run)."""
    return [str(j.get("_fantastic_internal_id"))
            for j in jobs
            if str(j.get("job_posted_at_datetime_utc") or "") == when
            and j.get("_fantastic_internal_id")]


def _cursor_stats(jobs: List[Dict[str, Any]], prior_high: str = "",
                  prior_high_ids: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Compute the continuation cursor from a set of acquired jobs. Tracks BOTH
    edges of the single stream: ``cursor_date`` (oldest acquired -> the DEEP/backward
    resume point) with its boundary IDs, and ``high_water`` (newest ever seen -> the
    FRESH-EDGE stop point) with the boundary IDs at that newest second. The oldest
    always advances older; the newest is monotonic non-decreasing (a run that only
    went deeper preserves the prior high_water AND its IDs). Returns None when no job
    carries a timestamp (an empty run never rewrites either edge)."""
    posted = sorted(str(j.get("job_posted_at_datetime_utc") or "")
                    for j in jobs if j.get("job_posted_at_datetime_utc"))
    if not posted:
        return None
    oldest, newest = posted[0], posted[-1]
    if prior_high and str(prior_high) >= newest:
        # This run acquired nothing newer than the prior fresh edge: keep it (and
        # its boundary IDs) so the next fresh-edge pass still stops at the right
        # second rather than regressing to this run's (older) newest.
        high_water = str(prior_high)
        high_water_ids = list(prior_high_ids or [])
    else:
        high_water = newest
        high_water_ids = _ids_at(jobs, newest)
    return {
        "cursor_date": oldest,
        "high_water": high_water,
        "high_water_ids": high_water_ids,
        "boundary_ids": _ids_at(jobs, oldest),
        "acquired_this_run": len(jobs),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

# Contact PII that must never enter canonical records, logs, artifacts or Airtable.
_PII_FIELDS = frozenset({
    "recruiter_name", "recruiter_title", "recruiter_url", "recruiter_email",
    "ai_hiring_manager_name", "ai_hiring_manager_email_address",
    "hiring_manager_name", "hiring_manager_email", "contact_email",
    "contact_phone", "phone", "personal_linkedin", "linkedin_profile",
})
_PII_SUBSTRINGS = ("recruiter", "hiring_manager", "email", "phone", "contact_")

# Segment definitions: (endpoint, source label, response-source match keys).
ATS_SOURCE = "fantastic_jobs_ats"
JB_SEGMENTS = {
    "wellfound": ("fantastic_jobs_wellfound", ("wellfound", "angellist", "angel.co")),
    "ycombinator": ("fantastic_jobs_ycombinator", ("ycombinator", "y combinator", "workatastartup")),
    "linkedin": ("fantastic_jobs_linkedin", ("linkedin",)),
}


@dataclass
class _QuotaState:
    jobs_remaining: Optional[int] = None
    requests_remaining: Optional[int] = None
    jobs_consumed: int = 0
    requests_consumed: int = 0
    stop_reason: str = ""


HttpGet = Callable[..., Any]


def _http_get(url: str, headers: Dict[str, str], params: Dict[str, Any], timeout: int):
    """Thin wrapper so tests can inject a fake; real path uses requests.get."""
    return requests.get(url, headers=headers, params=params, timeout=timeout)


def _is_pii(key: str) -> bool:
    k = str(key or "").lower()
    if k in _PII_FIELDS:
        return True
    return any(sub in k for sub in _PII_SUBSTRINGS)


def _strip_pii(record: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    clean: Dict[str, Any] = {}
    dropped = 0
    for key, value in record.items():
        if _is_pii(key):
            dropped += 1
            continue
        clean[key] = value
    return clean, dropped


def _host(url: str) -> str:
    try:
        host = urlparse(str(url or "").strip()).hostname or ""
    except (ValueError, TypeError):
        return ""
    return host.lower().removeprefix("www.")


def _intermediary_hosts() -> Tuple[str, ...]:
    raw = getattr(config, "INTERMEDIARY_JOB_DOMAINS", ()) or ()
    return tuple(str(x).lower() for x in raw)


def _defensible_domain(record: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Deterministic employer-domain precedence. Never inferred from name.

    Precedence: organization_url -> domain_derived -> org_linkedin_website.
    An ATS/intermediary/social host is not an employer domain and is rejected;
    the raw candidates are preserved (sanitized) for diagnostics.
    """
    intermediaries = _intermediary_hosts()
    candidates_raw = [
        str(record.get("organization_url") or "").strip(),
        str(record.get("domain_derived") or "").strip(),
        str(record.get("org_linkedin_website") or "").strip(),
    ]
    diagnostics = [c for c in candidates_raw if c]
    for candidate in candidates_raw:
        if not candidate:
            continue
        host = _host(candidate) or (candidate.lower() if "." in candidate and "/" not in candidate else "")
        if not host or "." not in host:
            continue
        if any(host == bad or host.endswith("." + bad) for bad in intermediaries):
            continue
        if not all(part.isascii() for part in host):
            continue
        return host, diagnostics
    return "", diagnostics


def _is_us(record: Dict[str, Any]) -> bool:
    countries = record.get("countries_derived") or record.get("countries") or []
    if isinstance(countries, str):
        countries = [countries]
    for c in countries:
        cl = str(c).strip().lower()
        if cl in {"us", "usa", "united states", "united states of america"}:
            return True
    locs = record.get("locations_derived") or record.get("locations") or []
    if isinstance(locs, str):
        locs = [locs]
    for loc in locs:
        if "united states" in str(loc).lower() or ", us" in str(loc).lower():
            return True
    return False


def _location_text(record: Dict[str, Any]) -> str:
    for key in ("locations_derived", "locations", "cities_derived", "regions_derived"):
        v = record.get(key)
        if isinstance(v, list) and v:
            return ", ".join(str(x) for x in v[:3] if str(x).strip())
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _safe_iso(value: Any) -> str:
    """Return a clean ISO-8601 string or '' — never a relative/human label.

    The existing Airtable payload builder (post PR #25) then renders it safely.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        from datetime import datetime, timezone
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    raw = str(value).strip()
    if not raw:
        return ""
    # Reject obvious human-relative strings; only accept parseable ISO.
    lowered = raw.lower()
    if any(tok in lowered for tok in ("ago", "yesterday", "today", "posted", "day", "hour", "week", "month")):
        # unless it is a real ISO timestamp that merely contains these letters
        pass
    from datetime import datetime
    candidate = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).isoformat()
    except ValueError:
        return ""


def _description(record: Dict[str, Any]) -> Tuple[str, bool]:
    """Return (description_text, is_ai_summary). Never fabricate; AI summaries are
    only used as a marked fallback, never passed off as the original description."""
    for key in ("description_text", "description"):
        v = record.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip(), False
    return "", False


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _employment_type(value: Any) -> str:
    """Normalize the provider ``employment_type`` to a clean scalar token.

    The Fantastic API returns this field as an array (e.g. ``["FULL_TIME"]``);
    naively stringifying it stored ``"['FULL_TIME']"`` in the canonical
    Employment Type field. Take the first element and never serialize a list.
    """
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip().upper() or "FULLTIME"


def map_record(record: Dict[str, Any], source_label: str, seg: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Map one Fantastic.jobs record into the canonical schema.

    Returns (job, "") on success or (None, reason) when the record is rejected
    (fail-closed at the identity level). PII is dropped before anything else; the
    dropped-field count is accumulated into ``seg['pii_dropped']`` when provided.
    """
    if not isinstance(record, dict):
        return None, "not_a_record"
    record, _dropped = _strip_pii(record)
    if seg is not None and _dropped:
        seg["pii_dropped"] = seg.get("pii_dropped", 0) + _dropped

    job_id = str(record.get("id") or "").strip()
    if not job_id:
        return None, "missing_stable_id"
    title = str(record.get("title") or "").strip()
    employer = str(record.get("organization") or record.get("org_linkedin_name") or "").strip()
    if not title or not employer:
        return None, "missing_identity"

    domain, domain_candidates = _defensible_domain(record)
    description, ai_desc = _description(record)
    url = str(record.get("url") or "").strip()

    job = {
        "job_id": f"fantastic_{job_id}",
        "job_title": title,
        "employer_name": employer,
        "employer_website": domain,
        # Preserve source identity/display candidates independently.  Downstream
        # resolvers may use them as evidence, but must never rewrite the canonical
        # employer identity above.
        "organization": str(record.get("organization") or "").strip(),
        "organization_url": str(record.get("organization_url") or "").strip(),
        "org_linkedin_name": str(record.get("org_linkedin_name") or "").strip(),
        "org_linkedin_slug": str(record.get("org_linkedin_slug") or "").strip(),
        "org_linkedin_website": str(record.get("org_linkedin_website") or "").strip(),
        "domain_derived": str(record.get("domain_derived") or "").strip(),
        "job_publisher": source_label,
        "job_description": description,
        "job_apply_link": url,
        "job_apply_is_direct": False,
        "job_google_link": "",
        "job_location": _location_text(record),
        "job_country": (str((record.get("countries_derived") or [""])[0]) if isinstance(record.get("countries_derived"), list) else str(record.get("countries_derived") or "")).strip(),
        "job_is_remote": str(record.get("location_type") or "").strip().lower() == "remote",
        "job_employment_type": _employment_type(record.get("employment_type")),
        "job_posted_at_datetime_utc": _safe_iso(record.get("date_posted") or record.get("date_created")),
        "job_offer_expiration_datetime_utc": _safe_iso(record.get("date_valid_through")),
        "job_min_salary": record.get("ai_salary_min_value") or record.get("salary_min"),
        "job_max_salary": record.get("ai_salary_max_value") or record.get("salary_max"),
        "job_salary_currency": str(record.get("ai_salary_currency") or "").strip(),
        "job_salary_period": str(record.get("ai_salary_unit_text") or "").strip(),
        "job_required_skills": [str(s) for s in (record.get("ai_key_skills") or []) if str(s or "").strip()],
        "apply_options": ([{"publisher": source_label, "apply_link": url, "is_direct": False}] if url else []),
        "canonical_source_url": url,
        "_acquisition_source": source_label,
        "_provider_record_structured": True,
        # sanitized diagnostics / observability (no PII)
        "_fantastic_internal_id": job_id,
        "_fantastic_source": str(record.get("source") or "").strip().lower(),
        "_fantastic_source_type": str(record.get("source_type") or "").strip().lower(),
        "_fantastic_us_location": _is_us(record),
        "_fantastic_ai_description_only": ai_desc,
        "_org_headcount": record.get("org_linkedin_headcount"),
        "_org_size": str(record.get("org_linkedin_size") or "").strip(),
        "_org_industry": str(record.get("org_linkedin_industry") or "").strip(),
        "_staffing_agency_flag": _bool(record.get("org_linkedin_recruitment_agency_derived")),
        "_ats_duplicate": _bool(record.get("ats_duplicate")),
        "_domain_candidates": domain_candidates,
        # Provider index time (assign-once; the date_created watermark key) kept
        # separately from date_posted so the two clocks are never conflated.
        "_fantastic_date_created": _safe_iso(record.get("date_created")),
        "_fantastic_date_posted": _safe_iso(record.get("date_posted")),
        # Canonical provider provenance: which Fantastic dataset this row came from.
        "_provider_dataset": "ats" if source_label == ATS_SOURCE else "jb",
    }
    return job, ""


def _jb_filter_params() -> Dict[str, Any]:
    """ICP filters pushed into the /v1/active-jb request.

    Only parameters confirmed supported by the live Direct API contract are sent
    (``location``, ``organization_headcount_gte``, ``ai_employment_type``,
    ``organization_agency=exclude``). The API exposes no headcount-maximum
    parameter, so the upper bound is enforced downstream (config.MAX_EMPLOYEES).
    Values are configuration, never guessed at call time.
    """
    params: Dict[str, Any] = {}
    location = str(getattr(config, "FANTASTIC_JOBS_LOCATION", "") or "").strip()
    if location:
        params["location"] = location
    headcount_min = int(getattr(config, "FANTASTIC_JOBS_HEADCOUNT_MIN", 0) or 0)
    if headcount_min > 0:
        params["organization_headcount_gte"] = headcount_min
    headcount_max = int(getattr(config, "FANTASTIC_JOBS_HEADCOUNT_MAX", 0) or 0)
    if headcount_max > 0:
        params["organization_headcount_lt"] = headcount_max
    employment = str(getattr(config, "FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE", "") or "").strip()
    if employment:
        params["ai_employment_type"] = employment
    if getattr(config, "FANTASTIC_JOBS_EXCLUDE_AGENCY", True):
        params["organization_agency"] = "exclude"
    return params


def _title_advanced_term(role: str) -> str:
    """Normalize a role into a title_advanced term (single-quote multi-word).

    NOTE: a quoted multi-word term is a tsquery PHRASE (all tokens, adjacent). A
    catalog title containing "/" (e.g. "UX/UI Designer") therefore collapses into
    an unintended 3-token AND phrase that real titles never satisfy; such roles get
    explicit aliases via ``config.FANTASTIC_TITLE_ALIASES`` (see ``_role_terms``).
    """
    cleaned = " ".join(str(role or "").lower().replace("/", " ").replace("-", " ").split())
    return f"'{cleaned}'" if " " in cleaned else cleaned


def _negation_term(token: str) -> str:
    """A tsquery negation for one contaminant: ``!'multi word'`` or ``!word``.
    PROVEN live (2026-08-22 count probe): only ``& !term`` is honored; ``-term`` and
    ``NOT term`` are rejected (HTTP 400) and are never emitted."""
    t = " ".join(str(token or "").lower().replace("/", " ").replace("-", " ").split())
    if not t:
        return ""
    return f"!'{t}'" if " " in t else f"!{t}"


def _role_terms(role: str) -> List[str]:
    """All inclusion terms for one catalog role: the normalized role plus any
    configured recall aliases (deduplicated, order-stable)."""
    # Aliases are FLAG-GATED (config.FANTASTIC_TITLE_ALIASES_ENABLED, default OFF):
    # even a pure-recall change alters the live billing query, so with the flag off
    # the expression is byte-identical to the proven production 118-term form (Gate-E D1).
    aliases = (getattr(config, "FANTASTIC_TITLE_ALIASES", {}) or {}) if bool(
        getattr(config, "FANTASTIC_TITLE_ALIASES_ENABLED", False)) else {}
    raw = [role] + [a for a in (aliases.get(role) or []) if str(a or "").strip()]
    out: List[str] = []
    seen: set = set()
    for r in raw:
        t = _title_advanced_term(r)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _role_family_id(role: str) -> str:
    return _family_id(role)


def build_title_query_plan() -> Dict[str, Any]:
    """Build the production ``title_advanced`` expression AND its attribution map.

    Returns ``{"expression": str, "clauses": [{"family": id, "role": str,
    "include": [terms], "exclude": [negations], "clause": str}], "global_exclusions":
    [...], "fingerprint": sha}``. Each role becomes ONE clause:

        ( 'term a' | 'term b' [ & !contaminant ... ] )

    joined by ``|``. Scoped negation (``config.FANTASTIC_TITLE_SCOPED_EXCLUSIONS``)
    is applied ONLY inside the clause of the role it was proven safe for, never
    globally; ``config.FANTASTIC_TITLE_GLOBAL_EXCLUSIONS`` wraps the whole union as
    ``( union ) & !term`` and is reserved for tokens proven collision-free across
    every FINAL_PASS title. A configured override expression wins verbatim (no
    attribution possible for an opaque override).
    """
    override = str(getattr(config, "FANTASTIC_JOBS_TITLE_ADVANCED_EXPRESSION", "") or "").strip()
    if override:
        return {"expression": override, "clauses": [], "global_exclusions": [],
                "fingerprint": _fingerprint(override), "override": True}
    try:
        from role_catalog import DEFAULT_ACQUISITION_ROLES
    except Exception:  # noqa: BLE001 - never let a missing catalog crash acquisition
        return {"expression": "", "clauses": [], "global_exclusions": [], "fingerprint": ""}
    scoped_on = bool(getattr(config, "FANTASTIC_TITLE_SCOPED_EXCLUSIONS_ENABLED", False))
    scoped = (getattr(config, "FANTASTIC_TITLE_SCOPED_EXCLUSIONS", {}) or {}) if scoped_on else {}
    global_on = bool(getattr(config, "FANTASTIC_TITLE_GLOBAL_EXCLUSIONS_ENABLED", False))
    global_neg = [n for n in ((getattr(config, "FANTASTIC_TITLE_GLOBAL_EXCLUSIONS", []) or [])
                              if global_on else []) if str(n or "").strip()]

    clauses: List[Dict[str, Any]] = []
    seen_terms: set = set()
    for role in sorted({str(r).strip() for r in DEFAULT_ACQUISITION_ROLES if str(r).strip()}):
        include = [t for t in _role_terms(role) if t not in seen_terms]
        if not include:
            continue
        seen_terms.update(include)
        exclude = [n for n in (_negation_term(x) for x in (scoped.get(role) or [])) if n]
        inner = " | ".join(include)
        if exclude:
            # Scoped: negate INSIDE this role's clause only.
            clause = f"({inner}) & " + " & ".join(exclude) if len(include) > 1 else f"{inner} & " + " & ".join(exclude)
            clause = f"({clause})"
        else:
            clause = inner if len(include) == 1 else f"({inner})"
        clauses.append({"family": _role_family_id(role), "role": role, "include": include,
                        "exclude": exclude, "clause": clause})
    union = " | ".join(c["clause"] for c in clauses)
    gneg = [n for n in (_negation_term(x) for x in global_neg) if n]
    expression = f"({union}) & " + " & ".join(gneg) if (union and gneg) else union
    return {"expression": expression, "clauses": clauses, "global_exclusions": gneg,
            "fingerprint": _fingerprint(expression), "override": False}


def _fingerprint(text: str) -> str:
    import hashlib
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _title_advanced_expression() -> str:
    """The Boolean OR-expression over the whole role catalog (benchmark parity)."""
    return build_title_query_plan()["expression"]


def attribute_title_family(job_title: str, plan: Dict[str, Any]) -> str:
    """Best-effort attribution of an acquired job title to the query family whose
    inclusion term(s) it matches (token-subset match mirroring tsquery phrase
    semantics). Returns the family id or "" when no clause matches (e.g. an
    opaque override expression)."""
    import re as _re
    title_tokens = _re.findall(r"[a-z0-9&]+", str(job_title or "").lower())
    title_str = " " + " ".join(title_tokens) + " "
    best = ""
    best_len = 0
    for c in plan.get("clauses") or []:
        for term in c.get("include") or []:
            phrase = term.strip("'")
            if f" {phrase} " in title_str and len(phrase) > best_len:
                best, best_len = c["family"], len(phrase)
    return best


def _server_industry_exclusions() -> List[str]:
    """Exact Fantastic taxonomy labels to send as ``exclude_organization_industry``
    (PROVEN param, live count probe 2026-08-22). Labels are NEVER derived from
    Apollo keyword lists; only configured exact strings are sent."""
    if not bool(getattr(config, "FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED", False)):
        return []
    raw = getattr(config, "FANTASTIC_EXCLUDED_ORG_INDUSTRIES", []) or []
    out: List[str] = []
    seen: set = set()
    for label in raw:
        s = str(label or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _apply_server_industry_exclusions(params: Dict[str, Any]) -> Dict[str, Any]:
    """Attach ``exclude_organization_industry`` (multi-value as repeated params, the
    OpenAPI array encoding) and record attribution in the params' sidecar."""
    labels = _server_industry_exclusions()
    if labels:
        params["exclude_organization_industry"] = labels if len(labels) > 1 else labels[0]
    return params


def provider_filter_attribution() -> Dict[str, Any]:
    """Metadata describing the upstream (pre-billing) exclusions in force, so a
    later analysis can attribute "jobs not purchased" WITHOUT pretending those rows
    were returned. Stored in run metadata + the yield ledger header."""
    plan = build_title_query_plan()
    labels = _server_industry_exclusions()
    return {
        "provider_filter_industry": bool(labels),
        "industry_labels": list(labels),
        "industry_config_fingerprint": _fingerprint("|".join(labels)),
        "title_query_fingerprint": plan.get("fingerprint", ""),
        "title_global_exclusions": list(plan.get("global_exclusions") or []),
        "title_scoped_exclusion_families": [c["family"] for c in (plan.get("clauses") or []) if c.get("exclude")],
    }


def _read_quota(headers: Any) -> Dict[str, Optional[int]]:
    def geti(name: str) -> Optional[int]:
        try:
            raw = headers.get(name)
            return int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None
    out: Dict[str, Any] = {
        "jobs_limit": geti("x-api-jobs-limit"),
        "jobs_remaining": geti("x-api-jobs-remaining"),
        "requests_limit": geti("x-api-requests-limit"),
        "requests_remaining": geti("x-api-requests-remaining"),
    }
    # Billing reset date (string header; used by the monthly governor). Absent on
    # some responses -> None; never treated as an error.
    try:
        nbd = headers.get("x-api-next-billing-date")
        out["next_billing_date"] = str(nbd).strip() if nbd not in (None, "") else None
    except (TypeError, AttributeError):
        out["next_billing_date"] = None
    return out


class FantasticAuthError(Exception):
    pass


class FantasticQuotaError(Exception):
    pass


class FantasticRequestError(Exception):
    """Structured, sanitized request failure. Carries the failure stage, a safe
    error code, and the HTTP status when a response was received. Never contains
    the API key, Authorization header, request headers, raw body or PII."""

    def __init__(self, stage: str, code: str, status: Optional[int] = None, retries: int = 0):
        self.stage = stage    # dispatch | http_response | json_parsing | schema
        self.code = code      # network_error:<ExcClass> | http_<status> | malformed_json | unexpected_schema
        self.status = status  # HTTP status if a response was received, else None
        self.retries = retries
        super().__init__(code)


def _request(endpoint: str, params: Dict[str, Any], http_get: HttpGet, seg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Optional[int]]]:
    """One bounded request with retries. Records dispatch/status/retries into
    ``seg`` even on failure. Raises FantasticAuthError (401/403),
    FantasticQuotaError (429), or FantasticRequestError (structured). Never logs
    the key or headers."""
    url = f"{config.FANTASTIC_JOBS_BASE_URL.rstrip('/')}{endpoint}"
    headers = {"Authorization": f"Bearer {config.FANTASTIC_JOBS_API_KEY}"}
    attempts = max(1, config.FANTASTIC_JOBS_MAX_RETRIES + 1)
    last_err = ""
    for attempt in range(attempts):
        seg["dispatched"] = True          # a network dispatch was attempted
        seg["retries"] = attempt
        try:
            resp = http_get(url, headers=headers, params=params, timeout=config.FANTASTIC_JOBS_REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:  # network/timeout/DNS (class name only; never the message)
            last_err = type(exc).__name__
            if attempt + 1 < attempts:
                time.sleep(min(4.0, (2 ** attempt) * 0.2) + random.random() * 0.1)
                continue
            raise FantasticRequestError("dispatch", f"network_error:{last_err}", None, attempt) from None
        status = getattr(resp, "status_code", None)
        seg["http_status"] = status       # record ACTUAL status, even on non-200
        if status in (401, 403):
            raise FantasticAuthError(f"auth_failed_status_{status}")
        if status == 429:
            raise FantasticQuotaError("rate_limited_429")
        if status in (408, 500, 502, 503, 504) and attempt + 1 < attempts:
            time.sleep(min(4.0, (2 ** attempt) * 0.2) + random.random() * 0.1)
            continue
        if status != 200:
            raise FantasticRequestError("http_response", f"http_{status}", status, attempt)
        quota = _read_quota(getattr(resp, "headers", {}) or {})
        try:
            payload = resp.json()
        except Exception:
            raise FantasticRequestError("json_parsing", "malformed_json", status, attempt) from None
        rows = payload if isinstance(payload, list) else (payload.get("jobs") or payload.get("data") or payload.get("results") or [])
        if not isinstance(rows, list):
            raise FantasticRequestError("schema", "unexpected_schema", status, attempt)
        return rows, quota
    raise FantasticRequestError("dispatch", f"network_error:{last_err}", None, attempts - 1)


def _effective_run_cap() -> int:
    """The maximum jobs this acquisition CALL may bill: the global validated ceiling
    (FANTASTIC_JOBS_MAX_JOBS_PER_RUN), further clamped to the per-iteration runtime
    slice budget (FANTASTIC_JOBS_RUN_SLICE_CAP) when the top-up loop has set one.
    Decoupled from the global so a slice smaller than a segment limit is valid: the
    config validator still checks segment_total <= the GLOBAL ceiling, while the fetch
    loop clamps this iteration's billing to the slice."""
    ceiling = int(getattr(config, "FANTASTIC_JOBS_MAX_JOBS_PER_RUN", 0) or 0)
    slice_cap = int(getattr(config, "FANTASTIC_JOBS_RUN_SLICE_CAP", 0) or 0)
    return min(ceiling, slice_cap) if slice_cap > 0 else ceiling


def _quota_would_breach(quota: _QuotaState, want: int) -> str:
    if quota.requests_remaining is not None and (quota.requests_remaining - 1) < config.FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING:
        return "requests_quota_reserve"
    if quota.jobs_remaining is not None and (quota.jobs_remaining - want) < config.FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING:
        return "jobs_quota_reserve"
    return ""


def _fetch_segment(endpoint: str, base_params: Dict[str, Any], source_label: str, cap: int,
                   quota: _QuotaState, http_get: HttpGet, seen_ids: set,
                   metrics: Dict[str, Any], accept_source: Optional[Tuple[str, ...]] = None,
                   stop_before_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """When ``stop_before_date`` is set (fresh-edge/head pass), the DESC feed is
    paged from the top and paging STOPS as soon as a job older than that timestamp
    is seen -- so only jobs newer than the prior high_water are collected. Jobs AT
    the boundary second flow through the normal ``seen_ids`` dedupe (the caller seeds
    it with the persisted high_water boundary IDs), so already-acquired boundary jobs
    are skipped while genuinely new same-second siblings are still kept."""
    jobs: List[Dict[str, Any]] = []
    boundary_hit = False
    seg = metrics["segments"].setdefault(source_label, {
        "attempted": 0, "requests_succeeded": 0, "returned": 0, "schema_valid": 0,
        "schema_rejected": 0, "pii_dropped": 0, "non_us": 0, "source_filtered_out": 0,
        "duplicates": 0, "stop_reason": "", "http_status": None, "dispatched": False,
        "retries": 0, "failure_stage": "", "error_code": "", "error_class": "",
    })
    page = 1
    fingerprints: set = set()
    # BILLING-ACCURATE CAP (Gate-A BUG 6 / Gate-D): the provider bills every RETURNED
    # row, including rows we then schema-reject, source-filter or dedupe. The cap
    # therefore bounds rows RETURNED this call (``returned``), not rows KEPT -- so a
    # governor/top-up budget of N can never be exceeded by a dup-heavy page run.
    # Kept jobs are always <= returned, so ``len(jobs) < cap`` remains implied.
    returned = 0
    while returned < cap:
        want = min(cap - returned, 100)
        breach = _quota_would_breach(quota, want)
        if breach:
            seg["stop_reason"] = breach
            quota.stop_reason = breach
            break
        params = dict(base_params)
        params.update({"limit": want, "offset": (page - 1) * want})
        seg["attempted"] += 1
        try:
            rows, q = _request(endpoint, params, http_get, seg)
        except FantasticRequestError as exc:
            # Per-segment fail-open: record sanitized diagnostics and stop THIS
            # segment; other segments and the rest of the pipeline continue.
            seg["failure_stage"] = exc.stage
            seg["error_code"] = exc.code
            seg["error_class"] = "FantasticRequestError"
            seg["retries"] = exc.retries
            seg["stop_reason"] = seg["stop_reason"] or "request_error"
            break
        quota.requests_consumed += 1
        seg["requests_succeeded"] += 1
        if q.get("jobs_remaining") is not None:
            quota.jobs_remaining = q["jobs_remaining"]
        if q.get("requests_remaining") is not None:
            quota.requests_remaining = q["requests_remaining"]
        if q.get("next_billing_date"):
            metrics["next_billing_date"] = q["next_billing_date"]
        if not rows:
            seg["stop_reason"] = seg["stop_reason"] or "empty_page"
            break
        fp = tuple(str(r.get("id")) for r in rows if isinstance(r, dict))
        if fp and fp in fingerprints:
            seg["stop_reason"] = "repeated_page"
            break
        fingerprints.add(fp)
        new_ids = 0
        for record in rows:
            seg["returned"] += 1
            quota.jobs_consumed += 1
            returned += 1
            job, reason = map_record(record, source_label, seg)
            if job is None:
                seg["schema_rejected"] += 1
                continue
            if accept_source is not None:
                src = job.get("_fantastic_source", "")
                if not any(tok in src for tok in accept_source):
                    seg["source_filtered_out"] += 1
                    continue
            if stop_before_date:
                posted_at = str(job.get("job_posted_at_datetime_utc") or "")
                if posted_at and posted_at < stop_before_date:
                    # Crossed below the prior fresh edge -> everything further down
                    # this DESC feed is already covered; stop the fresh-edge pass.
                    seg["stop_reason"] = seg["stop_reason"] or "head_boundary"
                    boundary_hit = True
                    break
            if job["job_id"] in seen_ids:
                seg["duplicates"] += 1
                continue
            seen_ids.add(job["job_id"])
            new_ids += 1
            seg["schema_valid"] += 1
            if not job.get("_fantastic_us_location"):
                seg["non_us"] += 1
            jobs.append(job)
            if len(jobs) >= cap:
                break
        if boundary_hit:
            break
        if returned >= cap:
            # Cap reached with a FULL page: the feed may hold more rows. Record it
            # explicitly so a date_created watermark never treats a cap-truncated
            # window as drained (Gate-B 5A). A short page below means exhaustion.
            if len(rows) >= want:
                seg["stop_reason"] = seg["stop_reason"] or "cap_reached"
            break
        if len(rows) < want:
            seg["stop_reason"] = seg["stop_reason"] or "short_page"
            break
        if new_ids == 0:
            seg["stop_reason"] = seg["stop_reason"] or "no_new_ids"
            break
        page += 1
        if page > max(1, int(getattr(config, "FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT", 50))):
            seg["stop_reason"] = "page_cap"
            break
    return jobs


def build_ats_params(title_advanced_expr: str) -> Dict[str, Any]:
    """``/v1/active-ats`` request with filter PARITY to the LinkedIn stream: the same
    role universe (title_advanced; PROVEN accepted by active-ats-count), the same
    US/headcount/full-time/agency ICP filters (all PROVEN accepted, 0 dropped) and
    the same server-side industry exclusions. Flags allow either parity layer to be
    switched off for a controlled A/B."""
    params: Dict[str, Any] = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                              "include_basic_organization_details": "true"}
    if bool(getattr(config, "FANTASTIC_ATS_APPLY_TITLE_ADVANCED", True)) and title_advanced_expr:
        params["title_advanced"] = title_advanced_expr
    if bool(getattr(config, "FANTASTIC_ATS_APPLY_ICP_FILTERS", True)):
        params.update(_jb_filter_params())
    _apply_server_industry_exclusions(params)
    return params


# NOTE (Gates B+E): a local "employer|title" cross-source posting identity was
# considered and REJECTED -- it would collapse two legitimately distinct openings
# (e.g. "Account Executive" NYC vs SF) and is redundant with the provider-side
# exclude_ats_duplicate=true de-twinning. ATS x JB twins are prevented at the
# provider; downstream company x function grouping handles enrichment dedupe.


def _save_quota_snapshot(quota: "_QuotaState", metrics: Dict[str, Any]) -> None:
    """Persist the last-known provider quota headers so the NEXT run's governor can
    read remaining credits / reset date without a row-producing call. Best-effort."""
    path = str(getattr(config, "FANTASTIC_QUOTA_SNAPSHOT_PATH", "") or "")
    if not path or quota.jobs_remaining is None:
        return
    snap = {"schema": "fantastic-quota-snapshot/1",
            "jobs_remaining": quota.jobs_remaining,
            "requests_remaining": quota.requests_remaining,
            "next_billing_date": metrics.get("next_billing_date", ""),
            "captured_at": datetime.now(timezone.utc).isoformat()}
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("quota snapshot not persisted: %s", type(exc).__name__)


def load_quota_snapshot() -> Dict[str, Any]:
    path = str(getattr(config, "FANTASTIC_QUOTA_SNAPSHOT_PATH", "") or "")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_WATERMARK_SCHEMA = "fantastic-watermark/1"


class DateCreatedWatermarkEngine:
    """Acquisition engine keyed on the provider's index clock (``date_created``).

    PROVEN (2026-08-22 live probe): ``date_created_gte``/``date_created_lt`` are both
    honored by ``/v1/active-jb``. Category 2: DEFAULT OFF.

    Safe algorithm (never the naive "advance to max seen"):
        upper = now_utc - LAG            (deterministic, frozen at run start)
        lower = prev_watermark - OVERLAP (re-query a small band behind the mark)
        request date_created_gte=lower & date_created_lt=upper
        advance watermark -> upper ONLY after the interval is fully processed
        and persisted; a crash before commit leaves the prior watermark, so the
        next run replays the bounded [lower, upper) interval (dedup by stable IDs).

    State file (separate from the date_posted continuation file; additive; never
    wipes; deploying the code needs no migration because the engine is OFF)::

        {"schema": "fantastic-watermark/1", "last_successful_watermark": iso,
         "window_start": iso, "window_end": iso, "overlap_start": iso,
         "in_flight_window_end": iso|"", "boundary_ids": [...], "run_epoch": int,
         "updated_at": iso}

    An EMPTY interval is a valid, successful interval (proof of absence) and still
    advances the watermark -- structurally immune to the zero-acquisition-stuck
    class that afflicts a date_posted cursor.
    """

    def __init__(self, *, result, quota, http_get, seen_ids, metrics, run_cap: int,
                 now: Optional[datetime] = None) -> None:
        self.result, self.quota, self.http_get = result, quota, http_get
        self.seen_ids, self.metrics, self.run_cap = seen_ids, metrics, run_cap
        self.now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        self.state: Dict[str, Any] = self._load()
        self.lower = ""
        self.upper = ""
        self.acquired: List[Dict[str, Any]] = []
        self.opened = False

    # -- state -----------------------------------------------------------------
    def _path(self) -> str:
        return str(getattr(config, "FANTASTIC_WATERMARK_STATE_PATH", "") or "")

    def _load(self) -> Dict[str, Any]:
        p = self._path()
        if not p:
            return {}
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("schema") == _WATERMARK_SCHEMA:
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save(self) -> None:
        p = self._path()
        if not p:
            return
        try:
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = f"{p}.tmp"
            self.state["schema"] = _WATERMARK_SCHEMA
            self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, p)
        except OSError as exc:
            logger.warning("watermark state not persisted: %s", type(exc).__name__)

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- window ------------------------------------------------------------------
    def open(self) -> None:
        """Compute the deterministic window for this run and persist the in-flight
        marker BEFORE any acquisition (so a crash is detectable on restart)."""
        # WINDOW REUSE (Gate-E D3): the top-up loop may call the adapter more than
        # once. If an in-flight window is already open (same run, or an earlier
        # crashed run) it is REUSED verbatim -- never re-derived from ``now`` -- so
        # later calls page the SAME [lower, upper) and can never re-bill it. Every ID
        # already acquired in that window (``window_acquired_ids``) plus the whole
        # previous overlap band (``overlap_band_ids``) is re-seeded into ``seen_ids``,
        # so replay/overlap dedupes the entire band, not only the boundary second (D6).
        lag = int(getattr(config, "FANTASTIC_DATE_CREATED_LAG_MINUTES", 180) or 0)
        overlap = int(getattr(config, "FANTASTIC_DATE_CREATED_OVERLAP_MINUTES", 60) or 0)
        prev = self.state.get("last_successful_watermark") or ""
        in_flight = self.state.get("in_flight_window_end") or ""
        reused = False
        # Guard a corrupt in-flight record (marker without a parsable window start):
        # treat it as "no open window" rather than crashing acquisition uncaught.
        try:
            datetime.fromisoformat(str(self.state.get("window_start") or "").replace("Z", "+00:00"))
            start_ok = bool(self.state.get("window_start"))
        except (ValueError, TypeError):
            start_ok = False
        if in_flight and start_ok:
            self.lower, self.upper = str(self.state.get("window_start")), str(in_flight)
            reused = True
        else:
            upper_dt = self.now - timedelta(minutes=max(0, lag))
            if prev:
                prev_dt = datetime.fromisoformat(str(prev).replace("Z", "+00:00"))
                if prev_dt.tzinfo is None:
                    prev_dt = prev_dt.replace(tzinfo=timezone.utc)
                lower_dt = prev_dt - timedelta(minutes=max(0, overlap))
            else:
                # Bootstrap: cover the current time_frame window exactly once.
                hours = _parse_time_frame_hours(config.FANTASTIC_JOBS_TIME_FRAME)
                lower_dt = upper_dt - timedelta(hours=hours)
            if lower_dt >= upper_dt:
                lower_dt = upper_dt  # run started inside the lag buffer: empty interval
            self.lower, self.upper = self._iso(lower_dt), self._iso(upper_dt)
            self.state["window_acquired_ids"] = []
        for key in ("boundary_ids", "overlap_band_ids", "window_acquired_ids"):
            self.seen_ids |= {f"fantastic_{i}" for i in (self.state.get(key) or [])}
        self.state.update({"window_start": self.lower, "window_end": self.upper,
                           "overlap_start": self.lower, "in_flight_window_end": self.upper,
                           "last_successful_watermark": prev})
        self._save()
        self.opened = True
        self.metrics["watermark"] = {
            "enabled": True, "lower": self.lower, "upper": self.upper,
            "previous_watermark": prev, "window_reused": reused,
            "lag_minutes": lag, "overlap_minutes": overlap, "empty_interval": (self.lower == self.upper)}

    def run_stream(self, endpoint: str, base_params: Dict[str, Any], label: str,
                   cap_limit: int, accept: Optional[Tuple[str, ...]]) -> None:
        """One pass over [lower, upper) for a source; shares billing/dedupe with the
        head/deep engine via ``_fetch_segment``."""
        if not self.opened or self.lower == self.upper:
            return
        room = min(cap_limit, self.run_cap - len(self.result.jobs))
        if room <= 0:
            return
        params = dict(base_params)
        params["date_created_gte"] = self.lower
        params["date_created_lt"] = self.upper
        got = _fetch_segment(endpoint, params, label, room, self.quota, self.http_get,
                             self.seen_ids, self.metrics, accept_source=accept)
        self.acquired.extend(got)
        self.result.jobs.extend(got)
        self.metrics["watermark"]["acquired"] = self.metrics["watermark"].get("acquired", 0) + len(got)

    # Segment stop reasons that mean the window was NATURALLY exhausted. Anything
    # else (cap hit, quota reserve, page_cap, request error, rate limit) means the
    # window was TRUNCATED and the watermark must NOT advance (Gate-B 5A: a partial
    # window that commits loses every un-fetched in-window job permanently).
    _DRAINED_STOPS = frozenset({"", "empty_page", "short_page", "no_new_ids"})

    def window_drained(self) -> bool:
        segs = self.metrics.get("segments") or {}
        if not segs:
            return self.lower == self.upper  # empty interval = trivially drained
        for s in segs.values():
            if s.get("error_code") or str(s.get("stop_reason") or "") not in self._DRAINED_STOPS:
                return False
        return True

    def checkpoint(self) -> None:
        """After this adapter call: persist the IDs acquired so far in the OPEN window
        (still in-flight; the watermark does NOT advance) and whether the window was
        fully DRAINED. A later slice or a crash replay dedupes against the IDs and
        never re-bills. Committing is the PIPELINE's job (``commit_watermark``),
        after processing + persistence (Gate-E D4)."""
        if not self.opened:
            return
        ids = set(str(i) for i in (self.state.get("window_acquired_ids") or []))
        ids |= {str(j.get("_fantastic_internal_id")) for j in self.acquired if j.get("_fantastic_internal_id")}
        self.state["window_acquired_ids"] = sorted(ids)
        self.state["window_drained"] = bool(self.window_drained())
        self._save()
        self.metrics["watermark"]["committed"] = False
        self.metrics["watermark"]["drained"] = self.state["window_drained"]


def commit_watermark(*, success: bool) -> Dict[str, Any]:
    """Advance the date_created watermark to the open window's ``upper`` bound.

    Called by the PIPELINE after the acquired postings were processed AND persisted
    (after SuppressionStore.commit_postings) -- never by the adapter (Gate-E D4). A
    crash between acquisition and persistence leaves the in-flight marker, so the
    next run REPLAYS the same bounded window and dedupes against the checkpointed
    IDs. An EMPTY window is a valid success (proof of absence) and still advances.
    The window's acquired IDs become the next run's ``overlap_band_ids`` so the
    overlap band is re-queried but never re-billed (Gate-E D6).
    """
    engine = DateCreatedWatermarkEngine(result=None, quota=None, http_get=None,
                                        seen_ids=set(), metrics={}, run_cap=0)
    st = engine.state
    upper = str(st.get("in_flight_window_end") or "")
    if not upper:
        return {"committed": False, "reason": "no_open_window"}
    if not success:
        engine._save()
        return {"committed": False, "reason": "run_not_successful", "upper": upper}
    if not bool(st.get("window_drained", False)):
        # Gate-B 5A: the window was TRUNCATED (cap / quota reserve / page_cap / error).
        # Leave the watermark where it is so the next run replays the SAME window
        # (deduping what was already acquired) instead of losing the remainder.
        engine._save()
        return {"committed": False, "reason": "window_truncated_replay_next_run", "upper": upper,
                "acquired_so_far": len(st.get("window_acquired_ids") or [])}
    acquired_ids = list(st.get("window_acquired_ids") or [])
    st.update({"last_successful_watermark": upper, "in_flight_window_end": "",
               "overlap_band_ids": acquired_ids, "boundary_ids": [],
               "run_epoch": int(st.get("run_epoch", 0) or 0) + 1,
               "acquired_last_window": len(acquired_ids), "window_acquired_ids": []})
    engine._save()
    return {"committed": True, "next_watermark": upper, "band_ids": len(acquired_ids)}


def run_fantastic_jobs_acquisition(http_get: HttpGet = _http_get) -> SourceResult:
    """Entry point used by the orchestrator. Fail-open at the source level."""
    result = SourceResult(source="fantastic_jobs")
    if not config.FANTASTIC_JOBS_ENABLED:
        result.metadata = {"enabled": False, "skipped_reason": "disabled"}
        return result

    metrics: Dict[str, Any] = {"enabled": True, "segments": {}, "stop_reason": ""}
    try:
        config.validate_fantastic_jobs_config()
    except Exception as exc:
        result.success = False
        result.errors.append(f"config_error:{type(exc).__name__}")
        result.metadata = {"enabled": True, "skipped_reason": "config_error"}
        if config.FANTASTIC_JOBS_FAIL_OPEN:
            logger.warning("Fantastic.jobs configuration invalid; skipping source (fail-open).")
            return result
        raise

    quota = _QuotaState()
    seen_ids: set = set()
    # Max jobs this CALL may bill: the validated global ceiling, clamped to the
    # per-iteration top-up slice budget when set. Validation above used the ceiling;
    # the fetch loop below uses run_cap so a slice < a segment limit is billed safely.
    run_cap = _effective_run_cap()

    # Cross-run continuation cursor (default off). Resume strictly OLDER than the
    # oldest job already acquired so deep batches never re-fetch/re-bill the
    # prefix; the boundary second is re-included (date_posted_lt = cursor + 1s)
    # and its already-acquired IDs are pre-seeded into seen_ids so no job is
    # skipped and the overlap is deduped rather than re-counted.
    continuation_enabled = bool(getattr(config, "FANTASTIC_JOBS_CONTINUATION_ENABLED", False))
    cont_state = _load_continuation_state() if continuation_enabled else {}
    # Stale 24h-window guard (single-stream cursor): a cursor older than the
    # current time_frame window would push date_posted_lt below the feed's lower
    # bound and silently return zero jobs. Reset to a fresh window rather than let
    # stale state suppress a new acquisition window.
    continuation_reset_reason = ""
    if (continuation_enabled and cont_state.get("cursor_date")
            and _cursor_is_stale(cont_state["cursor_date"], config.FANTASTIC_JOBS_TIME_FRAME)):
        continuation_reset_reason = "stale_window"
        logger.info("Fantastic continuation cursor is older than the %s window; "
                    "resetting to a fresh window.", config.FANTASTIC_JOBS_TIME_FRAME)
        cont_state = {}
    cursor_date_lt = ""
    if continuation_enabled and cont_state.get("cursor_date"):
        cursor_date_lt = _advance_iso_second(cont_state["cursor_date"], 1)
        seen_ids |= {f"fantastic_{i}" for i in (cont_state.get("boundary_ids") or [])}
    metrics["continuation"] = {
        "enabled": continuation_enabled,
        "resumed_from_cursor_date": cont_state.get("cursor_date", ""),
        "applied_date_posted_lt": cursor_date_lt,
        "reset_reason": continuation_reset_reason,
    }

    # Two-phase acquisition (single stream). The DESC feed has no lower-bound date
    # parameter, so the FRESH EDGE is reached by paging from the TOP and stopping
    # client-side at the prior high_water (head pass); the DEEP/backfill edge resumes
    # strictly OLDER than cursor_date (date_posted_lt). "head_then_deep" (default)
    # does both; "head" only discovers new jobs; "deep" only backfills (top-up slices
    # >=2 use this so the head query is billed at most once per run). The head pass is
    # INDEPENDENT of the deep cursor, so an exhausted historical crawl can never again
    # starve daily discovery of newly-posted jobs.
    acquire_mode = str(getattr(config, "FANTASTIC_JOBS_ACQUIRE_MODE",
                               "head_then_deep") or "head_then_deep").strip().lower()
    do_head = continuation_enabled and acquire_mode in ("head", "head_then_deep")
    do_deep = ((not continuation_enabled)
               or (acquire_mode in ("deep", "head_then_deep") and bool(cursor_date_lt)))
    if continuation_enabled and not do_head and not do_deep:
        do_deep = True  # degenerate config -> plain current-window fetch (never zero)
    # Fresh-edge anchor: the prior high_water (empty on the first run / after a stale
    # reset -> the head pass then fetches the full window from the top, exactly the
    # legacy first-run behavior, and establishes high_water). Seed seen_ids with the
    # persisted boundary IDs at that second so already-acquired boundary jobs dedupe
    # while genuinely new same-second siblings are still collected.
    head_high_water = str(cont_state.get("high_water") or "") if do_head else ""
    if head_high_water:
        seen_ids |= {f"fantastic_{i}" for i in (cont_state.get("high_water_ids") or [])}
    metrics["continuation"]["acquire_mode"] = acquire_mode
    metrics["continuation"]["head_from_high_water"] = head_high_water

    # NOTE: only parameters proven against the successful smoke test are sent.
    # `description_type` was a guessed parameter (the smoke test that returned
    # HTTP 200 never sent it) and was the divergence in the failed production
    # request; it is not sent. Full descriptions are absent from this API by
    # default (only AI-derived fields), which the mapper already handles by
    # leaving job_description empty rather than fabricating one.
    title_targeting = bool(getattr(config, "FANTASTIC_JOBS_TITLE_TARGETING_ENABLED", False))
    title_advanced_enabled = bool(getattr(config, "FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED", False))
    title_plan = build_title_query_plan() if title_advanced_enabled else {"expression": "", "clauses": []}
    title_advanced_expr = title_plan.get("expression", "") if title_advanced_enabled else ""
    metrics["provider_filters"] = provider_filter_attribution()

    # Acquisition ENGINE selection (common interface, see AcquisitionEngine below):
    #   * default  -> head/deep two-cursor (the production-safe path, unchanged)
    #   * flag ON  -> date_created watermark (Category 2; DEFAULT OFF)
    # Both engines share _fetch_segment/billing/quota/seen_ids; only the window
    # bounds and the persisted cursor differ, so no pipeline logic is duplicated.
    watermark_engine = None
    if bool(getattr(config, "FANTASTIC_DATE_CREATED_WATERMARK_ENABLED", False)):
        watermark_engine = DateCreatedWatermarkEngine(
            result=result, quota=quota, http_get=http_get, seen_ids=seen_ids,
            metrics=metrics, run_cap=run_cap)
        watermark_engine.open()
    # title_advanced (one Boolean LinkedIn stream) TAKES PRECEDENCE over per-family
    # title_targeting in acquisition. The continuation cursor must be persisted in
    # the mode that ACTUALLY ran: if title_advanced runs while title_targeting is
    # also enabled, saving in title_families mode would persist an EMPTY families
    # state and silently drop the single-stream cursor -- so every next run would
    # restart from the top of the window and re-bill the acquired prefix.
    title_advanced_active = bool(
        title_advanced_enabled and title_advanced_expr
        and config.FANTASTIC_JOBS_LINKEDIN_LIMIT > 0)
    used_title_families = bool(title_targeting and not title_advanced_active)
    new_family_states: Dict[str, Any] = dict((cont_state.get("families") or {})) if (
        continuation_enabled and used_title_families) else {}
    # The two edges advance INDEPENDENTLY: head jobs (fresh top) may only raise
    # high_water; deep jobs (backfill) may only lower cursor_date. Kept in separate
    # buckets so a head pass can never regress the deep floor (which would re-bill).
    stream_head_jobs: List[Dict[str, Any]] = []
    stream_deep_jobs: List[Dict[str, Any]] = []

    def _run_single_stream(endpoint: str, base_jb: Dict[str, Any], label: str,
                           cap_limit: int, accept: Optional[Tuple[str, ...]]) -> None:
        """Head (fresh-edge: page from the top, stop at the prior high_water) then
        Deep (strictly older than the deep cursor). Both passes share the base label
        (so a job's ``_acquisition_source`` is never mutated) and the global
        ``seen_ids`` (so the two passes never double-count). Either pass may be
        disabled by the acquire mode."""
        if do_head:
            room = min(cap_limit, run_cap - len(result.jobs))
            if room > 0:
                got = _fetch_segment(  # NO date_posted_lt: page from the top
                    endpoint, dict(base_jb), label, room, quota, http_get, seen_ids,
                    metrics, accept_source=accept, stop_before_date=(head_high_water or None))
                stream_head_jobs.extend(got)
                result.jobs.extend(got)
                metrics["continuation"]["head_acquired"] = (
                    metrics["continuation"].get("head_acquired", 0) + len(got))
        if do_deep:
            room = min(cap_limit, run_cap - len(result.jobs))
            if room > 0:
                deep_jb = dict(base_jb)
                if cursor_date_lt:
                    deep_jb["date_posted_lt"] = cursor_date_lt
                got = _fetch_segment(
                    endpoint, deep_jb, label, room, quota, http_get, seen_ids,
                    metrics, accept_source=accept)
                stream_deep_jobs.extend(got)
                result.jobs.extend(got)
                metrics["continuation"]["deep_acquired"] = (
                    metrics["continuation"].get("deep_acquired", 0) + len(got))

    try:
        # Segment priority: ATS first, then Wellfound, Y Combinator, LinkedIn.
        # active-ats is the complementary, NON-overlapping first-party dataset
        # (active-jb is queried with exclude_ats_duplicate=true, so ATS x JB twins
        # cannot be billed twice by construction). It is gated by BOTH the explicit
        # source flag (Category 2, default OFF) and a non-zero ATS limit, so a code
        # deploy can never activate it; production keeps FANTASTIC_JOBS_ATS_LIMIT=0.
        ats_enabled = (bool(getattr(config, "FANTASTIC_ATS_SOURCE_ENABLED", False))
                       and int(config.FANTASTIC_JOBS_ATS_LIMIT or 0) > 0)
        metrics["ats_source"] = {"enabled": ats_enabled, "limit": int(config.FANTASTIC_JOBS_ATS_LIMIT or 0)}
        if ats_enabled:
            ats_params = build_ats_params(title_advanced_expr)
            metrics["ats_source"]["params"] = sorted(k for k in ats_params if k != "title_advanced")
            ats_room = min(int(config.FANTASTIC_JOBS_ATS_LIMIT), run_cap - len(result.jobs))
            if ats_room > 0:
                if watermark_engine is not None:
                    watermark_engine.run_stream("/v1/active-ats", ats_params, ATS_SOURCE, ats_room, None)
                else:
                    got = _fetch_segment("/v1/active-ats", ats_params, ATS_SOURCE, ats_room,
                                         quota, http_get, seen_ids, metrics)
                    # ATS rows must NOT feed the LinkedIn date_posted cursor (Gate-B):
                    # an ATS row with a newer date_posted would inflate high_water and
                    # make the next LinkedIn head pass skip genuinely new jobs.
                    result.jobs.extend(got)

        if title_advanced_active:
            # ONE Boolean OR-expression over the whole role catalog (benchmark
            # parity). The union is returned counting each job ONCE -> zero
            # cross-query billing overlap, 118/118 coverage, ~all target-role.
            # It is a single LinkedIn stream, so it reuses the single-stream
            # date_posted cursor (head fresh-edge + deep backfill).
            metrics["title_advanced"] = {"expression_chars": len(title_advanced_expr),
                                         "fingerprint": title_plan.get("fingerprint", ""),
                                         "clauses": len(title_plan.get("clauses") or [])}
            jb_params = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                         "exclude_ats_duplicate": "true", "source": "linkedin",
                         "title_advanced": title_advanced_expr}
            jb_params.update(_jb_filter_params())
            _apply_server_industry_exclusions(jb_params)
            if watermark_engine is not None:
                watermark_engine.run_stream("/v1/active-jb", jb_params, "fantastic_jobs_linkedin",
                                            config.FANTASTIC_JOBS_LINKEDIN_LIMIT, ("linkedin",))
            else:
                _run_single_stream("/v1/active-jb", jb_params, "fantastic_jobs_linkedin",
                                   config.FANTASTIC_JOBS_LINKEDIN_LIMIT, ("linkedin",))
        elif used_title_families and config.FANTASTIC_JOBS_LINKEDIN_LIMIT > 0:
            # LinkedIn-only, but targeted per role FAMILY so ~all billed jobs are
            # on-portfolio (the broad feed is ~4% target-role). One global cap is
            # shared FAIRLY across families (deterministic order, per-family share)
            # so one high-volume family cannot exhaust the run. seen_ids is GLOBAL
            # -> a job returned by two families is billed twice but processed once;
            # its cross-family duplicate is counted for billing observability. Each
            # family keeps its OWN date_posted cursor so streams never leak.
            fam_states = (cont_state.get("families") or {}) if continuation_enabled else {}
            families = [str(t).strip() for t in (config.FANTASTIC_JOBS_TITLE_FAMILIES or []) if str(t).strip()]
            global_cap = int(run_cap)
            per_family_cap = max(1, global_cap // max(1, len(families)))
            metrics["title_families_planned"] = len(families)
            for term in families:
                if quota.stop_reason or len(result.jobs) >= global_cap:
                    break
                fid = _family_id(term)
                cap = min(per_family_cap, global_cap - len(result.jobs))
                if cap <= 0:
                    break
                fam = fam_states.get(fid) or {}
                fam_lt = ""
                if continuation_enabled and fam.get("cursor_date"):
                    if _cursor_is_stale(fam["cursor_date"], config.FANTASTIC_JOBS_TIME_FRAME):
                        # Stale family cursor -> reset that family to a fresh window.
                        new_family_states.pop(fid, None)
                        fam = {}
                        metrics["continuation"].setdefault("family_resets", []).append(fid)
                    else:
                        fam_lt = _advance_iso_second(fam["cursor_date"], 1)
                        seen_ids |= {f"fantastic_{i}" for i in (fam.get("boundary_ids") or [])}
                jb_params = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                             "exclude_ats_duplicate": "true", "source": "linkedin",
                             "title": term}
                jb_params.update(_jb_filter_params())
                if fam_lt:
                    jb_params["date_posted_lt"] = fam_lt
                fam_jobs = _fetch_segment(
                    "/v1/active-jb", jb_params, f"fantastic_jobs_linkedin::{fid}",
                    cap, quota, http_get, seen_ids, metrics, accept_source=("linkedin",))
                for job in fam_jobs:
                    job["_fantastic_title_family"] = fid
                    job["_fantastic_title_term"] = term
                result.jobs.extend(fam_jobs)
                if continuation_enabled:
                    stats = _cursor_stats(fam_jobs, prior_high=str(fam.get("high_water") or ""))
                    if stats:
                        new_family_states[fid] = {"term": term, **stats}
        else:
            seg_caps = {
                "wellfound": config.FANTASTIC_JOBS_WELLFOUND_LIMIT,
                "ycombinator": config.FANTASTIC_JOBS_YCOMBINATOR_LIMIT,
                "linkedin": config.FANTASTIC_JOBS_LINKEDIN_LIMIT,
            }
            for seg_key, cap in seg_caps.items():
                if cap <= 0 or quota.stop_reason or len(result.jobs) >= run_cap:
                    continue
                label, accept = JB_SEGMENTS[seg_key]
                jb_params = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                             "exclude_ats_duplicate": "true", "source": seg_key}
                jb_params.update(_jb_filter_params())
                _run_single_stream("/v1/active-jb", jb_params, label, cap, accept)
    except FantasticAuthError as exc:
        result.success = False
        result.errors.append(str(exc))
        metrics["stop_reason"] = "auth_failed"
    except FantasticQuotaError as exc:
        result.errors.append(str(exc))
        metrics["stop_reason"] = "rate_limited"
    except Exception as exc:
        result.errors.append(f"source_error:{type(exc).__name__}")
        metrics["stop_reason"] = "source_error"
        if not config.FANTASTIC_JOBS_FAIL_OPEN:
            raise

    # Aggregate sanitized per-segment request errors (fail-open preserved).
    segment_errors = []
    for label, s in metrics["segments"].items():
        if s.get("error_code"):
            segment_errors.append({
                "segment": label, "stage": s.get("failure_stage", ""),
                "error_code": s.get("error_code", ""), "http_status": s.get("http_status"),
                "dispatched": bool(s.get("dispatched")), "retries": s.get("retries", 0),
            })
            result.errors.append(f"{label}:{s.get('failure_stage','')}:{s.get('error_code','')}")
    metrics["segment_errors"] = segment_errors
    metrics["stop_reason"] = metrics["stop_reason"] or quota.stop_reason or ("request_error" if segment_errors else "complete")
    metrics["jobs_quota_consumed"] = quota.jobs_consumed
    metrics["requests_attempted"] = sum(s.get("attempted", 0) for s in metrics["segments"].values())
    metrics["requests_consumed"] = quota.requests_consumed
    metrics["jobs_quota_remaining"] = quota.jobs_remaining
    metrics["requests_quota_remaining"] = quota.requests_remaining
    metrics["unique_jobs"] = len(result.jobs)
    metrics["fail_open_result"] = "continued" if config.FANTASTIC_JOBS_FAIL_OPEN else "strict"
    result.raw_records = sum(s.get("returned", 0) for s in metrics["segments"].values())
    result.requests_attempted = metrics["requests_attempted"]  # actual dispatch attempts
    result.requests_succeeded = quota.requests_consumed        # successful (200) requests
    # Billing-overlap observability: duplicates counted across the segments are
    # jobs the provider returned (and billed) more than once -- within a family's
    # repeated pages or, in title mode, across families sharing a job.
    metrics["cross_query_duplicates"] = sum(
        int(s.get("duplicates", 0)) for s in metrics["segments"].values())
    metrics["title_targeting"] = title_targeting
    metrics["used_title_families"] = used_title_families
    # Per-source billing observability (jobs kept + rows returned/billed per dataset).
    metrics["per_source"] = {
        label: {"jobs": sum(1 for j in result.jobs if j.get("_acquisition_source") == label),
                "returned_billed": int(s.get("returned", 0)),
                "requests": int(s.get("requests_succeeded", 0))}
        for label, s in metrics["segments"].items()}
    # Query-family attribution for every acquired job (which title clause matched),
    # used by the yield ledger to compute net-new send-safe per title family.
    if title_plan.get("clauses"):
        for job in result.jobs:
            job["_title_family"] = attribute_title_family(job.get("job_title", ""), title_plan)
    # Last-known provider quota snapshot (0-credit input for the next run's
    # governor). Gated so an all-flags-OFF deploy writes NO new state (Gate-E D7).
    if bool(getattr(config, "FANTASTIC_MONTHLY_GOVERNOR_ENABLED", False)):
        _save_quota_snapshot(quota, metrics)

    # The watermark engine only CHECKPOINTS here (IDs acquired in the open window);
    # the watermark advances in the PIPELINE after processing + persistence (D4).
    if watermark_engine is not None:
        watermark_engine.checkpoint()

    # Advance and persist the continuation cursor from the jobs actually acquired
    # (billed). ``_cursor_stats`` spans BOTH edges of the merged head+deep set in one
    # save: cursor_date -> min (advances OLDER, deep resume), high_water -> newest ever
    # seen (advances NEWER on a head pass, PRESERVED with its boundary IDs when a run
    # only went deeper). An empty run returns None and rewrites NEITHER edge (the file
    # is left intact), so a zero-result deep query can never regress the fresh edge.
    # Per-family mode keeps an INDEPENDENT cursor per family; single-stream
    # (title_advanced or seg-caps) keeps one cursor.
    if watermark_engine is not None:
        pass  # watermark engine owns the window; the date_posted cursor file is left intact
    elif continuation_enabled and used_title_families:
        _save_continuation_state({
            "schema": _CONTINUATION_SCHEMA,
            "mode": "title_families",
            "families": new_family_states,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        metrics["continuation"]["families_tracked"] = len(new_family_states)
    elif continuation_enabled:
        prior_cursor = str(cont_state.get("cursor_date") or "")
        prior_high = str(cont_state.get("high_water") or "")
        prior_boundary = list(cont_state.get("boundary_ids") or [])
        prior_high_ids = list(cont_state.get("high_water_ids") or [])
        # DEEP edge (backfill floor) advances strictly OLDER, from the deep pass. On
        # the FIRST run (no prior floor) the full-window head pass establishes it.
        deep_source = stream_deep_jobs if prior_cursor else (stream_deep_jobs + stream_head_jobs)
        deep_stats = _cursor_stats(deep_source)
        if deep_stats and (not prior_cursor or deep_stats["cursor_date"] < prior_cursor):
            new_cursor = deep_stats["cursor_date"]
            new_boundary = deep_stats["boundary_ids"]
        else:
            new_cursor, new_boundary = prior_cursor, prior_boundary  # deep didn't advance
        # FRESH edge (high_water) advances strictly NEWER, from the head pass. On the
        # FIRST run the full-window head set is the whole acquisition.
        head_source = stream_head_jobs if prior_high else (stream_head_jobs + stream_deep_jobs)
        head_stats = _cursor_stats(head_source)
        if head_stats and (not prior_high or head_stats["high_water"] > prior_high):
            new_high = head_stats["high_water"]
            new_high_ids = _ids_at(head_source, new_high)
        else:
            new_high, new_high_ids = prior_high, prior_high_ids  # no fresher jobs
        if stream_head_jobs or stream_deep_jobs:
            _save_continuation_state({
                "schema": _CONTINUATION_SCHEMA, "source": "linkedin",
                "cursor_date": new_cursor, "high_water": new_high,
                "high_water_ids": new_high_ids, "boundary_ids": new_boundary,
                "acquired_this_run": len(stream_head_jobs) + len(stream_deep_jobs),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        # else: nothing acquired -> leave the persisted state file untouched.
        metrics["continuation"]["next_cursor_date"] = new_cursor
        metrics["continuation"]["high_water"] = new_high

    result.metadata = metrics
    for job in result.jobs:
        job.setdefault("_acquisition_source", "fantastic_jobs")
    return result
