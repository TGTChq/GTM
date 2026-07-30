"""Adaptive age-window recovery after the primary 0-14 day pass.

``run_age_recovery`` runs the 15-30 day window by default, and can also run
the 31-90 day extended/deep window (see freshness_policy.py) when called with
wider bounds -- both reuse the exact same deterministic filters and
downstream Job, Role, Account, Contact, and Email gates. Each pass is invoked
only when the prior lane has not reached the minimum FINAL_PASS SLA.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

import config
from final_pass_topup import combine_step3_results
from hiring_manager import Step3Result, company_key_for_job, run_hiring_manager_identification
from job_filter import run_filter
from jsearch_scraper import ScrapeResult
from pipeline_state import SeenJobsRegistry
from qualification_pipeline import run_precontact_qualification

logger = logging.getLogger(__name__)


def _reviewable_count(result: Step3Result) -> int:
    """Treat legacy FINAL_PASS-only result fixtures as reviewable inventory."""
    return max(int(result.reviewable_leads or 0), int(result.final_pass_leads or 0))


def _load(path: str) -> Dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _prune_processed_companies(path: str, excluded_company_keys: set[str]) -> tuple[str, int]:
    payload = _load(path)
    jobs = [
        job
        for job in payload.get("jobs", [])
        if company_key_for_job(job) not in excluded_company_keys
    ]
    pruned = len(payload.get("jobs", [])) - len(jobs)
    payload["jobs"] = jobs
    payload["total_jobs"] = len(jobs)
    payload.setdefault("stats", {})["excluded_company_already_processed"] = pruned
    output = Path(path).with_name(Path(path).stem + "_unique_companies.json")
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(output), pruned


def run_age_recovery(
    *,
    initial_scrape: ScrapeResult,
    initial_enriched: Step3Result,
    registry: SeenJobsRegistry,
    target_final_pass_leads: int,
    max_eligible_companies: Optional[int],
    exclude_company_keys: Optional[set[str]] = None,
    min_age_days: Optional[int] = None,
    max_age_days: Optional[int] = None,
    output_suffix: str = "age_recovery",
    enabled_flag: Optional[bool] = None,
) -> tuple[Step3Result, Dict]:
    """Process active older-than-primary-window inventory when the primary
    lane is below SLA.

    Defaults reproduce the original 15-30 day recovery pass exactly. Passing
    ``min_age_days``/``max_age_days``/``output_suffix``/``enabled_flag``
    lets the same pass logic run again over the 31-90 day extended/deep
    tiers (see freshness_policy.py), gated independently via
    ``config.EXTENDED_AGE_RECOVERY_ENABLED``.
    """
    min_age_days = config.RECOVERY_MIN_JOB_AGE_DAYS if min_age_days is None else min_age_days
    max_age_days = config.RECOVERY_MAX_JOB_AGE_DAYS if max_age_days is None else max_age_days
    enabled = config.AGE_RECOVERY_ENABLED if enabled_flag is None else enabled_flag
    details: Dict = {
        "enabled": bool(enabled),
        "window_min_days": int(min_age_days),
        "window_max_days": int(max_age_days),
        "initial_final_pass_leads": initial_enriched.final_pass_leads,
        "target_final_pass_leads": target_final_pass_leads,
        "attempted": False,
        "filter_kept": 0,
        "contact_eligible_jobs": 0,
        "recovered_final_pass_leads": 0,
        "errors": [],
    }
    if not enabled:
        details["stop_reason"] = "disabled"
        return initial_enriched, details
    if _reviewable_count(initial_enriched) >= target_final_pass_leads:
        details["stop_reason"] = "minimum_reached_in_primary_window"
        return initial_enriched, details

    details["attempted"] = True
    recovered_filter = run_filter(
        input_path=initial_scrape.output_path,
        registry=registry,
        max_age_days=max_age_days,
        min_age_days=min_age_days,
        output_suffix=output_suffix,
        allow_empty=True,
    )
    details.update({
        "filter_output": recovered_filter.output_path,
        "filter_rejected_output": recovered_filter.rejected_path,
        "filter_kept": recovered_filter.kept_count,
        "filter_stats": recovered_filter.stats,
    })
    if recovered_filter.errors:
        details["errors"].extend(recovered_filter.errors)
    if recovered_filter.kept_count == 0:
        details["stop_reason"] = "no_jobs_in_recovery_window"
        return initial_enriched, details

    excluded = {
        str(value)
        for value in [
            *(exclude_company_keys or set()),
            *initial_enriched.processed_company_keys,
        ]
        if value
    }
    unique_path, pruned = _prune_processed_companies(recovered_filter.output_path, excluded)
    details["duplicate_companies_pruned"] = pruned
    details["unique_filter_output"] = unique_path
    if not _load(unique_path).get("jobs"):
        details["stop_reason"] = "recovery_companies_already_processed"
        return initial_enriched, details

    qualified = run_precontact_qualification(unique_path, suffix=output_suffix)
    details.update({
        "qualification_output": qualified.output_path,
        "qualification_nonpass_output": qualified.nonpass_path,
        "contact_eligible_jobs": qualified.contact_eligible_jobs,
        "qualification_stats": qualified.stats,
    })
    if qualified.errors:
        details["errors"].extend(qualified.errors)
    if qualified.contact_eligible_jobs == 0:
        details["stop_reason"] = "recovery_downstream_gates_exhausted"
        return initial_enriched, details

    remaining_target = max(1, target_final_pass_leads - _reviewable_count(initial_enriched))
    remaining_company_cap = (
        None
        if max_eligible_companies is None
        else max(0, max_eligible_companies - initial_enriched.eligible_companies)
    )
    if remaining_company_cap == 0:
        details["stop_reason"] = "eligible_company_cap_reached_in_primary_window"
        return initial_enriched, details

    recovered = run_hiring_manager_identification(
        qualified.output_path,
        target_final_pass_leads=remaining_target,
        max_eligible_companies=remaining_company_cap,
        exclude_company_keys=excluded,
        output_suffix=output_suffix,
    )
    details["recovered_final_pass_leads"] = recovered.final_pass_leads
    details["recovered_reviewable_leads"] = _reviewable_count(recovered)
    details["recovered_companies_considered"] = recovered.companies_considered
    details["recovered_eligible_companies"] = recovered.eligible_companies
    details["recovery_hiring_manager_output"] = recovered.output_path
    if recovered.errors:
        details["errors"].extend(recovered.errors)

    combined = combine_step3_results(
        [initial_enriched, recovered],
        target_final_pass_leads=target_final_pass_leads,
        max_eligible_companies=max_eligible_companies,
        stop_reason=f"{output_suffix}_completed",
        additional_stats={
            f"{output_suffix}_filter_kept": recovered_filter.kept_count,
            f"{output_suffix}_contact_eligible": qualified.contact_eligible_jobs,
            f"{output_suffix}_final_pass": recovered.final_pass_leads,
        },
    )
    stop_reason = (
        f"final_pass_minimum_reached_after_{output_suffix}"
        if _reviewable_count(combined) >= target_final_pass_leads
        else f"{output_suffix}_exhausted"
    )
    combined_payload = _load(combined.output_path)
    combined_payload["stop_reason"] = stop_reason
    Path(combined.output_path).write_text(
        json.dumps(combined_payload, indent=2), encoding="utf-8"
    )
    combined.stop_reason = stop_reason
    details["combined_final_pass_leads"] = combined.final_pass_leads
    details["combined_reviewable_leads"] = _reviewable_count(combined)
    details["stop_reason"] = stop_reason
    logger.info(
        "%s %d-%dd: kept=%d contact_eligible=%d companies_considered=%d "
        "eligible_companies=%d reviewable_added=%d combined=%d/%d",
        output_suffix,
        min_age_days,
        max_age_days,
        recovered_filter.kept_count,
        qualified.contact_eligible_jobs,
        recovered.companies_considered,
        recovered.eligible_companies,
        _reviewable_count(recovered),
        _reviewable_count(combined),
        target_final_pass_leads,
    )
    return combined, details
