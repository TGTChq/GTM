"""Run free acquisition and local filtering without paid enrichment or writes.

This diagnostic never calls Apollo, Hunter, Airtable, or Instantly. It uses a
throwaway seen-jobs registry and writes only local shadow artifacts plus the ATS
board registry, whose contents are discovered automatically from public URLs.
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import config
from company_identity import normalize_company_name
from domain_utils import normalize_company_domain
from job_filter import run_filter
from multi_source_acquisition import run_multi_source_acquisition
from qualification_pipeline import run_precontact_qualification
from pipeline_state import SeenJobsRegistry


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        default="",
        help="Optional JSON report path. Defaults to data/state/shadow/evidence/.",
    )
    return parser.parse_args()




def _shadow_company_metrics(filtered_path: str) -> dict:
    """Summarize kept postings by employer without running paid enrichment."""
    payload = json.loads(Path(filtered_path).read_text(encoding="utf-8"))
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    counts: Counter[str] = Counter()
    labels: dict[str, str] = {}
    missing_identity = 0
    missing_domain = 0

    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        domain = normalize_company_domain(
            job.get("_employer_domain_input")
            or job.get("employer_domain")
            or job.get("employer_website")
            or ""
        )
        company = str(job.get("employer_name") or job.get("company_name") or "").strip()
        company_key = normalize_company_name(company).replace(" ", "")
        if domain:
            key = f"domain:{domain}"
        elif company_key:
            key = f"name:{company_key}"
            missing_domain += 1
        else:
            missing_identity += 1
            continue
        counts[key] += 1
        labels.setdefault(key, company or domain)

    clusters = [
        {"company": labels[key], "kept_jobs": count}
        for key, count in counts.most_common(15)
        if count > 1
    ]
    return {
        "unique_companies": len(counts),
        "jobs_with_company_identity": sum(counts.values()),
        "jobs_missing_company_identity": missing_identity,
        "jobs_missing_employer_domain": missing_domain,
        "companies_with_multiple_kept_jobs": sum(1 for count in counts.values() if count > 1),
        "extra_jobs_above_one_per_company": sum(max(0, count - 1) for count in counts.values()),
        "largest_company_clusters": clusters,
    }



def _shadow_rejection_diagnostics(
    rejected_path: str, *, samples_per_reason: int = 3
) -> dict:
    payload = json.loads(Path(rejected_path).read_text(encoding="utf-8"))
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    reason_counts: Counter[str] = Counter()
    source_counts: dict[str, Counter[str]] = {}
    samples: dict[str, list[dict]] = {}
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        reason = str(job.get("_filter_reason") or "unknown").strip() or "unknown"
        source = str(job.get("_acquisition_source") or job.get("job_publisher") or "unknown").strip() or "unknown"
        reason_counts[reason] += 1
        source_counts.setdefault(reason, Counter())[source] += 1
        bucket = samples.setdefault(reason, [])
        if len(bucket) < max(1, samples_per_reason):
            bucket.append({
                "company": str(job.get("employer_name") or ""),
                "title": str(job.get("job_title") or ""),
                "source": source,
                "location": str(job.get("job_location") or ""),
                "posted_at": str(job.get("job_posted_at_datetime_utc") or ""),
                "url": str(job.get("canonical_source_url") or job.get("job_apply_link") or ""),
            })
    top = []
    for reason, count in reason_counts.most_common(25):
        top.append({
            "reason": reason,
            "jobs": count,
            "sources": dict(source_counts.get(reason, Counter()).most_common(10)),
            "samples": samples.get(reason, []),
        })
    return {
        "total_rejected": sum(reason_counts.values()),
        "unique_exact_reasons": len(reason_counts),
        "top_exact_reasons": top,
    }


def _jsearch_request_metrics(acquisition_stats: dict) -> dict:
    source_metrics = acquisition_stats.get("source_metrics", {})
    source = source_metrics.get("jsearch", {}) if isinstance(source_metrics, dict) else {}
    jsearch = acquisition_stats.get("jsearch", {})
    nested = jsearch.get("stats", {}) if isinstance(jsearch, dict) else {}
    attempted = int(source.get("requests_attempted", nested.get("queries_attempted", 0)) or 0)
    succeeded = int(source.get("requests_succeeded", nested.get("queries_succeeded", 0)) or 0)
    units = int(nested.get("estimated_request_units", acquisition_stats.get("estimated_request_units", 0)) or 0)
    jobs = int(source.get("normalized_jobs", jsearch.get("jobs", 0) if isinstance(jsearch, dict) else 0) or 0)
    return {
        "enabled": bool(jsearch.get("enabled")) if isinstance(jsearch, dict) else False,
        "attempted": bool(jsearch.get("attempted")) if isinstance(jsearch, dict) else attempted > 0,
        "requests_attempted": attempted,
        "requests_succeeded": succeeded,
        "estimated_request_units": units,
        "jobs_normalized": jobs,
        "skipped_reason": str(jsearch.get("skipped_reason") or "") if isinstance(jsearch, dict) else "",
        "errors": list(jsearch.get("errors") or []) if isinstance(jsearch, dict) else [],
    }


def _shadow_recall_recovery_metrics(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    flags = {
        "greenhouse_unknown_age_review": "_freshness_review_required",
        "global_remote_includes_us_review": "_global_remote_review_required",
        "structured_identity_conflict_review": "_employer_identity_review_required",
        "ats_identity_repaired": "_employer_identity_repaired",
    }
    counts = {}
    samples = {}
    for label, flag in flags.items():
        matched = [job for job in jobs if isinstance(job, dict) and job.get(flag)]
        counts[label] = len(matched)
        samples[label] = [
            {
                "company": job.get("employer_name"),
                "title": job.get("job_title"),
                "source": job.get("_acquisition_source") or job.get("job_publisher"),
                "url": job.get("job_apply_link") or job.get("official_job_url"),
            }
            for job in matched[:5]
        ]
    return {
        "review_lane_counts": counts,
        "review_lane_total": sum(counts.values()),
        "samples": samples,
        "safety_contract": (
            "Recovered records still require Job, Role, Account, Contact, Email, "
            "CRM, human approval, and pre-send source revalidation."
        ),
    }


def _shadow_funnel_diagnostics(
    *,
    acquired: int,
    filter_stats: dict,
    filtered_company_metrics: dict,
    qualified_company_metrics: dict,
) -> dict:
    kept = int(filter_stats.get("kept", 0) or 0)
    contact_jobs = int(
        qualified_company_metrics.get("jobs_with_company_identity", 0) or 0
    )
    contact_companies = int(qualified_company_metrics.get("unique_companies", 0) or 0)
    target = max(1, int(config.get_final_pass_target()))
    rejection_families = [
        {"reason": key, "jobs": int(value or 0)}
        for key, value in filter_stats.items()
        if str(key).startswith("excluded_") and int(value or 0) > 0
    ]
    rejection_families.sort(key=lambda item: item["jobs"], reverse=True)
    modality_exclusions = int(filter_stats.get("excluded_in_person", 0) or 0)
    return {
        "acquired_jobs": int(acquired),
        "filter_kept_jobs": kept,
        "filter_kept_rate": round(kept / acquired, 4) if acquired else 0.0,
        "contact_eligible_jobs": contact_jobs,
        "contact_eligible_unique_companies": contact_companies,
        "kept_to_contact_eligible_rate": round(contact_jobs / kept, 4) if kept else 0.0,
        "minimum_final_pass_target": target,
        "precontact_unique_companies_above_minimum": contact_companies >= target,
        "final_pass_not_computed_in_shadow": True,
        "modality_exclusions": modality_exclusions,
        "modality_was_not_the_volume_constraint": modality_exclusions == 0,
        "top_filter_loss_families": rejection_families[:10],
        "filtered_unique_companies": int(
            filtered_company_metrics.get("unique_companies", 0) or 0
        ),
        "deficit_recovery": {
            "primary_window_days": "0-14",
            "age_recovery_window_days": "15-30",
            "age_recovery_enabled": bool(config.AGE_RECOVERY_ENABLED),
            "jsearch_microbatch_topup_enabled": bool(
                config.MULTI_SOURCE_JSEARCH_ENABLED
                and config.MULTI_SOURCE_JSEARCH_TOPUP_ENABLED
            ),
            "multi_source_iteration_limit": int(
                config.MULTI_SOURCE_FINAL_PASS_MAX_TOPUP_ITERATIONS
            ),
            "iteration_limit_zero_means": (
                "bounded by target, request budget, runtime, inventory, "
                "and downstream yield"
            ),
        },
    }


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    shadow_root = Path(config.STATE_DIR) / "shadow"
    raw_dir = shadow_root / "raw"
    filtered_dir = shadow_root / "filtered"
    evidence_dir = shadow_root / "evidence"
    qualified_dir = shadow_root / "qualified"
    source_cache_dir = shadow_root / "source_cache"
    for directory in (raw_dir, filtered_dir, evidence_dir, qualified_dir, source_cache_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # Keep production daily artifacts and seen-state untouched.
    original_output = config.OUTPUT_DIR
    original_filtered = config.FILTERED_OUTPUT_DIR
    original_source_cache = config.SOURCE_CACHE_DIR
    config.OUTPUT_DIR = str(raw_dir)
    config.FILTERED_OUTPUT_DIR = str(filtered_dir)
    config.SOURCE_CACHE_DIR = str(source_cache_dir)
    try:
        with tempfile.TemporaryDirectory(prefix="tgtc-v13-shadow-") as temp:
            registry = SeenJobsRegistry(path=str(Path(temp) / "seen_jobs.json"))
            acquisition = run_multi_source_acquisition(
                registry=registry,
                force_ats_refresh=True,
                ats_board_limit=max(1, config.ATS_SHADOW_FORCE_REFRESH_MAX_BOARDS),
            )
            filtered = run_filter(
                input_path=acquisition.output_path,
                registry=registry,
                output_dir=str(filtered_dir),
                max_age_days=config.PRIMARY_MAX_JOB_AGE_DAYS,
            )
            qualified = run_precontact_qualification(
                filtered.output_path,
                output_dir=str(qualified_dir),
                suffix="shadow",
                fetch_sources=True,
            )
    finally:
        config.OUTPUT_DIR = original_output
        config.FILTERED_OUTPUT_DIR = original_filtered
        config.SOURCE_CACHE_DIR = original_source_cache

    filtered_company_metrics = _shadow_company_metrics(filtered.output_path)
    qualified_company_metrics = _shadow_company_metrics(qualified.output_path)
    jsearch_requests = _jsearch_request_metrics(acquisition.stats)
    rejection_diagnostics = _shadow_rejection_diagnostics(filtered.rejected_path)
    recall_recovery = _shadow_recall_recovery_metrics(filtered.output_path)
    funnel_diagnostics = _shadow_funnel_diagnostics(
        acquired=acquisition.total_jobs,
        filter_stats=filtered.stats,
        filtered_company_metrics=filtered_company_metrics,
        qualified_company_metrics=qualified_company_metrics,
    )

    interpretation = {
        "filter_kept_are_not_final_leads": True,
        "contact_eligible_are_not_final_pass": True,
        "account_gate_run": False,
        "contact_gate_run": False,
        "email_gate_run": False,
        "airtable_write_run": False,
        "final_pass_computed": False,
        "meaning": (
            "Shadow validates public acquisition plus zero-credit Job and Role Gates. "
            "Verified Himalayas profiles can also reject clearly out-of-range company sizes "
            "and excluded industries for free. Remaining firmographics, hiring-manager, email "
            "and FINAL_PASS still require one controlled production run."
        ),
        "next_required_stage": "review shadow quality, then run one controlled production execution",
    }
    report = {
        "generated_at": datetime.now().isoformat(),
        "mode": "multi_source_shadow",
        "external_api_requests": {
            "apollo": 0,
            "hunter": 0,
            "airtable": 0,
            "instantly": 0,
            "jsearch": jsearch_requests,
        },
        # Compatibility field retained for older report consumers. JSearch now
        # reports actual attempted requests instead of a hard-coded zero.
        "external_paid_calls": {
            "apollo": 0,
            "hunter": 0,
            "airtable": 0,
            "instantly": 0,
            "jsearch": jsearch_requests["requests_attempted"],
        },
        "funnel_diagnostics": funnel_diagnostics,
        "rejection_diagnostics": rejection_diagnostics,
        "recall_recovery": recall_recovery,
        "interpretation": interpretation,
        "acquisition": {
            "success": acquisition.success,
            "errors": acquisition.errors,
            "total_jobs": acquisition.total_jobs,
            "output_path": acquisition.output_path,
            "stats": acquisition.stats,
        },
        "filtered_company_metrics": filtered_company_metrics,
        "contact_eligible_company_metrics": qualified_company_metrics,
        "filter": {
            "success": filtered.success,
            "errors": filtered.errors,
            "kept": filtered.kept_count,
            "rejected": filtered.rejected_count,
            "output_path": filtered.output_path,
            "rejected_path": filtered.rejected_path,
            "stats": filtered.stats,
        },
        "qualification": {
            "success": qualified.success,
            "errors": qualified.errors,
            "input_jobs": qualified.input_jobs,
            "contact_eligible_jobs": qualified.contact_eligible_jobs,
            "rejected_jobs": qualified.rejected_jobs,
            "unverified_jobs": qualified.unverified_jobs,
            "needs_check_jobs": qualified.needs_check_jobs,
            "output_path": qualified.output_path,
            "nonpass_path": qualified.nonpass_path,
            "stats": qualified.stats,
        },
    }
    report_path = Path(args.report) if args.report else evidence_dir / f"shadow_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({
        "report": str(report_path),
        "acquired": acquisition.total_jobs,
        "filter_kept": filtered.kept_count,
        "filter_rejected": filtered.rejected_count,
        "contact_eligible_jobs": qualified.contact_eligible_jobs,
        "source_outcomes": acquisition.stats.get("source_outcomes", {}),
        "himalayas_company_profiles": acquisition.stats.get(
            "himalayas_company_profiles", {}
        ),
        "filtered_company_metrics": filtered_company_metrics,
        "contact_eligible_company_metrics": qualified_company_metrics,
        "filter_stats": filtered.stats,
        "qualification_stats": qualified.stats,
        "funnel_diagnostics": funnel_diagnostics,
        "rejection_diagnostics": rejection_diagnostics,
        "interpretation": interpretation,
        "external_api_requests": report["external_api_requests"],
        "external_paid_calls": report["external_paid_calls"],
    }, indent=2, ensure_ascii=False))
    # Low market volume is diagnostic, not a technical failure. Acquisition,
    # filtering, and the free pre-contact gates themselves must still complete.
    return 0 if acquisition.success and filtered.success and qualified.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
