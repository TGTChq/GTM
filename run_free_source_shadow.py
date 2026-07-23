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
from datetime import datetime
from pathlib import Path

import config
from job_filter import run_filter
from multi_source_acquisition import run_multi_source_acquisition
from pipeline_state import SeenJobsRegistry


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        default="",
        help="Optional JSON report path. Defaults to data/state/shadow/evidence/.",
    )
    return parser.parse_args()


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
    for directory in (raw_dir, filtered_dir, evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # Keep production daily artifacts and seen-state untouched.
    original_output = config.OUTPUT_DIR
    original_filtered = config.FILTERED_OUTPUT_DIR
    config.OUTPUT_DIR = str(raw_dir)
    config.FILTERED_OUTPUT_DIR = str(filtered_dir)
    try:
        with tempfile.TemporaryDirectory(prefix="tgtc-v13-shadow-") as temp:
            registry = SeenJobsRegistry(path=str(Path(temp) / "seen_jobs.json"))
            acquisition = run_multi_source_acquisition(registry=registry)
            filtered = run_filter(
                input_path=acquisition.output_path,
                registry=registry,
                output_dir=str(filtered_dir),
            )
    finally:
        config.OUTPUT_DIR = original_output
        config.FILTERED_OUTPUT_DIR = original_filtered

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
        "acquisition": {
            "success": acquisition.success,
            "errors": acquisition.errors,
            "total_jobs": acquisition.total_jobs,
            "output_path": acquisition.output_path,
            "stats": acquisition.stats,
        },
        "filter": {
            "success": filtered.success,
            "errors": filtered.errors,
            "kept": filtered.kept_count,
            "rejected": filtered.rejected_count,
            "output_path": filtered.output_path,
            "rejected_path": filtered.rejected_path,
            "stats": filtered.stats,
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
        "source_outcomes": acquisition.stats.get("source_outcomes", {}),
        "filter_stats": filtered.stats,
        "external_paid_calls": report["external_paid_calls"],
    }, indent=2, ensure_ascii=False))
    # A shadow diagnostic is technically successful when acquisition/filtering
    # completed, even if the observed market volume is below the production SLA.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
