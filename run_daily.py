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
from capacity_controller import build_from_config
from company_identity import canonical_company_key
from audit_filter import run_audit
from age_recovery import run_age_recovery
from hiring_manager import run_hiring_manager_identification
from jsearch_scraper import ScrapeResult, run_daily_scrape
from multi_source_acquisition import run_multi_source_acquisition
from reviewable_topup import _merge_query_metrics, run_reviewable_topup
from final_pass_topup import run_final_pass_topup
from qualification_pipeline import run_precontact_qualification
from job_filter import dedup_key, run_filter
from pipeline_state import SeenJobsRegistry
from pipeline_checkpoint import PipelineCheckpoint
from observability import build_observability_report, save_observability_report
from recovery_inventory import FinalPassInventory, RecoverableJobQueue
from review_policy import is_airtable_reviewable
from pipeline_lock import PipelineRunLock

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


def _reason_counts(stats: dict, prefix: str) -> dict:
    """Aggregate non-zero `{prefix}reasoncode` stat keys into a plain dict for logging."""
    return {
        key.removeprefix(prefix): value
        for key, value in stats.items()
        if key.startswith(prefix) and value
    }


def _fail(summary: dict, step: str, errors: list[str]) -> dict:
    summary["failed_at"] = step
    summary["errors"] = errors
    summary["success"] = False
    summary["finished_at"] = datetime.now().isoformat()
    return summary



def _resume_scrape_from_checkpoint(jobs: list[dict], query_metrics: dict) -> ScrapeResult:
    """Create a raw artifact from crash-checkpoint jobs without repeating acquisition."""
    output_dir = Path(config.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"jobs_checkpoint_resume_{datetime.now():%Y-%m-%d_%H-%M-%S}.json"
    roles = {str(job.get("_search_role") or "").strip() for job in jobs if job.get("_search_role")}
    stats = {
        "checkpoint_resumed": True,
        "checkpoint_jobs": len(jobs),
        "query_metrics": dict(query_metrics or {}),
        "base_estimated_request_units": 0,
        "estimated_request_units": 0,
        "query_variant_metrics": {"checkpoint_resume": {"jobs": len(jobs)}},
    }
    path.write_text(
        json.dumps({"jobs": jobs, "total_jobs": len(jobs), "stats": stats}, indent=2),
        encoding="utf-8",
    )
    return ScrapeResult(
        output_path=str(path),
        total_jobs=len(jobs),
        roles_with_results=len(roles),
        stats=stats,
    )


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
    return any(
        bool((decision or {}).get("retryable"))
        for decision in (lead.get("_gate_decisions") or {}).values()
        if isinstance(decision, dict)
    )


def _precontact_is_retryable(job: dict) -> bool:
    return any(
        bool((job.get(field) or {}).get("retryable"))
        for field in ("_job_gate_decision", "_role_gate_decision")
        if isinstance(job.get(field), dict)
    )


def _load_jobs(path: str | None) -> list[dict]:
    if not path:
        return []
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [dict(job) for job in payload.get("jobs", []) if isinstance(job, dict)]


def _merge_precontact_capacity_jobs(hiring_input_path: str, extra_jobs: list[dict]) -> str:
    """Append capacity-expansion contact-eligible jobs into the hiring-manager
    input file, de-duplicated by job_id, and return the (same) path. The added
    jobs then flow through the single, unchanged paid hiring_manager step --
    no separate paid pass, no double-processing (company-level grouping and the
    SeenJobsRegistry already dedupe downstream)."""
    payload = json.loads(Path(hiring_input_path).read_text(encoding="utf-8"))
    jobs = list(payload.get("jobs", []))
    seen = {str(job.get("job_id") or "") for job in jobs if job.get("job_id")}
    added = 0
    for job in extra_jobs:
        jid = str(job.get("job_id") or "")
        if jid and jid in seen:
            continue
        jobs.append(job)
        if jid:
            seen.add(jid)
        added += 1
    payload["jobs"] = jobs
    payload["total_jobs"] = len(jobs)
    payload.setdefault("stats", {})["capacity_expansion_jobs_added"] = added
    Path(hiring_input_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return hiring_input_path


def _ready_targets(starting_ready_count: int) -> tuple[int, int]:
    """Return (minimum acquisition SLA, minimum delivery SLA).

    A zero delivery limit means unlimited delivery, not zero. The returned
    delivery target remains the minimum used for SLA reporting; selection later
    sends every READY lead when no explicit cap is configured.
    """
    configured_daily_target = max(1, int(config.get_final_pass_target()))
    configured_limit = int(config.READY_DAILY_DELIVERY_LIMIT)
    delivery_target = (
        configured_daily_target
        if configured_limit <= 0
        else max(1, min(configured_daily_target, configured_limit))
    )
    planned_existing_delivery = min(max(0, int(starting_ready_count)), delivery_target)
    inventory_after_planned_delivery = max(
        0, int(starting_ready_count) - planned_existing_delivery
    )
    acquisition_target = max(
        configured_daily_target,
        int(config.READY_INVENTORY_TARGET) - inventory_after_planned_delivery,
    )
    return acquisition_target, delivery_target


def _jsearch_topup_gates(
    acquisition_mode: str,
    *,
    jsearch_available: bool,
    target_final_pass: int,
) -> dict:
    """Every condition top-up requires, individually, so a disabled top-up is
    always attributable to a specific named gate instead of silently doing
    nothing (TECHNICAL_DESIGN.md D1 / ROOT_CAUSE_TABLE_STRUCTURAL.md row 1)."""
    mode = str(acquisition_mode or "").strip().lower()
    if mode == "multi_source":
        topup_switch = config.MULTI_SOURCE_JSEARCH_TOPUP_ENABLED
    elif config.FINAL_PASS_PIPELINE_ENABLED:
        topup_switch = config.FINAL_PASS_TOPUP_ENABLED
    else:
        topup_switch = config.JSEARCH_REVIEWABLE_TOPUP_ENABLED
    legacy_rounds_enabled = (
        mode == "multi_source" or config.JSEARCH_TOPUP_MAX_ROUNDS > 0
    )
    target_positive = int(target_final_pass) > 0
    return {
        "jsearch_available": bool(jsearch_available),
        "topup_switch": bool(topup_switch),
        "legacy_rounds_enabled": bool(legacy_rounds_enabled),
        "target_final_pass_positive": bool(target_positive),
    }


def _jsearch_topup_enabled(
    acquisition_mode: str,
    *,
    jsearch_available: bool,
    target_final_pass: int,
) -> bool:
    gates = _jsearch_topup_gates(
        acquisition_mode,
        jsearch_available=jsearch_available,
        target_final_pass=target_final_pass,
    )
    return all(gates.values())


def _jsearch_topup_disabled_reason(
    acquisition_mode: str,
    *,
    jsearch_available: bool,
    target_final_pass: int,
) -> str:
    gates = _jsearch_topup_gates(
        acquisition_mode,
        jsearch_available=jsearch_available,
        target_final_pass=target_final_pass,
    )
    failed = [name for name, passed in gates.items() if not passed]
    return "" if not failed else "disabled_by:" + ",".join(sorted(failed))


def run_pipeline() -> dict:
    # UNIVERSAL autorun chokepoint. Executes before EVERY side effect (Hunter
    # reset, registry/lock/state creation, source clients, all network calls).
    # No caller can run the pipeline by importing this module and calling
    # run_pipeline() while autorun is disabled. Runtime read (current env).
    if not config.autorun_is_enabled():
        logger.warning("Pipeline autorun disabled; no pipeline execution performed.")
        return {
            "success": True,
            "autorun_disabled": True,
            "technical_success": True,
            "sla_success": None,
            "steps": {},
        }
    started = datetime.now()
    # Hunter is optional corroboration, not a throughput dependency: reset its
    # run-level circuit breaker so a run with restored quota attempts it again,
    # while a mid-run quota exhaustion still stops further Hunter calls without
    # ever blocking Apollo-verified leads (Phase 13 §1).
    import hunter_client
    hunter_client.reset_run_state()
    registry = SeenJobsRegistry()
    recovery_queue = RecoverableJobQueue()
    final_pass_inventory = FinalPassInventory()
    checkpoint = PipelineCheckpoint()
    starting_ready_inventory = final_pass_inventory.available()
    target_final_pass, delivery_target = _ready_targets(len(starting_ready_inventory))
    summary = {
        "started_at": started.isoformat(),
        "production_mode": config.PRODUCTION,
        "date_posted": config.DATE_POSTED,
        "acquisition_mode": config.ACQUISITION_MODE,
        "steps": {},
        "success": False,
        "technical_success": False,
        "sla_success": False,
    }

    logger.info("=== STEP 1: SCRAPE ===")
    checkpoint_jobs = checkpoint.pending_jobs()
    checkpoint_metrics = checkpoint.query_metrics()
    due_recovery_jobs = recovery_queue.due_jobs()
    # JSearch is a resilient companion source in multi-source mode. It may add
    # deep-page inventory after free feeds/ATS and age recovery, but quota or
    # provider failure never disables the rest of acquisition.
    acquisition_mode = str(config.ACQUISITION_MODE).lower()
    jsearch_available = bool(config.RAPIDAPI_KEY) and (
        acquisition_mode == "jsearch"
        or (acquisition_mode == "multi_source" and config.MULTI_SOURCE_JSEARCH_ENABLED)
    )
    topup_enabled = _jsearch_topup_enabled(
        acquisition_mode,
        jsearch_available=jsearch_available,
        target_final_pass=target_final_pass,
    )
    topup_disabled_reason = "" if topup_enabled else _jsearch_topup_disabled_reason(
        acquisition_mode,
        jsearch_available=jsearch_available,
        target_final_pass=target_final_pass,
    )
    if topup_disabled_reason:
        logger.warning(
            "JSearch top-up (deficit recovery) is disabled this run: %s. "
            "This is the sole mechanism that pursues new supply after the "
            "primary pass falls short of the target -- confirm this is "
            "intentional (e.g. deliberate cost control), not an accident.",
            topup_disabled_reason,
        )
    if checkpoint_jobs:
        logger.warning(
            "Resuming %d checkpoint jobs; skipping duplicate base acquisition",
            len(checkpoint_jobs),
        )
        scrape = _resume_scrape_from_checkpoint(checkpoint_jobs, checkpoint_metrics)
    else:
        if str(config.ACQUISITION_MODE).lower() == "jsearch":
            scrape = run_daily_scrape(
                registry=registry,
                base_num_pages=(
                    config.JSEARCH_TOPUP_INITIAL_PAGES if topup_enabled else None
                ),
                # Initial adaptive acquisition and downstream micro-batch top-up are
                # independently bounded.
                allow_adaptive=None,
            )
        else:
            scrape = run_multi_source_acquisition(registry=registry)
        if checkpoint_metrics:
            scrape.stats["query_metrics"] = _merge_query_metrics(
                checkpoint_metrics,
                scrape.stats.get("query_metrics", {}),
            )
            scrape.stats["resumed_query_metric_roles"] = len(checkpoint_metrics)
    scrape = _merge_recovery_jobs(scrape, due_recovery_jobs)
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
    if str(config.ACQUISITION_MODE).lower() == "jsearch":
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
    else:
        logger.info(
            "Multi-source strategy: sources=%s source_metrics=%s ats_metrics=%s "
            "source_outcomes=%s boards_total=%d landing_discovery=%s",
            scrape.stats.get("enabled_sources", []),
            scrape.stats.get("source_metrics", {}),
            scrape.stats.get("ats_metrics", {}),
            scrape.stats.get("source_outcomes", {}),
            scrape.stats.get("boards_total", 0),
            scrape.stats.get("landing_discovery", {}),
        )

    logger.info("=== STEP 2: FILTER ===")
    filtered = run_filter(
        input_path=scrape.output_path,
        registry=registry,
        max_age_days=config.PRIMARY_MAX_JOB_AGE_DAYS,
        output_suffix="primary",
    )
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
    precontact_nonpass_paths: list[str] = []
    strict_runtime = False
    if config.FINAL_PASS_PIPELINE_ENABLED:
        logger.info("=== STEP 2C: JOB + ROLE GATES ===")
        qualified = run_precontact_qualification(filtered.output_path, suffix="initial")
        hiring_input_path = qualified.output_path
        strict_runtime = True
        precontact_nonpass_paths.append(qualified.nonpass_path)
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
        logger.info(
            "Source gate funnel: verified=%d corroborated=%d direct_structured=%d "
            "provider_structured=%d temporary=%d unresolved=%d retryable=%d "
            "direct_fast=%d discovery_fallback=%d "
            "discovery_budget_exhausted=%d",
            qualified.stats.get("source_state__ACTIVE_VERIFIED", 0),
            qualified.stats.get("source_state__ACTIVE_CORROBORATED", 0),
            qualified.stats.get("source_state__ACTIVE_DIRECT_STRUCTURED", 0),
            qualified.stats.get("source_state__ACTIVE_PROVIDER_STRUCTURED", 0),
            qualified.stats.get("source_state__SOURCE_TEMPORARILY_UNAVAILABLE", 0),
            qualified.stats.get("source_state__SOURCE_UNRESOLVED", 0),
            qualified.stats.get("source_retryable", 0),
            qualified.stats.get("source_note__direct_fast_path", 0),
            qualified.stats.get("source_note__company_discovery_fallback", 0),
            qualified.stats.get("source_attempt__discovery_budget_exhausted", 0),
        )
        if config.PRODUCTION and not qualified.success:
            return _fail(summary, "qualification", qualified.errors)

    # Pre-contact capacity expansion (Phase 13 integration correction). When
    # CAPACITY_CONTROLLER_ENABLED, the controller inspects the unique canonical
    # searchable-company pool and, while below target and a quality-safe
    # strategy remains, invokes REAL pre-contact acquisition/recovery (wider
    # age windows re-filtered + re-qualified through the same production
    # functions) BEFORE any paid Apollo/Hunter call -- materially altering
    # execution, not just reporting. Disabled -> strict no-op (baseline).
    summary["steps"]["capacity_expansion"] = {"enabled": False, "stop_reason": "disabled"}
    if config.CAPACITY_CONTROLLER_ENABLED and strict_runtime:
        import time as _time
        from capacity_strategies import expand_precontact_capacity
        _current_jobs = _load_jobs(hiring_input_path)
        _deadline = (
            _time.monotonic() + config.FINAL_PASS_MAX_RUNTIME_SECONDS * 0.5
            if config.FINAL_PASS_MAX_RUNTIME_SECONDS > 0 else None
        )
        _cap_state, _extra_jobs = expand_precontact_capacity(
            config_module=config,
            scrape_output_path=scrape.output_path,
            registry=registry,
            current_jobs=_current_jobs,
            runtime_deadline=_deadline,
        )
        summary["steps"]["capacity_expansion"] = _cap_state
        if _extra_jobs:
            hiring_input_path = _merge_precontact_capacity_jobs(hiring_input_path, _extra_jobs)
            logger.info(
                "Capacity expansion added %d contact-eligible jobs "
                "(searchable=%d/%d, stop=%s) before paid enrichment",
                len(_extra_jobs),
                _cap_state.get("searchable_companies_available"),
                _cap_state.get("searchable_company_target"),
                _cap_state.get("stop_reason"),
            )

    logger.info("=== STEP 3: HIRING MANAGER ===")
    existing_airtable_company_keys: set[str] = set()
    if config.AIRTABLE_SUPPRESS_EXISTING_COMPANY:
        try:
            existing_airtable_company_keys = (
                airtable_client.get_active_existing_company_keys_for_pipeline()
            )
            logger.info(
                "Pre-excluding %d active Airtable company keys before reviewable-lead counting",
                len(existing_airtable_company_keys),
            )
        except Exception as exc:
            logger.exception("Could not load existing Airtable companies")
            if config.PRODUCTION:
                return _fail(summary, "airtable_existing_companies", [str(exc)])
    logger.info(
        "READY throughput: acquisition_target=%d delivery_target=%d "
        "starting_inventory=%d reserve_target=%d eligible-company cap=%s",
        target_final_pass,
        delivery_target,
        len(starting_ready_inventory),
        config.READY_INVENTORY_TARGET,
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
    summary["steps"]["age_recovery"] = {
        "enabled": False,
        "attempted": False,
        "stop_reason": "not_applicable",
    }
    if strict_runtime:
        enriched, age_recovery_summary = run_age_recovery(
            initial_scrape=scrape,
            initial_enriched=enriched,
            registry=registry,
            target_final_pass_leads=target_final_pass,
            max_eligible_companies=(
                config.MAX_ELIGIBLE_COMPANIES_PER_RUN
                if config.MAX_ELIGIBLE_COMPANIES_PER_RUN > 0
                else None
            ),
            exclude_company_keys=existing_airtable_company_keys,
        )
        summary["steps"]["age_recovery"] = age_recovery_summary
        if age_recovery_summary.get("qualification_nonpass_output"):
            precontact_nonpass_paths.append(
                str(age_recovery_summary["qualification_nonpass_output"])
            )

    summary["steps"]["extended_age_recovery"] = {
        "enabled": False,
        "attempted": False,
        "stop_reason": "not_applicable",
    }
    if strict_runtime and config.EXTENDED_AGE_RECOVERY_ENABLED:
        # 31-90 day window (freshness_policy.py TIER_EXTENDED/TIER_DEEP).
        # Reuses run_age_recovery's exact pass logic; the wider window's
        # additional confirmed-active / difficult-to-fill evidence
        # requirements are enforced inside job_filter.is_stale_job, not here.
        enriched, extended_age_recovery_summary = run_age_recovery(
            initial_scrape=scrape,
            initial_enriched=enriched,
            registry=registry,
            target_final_pass_leads=target_final_pass,
            max_eligible_companies=(
                config.MAX_ELIGIBLE_COMPANIES_PER_RUN
                if config.MAX_ELIGIBLE_COMPANIES_PER_RUN > 0
                else None
            ),
            exclude_company_keys=existing_airtable_company_keys,
            min_age_days=config.RECOVERY_EXTENDED_MIN_JOB_AGE_DAYS,
            max_age_days=config.RECOVERY_DEEP_MAX_JOB_AGE_DAYS,
            output_suffix="extended_age_recovery",
            enabled_flag=True,
        )
        summary["steps"]["extended_age_recovery"] = extended_age_recovery_summary
        if extended_age_recovery_summary.get("qualification_nonpass_output"):
            precontact_nonpass_paths.append(
                str(extended_age_recovery_summary["qualification_nonpass_output"])
            )

    summary["steps"]["topup"] = {
        "enabled": topup_enabled,
        "disabled_reason": topup_disabled_reason,
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
        precontact_nonpass_paths.extend(
            str(round_item.get("qualification_nonpass_output") or "")
            for round_item in topup_summary.get("rounds", [])
            if round_item.get("qualification_nonpass_output")
        )
        logger.info(
            "Top-up final: mode=%s rounds=%d query_units=%d total_query_units=%d "
            "FINAL_PASS=%d review_rows=%d/%d stop_reason=%s",
            topup_summary.get("mode", "legacy_reviewable"),
            len(topup_summary.get("rounds", [])),
            topup_summary.get("topup_query_units", 0),
            topup_summary.get("total_query_units", 0),
            enriched.final_pass_leads,
            enriched.reviewable_leads,
            target_final_pass,
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
    # `no_matching_hiring_manager`/`candidate_*` keys are written only by the
    # legacy (non-strict) path; the strict pipeline records the same failure
    # reasons under `contact_reason__*`/`email_reason__*` instead, which the
    # original version of this line never read (always printed 0 in strict
    # mode). Both are now included so neither pipeline mode is silently blind.
    contact_reason_counts = _reason_counts(enriched.stats, "contact_reason__")
    email_reason_counts = _reason_counts(enriched.stats, "email_reason__")
    logger.info(
        "Hiring-manager funnel: companies_considered=%d eligible=%d FINAL_PASS=%d/%d review_rows=%d "
        "identified=%d contactable=%d | no_manager=%d no_email=%d invalid_email=%d "
        "org_domain_mismatch=%d email_domain_mismatch=%d founder_disallowed=%d "
        "person_match_attempts=%d | contact_reasons=%s email_reasons=%s",
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
        contact_reason_counts,
        email_reason_counts,
    )
    logger.info(
        "Hiring-manager selection tiers: direct=%d functional_exec=%d founder_fallback=%d",
        enriched.stats.get("selection_tier_direct_functional_leader", 0),
        enriched.stats.get("selection_tier_functional_executive", 0),
        enriched.stats.get("selection_tier_founder_fallback", 0),
    )
    logger.info(
        "Zero-attempt buckets: zero_apollo_people=%d no_title_match=%d all_candidates_previously_attempted=%d "
        "apollo_search_error=%d",
        enriched.stats.get("bucket_zero_apollo_people", 0),
        enriched.stats.get("bucket_no_title_match", 0),
        enriched.stats.get("bucket_all_candidates_previously_attempted", 0),
        enriched.stats.get("bucket_apollo_search_error", 0),
    )
    logger.info(
        "Row-2 reconciliation (bucket-level): eligible_company_buckets=%d people_search_calls=%d "
        "people_returned_total=%d buckets_with_person=%d title_matched_total=%d buckets_with_title_match=%d "
        "untried_total=%d buckets_with_untried=%d person_match_attempts=%d",
        enriched.total_output_leads - enriched.company_criteria_excluded,
        enriched.stats.get("row2_people_search_calls_total", 0),
        enriched.stats.get("row2_apollo_people_returned_total", 0),
        enriched.stats.get("row2_buckets_with_apollo_person", 0),
        enriched.stats.get("row2_title_matched_candidates_total", 0),
        enriched.stats.get("row2_buckets_with_title_match", 0),
        enriched.stats.get("row2_untried_candidates_total", 0),
        enriched.stats.get("row2_buckets_with_untried_candidate", 0),
        enriched.stats.get("person_match_attempts", 0),
    )
    logger.info(
        "Row-2 reconciliation (company-level, of %d eligible companies): with_people_search_call=%d "
        "with_person_returned=%d with_title_match=%d with_untried_candidate=%d with_person_match_attempt=%d",
        enriched.eligible_companies,
        enriched.stats.get("row2_companies_with_people_search_call", 0),
        enriched.stats.get("row2_companies_with_person_returned", 0),
        enriched.stats.get("row2_companies_with_title_match", 0),
        enriched.stats.get("row2_companies_with_untried_candidate", 0),
        enriched.stats.get("row2_companies_with_person_match_attempt", 0),
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
    if strict_runtime and not enriched.reviewable_target_reached:
        logger.warning(
            "Daily target not reached: %d/%d Airtable-reviewable leads. Stop reason: %s",
            enriched.reviewable_leads,
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
    precontact_nonpass_jobs = [
        job for path in precontact_nonpass_paths for job in _load_jobs(path)
    ]
    recoverable_jobs = [job for job in enriched_jobs if _lead_is_retryable(job)]
    recoverable_jobs.extend(
        job for job in precontact_nonpass_jobs if _precontact_is_retryable(job)
    )
    terminal_jobs = [
        job for job in enriched_jobs
        if not _lead_is_retryable(job)
    ]
    terminal_precontact_jobs = [
        job for job in precontact_nonpass_jobs if not _precontact_is_retryable(job)
    ]
    recovery_queue.upsert(recoverable_jobs)
    recovery_queue.remove([*terminal_jobs, *terminal_precontact_jobs])
    current_reviewable = [job for job in enriched_jobs if is_airtable_reviewable(job)]
    inventory_leads: list[dict] = []
    stage_result: dict = {}
    if strict_runtime:
        stage_result = final_pass_inventory.stage(current_reviewable)
        summary["steps"]["final_pass_inventory_stage"] = stage_result
        if stage_result.get("already_sent_same_bucket_suppressed"):
            logger.info(
                "Final-pass inventory: %d lead(s) suppressed as already-sent for "
                "the same account+function (lead_keys=%s)",
                stage_result["already_sent_same_bucket_suppressed"],
                stage_result.get("already_sent_suppressed_lead_keys", []),
            )
        inventory_leads = final_pass_inventory.available(
            limit=(
                None
                if int(config.READY_DAILY_DELIVERY_LIMIT) <= 0
                else int(config.READY_DAILY_DELIVERY_LIMIT)
            )
        )
        final_pass_inventory.reserve(inventory_leads)
        airtable_candidates = inventory_leads
    else:
        airtable_candidates = enriched_jobs
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
    if strict_runtime:
        final_pass_inventory.mark_persisted(airtable_result.get("persisted_lead_keys", []))
        final_pass_inventory.release_failed(airtable_result.get("failed_lead_keys", []))
    if airtable_result["failed"]:
        # Qualification/enrichment completed and READY leads remain in the
        # inventory. Do not repeat JSearch/Apollo after a pure Airtable outage.
        registry.mark_jobs([*terminal_jobs, *terminal_precontact_jobs])
        checkpoint.clear()
        return _fail(
            summary,
            "airtable",
            [f"{airtable_result['failed']} Airtable records failed to persist"],
        )

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
    registry.mark_jobs(terminal_precontact_jobs)
    # The crash checkpoint is only for work that never reached a downstream
    # disposition. Retryable outcomes already live in RecoverableJobQueue.
    checkpoint.remove_jobs([*processed_job_refs, *precontact_nonpass_jobs])

    net_created = int(airtable_result.get("created", 0))
    ready_for_delivery = len(inventory_leads) if strict_runtime else enriched.reviewable_leads
    upstream_target_reached = ready_for_delivery >= delivery_target
    net_target_reached = net_created >= delivery_target
    sla_success = upstream_target_reached and (
        net_target_reached if config.SLA_REQUIRE_NET_NEW_AIRTABLE else True
    )
    summary["technical_success"] = True
    summary["sla_success"] = sla_success
    summary["sla"] = {
        "target": delivery_target,
        "acquisition_target": target_final_pass,
        "upstream_final_pass": enriched.final_pass_leads,
        "ready_inventory_selected": ready_for_delivery,
        "upstream_target_reached": upstream_target_reached,
        "net_airtable_created": net_created,
        "net_target_reached": net_target_reached,
        "stop_reason": enriched.stop_reason,
    }
    if not sla_success:
        summary.setdefault("warnings", []).append(
            f"SLA target not reached: ready_for_delivery={ready_for_delivery}/{delivery_target}, "
            f"net_airtable_created={net_created}/{delivery_target}"
        )
        logger.error(summary["warnings"][-1])

    # Non-negotiable throughput policy reporting (spec: 30 is a minimum SLA,
    # never a cap -- FINAL_PASS >= 30 is the success condition, not == 30).
    # Every field below is assembled from counters already computed above;
    # this does not change what gets delivered, only how it is reported.
    total_final_pass_found = int(enriched.final_pass_leads)
    total_delivered = net_created
    above_sla_count = max(0, total_delivered - delivery_target)
    omission_reasons = {
        "already_sent_same_bucket_suppressed": int(stage_result.get("already_sent_same_bucket_suppressed", 0)),
        "airtable_skipped_existing": int(airtable_result.get("skipped_existing", 0)),
        "airtable_skipped_existing_company": int(airtable_result.get("skipped_existing_company", 0)),
        "airtable_failed": int(airtable_result.get("failed", 0)),
    }
    total_omitted = max(0, total_final_pass_found - total_delivered)
    unique_companies = int(enriched.companies_considered)
    duplicate_leads_avoided = (
        omission_reasons["already_sent_same_bucket_suppressed"]
        + omission_reasons["airtable_skipped_existing"]
        + omission_reasons["airtable_skipped_existing_company"]
    )
    summary["sla_report"] = {
        "sla_minimum": delivery_target,
        "success_condition": f"final_pass_delivered >= {delivery_target} (not ==)",
        "total_final_pass_found": total_final_pass_found,
        "total_delivered": total_delivered,
        "above_sla_count": above_sla_count,
        "total_omitted": total_omitted,
        "omission_reasons": omission_reasons,
        "unique_companies": unique_companies,
        "duplicate_leads_avoided": duplicate_leads_avoided,
    }
    # Authoritative deficit-driven controller state (FINAL_30_PLUS_SYSTEM_SPEC.md
    # section 6): consolidates fields already computed above and by each
    # acquisition/recovery step into the one run-level view the spec asks
    # for, rather than leaving them scattered across summary["steps"].
    # Fields this run cannot measure (provider quota/reserve -- no client in
    # this codebase currently exposes remaining Apollo/Hunter/JSearch quota)
    # are reported as null with an explicit reason, never fabricated.
    scrape_stats = summary["steps"].get("scrape", {}).get("stats", {})
    topup_step = summary["steps"].get("topup", {})
    deficit = max(0, delivery_target - net_created)
    summary["controller_state"] = {
        "target": delivery_target,
        "acquisition_target": target_final_pass,
        "final_pass_found": total_final_pass_found,
        "final_pass_delivered": total_delivered,
        "target_reached": bool(enriched.final_pass_target_reached),
        "deficit": deficit,
        "leads_above_target": above_sla_count,
        "unique_companies": unique_companies,
        "companies_considered": int(enriched.companies_considered),
        "eligible_companies": int(enriched.eligible_companies),
        "unresolved_companies": max(
            0, int(enriched.eligible_companies) - int(getattr(enriched, "lead_capable_companies", 0))
        ),
        "upstream_ready_inventory_at_start": len(starting_ready_inventory),
        "enabled_sources": list(scrape_stats.get("enabled_sources", [])),
        "source_health": {
            source: metric.get("success")
            for source, metric in scrape_stats.get("source_metrics", {}).items()
        },
        "ats_closed_or_removed_job_ids": sum(
            metric.get("closed_or_removed_job_ids", 0)
            for metric in scrape_stats.get("ats_metrics", {}).values()
        ),
        "topup_enabled": bool(topup_step.get("enabled")),
        "topup_disabled_reason": topup_step.get("disabled_reason", ""),
        "topup_unit_budget": topup_step.get("topup_unit_budget"),
        "topup_query_units_used": topup_step.get("topup_query_units"),
        "adzuna_enabled": bool(scrape_stats.get("adzuna", {}).get("enabled")),
        "provider_quota_remaining": None,
        "provider_quota_reserve": None,
        "provider_quota_not_observable_reason": (
            "no Apollo/Hunter/JSearch client in this codebase currently "
            "exposes remaining account-level quota; only per-run request/unit "
            "counts are observable (see topup_query_units_used above)."
        ),
        # Hunter is optional corroboration: its unavailability is observable
        # here but never blocks delivery of an Apollo-verified lead (Phase 13 §1).
        "hunter": hunter_client.run_status(),
        "stop_reason": enriched.stop_reason,
    }

    # Post-run capacity summary: the *authoritative* capacity control happens
    # pre-contact (STEP 2C->3, summary["steps"]["capacity_expansion"]); this
    # end-of-run block only mirrors the final unique-canonical searchable pool
    # (post-enrichment, for observability) and never drives acquisition.
    _post_capacity = build_from_config(config)
    _post_capacity.register_searchable(
        canonical_company_key(
            domain=str(lead.get("canonical_domain") or lead.get("company_domain") or ""),
            normalized_name=str(lead.get("canonical_company_name") or lead.get("employer_name") or "").lower(),
            blocked_domains=config.INTERMEDIARY_JOB_DOMAINS,
        )
        for lead in enriched_jobs
        if (lead.get("canonical_domain") or lead.get("company_domain"))
    )
    summary["capacity_controller_state"] = {
        **_post_capacity.state(recovery_inventory_available=len(starting_ready_inventory)),
        "note": "post-enrichment mirror; authoritative pre-contact control is in steps.capacity_expansion",
    }

    # A checkpoint represents an interrupted technical run, not unmet commercial
    # inventory. Every clean completion clears it; retryable work is already in
    # RecoverableJobQueue and READY leads are already in FinalPassInventory.
    checkpoint.clear()
    summary["success"] = True
    summary["finished_at"] = datetime.now().isoformat()
    summary["duration_seconds"] = round((datetime.now() - started).total_seconds(), 2)
    summary["registry_total_tracked"] = registry.total_tracked
    return summary


def main() -> int:
    # Guard before any side effect (including PipelineRunLock acquisition).
    if not config.autorun_is_enabled():
        logger.warning("Pipeline autorun disabled; no pipeline execution performed.")
        return 0
    try:
        with PipelineRunLock():
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
        logger.warning("Pipeline completed technically but missed the daily SLA")
        return 2 if config.PIPELINE_FAIL_PROCESS_ON_SLA_MISS else 0
    if summary.get("success"):
        logger.info("Pipeline completed successfully")
        return 0
    logger.error("Pipeline failed at %s: %s", summary.get("failed_at"), summary.get("errors"))
    return 1


def run_entrypoint() -> int:
    """Railway/script entrypoint. Guards autorun so that starting a deployment
    (``python run_daily.py``) can never execute the pipeline or consume paid
    credits unless PIPELINE_AUTORUN_ENABLED is explicitly set. Tests and developer
    tooling call run_pipeline()/main() directly and are unaffected."""
    if not config.autorun_is_enabled():
        logger.warning("Pipeline autorun disabled; no pipeline execution performed.")
        return 0
    return main()


if __name__ == "__main__":
    try:
        exit_code = run_entrypoint()
    finally:
        # Explicitly close file handlers so one-shot Railway services terminate
        # cleanly after the run summary is persisted.
        logging.shutdown()
    raise SystemExit(exit_code)
