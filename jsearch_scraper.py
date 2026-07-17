"""Step 1: pull fresh job postings from JSearch and choose the best role match."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import config
from http_utils import QuotaExhaustedError, request_with_retry, safe_json
from pipeline_state import SeenJobsRegistry
from role_catalog import canonical_role_for_search, role_specificity
from role_relevance import assess_role, normalize_relevance_score

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JSearchFetchResult:
    """One JSearch query plus non-sensitive quota/runtime metadata."""

    jobs: List[Dict]
    duration_seconds: float
    quota: Dict[str, object] = field(default_factory=dict)


@dataclass
class ScrapeResult:
    output_path: str
    total_jobs: int
    stats: Dict
    failed_roles: List[str] = field(default_factory=list)
    roles_with_results: int = 0
    success: bool = True
    errors: List[str] = field(default_factory=list)


_TITLE_EXCLUSION_PATTERNS = [
    re.compile(r"\b" + re.escape(keyword) + r"\b", re.I)
    for keyword in config.EXCLUDED_TITLE_KEYWORDS
]


def is_excluded_title(title: str) -> bool:
    return any(pattern.search(title or "") for pattern in _TITLE_EXCLUSION_PATTERNS)


def _header_value(headers, name: str) -> str:
    value = headers.get(name) if headers is not None else None
    return str(value).strip() if value not in (None, "") else ""


def _parse_optional_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _quota_snapshot(headers) -> Dict[str, object]:
    """Capture RapidAPI quota headers without exposing credentials or payloads."""
    limit_raw = _header_value(headers, "x-ratelimit-requests-limit")
    remaining_raw = _header_value(headers, "x-ratelimit-requests-remaining")
    reset_raw = _header_value(headers, "x-ratelimit-requests-reset")
    used_raw = _header_value(headers, "x-ratelimit-requests-used")
    return {
        "limit": _parse_optional_int(limit_raw),
        "remaining": _parse_optional_int(remaining_raw),
        "reset": _parse_optional_int(reset_raw) if reset_raw else None,
        "used": _parse_optional_int(used_raw),
    }


def fetch_jobs_for_role(role: str) -> JSearchFetchResult:
    headers = {
        "x-rapidapi-key": config.RAPIDAPI_KEY,
        "x-rapidapi-host": config.JSEARCH_HOST,
        "Content-Type": "application/json",
    }
    params = {
        "query": f"{role} in United States",
        "page": "1",
        "num_pages": str(config.NUM_PAGES),
        "date_posted": config.DATE_POSTED,
        "country": config.COUNTRY,
    }
    started = time.perf_counter()
    response = request_with_retry(
        "GET",
        config.JSEARCH_ENDPOINT,
        headers=headers,
        params=params,
        timeout=45,
    )
    duration_seconds = round(time.perf_counter() - started, 3)
    data = safe_json(response)
    if data.get("status") not in (None, "OK"):
        raise ValueError(f"JSearch returned status={data.get('status')!r} for {role}")

    payload = data.get("data", [])
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        jobs = []
    if not isinstance(jobs, list):
        raise ValueError(f"Unexpected JSearch jobs payload for {role}: {type(jobs).__name__}")
    return JSearchFetchResult(
        jobs=jobs,
        duration_seconds=duration_seconds,
        quota=_quota_snapshot(response.headers),
    )


def validate_preflight() -> None:
    if not config.RAPIDAPI_KEY:
        raise ValueError("RAPIDAPI_KEY is missing from .env")
    if not config.ROLES:
        raise ValueError("ROLES_JSON/config.ROLES is empty")
    if config.JSEARCH_MAX_QUERIES_PER_RUN < 0:
        raise ValueError("JSEARCH_MAX_QUERIES_PER_RUN cannot be negative")
    if config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN < 0:
        raise ValueError("JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN cannot be negative")
    if config.NUM_PAGES < 1:
        raise ValueError("NUM_PAGES must be at least 1")
    if config.JSEARCH_MIN_REMAINING_REQUESTS < 0:
        raise ValueError("JSEARCH_MIN_REMAINING_REQUESTS cannot be negative")
    if not 0 <= config.MAX_ROLE_FAILURE_RATE <= 1:
        raise ValueError("MAX_ROLE_FAILURE_RATE must be between 0 and 1")



def estimate_query_units(query_count: int, num_pages: Optional[int] = None) -> int:
    """Estimate RapidAPI request units before the first network call."""
    pages = config.NUM_PAGES if num_pages is None else num_pages
    return max(0, int(query_count)) * max(1, int(pages))


def validate_query_budget(query_count: int) -> int:
    estimated_units = estimate_query_units(query_count)
    budget = config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN
    if budget > 0 and estimated_units > budget:
        raise ValueError(
            "Estimated JSearch usage "
            f"({query_count} queries x {config.NUM_PAGES} pages = {estimated_units} units) "
            f"exceeds JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN={budget}. "
            "Set NUM_PAGES=1 for the daily 118-role catalog, reduce the query cap, "
            "or explicitly raise the budget for a supervised diagnostic."
        )
    return estimated_units

def _candidate_key(job: Dict) -> str:
    return str(job.get("job_id") or "").strip()


def _allowed_role_failures(attempted_queries: int) -> int:
    rate_allowance = math.ceil(attempted_queries * config.MAX_ROLE_FAILURE_RATE)
    return max(config.MAX_ROLE_FAILURES, rate_allowance)


def _coerce_fetch_result(value) -> JSearchFetchResult:
    """Keep compatibility with tests/custom integrations that return only a list."""
    if isinstance(value, JSearchFetchResult):
        return value
    if isinstance(value, list):
        return JSearchFetchResult(jobs=value, duration_seconds=0.0, quota={})
    raise TypeError(
        "fetch_jobs_for_role must return JSearchFetchResult or list, got "
        f"{type(value).__name__}"
    )


def run_daily_scrape(
    registry: Optional[SeenJobsRegistry] = None,
    *,
    search_roles: Optional[List[str]] = None,
    max_queries: Optional[int] = None,
) -> ScrapeResult:
    """Query target roles, then assign each posting to its strongest role match.

    The complete Brett-approved catalog remains the default search plan. Runtime
    limits are optional controls for diagnostics or emergency quota management;
    they are disabled unless explicitly configured.
    """
    validate_preflight()
    registry = registry or SeenJobsRegistry()

    planned_roles = list(search_roles if search_roles is not None else config.ROLES)
    effective_max = config.JSEARCH_MAX_QUERIES_PER_RUN if max_queries is None else max_queries
    if effective_max is not None and effective_max < 0:
        raise ValueError("max_queries cannot be negative")
    roles_to_query = planned_roles[:effective_max] if effective_max else planned_roles
    estimated_request_units = validate_query_budget(len(roles_to_query))

    candidates_by_job_id: Dict[str, List[Dict]] = {}
    failed_roles: List[str] = []
    zero_result_roles: List[str] = []
    raw_role_counts: Dict[str, int] = {}
    canonical_roles = [canonical_role_for_search(role) for role in roles_to_query]
    stats = {
        "queries_planned": len(planned_roles),
        "queries_scheduled": len(roles_to_query),
        "queries_attempted": 0,
        "queries_succeeded": 0,
        "num_pages_per_query": config.NUM_PAGES,
        "estimated_request_units": estimated_request_units,
        "estimated_unit_budget": config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN,
        "queries_failed": 0,
        "query_plan_truncated": len(roles_to_query) < len(planned_roles),
        "query_stop_reason": (
            f"max_queries={effective_max}" if len(roles_to_query) < len(planned_roles) else ""
        ),
        "query_metrics": {},
        "quota": {
            "limit": None,
            "remaining": None,
            "lowest_remaining": None,
            "reset": None,
            "used": None,
        },
        "raw_role_counts": raw_role_counts,
        "selected_role_counts": {role: 0 for role in dict.fromkeys(canonical_roles)},
        "selected_query_counts": {role: 0 for role in roles_to_query},
        "zero_result_roles": zero_result_roles,
        "excluded_by_seniority": 0,
        "excluded_role_mismatch": 0,
        "ambiguous_role_matches": 0,
        "query_duplicates": 0,
        "previously_seen_removed": 0,
        "missing_job_id_skipped": 0,
    }

    for search_role in roles_to_query:
        canonical_role = canonical_role_for_search(search_role)
        stats["queries_attempted"] += 1
        logger.info("[%s] Searching JSearch...", search_role)
        fetch_meta = JSearchFetchResult(jobs=[], duration_seconds=0.0, quota={})
        try:
            fetch_meta = _coerce_fetch_result(fetch_jobs_for_role(search_role))
            raw_jobs = fetch_meta.jobs
            stats["queries_succeeded"] += 1
            logger.info("[%s] Fetched %d raw postings", search_role, len(raw_jobs))
            query_status = "ok"
            query_error = ""
        except QuotaExhaustedError:
            logger.error(
                "[%s] JSearch monthly/subscription quota is exhausted; aborting the "
                "remaining query plan immediately.",
                search_role,
            )
            raise
        except Exception as exc:
            logger.exception("[%s] Search failed: %s", search_role, exc)
            raw_jobs = []
            failed_roles.append(search_role)
            stats["queries_failed"] += 1
            query_status = "error"
            query_error = str(exc)

        quota = fetch_meta.quota or {}
        remaining = quota.get("remaining")
        if quota:
            for key in ("limit", "remaining", "reset", "used"):
                if quota.get(key) is not None:
                    stats["quota"][key] = quota[key]
            if isinstance(remaining, int):
                lowest = stats["quota"]["lowest_remaining"]
                stats["quota"]["lowest_remaining"] = (
                    remaining if lowest is None else min(lowest, remaining)
                )

        raw_role_counts[search_role] = len(raw_jobs)
        stats["query_metrics"][search_role] = {
            "canonical_role": canonical_role,
            "status": query_status,
            "error": query_error,
            "raw_jobs": len(raw_jobs),
            "duration_seconds": fetch_meta.duration_seconds,
            "quota_remaining_after": remaining,
        }

        # A successful query with zero matches is a valid market observation,
        # not an API failure. Track it separately so a broad role catalog does
        # not trip the production failure gate.
        if not raw_jobs and search_role not in failed_roles:
            zero_result_roles.append(search_role)

        for job in raw_jobs:
            job_id = _candidate_key(job)
            if not job_id:
                stats["missing_job_id_skipped"] += 1
                continue
            if registry.has_job_id(job_id):
                stats["previously_seen_removed"] += 1
                continue
            if is_excluded_title(job.get("job_title", "")):
                stats["excluded_by_seniority"] += 1
                continue

            assessment = assess_role(job, canonical_role)
            candidate = dict(job)
            candidate["_matched_role"] = canonical_role
            candidate["_search_role"] = search_role
            candidate["_role_specificity"] = role_specificity(canonical_role)
            candidate["_role_relevance_status"] = assessment.status
            candidate["_role_relevance_points"] = assessment.score
            candidate["_role_relevance_score"] = normalize_relevance_score(assessment.score)
            candidate["_role_relevance_reasons"] = assessment.reasons
            candidates_by_job_id.setdefault(job_id, []).append(candidate)

        if (
            config.JSEARCH_STOP_ON_LOW_QUOTA
            and config.JSEARCH_MIN_REMAINING_REQUESTS > 0
            and isinstance(remaining, int)
            and remaining <= config.JSEARCH_MIN_REMAINING_REQUESTS
        ):
            stats["query_plan_truncated"] = True
            stats["query_stop_reason"] = (
                "quota_remaining="
                f"{remaining} <= JSEARCH_MIN_REMAINING_REQUESTS="
                f"{config.JSEARCH_MIN_REMAINING_REQUESTS}"
            )
            logger.warning("Stopping JSearch plan early: %s", stats["query_stop_reason"])
            break

        time.sleep(config.SEARCH_DELAY_SECONDS)

    selected_jobs: List[Dict] = []
    for job_id, candidates in candidates_by_job_id.items():
        if len(candidates) > 1:
            stats["query_duplicates"] += len(candidates) - 1
        candidates.sort(
            key=lambda item: (
                item.get("_role_relevance_status") == "accept",
                item.get("_role_relevance_score", 0),
                item.get("_role_specificity", 0),
            ),
            reverse=True,
        )
        best = candidates[0]
        best["_query_roles"] = list(dict.fromkeys(
            item["_matched_role"] for item in candidates
        ))
        best["_query_search_titles"] = list(dict.fromkeys(
            item.get("_search_role", item["_matched_role"]) for item in candidates
        ))
        best["_query_role_scores"] = {
            item.get("_search_role", item["_matched_role"]): item.get("_role_relevance_score", 0)
            for item in candidates
        }
        best["_query_role_points"] = {
            item.get("_search_role", item["_matched_role"]): item.get("_role_relevance_points", 0)
            for item in candidates
        }
        if best.get("_role_relevance_status") == "reject":
            stats["excluded_role_mismatch"] += 1
            continue
        if best.get("_role_relevance_status") == "review":
            stats["ambiguous_role_matches"] += 1
        stats["selected_role_counts"][best["_matched_role"]] += 1
        selected_search_role = best.get("_search_role", best["_matched_role"])
        stats["selected_query_counts"].setdefault(selected_search_role, 0)
        stats["selected_query_counts"][selected_search_role] += 1
        selected_jobs.append(best)

    for search_role, selected_count in stats["selected_query_counts"].items():
        if search_role in stats["query_metrics"]:
            stats["query_metrics"][search_role]["selected_jobs"] = selected_count

    attempted = stats["queries_attempted"]
    allowed_failures = _allowed_role_failures(attempted)
    stats["allowed_role_failures"] = allowed_failures
    stats["role_failure_rate"] = (
        round(len(set(failed_roles)) / attempted, 4) if attempted else 0.0
    )

    output_path = str(Path(config.OUTPUT_DIR) / f"jobs_{datetime.now():%Y-%m-%d}.json")
    Path(output_path).write_text(
        json.dumps(
            {
                "scrape_date": datetime.now().isoformat(),
                "date_posted_window": config.DATE_POSTED,
                "total_jobs": len(selected_jobs),
                "stats": stats,
                "jobs": selected_jobs,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    roles_with_results = sum(
        1 for count in stats["selected_role_counts"].values() if count > 0
    )
    errors: List[str] = []
    if len(selected_jobs) < config.MIN_JOBS_PER_RUN:
        errors.append(
            f"Only {len(selected_jobs)} role-relevant jobs scraped "
            f"(minimum {config.MIN_JOBS_PER_RUN})"
        )
    if roles_with_results < config.MIN_ROLES_WITH_RESULTS:
        errors.append(
            f"Only {roles_with_results} roles returned selected jobs "
            f"(minimum {config.MIN_ROLES_WITH_RESULTS})"
        )
    failed_count = len(set(failed_roles))
    if failed_count > allowed_failures:
        errors.append(
            f"{failed_count} role searches failed "
            f"(maximum {allowed_failures} for {attempted} attempted queries)"
        )

    success = not errors or not config.PRODUCTION
    logger.info(
        "Scrape complete: %d jobs from %d/%d successful queries -> %s",
        len(selected_jobs),
        stats["queries_succeeded"],
        stats["queries_attempted"],
        output_path,
    )
    return ScrapeResult(
        output_path=output_path,
        total_jobs=len(selected_jobs),
        stats=stats,
        failed_roles=sorted(set(failed_roles)),
        roles_with_results=roles_with_results,
        success=success,
        errors=errors,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_daily_scrape()
    if config.PRODUCTION and not result.success:
        sys.exit(1)
