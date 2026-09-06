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
import tempfile
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


#: Called with the rows acquired so far, immediately BEFORE the window checkpoint
#: makes the per-source offsets durable. Set by the pipeline.
#:
#: The ordering is the whole point and it is not "before enrichment". Offsets become
#: durable inside ``DateCreatedWatermarkEngine.checkpoint()``, which runs at the END
#: of acquisition -- before the pipeline has even seen the postings. A run that died
#: between those two points advanced the cursor past rows nothing had kept. So the
#: hook runs first, and a FAILED hook stops the checkpoint: re-billing a page is
#: recoverable, losing it is not.
_CUSTODY_HOOK = None


def set_custody_hook(fn) -> None:
    """Install (or clear, with ``None``) the pre-checkpoint custody hook."""
    global _CUSTODY_HOOK
    _CUSTODY_HOOK = fn


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


#: The time frames the provider documents for /v1/active-jb and /v1/active-ats.
#: Anything else is a configuration error, not a value to guess at.
SUPPORTED_TIME_FRAMES = ("1h", "24h", "7d", "6m")

#: Days per month for the HOURS form of a month-based frame. Named for what it is: a
#: 30-day approximation, used only where an hours figure is required (the bootstrap
#: window width). It is NOT used for the frame horizon, which is calendar-exact --
#: an approximation there would either invent a floor above the provider's, silently
#: discarding months of recoverable inventory, or one below it, leaving a dead zone.
_APPROX_DAYS_PER_MONTH = 30


def _subtract_months(moment: datetime, months: int) -> datetime:
    """``moment`` minus N calendar months, clamping the day into the target month.

    A calendar six months is 181-184 days depending on where you start. Converting
    months to a fixed number of days and subtracting that is wrong in one of two
    directions, and both directions cost something real: too few days puts the
    horizon later than the provider's, so windows are clamped above inventory the
    feed would still serve; too many puts it earlier, leaving a dead zone below the
    real floor that no request can reach.
    """
    year, month = moment.year, moment.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = moment.day
    while True:
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            # 31st landing in a 30-day month (or Feb 29 in a common year).
            day -= 1


def _parse_time_frame_hours(time_frame: str) -> float:
    """Parse a documented Fantastic ``time_frame`` into hours.

    ``1h`` / ``24h`` / ``7d`` / ``6m`` are the four the provider documents. ``6m`` is
    a rolling SIX-MONTH window, and parsing it with an hours/days-only pattern
    returned the 24.0 fallback -- a 6-month frame read as one day, which would drive
    ``_frame_horizon`` to clamp every window to the last 24 hours and abandon any
    window older than that as unreachable.

    An unrecognised value still returns the 24h fallback rather than raising, because
    this runs inside acquisition; but it can no longer arrive from configuration --
    ``config`` validates ``FANTASTIC_JOBS_TIME_FRAME`` against
    ``SUPPORTED_TIME_FRAMES`` at import, so a typo fails the deploy instead of
    quietly shrinking the window a run can see."""
    m = re.match(r"^\s*(\d+)\s*([hdm])\s*$", str(time_frame or "").lower())
    if not m:
        return 24.0
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return n * _APPROX_DAYS_PER_MONTH * 24.0
    return n * 24.0 if unit == "d" else float(n)


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


def _frame_horizon(now: datetime, time_frame: str, margin_minutes: int = 0) -> datetime:
    """The oldest ``date_created`` the feed will return for a query carrying
    ``time_frame``.

    PROVEN (2026-09-05 live probe, ``/v1/active-jb-count``, zero Jobs credits): the
    provider INTERSECTS ``time_frame`` with ``date_created_gte``/``date_created_lt``
    rather than ignoring it. A window lying entirely below the frame returned 0 with
    ``time_frame=7d`` and 24,784 without it. So the EFFECTIVE lower bound of any
    window is ``max(window_lower, now - time_frame)`` -- and that bound advances with
    the clock, continuously, whether or not the window is reused.

    Dropping ``time_frame`` is not currently available: the two requests made without
    it both failed -- a 45s read timeout on the full window, and HTTP 504 at 240s on
    a 5.95-day slice carrying the production title expression. Those two shapes are
    not servable. Nothing about that rules out a smaller partition, or the same range
    without the 4,222-character ``title_advanced``; neither was tried. So the window
    is kept inside the frame because that is what answers today, not because the
    alternative was proven impossible.

    ``margin_minutes`` holds the window a little above the horizon so a long run does
    not slide off the edge it was clamped to while it is still paging."""
    # Months are subtracted by CALENDAR, never by an average day count: the horizon
    # is a floor the provider enforces, and approximating it moves real inventory in
    # or out of reach. Hours and days are exact already.
    m = re.match(r"^\s*(\d+)\s*m\s*$", str(time_frame or "").lower())
    if m:
        base = _subtract_months(now, int(m.group(1)))
    else:
        base = now - timedelta(hours=_parse_time_frame_hours(time_frame))
    return base + timedelta(minutes=max(0, margin_minutes))


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
#: The functional segment is a QUERY SHAPE, not a provider source: it reaches the
#: same job boards through active-jb without a title restriction. It carries its own
#: label so its billing, novelty and drain state are attributable on their own.
FUNCTIONAL_SOURCE = "fantastic_jobs_functional"
#: Historical recovery is its own labelled stream: cursor mode, 6m frame, its
#: own state file. It must never be confused with a windowed offset pass.
HISTORICAL_SOURCE = "fantastic_jobs_historical"
_HISTORICAL_SCHEMA = "fantastic-historical-recovery/1"

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


#: Bounded so a pathological run cannot bloat the run artifact with collisions.
_CANDIDATE_COLLISION_DETAIL_CAP = 200

# Count endpoint: returns a COUNT, never job rows. Used by the watermark
# visibility audit and by refresh_quota_snapshot().
_COUNT_ENDPOINT = "/v1/active-jb-count"


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_billing_date(raw: Any) -> str:
    """Return ``raw`` only when it parses the way the governor will parse it.

    Mirrors ``fantastic_governor._parse_iso`` exactly. An unparseable date is
    persisted as "" rather than stored verbatim: a garbage value would otherwise
    become a NEW ledger cycle key on every run, repeatedly rolling the cycle (and
    zeroing the local spend counter that the ledger-authoritative path relies on).
    Blank simply means "unknown reset", which the governor already treats as a
    conservative 30-day horizon.
    """
    if not raw:
        return ""
    try:
        datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    return str(raw).strip()


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
        # AI taxonomy (SERVER-QUERYABLE via ai_taxonomies_a / _primary /
        # exclude_ai_taxonomies_a). Captured for OBSERVATION only -- historical
        # artifacts contain none, so taxonomy->yield is currently unmeasurable;
        # persisting it now lets the yield ledger measure it from live runs.
        "_ai_taxonomies": [str(t) for t in (record.get("ai_taxonomies_a") or []) if str(t or "").strip()],
        "_ai_taxonomy_primary": (str((record.get("ai_taxonomies_a") or [""])[0]).strip()
                                 if (record.get("ai_taxonomies_a") or []) else ""),
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

    # FUNCTIONAL ROLE EXPANSION (flag-gated): adjacent titles that describe the SAME
    # underlying work as a measured high-yield activity cluster but are absent from
    # the 118-term catalog. Each expansion belongs to exactly ONE acquisition family
    # and inherits that family's contamination exclusions, so a broad activity word
    # (e.g. "automation") can never be bought naked. With the flag OFF the catalog
    # and therefore the expression are byte-identical to production.
    expansion_terms: Dict[str, str] = {}          # role -> family
    family_exclusions: Dict[str, Tuple[str, ...]] = {}
    if bool(getattr(config, "FANTASTIC_FUNCTIONAL_ROLE_EXPANSION_ENABLED", False)):
        try:
            from orchestrator.function_acquisition import (
                ADJACENT_TITLE_CANDIDATES, FAMILY_SCOPED_EXCLUSIONS, family_for_role)
            family_exclusions = dict(FAMILY_SCOPED_EXCLUSIONS)
            for fam, titles in ADJACENT_TITLE_CANDIDATES.items():
                for t in titles:
                    expansion_terms[str(t)] = fam
        except Exception:  # noqa: BLE001 - expansion never breaks acquisition
            expansion_terms, family_exclusions = {}, {}

    catalog = {str(r).strip() for r in DEFAULT_ACQUISITION_ROLES if str(r).strip()}
    # An "expansion" must be a title the catalog does NOT already cover. Without this
    # guard a duplicate would shadow the base clause (alphabetical ordering decides
    # which wins), silently reclassifying a base role as expanded and corrupting the
    # base-vs-expanded yield attribution the ledger depends on.
    _catalog_terms = {t for r in catalog for t in _role_terms(r)}
    expansion_terms = {r: f for r, f in expansion_terms.items()
                       if not (set(_role_terms(r)) & _catalog_terms)}
    all_roles = sorted(catalog | set(expansion_terms))

    clauses: List[Dict[str, Any]] = []
    seen_terms: set = set()
    for role in all_roles:
        include = [t for t in _role_terms(role) if t not in seen_terms]
        if not include:
            continue
        seen_terms.update(include)
        exclude = [n for n in (_negation_term(x) for x in (scoped.get(role) or [])) if n]
        # An expanded title inherits its family's contamination defence.
        if role in expansion_terms and family_exclusions:
            fam_ex = family_exclusions.get(expansion_terms[role]) or ()
            for n in (_negation_term(x) for x in fam_ex):
                if n and n not in exclude:
                    exclude.append(n)
        inner = " | ".join(include)
        if exclude:
            # Scoped: negate INSIDE this role's clause only.
            clause = f"({inner}) & " + " & ".join(exclude) if len(include) > 1 else f"{inner} & " + " & ".join(exclude)
            clause = f"({clause})"
        else:
            clause = inner if len(include) == 1 else f"({inner})"
        clauses.append({"family": _role_family_id(role), "role": role, "include": include,
                        "exclude": exclude, "clause": clause,
                        "expanded": role in expansion_terms,
                        "function_family": expansion_terms.get(role, "")})
    union = " | ".join(c["clause"] for c in clauses)
    gneg = [n for n in (_negation_term(x) for x in global_neg) if n]
    expression = f"({union}) & " + " & ".join(gneg) if (union and gneg) else union
    return {"expression": expression, "clauses": clauses, "global_exclusions": gneg,
            "fingerprint": _fingerprint(expression), "override": False,
            "expanded_clauses": sum(1 for c in clauses if c.get("expanded")),
            "base_clauses": sum(1 for c in clauses if not c.get("expanded"))}


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
    """Attach ``exclude_organization_industry`` using the PROVEN multi-value encoding.

    The OpenAPI schema declares ``type: array, style: form, explode: false`` -- i.e. a
    SINGLE comma-joined value, NOT repeated query params. Verified live against
    /v1/active-jb-count (2026-08-23): comma form removed both labels (6118 -> 6116)
    while repeated params (``a=x&a=y``, what ``requests`` emits for a list) applied
    only the FIRST label and silently under-filtered. Labels are exact, case-sensitive
    LinkedIn taxonomy strings taken from configuration -- never translated from Apollo
    keyword lists.
    """
    labels = _server_industry_exclusions()
    if labels:
        params["exclude_organization_industry"] = ",".join(labels)
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


@dataclass
class _SourceSegment:
    """One INDEPENDENTLY executable Fantastic source.

    Sources used to be mutually-exclusive acquisition MODES (Wellfound/YC lived
    only in the final ``else`` of an if/elif/else and were unreachable whenever
    title_advanced was active). They are now segments in a plan: each is enabled
    on its own, carries its own configured limit and provider parameters, and
    draws from ONE shared governor run budget.
    """
    key: str                       # ats | linkedin | wellfound | ycombinator
    label: str                     # metrics / _acquisition_source label
    endpoint: str
    limit: int
    accept: Optional[Tuple[str, ...]]
    feeds_cursor: bool = False     # only the LinkedIn stream drives date_posted cursors
    dispatch: str = "plan"         # plan | title_advanced | title_families


class _SourceBudgetAllocator:
    """Divides ONE governor ``run_cap`` across source segments.

    HARD INVARIANT: the sum of every grant this allocator issues, measured against
    provider-BILLED rows, can never exceed ``run_cap``. Grants are computed BEFORE
    any request is dispatched -- never by truncating results afterwards.

    ``sequential`` reproduces the legacy behaviour exactly (each segment may draw
    the whole remaining budget, in deterministic order). ``fair_share`` reserves an
    equal floor per enabled segment and cascades whatever a segment does not spend.
    """

    def __init__(self, budget: int, segments: List[_SourceSegment], policy: str = "sequential"):
        #: The pool THIS allocator distributes. For the steady-state allocator it is
        #: run_cap minus the bootstrap reserve -- deliberately NOT called run_cap.
        self.budget = max(0, int(budget))
        self.policy = policy if policy in ("sequential", "fair_share") else "sequential"
        self.segments = list(segments)
        self.grants: Dict[str, int] = {}
        self.spent: Dict[str, int] = {}
        self.rounds = 0
        self.alloc: Dict[str, int] = {}
        self.pool = 0
        self.open_round(self.segments)

    def billed_total(self) -> int:
        return sum(self.spent.values())

    def open_round(self, active: List[_SourceSegment]) -> int:
        """Re-share the REMAINING budget across the segments that can still consume.

        Within one round, an unspent reservation cascades FORWARD to later segments.
        That alone strands budget whenever a sparse source sits late in the plan: its
        reservation is released only after the sources that could have used it have
        already run. Re-opening a round hands that budget BACK to the segments still
        able to spend it, so source ORDER can no longer decide whether the run budget
        gets used -- while every enabled source still receives its equal floor in
        round 1, before any recycling happens.
        """
        self.rounds += 1
        active = list(active)
        remaining = max(0, self.budget - self.billed_total())
        n = len(active)
        if self.policy == "fair_share" and n:
            base = remaining // n
            self.alloc = {s.key: min(max(0, int(s.limit) - self.spent.get(s.key, 0)), base)
                          for s in active}
            self.pool = remaining - sum(self.alloc.values())
        else:
            self.alloc = {s.key: remaining for s in active}
            self.pool = 0
        return n

    def grant(self, seg: _SourceSegment, billed_total: int) -> int:
        """Jobs this segment may cause the provider to BILL. Never exceeds the
        segment's configured limit, its allocation, or the unspent run budget.

        The per-source limit is CUMULATIVE across rounds: a segment granted budget
        again in a later round may still only reach its configured total.
        """
        remaining = self.budget - max(0, int(billed_total))
        headroom = int(seg.limit) - self.spent.get(seg.key, 0)
        if remaining <= 0 or headroom <= 0:
            self.grants.setdefault(seg.key, 0)
            return 0
        room = min(headroom, remaining)
        if self.policy == "fair_share":
            room = min(room, self.alloc.get(seg.key, 0) + self.pool)
        room = max(0, int(room))
        self.grants[seg.key] = self.grants.get(seg.key, 0) + room
        return room

    def settle(self, seg: _SourceSegment, billed: int) -> None:
        """Return a segment's UNUSED reservation to the shared pool."""
        billed = max(0, int(billed))
        self.spent[seg.key] = self.spent.get(seg.key, 0) + billed
        if self.policy != "fair_share":
            return
        reserved = self.alloc.get(seg.key, 0)
        from_alloc = min(billed, reserved)
        from_pool = billed - from_alloc
        self.pool = max(0, self.pool - from_pool + (reserved - from_alloc))
        self.alloc[seg.key] = 0

    def to_dict(self) -> Dict[str, Any]:
        """Explicitly named: ``budget`` is the pool THIS allocator distributes (for
        the steady-state allocator that is run_cap MINUS any bootstrap reserve, not
        the run cap), ``granted`` is permission and ``billed`` is what the provider
        actually charged. They are different sides of the accounting and are never
        collapsed into one ``total``."""
        granted_total = sum(self.grants.values())
        billed_total = sum(self.spent.values())
        return {"policy": self.policy, "budget": self.budget, "rounds": self.rounds,
                "segments": [s.key for s in self.segments],
                "granted": dict(self.grants), "billed": dict(self.spent),
                "granted_total": granted_total, "billed_total": billed_total,
                "unspent_budget": max(0, self.budget - billed_total),
                "invariant_ok": billed_total <= self.budget}


def build_source_plan(*, title_advanced_active: bool, used_title_families: bool) -> List[_SourceSegment]:
    """The ordered set of source segments enabled for THIS run.

    ATS and LinkedIn keep their existing enablement rules. Wellfound and Y
    Combinator each need BOTH an explicit source flag and a non-zero limit (the
    same two-key gate active-ats uses), so a code deploy can never turn them on.
    LinkedIn is omitted here when a title MODE owns its dispatch (title_advanced
    single-stream or per-family), because those modes drive the date_posted cursor.
    """
    plan: List[_SourceSegment] = []
    ats_limit = int(getattr(config, "FANTASTIC_JOBS_ATS_LIMIT", 0) or 0)
    if bool(getattr(config, "FANTASTIC_ATS_SOURCE_ENABLED", False)) and ats_limit > 0:
        plan.append(_SourceSegment(key="ats", label=ATS_SOURCE, endpoint="/v1/active-ats",
                                   limit=ats_limit, accept=None))
    li_limit = int(getattr(config, "FANTASTIC_JOBS_LINKEDIN_LIMIT", 0) or 0)
    if li_limit > 0:
        label, accept = JB_SEGMENTS["linkedin"]
        # LinkedIn is ALWAYS a planned segment so it draws an allocator grant like
        # every other source. Which query shape it uses (title_advanced single
        # stream, per-family, or plain) is a MODE, recorded here but dispatched
        # separately because those modes own the date_posted cursor.
        plan.append(_SourceSegment(
            key="linkedin", label=label, endpoint="/v1/active-jb", limit=li_limit,
            accept=accept, feeds_cursor=True,
            dispatch=("title_advanced" if title_advanced_active
                      else "title_families" if used_title_families else "plan")))
    # FUNCTIONAL (task-based) discovery: its own segment, because a description
    # expression ANDed onto the title query could only narrow it. Two-key gated and
    # default OFF, so a deploy cannot start paying for it. It draws an allocator
    # grant like every other source, which is what keeps it from starving the ones
    # with proven yield -- and what lets the allocator's exploration floor fund it
    # while it has none.
    fn_limit = int(getattr(config, "FANTASTIC_JOBS_FUNCTIONAL_LIMIT", 0) or 0)
    if (bool(getattr(config, "FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED", False))
            and fn_limit > 0
            and str(getattr(config, "FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED", "") or "").strip()):
        plan.append(_SourceSegment(key="functional", label=FUNCTIONAL_SOURCE,
                                   endpoint="/v1/active-jb", limit=fn_limit,
                                   accept=None, dispatch="functional"))
    for key, flag in (("wellfound", "FANTASTIC_WELLFOUND_SOURCE_ENABLED"),
                      ("ycombinator", "FANTASTIC_YCOMBINATOR_SOURCE_ENABLED")):
        limit = int(getattr(config, f"FANTASTIC_JOBS_{key.upper()}_LIMIT", 0) or 0)
        if bool(getattr(config, flag, False)) and limit > 0:
            label, accept = JB_SEGMENTS[key]
            plan.append(_SourceSegment(key=key, label=label, endpoint="/v1/active-jb",
                                       limit=limit, accept=accept))
    return plan


#: Server-side predicates that EXCLUDE NULLS. ``organization_headcount_gte/lt`` is a
#: ">=" / "<" comparison and ``exclude_organization_industry`` is a NOT-IN, so a row
#: whose firmographics the provider never populated is dropped by EACH of them --
#: silently, as an empty page rather than an error.
_NULL_EXCLUDING_FIRMOGRAPHIC_PARAMS = ("organization_headcount_gte",
                                       "organization_headcount_lt",
                                       "exclude_organization_industry")


def source_supports_provider_firmographics(source_key: str) -> bool:
    """False for sources whose rows carry no provider firmographics (Wellfound, Y
    Combinator). Configuration, never a guess: the incompatible set was measured
    with 0-credit count probes (see config.FANTASTIC_FIRMOGRAPHIC_INCOMPATIBLE_SOURCES)."""
    bad = {str(s).strip().lower() for s in
           (getattr(config, "FANTASTIC_FIRMOGRAPHIC_INCOMPATIBLE_SOURCES", []) or [])}
    return str(source_key or "").strip().lower() not in bad


def build_jb_params(source_key: str, *, title_advanced_expr: str = "") -> Dict[str, Any]:
    """``/v1/active-jb`` request for ONE source segment.

    Identical to the previous inline construction for every firmographics-carrying
    source, with two deliberate differences for the others:

      * the NULL-EXCLUDING firmographic predicates are omitted, because they drop
        100% of that source's rows rather than filtering them (ICP is then enforced
        downstream on Apollo facts, which is already authoritative for PASS);
      * role targeting is APPLIED. The plan loop previously sent no
        ``title_advanced`` at all, so a plan-dispatched source would have returned
        its whole unfiltered feed -- 542 Wellfound rows instead of the 159 that are
        on-portfolio. Filtering the ROLE server-side is what makes a sparse source
        worth its credits.
    """
    params: Dict[str, Any] = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                              "exclude_ats_duplicate": "true", "source": source_key}
    params.update(_jb_filter_params())
    _apply_server_industry_exclusions(params)
    if title_advanced_expr:
        params["title_advanced"] = title_advanced_expr
    if not source_supports_provider_firmographics(source_key):
        for key in _NULL_EXCLUDING_FIRMOGRAPHIC_PARAMS:
            params.pop(key, None)
    return params


def run_historical_recovery(http_get: HttpGet, quota: "_QuotaState",
                            seen_ids: set, metrics: Dict[str, Any],
                            *, max_rows: Optional[int] = None) -> List[Dict[str, Any]]:
    """Bounded backfill over ``time_frame=6m`` using the provider's CURSOR mode.

    The windowed engine pages `date_created` by OFFSET, which is what the provider
    documents for 1h/24h/7d. It cannot cover an interruption longer than the frame:
    once the floor rises past a gap those postings are unreachable by any windowed
    request, whatever offset is used.

    The documented way back is the other mode -- `time_frame=6m` with `cursor` set to
    the LAST ID RETURNED, which orders by `id` ASCENDING rather than `date_posted`
    descending. Two consequences shape everything here:

      * an id cursor is not an offset and the two are never mixed. The provider warns
        against resuming one with the other, so this keeps its own state file and
        never reads or writes `window_offsets`;
      * ascending id order makes the cursor genuinely resumable. Progress is
        persisted after EVERY page, so an interruption resumes at the last id
        instead of restarting the backfill.

    Bounded by construction: disabled unless both the flag and a non-zero row budget
    say otherwise, and it never bills past that budget. Rows already in `seen_ids`
    are deduped exactly as everywhere else -- recovery cannot double-count what the
    windowed engine already has.

    WHAT IT CANNOT DO: reach past provider retention, or resurrect an expired
    posting. `6m` is the oldest frame offered; a gap older than that stays a gap.
    """
    block = metrics.setdefault("historical_recovery", {"enabled": False})
    if not bool(getattr(config, "FANTASTIC_HISTORICAL_RECOVERY_ENABLED", False)):
        block["stop_reason"] = "disabled"
        return []
    budget = int(max_rows if max_rows is not None
                 else getattr(config, "FANTASTIC_HISTORICAL_RECOVERY_MAX_ROWS_PER_RUN", 0) or 0)
    if budget <= 0:
        # No implicit budget. An unbounded historical backfill must not be reachable
        # by flipping one flag.
        block.update({"enabled": True, "stop_reason": "no_row_budget", "billed": 0})
        return []

    state_path = str(getattr(config, "FANTASTIC_HISTORICAL_RECOVERY_STATE_PATH", "") or "")
    state: Dict[str, Any] = {}
    if state_path and os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and loaded.get("schema") == _HISTORICAL_SCHEMA:
                state = loaded
        except (OSError, ValueError):
            state = {}
    cursor = str(state.get("cursor") or "")

    def _save() -> None:
        if not state_path:
            return
        try:
            os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
            state["schema"] = _HISTORICAL_SCHEMA
            state["updated_at"] = _iso_z(datetime.now(timezone.utc))
            tmp = f"{state_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, state_path)
        except OSError:
            pass

    base = {"time_frame": "6m", "exclude_ats_duplicate": "true"}
    base.update(_jb_filter_params())
    _apply_server_industry_exclusions(base)
    expr = build_title_query_plan().get("expression") or ""
    if expr:
        base["title_advanced"] = expr

    endpoint = "/v1/active-jb"
    label = HISTORICAL_SOURCE
    billed = kept = pages = 0
    jobs: List[Dict[str, Any]] = []
    stop = ""
    while billed < budget:
        want = min(budget - billed, 100)
        params = dict(base, limit=want)
        if cursor:
            params["cursor"] = cursor
        try:
            rows, _q = _request(endpoint, params, http_get,
                                metrics["segments"].setdefault(label, {}))
        except FantasticRequestError as exc:
            stop = f"request_error:{exc.code}"
            break
        pages += 1
        if not rows:
            stop = "exhausted"
            break
        last_id = ""
        for record in rows:
            billed += 1
            quota.jobs_consumed += 1
            rid = str((record or {}).get("id") or "")
            if rid:
                last_id = rid
            job, _reason = map_record(record, label, metrics["segments"].setdefault(label, {}))
            if job is None:
                continue
            if job["job_id"] in seen_ids:
                continue
            seen_ids.add(job["job_id"])
            jobs.append(job)
            kept += 1
        if not last_id:
            # Without an id there is no cursor to advance, and repeating the same
            # request would bill the same page forever.
            stop = "no_cursor_id"
            break
        cursor = last_id
        state["cursor"] = cursor
        state["billed_total"] = int(state.get("billed_total", 0)) + len(rows)
        _save()          # after EVERY page: an interruption resumes, never restarts
        if len(rows) < want:
            stop = "exhausted"
            break
    block.update({"enabled": True, "billed": billed, "kept": kept, "pages": pages,
                  "cursor": cursor, "stop_reason": stop or "row_budget_reached",
                  "budget": budget, "state_path": state_path,
                  "pagination": "cursor(id asc)", "time_frame": "6m"})
    return jobs


def _classify_functional_rows(rows: List[Dict[str, Any]], metrics: Dict[str, Any]) -> None:
    """Give functional-segment rows the role target RoleGate verifies against.

    Runs the SAME `multi_source_acquisition._classify` the external-batch path uses,
    for the same reason: a row that did not arrive from a title-matched query has no
    `_matched_role`, and RoleGate cannot verify a title against a target that is not
    there. Without this every functional row is UNVERIFIED by construction, which
    reads as "the segment finds nothing relevant" when it means "the gate was never
    told what to check".

    It supplies the missing input and nothing else. The classifier assigns a CATALOG
    role or none; a title the catalog does not cover still ends with no role and
    stays reviewable. No commercial fit is inferred from task vocabulary, and every
    downstream gate is untouched.

    Fail-open per row: a classifier error leaves that row unclassified (reviewable)
    rather than losing the acquisition.
    """
    classified = unmapped = errors = 0
    try:
        from multi_source_acquisition import _classify
    except Exception:  # noqa: BLE001 - acquisition must not fail on an import
        metrics.setdefault("functional_discovery", {})["classify"] = "unavailable"
        return
    # `_matched_role` is ALWAYS set: `_classify` picks the best catalog role for any
    # title, and for an irrelevant one it picks a poor match and says so in
    # `_role_relevance_status`. Counting role PRESENCE would therefore report every
    # row as classified and overstate what this segment finds. The assessment is the
    # signal, and a rejected assessment is the classifier correctly declining -- the
    # row keeps its acquisition and reaches review carrying that verdict.
    for job in rows:
        try:
            _classify(job)
            if str(job.get("_role_relevance_status") or "").lower() == "reject":
                unmapped += 1
            else:
                classified += 1
        except Exception:  # noqa: BLE001
            errors += 1
    metrics.setdefault("functional_discovery", {}).update({
        "rows": len(rows), "role_relevant": classified,
        "role_rejected_reviewable": unmapped, "classify_errors": errors})


def build_functional_params() -> Dict[str, Any]:
    """``/v1/active-jb`` request for the FUNCTIONAL (task-based) segment.

    Carries ``description_advanced`` and deliberately NO ``title_advanced``.

    The provider ANDs the ``_advanced`` parameters together, so adding a description
    expression to the existing title query can only narrow it -- it cannot reach a
    job the title filter already excluded. Title synonyms have the same ceiling: they
    lengthen the list of titles we recognise, and a posting whose title is nothing
    like any of them stays invisible however many aliases are added. Reaching work
    under an unfamiliar title needs a request that does not filter on title at all.

    Every other ICP filter is kept, so this widens the query on ONE axis only. What
    comes back is subject to the same RoleGate, ICP, firmographic and send-safe
    decisions as everything else: this widens what is CONSIDERED, never what is
    approved.
    """
    expression = str(getattr(config, "FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED", "") or "").strip()
    if not expression:
        return {}
    params: Dict[str, Any] = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                              "exclude_ats_duplicate": "true",
                              "description_advanced": expression}
    params.update(_jb_filter_params())
    _apply_server_industry_exclusions(params)
    return params


def title_expansion_alignment() -> Tuple[List[str], List[str]]:
    """``(aligned, unmapped)`` candidate title families.

    THE INVARIANT: a family may only enter the acquisition query if the role
    catalog can resolve it to a ``RoleDefinition``. A query-only family spends
    credits on postings the role gate must then mark
    ``UNVERIFIED_ROLE_CLASSIFICATION`` -- recall bought and thrown away. Measured
    2026-09-04: the 13 candidate titles were in the QUERY arm but not the catalog,
    so every one of them classified UNVERIFIED even when handed its own title as
    the matched role, while catalog titles reached ROLE_PASS on the same fixture.

    The catalog is the single source of truth: ``build_title_query_plan`` already
    generates the production expression FROM it, so adding a role definition
    expands the query and teaches the classifier in one edit. This function is
    what keeps a future candidate from re-opening the gap.
    """
    from role_catalog import get_role_definition
    aligned: List[str] = []
    unmapped: List[str] = []
    for title in sorted(getattr(config, "FANTASTIC_CANDIDATE_TITLES", {}) or {}):
        (aligned if get_role_definition(title) else unmapped).append(title)
    return aligned, unmapped


def candidate_title_expression() -> str:
    """Production expression + any candidate family the CATALOG can classify.

    Built by appending to the production expression, never by rebuilding it, so
    the control half stays byte-identical. Families the catalog cannot resolve are
    DROPPED rather than queried -- fail-safe, and
    ``title_expansion_alignment()`` reports them so a test fails loudly instead of
    production quietly buying rows it cannot qualify.

    Once a family is added to the role catalog this returns the production
    expression unchanged, because the catalog already carries it.
    """
    base = build_title_query_plan().get("expression", "")
    aligned, _unmapped = title_expansion_alignment()
    extra = [_title_advanced_term(t) for t in aligned]
    extra = [e for e in extra if e and e not in base]
    return " | ".join([base] + extra) if base else " | ".join(extra)


def candidate_cross_source_key(job: Dict[str, Any]) -> str:
    """A DETERMINISTIC candidate key for the same posting seen via two sources.

    OBSERVABILITY ONLY -- it is never used to merge or drop a job. Cross-source
    dedupe stays on provider ids because no field in the normalized schema is a
    safe cross-provider identity:

      * ``canonical_source_url`` is source-SPECIFIC (a linkedin.com/jobs/view URL
        vs the company's Greenhouse/Lever board URL for the same opening), so URL
        equality cannot detect the duplicate;
      * the highest-volume overlap pair (ATS x LinkedIn) is already de-twinned by
        the provider via ``exclude_ats_duplicate=true``, so little is left to catch;
      * a domain+title+date tuple WOULD collapse legitimately distinct openings --
        multi-headcount reqs, multi-location postings and intentional reposts --
        which is worse than missing a duplicate.

    Recording collisions lets the source experiment MEASURE how much real overlap a
    tuple key would have caught, so the decision can later be made on evidence
    instead of assumption.
    """
    domain = str(job.get("employer_website") or job.get("domain_derived") or "").strip().lower()
    title = re.sub(r"[^a-z0-9 ]", " ", str(job.get("job_title") or "").lower())
    title = re.sub(r"\s+", " ", title).strip()
    loc = re.sub(r"\s+", " ", str(job.get("job_location") or "").lower()).strip()
    day = str(job.get("job_posted_at_datetime_utc") or "")[:10]
    if not (domain and title):
        return ""
    return f"{domain}|{title}|{loc}|{day}"


def _quota_would_breach(quota: _QuotaState, want: int) -> str:
    if quota.requests_remaining is not None and (quota.requests_remaining - 1) < config.FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING:
        return "requests_quota_reserve"
    if quota.jobs_remaining is not None and (quota.jobs_remaining - want) < config.FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING:
        return "jobs_quota_reserve"
    return ""


def _observe_order(seg: Dict[str, Any], rows: List[Any],
                   field: str = "date_posted") -> None:
    """Watch the direction the feed returns ``field`` in, across a whole pass.

    The provider documents the default order as ``date_posted`` DESCENDING (cursor
    mode instead orders by ``id`` ascending, and we do not use cursor mode). This
    watched ``date_created`` -- the field our WINDOW is bounded on, and not the field
    the rows are sorted by -- so its reading said nothing about the page order.

    That distinction is the hazard, not a detail. Pages are ordered by
    ``date_posted``; the frame floor rises on ``date_created``. The two correlate but
    are not the same instant, so rows dropping out below the floor are scattered
    through the page order rather than confined to either end of it. No observed
    direction can make a persisted offset safe across such a change -- which is why
    the coverage guard keys on whether the floor is INSIDE the window, and this
    stays an observation.

    Sets ``order_observed`` to ``desc``, ``asc``, ``constant`` or ``unordered``.
    Rows without a parsable date are skipped rather than treated as a break."""
    dates = [str(r.get(field) or "") for r in rows if isinstance(r, dict)]
    dates = [d for d in dates if d]
    if not dates:
        return
    probe = seg.setdefault("order_probe", {
        "first": dates[0], "last": "", "rows": 0, "desc_breaks": 0, "asc_breaks": 0})
    prev = probe.get("last") or probe["first"]
    for value in dates:
        if value > prev:
            probe["desc_breaks"] += 1
        elif value < prev:
            probe["asc_breaks"] += 1
        prev = value
    probe["last"] = dates[-1]
    probe["rows"] += len(dates)
    if not probe["desc_breaks"] and not probe["asc_breaks"]:
        probe["order_observed"] = "constant"
    elif not probe["desc_breaks"]:
        probe["order_observed"] = "desc"
    elif not probe["asc_breaks"]:
        probe["order_observed"] = "asc"
    else:
        probe["order_observed"] = "unordered"


def _fetch_segment(endpoint: str, base_params: Dict[str, Any], source_label: str, cap: int,
                   quota: _QuotaState, http_get: HttpGet, seen_ids: set,
                   metrics: Dict[str, Any], accept_source: Optional[Tuple[str, ...]] = None,
                   stop_before_date: Optional[str] = None,
                   start_offset: int = 0,
                   durable_cursor: bool = False) -> List[Dict[str, Any]]:
    """When ``stop_before_date`` is set (fresh-edge/head pass), the DESC feed is
    paged from the top and paging STOPS as soon as a job older than that timestamp
    is seen -- so only jobs newer than the prior high_water are collected. Jobs AT
    the boundary second flow through the normal ``seen_ids`` dedupe (the caller seeds
    it with the persisted high_water boundary IDs), so already-acquired boundary jobs
    are skipped while genuinely new same-second siblings are still kept.

    ``durable_cursor`` says the caller persists this segment's offset across runs
    (the canonical windowed pass and the bootstrap backfill both do).

    It changes what a FULL page of already-seen rows COSTS, not what it proves.
    Such a page never proves exhaustion -- it says the rows at this offset are ones
    we already hold, and nothing about what lies deeper, which is why
    ``no_new_ids`` is not a drained stop. Without a durable cursor the offset dies
    with the run, so paging on spends on rows the next run must re-buy from zero
    anyway: stopping is the cheaper of two bad options. With one the offset
    survives, so paging on is the only thing that ever reaches the tail."""
    jobs: List[Dict[str, Any]] = []
    boundary_hit = False
    duplicate_pages = 0
    #: How many CONSECUTIVE all-duplicate pages we will pay to skip before giving
    #: up on this window for this run. The run cap already bounds total spend; this
    #: bounds the pathological case where a whole window is overlap, so one run
    #: cannot spend its entire budget discovering that.
    max_duplicate_pages = max(1, int(getattr(
        config, "FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES", 40)))
    seg = metrics["segments"].setdefault(source_label, {
        "attempted": 0, "requests_succeeded": 0, "returned": 0, "schema_valid": 0,
        "schema_rejected": 0, "pii_dropped": 0, "non_us": 0, "source_filtered_out": 0,
        "duplicates": 0, "cross_source_duplicates": 0,
        "stop_reason": "", "http_status": None, "dispatched": False,
        "retries": 0, "failure_stage": "", "error_code": "", "error_class": "",
    })
    # Cross-source attribution state, shared by every segment of this run. The
    # id->source map is transient (dropped before metrics are persisted); only the
    # aggregate pair counts survive.
    attribution = metrics.setdefault("source_attribution", {})
    first_seen: Dict[str, str] = metrics.setdefault("_first_seen", {})
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
        # OFFSET = rows ALREADY RETURNED this call, never ``(page - 1) * want``.
        # ``want`` shrinks on the final page (min(cap - returned, 100)), so page
        # arithmetic re-requests an EARLIER offset whenever the cap is not a
        # multiple of the page size -- cap 250 pages 0, 100, then 2*50 = 100 AGAIN:
        # the tail rows are never inspected and the prefix is billed twice.
        # ``start_offset`` RESUMES a previously truncated pass (bootstrap
        # continuation): without it every run would re-page from offset 0, re-bill
        # the same prefix, dedupe it all, and never reach the window's tail. It is
        # persisted as start + rows returned, so contiguous paging is what makes
        # that saved cursor a valid resume point -- the two rules are one invariant.
        params.update({"limit": want, "offset": start_offset + returned})
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
        # ORDERING OBSERVATION -- free, and the only evidence we have.
        #
        # No `order_by`/`sort` parameter is sent (see build_jb_params), and the
        # provider documents no ordering, so the sequence rows arrive in is entirely
        # theirs. That sequence decides whether a persisted OFFSET is safe: if the
        # feed is newest-first, rows ageing out of the time_frame leave the tail and
        # a consumed prefix keeps its meaning; if it is oldest-first, they leave the
        # head and every saved offset points past rows nobody inspected.
        #
        # We already receive every row's date_created, so the direction can simply be
        # watched. Recorded, never acted on: this establishes the ordering from real
        # traffic instead of a probe assuming it.
        _observe_order(seg, rows)                       # documented sort: date_posted
        _observe_order(seg.setdefault("window_field_order", {}), rows, "date_created")
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
                # CROSS-SOURCE attribution: the same posting reached us from another
                # source first. Counted separately from intra-segment duplicates so
                # source overlap is measurable (the ID map itself is transient and is
                # dropped before metrics are persisted).
                owner = first_seen.get(job["job_id"])
                if owner and owner != source_label:
                    seg["cross_source_duplicates"] = seg.get("cross_source_duplicates", 0) + 1
                    pairs = attribution.setdefault("cross_source_pairs", {})
                    pair = f"{owner}->{source_label}"
                    pairs[pair] = pairs.get(pair, 0) + 1
                continue
            seen_ids.add(job["job_id"])
            first_seen[job["job_id"]] = source_label
            # Candidate-key collision MEASUREMENT (never a merge): how often would a
            # deterministic tuple key have matched a posting we kept from another
            # source? Feeds the source experiment; changes no dedupe decision.
            ck = candidate_cross_source_key(job)
            if ck:
                keymap = metrics.setdefault("_candidate_keys", {})
                owner = keymap.get(ck)
                if owner and owner[0] != source_label:
                    attribution["candidate_key_collisions"] = (
                        attribution.get("candidate_key_collisions", 0) + 1)
                    seg["candidate_key_collisions"] = seg.get("candidate_key_collisions", 0) + 1
                    # Full detail so a human can judge whether these are really the
                    # same opening. Both rows are KEPT: this never collapses anything.
                    details = attribution.setdefault("candidate_key_detail", [])
                    if len(details) < _CANDIDATE_COLLISION_DETAIL_CAP:
                        details.append({
                            "candidate_key": ck,
                            "source_a": owner[0], "provider_id_a": owner[1],
                            "source_b": source_label,
                            "provider_id_b": job.get("_fantastic_internal_id", ""),
                            "title": job.get("job_title", ""),
                            "company": job.get("employer_name", ""),
                            "domain": job.get("employer_website", ""),
                            "location": job.get("job_location", ""),
                            "posted_at": job.get("job_posted_at_datetime_utc", ""),
                            "url_a": owner[2], "url_b": job.get("canonical_source_url", ""),
                        })
                elif not owner:
                    keymap[ck] = (source_label, job.get("_fantastic_internal_id", ""),
                                  job.get("canonical_source_url", ""))
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
            # A FULL page in which every row was already seen. This is not
            # evidence of exhaustion -- see ``no_new_ids`` above and its absence
            # from ``_DRAINED_STOPS``. It is only evidence about this offset.
            #
            # Measured 2026-09-05: the first canonical-window run after the cursor
            # shipped opened a window whose prefix a PREVIOUS run had already
            # bought (3,000 rows per source), found no persisted offset because
            # that run predates the cursor, re-paged from 0, hit this branch on
            # page 1 and stopped. 200 rows billed, 0 net-new, cursor advanced 100.
            # At one page per run that is ~30 runs and ~6,000 credits to page back
            # to where the previous run had already reached.
            #
            # With a durable cursor the offset has already moved, so the next
            # page is genuinely further in and the spend is not repeated next run.
            # Stopping here throws that away.
            if not durable_cursor:
                seg["stop_reason"] = seg["stop_reason"] or "no_new_ids"
                break
            duplicate_pages += 1
            seg["duplicate_pages_skipped"] = duplicate_pages
            if duplicate_pages >= max_duplicate_pages:
                seg["stop_reason"] = seg["stop_reason"] or "duplicate_page_cap"
                break
        else:
            duplicate_pages = 0
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


def _fsync_dir(directory: str) -> None:
    """Best-effort fsync of a DIRECTORY so a completed rename survives power loss.

    POSIX only: Windows cannot open a directory handle for fsync, and the local
    dev/test environment does not need the guarantee (production is Linux).
    Never fatal -- if the platform or filesystem refuses, the snapshot content is
    already fsynced and the rename is still atomic; only durability of the rename
    across an abrupt power loss is weakened, never correctness.
    """
    if os.name != "posix":
        return
    fd = None
    try:
        fd = os.open(directory, os.O_RDONLY)
        os.fsync(fd)
    except Exception:  # noqa: BLE001 - strictly best-effort; must never propagate
        pass           # (it runs AFTER a successful publish, so raising here would
                       #  wrongly report failure for a snapshot that IS on disk)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:  # noqa: BLE001
                pass


def _write_quota_snapshot(*, jobs_remaining: Optional[int], requests_remaining: Optional[int],
                          next_billing_date: Any, jobs_limit: Optional[int] = None,
                          requests_limit: Optional[int] = None) -> bool:
    """Atomically persist ONE quota snapshot in the single `fantastic-quota-snapshot/1`
    format. Returns True only when the file was actually replaced.

    The optional ``*_limit`` fields are appended ONLY when supplied, so the
    acquisition path (which tracks no limits) keeps writing exactly the historical
    key set and no second/incompatible snapshot format is introduced.
    """
    path = str(getattr(config, "FANTASTIC_QUOTA_SNAPSHOT_PATH", "") or "")
    if not path or jobs_remaining is None:
        return False
    snap: Dict[str, Any] = {"schema": "fantastic-quota-snapshot/1",
                            "jobs_remaining": jobs_remaining,
                            "requests_remaining": requests_remaining,
                            "next_billing_date": next_billing_date,
                            "captured_at": datetime.now(timezone.utc).isoformat()}
    if jobs_limit is not None:
        snap["jobs_limit"] = jobs_limit
    if requests_limit is not None:
        snap["requests_limit"] = requests_limit
    directory = os.path.dirname(path) or "."
    tmp_path = ""
    try:
        os.makedirs(directory, exist_ok=True)
        # UNIQUE temp file in the DESTINATION directory: same filesystem (so the
        # replace is a true atomic rename) and no fixed name, so two concurrent
        # writers can never share -- or delete -- each other's temp file.
        handle, tmp_path = tempfile.mkstemp(dir=directory, prefix=".quota-snapshot-",
                                            suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(snap, fh)
            fh.flush()
            os.fsync(fh.fileno())       # CONTENT durable before it is published
        os.replace(tmp_path, path)      # atomic publish; readers see old or new
        tmp_path = ""                   # ownership transferred; nothing to clean
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("quota snapshot not persisted: %s", type(exc).__name__)
        return False
    finally:
        # Remove ONLY our own temp file, and only when the publish did not happen.
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    # PUBLISHED. Making the RENAME durable is strictly best-effort and lives
    # OUTSIDE the try: it must never turn an already-published snapshot into a
    # reported failure (which would block acquisition on a snapshot that is on disk).
    try:
        _fsync_dir(directory)
    except Exception:  # noqa: BLE001 - belt-and-braces around a best-effort helper
        pass
    return True


def _save_quota_snapshot(quota: "_QuotaState", metrics: Dict[str, Any]) -> None:
    """Persist the last-known provider quota headers so the NEXT run's governor can
    read remaining credits / reset date without a row-producing call. Best-effort."""
    _write_quota_snapshot(jobs_remaining=quota.jobs_remaining,
                          requests_remaining=quota.requests_remaining,
                          next_billing_date=metrics.get("next_billing_date", ""))


def refresh_quota_snapshot(http_get: Optional[HttpGet] = None) -> Dict[str, Any]:
    """Refresh the persisted provider quota snapshot with EXACTLY ONE count request.

    Why this exists: ``_save_quota_snapshot`` is only reachable from inside
    :func:`acquire`, so once the governor grants 0 the snapshot that caused the 0
    can never be rewritten -- the zero-budget state survives even a real provider
    quota reset. This is the only 0-row path that can break that deadlock.

    Contract:
      * ONE request to the count endpoint. No retry, no pagination, no job rows.
      * FAIL CLOSED. Any failure (exception, timeout, non-200, missing or
        malformed quota metadata, unwritable snapshot) leaves the persisted
        snapshot untouched and reports ``refreshed=False``. Unknown provider state
        is NEVER converted into permission to spend credits.

    Returns a dict describing the attempt; never raises.
    """
    out: Dict[str, Any] = {"refreshed": False, "reason": "", "http_status": None,
                           "requests_made": 0, "jobs_remaining": None,
                           "jobs_limit": None, "next_billing_date": None,
                           "endpoint": _COUNT_ENDPOINT}
    try:
        key = str(getattr(config, "FANTASTIC_JOBS_API_KEY", "") or "")
        if not key:
            out["reason"] = "no_api_key"
            return out
        base = str(getattr(config, "FANTASTIC_JOBS_BASE_URL", "") or "").rstrip("/")
        if not base:
            out["reason"] = "no_base_url"
            return out
        getter = http_get or _http_get
        # Narrowest useful window: the count VALUE is incidental, the response
        # HEADERS are the payload. date_created_gte/lt are proven-honored filters.
        now = datetime.now(timezone.utc).replace(microsecond=0)
        params = {"source": "linkedin", "exclude_ats_duplicate": "true",
                  "date_created_gte": _iso_z(now - timedelta(hours=1)),
                  "date_created_lt": _iso_z(now)}
        out["requests_made"] = 1  # counted as ATTEMPTED: an exception below is still one call
        resp = getter(f"{base}{_COUNT_ENDPOINT}",
                      headers={"Authorization": f"Bearer {key}"},
                      params=params,
                      timeout=int(getattr(config, "FANTASTIC_JOBS_REQUEST_TIMEOUT_SECONDS", 30) or 30))
        status = getattr(resp, "status_code", None)
        out["http_status"] = status
        if status != 200:
            out["reason"] = f"http_{status}"
            return out
        quota = _read_quota(getattr(resp, "headers", {}) or {})
        jobs_remaining = quota.get("jobs_remaining")
        # REQUIRED field. `_read_quota` already yields None for absent/unparseable
        # headers, so this rejects missing AND malformed values. Every other quota
        # header is OPTIONAL metadata (see the module docstring of the test suite).
        if not isinstance(jobs_remaining, int) or isinstance(jobs_remaining, bool):
            out["reason"] = "missing_quota_metadata"
            return out
        jobs_limit = quota.get("jobs_limit")
        clamps: List[str] = []
        # Negative remaining means EXHAUSTED, matching the governor's own header
        # convention (Gate-A BUG 3) -- one interpretation across the codebase.
        if jobs_remaining < 0:
            jobs_remaining = 0
            clamps.append("negative_to_zero")
        # Incoherent metadata (remaining > limit) is clamped DOWN to the limit:
        # conservative, and still breaks the deadlock. Rejecting outright would let
        # one odd provider response deadlock acquisition permanently.
        if isinstance(jobs_limit, int) and not isinstance(jobs_limit, bool) \
                and jobs_limit > 0 and jobs_remaining > jobs_limit:
            jobs_remaining = jobs_limit
            clamps.append("remaining_gt_limit")
        nbd = _valid_billing_date(quota.get("next_billing_date"))
        if not _write_quota_snapshot(jobs_remaining=jobs_remaining,
                                     requests_remaining=quota.get("requests_remaining"),
                                     next_billing_date=nbd,
                                     jobs_limit=jobs_limit,
                                     requests_limit=quota.get("requests_limit")):
            out["reason"] = "snapshot_not_persisted"
            return out
        if clamps:
            logger.warning("quota headers clamped before persisting: %s", ",".join(clamps))
        out.update({"refreshed": True, "reason": "ok", "jobs_remaining": jobs_remaining,
                    "jobs_limit": jobs_limit, "next_billing_date": nbd,
                    "requests_remaining": quota.get("requests_remaining"),
                    "clamps": clamps})
        return out
    except Exception as exc:  # noqa: BLE001 - fail closed on ANY provider/IO failure
        out["reason"] = f"error:{type(exc).__name__}"
        logger.warning("quota refresh failed (%s); keeping the existing zero-budget decision",
                       type(exc).__name__)
        return out


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
        return _iso_z(dt)

    def _slippage_minutes(self, horizon_dt: datetime) -> float:
        """Minutes the frame horizon has advanced since this window opened.

        0.0 for a fresh window, and 0.0 when the birth horizon was never recorded
        (state written by an older build) -- an unknown is not reported as movement."""
        born = str(self.state.get("frame_horizon_at_open") or "")
        if not born:
            return 0.0
        try:
            born_dt = datetime.fromisoformat(born.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return 0.0
        if born_dt.tzinfo is None:
            born_dt = born_dt.replace(tzinfo=timezone.utc)
        return round(max(0.0, (horizon_dt - born_dt).total_seconds() / 60.0), 1)

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
        # THE FRAME HORIZON. ``time_frame`` is intersected with ``date_created``
        # (proven live -- see _frame_horizon), so no window can reach below this
        # instant, and the instant advances with the clock. A window is therefore
        # only as durable as its distance from the horizon, and both the reuse path
        # and the derivation below have to be kept above it.
        margin = int(getattr(config, "FANTASTIC_TIME_FRAME_MARGIN_MINUTES", 30) or 0)
        horizon_dt = _frame_horizon(self.now, config.FANTASTIC_JOBS_TIME_FRAME, margin)
        horizon = self._iso(horizon_dt)
        abandoned: Optional[Dict[str, Any]] = None
        clamped: Optional[Dict[str, Any]] = None
        # Guard a corrupt in-flight record (marker without a parsable window start):
        # treat it as "no open window" rather than crashing acquisition uncaught.
        try:
            datetime.fromisoformat(str(self.state.get("window_start") or "").replace("Z", "+00:00"))
            start_ok = bool(self.state.get("window_start"))
        except (ValueError, TypeError):
            start_ok = False
        if in_flight and start_ok and str(in_flight) <= horizon:
            # The window's UPPER bound has itself fallen below the horizon: every row
            # it could ever return is now unreachable, so replaying it returns empty
            # forever. Nothing marks a source drained on an empty interval, so
            # ``window_drained`` never becomes true and ``commit_watermark`` never
            # advances -- the window stays open and acquisition stops indefinitely.
            # That is the ten-day zero-acquisition outage of 2026-08, and this is the
            # branch that ends it: abandon the dead window, record exactly what was
            # skipped, and derive a fresh in-frame one from where it ended.
            abandoned = {"lower": str(self.state.get("window_start") or ""),
                         "upper": str(in_flight), "horizon": horizon,
                         "reason": "window_entirely_below_frame_horizon"}
            prev, in_flight = str(in_flight), ""
            self.state["window_acquired_ids"] = []
            self.state["window_drained_sources"] = {}
            self.state["window_offsets"] = {}
            # ... and the slice cursor with it. A slice key is a date RANGE, so a
            # stale key from the previous window could align with a new window's
            # boundary and silently skip a range nobody paged. Cheaper to clear
            # than to reason about which overlaps are safe.
            self.state["window_slices"] = {}
            self.state["window_slice_offsets"] = {}
        if in_flight and start_ok:
            self.lower, self.upper = str(self.state.get("window_start")), str(in_flight)
            reused = True
            if self.lower < horizon:
                # Part of the window is still reachable, part is not. Raise the lower
                # bound to what the feed will actually serve, so the window states its
                # real extent and the conceded span is recorded instead of absorbed.
                #
                # This does NOT reset the persisted offsets, and must not: the feed
                # serves from max(lower, horizon) whether or not we say so, and this
                # only makes the request agree with the answer. It returns the same
                # rows either way, so there is nothing here for a cursor to lose --
                # while resetting on a window that sits near the horizon would rewind
                # the cursor on every run and rebuild the very livelock the durable
                # cursor was added to end.
                clamped = {"was": self.lower, "now": horizon,
                           "unreachable_days": round(
                               (horizon_dt - datetime.fromisoformat(
                                   self.lower.replace("Z", "+00:00"))).total_seconds() / 86400.0, 2)}
                self.lower = horizon
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
            if lower_dt < horizon_dt:
                # Opening below the horizon would build a window with a dead zone at
                # its old end from the very first request. Start where the feed can
                # actually answer, and say how far behind we were.
                clamped = {"was": self._iso(lower_dt), "now": horizon,
                           "unreachable_days": round(
                               (horizon_dt - lower_dt).total_seconds() / 86400.0, 2)}
                lower_dt = horizon_dt
            if lower_dt >= upper_dt:
                lower_dt = upper_dt  # run started inside the lag buffer: empty interval
            self.lower, self.upper = self._iso(lower_dt), self._iso(upper_dt)
            self.state["window_acquired_ids"] = []
            # A NEW window starts with every source un-drained. Never carried over:
            # a source must earn "drained" for the window it actually paged.
            self.state["window_drained_sources"] = {}
            # ... and at offset 0. A cursor is only meaningful for the window it
            # was measured in; carrying it forward would skip the new window's head.
            self.state["window_offsets"] = {}
            # Where the horizon stood when this window was born. The gap between that
            # and the horizon on a later run is how far the feed's floor has risen
            # underneath a window we are still paging with saved offsets -- see
            # ``frame_slippage_minutes``.
            self.state["frame_horizon_at_open"] = horizon
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
            "lag_minutes": lag, "overlap_minutes": overlap, "empty_interval": (self.lower == self.upper),
            # What the feed's own time_frame took off this window, stated rather than
            # absorbed: the horizon it could not reach below, the span conceded to it
            # (if any), and a window abandoned outright for falling entirely below it.
            # The PROVIDER's floor and OUR margin, reported apart so they can never
            # be confused. `frame_floor` is now - time_frame, which is the provider's
            # and is not ours to move. `frame_horizon` is what we actually clamped
            # to; with the margin at its default 0 they are the same instant, and any
            # gap between them is inventory WE chose to concede, not inventory the
            # feed refused.
            "frame_floor": self._iso(_frame_horizon(self.now, config.FANTASTIC_JOBS_TIME_FRAME, 0)),
            "frame_horizon": horizon, "frame_margin_minutes": margin,
            "lower_clamped_to_frame": clamped, "window_abandoned_below_frame": abandoned,
            # How far the feed's floor has risen since this window opened. Zero on a
            # fresh window. Above zero it means rows have left the result set that
            # the persisted offsets index into, and whether that SHIFTS the remaining
            # rows depends on an ordering the provider does not document and we have
            # not established -- newest-first would leave the consumed prefix intact,
            # oldest-first would not. Reported so the effect can be measured on a real
            # run rather than assumed in either direction.
            "frame_slippage_minutes": self._slippage_minutes(horizon_dt),
            # COVERAGE ACCOUNTING. Four different things get flattened into "the run
            # acquired N", and only the first is throughput:
            #
            #   inspected              rows this window has actually paid for and
            #                          looked at, per source (the saved offsets);
            #   outside_frame          the part of the window BELOW the provider's
            #                          floor -- reported as a SPAN, not a count,
            #                          because counting it needs a request we have
            #                          not spent;
            #   excluded_by_config     narrowed away by OUR filters before the feed
            #                          ever ranked a row. Not missing inventory: a
            #                          choice, listed so it reads as one;
            #   possibly_skipped       sources where an offset was resumed across a
            #                          moved floor, so rows may have shifted past it.
            #
            # An empty page tells you a query returned nothing NOW. It cannot tell
            # the four apart, and treating it as "everything was inspected" is how a
            # watermark advances over rows nobody read.
            "coverage": {
                "inspected_rows_by_source": dict(self.window_offsets()),
                "outside_frame": ({"from": self.state.get("window_start_requested") or self.lower,
                                   "to": horizon,
                                   "days": (clamped or {}).get("unreachable_days", 0.0),
                                   "counted": False}
                                  if clamped else None),
                "excluded_by_config": sorted(
                    k for k in _jb_filter_params()
                    if k not in ("limit", "offset")),
                "possibly_skipped_sources": [],
            },
            # The cursor state as it stood BEFORE this run paged anything. Production
            # acceptance of the persisted cursor is "the first offset we requested
            # equals the offset the previous run left behind", and that is not
            # answerable from the end state alone -- the run has already moved it.
            "offsets_at_open": dict(self.window_offsets()),
            "drained_at_open": dict(self.drained_sources()),
            # Filled per source by ``run_stream``: where each pass started, where it
            # ended, how many rows the provider billed to move it, and whether the
            # source finished the window.
            "window_cursors": {}}

    def run_stream(self, endpoint: str, base_params: Dict[str, Any], label: str,
                   cap_limit: int, accept: Optional[Tuple[str, ...]],
                   start_offset: int = 0) -> None:
        """One pass over [lower, upper) for a source; shares billing/dedupe with the
        head/deep engine via ``_fetch_segment``."""
        if not self.opened or self.lower == self.upper:
            return
        if self.source_already_drained(label):
            # This source finished the canonical window on an earlier run; another
            # source is why the window is still open. Re-paging it would re-bill
            # inventory we already have.
            self.metrics["segments"].setdefault(label, {}).setdefault(
                "stop_reason", "already_drained_this_window")
            # A drained source must still be VISIBLE in the cursor table, otherwise
            # "not repaged" and "never enabled" look identical in the artifacts.
            base_off = int(self.window_offsets().get(str(label), 0) or 0)
            self.metrics["watermark"].setdefault("window_cursors", {})[str(label)] = {
                "offset_from": base_off, "offset_to": base_off, "billed": 0,
                "passes": 0, "kept": 0, "drained": True,
                "stop_reason": "already_drained_this_window"}
            return
        # PERSISTED CURSOR, and the single source of truth for where to resume.
        # The caller's ``start_offset`` is progress made in THIS run only (it reads
        # the in-memory segment metrics), so without a durable base every run
        # re-paged a truncated window from offset 0: it re-billed the same prefix,
        # deduped all of it, and never reached the tail. The bootstrap path has
        # carried such an offset since it was written and its comment names the
        # same livelock; the canonical window simply never got one.
        #
        # It is deliberately NOT added to ``start_offset``: this cursor is advanced
        # after every pass, so by round 2 of the same run it already CONTAINS that
        # run's progress, and adding the two would skip a page per round.
        #
        # A saved offset is only a valid resume point while the window still selects
        # the same rows. It does NOT do so indefinitely: ``time_frame`` is
        # intersected with ``date_created``, so the window's effective lower bound
        # advances with the clock (proven 2026-09-05; see _frame_horizon). ``open()``
        # keeps the window above that horizon and resets these offsets on the one
        # transition where the set demonstrably changed, which is what makes the
        # cursor safe to carry across runs -- not any inherent stability of offset
        # paging.
        if bool(getattr(config, "FANTASTIC_WINDOW_SLICING_ENABLED", True)):
            # ``start_offset`` is deliberately IGNORED here. Callers pass it to make
            # a reclamation pass resume where the segment left off inside this run;
            # under slicing that is already the behaviour and better expressed --
            # the pass simply continues with slices that are not yet drained, and
            # cannot redo a finished one. The bootstrap path does not come through
            # here at all (it calls `_fetch_segment` with its own
            # ``source_bootstrap`` offsets against a different window).
            self._run_sliced(endpoint, base_params, label, cap_limit, accept)
            return

        base = int(self.window_offsets().get(str(label), 0) or 0)
        # The bound this offset was measured against, read BEFORE this pass
        # re-stamps it. Read afterwards it always equals the current bound,
        # which is exactly the drift it is supposed to reveal.
        base_basis = self.window_offset_basis().get(str(label), "")
        # BILLED, not KEPT (see _fetch_segment's billing-accurate cap): the provider
        # bills every RETURNED row, so measuring cross-segment consumption by kept
        # jobs would let dup-heavy segments overspend the shared run budget.
        room = min(cap_limit, self.run_cap - self.quota.jobs_consumed)
        if room <= 0:
            return
        params = dict(base_params)
        params["date_created_gte"] = self.lower
        params["date_created_lt"] = self.upper
        before = self.quota.jobs_consumed
        got = _fetch_segment(endpoint, params, label, room, self.quota, self.http_get,
                             self.seen_ids, self.metrics, accept_source=accept,
                             start_offset=base, durable_cursor=True)
        # Advance by every row the provider RETURNED for this label in this pass
        # (billed rows, not kept rows -- the next unseen row sits exactly after
        # them because paging is contiguous). Same accounting the bootstrap uses.
        billed_this_pass = self.quota.jobs_consumed - before
        self.record_window_offset(label, base + billed_this_pass)
        self.acquired.extend(got)
        self.result.jobs.extend(got)
        self.metrics["watermark"]["acquired"] = self.metrics["watermark"].get("acquired", 0) + len(got)
        # COVERAGE, not just position. Running out of rows proves this source is
        # exhausted only if the offset we resumed from still addressed the row it
        # addressed when it was saved. It does not when BOTH of these hold:
        #
        #   * the pass started from a saved offset rather than the head, and
        #   * the frame floor has risen since the window opened, so rows have left
        #     the result set that offset indexes into.
        #
        # Under a newest-first feed those rows leave the tail and nothing moves.
        # Under an oldest-first feed they leave the head, every later row shifts down
        # by the number that left, and the saved offset lands that far past where it
        # pointed -- stepping over rows no request ever inspected. The feed is asked
        # for no ordering and documents none, so which of those we are in is not
        # established (``order_probe`` is now recording it from real traffic).
        #
        # The danger is not the wasted page; it is that ``empty_page`` is a DRAINED
        # stop, so an over-shot offset reaches the end of a shrunken set, the window
        # commits, and the watermark advances past those rows permanently.
        #
        # So: do not accept that drain. Rewind this source to the head instead. The
        # next run re-pages the window from 0, ``seen_ids`` discards what we already
        # hold, ``duplicate_page_cap`` bounds what that costs, and the durable cursor
        # lets it page THROUGH the duplicates to the rows that were skipped. It can
        # happen at most once per window per source -- the re-pass starts at 0, so
        # this condition cannot hold for it -- and the window then drains honestly.
        seg_now = (self.metrics.get("segments", {}).get(label) or {})
        slipped = float(self.metrics["watermark"].get("frame_slippage_minutes") or 0.0)
        stop = str(seg_now.get("stop_reason") or "")
        # ASSESS ALWAYS, ACT NARROWLY. This previously required a DRAINED stop, so a
        # source that ran out of BUDGET while the floor was cutting into its window
        # was never assessed at all -- and that is the more exposed case, not the
        # less: it carries an unfinished offset into a set that has since moved. On
        # 2026-09-06 both sources stopped `cap_reached` with `lower_clamped_to_frame`
        # true, and the run recorded no coverage doubt whatsoever.
        #
        # The rewind DECISION is deliberately left where it was. Sending a
        # budget-stopped source back to the head would make it re-page a prefix it
        # already holds, and the duplicate-page cap cannot stop that within one
        # grant -- so it would spend the budget without reaching the tail. What is
        # fixed here is the truthfulness of the coverage state, not the behaviour.
        at_risk = bool(base > 0 and slipped > 0)
        exposed = at_risk
        rewind_eligible = at_risk and stop in self._DRAINED_STOPS
        # IS THE FLOOR INSIDE THE WINDOW? That, not an observed direction, decides
        # whether rows are actually leaving the set this offset indexes into.
        #
        # The provider sorts pages by `date_posted` DESC (documented), while the
        # frame floor rises on `date_created`. Those correlate but are not the same
        # instant, so rows dropping below the floor leave from scattered positions in
        # the page order -- not from one end. No observed sort direction can make a
        # persisted offset safe across that, which is why the earlier gate on
        # `order_observed` was answering a question the ordering cannot settle.
        #
        # What CAN be settled is whether the floor cuts the window at all. While
        # `lower >= horizon` the whole window sits inside the frame and the floor
        # removes nothing, so the offset still addresses what it addressed -- and in
        # steady state that is every run, because a one-day window opened from
        # yesterday's watermark sits six days above a 7d floor. The guard fires only
        # for a window the floor has actually reached, which is the backlog case.
        horizon = str(self.metrics["watermark"].get("frame_horizon") or "")
        # Read the CLAMP, not the bounds. `open()` raises a reused window's lower
        # bound to the horizon when the floor has reached into it, so by the time we
        # are here `lower == horizon` and comparing them can never be true again --
        # the evidence that the floor cut in is that the clamp fired.
        floor_inside_window = bool(self.metrics["watermark"].get("lower_clamped_to_frame"))
        order = str((seg_now.get("order_probe") or {}).get("order_observed") or "")
        adverse = floor_inside_window
        if exposed:
            seg_now["coverage_uncertain"] = {
                "resumed_from": base, "frame_slippage_minutes": slipped,
                "window_lower": self.lower, "frame_horizon": horizon,
                "floor_inside_window": floor_inside_window,
                "page_order_observed": order or "not_yet_observed",
                "drained_stop": stop,
                "stop_reason": stop,
                "rewind_eligible": rewind_eligible,
                # The bound this offset was measured against, versus the one in
                # force now. Different values mean the resumed index addresses a
                # different result set.
                "offset_basis": base_basis,
                "offset_basis_now": self.lower,
                "offset_basis_changed": bool(base_basis and base_basis != self.lower),
            }
        already_rewound = self.coverage_rewinds().get(str(label), 0) > 0
        if exposed and not adverse:
            seg_now["coverage_uncertain"]["resolution"] = "floor_outside_window"
            seg_now["coverage_uncertain"]["action"] = (
                "window allowed to close; the frame floor never reached into it")
        uncertain = rewind_eligible and adverse
        if exposed:
            skipped = self.metrics["watermark"].setdefault("coverage", {}).setdefault(
                "possibly_skipped_sources", [])
            if str(label) not in skipped:
                skipped.append(str(label))
        if uncertain and not already_rewound:
            self.rewind_window_offset(label)
            seg_now["coverage_uncertain"]["resolution"] = "rewound"
            seg_now["coverage_uncertain"]["action"] = (
                "offset rewound to 0; window held open for a full re-pass")
        else:
            if uncertain:
                # Already re-passed once and still cannot be certain. Accept the
                # drain so the window can close and acquisition keeps moving, but
                # SAY SO: the closed-window audit re-counts each window at zero Jobs
                # credits, and a count above what we acquired is how a skipped row
                # gets found after the fact.
                seg_now["coverage_uncertain"]["resolution"] = "accepted_after_rewind"
                seg_now["coverage_uncertain"]["action"] = (
                    "window allowed to close; recheck the closed-window count")
            self.mark_source_drained(label)
        # CURSOR OBSERVABILITY. Without this the only evidence that the persisted
        # cursor was honoured is a diff of two runs' saved state, which nobody has
        # at report time. ``offset_from`` is pinned to the FIRST pass of this run
        # (the resume point); ``offset_to`` and the billed total accumulate.
        cursors = self.metrics["watermark"].setdefault("window_cursors", {})
        cur = cursors.setdefault(str(label), {"offset_from": base, "offset_to": base,
                                              "billed": 0, "passes": 0})
        cur["offset_to"] = base + billed_this_pass
        cur["billed"] = int(cur.get("billed", 0)) + billed_this_pass
        cur["passes"] = int(cur.get("passes", 0)) + 1
        cur["kept"] = int(cur.get("kept", 0)) + len(got)
        seg = self.metrics.get("segments", {}).get(str(label)) or {}
        cur["stop_reason"] = seg.get("stop_reason", "")
        cur["drained"] = bool(self.drained_sources().get(str(label)))

    # Segment stop reasons that mean the window was NATURALLY exhausted. Anything
    # else (cap hit, quota reserve, page_cap, request error, rate limit) means the
    # window was TRUNCATED and the watermark must NOT advance (Gate-B 5A: a partial
    # window that commits loses every un-fetched in-window job permanently).
    #
    # ``no_new_ids`` is deliberately NOT here. It fires on a FULL page whose rows
    # were all already seen -- the overlap band, a cross-source duplicate run, or a
    # replayed prefix. A full page of duplicates says nothing about what lies
    # deeper, so treating it as exhaustion committed the watermark past inventory
    # no request ever inspected. Combined with the missing per-source offset that
    # was exactly the observed failure: run 1 caps at 3000/1000-row window, run 2
    # re-pages the same prefix, reports ``no_new_ids``, is called drained, and the
    # remaining 700 rows are lost behind the advancing watermark forever.
    _DRAINED_STOPS = frozenset({"", "empty_page", "short_page"})

    # -- first-enablement bootstrap ------------------------------------------------
    def ensure_bootstraps(self, enabled_labels: Tuple[str, ...]) -> None:
        """Give a source enabled for the FIRST TIME a bounded historical backfill.

        The canonical watermark has already advanced past inventory that a brand-new
        source never saw. Its bootstrap window is
        ``[canonical_upper - configured_lookback, canonical_lower)`` -- bounded by
        ``FANTASTIC_JOBS_TIME_FRAME``, never unlimited history.

        Recorded per source and idempotent: a source that already has a record
        (drained or not) keeps it, so disabling and re-enabling resumes rather than
        restarting. Bootstrap NEVER moves the canonical watermark.
        """
        if not bool(getattr(config, "FANTASTIC_SOURCE_BOOTSTRAP_ENABLED", True)):
            return
        boots = dict(self.state.get("source_bootstrap") or {})
        # TWO cases record every enabled source as complete instead of granting it a
        # backfill, and the PRESENCE of the key -- not its contents -- separates them
        # from a genuine newcomer:
        #
        #   * FIRST EVER RUN -- the canonical window already covers the lookback, so
        #     there is no history behind it;
        #   * UPGRADE -- state written before this code existed has an advanced
        #     watermark and NO ``source_bootstrap`` key at all. Every source enabled
        #     at that moment has been running all along and owes no backfill.
        #     Without this, the first run after deploying would hand each LIVE source
        #     a full-lookback re-page of inventory it has already processed, and the
        #     reserve would fund that re-page on every run from then on.
        #
        # Once the key exists, a label missing from it is a REAL first enablement.
        if "source_bootstrap" not in self.state or not self.state.get("last_successful_watermark"):
            reason = ("no_history_behind_first_window"
                      if not self.state.get("last_successful_watermark")
                      else "pre_existing_source_at_upgrade")
            for lbl in enabled_labels:
                boots.setdefault(str(lbl), {"lower": self.lower, "upper": self.lower,
                                            "drained": True, "reason": reason})
            self.state["source_bootstrap"] = boots
            return
        try:
            b_upper = datetime.fromisoformat(self.lower.replace("Z", "+00:00"))
            hours = _parse_time_frame_hours(config.FANTASTIC_JOBS_TIME_FRAME)
            b_lower = datetime.fromisoformat(self.upper.replace("Z", "+00:00")) - timedelta(hours=hours)
        except (ValueError, TypeError):
            return
        for lbl in enabled_labels:
            key = str(lbl)
            if key in boots:
                continue                       # already bootstrapped or in progress
            if b_lower >= b_upper:
                boots[key] = {"lower": self._iso(b_upper), "upper": self._iso(b_upper),
                              "drained": True, "reason": "canonical_window_covers_lookback"}
            else:
                boots[key] = {"lower": self._iso(b_lower), "upper": self._iso(b_upper),
                              "drained": False}
        self.state["source_bootstrap"] = boots

    def bootstrap_pending(self, label: str) -> Optional[Dict[str, Any]]:
        rec = (self.state.get("source_bootstrap") or {}).get(str(label))
        if not isinstance(rec, dict) or rec.get("drained"):
            return None
        if str(rec.get("lower")) >= str(rec.get("upper")):
            return None
        return rec

    def mark_bootstrap_drained(self, label: str) -> None:
        seg = (self.metrics.get("segments") or {}).get(f"{label}::bootstrap") or {}
        ok = (not seg.get("error_code")
              and str(seg.get("stop_reason") or "") in self._DRAINED_STOPS)
        boots = dict(self.state.get("source_bootstrap") or {})
        rec = dict(boots.get(str(label)) or {})
        if rec:
            rec["drained"] = bool(rec.get("drained")) or bool(ok)
            boots[str(label)] = rec
            self.state["source_bootstrap"] = boots

    def run_bootstrap(self, endpoint: str, base_params: Dict[str, Any], label: str,
                      cap_limit: int, accept: Optional[Tuple[str, ...]]) -> None:
        """Page the source's BOUNDED historical window. Separate from steady state:
        it uses its own segment label, its own drain flag, and cannot influence the
        canonical window's advancement in either direction."""
        rec = self.bootstrap_pending(label)
        if rec is None or not self.opened:
            return
        room = min(cap_limit, self.run_cap - self.quota.jobs_consumed)
        if room <= 0:
            return
        params = dict(base_params)
        params["date_created_gte"] = rec["lower"]
        params["date_created_lt"] = rec["upper"]
        blabel = f"{label}::bootstrap"
        # CONTINUATION: resume where the previous (truncated) pass stopped. Without
        # a persisted offset a capped bootstrap re-pages from 0 every run, re-bills
        # the same prefix, dedupes it entirely and NEVER reaches the tail -- a
        # livelock on page 1. Offset paging over a historical, lag-bounded window is
        # stable; a row inserted behind the cursor could be missed, which is the
        # deliberate trade against re-billing forever (seen_ids still dedupes).
        start = int(rec.get("offset", 0) or 0)
        before = self.quota.jobs_consumed
        got = _fetch_segment(endpoint, params, blabel, room, self.quota, self.http_get,
                             self.seen_ids, self.metrics, accept_source=accept,
                             start_offset=start, durable_cursor=True)
        consumed = self.quota.jobs_consumed - before
        self.acquired.extend(got)
        self.result.jobs.extend(got)
        boots = dict(self.state.get("source_bootstrap") or {})
        cur = dict(boots.get(str(label)) or {})
        cur["offset"] = start + consumed
        cur["acquired"] = int(cur.get("acquired", 0) or 0) + len(got)
        boots[str(label)] = cur
        self.state["source_bootstrap"] = boots
        self.mark_bootstrap_drained(label)
        self.metrics.setdefault("bootstrap", {})[label] = {
            "lower": rec["lower"], "upper": rec["upper"], "acquired": len(got),
            "offset_from": start, "offset_to": start + consumed,
            "drained": bool((self.state.get("source_bootstrap") or {}).get(label, {}).get("drained"))}

    # -- per-source drain state ---------------------------------------------------
    def drained_sources(self) -> Dict[str, bool]:
        m = self.state.get("window_drained_sources")
        return dict(m) if isinstance(m, dict) else {}

    def window_offsets(self) -> Dict[str, int]:
        """Rows already returned per source for the CURRENT window, across runs."""
        m = self.state.get("window_offsets")
        return {str(k): int(v or 0) for k, v in m.items()} if isinstance(m, dict) else {}

    def record_window_offset(self, label: str, value: int) -> None:
        """Persist the resume point. Never moves backwards: a later pass that
        returned nothing must not rewind a cursor an earlier pass advanced.

        The window's LOWER BOUND at the time of measurement is stamped alongside it.
        An offset is an index into a result set, and it only means what it meant
        while that set is the same one. The frame floor rises between runs whether
        or not we clamp, so a resumed offset can silently address different rows --
        the stamp is what makes that detectable instead of invisible.
        """
        m = self.window_offsets()
        m[str(label)] = max(int(m.get(str(label), 0) or 0), int(value or 0))
        self.state["window_offsets"] = m
        basis = dict(self.state.get("window_offset_basis") or {})
        basis[str(label)] = self.lower
        self.state["window_offset_basis"] = basis

    def window_offset_basis(self) -> Dict[str, str]:
        """The window lower bound each persisted offset was measured against."""
        m = self.state.get("window_offset_basis")
        return {str(k): str(v or "") for k, v in m.items()} if isinstance(m, dict) else {}

    #: Slices of the window, by `date_created`, that a source has DRAINED.
    #:
    #: This is the cursor that replaces a cross-run offset. An offset is an index
    #: into a result set, and the provider documents it only for draining a set in
    #: ONE pass -- "keep making requests until the API returns less jobs than the
    #: limit". Nothing documents that an index still addresses the same row on a
    #: later day, and the rising 7-day frame floor guarantees it does not. A
    #: `date_created` boundary has no such problem: a row's `date_created` never
    #: changes, so a drained slice is drained forever and a slice never revisits
    #: rows another slice already took.
    def window_slices_done(self) -> Dict[str, List[str]]:
        m = self.state.get("window_slices")
        if not isinstance(m, dict):
            return {}
        return {str(k): [str(x) for x in (v or [])] for k, v in m.items()}

    #: Progress WITHIN a slice that a run could not finish. Safe where a
    #: whole-window offset is not: a slice is a narrow `date_created` range, so a
    #: row leaves it only when the entire slice drops below the frame floor -- at
    #: which point the slice is unreachable anyway and its offset is moot. Without
    #: this an unfinished slice restarts from zero every run and re-buys what it
    #: already took, which is exactly how a starved budget stops making progress.
    def window_slice_offsets(self) -> Dict[str, Dict[str, int]]:
        m = self.state.get("window_slice_offsets")
        if not isinstance(m, dict):
            return {}
        return {str(k): {str(kk): int(vv or 0) for kk, vv in (v or {}).items()}
                for k, v in m.items()}

    def record_slice_offset(self, label: str, key: str, value: int) -> None:
        m = self.window_slice_offsets()
        per = m.setdefault(str(label), {})
        per[key] = max(int(per.get(key, 0) or 0), int(value or 0))
        self.state["window_slice_offsets"] = m

    def mark_slice_done(self, label: str, key: str) -> None:
        m = self.window_slices_done()
        done = m.setdefault(str(label), [])
        if key not in done:
            done.append(key)
        self.state["window_slices"] = m

    def window_slice_bounds(self) -> List[Tuple[str, str]]:
        """`[lower, upper)` cut into fixed `date_created` slices, OLDEST FIRST.

        Oldest first is not cosmetic: the rows closest to the frame floor are the
        ones about to become permanently unreachable, so they are the ones a limited
        budget should buy before they expire.
        """
        hours = max(1, int(getattr(config, "FANTASTIC_WINDOW_SLICE_HOURS", 6) or 6))
        try:
            lo = datetime.fromisoformat(self.lower.replace("Z", "+00:00"))
            hi = datetime.fromisoformat(self.upper.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return []
        out: List[Tuple[str, str]] = []
        cur = lo
        while cur < hi and len(out) < 400:
            nxt = min(cur + timedelta(hours=hours), hi)
            out.append((self._iso(cur), self._iso(nxt)))
            cur = nxt
        return out

    def coverage_rewinds(self) -> Dict[str, int]:
        """How many times each source was sent back to the head of THIS window."""
        m = self.state.get("window_coverage_rewinds")
        return {str(k): int(v or 0) for k, v in m.items()} if isinstance(m, dict) else {}

    def rewind_window_offset(self, label: str) -> None:
        """Send a source back to the head of the current window, once.

        ``record_window_offset`` is deliberately monotonic -- a later pass that
        returned nothing must never undo an earlier pass's progress -- so a rewind
        cannot go through it and is written directly. The counter is what keeps this
        terminating: a window whose consumed prefix is longer than the duplicate-page
        cap cannot page back to its tail in one run, so without a bound the refuse ->
        rewind -> partial re-pass -> refuse cycle could repeat indefinitely. One
        rewind per source per window; after that the drain is accepted and the
        residual doubt is carried into the closed-window record instead.
        """
        offsets = self.window_offsets()
        offsets[str(label)] = 0
        self.state["window_offsets"] = offsets
        counts = self.coverage_rewinds()
        counts[str(label)] = counts.get(str(label), 0) + 1
        self.state["window_coverage_rewinds"] = counts
        self._save()

    def source_already_drained(self, label: str) -> bool:
        """True when THIS source already exhausted the CURRENT window.

        Lets a re-opened (reused) window continue only the sources that still owe
        inventory, instead of re-billing sources that already finished -- the
        failure mode that made a shared window unsafe with 3-4 sources.
        """
        return bool(self.drained_sources().get(str(label)))

    def mark_source_drained(self, label: str) -> None:
        """Record whether a source exhausted the window, from ITS OWN segment stats.

        A source is drained only on a natural end. Any error, cap, quota reserve or
        page-cap leaves it NOT drained, so it resumes on the next run and the global
        watermark stays put.
        """
        seg = (self.metrics.get("segments") or {}).get(str(label)) or {}
        ok = (not seg.get("error_code")
              and str(seg.get("stop_reason") or "") in self._DRAINED_STOPS)
        m = self.drained_sources()
        # Never downgrade: a source drained earlier in this window stays drained even
        # if a later pass records nothing for it.
        m[str(label)] = bool(m.get(str(label))) or bool(ok)
        self.state["window_drained_sources"] = m

    def window_drained(self, enabled_labels: Tuple[str, ...]) -> bool:
        """The CANONICAL window is drained only when EVERY ENABLED source drained.

        ``enabled_labels`` is REQUIRED and is the authoritative enabled set. It is
        deliberately not defaulted to ``metrics["segments"].keys()``: those are the
        segments that HAPPENED TO EXECUTE, so an aborted run (or one where a source
        never got budget) would look "fully drained" and advance the window past
        inventory no source ever inspected.

        Scoping to the enabled set is also what stops a stale entry (a source later
        removed from config) from blocking advancement forever, and stops a newly
        enabled source from inheriting another source's completion.
        """
        if self.lower == self.upper:
            return True                                   # empty interval: trivially drained
        if not enabled_labels:
            return False
        drained = self.drained_sources()
        return all(bool(drained.get(str(lbl))) for lbl in enabled_labels)

    def _run_sliced(self, endpoint: str, base_params: Dict[str, Any], label: str,
                    cap_limit: int, accept: Optional[Tuple[str, ...]]) -> None:
        """Drain the window one `date_created` slice at a time, oldest first.

        Each slice is paged from offset 0 WITHIN THIS RUN, which is exactly the
        documented usage. Nothing about a slice's position is carried between runs;
        what persists is the fact that the slice is finished, and that fact stays
        true however the feed reorders itself.
        """
        done = set(self.window_slices_done().get(str(label), []))
        slices = self.window_slice_bounds()
        # NOT `setdefault(label, {})`: `_fetch_segment` initialises the segment with
        # its full counter shape only when the key is absent, so pre-creating an
        # empty dict here left it without `attempted` and the first call raised.
        stats = self.metrics["watermark"].setdefault("slices", {}).setdefault(str(label), {
            "total": len(slices), "already_done": 0, "drained_now": 0,
            "attempted": 0, "billed": 0, "kept": 0, "budget_exhausted": False})
        stats["total"] = len(slices)
        stats["already_done"] = sum(1 for lo, hi in slices if f"{lo}|{hi}" in done)

        for lo, hi in slices:
            key = f"{lo}|{hi}"
            if key in done:
                continue
            room = min(cap_limit, self.run_cap - self.quota.jobs_consumed)
            if room <= 0:
                stats["budget_exhausted"] = True
                seg = self.metrics["segments"].get(label)
                if seg is not None:
                    seg["stop_reason"] = seg.get("stop_reason") or "cap_reached"
                break
            params = dict(base_params)
            params["date_created_gte"] = lo
            params["date_created_lt"] = hi
            resume = int(self.window_slice_offsets().get(str(label), {}).get(key, 0) or 0)
            # Read this slice's OWN stop reason: `_fetch_segment` only ever sets the
            # segment's reason if it is empty, so an earlier slice's reason would
            # otherwise mask every later one.
            seg_key = self.metrics["segments"].get(label)
            carried = (seg_key.get("stop_reason") or "") if seg_key else ""
            if seg_key is not None:
                seg_key["stop_reason"] = ""
            before = self.quota.jobs_consumed
            got = _fetch_segment(endpoint, params, label, room, self.quota,
                                 self.http_get, self.seen_ids, self.metrics,
                                 accept_source=accept, start_offset=resume,
                                 durable_cursor=True)
            seg_key = self.metrics["segments"].get(label) or {}
            slice_stop = str(seg_key.get("stop_reason") or "")
            if seg_key:
                seg_key["stop_reason"] = carried or slice_stop
            billed = self.quota.jobs_consumed - before
            stats["attempted"] += 1
            stats["billed"] += billed
            stats["kept"] += len(got)
            self.acquired.extend(got)
            self.result.jobs.extend(got)
            self.metrics["watermark"]["acquired"] = (
                self.metrics["watermark"].get("acquired", 0) + len(got))
            # A slice is finished only when the FEED ran out inside it. Stopping on
            # budget, a repeated page or a quota breach proves nothing about the
            # slice, and marking it done would strand whatever it still holds.
            if slice_stop in ("empty_page", "short_page"):
                self.mark_slice_done(label, key)
                done.add(key)
                stats["drained_now"] += 1
            elif billed:
                # Unfinished: remember how far into THIS slice we got, so the next
                # run continues rather than re-buying the prefix.
                self.record_slice_offset(label, key, resume + billed)

        if len(done) >= len(slices) and slices:
            self.mark_source_drained_from_slices(label)
        # DELIBERATELY NOT `self._save()`. Slice progress is continuation state, and
        # continuation must not become durable before the rows it advances past are
        # in custody. `checkpoint()` runs the custody hook and only then saves, so
        # persisting here would reopen the exact gap the hook exists to close: a run
        # that died in between would have recorded slices as drained while nothing
        # held their rows. In-memory progress is enough -- a run that never reaches
        # checkpoint has bought nothing it can keep either.

    def mark_source_drained_from_slices(self, label: str) -> None:
        """Every slice of this window drained -- the source is genuinely finished.

        This is a stronger statement than the offset path could ever make: it means
        every `date_created` sub-range was paged to exhaustion, not that one index
        stopped returning rows.
        """
        m = self.drained_sources()
        m[str(label)] = True
        self.state["window_drained_sources"] = m

    def checkpoint(self, enabled_labels: Tuple[str, ...]) -> None:
        """After this adapter call: persist the IDs acquired so far in the OPEN window
        (still in-flight; the watermark does NOT advance) and whether the window was
        fully DRAINED. A later slice or a crash replay dedupes against the IDs and
        never re-bills. Committing is the PIPELINE's job (``commit_watermark``),
        after processing + persistence (Gate-E D4).

        ``enabled_labels`` is the set of sources ENABLED for this run: the canonical
        window is drained only when every one of them drained, so a stale entry for a
        removed source cannot block advancement and a newly enabled source cannot
        inherit someone else's completion."""
        if not self.opened:
            return
        # CUSTODY BEFORE CONTINUATION. `_save()` below makes `window_offsets`
        # durable, and those offsets are replayed FORWARD -- once saved, the rows
        # between the old and new offset are never requested again. So anything
        # acquired has to be safely held FIRST. A hook that fails leaves the cursor
        # exactly where it was: the next run re-bills those pages, which costs
        # credits, where advancing would cost the work itself.
        if _CUSTODY_HOOK is not None and self.acquired:
            try:
                held = bool(_CUSTODY_HOOK(list(self.acquired)))
            except Exception as exc:  # noqa: BLE001
                held = False
                self.metrics["watermark"]["custody_error"] = f"{type(exc).__name__}: {exc}"
            if not held:
                self.metrics["watermark"]["custody_failed"] = True
                self.metrics["watermark"]["committed"] = False
                self.metrics["watermark"]["offsets_not_advanced"] = True
                return
            self.metrics["watermark"]["custody_ok"] = True

        ids = set(str(i) for i in (self.state.get("window_acquired_ids") or []))
        ids |= {str(j.get("_fantastic_internal_id")) for j in self.acquired if j.get("_fantastic_internal_id")}
        self.state["window_acquired_ids"] = sorted(ids)
        self.state["window_drained"] = bool(self.window_drained(enabled_labels))
        self._save()
        self.metrics["watermark"]["committed"] = False
        self.metrics["watermark"]["drained"] = self.state["window_drained"]
        self.metrics["watermark"]["drained_sources"] = self.drained_sources()
        self.metrics["watermark"]["enabled_sources"] = list(enabled_labels or ())
        # The offsets as PERSISTED for the next run, paired with the ones read at
        # open. Together they are the whole cursor acceptance test: resumed-from,
        # advanced-to, and which sources are still owed pages in this window.
        self.metrics["watermark"]["offsets_at_close"] = dict(self.window_offsets())
        self.metrics["watermark"]["undrained_sources"] = sorted(
            str(lbl) for lbl in (enabled_labels or ())
            if not bool(self.drained_sources().get(str(lbl))))


def audit_closed_windows(http_get: HttpGet, base_params: Dict[str, Any],
                         metrics: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """VISIBILITY-LAG SELF-AUDIT -- 0 Jobs credits, count endpoint only.

    The 180-minute lag buffer is an assumption, not a measurement. Each run
    re-counts ONE previously closed ``date_created`` window with the count endpoint
    (which returns no rows and therefore bills no Jobs credits) and compares against
    the count recorded when that window was closed. Rows appearing later are, by
    definition, records that became visible AFTER we declared the window complete --
    i.e. real evidence that the configured lag is too small.

    On detected late growth the audit does NOT silently mutate state: it records the
    evidence, raises a loud metric/warning, and reports the observed maximum so the
    lag can be widened deliberately (config), with head/deep remaining the safe
    fallback. Never raises into acquisition.
    """
    out: Dict[str, Any] = {"performed": False}
    try:
        eng = DateCreatedWatermarkEngine(result=None, quota=None, http_get=None,
                                         seen_ids=set(), metrics={}, run_cap=0)
        st = eng.state
        closed = list(st.get("closed_windows") or [])
        if not closed:
            out["reason"] = "no_closed_windows_yet"
            metrics["watermark_audit"] = out
            return out
        now = now or datetime.now(timezone.utc)
        # Audit the oldest window not yet re-checked enough times (bounded work: 1/run).
        target = min(closed, key=lambda w: (int(w.get("rechecks", 0)), str(w.get("upper", ""))))
        params = dict(base_params)
        params["date_created_gte"] = target["lower"]
        params["date_created_lt"] = target["upper"]
        params.pop("limit", None); params.pop("offset", None)
        url = f"{config.FANTASTIC_JOBS_BASE_URL.rstrip('/')}{_COUNT_ENDPOINT}"
        headers = {"Authorization": f"Bearer {config.FANTASTIC_JOBS_API_KEY}"}
        resp = http_get(url, headers=headers, params=params,
                        timeout=config.FANTASTIC_JOBS_REQUEST_TIMEOUT_SECONDS)
        if getattr(resp, "status_code", 0) != 200:
            out["reason"] = f"count_http_{getattr(resp, 'status_code', '?')}"
            metrics["watermark_audit"] = out
            return out
        body = resp.json()
        later = body if isinstance(body, int) else None
        if later is None and isinstance(body, dict):
            for k in ("count", "total", "total_count"):
                if isinstance(body.get(k), int):
                    later = body[k]; break
        if later is None:
            out["reason"] = "count_unparseable"
            metrics["watermark_audit"] = out
            return out
        first = int(target.get("count_at_close", 0) or 0)
        try:
            closed_at = datetime.fromisoformat(str(target.get("closed_at")).replace("Z", "+00:00"))
            hours = round((now - closed_at).total_seconds() / 3600.0, 2)
        except (ValueError, TypeError):
            hours = None
        growth = max(0, later - first)
        target["rechecks"] = int(target.get("rechecks", 0)) + 1
        target["last_count"] = later
        target["late_growth"] = growth
        st["closed_windows"] = closed[-int(getattr(config, "FANTASTIC_WATERMARK_AUDIT_KEEP", 12) or 12):]
        if growth > 0:
            st["observed_late_growth_max"] = max(int(st.get("observed_late_growth_max", 0) or 0), growth)
            st["observed_late_growth_hours"] = hours
        eng._save()
        out.update({"performed": True, "window": f'{target["lower"]}..{target["upper"]}',
                    "count_at_close": first, "later_count": later,
                    "hours_since_close": hours, "late_growth": growth,
                    "observed_late_growth_max": int(st.get("observed_late_growth_max", 0) or 0),
                    "configured_lag_minutes": int(getattr(config, "FANTASTIC_DATE_CREATED_LAG_MINUTES", 0) or 0)})
        if growth > 0:
            out["ALERT"] = ("late_visible_rows_after_window_close -- configured lag is "
                            "TOO SMALL for this provider; widen FANTASTIC_DATE_CREATED_LAG_MINUTES")
            logger.warning("WATERMARK LAG ALERT: window %s gained %d rows %.2fh after close "
                           "(lag=%dm). Widen the lag buffer; head/deep remains the safe fallback.",
                           out["window"], growth, hours or -1.0,
                           int(getattr(config, "FANTASTIC_DATE_CREATED_LAG_MINUTES", 0) or 0))
    except Exception as exc:  # noqa: BLE001 - the audit never affects acquisition
        out["reason"] = f"audit_error:{type(exc).__name__}"
    metrics["watermark_audit"] = out
    return out


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
    # Record the closed window + the count we believed complete, so a later run can
    # re-count it (0 Jobs credits) and MEASURE the provider's true visibility lag.
    closed = list(st.get("closed_windows") or [])
    closed.append({"lower": str(st.get("window_start") or ""), "upper": upper,
                   "count_at_close": len(acquired_ids),
                   "closed_at": datetime.now(timezone.utc).isoformat(), "rechecks": 0})
    st["closed_windows"] = closed[-int(getattr(config, "FANTASTIC_WATERMARK_AUDIT_KEEP", 12) or 12):]
    st.update({"last_successful_watermark": upper, "in_flight_window_end": "",
               "overlap_band_ids": acquired_ids, "boundary_ids": [],
               "run_epoch": int(st.get("run_epoch", 0) or 0) + 1,
               "acquired_last_window": len(acquired_ids), "window_acquired_ids": []})
    engine._save()
    return {"committed": True, "next_watermark": upper, "band_ids": len(acquired_ids)}


def _family_grants(families, global_cap, metrics):
    """Distribute the governor's run budget across title families.

    The GOVERNOR remains the sole budget authority: this only decides how ONE
    already-granted ``global_cap`` is split, and the returned grants always sum to at
    most that cap. With the allocator off (or without enough billed evidence) it
    returns the historical equal split, byte-for-byte.
    """
    equal = {t: max(1, global_cap // max(1, len(families))) for t in families}
    if not bool(getattr(config, "SEGMENT_ALLOCATOR_ENABLED", False)) or not families:
        return equal, "equal_split"
    try:
        from orchestrator.segment_allocator import (
            BROAD_SEGMENT, allocate, load_yield_table, refresh_yield_table,
            segments_from_table)

        # Rebuild the evidence from COMPLETED runs before allocating. Without this
        # the allocator has no producer and the flag would be decorative.
        refreshed = refresh_yield_table(
            str(getattr(config, "YIELD_LEDGER_PATH", "")),
            config.SEGMENT_ALLOCATOR_YIELD_TABLE_PATH)
        table = load_yield_table(config.SEGMENT_ALLOCATOR_YIELD_TABLE_PATH)
        metrics["segment_allocator_evidence_segments"] = refreshed
        segments = [s for s in segments_from_table(table, list(families))
                    if s.id in set(families)]
        alloc = allocate(
            int(global_cap), segments, enabled=True,
            min_evidence_credits=int(config.SEGMENT_ALLOCATOR_MIN_EVIDENCE_CREDITS),
            max_segment_share=float(config.SEGMENT_ALLOCATOR_MAX_SEGMENT_SHARE),
            exploration_floor=float(config.SEGMENT_ALLOCATOR_EXPLORATION_FLOOR))
        metrics["segment_allocator"] = alloc.to_dict()
        if alloc.mode != "weighted":
            return equal, alloc.mode
        broad = int(alloc.grants.get(BROAD_SEGMENT, 0))
        # The broad remainder is exploration: spread it evenly so no family starves.
        spread = broad // max(1, len(families))
        grants = {t: int(alloc.grants.get(t, 0)) + spread for t in families}
        # Families with no grant at all still get a floor so they can re-earn evidence.
        grants = {t: max(1, v) for t, v in grants.items()}
        total = sum(grants.values())
        if total > global_cap:  # HARD INVARIANT: never exceed the governor's grant
            scale = global_cap / float(total)
            grants = {t: max(1, int(v * scale)) for t, v in grants.items()}
        metrics["segment_allocator"]["applied_grants"] = dict(grants)
        return grants, "weighted"
    except Exception:  # noqa: BLE001 - allocation never breaks acquisition
        metrics["segment_allocator"] = {"mode": "error_fallback_broad"}
        return equal, "error_fallback_broad"


def _function_dedupe_context(covered_function_keys, metrics):
    """Open the slug crosswalk and decide whether THIS run suppresses covered orgs.

    Returns ``(crosswalk, covered_keys, suppress)``. Everything is best-effort: any
    failure yields ``(None, frozenset(), False)`` so acquisition proceeds exactly as
    it does with the feature off. Never raises.
    """
    if not bool(getattr(config, "FANTASTIC_FUNCTION_AWARE_UPSTREAM_DEDUPE_ENABLED", False)):
        return None, frozenset(), False
    try:
        from orchestrator.function_acquisition import SlugCrosswalk

        crosswalk = SlugCrosswalk(
            config.FANTASTIC_SLUG_CROSSWALK_PATH,
            ttl_days=int(config.FANTASTIC_SLUG_CROSSWALK_TTL_DAYS))
        if covered_function_keys is None:
            import airtable_client

            covered_function_keys = frozenset(airtable_client.snapshot_existing_identity())
        # Bounded exploration: every Nth run suppresses nothing, so a stale crosswalk
        # can never blind acquisition permanently.
        every = int(getattr(config, "FANTASTIC_FUNCTION_DEDUPE_EXPLORATION_EVERY_N_RUNS", 0) or 0)
        runs = int(crosswalk.state.get("runs", 0)) + 1
        crosswalk.state["runs"] = runs
        crosswalk._dirty = True
        suppress = not (every > 0 and runs % every == 0)
        metrics["function_dedupe"] = {
            "enabled": True, "run_index": runs, "suppressing": suppress,
            "crosswalk_entries": len(crosswalk.state.get("by_slug") or {}),
            "families_excluded": {}, "truncated_families": [], "provider_fallbacks": [],
        }
        return crosswalk, frozenset(covered_function_keys or ()), suppress
    except Exception:  # noqa: BLE001 - dedupe must never break acquisition
        metrics["function_dedupe"] = {"enabled": True, "error": "context_unavailable"}
        return None, frozenset(), False


def _family_exclusion_slugs(crosswalk, covered_keys, term, metrics):
    """Covered slugs to exclude for ONE title family (ACTIVE coverage only)."""
    try:
        from orchestrator.function_acquisition import (
            chunk_slugs, covered_slugs_for_family, family_for_role)

        family = family_for_role(str(term))
        slugs = covered_slugs_for_family(crosswalk, covered_keys, family)
        if not slugs:
            return []
        cap = int(getattr(config, "FANTASTIC_FUNCTION_DEDUPE_MAX_SLUGS_PER_FAMILY", 250))
        chunks = chunk_slugs(slugs, chunk_size=max(1, cap))
        first = chunks[0] if chunks else []
        block = metrics.setdefault("function_dedupe", {})
        block.setdefault("families_excluded", {})[str(term)] = len(first)
        if len(chunks) > 1:
            block.setdefault("truncated_families", []).append(str(term))
        return first
    except Exception:  # noqa: BLE001
        return []


def run_fantastic_jobs_acquisition(http_get: HttpGet = _http_get, *,
                                   covered_function_keys=None) -> SourceResult:
    """Entry point used by the orchestrator. Fail-open at the source level."""
    result = SourceResult(source="fantastic_jobs")
    if not config.FANTASTIC_JOBS_ENABLED:
        result.metadata = {"enabled": False, "skipped_reason": "disabled"}
        return result

    _cw = None
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
    # WATERMARK CIRCUIT BREAKER: any state/contract anomaly falls back to the tested
    # head/deep engine for THIS RUN. Run-local only -- never mutates configuration.
    watermark_engine = None
    if bool(getattr(config, "FANTASTIC_DATE_CREATED_WATERMARK_ENABLED", False)):
        try:
            eng = DateCreatedWatermarkEngine(
                result=result, quota=quota, http_get=http_get, seen_ids=seen_ids,
                metrics=metrics, run_cap=run_cap)
            eng.open()
            if not eng.opened or not eng.upper:
                raise ValueError("watermark window did not open")
            watermark_engine = eng
        except Exception as exc:  # noqa: BLE001
            metrics["watermark"] = {"enabled": True, "circuit_open": True,
                                    "fallback": "head_deep",
                                    "reason": f"{type(exc).__name__}"}
            logger.warning("watermark unhealthy (%s); falling back to head/deep for this run",
                           type(exc).__name__)
            watermark_engine = None
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
    # Filter set reused by the 0-credit watermark visibility-lag audit (count only).
    jb_audit_params: Dict[str, Any] = {}

    def _run_single_stream(endpoint: str, base_jb: Dict[str, Any], label: str,
                           cap_limit: int, accept: Optional[Tuple[str, ...]]) -> None:
        """Head (fresh-edge: page from the top, stop at the prior high_water) then
        Deep (strictly older than the deep cursor). Both passes share the base label
        (so a job's ``_acquisition_source`` is never mutated) and the global
        ``seen_ids`` (so the two passes never double-count). Either pass may be
        disabled by the acquire mode."""
        if do_head:
            room = min(cap_limit, run_cap - quota.jobs_consumed)
            if room > 0:
                got = _fetch_segment(  # NO date_posted_lt: page from the top
                    endpoint, dict(base_jb), label, room, quota, http_get, seen_ids,
                    metrics, accept_source=accept, stop_before_date=(head_high_water or None))
                stream_head_jobs.extend(got)
                result.jobs.extend(got)
                metrics["continuation"]["head_acquired"] = (
                    metrics["continuation"].get("head_acquired", 0) + len(got))
        if do_deep:
            room = min(cap_limit, run_cap - quota.jobs_consumed)
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

    allocator: Optional[_SourceBudgetAllocator] = None
    source_plan: List[_SourceSegment] = []
    # How to RESUME each segment if unspent budget is recycled to it. Only segments
    # paged by OFFSET can resume; the head/deep date-cursor stream and the
    # per-family loop own their own cursors, so they run once and are left out.
    seg_resume: Dict[str, Dict[str, Any]] = {}
    try:
        # Segment priority: ATS first, then Wellfound, Y Combinator, LinkedIn.
        # active-ats is the complementary, NON-overlapping first-party dataset
        # (active-jb is queried with exclude_ats_duplicate=true, so ATS x JB twins
        # cannot be billed twice by construction). It is gated by BOTH the explicit
        # source flag (Category 2, default OFF) and a non-zero ATS limit, so a code
        # deploy can never activate it; production keeps FANTASTIC_JOBS_ATS_LIMIT=0.
        source_plan = build_source_plan(title_advanced_active=title_advanced_active,
                                        used_title_families=used_title_families)
        # BOOTSTRAP RESERVE: withhold a deterministic slice of the run budget while
        # any first-enablement backfill is pending, so steady state cannot starve it
        # forever. Derived from the fair-share split (bootstrap = N extra claimants),
        # never an invented percentage. Steady state is allocated the remainder; both
        # still sum to the SAME run_cap.
        _boot_reserve = 0
        if watermark_engine is not None:
            watermark_engine.ensure_bootstraps(tuple(s.label for s in source_plan))
            _pending = [s for s in source_plan
                        if watermark_engine.bootstrap_pending(s.label) is not None]
            _shares = max(0, int(getattr(config, "FANTASTIC_BOOTSTRAP_RESERVE_SHARES", 1) or 0))
            if _pending and _shares > 0 and source_plan:
                _boot_reserve = (run_cap * _shares) // (len(source_plan) + _shares)
        metrics["bootstrap_reserve"] = _boot_reserve
        allocator = _SourceBudgetAllocator(
            max(0, run_cap - _boot_reserve), source_plan,
            policy=str(getattr(config, "FANTASTIC_SOURCE_ALLOCATION", "sequential") or "sequential"))
        metrics["source_plan"] = {
            "segments": [{"source": s.key, "label": s.label, "endpoint": s.endpoint,
                          "configured_limit": s.limit} for s in source_plan],
            "allocation_policy": allocator.policy,
            "linkedin_mode": ("title_advanced" if title_advanced_active
                              else "title_families" if used_title_families else "plan"),
        }
        _li_seg = next((s for s in source_plan if s.key == "linkedin"), None)
        _fam_before = 0
        ats_seg = next((s for s in source_plan if s.key == "ats"), None)
        ats_enabled = ats_seg is not None
        metrics["ats_source"] = {"enabled": ats_enabled, "limit": int(config.FANTASTIC_JOBS_ATS_LIMIT or 0)}
        if ats_seg is not None:
            ats_params = build_ats_params(title_advanced_expr)
            metrics["ats_source"]["params"] = sorted(k for k in ats_params if k != "title_advanced")
            seg_resume[ATS_SOURCE] = {"seg": ats_seg, "endpoint": "/v1/active-ats",
                                      "params": ats_params, "accept": None, "resumable": True}
            # ATS budget shares the SINGLE combined run cap with every other source:
            # the governor controls them together and no source can claim an
            # independent allowance.
            ats_room = allocator.grant(ats_seg, quota.jobs_consumed)
            if ats_room > 0:
                before = len(result.jobs)
                if watermark_engine is not None:
                    watermark_engine.run_stream("/v1/active-ats", ats_params, ATS_SOURCE, ats_room, None)
                else:
                    got = _fetch_segment("/v1/active-ats", ats_params, ATS_SOURCE, ats_room,
                                         quota, http_get, seen_ids, metrics)
                    # ATS rows must NOT feed the LinkedIn date_posted cursor (Gate-B):
                    # an ATS row with a newer date_posted would inflate high_water and
                    # make the next LinkedIn head pass skip genuinely new jobs.
                    result.jobs.extend(got)
                # ATS CIRCUIT BREAKER (run-local): if the ATS segment errored or its
                # rows were overwhelmingly unparseable, record it and continue with
                # JB. One malformed source can never take down the baseline path.
                seg = (metrics["segments"].get(ATS_SOURCE) or {})
                returned = int(seg.get("returned", 0) or 0)
                rejected = int(seg.get("schema_rejected", 0) or 0)
                bad_rate = (rejected / returned) if returned else 0.0
                thresh = float(getattr(config, "FANTASTIC_ATS_MAX_SCHEMA_REJECT_RATE", 0.5) or 0.5)
                tripped = bool(seg.get("error_code")) or (returned >= 20 and bad_rate > thresh)
                metrics["ats_source"].update({
                    "acquired": len(result.jobs) - before, "returned": returned,
                    "schema_rejected": rejected, "schema_reject_rate": round(bad_rate, 4),
                    "circuit_open": tripped})
                if tripped:
                    logger.warning("ATS circuit breaker tripped (err=%s reject_rate=%.2f); "
                                   "continuing with active-jb only",
                                   seg.get("error_code") or "-", bad_rate)
                allocator.settle(ats_seg, returned)
            else:
                allocator.settle(ats_seg, 0)

        if title_advanced_active:
            # ONE Boolean OR-expression over the whole role catalog (benchmark
            # parity). The union is returned counting each job ONCE -> zero
            # cross-query billing overlap, 118/118 coverage, ~all target-role.
            # It is a single LinkedIn stream, so it reuses the single-stream
            # date_posted cursor (head fresh-edge + deep backfill).
            metrics["title_advanced"] = {"expression_chars": len(title_advanced_expr),
                                         "fingerprint": title_plan.get("fingerprint", ""),
                                         "clauses": len(title_plan.get("clauses") or [])}
            # FUNCTION-AWARE UPSTREAM SUPPRESSION IS NOT APPLICABLE HERE, and that is
            # a provider constraint rather than a wiring gap.
            #
            # `exclude_organization_slug` applies to the WHOLE request. The single
            # title_advanced stream asks one question covering every role family at
            # once, so excluding a company covered for GTM would also remove its
            # Engineering demand from the same response. There is no way to scope the
            # exclusion by function inside one combined query, and suppressing a whole
            # company because one of its functions is covered is precisely what the
            # rule forbids.
            #
            # Per-function scoping needs the per-family partition, which is a
            # different billing shape: N requests, and a job matched by two families
            # billed twice. That trade is deliberately not made by default.
            #
            # So the earliest SAFE suppression on this path is downstream:
            # PRE_APOLLO_EXISTING_DEDUPE (enabled), which runs after Fantastic has
            # already billed the row. It saves Apollo credits, not acquisition
            # credits, and the rows for an already-covered company x function are an
            # unavoidable acquisition cost of querying all families together.
            metrics["function_dedupe"] = {
                "enabled": False,
                "applicable": False,
                "reason": ("exclude_organization_slug scopes to the whole request; the "
                           "single title_advanced stream covers every role family, so "
                           "a per-function exclusion cannot be expressed without "
                           "partitioning into per-family queries"),
                "earliest_safe_suppression": "PRE_APOLLO_EXISTING_DEDUPE (post-billing)",
            }
            jb_params = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                         "exclude_ats_duplicate": "true", "source": "linkedin",
                         "title_advanced": title_advanced_expr}
            jb_params.update(_jb_filter_params())
            _apply_server_industry_exclusions(jb_params)
            jb_audit_params = dict(jb_params)
            # LinkedIn draws an ALLOCATOR GRANT like every other source: without this
            # the title_advanced stream would consume the whole remaining run budget
            # and starve Wellfound/YC under fair_share (they would be reachable but
            # never funded). Under "sequential" the grant equals the remaining budget,
            # so production behaviour is unchanged.
            li_seg = next((s for s in source_plan if s.key == "linkedin"), None)
            if li_seg is not None:
                # Resumable ONLY under the watermark engine: that path pages the window
                # by offset. Without it the stream is driven by head/deep date cursors,
                # which a second pass would replay rather than continue.
                seg_resume[li_seg.label] = {
                    "seg": li_seg, "endpoint": "/v1/active-jb", "params": jb_params,
                    "accept": ("linkedin",), "resumable": watermark_engine is not None}
            li_room = (allocator.grant(li_seg, quota.jobs_consumed) if li_seg is not None
                       else int(config.FANTASTIC_JOBS_LINKEDIN_LIMIT))
            li_before = quota.jobs_consumed
            if li_room > 0:
                if watermark_engine is not None:
                    watermark_engine.run_stream("/v1/active-jb", jb_params,
                                                "fantastic_jobs_linkedin", li_room, ("linkedin",))
                else:
                    _run_single_stream("/v1/active-jb", jb_params, "fantastic_jobs_linkedin",
                                       li_room, ("linkedin",))
            if li_seg is not None:
                allocator.settle(li_seg, quota.jobs_consumed - li_before)
        elif used_title_families and config.FANTASTIC_JOBS_LINKEDIN_LIMIT > 0:
            # LinkedIn-only, but targeted per role FAMILY so ~all billed jobs are
            # on-portfolio (the broad feed is ~4% target-role). One global cap is
            # shared FAIRLY across families (deterministic order, per-family share)
            # so one high-volume family cannot exhaust the run. seen_ids is GLOBAL
            # -> a job returned by two families is billed twice but processed once;
            # its cross-family duplicate is counted for billing observability. Each
            # family keeps its OWN date_posted cursor so streams never leak.
            _cw, _covered_keys, _suppress = _function_dedupe_context(
                covered_function_keys, metrics)
            fam_states = (cont_state.get("families") or {}) if continuation_enabled else {}
            families = [str(t).strip() for t in (config.FANTASTIC_JOBS_TITLE_FAMILIES or []) if str(t).strip()]
            # Families share the LinkedIn segment's ALLOCATOR GRANT, not the whole
            # run budget, so per-family targeting cannot starve other sources either.
            global_cap = (allocator.grant(_li_seg, quota.jobs_consumed)
                          if _li_seg is not None else int(run_cap))
            _fam_before = quota.jobs_consumed
            _grants, _alloc_mode = _family_grants(families, global_cap, metrics)
            metrics["title_families_planned"] = len(families)
            metrics["title_family_allocation_mode"] = _alloc_mode
            for term in families:
                if quota.stop_reason or (quota.jobs_consumed - _fam_before) >= global_cap:
                    break
                fid = _family_id(term)
                cap = min(int(_grants.get(term, 1)),
                          global_cap - (quota.jobs_consumed - _fam_before))
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
                _excluded = (_family_exclusion_slugs(_cw, _covered_keys, term, metrics)
                             if (_cw is not None and _suppress) else [])
                if _excluded:
                    # style=form, explode=false -> ONE comma-joined value. Repeated
                    # params are silently ignored past the first by this provider.
                    jb_params["exclude_organization_slug"] = ",".join(_excluded)
                fam_jobs = _fetch_segment(
                    "/v1/active-jb", jb_params, f"fantastic_jobs_linkedin::{fid}",
                    cap, quota, http_get, seen_ids, metrics, accept_source=("linkedin",))
                if _excluded and not fam_jobs and metrics["segments"].get(
                        f"fantastic_jobs_linkedin::{fid}", {}).get("error_code"):
                    # The provider rejected/ignored the exclusion. Losing a family's
                    # acquisition is worse than acquiring a covered company, so retry
                    # once WITHOUT it rather than accepting a silent zero-row family.
                    metrics.setdefault("function_dedupe", {}).setdefault(
                        "provider_fallbacks", []).append(str(term))
                    jb_params.pop("exclude_organization_slug", None)
                    fam_jobs = _fetch_segment(
                        "/v1/active-jb", jb_params, f"fantastic_jobs_linkedin::{fid}",
                        cap, quota, http_get, seen_ids, metrics,
                        accept_source=("linkedin",))
                for job in fam_jobs:
                    job["_fantastic_title_family"] = fid
                    job["_fantastic_title_term"] = term
                result.jobs.extend(fam_jobs)
                if continuation_enabled:
                    stats = _cursor_stats(fam_jobs, prior_high=str(fam.get("high_water") or ""))
                    if stats:
                        new_family_states[fid] = {"term": term, **stats}
        # INDEPENDENT SOURCE SEGMENTS (source union, not mutually-exclusive modes).
        # Previously Wellfound/YC lived in the final `else` of this chain and were
        # unreachable whenever title_advanced was active -- i.e. always, in
        # production. They now execute from the plan in EVERY LinkedIn mode, each
        # bounded by the SAME governor run budget via the allocator.
        if used_title_families and _li_seg is not None:
            allocator.settle(_li_seg, quota.jobs_consumed - _fam_before)

        for seg in source_plan:
            if seg.key == "ats":
                continue  # dispatched above (different endpoint + circuit breaker)
            if seg.dispatch not in ("plan", "functional"):
                continue  # LinkedIn under a title MODE: dispatched + settled above
            if quota.stop_reason:
                metrics["segments"].setdefault(seg.label, {}).setdefault(
                    "stop_reason", quota.stop_reason)
                allocator.settle(seg, 0)
                continue
            room = allocator.grant(seg, quota.jobs_consumed)
            if room <= 0:
                metrics["segments"].setdefault(seg.label, {}).setdefault(
                    "stop_reason", "no_run_budget")
                allocator.settle(seg, 0)
                continue
            # SOURCE-AWARE: omits the null-excluding firmographic predicates for a
            # source that carries no provider firmographics, and applies role
            # targeting -- which this loop never used to send at all.
            if seg.dispatch == "functional":
                # Description expression, NO title_advanced. The two are ANDed by the
                # provider, so a title filter here would confine this segment to the
                # jobs the main query already reaches -- which is the one thing it
                # exists not to do.
                #
                # AND THAT CREATES AN INTEGRATION PROBLEM THIS SEGMENT MUST SOLVE.
                # RoleGate verifies a title against a TARGET role (`_matched_role`),
                # and the title-matched path sets that from the family that matched.
                # A functional result has no matched family by construction, so
                # without a classification step every row it returns arrives with no
                # target and is UNVERIFIED regardless of what it is -- which would
                # look like "functional discovery finds nothing relevant" when it is
                # really "we never told the gate what to check against".
                #
                # `_classify` is the existing, supported answer: the same step the
                # external-batch path runs so RoleGate has a target to verify. It
                # assigns the best CATALOG role and its relevance assessment. It
                # infers nothing from task keywords, and a title the catalog does not
                # cover still ends with no role -- reviewable, never approved. The
                # uncertainty is preserved; only the missing input is supplied.
                jb_params = build_functional_params()
                if not jb_params:
                    metrics["segments"].setdefault(seg.label, {}).setdefault(
                        "stop_reason", "no_functional_expression")
                    allocator.settle(seg, 0)
                    continue
            else:
                jb_params = build_jb_params(seg.key, title_advanced_expr=title_advanced_expr)
            seg_resume[seg.label] = {"seg": seg, "endpoint": seg.endpoint,
                                     "params": jb_params, "accept": seg.accept,
                                     "resumable": not seg.feeds_cursor}
            before_billed = quota.jobs_consumed
            if seg.dispatch == "functional":
                _before_fn = len(result.jobs)
                if watermark_engine is not None:
                    watermark_engine.run_stream(seg.endpoint, jb_params, seg.label,
                                                room, seg.accept)
                else:
                    result.jobs.extend(_fetch_segment(
                        seg.endpoint, jb_params, seg.label, room, quota, http_get,
                        seen_ids, metrics, accept_source=seg.accept))
                _classify_functional_rows(result.jobs[_before_fn:], metrics)
            elif seg.feeds_cursor:
                # Plain LinkedIn stream (no title mode): keeps the single-stream
                # head/deep date_posted cursor exactly as before.
                _run_single_stream(seg.endpoint, jb_params, seg.label, room, seg.accept)
            elif watermark_engine is not None:
                watermark_engine.run_stream(seg.endpoint, jb_params, seg.label, room, seg.accept)
            else:
                got = _fetch_segment(seg.endpoint, jb_params, seg.label, room, quota,
                                     http_get, seen_ids, metrics, accept_source=seg.accept)
                result.jobs.extend(got)
            allocator.settle(seg, quota.jobs_consumed - before_billed)
        # ---- BACKWARD RECLAMATION -------------------------------------------------
        # Round 1 above gave every enabled source its equal floor. A sparse source
        # that could not consume its floor releases it only AFTER the sources able to
        # spend it have already run, so with two empty sources HALF the run budget was
        # stranded (measured live: 1582 of 3164). Re-offer the unspent remainder to the
        # segments that still have inventory, so source ORDER can never decide whether
        # the run budget gets used.
        #
        # ONLY a segment that stopped on "cap_reached" is asked again: that is the one
        # stop reason meaning "I filled my grant and there may be more". empty_page /
        # short_page / no_new_ids mean exhausted, and an error_code means the segment
        # failed -- neither is re-dispatched, so a dead source is never re-polled.
        # Each resumed pass continues at the offset the segment already reached, so no
        # page is billed twice. sequential is left byte-identical: it has nothing to
        # reclaim (the first segment may already draw the whole remaining budget).
        if (allocator is not None and allocator.policy == "fair_share" and seg_resume
                and not quota.stop_reason):
            def _resumable_now() -> List[Dict[str, Any]]:
                out = []
                for lbl, rec in seg_resume.items():
                    if not rec.get("resumable"):
                        continue
                    s = (metrics["segments"].get(lbl) or {})
                    if s.get("error_code"):
                        continue
                    if str(s.get("stop_reason") or "") != "cap_reached":
                        continue
                    out.append(rec)
                return out

            _max_rounds = int(getattr(config, "FANTASTIC_SOURCE_MAX_ALLOCATION_ROUNDS", 4) or 0)
            for _ in range(max(0, _max_rounds - 1)):
                _active = _resumable_now()
                if not _active or quota.stop_reason:
                    break
                if allocator.budget - quota.jobs_consumed <= 0:
                    break
                allocator.open_round([r["seg"] for r in _active])
                _round_billed = 0
                for _rec in _active:
                    if quota.stop_reason:
                        break
                    _rseg = _rec["seg"]
                    _room = allocator.grant(_rseg, quota.jobs_consumed)
                    if _room <= 0:
                        continue
                    _sm = metrics["segments"].setdefault(_rseg.label, {})
                    # Rows already billed for this label ARE the offset to resume at:
                    # paging is contiguous, so the next unseen row is exactly there.
                    _start = int(_sm.get("returned", 0) or 0)
                    # The LAST pass decides how the segment finished; otherwise a
                    # round-1 "cap_reached" would mask a later genuine drain and the
                    # canonical watermark could never advance.
                    _sm["stop_reason"] = ""
                    _b4 = quota.jobs_consumed
                    if watermark_engine is not None:
                        watermark_engine.run_stream(_rec["endpoint"], _rec["params"],
                                                    _rseg.label, _room, _rec["accept"],
                                                    start_offset=_start)
                    else:
                        _got = _fetch_segment(_rec["endpoint"], _rec["params"], _rseg.label,
                                              _room, quota, http_get, seen_ids, metrics,
                                              accept_source=_rec["accept"],
                                              start_offset=_start)
                        result.jobs.extend(_got)
                    _delta = quota.jobs_consumed - _b4
                    allocator.settle(_rseg, _delta)
                    _round_billed += _delta
                if _round_billed <= 0:
                    break            # nothing left to win: stop rather than re-poll

        # (moved here from the checkpoint section: it BILLS, so it must run
        #  before metrics/quota aggregation or the run would under-report spend)
        if watermark_engine is not None:
            # BOUNDED HISTORICAL BACKFILL for newly enabled sources, using whatever run
            # budget the steady-state segments did not spend. Runs AFTER steady state so
            # a backfill can never delay current-window coverage, and it is accounted in
            # the same allocator/quota so it cannot exceed the one run budget.
            # The reserve is a GLOBAL bootstrap pool, then split across EVERY pending
            # bootstrap with the same fair-share allocator the sources use. Handing the
            # pool out first-come would starve the second newcomer exactly the way
            # sequential source allocation starves the second source: enabling Wellfound
            # and Y Combinator together, the first in plan order would take the whole
            # reserve every run and the other would never progress.
            _pending_segs = [s for s in source_plan
                             if watermark_engine.bootstrap_pending(s.label) is not None]
            if _pending_segs and _boot_reserve > 0:
                _boot_alloc = _SourceBudgetAllocator(_boot_reserve, _pending_segs, "fair_share")
                _boot_start = quota.jobs_consumed
                metrics["bootstrap_allocation"] = {"reserve": _boot_reserve,
                                                   "pending": [s.label for s in _pending_segs]}
                for _seg in _pending_segs:
                    if quota.stop_reason:
                        break
                    _boot_room = _boot_alloc.grant(_seg, quota.jobs_consumed - _boot_start)
                    # Never exceed the ONE run budget either.
                    _boot_room = min(_boot_room, max(0, run_cap - quota.jobs_consumed))
                    if _boot_room <= 0:
                        _boot_alloc.settle(_seg, 0)
                        continue
                    if _seg.key == "ats":
                        _bp = build_ats_params(title_advanced_expr)
                    else:
                        # Same source-aware shape as steady state, so a backfill and
                        # the live window ask the provider the SAME question.
                        _bp = build_jb_params(_seg.key,
                                              title_advanced_expr=title_advanced_expr)
                    _b4 = quota.jobs_consumed
                    watermark_engine.run_bootstrap(_seg.endpoint, _bp, _seg.label,
                                                   _boot_room, _seg.accept)
                    _boot_alloc.settle(_seg, quota.jobs_consumed - _b4)
                metrics["bootstrap_allocation"].update(_boot_alloc.to_dict())

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

    # Learn slug -> domain/company/function from THIS run's rows, then persist so the
    # crosswalk survives restarts. Best-effort: never fails the run.
    try:
        if _cw is not None:
            learned = _cw.observe_jobs(result.jobs)
            _cw.save()
            metrics.setdefault("function_dedupe", {})["slugs_learned"] = learned
    except Exception:  # noqa: BLE001
        pass

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
    metrics["cross_source_duplicates"] = sum(
        int(s.get("cross_source_duplicates", 0)) for s in metrics["segments"].values())
    # Per-source acquisition attribution (raw/unique/duplicates/billed per source)
    # so incremental yield PER PROVIDER CREDIT is computable per source.
    metrics.setdefault("source_attribution", {})["per_source"] = {
        label: {"returned_billed": int(s.get("returned", 0) or 0),
                "unique_kept": int(s.get("schema_valid", 0) or 0),
                "duplicates": int(s.get("duplicates", 0) or 0),
                "cross_source_duplicates": int(s.get("cross_source_duplicates", 0) or 0),
                "schema_rejected": int(s.get("schema_rejected", 0) or 0),
                "source_filtered_out": int(s.get("source_filtered_out", 0) or 0),
                "requests": int(s.get("requests_succeeded", 0) or 0),
                "stop_reason": s.get("stop_reason", ""),
                "error_code": s.get("error_code", "")}
        for label, s in metrics["segments"].items()}
    metrics.pop("_first_seen", None)      # transient id map: never persisted
    metrics.pop("_candidate_keys", None)  # transient candidate-key map: never persisted
    if allocator is not None:
        metrics["source_allocation"] = allocator.to_dict()
    # ONE reconciliation object for the whole run. Steady state and bootstrap draw
    # from DIFFERENT pools, so the run-level invariant is checked against the true
    # run_cap -- never against a single allocator's (smaller) budget.
    _steady_billed = sum(int(v.get("returned", 0) or 0)
                         for k, v in metrics["segments"].items() if "::bootstrap" not in k)
    _boot_billed = sum(int(v.get("returned", 0) or 0)
                       for k, v in metrics["segments"].items() if "::bootstrap" in k)
    _reserve = int(metrics.get("bootstrap_reserve", 0) or 0)
    metrics["run_budget_accounting"] = {
        "run_cap": run_cap, "steady_budget": max(0, run_cap - _reserve),
        "bootstrap_reserve": _reserve,
        "steady_billed": _steady_billed, "bootstrap_billed": _boot_billed,
        "total_billed": quota.jobs_consumed,
        "unused_run_budget": max(0, run_cap - quota.jobs_consumed),
        "segments_reconcile": (_steady_billed + _boot_billed) == quota.jobs_consumed,
        "within_run_cap": quota.jobs_consumed <= run_cap,
    }
    if not metrics["run_budget_accounting"]["within_run_cap"]:
        logger.error("run budget overspend: billed=%s run_cap=%s",
                     quota.jobs_consumed, run_cap)
    if not metrics["run_budget_accounting"]["segments_reconcile"]:
        logger.error("billing does not reconcile: steady=%s bootstrap=%s quota=%s",
                     _steady_billed, _boot_billed, quota.jobs_consumed)
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
        # The canonical window is drained only when EVERY source enabled for THIS
        # run drained it -- computed from the plan, so disabling a source releases
        # the window and enabling one re-opens it. Bootstrap state is deliberately
        # NOT consulted: a backfill must never advance or hold the canonical window.
        watermark_engine.checkpoint(tuple(s.label for s in source_plan))
        # Visibility-lag self-audit: re-count ONE previously closed window with the
        # count endpoint (0 Jobs credits) to measure real late-visibility.
        if bool(getattr(config, "FANTASTIC_WATERMARK_AUDIT_ENABLED", True)):
            audit_base = dict(jb_audit_params or {})
            if audit_base:
                audit_closed_windows(http_get, audit_base, metrics)

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
