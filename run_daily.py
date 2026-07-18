"""Production entry point for Steps 1-4.

Runs scrape -> filter -> audit -> hiring-manager enrichment -> Airtable review
queue. Instantly enrollment remains a separate approval-driven process handled
by run_approved.py (or an n8n schedule calling it every minute).
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

import airtable_client
import config
from audit_filter import run_audit
from hiring_manager import run_hiring_manager_identification
from jsearch_scraper import run_daily_scrape
from job_filter import run_filter
from pipeline_state import SeenJobsRegistry

Path(config.LOG_DIR).mkdir(parents=True, exist_ok=True)
Path(config.RUN_SUMMARY_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(Path(config.LOG_DIR) / f"pipeline_{datetime.now():%Y-%m-%d}.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def save_run_summary(summary: dict) -> str:
    path = Path(config.RUN_SUMMARY_DIR) / f"run_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return str(path)


def _fail(summary: dict, step: str, errors: list[str]) -> dict:
    summary["failed_at"] = step
    summary["errors"] = errors
    summary["success"] = False
    summary["finished_at"] = datetime.now().isoformat()
    return summary


def run_pipeline() -> dict:
    started = datetime.now()
    registry = SeenJobsRegistry()
    summary = {
        "started_at": started.isoformat(),
        "production_mode": config.PRODUCTION,
        "date_posted": config.DATE_POSTED,
        "steps": {},
        "success": False,
    }

    logger.info("=== STEP 1: SCRAPE ===")
    scrape = run_daily_scrape(registry=registry)
    summary["steps"]["scrape"] = {
        "success": scrape.success,
        "total_jobs": scrape.total_jobs,
        "roles_with_results": scrape.roles_with_results,
        "failed_roles": scrape.failed_roles,
        "stats": scrape.stats,
        "output": scrape.output_path,
        "errors": scrape.errors,
    }
    if config.PRODUCTION and not scrape.success:
        return _fail(summary, "scrape", scrape.errors)
    logger.info(
        "JSearch strategy: remote_only=%s remote_query_bias=%s base_units=%d "
        "adaptive_queries=%d adaptive_prefilter_viable_added=%d buckets=%s",
        config.JSEARCH_REMOTE_JOBS_ONLY,
        config.JSEARCH_REMOTE_QUERY_BIAS,
        scrape.stats.get("base_estimated_request_units", 0),
        scrape.stats.get("adaptive_extra_queries", 0),
        scrape.stats.get("adaptive_prefilter_viable_added", 0),
        scrape.stats.get("adaptive_bucket_counts", {}),
    )

    logger.info("=== STEP 2: FILTER ===")
    filtered = run_filter(input_path=scrape.output_path, registry=registry)
    summary["steps"]["filter"] = {
        "success": filtered.success,
        "kept": filtered.kept_count,
        "rejected": filtered.rejected_count,
        "stats": filtered.stats,
        "output": filtered.output_path,
        "rejected_output": filtered.rejected_path,
        "errors": filtered.errors,
    }
    logger.info(
        "Filter funnel: input=%d kept=%d rejected=%d | aggregator=%d staffing=%d "
        "industry=%d in_person=%d non_active=%d non_full_time=%d non_us=%d "
        "crm=%d duplicate=%d previously_seen=%d",
        filtered.stats.get("input_total", 0),
        filtered.kept_count,
        filtered.rejected_count,
        filtered.stats.get("excluded_aggregator", 0),
        filtered.stats.get("excluded_staffing", 0),
        filtered.stats.get("excluded_industry", 0),
        filtered.stats.get("excluded_in_person", 0),
        filtered.stats.get("excluded_non_active", 0),
        filtered.stats.get("excluded_non_full_time", 0),
        filtered.stats.get("excluded_non_us", 0),
        filtered.stats.get("excluded_crm", 0),
        filtered.stats.get("excluded_duplicate", 0),
        filtered.stats.get("excluded_previously_seen", 0),
    )
    if config.PRODUCTION and not filtered.success:
        return _fail(summary, "filter", filtered.errors)

    logger.info("=== STEP 2B: AUDIT ===")
    audit = run_audit(filtered.output_path, filtered.rejected_path, scrape.output_path)
    summary["steps"]["audit"] = {
        "passed": audit.passed,
        "summary": audit.summary,
        "report": audit.report_path,
        "warnings": audit.warnings,
        "failures": audit.failures,
    }
    if config.PRODUCTION and not audit.passed:
        return _fail(summary, "audit", audit.failures)

    logger.info("=== STEP 3: HIRING MANAGER ===")
    logger.info(
        "Daily throughput: target=%d reviewable leads, safety cap=%d eligible companies",
        config.TARGET_REVIEWABLE_LEADS_PER_RUN,
        config.MAX_ELIGIBLE_COMPANIES_PER_RUN,
    )
    enriched = run_hiring_manager_identification(
        filtered.output_path,
        target_reviewable_leads=config.TARGET_REVIEWABLE_LEADS_PER_RUN,
        max_eligible_companies=config.MAX_ELIGIBLE_COMPANIES_PER_RUN,
    )
    summary["steps"]["hiring_manager"] = {
        "success": enriched.success,
        "input_jobs": enriched.total_input_jobs,
        "output_leads": enriched.total_output_leads,
        "hiring_managers_identified": enriched.hiring_manager_found,
        "hiring_managers_not_identified": enriched.hiring_manager_not_found,
        "identification_rate": enriched.match_rate,
        "contactable_hiring_managers": enriched.contactable_hiring_managers,
        "uncontactable_hiring_managers": enriched.uncontactable_hiring_managers,
        "contactable_rate": enriched.contactable_rate,
        "target_reviewable_leads": enriched.target_reviewable_leads,
        "reviewable_leads": enriched.reviewable_leads,
        "reviewable_target_reached": enriched.reviewable_target_reached,
        "max_eligible_companies": enriched.max_eligible_companies,
        "eligible_company_limit_reached": enriched.eligible_company_limit_reached,
        "companies_considered": enriched.companies_considered,
        "eligible_companies": enriched.eligible_companies,
        "stop_reason": enriched.stop_reason,
        "excluded": enriched.company_criteria_excluded,
        "stats": enriched.stats,
        "output": enriched.output_path,
        "errors": enriched.errors,
    }
    logger.info(
        "Hiring-manager funnel: companies_considered=%d eligible=%d reviewable=%d/%d "
        "identified=%d contactable=%d | no_manager=%d no_email=%d invalid_email=%d "
        "org_domain_mismatch=%d email_domain_mismatch=%d founder_disallowed=%d "
        "person_match_attempts=%d",
        enriched.companies_considered,
        enriched.eligible_companies,
        enriched.reviewable_leads,
        enriched.target_reviewable_leads or 0,
        enriched.hiring_manager_found,
        enriched.contactable_hiring_managers,
        enriched.stats.get("no_matching_hiring_manager", 0),
        enriched.stats.get("candidate_no_usable_email", 0),
        enriched.stats.get("candidate_email_invalid", 0),
        enriched.stats.get("candidate_organization_domain_mismatch", 0),
        enriched.stats.get("candidate_email_domain_mismatch", 0),
        enriched.stats.get("candidate_founder_fallback_disallowed", 0),
        enriched.stats.get("person_match_attempts", 0),
    )
    logger.info(
        "Hiring-manager selection tiers: direct=%d functional_exec=%d founder_fallback=%d",
        enriched.stats.get("selection_tier_direct_functional_leader", 0),
        enriched.stats.get("selection_tier_functional_executive", 0),
        enriched.stats.get("selection_tier_founder_fallback", 0),
    )
    if config.PRODUCTION and not enriched.success:
        return _fail(summary, "hiring_manager", enriched.errors)
    if not enriched.reviewable_target_reached:
        logger.warning(
            "Daily target not reached: %d/%d reviewable leads. Stop reason: %s",
            enriched.reviewable_leads,
            enriched.target_reviewable_leads or 0,
            enriched.stop_reason,
        )

    logger.info("=== STEP 4: AIRTABLE REVIEW QUEUE ===")
    enriched_payload = json.loads(Path(enriched.output_path).read_text(encoding="utf-8"))
    airtable_result = airtable_client.push_leads(enriched_payload.get("jobs", []))
    summary["steps"]["airtable"] = airtable_result
    logger.info(
        "Airtable result: reviewable=%d created=%d skipped_existing=%d "
        "skipped_existing_company=%d failed=%d",
        airtable_result.get("reviewable", 0),
        airtable_result.get("created", 0),
        airtable_result.get("skipped_existing", 0),
        airtable_result.get("skipped_existing_company", 0),
        airtable_result.get("failed", 0),
    )
    if airtable_result["failed"]:
        return _fail(
            summary,
            "airtable",
            [f"{airtable_result['failed']} Airtable records failed to persist"],
        )

    # Commit seen-state only after the downstream review queue is safely updated.
    # When the daily throughput target/cap stops enrichment early, mark only the
    # jobs that were actually processed. Unprocessed jobs remain eligible for a
    # later run instead of disappearing from the queue.
    processed_job_refs = enriched_payload.get("processed_job_refs", [])
    if not processed_job_refs:
        logger.warning(
            "Step 3 output did not include processed_job_refs; falling back to "
            "the full filtered set for backward compatibility"
        )
        filtered_payload = json.loads(Path(filtered.output_path).read_text(encoding="utf-8"))
        processed_job_refs = filtered_payload.get("jobs", [])
    registry.mark_jobs(processed_job_refs)

    summary["success"] = True
    summary["finished_at"] = datetime.now().isoformat()
    summary["duration_seconds"] = round((datetime.now() - started).total_seconds(), 2)
    summary["registry_total_tracked"] = registry.total_tracked
    return summary


def main() -> int:
    try:
        summary = run_pipeline()
    except Exception:
        trace = traceback.format_exc()
        logger.error("Pipeline crashed:\n%s", trace)
        summary = {
            "success": False,
            "failed_at": "crash",
            "errors": [trace],
            "finished_at": datetime.now().isoformat(),
        }

    summary_path = save_run_summary(summary)
    logger.info("Run summary: %s", summary_path)
    if summary.get("success"):
        logger.info("Pipeline completed successfully")
        return 0
    logger.error("Pipeline failed at %s: %s", summary.get("failed_at"), summary.get("errors"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
