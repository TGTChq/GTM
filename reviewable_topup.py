"""Bounded closed-loop JSearch top-up for the daily reviewable-lead target."""

from __future__ import annotations

import json
import logging
import math
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import config
from hiring_manager import (
    Step3Result,
    company_key_for_job,
    run_hiring_manager_identification,
)
from job_filter import run_filter
from jsearch_scraper import ScrapeResult, run_targeted_topup_scrape
from pipeline_state import SeenJobsRegistry

logger = logging.getLogger(__name__)


def _load(path: str) -> Dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _reviewable_lead_keys(leads: Iterable[Dict]) -> set[str]:
    return {
        str(lead.get("lead_key"))
        for lead in leads
        if lead.get("_step3_status") == "found"
        and lead.get("hiring_manager_email")
        and lead.get("lead_key")
    }


def _lead_dedupe_key(lead: Dict) -> Tuple[str, ...]:
    if lead.get("lead_key"):
        return ("lead", str(lead["lead_key"]))
    return (
        "diagnostic",
        company_key_for_job(lead),
        str(lead.get("_role_bucket") or ""),
        str(lead.get("_step3_status") or ""),
        str(lead.get("_step3_reason") or ""),
        str(lead.get("job_id") or ""),
    )


def _dedupe_leads(leads: Iterable[Dict]) -> List[Dict]:
    output: List[Dict] = []
    seen: set[Tuple[str, ...]] = set()
    for lead in leads:
        key = _lead_dedupe_key(lead)
        if key in seen:
            continue
        seen.add(key)
        output.append(lead)
    return output


def _job_ref_key(job: Dict) -> Tuple[str, ...]:
    job_id = str(job.get("job_id") or "").strip()
    if job_id:
        return ("id", job_id)
    return (
        "fallback",
        company_key_for_job(job),
        str(job.get("job_title") or "").strip().lower(),
    )


def _dedupe_job_refs(jobs: Iterable[Dict]) -> List[Dict]:
    output: List[Dict] = []
    seen: set[Tuple[str, ...]] = set()
    for job in jobs:
        key = _job_ref_key(job)
        if key in seen:
            continue
        seen.add(key)
        output.append(job)
    return output


def _merge_numeric_stats(target: Dict[str, int], source: Dict) -> None:
    for key, value in (source or {}).items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            target[key] += value


def _merge_query_metrics(base: Dict[str, Dict], increment: Dict[str, Dict]) -> Dict[str, Dict]:
    merged = deepcopy(base or {})
    for role, incoming in (increment or {}).items():
        current = merged.setdefault(
            role,
            {
                "canonical_role": incoming.get("canonical_role"),
                "pages": [],
            },
        )
        if incoming.get("canonical_role"):
            current["canonical_role"] = incoming["canonical_role"]
        current.setdefault("pages", []).extend(deepcopy(incoming.get("pages") or []))
        for key, value in incoming.items():
            if key in {"canonical_role", "pages", "status", "error", "quota_remaining_after"}:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                current[key] = current.get(key, 0) + value
        if incoming.get("status") == "error":
            current["status"] = "error"
            current["error"] = " | ".join(
                filter(None, [str(current.get("error") or ""), str(incoming.get("error") or "")])
            )
        else:
            current.setdefault("status", incoming.get("status", "ok"))
        if incoming.get("quota_remaining_after") is not None:
            current["quota_remaining_after"] = incoming["quota_remaining_after"]
    return merged


def _preferred_search_roles(leads: Iterable[Dict]) -> List[str]:
    roles = [
        str(lead.get("_search_role"))
        for lead in leads
        if lead.get("_search_role")
        and lead.get("_step3_status") == "found"
        and lead.get("hiring_manager_email")
    ]
    counts = Counter(roles)
    return [role for role, count in counts.most_common() for _ in range(count)]


def _combine_results(
    *,
    results: List[Step3Result],
    payloads: List[Dict],
    target_reviewable_leads: int,
    max_eligible_companies: int,
    stop_reason: str,
    topup_stats: Dict,
) -> Step3Result:
    all_leads = _dedupe_leads(
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
    reviewable = len(_reviewable_lead_keys(all_leads))
    cumulative_stats: Dict[str, int] = defaultdict(int)
    for result in results:
        _merge_numeric_stats(cumulative_stats, result.stats)
    _merge_numeric_stats(cumulative_stats, topup_stats)

    excluded_buckets = sum(
        1 for lead in all_leads if lead.get("_step3_status") == "excluded"
    )
    eligible_leads = [
        lead for lead in all_leads if lead.get("_step3_status") != "excluded"
    ]
    identified = sum(1 for lead in eligible_leads if lead.get("hiring_manager_name"))
    contactable = sum(1 for lead in eligible_leads if lead.get("_step3_status") == "found")
    eligible_buckets = len(eligible_leads)
    match_rate = identified / eligible_buckets if eligible_buckets else 0.0
    contactable_rate = contactable / eligible_buckets if eligible_buckets else 0.0
    companies_considered = sum(result.companies_considered for result in results)
    eligible_companies = sum(result.eligible_companies for result in results)
    excluded_companies = sum(
        result.company_criteria_excluded_companies for result in results
    )
    target_reached = reviewable >= target_reviewable_leads
    limit_reached = eligible_companies >= max_eligible_companies

    output_path = str(
        Path(config.STEP3_OUTPUT_DIR)
        / f"jobs_enriched_{datetime.now():%Y-%m-%d}_combined.json"
    )
    combined_payload = {
        "run_date": datetime.now().isoformat(),
        "source_file": "closed_loop_reviewable_topup",
        "source_total_jobs": len(processed_refs),
        "total_input_jobs": len(processed_refs),
        "total_output_leads": len(all_leads),
        "companies_considered": companies_considered,
        "eligible_companies": eligible_companies,
        "company_criteria_excluded_companies": excluded_companies,
        "target_reviewable_leads": target_reviewable_leads,
        "reviewable_leads": reviewable,
        "reviewable_target_reached": target_reached,
        "max_eligible_companies": max_eligible_companies,
        "eligible_company_limit_reached": limit_reached,
        "stop_reason": stop_reason,
        "target_reached": target_reached,
        "company_criteria_excluded": excluded_buckets,
        "eligible_company_buckets": eligible_buckets,
        "hiring_manager_identified": identified,
        "hiring_manager_not_identified": eligible_buckets - identified,
        "hiring_manager_identification_rate": round(match_rate, 4),
        "contactable_hiring_managers": contactable,
        "uncontactable_hiring_managers": eligible_buckets - contactable,
        "contactable_rate": round(contactable_rate, 4),
        "processed_job_refs": processed_refs,
        "processed_company_keys": processed_company_keys,
        "hiring_manager_found": identified,
        "hiring_manager_not_found": eligible_buckets - identified,
        "match_rate": round(match_rate, 4),
        "stats": dict(cumulative_stats),
        "jobs": all_leads,
    }
    Path(output_path).write_text(
        json.dumps(combined_payload, indent=2), encoding="utf-8"
    )

    return Step3Result(
        output_path=output_path,
        total_input_jobs=len(processed_refs),
        total_output_leads=len(all_leads),
        company_criteria_excluded=excluded_buckets,
        hiring_manager_found=identified,
        hiring_manager_not_found=eligible_buckets - identified,
        match_rate=match_rate,
        contactable_hiring_managers=contactable,
        uncontactable_hiring_managers=eligible_buckets - contactable,
        contactable_rate=contactable_rate,
        companies_considered=companies_considered,
        eligible_companies=eligible_companies,
        company_criteria_excluded_companies=excluded_companies,
        target_reviewable_leads=target_reviewable_leads,
        reviewable_leads=reviewable,
        reviewable_target_reached=target_reached,
        max_eligible_companies=max_eligible_companies,
        eligible_company_limit_reached=limit_reached,
        target_reached=target_reached,
        stop_reason=stop_reason,
        processed_company_keys=processed_company_keys,
        stats=dict(cumulative_stats),
        # Top-up is best-effort. A late optional round must never discard the
        # valid leads produced by the initial production pass.
        success=results[0].success,
        errors=list(results[0].errors),
    )


def run_reviewable_topup(
    *,
    initial_scrape: ScrapeResult,
    initial_enriched: Step3Result,
    registry: SeenJobsRegistry,
    target_reviewable_leads: int,
    max_eligible_companies: int,
) -> tuple[Step3Result, Dict]:
    """Run finite targeted top-up rounds and return one combined Step 3 result."""
    initial_payload = _load(initial_enriched.output_path)
    payloads = [initial_payload]
    results = [initial_enriched]
    all_leads = list(initial_payload.get("jobs", []))
    considered_company_keys = set(initial_enriched.processed_company_keys)
    selected_job_ids = {
        str(job.get("job_id"))
        for job in _load(initial_scrape.output_path).get("jobs", [])
        if job.get("job_id")
    }
    query_metrics = deepcopy(initial_scrape.stats.get("query_metrics", {}))
    total_query_units = int(initial_scrape.stats.get("estimated_request_units", 0))
    topup_units = 0
    topup_details: Dict = {
        "enabled": True,
        "initial_reviewable_leads": initial_enriched.reviewable_leads,
        "target_reviewable_leads": target_reviewable_leads,
        "initial_query_units": total_query_units,
        "rounds": [],
        "errors": [],
    }
    stop_reason = initial_enriched.stop_reason

    if initial_enriched.reviewable_leads >= target_reviewable_leads:
        stop_reason = "reviewable_lead_target_reached_initial_pass"
    else:
        for round_number in range(1, config.JSEARCH_TOPUP_MAX_ROUNDS + 1):
            current_reviewable = len(_reviewable_lead_keys(all_leads))
            if current_reviewable >= target_reviewable_leads:
                stop_reason = "reviewable_lead_target_reached"
                break
            eligible_used = sum(result.eligible_companies for result in results)
            eligible_remaining = max_eligible_companies - eligible_used
            if eligible_remaining <= 0:
                stop_reason = "eligible_company_safety_cap_reached"
                break
            total_budget = config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN
            budget_remaining = (
                max(0, total_budget - total_query_units)
                if total_budget > 0
                else config.JSEARCH_TOPUP_MAX_UNITS_PER_ROUND
            )
            if budget_remaining <= 0:
                stop_reason = "jsearch_unit_budget_exhausted"
                break
            round_budget = min(
                budget_remaining, config.JSEARCH_TOPUP_MAX_UNITS_PER_ROUND
            )
            reviewable_remaining = target_reviewable_leads - current_reviewable
            observed_ratio = (
                sum(result.companies_considered for result in results) / current_reviewable
                if current_reviewable
                else config.JSEARCH_TOPUP_PREFILTER_MULTIPLIER
            )
            candidate_multiplier = max(
                float(config.JSEARCH_TOPUP_PREFILTER_MULTIPLIER),
                float(observed_ratio),
            )
            target_prefilter = max(
                config.JSEARCH_TOPUP_MIN_PREFILTER_TARGET,
                int(math.ceil(reviewable_remaining * candidate_multiplier)),
            )
            logger.info(
                "=== REVIEWABLE TOP-UP ROUND %d === target_remaining=%d "
                "unit_budget=%d prefilter_target=%d",
                round_number,
                reviewable_remaining,
                round_budget,
                target_prefilter,
            )
            try:
                topup_scrape = run_targeted_topup_scrape(
                    registry=registry,
                    prior_query_metrics=query_metrics,
                    exclude_job_ids=selected_job_ids,
                    preferred_search_roles=_preferred_search_roles(all_leads),
                    unit_budget=round_budget,
                    target_prefilter_viable=target_prefilter,
                    round_number=round_number,
                )
            except Exception as exc:  # Best-effort: preserve initial leads.
                logger.exception("Reviewable top-up round %d scrape failed", round_number)
                topup_details["errors"].append(f"round_{round_number}_scrape: {exc}")
                stop_reason = "topup_scrape_error"
                break

            round_units = int(topup_scrape.stats.get("estimated_request_units", 0))
            topup_units += round_units
            total_query_units += round_units
            query_metrics = _merge_query_metrics(
                query_metrics, topup_scrape.stats.get("query_metrics", {})
            )
            topup_payload = _load(topup_scrape.output_path)
            selected_job_ids.update(
                str(job.get("job_id"))
                for job in topup_payload.get("jobs", [])
                if job.get("job_id")
            )
            round_detail = {
                "round": round_number,
                "query_units": round_units,
                "queries_attempted": topup_scrape.stats.get("queries_attempted", 0),
                "queries_succeeded": topup_scrape.stats.get("queries_succeeded", 0),
                "scraped_jobs": topup_scrape.total_jobs,
                "prefilter_viable_added": topup_scrape.stats.get(
                    "topup_new_prefilter_viable", 0
                ),
                "scrape_stop_reason": topup_scrape.stats.get("topup_stop_reason", ""),
                "scrape_output": topup_scrape.output_path,
                "scrape_errors": topup_scrape.errors,
            }
            if topup_scrape.errors:
                topup_details["errors"].extend(
                    f"round_{round_number}_scrape: {error}"
                    for error in topup_scrape.errors
                )
            if topup_scrape.total_jobs <= 0:
                round_detail.update({"filtered_jobs": 0, "reviewable_added": 0})
                topup_details["rounds"].append(round_detail)
                stop_reason = "topup_no_new_jobs"
                break

            filter_dir = (
                Path(config.FILTERED_OUTPUT_DIR)
                / f"topup_{datetime.now():%Y-%m-%d}_r{round_number}"
            )
            filtered = run_filter(
                input_path=topup_scrape.output_path,
                registry=registry,
                output_dir=str(filter_dir),
            )
            round_detail["filtered_jobs"] = filtered.kept_count
            round_detail["filter_rejected"] = filtered.rejected_count
            round_detail["filter_output"] = filtered.output_path
            if filtered.kept_count <= 0:
                round_detail["reviewable_added"] = 0
                topup_details["rounds"].append(round_detail)
                stop_reason = "topup_no_new_filtered_jobs"
                break

            before_reviewable = len(_reviewable_lead_keys(all_leads))
            try:
                topup_enriched = run_hiring_manager_identification(
                    filtered.output_path,
                    target_reviewable_leads=max(
                        1, target_reviewable_leads - before_reviewable
                    ),
                    max_eligible_companies=max(1, eligible_remaining),
                    exclude_company_keys=considered_company_keys,
                    output_suffix=f"topup_r{round_number}",
                )
            except Exception as exc:  # Preserve earlier successful rounds.
                logger.exception("Reviewable top-up round %d enrichment failed", round_number)
                topup_details["errors"].append(
                    f"round_{round_number}_enrichment: {exc}"
                )
                round_detail["enrichment_error"] = str(exc)
                topup_details["rounds"].append(round_detail)
                stop_reason = "topup_enrichment_error"
                break

            if topup_enriched.errors:
                topup_details["errors"].extend(
                    f"round_{round_number}_enrichment: {error}"
                    for error in topup_enriched.errors
                )
            topup_enriched_payload = _load(topup_enriched.output_path)
            results.append(topup_enriched)
            payloads.append(topup_enriched_payload)
            all_leads = _dedupe_leads([
                *all_leads,
                *topup_enriched_payload.get("jobs", []),
            ])
            considered_company_keys.update(topup_enriched.processed_company_keys)
            after_reviewable = len(_reviewable_lead_keys(all_leads))
            round_detail.update({
                "companies_considered": topup_enriched.companies_considered,
                "eligible_companies": topup_enriched.eligible_companies,
                "reviewable_added": after_reviewable - before_reviewable,
                "cumulative_reviewable": after_reviewable,
                "enrichment_stop_reason": topup_enriched.stop_reason,
                "enrichment_output": topup_enriched.output_path,
            })
            topup_details["rounds"].append(round_detail)
            logger.info(
                "Reviewable top-up round %d result: +%d reviewable (%d/%d), "
                "%d companies considered, %d eligible",
                round_number,
                after_reviewable - before_reviewable,
                after_reviewable,
                target_reviewable_leads,
                topup_enriched.companies_considered,
                topup_enriched.eligible_companies,
            )
            if after_reviewable >= target_reviewable_leads:
                stop_reason = "reviewable_lead_target_reached"
                break
            if topup_enriched.companies_considered <= 0:
                stop_reason = "topup_no_new_companies"
                break
        else:
            stop_reason = "topup_round_limit_reached"

    final_reviewable = len(_reviewable_lead_keys(all_leads))
    topup_summary_stats = {
        "topup_rounds_completed": len(topup_details["rounds"]),
        "topup_query_units": topup_units,
        "topup_reviewable_added": max(
            0, final_reviewable - initial_enriched.reviewable_leads
        ),
    }
    combined = _combine_results(
        results=results,
        payloads=payloads,
        target_reviewable_leads=target_reviewable_leads,
        max_eligible_companies=max_eligible_companies,
        stop_reason=stop_reason,
        topup_stats=topup_summary_stats,
    )
    topup_details.update({
        "topup_query_units": topup_units,
        "total_query_units": total_query_units,
        "final_reviewable_leads": combined.reviewable_leads,
        "reviewable_target_reached": combined.reviewable_target_reached,
        "stop_reason": stop_reason,
        "combined_output": combined.output_path,
    })
    return combined, topup_details
