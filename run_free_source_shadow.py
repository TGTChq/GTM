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
        "mode": "free_multi_source_shadow",
        "external_paid_calls": {
            "apollo": 0,
            "hunter": 0,
            "airtable": 0,
            "instantly": 0,
            "jsearch": 0,
        },
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
        "interpretation": interpretation,
        "external_paid_calls": report["external_paid_calls"],
    }, indent=2, ensure_ascii=False))
    # Low market volume is diagnostic, not a technical failure. Acquisition,
    # filtering, and the free pre-contact gates themselves must still complete.
    return 0 if acquisition.success and filtered.success and qualified.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
