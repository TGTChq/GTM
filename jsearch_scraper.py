"""Step 1: pull fresh job postings from JSearch and choose the best role match."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import config
from job_filter import assess_pre_enrichment_viability
from job_quality import normalize_job_identity
from http_utils import QuotaExhaustedError, request_with_retry, safe_json
from pipeline_state import SeenJobsRegistry
from role_catalog import canonical_role_for_search, role_specificity
from role_mapping import get_bucket_name
from role_relevance import assess_role, normalize_relevance_score

logger = logging.getLogger(__name__)

_QUERY_VARIANTS = {"base", "hiring", "linkedin", "indeed", "glassdoor"}


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


def build_search_query(
    role: str, *, intent_variant: bool = False, query_variant: Optional[str] = None
) -> str:
    """Build a remote-biased US query without changing the canonical role.

    The reserved lookback pass can use publisher-scoped variants supported by
    JSearch (``via linkedin``, ``via indeed``, ``via glassdoor``). These improve
    retrieval diversity without weakening any local quality gate.
    """
    cleaned = str(role or "").strip()
    variant = str(query_variant or ("hiring" if intent_variant else "base")).strip().lower()
    if variant not in _QUERY_VARIANTS:
        raise ValueError(f"Unsupported JSearch query variant: {variant!r}")

    remote_role = cleaned
    if config.JSEARCH_REMOTE_QUERY_BIAS and not re.search(r"\bremote\b", cleaned, re.I):
        remote_role = f"remote {cleaned}"
    if variant == "hiring":
        return f"{remote_role} jobs hiring in United States"
    if variant in {"linkedin", "indeed", "glassdoor"}:
        return f"{remote_role} in United States via {variant}"
    return f"{remote_role} in United States"


def fetch_jobs_for_role(
    role: str, *, page: int = 1, num_pages: Optional[int] = None,
    date_posted: Optional[str] = None, intent_variant: bool = False,
    query_variant: Optional[str] = None,
) -> JSearchFetchResult:
    headers = {
        "x-rapidapi-key": config.RAPIDAPI_KEY,
        "x-rapidapi-host": config.JSEARCH_HOST,
        "Content-Type": "application/json",
    }
    params = {
        "query": build_search_query(
            role, intent_variant=intent_variant, query_variant=query_variant
        ),
        "page": str(max(1, int(page))),
        "num_pages": str(config.NUM_PAGES if num_pages is None else max(1, int(num_pages))),
        "date_posted": date_posted or config.DATE_POSTED,
        "country": config.COUNTRY,
    }
    if config.JSEARCH_REMOTE_JOBS_ONLY:
        params[config.JSEARCH_REMOTE_FILTER_PARAMETER] = "true"
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
    if config.JSEARCH_MAX_EXTRA_PAGES_PER_ROLE < 0:
        raise ValueError("JSEARCH_MAX_EXTRA_PAGES_PER_ROLE cannot be negative")
    if config.JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES < 0:
        raise ValueError("JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES cannot be negative")
    if config.JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE < 0:
        raise ValueError("JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE cannot be negative")
    if config.JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES < 0:
        raise ValueError("JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES cannot be negative")
    if config.JSEARCH_TARGET_PREFILTER_VIABLE < 0:
        raise ValueError("JSEARCH_TARGET_PREFILTER_VIABLE cannot be negative")
    if config.JSEARCH_TOPUP_INITIAL_PAGES < 1:
        raise ValueError("JSEARCH_TOPUP_INITIAL_PAGES must be at least 1")
    if config.JSEARCH_TOPUP_MAX_ROUNDS < 0:
        raise ValueError("JSEARCH_TOPUP_MAX_ROUNDS cannot be negative")
    if config.JSEARCH_TOPUP_MAX_UNITS_PER_ROUND < 0:
        raise ValueError("JSEARCH_TOPUP_MAX_UNITS_PER_ROUND cannot be negative")
    if config.JSEARCH_TOPUP_PAGES_PER_QUERY < 1:
        raise ValueError("JSEARCH_TOPUP_PAGES_PER_QUERY must be at least 1")
    if config.JSEARCH_TOPUP_MAX_PAGE < 1:
        raise ValueError("JSEARCH_TOPUP_MAX_PAGE must be at least 1")
    if config.JSEARCH_TOPUP_PREFILTER_MULTIPLIER <= 0:
        raise ValueError("JSEARCH_TOPUP_PREFILTER_MULTIPLIER must be positive")
    if config.JSEARCH_TOPUP_MIN_PREFILTER_TARGET < 0:
        raise ValueError("JSEARCH_TOPUP_MIN_PREFILTER_TARGET cannot be negative")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", config.JSEARCH_REMOTE_FILTER_PARAMETER):
        raise ValueError("JSEARCH_REMOTE_FILTER_PARAMETER must be a valid parameter name")
    if not isinstance(config.JSEARCH_LOOKBACK_QUERY_VARIANTS, list):
        raise ValueError("JSEARCH_LOOKBACK_QUERY_VARIANTS must be a JSON list")
    invalid_variants = [
        value for value in config.JSEARCH_LOOKBACK_QUERY_VARIANTS
        if str(value).strip().lower() not in _QUERY_VARIANTS - {"base"}
    ]
    if invalid_variants:
        raise ValueError(
            "Unsupported JSEARCH_LOOKBACK_QUERY_VARIANTS: "
            + ", ".join(map(str, invalid_variants))
        )
    if not 0 <= config.MAX_ROLE_FAILURE_RATE <= 1:
        raise ValueError("MAX_ROLE_FAILURE_RATE must be between 0 and 1")



def estimate_query_units(query_count: int, num_pages: Optional[int] = None) -> int:
    """Estimate RapidAPI request units before the first network call."""
    pages = config.NUM_PAGES if num_pages is None else num_pages
    return max(0, int(query_count)) * max(1, int(pages))


def validate_query_budget(
    query_count: int, num_pages: Optional[int] = None
) -> int:
    pages = config.NUM_PAGES if num_pages is None else max(1, int(num_pages))
    estimated_units = estimate_query_units(query_count, pages)
    budget = config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN
    if budget > 0 and estimated_units > budget:
        raise ValueError(
            "Estimated JSearch usage "
            f"({query_count} queries x {pages} pages = {estimated_units} units) "
            f"exceeds JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN={budget}. "
            "Raise JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN to at least "
            "query_count x pages, or reduce the page/query count."
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


def _merge_quota_snapshot(stats: Dict, quota: Dict[str, object]) -> Optional[int]:
    remaining = quota.get("remaining") if quota else None
    if quota:
        for key in ("limit", "remaining", "reset", "used"):
            if quota.get(key) is not None:
                stats["quota"][key] = quota[key]
        if isinstance(remaining, int):
            lowest = stats["quota"]["lowest_remaining"]
            stats["quota"]["lowest_remaining"] = (
                remaining if lowest is None else min(lowest, remaining)
            )
    return remaining if isinstance(remaining, int) else None


def _prefilter_metric_name(stat_name: str) -> str:
    mapping = {
        "excluded_posting_integrity": "prefilter_rejected_posting_integrity",
        "excluded_restricted_role": "prefilter_rejected_restricted_role",
        "excluded_outsourcing": "prefilter_rejected_outsourcing",
        "excluded_contextual_mismatch": "prefilter_rejected_contextual_mismatch",
        "excluded_aggregator": "prefilter_rejected_aggregator",
        "excluded_stale": "prefilter_rejected_stale",
        "excluded_staffing": "prefilter_rejected_staffing",
        "excluded_industry": "prefilter_rejected_industry",
        "excluded_role_mismatch": "prefilter_rejected_role_mismatch",
        "excluded_in_person": "prefilter_rejected_in_person",
        "excluded_non_paying": "prefilter_rejected_non_paying",
        "excluded_non_active": "prefilter_rejected_non_active",
        "excluded_non_full_time": "prefilter_rejected_non_full_time",
        "excluded_non_us": "prefilter_rejected_non_us",
    }
    return mapping.get(stat_name, "prefilter_rejected_other")


def _ingest_query_jobs(
    *,
    raw_jobs: List[Dict],
    search_role: str,
    canonical_role: str,
    registry: SeenJobsRegistry,
    candidates_by_job_id: Dict[str, List[Dict]],
    stats: Dict,
    exclude_job_ids: Optional[set[str]] = None,
) -> Dict[str, int]:
    metrics = {
        "accepted_candidates": 0,
        "review_candidates": 0,
        "rejected_candidates": 0,
        "candidate_rows": 0,
        "new_unique_candidates": 0,
        "prefilter_viable_candidates": 0,
        "new_prefilter_viable_candidates": 0,
        "prefilter_rejected_candidates": 0,
        "prefilter_rejected_posting_integrity": 0,
        "prefilter_rejected_restricted_role": 0,
        "prefilter_rejected_outsourcing": 0,
        "prefilter_rejected_contextual_mismatch": 0,
        "prefilter_rejected_aggregator": 0,
        "prefilter_rejected_stale": 0,
        "prefilter_rejected_staffing": 0,
        "prefilter_rejected_industry": 0,
        "prefilter_rejected_role_mismatch": 0,
        "prefilter_rejected_in_person": 0,
        "prefilter_rejected_non_paying": 0,
        "prefilter_rejected_non_active": 0,
        "prefilter_rejected_non_full_time": 0,
        "prefilter_rejected_non_us": 0,
        "prefilter_rejected_other": 0,
    }
    for job in raw_jobs:
        job_id = _candidate_key(job)
        if not job_id:
            stats["missing_job_id_skipped"] += 1
            continue
        if exclude_job_ids and job_id in exclude_job_ids:
            stats["in_run_existing_job_ids_removed"] = (
                int(stats.get("in_run_existing_job_ids_removed", 0)) + 1
            )
            continue
        if registry.has_job_id(job_id):
            stats["previously_seen_removed"] += 1
            continue
        if is_excluded_title(job.get("job_title", "")):
            stats["excluded_by_seniority"] += 1
            continue

        already_seen_in_run = job_id in candidates_by_job_id
        # Preserve the request constraints that produced this row. The provider's
        # returned country/remote fields alone may be query echoes, but agreement
        # between the explicit request constraints and the returned structured
        # fields is useful corroboration when no foreign/global contradiction is
        # present.
        job["_jsearch_country_filter"] = config.COUNTRY
        job["_jsearch_remote_filter_applied"] = bool(config.JSEARCH_REMOTE_JOBS_ONLY)
        normalize_job_identity(job)
        assessment = assess_role(job, canonical_role)
        candidate = dict(job)
        candidate["_matched_role"] = canonical_role
        candidate["_search_role"] = search_role
        candidate["_role_specificity"] = role_specificity(canonical_role)
        candidate["_role_relevance_status"] = assessment.status
        candidate["_role_relevance_points"] = assessment.score
        candidate["_role_relevance_score"] = normalize_relevance_score(assessment.score)
        candidate["_role_relevance_reasons"] = assessment.reasons

        prefilter = assess_pre_enrichment_viability(candidate)
        candidate["_prefilter_viable"] = prefilter.eligible
        candidate["_prefilter_stat"] = prefilter.stat_name
        candidate["_prefilter_reason"] = prefilter.reason
        candidates_by_job_id.setdefault(job_id, []).append(candidate)

        metrics["candidate_rows"] += 1
        if not already_seen_in_run:
            metrics["new_unique_candidates"] += 1
        if assessment.status == "accept":
            metrics["accepted_candidates"] += 1
        elif assessment.status == "review":
            metrics["review_candidates"] += 1
        else:
            metrics["rejected_candidates"] += 1

        # Deepening yield must reflect candidates that can actually survive Step 2.
        if assessment.status in {"accept", "review"}:
            if prefilter.eligible:
                metrics["prefilter_viable_candidates"] += 1
                if not already_seen_in_run:
                    metrics["new_prefilter_viable_candidates"] += 1
            else:
                metrics["prefilter_rejected_candidates"] += 1
                metrics[_prefilter_metric_name(prefilter.stat_name)] += 1
    return metrics


def _record_query_variant(
    stats: Dict,
    *,
    variant: str,
    raw_jobs: List[Dict],
    ingest: Dict[str, int],
    duration_seconds: float,
    success: bool,
) -> None:
    """Aggregate yield by query formulation for evidence-based tuning."""
    metric = stats["query_variant_metrics"].setdefault(
        variant,
        {
            "queries": 0,
            "queries_succeeded": 0,
            "raw_jobs": 0,
            "new_unique_candidates": 0,
            "new_prefilter_viable_candidates": 0,
            "prefilter_rejected_candidates": 0,
            "duration_seconds": 0.0,
        },
    )
    metric["queries"] += 1
    metric["queries_succeeded"] += int(success)
    metric["raw_jobs"] += len(raw_jobs)
    metric["duration_seconds"] = round(
        float(metric.get("duration_seconds", 0.0)) + duration_seconds, 3
    )
    for key in (
        "new_unique_candidates",
        "new_prefilter_viable_candidates",
        "prefilter_rejected_candidates",
    ):
        metric[key] += int(ingest.get(key, 0))


def _select_best_jobs(
    candidates_by_job_id: Dict[str, List[Dict]], stats: Dict
) -> List[Dict]:
    """Collapse multi-query matches to one strongest role assignment per job."""
    selected_jobs: List[Dict] = []
    for job_id, candidates in candidates_by_job_id.items():
        if len(candidates) > 1:
            stats["query_duplicates"] = int(stats.get("query_duplicates", 0)) + len(candidates) - 1
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
            stats["excluded_role_mismatch"] = int(stats.get("excluded_role_mismatch", 0)) + 1
            continue
        if best.get("_role_relevance_status") == "review":
            stats["ambiguous_role_matches"] = int(stats.get("ambiguous_role_matches", 0)) + 1
        matched_role = best["_matched_role"]
        stats.setdefault("selected_role_counts", {})
        stats["selected_role_counts"][matched_role] = (
            int(stats["selected_role_counts"].get(matched_role, 0)) + 1
        )
        selected_search_role = best.get("_search_role", matched_role)
        stats.setdefault("selected_query_counts", {})
        stats["selected_query_counts"][selected_search_role] = (
            int(stats["selected_query_counts"].get(selected_search_role, 0)) + 1
        )
        selected_jobs.append(best)

    for search_role, selected_count in stats.get("selected_query_counts", {}).items():
        if search_role in stats.get("query_metrics", {}):
            stats["query_metrics"][search_role]["selected_jobs"] = selected_count
    return selected_jobs


def _adaptive_role_score(role: str, query_metrics: Dict[str, Dict], role_order: Dict[str, int]):
    metric = query_metrics[role]
    return (
        int(metric.get("new_prefilter_viable_candidates", 0)),
        int(metric.get("prefilter_viable_candidates", 0)),
        int(metric.get("accepted_candidates", 0)),
        int(metric.get("review_candidates", 0)),
        int(metric.get("new_unique_candidates", 0)),
        int(metric.get("raw_jobs", 0)),
        -role_order[role],
    )


def _balanced_adaptive_role_order(
    roles: List[str], query_metrics: Dict[str, Dict], role_order: Dict[str, int]
) -> List[str]:
    """Round-robin viable roles across buckets before allocating overflow."""
    if not config.JSEARCH_ADAPTIVE_BUCKET_BALANCING:
        return sorted(
            roles,
            key=lambda role: _adaptive_role_score(role, query_metrics, role_order),
            reverse=True,
        )

    by_bucket: Dict[str, List[str]] = defaultdict(list)
    for role in roles:
        canonical_role = query_metrics[role].get("canonical_role") or canonical_role_for_search(role)
        by_bucket[get_bucket_name(canonical_role)].append(role)
    for bucket_roles in by_bucket.values():
        bucket_roles.sort(
            key=lambda role: _adaptive_role_score(role, query_metrics, role_order),
            reverse=True,
        )

    bucket_order = sorted(
        by_bucket,
        key=lambda bucket: (
            sum(
                int(query_metrics[role].get("new_prefilter_viable_candidates", 0))
                for role in by_bucket[bucket]
            ),
            -min(role_order[role] for role in by_bucket[bucket]),
            bucket,
        ),
        reverse=True,
    )
    ordered: List[str] = []
    while any(by_bucket[bucket] for bucket in bucket_order):
        for bucket in bucket_order:
            if by_bucket[bucket]:
                ordered.append(by_bucket[bucket].pop(0))
    return ordered


def _adaptive_deepening_is_enabled(
    *,
    search_roles: Optional[List[str]],
    max_queries: Optional[int],
    effective_max: Optional[int],
    planned_roles: List[str],
    num_pages: Optional[int] = None,
    allow_adaptive: Optional[bool] = None,
) -> bool:
    # Keep smoke tests and intentionally limited diagnostics deterministic.
    pages = config.NUM_PAGES if num_pages is None else max(1, int(num_pages))
    if allow_adaptive is False:
        return False
    return bool(
        config.JSEARCH_ADAPTIVE_DEEPENING
        and pages == 1
        and config.JSEARCH_MAX_EXTRA_PAGES_PER_ROLE > 0
        and search_roles is None
        and max_queries is None
        and len(planned_roles) >= 50
        and not effective_max
        and len(planned_roles) >= 100
    )


def run_daily_scrape(
    registry: Optional[SeenJobsRegistry] = None,
    *,
    search_roles: Optional[List[str]] = None,
    max_queries: Optional[int] = None,
    base_num_pages: Optional[int] = None,
    allow_adaptive: Optional[bool] = None,
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
    base_pages = (
        config.NUM_PAGES if base_num_pages is None else max(1, int(base_num_pages))
    )
    estimated_request_units = validate_query_budget(
        len(roles_to_query), num_pages=base_pages
    )

    candidates_by_job_id: Dict[str, List[Dict]] = {}
    failed_roles: List[str] = []
    zero_result_roles: List[str] = []
    raw_role_counts: Dict[str, int] = {}
    canonical_roles = [canonical_role_for_search(role) for role in roles_to_query]
    lookback_enabled = bool(
        allow_adaptive is not False
        and config.JSEARCH_ADAPTIVE_LOOKBACK
        and config.PRODUCTION
        and search_roles is None
        and max_queries is None
        and len(planned_roles) >= 100
        and not effective_max
    )
    stats = {
        "queries_planned": len(planned_roles),
        "queries_scheduled": len(roles_to_query),
        "queries_attempted": 0,
        "queries_succeeded": 0,
        "num_pages_per_query": base_pages,
        "base_estimated_request_units": estimated_request_units,
        "estimated_request_units": estimated_request_units,
        "estimated_unit_budget": config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN,
        "adaptive_deepening_enabled": _adaptive_deepening_is_enabled(
            search_roles=search_roles,
            max_queries=max_queries,
            effective_max=effective_max,
            planned_roles=planned_roles,
            num_pages=base_pages,
            allow_adaptive=allow_adaptive,
        ),
        "adaptive_extra_queries": 0,
        "adaptive_extra_units": 0,
        "adaptive_query_cap": config.JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES,
        "adaptive_candidate_roles": [],
        "adaptive_deepened_roles": [],
        "adaptive_bucket_counts": {},
        "adaptive_prefilter_viable_added": 0,
        "adaptive_zero_yield_roles": [],
        "adaptive_stop_reason": "",
        "adaptive_lookback_enabled": lookback_enabled,
        "adaptive_lookback_queries": 0,
        "adaptive_lookback_roles": [],
        "adaptive_lookback_prefilter_viable_added": 0,
        "adaptive_lookback_date_posted": config.JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED,
        "queries_failed": 0,
        "query_plan_truncated": len(roles_to_query) < len(planned_roles),
        "query_stop_reason": (
            f"max_queries={effective_max}" if len(roles_to_query) < len(planned_roles) else ""
        ),
        "query_metrics": {},
        "query_variant_metrics": {},
        "adaptive_lookback_variant_counts": {},
        "adaptive_lookback_query_details": [],
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
        "in_run_existing_job_ids_removed": 0,
        "missing_job_id_skipped": 0,
    }

    for search_role in roles_to_query:
        canonical_role = canonical_role_for_search(search_role)
        stats["queries_attempted"] += 1
        logger.info("[%s] Searching JSearch...", search_role)
        fetch_meta = JSearchFetchResult(jobs=[], duration_seconds=0.0, quota={})
        try:
            fetch_meta = _coerce_fetch_result(
                fetch_jobs_for_role(search_role)
                if base_num_pages is None
                else fetch_jobs_for_role(search_role, num_pages=base_pages)
            )
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
        remaining = _merge_quota_snapshot(stats, quota)

        raw_role_counts[search_role] = len(raw_jobs)
        query_ingest = _ingest_query_jobs(
            raw_jobs=raw_jobs,
            search_role=search_role,
            canonical_role=canonical_role,
            registry=registry,
            candidates_by_job_id=candidates_by_job_id,
            stats=stats,
        )
        stats["query_metrics"][search_role] = {
            "canonical_role": canonical_role,
            "status": query_status,
            "error": query_error,
            "raw_jobs": len(raw_jobs),
            "duration_seconds": fetch_meta.duration_seconds,
            "quota_remaining_after": remaining,
            "pages": [{
                "page": 1,
                "num_pages": base_pages,
                "last_page": base_pages,
                "query_variant": "base",
                "query": build_search_query(search_role),
                "raw_jobs": len(raw_jobs),
                "duration_seconds": fetch_meta.duration_seconds,
                "quota_remaining_after": remaining,
                **query_ingest,
            }],
            **query_ingest,
        }
        _record_query_variant(
            stats,
            variant="base",
            raw_jobs=raw_jobs,
            ingest=query_ingest,
            duration_seconds=fetch_meta.duration_seconds,
            success=query_status == "ok",
        )

        # A successful query with zero matches is a valid market observation,
        # not an API failure. Track it separately so a broad role catalog does
        # not trip the production failure gate.
        if not raw_jobs and search_role not in failed_roles:
            zero_result_roles.append(search_role)

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

    # Use only unused request units for page-2 discovery. Roles qualify only when
    # page 1 produced candidates that survive the same zero-credit gates as Step 2.
    # Allocation is round-robin by functional bucket before any bucket receives
    # additional overflow, preventing one broad function from consuming the run.
    if stats["adaptive_deepening_enabled"] and not stats["query_stop_reason"]:
        unit_budget = config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN
        budget_remaining = (
            max(0, unit_budget - estimated_request_units) if unit_budget > 0 else 0
        )
        configured_cap = config.JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES
        lookback_reserve = (
            min(config.JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES, budget_remaining)
            if lookback_enabled
            else 0
        )
        page2_budget = max(0, budget_remaining - lookback_reserve)
        remaining_units = min(page2_budget, configured_cap) if configured_cap > 0 else 0
        role_order = {role: index for index, role in enumerate(roles_to_query)}
        eligible_roles = [
            role
            for role in roles_to_query
            if int(
                stats["query_metrics"].get(role, {}).get(
                    "new_prefilter_viable_candidates", 0
                )
            )
            >= config.JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE
        ]
        ranked_roles = _balanced_adaptive_role_order(
            eligible_roles, stats["query_metrics"], role_order
        )
        stats["adaptive_candidate_roles"] = list(ranked_roles)
        active_roles = list(ranked_roles)

        for extra_page_offset in range(config.JSEARCH_MAX_EXTRA_PAGES_PER_ROLE):
            page_number = 2 + extra_page_offset
            next_active_roles: List[str] = []
            for search_role in active_roles:
                if remaining_units <= 0:
                    stats["adaptive_stop_reason"] = "adaptive_query_or_unit_budget_exhausted"
                    break
                canonical_role = canonical_role_for_search(search_role)
                role_bucket = get_bucket_name(canonical_role)
                stats["queries_attempted"] += 1
                stats["adaptive_extra_queries"] += 1
                stats["adaptive_extra_units"] += 1
                stats["estimated_request_units"] += 1
                remaining_units -= 1
                logger.info(
                    "[%s] Adaptive JSearch deepening on page %d (bucket=%s)...",
                    search_role,
                    page_number,
                    role_bucket,
                )
                fetch_meta = JSearchFetchResult(jobs=[], duration_seconds=0.0, quota={})
                try:
                    fetch_meta = _coerce_fetch_result(
                        fetch_jobs_for_role(search_role, page=page_number, num_pages=1)
                    )
                    raw_jobs = fetch_meta.jobs
                    stats["queries_succeeded"] += 1
                    query_status = "ok"
                    query_error = ""
                except QuotaExhaustedError:
                    logger.error(
                        "[%s] JSearch quota exhausted during adaptive page %d; aborting.",
                        search_role,
                        page_number,
                    )
                    raise
                except Exception as exc:
                    logger.exception(
                        "[%s] Adaptive page %d failed: %s",
                        search_role,
                        page_number,
                        exc,
                    )
                    raw_jobs = []
                    failed_roles.append(f"{search_role} (page {page_number})")
                    stats["queries_failed"] += 1
                    query_status = "error"
                    query_error = str(exc)

                remaining = _merge_quota_snapshot(stats, fetch_meta.quota or {})
                query_ingest = _ingest_query_jobs(
                    raw_jobs=raw_jobs,
                    search_role=search_role,
                    canonical_role=canonical_role,
                    registry=registry,
                    candidates_by_job_id=candidates_by_job_id,
                    stats=stats,
                )
                raw_role_counts[search_role] = raw_role_counts.get(search_role, 0) + len(raw_jobs)
                metric = stats["query_metrics"][search_role]
                metric["status"] = "error" if query_status == "error" else metric.get("status", "ok")
                if query_error:
                    metric["error"] = " | ".join(
                        filter(None, [metric.get("error", ""), query_error])
                    )
                metric["raw_jobs"] += len(raw_jobs)
                metric["duration_seconds"] = round(
                    float(metric.get("duration_seconds", 0)) + fetch_meta.duration_seconds, 3
                )
                metric["quota_remaining_after"] = remaining
                for key, value in query_ingest.items():
                    metric[key] = int(metric.get(key, 0)) + value
                metric["pages"].append({
                    "page": page_number,
                    "query_variant": "base",
                    "query": build_search_query(search_role),
                    "raw_jobs": len(raw_jobs),
                    "duration_seconds": fetch_meta.duration_seconds,
                    "quota_remaining_after": remaining,
                    **query_ingest,
                })
                _record_query_variant(
                    stats,
                    variant="base",
                    raw_jobs=raw_jobs,
                    ingest=query_ingest,
                    duration_seconds=fetch_meta.duration_seconds,
                    success=query_status == "ok",
                )
                stats["adaptive_deepened_roles"].append(search_role)
                bucket_counts = stats["adaptive_bucket_counts"]
                bucket_counts[role_bucket] = int(bucket_counts.get(role_bucket, 0)) + 1
                page_viable = int(query_ingest.get("new_prefilter_viable_candidates", 0))
                stats["adaptive_prefilter_viable_added"] += page_viable
                logger.info(
                    "[%s] Adaptive page %d fetched %d raw postings; %d new jobs "
                    "survived pre-enrichment gates",
                    search_role,
                    page_number,
                    len(raw_jobs),
                    page_viable,
                )
                if page_viable >= config.JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE:
                    next_active_roles.append(search_role)
                else:
                    stats["adaptive_zero_yield_roles"].append(search_role)

                if (
                    config.JSEARCH_STOP_ON_LOW_QUOTA
                    and config.JSEARCH_MIN_REMAINING_REQUESTS > 0
                    and isinstance(remaining, int)
                    and remaining <= config.JSEARCH_MIN_REMAINING_REQUESTS
                ):
                    stats["adaptive_stop_reason"] = (
                        "quota_remaining="
                        f"{remaining} <= JSEARCH_MIN_REMAINING_REQUESTS="
                        f"{config.JSEARCH_MIN_REMAINING_REQUESTS}"
                    )
                    break
                time.sleep(config.SEARCH_DELAY_SECONDS)
            if stats["adaptive_stop_reason"]:
                break
            active_roles = _balanced_adaptive_role_order(
                next_active_roles, stats["query_metrics"], role_order
            )
            if not active_roles:
                stats["adaptive_stop_reason"] = "no_roles_with_incremental_prefilter_yield"
                break

    # A second, diversified pass uses the reserved budget only when the first
    # pass did not produce enough zero-credit viable inventory. It widens the
    # posting window for the best-yielding roles instead of weakening filters or
    # fetching page 2 for all 118 roles. In-run job-id dedupe removes overlap.
    viable_ids = {
        job_id
        for job_id, candidates in candidates_by_job_id.items()
        if any(
            item.get("_role_relevance_status") in {"accept", "review"}
            and item.get("_prefilter_viable")
            for item in candidates
        )
    }
    unit_budget = config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN
    budget_remaining = max(0, unit_budget - stats["estimated_request_units"]) if unit_budget > 0 else 0
    if (
        lookback_enabled
        and budget_remaining > 0
        and len(viable_ids) < config.JSEARCH_TARGET_PREFILTER_VIABLE
        and not stats.get("query_stop_reason")
    ):
        role_order = {role: index for index, role in enumerate(roles_to_query)}
        lookback_roles = _balanced_adaptive_role_order(
            list(roles_to_query), stats["query_metrics"], role_order
        )
        lookback_cap = min(
            budget_remaining, config.JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES
        )
        lookback_variants = [
            str(value).strip().lower()
            for value in config.JSEARCH_LOOKBACK_QUERY_VARIANTS
            if str(value).strip()
        ] or ["hiring"]
        for lookback_index, search_role in enumerate(lookback_roles[:lookback_cap]):
            query_variant = lookback_variants[lookback_index % len(lookback_variants)]
            canonical_role = canonical_role_for_search(search_role)
            stats["queries_attempted"] += 1
            stats["queries_succeeded"] += 0
            stats["adaptive_lookback_queries"] += 1
            stats["estimated_request_units"] += 1
            fetch_meta = JSearchFetchResult(jobs=[], duration_seconds=0.0, quota={})
            try:
                fetch_meta = _coerce_fetch_result(
                    fetch_jobs_for_role(
                        search_role,
                        page=1,
                        num_pages=1,
                        date_posted=config.JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED,
                        intent_variant=query_variant == "hiring",
                        query_variant=query_variant,
                    )
                )
                raw_jobs = fetch_meta.jobs
                stats["queries_succeeded"] += 1
            except QuotaExhaustedError:
                raise
            except Exception as exc:
                logger.exception("[%s] Adaptive lookback failed: %s", search_role, exc)
                raw_jobs = []
                failed_roles.append(f"{search_role} (lookback)")
                stats["queries_failed"] += 1
            remaining = _merge_quota_snapshot(stats, fetch_meta.quota or {})
            query_ingest = _ingest_query_jobs(
                raw_jobs=raw_jobs,
                search_role=search_role,
                canonical_role=canonical_role,
                registry=registry,
                candidates_by_job_id=candidates_by_job_id,
                stats=stats,
            )
            added = int(query_ingest.get("new_prefilter_viable_candidates", 0))
            stats["adaptive_lookback_prefilter_viable_added"] += added
            stats["adaptive_lookback_roles"].append(search_role)
            variant_counts = stats["adaptive_lookback_variant_counts"]
            variant_counts[query_variant] = int(variant_counts.get(query_variant, 0)) + 1
            stats["adaptive_lookback_query_details"].append({
                "role": search_role,
                "query_variant": query_variant,
                "query": build_search_query(search_role, query_variant=query_variant),
                "raw_jobs": len(raw_jobs),
                "new_prefilter_viable_candidates": added,
            })
            _record_query_variant(
                stats,
                variant=query_variant,
                raw_jobs=raw_jobs,
                ingest=query_ingest,
                duration_seconds=fetch_meta.duration_seconds,
                success=not any(
                    failed == f"{search_role} (lookback)" for failed in failed_roles
                ),
            )
            metric = stats["query_metrics"][search_role]
            metric["raw_jobs"] += len(raw_jobs)
            for key, value in query_ingest.items():
                metric[key] = int(metric.get(key, 0)) + value
            metric["pages"].append({
                "page": 1,
                "mode": "lookback",
                "query_variant": query_variant,
                "query": build_search_query(search_role, query_variant=query_variant),
                "date_posted": config.JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED,
                "raw_jobs": len(raw_jobs),
                "quota_remaining_after": remaining,
                **query_ingest,
            })
            viable_ids = {
                job_id
                for job_id, candidates in candidates_by_job_id.items()
                if any(
                    item.get("_role_relevance_status") in {"accept", "review"}
                    and item.get("_prefilter_viable")
                    for item in candidates
                )
            }
            if len(viable_ids) >= config.JSEARCH_TARGET_PREFILTER_VIABLE:
                break
            time.sleep(config.SEARCH_DELAY_SECONDS)

    stats["queries_scheduled"] = stats["queries_attempted"]

    selected_jobs = _select_best_jobs(candidates_by_job_id, stats)

    attempted = stats["queries_attempted"]
    allowed_failures = _allowed_role_failures(attempted)
    stats["allowed_role_failures"] = allowed_failures
    stats["role_failure_rate"] = (
        round(len(set(failed_roles)) / attempted, 4) if attempted else 0.0
    )

    saved_at = datetime.now()
    payload = {
        "scrape_date": saved_at.isoformat(),
        "date_posted_window": config.DATE_POSTED,
        "total_jobs": len(selected_jobs),
        "stats": stats,
        "jobs": selected_jobs,
    }
    serialized = json.dumps(payload, indent=2)
    output_path = str(Path(config.OUTPUT_DIR) / f"jobs_{saved_at:%Y-%m-%d}.json")
    Path(output_path).write_text(serialized, encoding="utf-8")

    history_dir = Path(config.OUTPUT_DIR) / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"jobs_{saved_at:%Y-%m-%d_%H-%M-%S_%f}.json"
    history_path.write_text(serialized, encoding="utf-8")
    logger.info("Immutable raw scrape archive saved to %s", history_path)

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


def _query_page_last(detail: Dict) -> int:
    page = max(1, int(detail.get("page") or 1))
    pages = max(1, int(detail.get("num_pages") or 1))
    return max(page, int(detail.get("last_page") or (page + pages - 1)))


def _next_topup_query_spec(
    metric: Dict,
    *,
    unit_budget_remaining: int,
) -> Optional[Dict[str, object]]:
    """Choose a non-overlapping page window for one targeted role query."""
    if unit_budget_remaining <= 0:
        return None
    pages_per_query = min(
        config.JSEARCH_TOPUP_PAGES_PER_QUERY, unit_budget_remaining
    )
    max_page = config.JSEARCH_TOPUP_MAX_PAGE
    page_details = list(metric.get("pages") or [])

    base_details = [
        detail for detail in page_details
        if str(detail.get("query_variant") or "base").lower() == "base"
        and str(detail.get("mode") or "").lower() != "lookback"
    ]
    next_base_page = 1 + max(
        (_query_page_last(detail) for detail in base_details), default=0
    )
    if next_base_page <= max_page:
        batch = min(pages_per_query, max_page - next_base_page + 1)
        return {
            "page": next_base_page,
            "num_pages": batch,
            "last_page": next_base_page + batch - 1,
            "query_variant": "base",
            "date_posted": config.DATE_POSTED,
            "mode": "topup_deep_page",
        }

    variants = [
        str(value).strip().lower()
        for value in config.JSEARCH_LOOKBACK_QUERY_VARIANTS
        if str(value).strip()
    ] or ["hiring"]
    date_windows = [
        str(value).strip().lower()
        for value in config.JSEARCH_TOPUP_DATE_WINDOWS
        if str(value).strip()
    ] or [config.JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED]
    candidates: List[tuple[int, int, int, str, str]] = []
    for window_index, date_window in enumerate(date_windows):
        for variant_index, variant in enumerate(variants):
            variant_details = [
                detail for detail in page_details
                if str(detail.get("query_variant") or "").lower() == variant
                and str(detail.get("date_posted") or "").lower() == date_window
                and str(detail.get("mode") or "").lower() in {"lookback", "topup_lookback"}
            ]
            next_page = 1 + max(
                (_query_page_last(detail) for detail in variant_details), default=0
            )
            if next_page <= max_page:
                candidates.append((next_page, window_index, variant_index, variant, date_window))
    if not candidates:
        return None
    next_page, _window_index, _variant_index, variant, date_window = min(candidates)
    batch = min(pages_per_query, max_page - next_page + 1)
    return {
        "page": next_page,
        "num_pages": batch,
        "last_page": next_page + batch - 1,
        "query_variant": variant,
        "date_posted": date_window,
        "mode": "topup_lookback",
    }


def _targeted_topup_role_order(
    prior_query_metrics: Dict[str, Dict],
    preferred_search_roles: Optional[List[str]] = None,
    exclude_search_roles: Optional[set[str]] = None,
) -> List[str]:
    """Prioritize roles that produced reviewable contacts, then viable jobs."""
    preferred = Counter(str(role) for role in (preferred_search_roles or []) if role)
    configured_order = {role: index for index, role in enumerate(config.ROLES)}
    roles = list(dict.fromkeys([
        *prior_query_metrics.keys(),
        *config.ROLES,
    ]))

    def metric_for(role: str) -> Dict:
        return prior_query_metrics.get(role, {})

    def score(role: str) -> tuple:
        metric = metric_for(role)
        return (
            int(preferred.get(role, 0)),
            int(metric.get("new_prefilter_viable_candidates", 0)),
            int(metric.get("prefilter_viable_candidates", 0)),
            int(metric.get("selected_jobs", 0)),
            int(metric.get("new_unique_candidates", 0)),
            int(metric.get("raw_jobs", 0)),
            -configured_order.get(role, len(configured_order) + 1),
        )

    productive = [
        role for role in roles
        if preferred.get(role, 0)
        or int(metric_for(role).get("new_prefilter_viable_candidates", 0)) > 0
        or int(metric_for(role).get("prefilter_viable_candidates", 0)) > 0
    ]
    productive.extend(
        role for role in roles
        if role not in productive and int(metric_for(role).get("raw_jobs", 0)) > 0
    )
    # Low-yield first pages are not proof that a role has no valid inventory.
    # Keep the entire approved catalog in the breadth cycle, after the live-yield
    # leaders, so deeper pages and wider date windows are eventually explored.
    productive.extend(role for role in roles if role not in productive)

    by_bucket: Dict[str, List[str]] = defaultdict(list)
    for role in productive:
        canonical = metric_for(role).get("canonical_role") or canonical_role_for_search(role)
        by_bucket[get_bucket_name(canonical)].append(role)
    for bucket_roles in by_bucket.values():
        bucket_roles.sort(key=score, reverse=True)
    bucket_order = sorted(
        by_bucket,
        key=lambda bucket: max((score(role) for role in by_bucket[bucket]), default=()),
        reverse=True,
    )
    ordered: List[str] = []
    while any(by_bucket[bucket] for bucket in bucket_order):
        for bucket in bucket_order:
            if by_bucket[bucket]:
                ordered.append(by_bucket[bucket].pop(0))
    excluded = {str(role) for role in (exclude_search_roles or set()) if role}
    return [role for role in ordered if role not in excluded]


def run_targeted_topup_scrape(
    *,
    registry: SeenJobsRegistry,
    prior_query_metrics: Dict[str, Dict],
    exclude_job_ids: Optional[set[str]] = None,
    preferred_search_roles: Optional[List[str]] = None,
    exclude_search_roles: Optional[set[str]] = None,
    unit_budget: int,
    target_prefilter_viable: int,
    round_number: int,
) -> ScrapeResult:
    """Spend a bounded unit budget on the roles with the strongest live yield.

    This is intentionally a separate pass from the full-catalog scrape. It never
    weakens quality gates and never repeats a page/query window already recorded
    in ``prior_query_metrics``. The caller decides whether another round is useful
    after seeing real Apollo contactability.
    """
    validate_preflight()
    unit_budget = max(0, int(unit_budget))
    target_prefilter_viable = max(0, int(target_prefilter_viable))
    round_number = max(1, int(round_number))
    ordered_roles = _targeted_topup_role_order(
        prior_query_metrics, preferred_search_roles, exclude_search_roles
    )
    candidates_by_job_id: Dict[str, List[Dict]] = {}
    failed_roles: List[str] = []
    raw_role_counts: Dict[str, int] = {}
    canonical_roles = [canonical_role_for_search(role) for role in ordered_roles]
    stats: Dict = {
        "mode": "reviewable_topup",
        "topup_round": round_number,
        "queries_planned": len(ordered_roles),
        "planned_search_roles": list(ordered_roles),
        "queried_search_roles": [],
        "queries_scheduled": 0,
        "queries_attempted": 0,
        "queries_succeeded": 0,
        "queries_failed": 0,
        "estimated_request_units": 0,
        "estimated_unit_budget": unit_budget,
        "topup_target_prefilter_viable": target_prefilter_viable,
        "topup_new_prefilter_viable": 0,
        "topup_stop_reason": "",
        "query_metrics": {},
        "query_variant_metrics": {},
        "quota": {
            "limit": None,
            "remaining": None,
            "lowest_remaining": None,
            "reset": None,
            "used": None,
        },
        "raw_role_counts": raw_role_counts,
        "selected_role_counts": {role: 0 for role in dict.fromkeys(canonical_roles)},
        "selected_query_counts": {role: 0 for role in ordered_roles},
        "zero_result_roles": [],
        "excluded_by_seniority": 0,
        "excluded_role_mismatch": 0,
        "ambiguous_role_matches": 0,
        "query_duplicates": 0,
        "previously_seen_removed": 0,
        "in_run_existing_job_ids_removed": 0,
        "missing_job_id_skipped": 0,
    }

    planning_metrics = json.loads(json.dumps(prior_query_metrics or {}))
    for search_role in ordered_roles:
        remaining_units = unit_budget - int(stats["estimated_request_units"])
        if remaining_units <= 0:
            stats["topup_stop_reason"] = "topup_unit_budget_exhausted"
            break
        if (
            target_prefilter_viable > 0
            and int(stats["topup_new_prefilter_viable"]) >= target_prefilter_viable
        ):
            stats["topup_stop_reason"] = "topup_prefilter_target_reached"
            break

        planning_metric = planning_metrics.setdefault(
            search_role,
            {
                "canonical_role": canonical_role_for_search(search_role),
                "pages": [],
            },
        )
        spec = _next_topup_query_spec(
            planning_metric, unit_budget_remaining=remaining_units
        )
        if spec is None:
            continue
        canonical_role = canonical_role_for_search(search_role)
        page = int(spec["page"])
        num_pages = int(spec["num_pages"])
        query_variant = str(spec["query_variant"])
        date_posted = str(spec["date_posted"])
        mode = str(spec["mode"])

        stats["queries_attempted"] += 1
        stats["queries_scheduled"] += 1
        stats["queried_search_roles"].append(search_role)
        stats["estimated_request_units"] += num_pages
        logger.info(
            "[%s] Reviewable top-up round %d: %s pages %d-%d (%s)",
            search_role,
            round_number,
            mode,
            page,
            int(spec["last_page"]),
            query_variant,
        )
        fetch_meta = JSearchFetchResult(jobs=[], duration_seconds=0.0, quota={})
        query_status = "ok"
        query_error = ""
        try:
            fetch_meta = _coerce_fetch_result(
                fetch_jobs_for_role(
                    search_role,
                    page=page,
                    num_pages=num_pages,
                    date_posted=date_posted,
                    intent_variant=query_variant == "hiring",
                    query_variant=query_variant,
                )
            )
            raw_jobs = fetch_meta.jobs
            stats["queries_succeeded"] += 1
        except QuotaExhaustedError:
            stats["topup_stop_reason"] = "jsearch_quota_exhausted"
            raise
        except Exception as exc:
            logger.exception("[%s] Reviewable top-up query failed: %s", search_role, exc)
            raw_jobs = []
            failed_roles.append(
                f"{search_role} ({mode} {query_variant} pages {page}-{spec['last_page']})"
            )
            stats["queries_failed"] += 1
            query_status = "error"
            query_error = str(exc)

        remaining = _merge_quota_snapshot(stats, fetch_meta.quota or {})
        query_ingest = _ingest_query_jobs(
            raw_jobs=raw_jobs,
            search_role=search_role,
            canonical_role=canonical_role,
            registry=registry,
            candidates_by_job_id=candidates_by_job_id,
            stats=stats,
            exclude_job_ids=exclude_job_ids,
        )
        raw_role_counts[search_role] = len(raw_jobs)
        added = int(query_ingest.get("new_prefilter_viable_candidates", 0))
        stats["topup_new_prefilter_viable"] += added
        detail = {
            "page": page,
            "num_pages": num_pages,
            "last_page": int(spec["last_page"]),
            "mode": mode,
            "query_variant": query_variant,
            "query": build_search_query(search_role, query_variant=query_variant),
            "date_posted": date_posted,
            "raw_jobs": len(raw_jobs),
            "duration_seconds": fetch_meta.duration_seconds,
            "quota_remaining_after": remaining,
            **query_ingest,
        }
        metric = stats["query_metrics"].setdefault(
            search_role,
            {
                "canonical_role": canonical_role,
                "status": "ok",
                "error": "",
                "raw_jobs": 0,
                "duration_seconds": 0.0,
                "quota_remaining_after": remaining,
                "pages": [],
            },
        )
        if query_status == "error":
            metric["status"] = "error"
            metric["error"] = query_error
        metric["raw_jobs"] += len(raw_jobs)
        metric["duration_seconds"] = round(
            float(metric.get("duration_seconds", 0.0)) + fetch_meta.duration_seconds, 3
        )
        metric["quota_remaining_after"] = remaining
        for key, value in query_ingest.items():
            metric[key] = int(metric.get(key, 0)) + value
        metric["pages"].append(detail)
        _record_query_variant(
            stats,
            variant=query_variant,
            raw_jobs=raw_jobs,
            ingest=query_ingest,
            duration_seconds=fetch_meta.duration_seconds,
            success=query_status == "ok",
        )
        planning_metric.setdefault("pages", []).append(detail)
        for key, value in query_ingest.items():
            planning_metric[key] = int(planning_metric.get(key, 0)) + value
        planning_metric["raw_jobs"] = int(planning_metric.get("raw_jobs", 0)) + len(raw_jobs)

        if not raw_jobs and query_status == "ok":
            stats["zero_result_roles"].append(search_role)
        if (
            config.JSEARCH_STOP_ON_LOW_QUOTA
            and config.JSEARCH_MIN_REMAINING_REQUESTS > 0
            and isinstance(remaining, int)
            and remaining <= config.JSEARCH_MIN_REMAINING_REQUESTS
        ):
            stats["topup_stop_reason"] = (
                f"quota_remaining={remaining} <= JSEARCH_MIN_REMAINING_REQUESTS="
                f"{config.JSEARCH_MIN_REMAINING_REQUESTS}"
            )
            break
        time.sleep(config.SEARCH_DELAY_SECONDS)

    if not stats["topup_stop_reason"]:
        if int(stats["estimated_request_units"]) >= unit_budget:
            stats["topup_stop_reason"] = "topup_unit_budget_exhausted"
        elif not stats["queries_attempted"]:
            stats["topup_stop_reason"] = "no_unused_query_windows"
        else:
            stats["topup_stop_reason"] = "topup_role_plan_exhausted"

    selected_jobs = _select_best_jobs(candidates_by_job_id, stats)
    attempted = int(stats["queries_attempted"])
    allowed_failures = _allowed_role_failures(attempted)
    stats["allowed_role_failures"] = allowed_failures
    stats["role_failure_rate"] = (
        round(len(set(failed_roles)) / attempted, 4) if attempted else 0.0
    )

    saved_at = datetime.now()
    payload = {
        "scrape_date": saved_at.isoformat(),
        "date_posted_window": "reviewable_topup",
        "total_jobs": len(selected_jobs),
        "stats": stats,
        "jobs": selected_jobs,
    }
    serialized = json.dumps(payload, indent=2)
    output_path = str(
        Path(config.OUTPUT_DIR)
        / f"jobs_topup_{saved_at:%Y-%m-%d_%H-%M-%S}_r{round_number}.json"
    )
    Path(output_path).write_text(serialized, encoding="utf-8")
    history_dir = Path(config.OUTPUT_DIR) / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / (
        f"jobs_topup_{saved_at:%Y-%m-%d_%H-%M-%S_%f}_r{round_number}.json"
    )
    history_path.write_text(serialized, encoding="utf-8")
    logger.info("Immutable top-up scrape archive saved to %s", history_path)

    roles_with_results = sum(
        1 for count in stats["selected_role_counts"].values() if count > 0
    )
    failed_count = len(set(failed_roles))
    errors: List[str] = []
    if failed_count > allowed_failures:
        errors.append(
            f"{failed_count} top-up searches failed "
            f"(maximum {allowed_failures} for {attempted} attempted queries)"
        )
    logger.info(
        "Reviewable top-up round %d complete: %d jobs, %d viable additions, "
        "%d units from %d/%d successful queries -> %s",
        round_number,
        len(selected_jobs),
        stats["topup_new_prefilter_viable"],
        stats["estimated_request_units"],
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
        success=not errors,
        errors=errors,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_daily_scrape()
    if config.PRODUCTION and not result.success:
        sys.exit(1)
