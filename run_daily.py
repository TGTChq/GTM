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
    enriched = run_hiring_manager_identification(filtered.output_path)
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
        "excluded": enriched.company_criteria_excluded,
        "stats": enriched.stats,
        "output": enriched.output_path,
        "errors": enriched.errors,
    }
    if config.PRODUCTION and not enriched.success:
        return _fail(summary, "hiring_manager", enriched.errors)

    logger.info("=== STEP 4: AIRTABLE REVIEW QUEUE ===")
    enriched_payload = json.loads(Path(enriched.output_path).read_text(encoding="utf-8"))
    airtable_result = airtable_client.push_leads(enriched_payload.get("jobs", []))
    summary["steps"]["airtable"] = airtable_result
    if airtable_result["failed"]:
        return _fail(
            summary,
            "airtable",
            [f"{airtable_result['failed']} Airtable records failed to persist"],
        )

    # Commit seen-state only after the downstream review queue is safely updated.
    filtered_payload = json.loads(Path(filtered.output_path).read_text(encoding="utf-8"))
    registry.mark_jobs(filtered_payload.get("jobs", []))

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
