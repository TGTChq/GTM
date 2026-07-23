"""Free multi-source acquisition entry point.

Global remote-job feeds provide broad discovery. Automatically discovered public
ATS boards provide first-party inventory. All rows are normalized, classified,
deduplicated, and then passed through the unchanged downstream safety gates.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlparse

import config
from ats_board_registry import AtsBoardRegistry, detect_board_ref, fetch_board_jobs
from free_job_sources import build_adapters, default_fetcher, provider_domain
from job_filter import assess_pre_enrichment_viability, dedup_key, get_safe_employer_domain
from job_quality import normalize_job_identity
from company_identity import company_names_compatible, normalize_company_name
from jsearch_scraper import ScrapeResult, is_excluded_title
from pipeline_state import SeenJobsRegistry
from role_catalog import DEFAULT_SEARCH_ROLES, role_specificity
from role_relevance import assess_role, normalize_relevance_score

logger = logging.getLogger(__name__)


def _normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _best_role(job: Mapping[str, Any]) -> Tuple[str, Any]:
    status_rank = {"accept": 2, "review": 1, "reject": 0}
    candidates = [(role, assess_role(dict(job), role)) for role in DEFAULT_SEARCH_ROLES]
    return max(
        candidates,
        key=lambda item: (
            status_rank.get(item[1].status, 0),
            item[1].score,
            role_specificity(item[0]),
        ),
    )


def _classify(job: Dict[str, Any]) -> Dict[str, Any]:
    normalize_job_identity(job)
    matched_role, assessment = _best_role(job)
    job["_matched_role"] = matched_role
    job["_search_role"] = str(job.get("_acquisition_source") or job.get("job_publisher") or "multi_source")
    job["_role_specificity"] = role_specificity(matched_role)
    job["_role_relevance_status"] = assessment.status
    job["_role_relevance_points"] = assessment.score
    job["_role_relevance_score"] = normalize_relevance_score(assessment.score)
    job["_role_relevance_reasons"] = assessment.reasons
    return job


def _is_public_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().strip(".")
    if host in {"localhost", "0.0.0.0", "::1"} or host.startswith("127."):
        return False
    if re.match(r"^(?:10\.|192\.168\.|169\.254\.|172\.(?:1[6-9]|2\d|3[01])\.)", host):
        return False
    return True


def _extract_links(html_text: str, base_url: str) -> List[str]:
    links: List[str] = []
    for match in re.finditer(r"(?:href|url|applicationUrl|sameAs)\s*[=:]\s*[\"']([^\"']+)[\"']", html_text, re.I):
        candidate = urljoin(base_url, match.group(1).replace("\\/", "/"))
        if _is_public_http_url(candidate) and candidate not in links:
            links.append(candidate)
    for match in re.finditer(r"https?://[^\s<>\"')]+", html_text, re.I):
        candidate = match.group(0).replace("\\/", "/").rstrip(".,;:")
        if _is_public_http_url(candidate) and candidate not in links:
            links.append(candidate)
    return links[:200]


def _company_website_candidate(url: str, company: str, provider_host: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host or host == provider_host or host.endswith("." + provider_host):
        return False
    if detect_board_ref(url):
        return False
    blocked = (
        "linkedin.com", "indeed.com", "glassdoor.com", "google.com", "facebook.com",
        "twitter.com", "x.com", "youtube.com", "instagram.com", "jobright.ai",
    )
    if any(host == item or host.endswith("." + item) for item in blocked):
        return False
    domain_brand = re.sub(r"[-_]+", " ", host.split(".")[0]).strip()
    # Substring matching can poison employer identity (for example Meta vs
    # metabase.com). Reuse the repository's conservative organization matcher.
    return company_names_compatible(company, domain_brand)


def _discover_landing_links(
    jobs: List[Dict[str, Any]],
    *,
    fetcher=default_fetcher,
) -> Dict[str, int]:
    if not config.FREE_SOURCE_LANDING_DISCOVERY_ENABLED:
        return {"attempted": 0, "succeeded": 0, "ats_links": 0, "company_websites": 0}
    attempted = succeeded = ats_links = websites = 0
    max_requests = max(0, config.FREE_SOURCE_LANDING_DISCOVERY_MAX_REQUESTS)
    candidates = []
    for job in jobs:
        matched_role, assessment = _best_role(job)
        if assessment.status not in {"accept", "review"}:
            continue
        url = str(job.get("job_apply_link") or "").strip()
        if not _is_public_http_url(url):
            continue
        candidates.append((assessment.score, len(str(job.get("job_description") or "")), matched_role, job))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    for _score, _description_len, matched_role, job in candidates[:max_requests]:
        url = str(job.get("job_apply_link") or "")
        payload = fetcher(url, headers={"Accept": "text/html,application/xhtml+xml"})
        attempted += 1
        if payload.status_code != 200 or not payload.text:
            continue
        succeeded += 1
        provider_host = provider_domain(str(job.get("_acquisition_source") or ""))
        found = _extract_links(payload.text, payload.url or url)
        options = list(job.get("apply_options") or [])
        existing = {str(item.get("apply_link") or "") for item in options if isinstance(item, dict)}
        for candidate in found:
            ref = detect_board_ref(candidate)
            if ref:
                if candidate not in existing:
                    options.append({"publisher": ref.provider.title(), "apply_link": candidate, "is_direct": True})
                    existing.add(candidate)
                ats_links += 1
                if not job.get("official_job_url"):
                    job["official_job_url"] = candidate
                continue
            if not job.get("employer_website") and _company_website_candidate(
                candidate, str(job.get("employer_name") or ""), provider_host
            ):
                parsed = urlparse(candidate)
                job["employer_website"] = f"{parsed.scheme}://{parsed.netloc}/"
                websites += 1
        job["apply_options"] = options
        job["_landing_discovery_attempted"] = True
        job["_landing_discovery_role"] = matched_role
    return {"attempted": attempted, "succeeded": succeeded, "ats_links": ats_links, "company_websites": websites}


def _propagation_company_key(value: Any) -> str:
    # Collapse only legal suffix variants (Acme vs Acme Corporation). Avoid
    # fuzzy grouping so unrelated short brands cannot share a domain.
    return _normalized(normalize_company_name(str(value or "")))


def _propagate_company_websites(jobs: List[Dict[str, Any]]) -> int:
    domains_by_company: Dict[str, set[str]] = {}
    website_by_domain: Dict[str, str] = {}
    for job in jobs:
        company_key = _propagation_company_key(job.get("employer_name"))
        if len(company_key) < 4:
            continue
        domain, _source = get_safe_employer_domain(job)
        if not domain:
            continue
        domains_by_company.setdefault(company_key, set()).add(domain)
        website_by_domain.setdefault(domain, str(job.get("employer_website") or f"https://{domain}"))

    propagated = 0
    for job in jobs:
        if job.get("employer_website"):
            continue
        company_key = _propagation_company_key(job.get("employer_name"))
        domains = domains_by_company.get(company_key, set())
        if len(domains) != 1:
            continue
        domain = next(iter(domains))
        job["employer_website"] = website_by_domain[domain]
        job["_employer_website_propagated"] = True
        propagated += 1
    return propagated


def _merge_options(target: Dict[str, Any], source: Mapping[str, Any]) -> None:
    options = list(target.get("apply_options") or [])
    known = {str(item.get("apply_link") or "") for item in options if isinstance(item, dict)}
    for item in source.get("apply_options") or []:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("apply_link") or "")
        if url and url not in known:
            options.append(dict(item))
            known.add(url)
    target["apply_options"] = options


def _strength(job: Mapping[str, Any]) -> Tuple[int, int, int, int]:
    return (
        1 if str(job.get("_acquisition_source") or "").startswith("ats_") else 0,
        1 if job.get("job_apply_is_direct") else 0,
        1 if job.get("employer_website") else 0,
        len(str(job.get("job_description") or "")),
    )


def _company_identity_values(job: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    company = re.sub(
        r"\b(?:incorporated|corporation|company|limited|holdings?|group|llc|inc|ltd|corp|co)\b",
        " ",
        str(job.get("employer_name") or "").lower(),
    )
    company_key = _normalized(company)
    if len(company_key) >= 4:
        values.add(company_key)
    domain = get_safe_employer_domain(dict(job))[0]
    if domain:
        root = domain.split(".")[0]
        root_key = _normalized(root)
        if len(root_key) >= 4:
            values.add(root_key)
        values.add(_normalized(domain))
    return {value for value in values if value}


def _identity_keys(job: Mapping[str, Any]) -> set[Tuple[str, str]]:
    _company, title = dedup_key(dict(job))
    if not title:
        return set()
    return {(value, title) for value in _company_identity_values(job)}


def _dedupe(jobs: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    records: List[Dict[str, Any]] = []
    key_to_index: Dict[Tuple[str, str], int] = {}
    fallback_ids: Dict[str, int] = {}
    duplicates = 0
    for job in jobs:
        keys = _identity_keys(job)
        sources = set(job.get("_discovery_sources") or [])
        sources.add(str(job.get("_acquisition_source") or job.get("job_publisher") or "unknown"))
        job["_discovery_sources"] = sorted(source for source in sources if source)

        matching_indexes = {key_to_index[key] for key in keys if key in key_to_index}
        if matching_indexes:
            index = min(matching_indexes)
            current = records[index]
            duplicates += 1
            if _strength(job) > _strength(current):
                _merge_options(job, current)
                job["_discovery_sources"] = sorted(
                    set(job.get("_discovery_sources") or [])
                    | set(current.get("_discovery_sources") or [])
                )
                records[index] = job
                current = job
            else:
                _merge_options(current, job)
                current["_discovery_sources"] = sorted(
                    set(current.get("_discovery_sources") or [])
                    | set(job.get("_discovery_sources") or [])
                )
            for key in keys | _identity_keys(current):
                key_to_index[key] = index
            continue

        if keys:
            index = len(records)
            records.append(job)
            for key in keys:
                key_to_index[key] = index
            continue

        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        if job_id in fallback_ids:
            duplicates += 1
            continue
        fallback_ids[job_id] = len(records)
        records.append(job)
    return records, duplicates


def _source_outcomes(jobs: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    outcomes: Dict[str, Dict[str, int]] = {}
    for job in jobs:
        primary = str(job.get("_acquisition_source") or job.get("job_publisher") or "unknown")
        sources = {str(value) for value in (job.get("_discovery_sources") or []) if value}
        sources.add(primary)
        for source in sources:
            metric = outcomes.setdefault(source, {
                "selected_any_provenance": 0,
                "selected_as_primary": 0,
                "role_accept": 0,
                "role_review": 0,
                "prefilter_viable": 0,
            })
            metric["selected_any_provenance"] += 1
            if source == primary:
                metric["selected_as_primary"] += 1
            status = str(job.get("_role_relevance_status") or "")
            if status in {"accept", "review"}:
                metric[f"role_{status}"] += 1
            if job.get("_prefilter_viable") and status in {"accept", "review"}:
                metric["prefilter_viable"] += 1
    return dict(sorted(outcomes.items()))


def _save_raw(jobs: List[Dict[str, Any]], stats: Mapping[str, Any]) -> str:
    output_dir = Path(config.OUTPUT_DIR)
    history_dir = output_dir / "history"
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    payload = {
        "scrape_date": now.isoformat(),
        "acquisition_mode": "free_multi_source",
        "total_jobs": len(jobs),
        "stats": dict(stats),
        "jobs": jobs,
    }
    daily = output_dir / f"jobs_{now:%Y-%m-%d}.json"
    archive = history_dir / f"jobs_multisource_{now:%Y-%m-%d_%H-%M-%S_%f}.json"
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    daily.write_text(text, encoding="utf-8")
    archive.write_text(text, encoding="utf-8")
    logger.info("Immutable multi-source raw archive saved to %s", archive)
    return str(daily)


def run_multi_source_acquisition(
    registry: Optional[SeenJobsRegistry] = None,
    *,
    fetcher=default_fetcher,
    force_ats_refresh: bool = False,
    ats_board_limit: Optional[int] = None,
) -> ScrapeResult:
    registry = registry or SeenJobsRegistry()
    board_registry = AtsBoardRegistry()
    history_seed = board_registry.seed_from_history() if config.ATS_REGISTRY_AUTO_SEED_HISTORY else {
        "files_scanned": 0, "jobs_scanned": 0, "boards_added_or_updated": 0
    }

    all_jobs: List[Dict[str, Any]] = []
    source_metrics: Dict[str, Dict[str, Any]] = {}
    failed_sources: List[str] = []
    for adapter in build_adapters(config.FREE_JOB_SOURCES):
        source_result = adapter.fetch(fetcher)
        all_jobs.extend(source_result.jobs)
        source_metrics[source_result.source] = {
            "success": source_result.success,
            "requests_attempted": source_result.requests_attempted,
            "requests_succeeded": source_result.requests_succeeded,
            "pages": source_result.pages,
            "raw_records": source_result.raw_records,
            "normalized_jobs": len(source_result.jobs),
            "errors": list(source_result.errors),
            "metadata": dict(source_result.metadata),
        }
        if not source_result.success:
            failed_sources.append(source_result.source)
        logger.info(
            "[%s] requests=%d/%d raw=%d normalized=%d errors=%s",
            source_result.source,
            source_result.requests_succeeded,
            source_result.requests_attempted,
            source_result.raw_records,
            len(source_result.jobs),
            source_result.errors,
        )

    landing_metrics = _discover_landing_links(all_jobs, fetcher=fetcher)
    propagated_before_ats = _propagate_company_websites(all_jobs)
    boards_from_feeds = board_registry.upsert_from_jobs(all_jobs)

    ats_jobs: List[Dict[str, Any]] = []
    ats_metrics: Dict[str, Dict[str, int]] = {}
    greenhouse_detail_remaining = max(
        0, int(getattr(config, "ATS_GREENHOUSE_DETAIL_MAX_REQUESTS_PER_RUN", 100))
    )
    if config.ATS_DIRECT_ACQUISITION_ENABLED:
        for board in board_registry.due_entries(limit=ats_board_limit, force=force_ats_refresh):
            provider = str(board.get("provider") or "unknown")
            jobs, error = fetch_board_jobs(
                board,
                fetcher,
                greenhouse_detail_budget=(
                    greenhouse_detail_remaining if provider == "greenhouse" else None
                ),
            )
            detail_requests = sum(
                1 for job in jobs if job.get("_greenhouse_detail_request_made")
            )
            greenhouse_detail_remaining = max(0, greenhouse_detail_remaining - detail_requests)
            metric = ats_metrics.setdefault(provider, {
                "boards_attempted": 0,
                "boards_succeeded": 0,
                "jobs": 0,
                "errors": 0,
                "detail_requests": 0,
            })
            metric["detail_requests"] += detail_requests
            metric["boards_attempted"] += 1
            if error:
                metric["errors"] += 1
                board_registry.record_result(
                    str(board.get("key") or ""), success=False, error=error, save=False
                )
                logger.warning("ATS board %s failed: %s", board.get("key"), error)
                continue
            metric["boards_succeeded"] += 1
            metric["jobs"] += len(jobs)
            ats_jobs.extend(jobs)
            board_registry.record_result(
                str(board.get("key") or ""), success=True, job_count=len(jobs), save=False
            )
        if ats_jobs:
            # Feed official ATS identity back into the registry so a weak or stale
            # discovery label can be upgraded without manual maintenance.
            board_registry.upsert_from_jobs(ats_jobs, save=False)
        if ats_metrics:
            board_registry.save()
    all_jobs.extend(ats_jobs)
    propagated_after_ats = _propagate_company_websites(all_jobs)
    propagated_websites = propagated_before_ats + propagated_after_ats

    normalized: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {
        "acquisition_mode": "free_multi_source",
        "enabled_sources": list(config.FREE_JOB_SOURCES),
        "source_metrics": source_metrics,
        "ats_metrics": ats_metrics,
        "ats_force_refresh": bool(force_ats_refresh),
        "ats_board_limit": ats_board_limit,
        "history_registry_seed": history_seed,
        "landing_discovery": landing_metrics,
        "company_websites_propagated": propagated_websites,
        "boards_discovered_from_current_feeds": boards_from_feeds,
        "boards_total": len(board_registry.entries),
        "raw_records_total": len(all_jobs),
        "excluded_by_seniority": 0,
        "previously_seen_removed": 0,
        "missing_job_id_skipped": 0,
        "cross_source_duplicates_removed": 0,
        "prefilter_viable": 0,
        "prefilter_rejected": 0,
        "role_accept": 0,
        "role_review": 0,
        "role_reject": 0,
        "query_metrics": {},
        "query_variant_metrics": {},
        "base_estimated_request_units": 0,
        "estimated_request_units": 0,
        "adaptive_extra_queries": 0,
        "adaptive_prefilter_viable_added": 0,
        "adaptive_lookback_queries": 0,
        "adaptive_lookback_prefilter_viable_added": 0,
        "adaptive_bucket_counts": {},
        "adaptive_lookback_variant_counts": {},
    }

    for original in all_jobs:
        job = dict(original)
        if not str(job.get("job_id") or "").strip():
            stats["missing_job_id_skipped"] += 1
            continue
        if registry.has_job_id(str(job.get("job_id"))):
            stats["previously_seen_removed"] += 1
            continue
        if is_excluded_title(str(job.get("job_title") or "")):
            stats["excluded_by_seniority"] += 1
            continue
        _classify(job)
        status = str(job.get("_role_relevance_status") or "reject")
        stats[f"role_{status}"] = int(stats.get(f"role_{status}", 0)) + 1
        assessment = assess_pre_enrichment_viability(job)
        job["_prefilter_viable"] = assessment.eligible
        job["_prefilter_stat"] = assessment.stat_name
        job["_prefilter_reason"] = assessment.reason
        if assessment.eligible and status in {"accept", "review"}:
            stats["prefilter_viable"] += 1
        elif status in {"accept", "review"}:
            stats["prefilter_rejected"] += 1
        normalized.append(job)

    selected, duplicate_count = _dedupe(normalized)
    stats["cross_source_duplicates_removed"] = duplicate_count
    stats["selected_jobs"] = len(selected)
    stats["source_outcomes"] = _source_outcomes(selected)
    roles = {str(job.get("_matched_role") or "") for job in selected if job.get("_matched_role")}
    successful_sources = sum(1 for metric in source_metrics.values() if metric.get("success"))
    direct_boards_succeeded = sum(metric.get("boards_succeeded", 0) for metric in ats_metrics.values())
    minimum_sources = max(1, config.FREE_SOURCE_MIN_SUCCESSFUL_SOURCES)
    errors: List[str] = []
    if successful_sources < minimum_sources and direct_boards_succeeded <= 0:
        errors.append(
            f"Only {successful_sources} free source(s) succeeded; minimum is {minimum_sources}"
        )
    if config.PRODUCTION and len(selected) < config.MIN_JOBS_PER_RUN:
        errors.append(
            f"Only {len(selected)} jobs acquired; production minimum is {config.MIN_JOBS_PER_RUN}"
        )
    output = _save_raw(selected, stats)
    logger.info(
        "Multi-source acquisition complete: %d selected jobs from %d global records + %d ATS jobs; "
        "sources=%d/%d boards=%d",
        len(selected), len(all_jobs) - len(ats_jobs), len(ats_jobs), successful_sources,
        len(source_metrics), len(board_registry.entries),
    )
    return ScrapeResult(
        output_path=output,
        total_jobs=len(selected),
        stats=stats,
        failed_roles=failed_sources,
        roles_with_results=len(roles),
        success=not errors,
        errors=errors,
    )
