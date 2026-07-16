"""Step 1: pull fresh job postings from JSearch and choose the best role match."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import config
from http_utils import request_with_retry, safe_json
from pipeline_state import SeenJobsRegistry
from role_catalog import canonical_role_for_search, role_specificity
from role_relevance import assess_role, normalize_relevance_score

logger = logging.getLogger(__name__)


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


def fetch_jobs_for_role(role: str) -> List[Dict]:
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
    response = request_with_retry(
        "GET",
        config.JSEARCH_ENDPOINT,
        headers=headers,
        params=params,
        timeout=45,
    )
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
    return jobs


def validate_preflight() -> None:
    if not config.RAPIDAPI_KEY:
        raise ValueError("RAPIDAPI_KEY is missing from .env")
    if not config.ROLES:
        raise ValueError("ROLES_JSON/config.ROLES is empty")


def _candidate_key(job: Dict) -> str:
    return str(job.get("job_id") or "").strip()


def run_daily_scrape(registry: Optional[SeenJobsRegistry] = None) -> ScrapeResult:
    """Query every target role, then assign each posting to its strongest role match.

    A job can appear in several searches. The previous implementation kept the
    first role that returned it, which could mislabel a posting. This version
    evaluates every query-role candidate and keeps the highest-scoring match.
    """
    validate_preflight()
    registry = registry or SeenJobsRegistry()

    candidates_by_job_id: Dict[str, List[Dict]] = {}
    failed_roles: List[str] = []
    zero_result_roles: List[str] = []
    raw_role_counts: Dict[str, int] = {}
    canonical_roles = [canonical_role_for_search(role) for role in config.ROLES]
    stats = {
        "raw_role_counts": raw_role_counts,
        "selected_role_counts": {role: 0 for role in dict.fromkeys(canonical_roles)},
        "zero_result_roles": zero_result_roles,
        "excluded_by_seniority": 0,
        "excluded_role_mismatch": 0,
        "ambiguous_role_matches": 0,
        "query_duplicates": 0,
        "previously_seen_removed": 0,
        "missing_job_id_skipped": 0,
    }

    for search_role in config.ROLES:
        canonical_role = canonical_role_for_search(search_role)
        logger.info("[%s] Searching JSearch...", search_role)
        try:
            raw_jobs = fetch_jobs_for_role(search_role)
            logger.info("[%s] Fetched %d raw postings", search_role, len(raw_jobs))
        except Exception as exc:
            logger.exception("[%s] Search failed: %s", search_role, exc)
            raw_jobs = []
            failed_roles.append(search_role)

        raw_role_counts[search_role] = len(raw_jobs)
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
        selected_jobs.append(best)

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
    if len(set(failed_roles)) > config.MAX_ROLE_FAILURES:
        errors.append(
            f"{len(set(failed_roles))} role searches failed "
            f"(maximum {config.MAX_ROLE_FAILURES})"
        )

    success = not errors or not config.PRODUCTION
    logger.info("Scrape complete: %d jobs -> %s", len(selected_jobs), output_path)
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
