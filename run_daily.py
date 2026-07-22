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
from reviewable_topup import _merge_query_metrics, run_reviewable_topup
from final_pass_topup import run_final_pass_topup
from qualification_pipeline import run_precontact_qualification
from job_filter import dedup_key, run_filter
from pipeline_state import SeenJobsRegistry
from pipeline_checkpoint import PipelineCheckpoint
from observability import build_observability_report, save_observability_report
from recovery_inventory import FinalPassInventory, RecoverableJobQueue

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



def _merge_recovery_jobs(scrape, recovery_jobs: list[dict]):
    if not recovery_jobs:
        return scrape
    path = Path(scrape.output_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    jobs = list(payload.get("jobs", []))
    seen = {
        (str(job.get("job_id") or ""), dedup_key(job))
        for job in jobs
    }
    added = 0
    for job in recovery_jobs:
        marker = (str(job.get("job_id") or ""), dedup_key(job))
        if marker in seen:
            continue
        jobs.append(job)
        seen.add(marker)
        added += 1
    payload["jobs"] = jobs
    payload["total_jobs"] = len(jobs)
    payload.setdefault("stats", {})["recoverable_jobs_reinjected"] = added
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    scrape.total_jobs = len(jobs)
    scrape.stats["recoverable_jobs_reinjected"] = added
    return scrape


def _lead_is_retryable(lead: dict) -> bool:
    if str(lead.get("_final_state") or "") in {"REROUTE", "UNVERIFIED", "NEEDS_CHECK"}:
        return True
    return any(
        bool((decision or {}).get("retryable"))
        for decision in (lead.get("_gate_decisions") or {}).values()
        if isinstance(decision, dict)
    )


def run_pipeline() -> dict:
    started = datetime.now()
    registry = SeenJobsRegistry()
    recovery_queue = RecoverableJobQueue()
    final_pass_inventory = FinalPassInventory()
    checkpoint = PipelineCheckpoint()
    summary = {
        "started_at": started.isoformat(),
        "production_mode": config.PRODUCTION,
        "date_posted": config.DATE_POSTED,
        "steps": {},
        "success": False,
        "technical_success": False,
        "sla_success": False,
    }

    logger.info("=== STEP 1: SCRAPE ===")
    topup_enabled = bool(
        (
            config.FINAL_PASS_TOPUP_ENABLED
            if config.FINAL_PASS_PIPELINE_ENABLED
            else config.JSEARCH_REVIEWABLE_TOPUP_ENABLED
            and config.JSEARCH_TOPUP_MAX_ROUNDS > 0
        )
        and config.get_final_pass_target() > 0
    )
    scrape = run_daily_scrape(
        registry=registry,
        base_num_pages=(
            config.JSEARCH_TOPUP_INITIAL_PAGES if topup_enabled else None
        ),
        # Closed-loop mode reserves the post-filter budget for queries selected
        # using actual Apollo contactability instead of spending it blindly.
        allow_adaptive=False if topup_enabled else None,
    )
    due_recovery_jobs = recovery_queue.due_jobs()
    checkpoint_jobs = checkpoint.pending_jobs()
    checkpoint_metrics = checkpoint.query_metrics()
    if checkpoint_metrics:
        scrape.stats["query_metrics"] = _merge_query_metrics(
            checkpoint_metrics,
            scrape.stats.get("query_metrics", {}),
        )
        scrape.stats["resumed_query_metric_roles"] = len(checkpoint_metrics)
    scrape = _merge_recovery_jobs(scrape, [*checkpoint_jobs, *due_recovery_jobs])
    initial_raw_payload = json.loads(Path(scrape.output_path).read_text(encoding="utf-8"))
    checkpoint.append_jobs(
        initial_raw_payload.get("jobs", []),
        query_metrics=scrape.stats.get("query_metrics", {}),
    )
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
        "JSearch strategy: remote_only=%s remote_filter=%s remote_query_bias=%s "
        "base_units=%d adaptive_queries=%d adaptive_viable_added=%d "
        "lookback_queries=%d lookback_viable_added=%d estimated_units=%d buckets=%s",
        config.JSEARCH_REMOTE_JOBS_ONLY,
        config.JSEARCH_REMOTE_FILTER_PARAMETER,
        config.JSEARCH_REMOTE_QUERY_BIAS,
        scrape.stats.get("base_estimated_request_units", 0),
        scrape.stats.get("adaptive_extra_queries", 0),
        scrape.stats.get("adaptive_prefilter_viable_added", 0),
        scrape.stats.get("adaptive_lookback_queries", 0),
        scrape.stats.get("adaptive_lookback_prefilter_viable_added", 0),
        scrape.stats.get("estimated_request_units", 0),
        scrape.stats.get("adaptive_bucket_counts", {}),
    )
    logger.info(
        "JSearch query variants: lookback_counts=%s yield=%s",
        scrape.stats.get("adaptive_lookback_variant_counts", {}),
        scrape.stats.get("query_variant_metrics", {}),
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
        "Filter funnel: input=%d kept=%d rejected=%d | integrity=%d restricted=%d "
        "outsourcing=%d contextual=%d aggregator=%d staffing=%d industry=%d "
        "in_person=%d non_active=%d non_full_time=%d non_us=%d crm=%d "
        "duplicate=%d previously_seen=%d seniority=%d stale=%d role_mismatch=%d non_paying=%d",
        filtered.stats.get("input_total", 0),
        filtered.kept_count,
        filtered.rejected_count,
        filtered.stats.get("excluded_posting_integrity", 0),
        filtered.stats.get("excluded_restricted_role", 0),
        filtered.stats.get("excluded_outsourcing", 0),
        filtered.stats.get("excluded_contextual_mismatch", 0),
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
        scrape.stats.get("excluded_by_seniority", 0),
        filtered.stats.get("excluded_stale", 0),
        filtered.stats.get("excluded_role_mismatch", 0),
        filtered.stats.get("excluded_non_paying", 0),
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

    hiring_input_path = filtered.output_path
    strict_runtime = False
    if config.FINAL_PASS_PIPELINE_ENABLED:
        logger.info("=== STEP 2C: JOB + ROLE GATES ===")
        qualified = run_precontact_qualification(filtered.output_path, suffix="initial")
        hiring_input_path = qualified.output_path
        strict_runtime = not bool(qualified.stats.get("compatibility_bypass"))
        summary["steps"]["qualification"] = {
            "success": qualified.success,
            "input_jobs": qualified.input_jobs,
            "contact_eligible_jobs": qualified.contact_eligible_jobs,
            "rejected_jobs": qualified.rejected_jobs,
            "unverified_jobs": qualified.unverified_jobs,
            "needs_check_jobs": qualified.needs_check_jobs,
            "stats": qualified.stats,
            "output": qualified.output_path,
            "nonpass_output": qualified.nonpass_path,
            "errors": qualified.errors,
        }
        if config.PRODUCTION and not qualified.success:
            return _fail(summary, "qualification", qualified.errors)

    logger.info("=== STEP 3: HIRING MANAGER ===")
    target_final_pass = config.get_final_pass_target()
    existing_airtable_company_keys: set[str] = set()
    if config.AIRTABLE_SUPPRESS_EXISTING_COMPANY:
        try:
            existing_airtable_company_keys = (
                airtable_client.get_active_existing_company_keys_for_pipeline()
            )
            logger.info(
                "Pre-excluding %d active Airtable company keys before FINAL_PASS counting",
                len(existing_airtable_company_keys),
            )
        except Exception as exc:
            logger.exception("Could not load existing Airtable companies")
            if config.PRODUCTION:
                return _fail(summary, "airtable_existing_companies", [str(exc)])
    logger.info(
        "Daily throughput: target=%d FINAL_PASS leads, eligible-company cap=%s",
        target_final_pass,
        str(config.MAX_ELIGIBLE_COMPANIES_PER_RUN)
        if config.MAX_ELIGIBLE_COMPANIES_PER_RUN > 0
        else "unlimited",
    )
    enriched = run_hiring_manager_identification(
        hiring_input_path,
        target_final_pass_leads=target_final_pass if strict_runtime else None,
        target_reviewable_leads=None if strict_runtime else target_final_pass,
        max_eligible_companies=(
            config.MAX_ELIGIBLE_COMPANIES_PER_RUN
            if config.MAX_ELIGIBLE_COMPANIES_PER_RUN > 0
            else None
        ),
        exclude_company_keys=existing_airtable_company_keys,
        output_suffix="initial",
    )
    summary["steps"]["topup"] = {
        "enabled": topup_enabled,
        "mode": "final_pass" if strict_runtime else "legacy_reviewable",
        "rounds": [],
        "initial_final_pass_leads": enriched.final_pass_leads,
        "initial_reviewable_leads": enriched.reviewable_leads,
        "target_final_pass_leads": target_final_pass,
    }
    if topup_enabled:
        if strict_runtime:
            enriched, topup_summary = run_final_pass_topup(
                initial_scrape=scrape,
                initial_enriched=enriched,
                registry=registry,
                target_final_pass_leads=target_final_pass,
                max_eligible_companies=config.MAX_ELIGIBLE_COMPANIES_PER_RUN,
                exclude_company_keys=existing_airtable_company_keys,
            )
        else:
            # Tiny test fixtures and rollback-mode payloads retain the legacy
            # top-up path; production JSearch rows always use strict mode.
            enriched, topup_summary = run_reviewable_topup(
                initial_scrape=scrape,
                initial_enriched=enriched,
                registry=registry,
                target_reviewable_leads=target_final_pass,
                max_eligible_companies=config.MAX_ELIGIBLE_COMPANIES_PER_RUN,
            )
        summary["steps"]["topup"] = topup_summary
        logger.info(
            "Top-up final: mode=%s rounds=%d query_units=%d total_query_units=%d "
            "FINAL_PASS=%d/%d review_rows=%d stop_reason=%s",
            topup_summary.get("mode", "legacy_reviewable"),
            len(topup_summary.get("rounds", [])),
            topup_summary.get("topup_query_units", 0),
            topup_summary.get("total_query_units", 0),
            enriched.final_pass_leads,
            target_final_pass,
            enriched.reviewable_leads,
            topup_summary.get("stop_reason", ""),
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
        "final_pass_target": enriched.final_pass_target,
        "final_pass_leads": enriched.final_pass_leads,
        "final_pass_target_reached": enriched.final_pass_target_reached,
        "needs_check_leads": enriched.needs_check_leads,
        "reroute_leads": enriched.reroute_leads,
        "unverified_leads": enriched.unverified_leads,
        "rejected_leads": enriched.rejected_leads,
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
        "Hiring-manager funnel: companies_considered=%d eligible=%d FINAL_PASS=%d/%d review_rows=%d "
        "identified=%d contactable=%d | no_manager=%d no_email=%d invalid_email=%d "
        "org_domain_mismatch=%d email_domain_mismatch=%d founder_disallowed=%d "
        "person_match_attempts=%d",
        enriched.companies_considered,
        enriched.eligible_companies,
        enriched.final_pass_leads,
        enriched.final_pass_target or target_final_pass,
        enriched.reviewable_leads,
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
    company_reason_counts = {
        key.removeprefix("company_criteria_reason__"): value
        for key, value in enriched.stats.items()
        if key.startswith("company_criteria_reason__") and value
    }
    logger.info(
        "Company eligibility diagnostics: reasons=%s unresolved_domain_companies=%d "
        "missing_domain_buckets=%d",
        company_reason_counts,
        enriched.stats.get("company_domain_unresolved", 0),
        enriched.stats.get("missing_company_domain_buckets", 0),
    )
    if config.PRODUCTION and not enriched.success:
        return _fail(summary, "hiring_manager", enriched.errors)
    if strict_runtime and not enriched.final_pass_target_reached:
        logger.warning(
            "Daily target not reached: %d/%d FINAL_PASS leads. Stop reason: %s",
            enriched.final_pass_leads,
            enriched.final_pass_target or target_final_pass,
            enriched.stop_reason,
        )
    elif not strict_runtime and not enriched.reviewable_target_reached:
        logger.warning(
            "Legacy daily target not reached: %d/%d reviewable leads. Stop reason: %s",
            enriched.reviewable_leads,
            enriched.target_reviewable_leads or 0,
            enriched.stop_reason,
        )

    logger.info("=== STEP 4: AIRTABLE REVIEW QUEUE ===")
    enriched_payload = json.loads(Path(enriched.output_path).read_text(encoding="utf-8"))
    enriched_jobs = list(enriched_payload.get("jobs", []))
    recoverable_jobs = [job for job in enriched_jobs if _lead_is_retryable(job)]
    terminal_jobs = [
        job for job in enriched_jobs
        if str(job.get("_final_state") or "") in {"FINAL_PASS", "REJECT"}
    ]
    recovery_queue.upsert(recoverable_jobs)
    recovery_queue.remove(terminal_jobs)
    current_final_pass = [
        job for job in enriched_jobs if str(job.get("_final_state") or "") == "FINAL_PASS"
    ]
    final_pass_inventory.stage(current_final_pass)
    inventory_leads = final_pass_inventory.valid_leads()
    airtable_candidates = list({
        str(job.get("lead_key") or f"{job.get('employer_name')}|{job.get('job_id')}"): job
        for job in [*inventory_leads, *enriched_jobs]
    }.values())
    airtable_result = airtable_client.push_leads(airtable_candidates)
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

    final_pass_inventory.remove(inventory_leads)

    if strict_runtime:
        evidence_report = build_observability_report(
            enriched_payload=enriched_payload,
            topup_summary=summary["steps"].get("topup") or {},
            airtable_result=airtable_result,
        )
        evidence_path = save_observability_report(evidence_report)
        summary["steps"]["observability"] = {**evidence_report, "output": evidence_path}
        logger.info(
            "Final decision: FINAL_PASS=%d/%d deficit=%d NEEDS_CHECK=%d REROUTE=%d "
            "UNVERIFIED=%d REJECT=%d stop_reason=%s evidence=%s",
            evidence_report["final_pass"],
            evidence_report["target_final_pass"],
            evidence_report["deficit_remaining"],
            evidence_report["state_counts"].get("NEEDS_CHECK", 0),
            evidence_report["state_counts"].get("REROUTE", 0),
            evidence_report["state_counts"].get("UNVERIFIED", 0),
            evidence_report["state_counts"].get("REJECT", 0),
            evidence_report.get("stop_reason"),
            evidence_path,
        )

    # Commit seen-state only after the downstream review queue is safely updated.
    # When the daily throughput target/cap stops enrichment early, mark only the
    # jobs that were actually processed. Unprocessed jobs remain eligible for a
    # later run instead of disappearing from the queue.
    # Only terminal outcomes enter seen-state in strict mode. Legacy mode has no
    # final-state annotations, so retain its historical processed-ref behavior.
    processed_job_refs = enriched_payload.get("processed_job_refs", [])
    if strict_runtime:
        terminal_ids = {
            str(job.get("job_id") or job.get("canonical_job_id") or "")
            for job in terminal_jobs
            if job.get("job_id") or job.get("canonical_job_id")
        }
        refs_to_mark = [
            ref for ref in processed_job_refs
            if str(ref.get("job_id") or ref.get("canonical_job_id") or "") in terminal_ids
        ]
    else:
        refs_to_mark = processed_job_refs
    registry.mark_jobs(refs_to_mark)
    # The crash checkpoint is only for work that never reached a downstream
    # disposition. Retryable outcomes already live in RecoverableJobQueue.
    checkpoint.remove_jobs(processed_job_refs)

    net_created = int(airtable_result.get("created", 0))
    upstream_target_reached = bool(
        enriched.final_pass_target_reached if strict_runtime
        else enriched.reviewable_target_reached
    )
    net_target_reached = net_created >= target_final_pass
    sla_success = upstream_target_reached and (
        net_target_reached if config.SLA_REQUIRE_NET_NEW_AIRTABLE else True
    )
    summary["technical_success"] = True
    summary["sla_success"] = sla_success
    summary["sla"] = {
        "target": target_final_pass,
        "upstream_final_pass": enriched.final_pass_leads,
        "upstream_target_reached": upstream_target_reached,
        "net_airtable_created": net_created,
        "net_target_reached": net_target_reached,
        "stop_reason": enriched.stop_reason,
    }
    if not sla_success:
        summary.setdefault("warnings", []).append(
            f"SLA target not reached: upstream={enriched.final_pass_leads}/{target_final_pass}, "
            f"net_airtable_created={net_created}/{target_final_pass}"
        )
        logger.error(summary["warnings"][-1])
    if sla_success:
        checkpoint.clear()
    else:
        summary.setdefault("warnings", []).append(
            "Pipeline checkpoint retained because the daily SLA was not reached"
        )
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
    if summary.get("success") and summary.get("sla_success") is False:
        logger.error("Pipeline completed technically but missed the daily SLA")
        return 2
    if summary.get("success"):
        logger.info("Pipeline completed successfully")
        return 0
    logger.error("Pipeline failed at %s: %s", summary.get("failed_at"), summary.get("errors"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
