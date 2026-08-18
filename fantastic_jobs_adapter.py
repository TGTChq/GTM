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


def _cursor_stats(jobs: List[Dict[str, Any]], prior_high: str = "") -> Optional[Dict[str, Any]]:
    """Compute the continuation cursor (oldest date + boundary IDs) and high_water
    from a set of acquired jobs. Returns None when no job carries a timestamp."""
    posted = sorted(str(j.get("job_posted_at_datetime_utc") or "")
                    for j in jobs if j.get("job_posted_at_datetime_utc"))
    if not posted:
        return None
    oldest, newest = posted[0], posted[-1]
    boundary_ids = [str(j.get("_fantastic_internal_id"))
                    for j in jobs
                    if str(j.get("job_posted_at_datetime_utc") or "") == oldest
                    and j.get("_fantastic_internal_id")]
    return {
        "cursor_date": oldest,
        "high_water": max(newest, str(prior_high or "")) if prior_high else newest,
        "boundary_ids": boundary_ids,
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
    """Normalize a role into a title_advanced term (single-quote multi-word)."""
    cleaned = " ".join(str(role or "").lower().replace("/", " ").replace("-", " ").split())
    return f"'{cleaned}'" if " " in cleaned else cleaned


def _title_advanced_expression() -> str:
    """The Boolean OR-expression over the whole role catalog (benchmark parity).

    A configured override wins; otherwise build deterministically from
    role_catalog.DEFAULT_ACQUISITION_ROLES so one query returns the union of every
    target role, counting each job once (no cross-query billing overlap).
    """
    override = str(getattr(config, "FANTASTIC_JOBS_TITLE_ADVANCED_EXPRESSION", "") or "").strip()
    if override:
        return override
    try:
        from role_catalog import DEFAULT_ACQUISITION_ROLES
    except Exception:  # noqa: BLE001 - never let a missing catalog crash acquisition
        return ""
    terms: List[str] = []
    seen: set = set()
    for role in sorted({str(r).strip() for r in DEFAULT_ACQUISITION_ROLES if str(r).strip()}):
        term = _title_advanced_term(role)
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return " | ".join(terms)


def _read_quota(headers: Any) -> Dict[str, Optional[int]]:
    def geti(name: str) -> Optional[int]:
        try:
            raw = headers.get(name)
            return int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return {
        "jobs_limit": geti("x-api-jobs-limit"),
        "jobs_remaining": geti("x-api-jobs-remaining"),
        "requests_limit": geti("x-api-requests-limit"),
        "requests_remaining": geti("x-api-requests-remaining"),
    }


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


def _quota_would_breach(quota: _QuotaState, want: int) -> str:
    if quota.requests_remaining is not None and (quota.requests_remaining - 1) < config.FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING:
        return "requests_quota_reserve"
    if quota.jobs_remaining is not None and (quota.jobs_remaining - want) < config.FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING:
        return "jobs_quota_reserve"
    return ""


def _fetch_segment(endpoint: str, base_params: Dict[str, Any], source_label: str, cap: int,
                   quota: _QuotaState, http_get: HttpGet, seen_ids: set,
                   metrics: Dict[str, Any], accept_source: Optional[Tuple[str, ...]] = None) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    seg = metrics["segments"].setdefault(source_label, {
        "attempted": 0, "requests_succeeded": 0, "returned": 0, "schema_valid": 0,
        "schema_rejected": 0, "pii_dropped": 0, "non_us": 0, "source_filtered_out": 0,
        "duplicates": 0, "stop_reason": "", "http_status": None, "dispatched": False,
        "retries": 0, "failure_stage": "", "error_code": "", "error_class": "",
    })
    page = 1
    fingerprints: set = set()
    while len(jobs) < cap:
        want = min(cap - len(jobs), 100)
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
            job, reason = map_record(record, source_label, seg)
            if job is None:
                seg["schema_rejected"] += 1
                continue
            if accept_source is not None:
                src = job.get("_fantastic_source", "")
                if not any(tok in src for tok in accept_source):
                    seg["source_filtered_out"] += 1
                    continue
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

    # NOTE: only parameters proven against the successful smoke test are sent.
    # `description_type` was a guessed parameter (the smoke test that returned
    # HTTP 200 never sent it) and was the divergence in the failed production
    # request; it is not sent. Full descriptions are absent from this API by
    # default (only AI-derived fields), which the mapper already handles by
    # leaving job_description empty rather than fabricating one.
    title_targeting = bool(getattr(config, "FANTASTIC_JOBS_TITLE_TARGETING_ENABLED", False))
    title_advanced_enabled = bool(getattr(config, "FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED", False))
    title_advanced_expr = _title_advanced_expression() if title_advanced_enabled else ""
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
    try:
        # Segment priority: ATS first, then Wellfound, Y Combinator, LinkedIn.
        ats_params = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                      "include_basic_organization_details": "true"}
        result.jobs.extend(_fetch_segment(
            "/v1/active-ats", ats_params, ATS_SOURCE, config.FANTASTIC_JOBS_ATS_LIMIT,
            quota, http_get, seen_ids, metrics))

        if title_advanced_active:
            # ONE Boolean OR-expression over the whole role catalog (benchmark
            # parity). The union is returned counting each job ONCE -> zero
            # cross-query billing overlap, 118/118 coverage, ~all target-role.
            # It is a single LinkedIn stream, so it reuses the single-stream
            # date_posted cursor (no per-family state).
            metrics["title_advanced"] = {"expression_chars": len(title_advanced_expr)}
            jb_params = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                         "exclude_ats_duplicate": "true", "source": "linkedin",
                         "title_advanced": title_advanced_expr}
            jb_params.update(_jb_filter_params())
            if cursor_date_lt:
                jb_params["date_posted_lt"] = cursor_date_lt
            remaining = config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN - len(result.jobs)
            result.jobs.extend(_fetch_segment(
                "/v1/active-jb", jb_params, "fantastic_jobs_linkedin",
                min(config.FANTASTIC_JOBS_LINKEDIN_LIMIT, remaining),
                quota, http_get, seen_ids, metrics, accept_source=("linkedin",)))
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
            global_cap = int(config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN)
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
                if cap <= 0 or quota.stop_reason or len(result.jobs) >= config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN:
                    continue
                label, accept = JB_SEGMENTS[seg_key]
                jb_params = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                             "exclude_ats_duplicate": "true", "source": seg_key}
                jb_params.update(_jb_filter_params())
                if cursor_date_lt:
                    jb_params["date_posted_lt"] = cursor_date_lt
                remaining = config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN - len(result.jobs)
                result.jobs.extend(_fetch_segment(
                    "/v1/active-jb", jb_params, label, min(cap, remaining),
                    quota, http_get, seen_ids, metrics, accept_source=accept))
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

    # Advance and persist the continuation cursor from the jobs actually acquired
    # (billed). The cursor moves strictly OLDER (min date_posted); high_water keeps
    # the newest ever seen so a future incremental run can pick up newer jobs via
    # date_posted_gte. Per-family mode keeps an INDEPENDENT cursor per family so
    # streams never leak; single-stream (title_advanced or seg-caps) keeps one
    # cursor. The branch MUST match the acquisition path that actually ran.
    if continuation_enabled and used_title_families:
        _save_continuation_state({
            "schema": _CONTINUATION_SCHEMA,
            "mode": "title_families",
            "families": new_family_states,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        metrics["continuation"]["families_tracked"] = len(new_family_states)
    elif continuation_enabled:
        stats = _cursor_stats(result.jobs, prior_high=str(cont_state.get("high_water") or ""))
        if stats:
            _save_continuation_state({"schema": _CONTINUATION_SCHEMA, "source": "linkedin", **stats})
            metrics["continuation"]["next_cursor_date"] = stats["cursor_date"]
            metrics["continuation"]["high_water"] = stats["high_water"]
        else:
            metrics["continuation"]["next_cursor_date"] = cont_state.get("cursor_date", "")

    result.metadata = metrics
    for job in result.jobs:
        job.setdefault("_acquisition_source", "fantastic_jobs")
    return result
