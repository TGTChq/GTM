"""Closed-loop micro-batch top-up to 30 fully validated FINAL_PASS leads."""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import config
from hiring_manager import Step3Result, company_key_for_job, run_hiring_manager_identification
from job_filter import run_filter
from jsearch_scraper import ScrapeResult, run_targeted_topup_scrape
from pipeline_state import SeenJobsRegistry
from pipeline_checkpoint import PipelineCheckpoint
from qualification_pipeline import run_precontact_qualification
from review_policy import is_airtable_reviewable
from reviewable_topup import (
    _dedupe_job_refs,
    _lead_dedupe_key,
    _load,
    _merge_numeric_stats,
    _merge_query_metrics,
)

logger = logging.getLogger(__name__)


def _final_pass_keys(leads: Iterable[Dict]) -> set[str]:
    return {
        str(lead.get("lead_key"))
        for lead in leads
        if lead.get("_final_state") == "FINAL_PASS" and lead.get("lead_key")
    }


def _surface_keys(leads: Iterable[Dict]) -> set[str]:
    return {
        str(lead.get("lead_key"))
        for lead in leads
        if lead.get("lead_key")
        and (
            lead.get("_final_state") == "FINAL_PASS"
            or is_airtable_reviewable(lead)
        )
    }


def _dedupe_leads_prefer_stronger(leads: Iterable[Dict]) -> List[Dict]:
    rank = {"FINAL_PASS": 5, "NEEDS_CHECK": 4, "REROUTE": 3, "UNVERIFIED": 2, "REJECT": 1, "": 0}
    selected: Dict[Tuple[str, ...], Dict] = {}
    order: List[Tuple[str, ...]] = []
    for lead in leads:
        key = _lead_dedupe_key(lead)
        if key not in selected:
            order.append(key)
            selected[key] = lead
            continue
        if rank.get(str(lead.get("_final_state") or ""), 0) > rank.get(str(selected[key].get("_final_state") or ""), 0):
            selected[key] = lead
    return [selected[key] for key in order]


def _preferred_roles(leads: Iterable[Dict]) -> List[str]:
    counts = Counter(
        str(lead.get("_search_role"))
        for lead in leads
        if lead.get("_search_role")
        and (
            lead.get("_final_state") == "FINAL_PASS"
            or is_airtable_reviewable(lead)
        )
    )
    return [role for role, count in counts.most_common() for _ in range(count)]


def _combine(
    *,
    results: List[Step3Result],
    payloads: List[Dict],
    target: int,
    max_eligible_companies: Optional[int],
    stop_reason: str,
    topup_stats: Dict,
) -> Step3Result:
    all_leads = _dedupe_leads_prefer_stronger(
        lead for payload in payloads for lead in payload.get("jobs", [])
    )
    processed_refs = _dedupe_job_refs(
        job for payload in payloads for job in payload.get("processed_job_refs", [])
    )
    processed_company_keys = list(dict.fromkeys(
        str(key)
        for payload in payloads
        for key in payload.get("processed_company_keys", [])
        if key
    ))
    pass_count = len(_final_pass_keys(all_leads))
    surface_count = len(_surface_keys(all_leads))
    state_counts = Counter(str(lead.get("_final_state") or "") for lead in all_leads)
    cumulative_stats: Dict[str, int] = defaultdict(int)
    for result in results:
        _merge_numeric_stats(cumulative_stats, result.stats)
    _merge_numeric_stats(cumulative_stats, topup_stats)
    eligible_leads = [
        lead for lead in all_leads
        if lead.get("_account_gate_state") in {"PASS", "NEEDS_CHECK", "UNVERIFIED"}
    ]
    identified = sum(1 for lead in eligible_leads if lead.get("hiring_manager_name"))
    contactable = sum(1 for lead in eligible_leads if lead.get("_step3_status") == "found")
    eligible_buckets = len(eligible_leads)
    match_rate = identified / eligible_buckets if eligible_buckets else 0.0
    contactable_rate = contactable / eligible_buckets if eligible_buckets else 0.0
    companies_considered = sum(result.companies_considered for result in results)
    eligible_companies = sum(result.eligible_companies for result in results)
    excluded_companies = sum(result.company_criteria_excluded_companies for result in results)
    target_reached = surface_count >= target
    limit_reached = bool(
        max_eligible_companies and eligible_companies >= max_eligible_companies
    )

    output_path = str(Path(config.STEP3_OUTPUT_DIR) / f"jobs_enriched_{datetime.now():%Y-%m-%d}_final_pass_combined.json")
    payload = {
        "run_date": datetime.now().isoformat(),
        "validation_version": config.VALIDATION_VERSION,
        "strict_final_pass_mode": True,
        "source_file": "closed_loop_final_pass_topup",
        "source_total_jobs": len(processed_refs),
        "total_input_jobs": len(processed_refs),
        "total_output_leads": len(all_leads),
        "companies_considered": companies_considered,
        "eligible_companies": eligible_companies,
        "company_criteria_excluded_companies": excluded_companies,
        "final_pass_target": target,
        "final_pass_leads": pass_count,
        "final_pass_target_reached": pass_count >= target,
        "reviewable_leads": surface_count,
        "reviewable_target_reached": target_reached,
        "needs_check_leads": state_counts["NEEDS_CHECK"],
        "reroute_leads": state_counts["REROUTE"],
        "unverified_leads": state_counts["UNVERIFIED"],
        "rejected_leads": state_counts["REJECT"],
        "max_eligible_companies": max_eligible_companies,
        "eligible_company_limit_reached": limit_reached,
        "stop_reason": stop_reason,
        "target_reached": target_reached,
        "hiring_manager_identified": identified,
        "hiring_manager_not_identified": eligible_buckets - identified,
        "hiring_manager_identification_rate": round(match_rate, 4),
        "contactable_hiring_managers": contactable,
        "uncontactable_hiring_managers": eligible_buckets - contactable,
        "contactable_rate": round(contactable_rate, 4),
        "processed_job_refs": processed_refs,
        "processed_company_keys": processed_company_keys,
        "stats": dict(cumulative_stats),
        "jobs": all_leads,
    }
    Path(output_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return Step3Result(
        output_path=output_path,
        total_input_jobs=len(processed_refs),
        total_output_leads=len(all_leads),
        company_criteria_excluded=state_counts["REJECT"],
        hiring_manager_found=identified,
        hiring_manager_not_found=eligible_buckets - identified,
        match_rate=match_rate,
        contactable_hiring_managers=contactable,
        uncontactable_hiring_managers=eligible_buckets - contactable,
        contactable_rate=contactable_rate,
        companies_considered=companies_considered,
        eligible_companies=eligible_companies,
        company_criteria_excluded_companies=excluded_companies,
        target_reviewable_leads=target,
        reviewable_leads=surface_count,
        reviewable_target_reached=surface_count >= target,
        final_pass_target=target,
        final_pass_leads=pass_count,
        needs_check_leads=state_counts["NEEDS_CHECK"],
        reroute_leads=state_counts["REROUTE"],
        unverified_leads=state_counts["UNVERIFIED"],
        rejected_leads=state_counts["REJECT"],
        final_pass_target_reached=pass_count >= target,
        max_eligible_companies=max_eligible_companies,
        eligible_company_limit_reached=limit_reached,
        target_reached=target_reached,
        stop_reason=stop_reason,
        processed_company_keys=processed_company_keys,
        stats=dict(cumulative_stats),
        success=all(result.success for result in results),
        errors=list(dict.fromkeys(
            error
            for result in results
            for error in result.errors
            if error
        )),
    )


def combine_step3_results(
    results: List[Step3Result],
    *,
    target_final_pass_leads: int,
    max_eligible_companies: Optional[int],
    stop_reason: str,
    additional_stats: Optional[Dict] = None,
) -> Step3Result:
    """Combine independently validated Step-3 batches without weakening gates."""
    if not results:
        raise ValueError("At least one Step3Result is required")
    payloads = [_load(result.output_path) for result in results]
    return _combine(
        results=results,
        payloads=payloads,
        target=target_final_pass_leads,
        max_eligible_companies=max_eligible_companies,
        stop_reason=stop_reason,
        topup_stats=dict(additional_stats or {}),
    )


def run_final_pass_topup(
    *,
    initial_scrape: ScrapeResult,
    initial_enriched: Step3Result,
    registry: SeenJobsRegistry,
    target_final_pass_leads: int,
    max_eligible_companies: int,
    exclude_company_keys: Optional[set[str]] = None,
) -> tuple[Step3Result, Dict]:
    """Run reroute-first, bounded micro-batches until the FINAL_PASS target."""
    started = time.monotonic()
    checkpoint = PipelineCheckpoint()
    initial_payload = _load(initial_enriched.output_path)
    payloads = [initial_payload]
    results = [initial_enriched]
    all_leads = list(initial_payload.get("jobs", []))
    considered_company_keys = {
        str(value)
        for value in [
            *(exclude_company_keys or set()),
            *initial_enriched.processed_company_keys,
        ]
        if value
    }
    selected_job_ids = {
        str(job.get("job_id"))
        for job in _load(initial_scrape.output_path).get("jobs", [])
        if job.get("job_id")
    }
    query_metrics = deepcopy(initial_scrape.stats.get("query_metrics", {}))
    total_query_units = int(initial_scrape.stats.get("estimated_request_units", 0))
    topup_units = 0
    details: Dict = {
        "enabled": True,
        "mode": "final_pass_microbatch",
        "initial_final_pass_leads": initial_enriched.final_pass_leads,
        "target_final_pass_leads": target_final_pass_leads,
        "initial_query_units": total_query_units,
        "reroute_rounds": [],
        "rounds": [],
        "errors": [],
    }
    stop_reason = initial_enriched.stop_reason
    attempted_role_cycle: set[str] = set()
    empty_query_cycles = 0
    zero_downstream_batches = 0
    multi_source_mode = str(config.ACQUISITION_MODE or "").strip().lower() == "multi_source"
    iteration_limit = (
        config.MULTI_SOURCE_FINAL_PASS_MAX_TOPUP_ITERATIONS
        if multi_source_mode
        else config.FINAL_PASS_MAX_TOPUP_ITERATIONS
    )
    zero_downstream_limit = max(
        1,
        config.MULTI_SOURCE_TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES
        if multi_source_mode
        else config.TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES,
    )
    details["iteration_limit"] = iteration_limit
    details["zero_downstream_batch_limit"] = zero_downstream_limit
    details["bounded_by"] = [
        "final_pass_target",
        "jsearch_request_budget",
        "runtime",
        "valid_inventory",
        "downstream_yield",
    ]

    if len(_surface_keys(all_leads)) >= target_final_pass_leads:
        stop_reason = "final_pass_target_reached_initial_pass"
    else:
        # Reroute is cheaper than new JSearch inventory. Re-run only accounts that
        # ended in REROUTE, allowing the persistent registry to skip prior people.
        reroute_jobs = [lead for lead in all_leads if lead.get("_final_state") == "REROUTE"]
        if reroute_jobs:
            reroute_input = Path(config.FILTERED_OUTPUT_DIR) / f"reroute_candidates_{datetime.now():%Y-%m-%d_%H%M%S}.json"
            reroute_input.write_text(json.dumps({"jobs": reroute_jobs}, indent=2), encoding="utf-8")
            before = len(_surface_keys(all_leads))
            try:
                rerouted = run_hiring_manager_identification(
                    str(reroute_input),
                    target_final_pass_leads=max(1, target_final_pass_leads - before),
                    max_eligible_companies=(
                        max_eligible_companies if max_eligible_companies > 0 else None
                    ),
                    output_suffix="reroute",
                )
                reroute_payload = _load(rerouted.output_path)
                payloads.append(reroute_payload)
                results.append(rerouted)
                all_leads.extend(reroute_payload.get("jobs", []))
                after = len(_surface_keys(_dedupe_leads_prefer_stronger(all_leads)))
                details["reroute_rounds"].append({
                    "attempted": len(reroute_jobs),
                    "reviewable_added": max(0, after - before),
                    "final_pass_added": max(0, after - before),
                    "output": rerouted.output_path,
                })
            except Exception as exc:
                logger.exception("Reroute recovery failed")
                details["errors"].append(f"reroute: {exc}")

        iteration = 0
        while True:
            iteration += 1
            if iteration_limit > 0 and iteration > iteration_limit:
                stop_reason = "topup_iteration_limit_reached"
                break
            current = len(_surface_keys(_dedupe_leads_prefer_stronger(all_leads)))
            if current >= target_final_pass_leads:
                stop_reason = "final_pass_target_reached"
                break
            if (
                config.FINAL_PASS_MAX_RUNTIME_SECONDS > 0
                and time.monotonic() - started >= config.FINAL_PASS_MAX_RUNTIME_SECONDS
            ):
                stop_reason = "runtime_limit_reached"
                break
            eligible_used = sum(result.eligible_companies for result in results)
            eligible_remaining = (
                max_eligible_companies - eligible_used
                if max_eligible_companies > 0
                else None
            )
            if eligible_remaining is not None and eligible_remaining <= 0:
                stop_reason = "eligible_company_safety_cap_reached"
                break
            total_budget = config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN
            budget_remaining = max(0, total_budget - total_query_units) if total_budget > 0 else config.FINAL_PASS_MICROBATCH_QUERY_UNITS
            if budget_remaining <= 0:
                stop_reason = "jsearch_unit_budget_exhausted"
                break
            unit_budget = min(budget_remaining, max(1, config.FINAL_PASS_MICROBATCH_QUERY_UNITS))
            deficit = target_final_pass_leads - current
            target_prefilter = max(1, min(12, int(math.ceil(deficit * 2.0))))
            logger.info(
                "=== FINAL_PASS MICRO-BATCH %d === deficit=%d unit_budget=%d prefilter_target=%d",
                iteration, deficit, unit_budget, target_prefilter,
            )
            try:
                scrape = run_targeted_topup_scrape(
                    registry=registry,
                    prior_query_metrics=query_metrics,
                    exclude_job_ids=selected_job_ids,
                    preferred_search_roles=_preferred_roles(all_leads),
                    exclude_search_roles=set(attempted_role_cycle),
                    unit_budget=unit_budget,
                    target_prefilter_viable=target_prefilter,
                    round_number=iteration,
                )
            except Exception as exc:
                logger.exception("FINAL_PASS top-up scrape failed")
                details["errors"].append(f"round_{iteration}_scrape: {exc}")
                stop_reason = "topup_scrape_error"
                break

            units = int(scrape.stats.get("estimated_request_units", 0))
            queried_roles = {
                str(role)
                for role in scrape.stats.get("queried_search_roles", [])
                if role
            }
            topup_units += units
            total_query_units += units
            query_metrics = _merge_query_metrics(query_metrics, scrape.stats.get("query_metrics", {}))
            raw_payload = _load(scrape.output_path)
            checkpoint.append_jobs(
                raw_payload.get("jobs", []),
                query_metrics=query_metrics,
            )
            selected_job_ids.update(str(job.get("job_id")) for job in raw_payload.get("jobs", []) if job.get("job_id"))
            round_detail = {
                "round": iteration,
                "query_units": units,
                "queries_attempted": scrape.stats.get("queries_attempted", 0),
                "scraped_jobs": scrape.total_jobs,
                "prefilter_viable_added": scrape.stats.get("topup_new_prefilter_viable", 0),
                "scrape_stop_reason": scrape.stats.get("topup_stop_reason", ""),
                "scrape_output": scrape.output_path,
                "queried_search_roles": sorted(queried_roles),
            }
            if int(scrape.stats.get("queries_attempted", 0)) <= 0:
                round_detail["final_pass_added"] = 0
                details["rounds"].append(round_detail)
                if attempted_role_cycle:
                    # Every role in the current breadth cycle has been offered a
                    # query window. Reset the cycle so the next call can advance
                    # all roles to deeper pages/date windows instead of repeating
                    # only the two highest-yield roles forever.
                    attempted_role_cycle.clear()
                    round_detail["cycle_reset"] = True
                    continue
                empty_query_cycles += 1
                if empty_query_cycles >= max(1, config.FINAL_PASS_MAX_EMPTY_QUERY_CYCLES):
                    stop_reason = "valid_inventory_exhausted"
                    break
                continue

            empty_query_cycles = 0
            attempted_role_cycle.update(queried_roles)
            if scrape.total_jobs <= 0 or scrape.stats.get("topup_new_prefilter_viable", 0) <= 0:
                # A zero-yield micro-batch proves only that these query windows
                # were empty. It does not prove that the other roles, deeper
                # pages or wider date windows are exhausted.
                round_detail["final_pass_added"] = 0
                details["rounds"].append(round_detail)
                continue

            filtered = run_filter(input_path=scrape.output_path, registry=registry)
            round_detail.update({"filter_kept": filtered.kept_count, "filter_rejected": filtered.rejected_count, "filter_output": filtered.output_path})
            zero_yield_error = bool(
                filtered.kept_count <= 0
                and filtered.errors
                and all(
                    str(error) == "Filter kept zero jobs from a non-empty scrape"
                    for error in filtered.errors
                )
            )
            if not filtered.success or filtered.kept_count <= 0:
                round_detail["final_pass_added"] = 0
                if zero_yield_error:
                    # A top-up batch whose rows are all legitimately rejected is
                    # zero downstream yield, not a technical filter failure. Keep
                    # exploring the remaining bounded query windows.
                    round_detail["filter_zero_yield"] = True
                    details["rounds"].append(round_detail)
                    zero_downstream_batches += 1
                    if zero_downstream_batches >= zero_downstream_limit:
                        stop_reason = "zero_downstream_yield"
                        break
                    continue
                details["rounds"].append(round_detail)
                if not filtered.success:
                    details["errors"].extend(filtered.errors)
                    stop_reason = "topup_filter_error"
                    break
                continue

            qualified = run_precontact_qualification(filtered.output_path, suffix=f"topup_{iteration}")
            round_detail.update({
                "contact_eligible_jobs": qualified.contact_eligible_jobs,
                "precontact_rejected": qualified.rejected_jobs,
                "precontact_unverified": qualified.unverified_jobs,
                "qualification_output": qualified.output_path,
                "qualification_nonpass_output": getattr(qualified, "nonpass_path", ""),
            })
            if qualified.contact_eligible_jobs <= 0:
                round_detail["final_pass_added"] = 0
                details["rounds"].append(round_detail)
                zero_downstream_batches += 1
                if zero_downstream_batches >= zero_downstream_limit:
                    stop_reason = "zero_downstream_yield"
                    break
                continue

            before = current
            enriched = run_hiring_manager_identification(
                qualified.output_path,
                target_final_pass_leads=deficit,
                max_eligible_companies=eligible_remaining,
                exclude_company_keys=set(considered_company_keys),
                output_suffix=f"topup_{iteration}",
            )
            payload = _load(enriched.output_path)
            payloads.append(payload)
            results.append(enriched)
            all_leads.extend(payload.get("jobs", []))
            considered_company_keys.update(enriched.processed_company_keys)
            after = len(_surface_keys(_dedupe_leads_prefer_stronger(all_leads)))
            round_detail.update({
                "companies_considered": enriched.companies_considered,
                "eligible_companies": enriched.eligible_companies,
                "reviewable_added": max(0, after - before),
                "final_pass_added": max(0, after - before),
                "reviewable_total": after,
                "enrichment_output": enriched.output_path,
            })
            details["rounds"].append(round_detail)
            if after > before:
                zero_downstream_batches = 0
            else:
                zero_downstream_batches += 1
            if after >= target_final_pass_leads:
                stop_reason = "final_pass_target_reached"
                break
            if zero_downstream_batches >= zero_downstream_limit:
                stop_reason = "zero_downstream_yield"
                break
            if units <= 0:
                stop_reason = "jsearch_unit_budget_exhausted"
                break

    details.update({
        "topup_query_units": topup_units,
        "total_query_units": total_query_units,
        "final_pass_leads": len(_final_pass_keys(_dedupe_leads_prefer_stronger(all_leads))),
        "reviewable_leads": len(_surface_keys(_dedupe_leads_prefer_stronger(all_leads))),
        "stop_reason": stop_reason,
        "deficit_remaining": max(0, target_final_pass_leads - len(_surface_keys(_dedupe_leads_prefer_stronger(all_leads)))),
    })
    combined = _combine(
        results=results,
        payloads=payloads,
        target=target_final_pass_leads,
        max_eligible_companies=max_eligible_companies,
        stop_reason=stop_reason,
        topup_stats={"topup_query_units": topup_units, "topup_iterations": len(details["rounds"])},
    )
    return combined, details
