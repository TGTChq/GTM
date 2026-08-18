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

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

import config
from free_job_sources import SourceResult

logger = logging.getLogger(__name__)

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
    employment = str(getattr(config, "FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE", "") or "").strip()
    if employment:
        params["ai_employment_type"] = employment
    if getattr(config, "FANTASTIC_JOBS_EXCLUDE_AGENCY", True):
        params["organization_agency"] = "exclude"
    return params


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
        if page > 50:
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
    # NOTE: only parameters proven against the successful smoke test are sent.
    # `description_type` was a guessed parameter (the smoke test that returned
    # HTTP 200 never sent it) and was the divergence in the failed production
    # request; it is not sent. Full descriptions are absent from this API by
    # default (only AI-derived fields), which the mapper already handles by
    # leaving job_description empty rather than fabricating one.
    try:
        # Segment priority: ATS first, then Wellfound, Y Combinator, LinkedIn.
        ats_params = {"time_frame": config.FANTASTIC_JOBS_TIME_FRAME,
                      "include_basic_organization_details": "true"}
        result.jobs.extend(_fetch_segment(
            "/v1/active-ats", ats_params, ATS_SOURCE, config.FANTASTIC_JOBS_ATS_LIMIT,
            quota, http_get, seen_ids, metrics))
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
    result.metadata = metrics
    for job in result.jobs:
        job.setdefault("_acquisition_source", "fantastic_jobs")
    return result
