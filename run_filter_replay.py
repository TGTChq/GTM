"""Offline replay of TGTC business filters against saved JSearch results.

No network clients are imported or called. The command reuses a saved raw scrape
(or a prior accepted/rejected pair), writes replay outputs locally, and produces
role/reason quality metrics without consuming RapidAPI, Apollo, Hunter, Airtable,
or Instantly credits.
"""

from __future__ import annotations

import argparse
import csv
import json
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

import config
import job_filter
from pipeline_state import SeenJobsRegistry


def _read_jobs(path: str) -> tuple[List[Dict], Dict]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError(f"{source} does not contain a JSON list at payload['jobs']")
    return jobs, payload


def _build_input(args: argparse.Namespace, work_dir: Path) -> tuple[str, Dict]:
    if args.input:
        jobs, payload = _read_jobs(args.input)
        return args.input, {
            "input_mode": "raw_scrape",
            "input_jobs": len(jobs),
            "baseline_kept": None,
            "baseline_rejected": None,
            "source": str(Path(args.input).resolve()),
            "source_payload": payload,
        }

    accepted, accepted_payload = _read_jobs(args.accepted)
    rejected, rejected_payload = _read_jobs(args.rejected)
    jobs = accepted + rejected
    reconstructed = work_dir / "reconstructed_raw_jobs.json"
    reconstructed.write_text(
        json.dumps(
            {
                "scrape_date": datetime.now().isoformat(),
                "total_jobs": len(jobs),
                "jobs": jobs,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(reconstructed), {
        "input_mode": "accepted_rejected_pair",
        "input_jobs": len(jobs),
        "baseline_kept": len(accepted),
        "baseline_rejected": len(rejected),
        "source": [
            str(Path(args.accepted).resolve()),
            str(Path(args.rejected).resolve()),
        ],
        "source_payload": {
            "accepted": accepted_payload.get("stats", {}),
            "rejected": rejected_payload.get("stats", {}),
        },
    }


def _role_name(job: Dict) -> str:
    return str(job.get("_matched_role") or job.get("_search_role") or "Unknown")


def _summarize(kept: Iterable[Dict], rejected: Iterable[Dict]) -> Dict:
    kept = list(kept)
    rejected = list(rejected)
    by_role: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"input": 0, "kept": 0, "rejected": 0}
    )
    reason_counts: Counter[str] = Counter()
    arrangement_counts: Counter[str] = Counter()
    remote_flag_conflicts_detected = 0
    remote_flag_conflicts_kept = 0

    for job in kept:
        role = _role_name(job)
        by_role[role]["input"] += 1
        by_role[role]["kept"] += 1
        arrangement_counts[str(job.get("_work_arrangement") or "unknown")] += 1
        if job.get("job_is_remote") is False and job.get("_work_arrangement") == "remote":
            remote_flag_conflicts_detected += 1
            remote_flag_conflicts_kept += 1

    for job in rejected:
        role = _role_name(job)
        by_role[role]["input"] += 1
        by_role[role]["rejected"] += 1
        reason = str(job.get("_filter_reason") or "unknown")
        reason_counts[reason.split(":", 1)[0]] += 1
        arrangement_counts[str(job.get("_work_arrangement") or "unknown")] += 1
        if job.get("job_is_remote") is False and job.get("_work_arrangement") == "remote":
            remote_flag_conflicts_detected += 1

    rows = []
    for role, counts in by_role.items():
        input_count = counts["input"]
        rows.append(
            {
                "role": role,
                **counts,
                "keep_rate": round(counts["kept"] / input_count, 4) if input_count else 0.0,
            }
        )
    rows.sort(key=lambda row: (row["kept"], row["input"], row["role"]), reverse=True)

    return {
        "rejection_reasons": dict(reason_counts.most_common()),
        "work_arrangements": dict(arrangement_counts.most_common()),
        "remote_flag_conflicts_detected": remote_flag_conflicts_detected,
        "remote_flag_conflicts_kept": remote_flag_conflicts_kept,
        "roles": rows,
    }


def _write_role_csv(path: Path, rows: List[Dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["role", "input", "kept", "rejected", "keep_rate"],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--input", help="Saved raw jobs_YYYY-MM-DD.json")
    source_group.add_argument(
        "--accepted",
        help="Prior jobs_filtered_YYYY-MM-DD.json; requires --rejected",
    )
    parser.add_argument(
        "--rejected",
        help="Prior jobs_rejected_YYYY-MM-DD.json used with --accepted",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(config.BASE_DIR) / "data" / "replay"),
        help="Local replay output directory",
    )
    args = parser.parse_args()
    if bool(args.accepted) != bool(args.rejected):
        parser.error("--accepted and --rejected must be provided together")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tgtc-filter-replay-") as temp_dir:
        work_dir = Path(temp_dir)
        input_path, input_meta = _build_input(args, work_dir)
        registry = SeenJobsRegistry(path=str(work_dir / "seen_jobs.json"))
        result = job_filter.run_filter(
            input_path=input_path,
            registry=registry,
            output_dir=str(output_dir),
        )

    kept_payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
    rejected_payload = json.loads(Path(result.rejected_path).read_text(encoding="utf-8"))
    quality = _summarize(kept_payload["jobs"], rejected_payload["jobs"])

    stamp = datetime.now().strftime("%Y-%m-%d")
    role_csv = output_dir / f"filter_replay_by_role_{stamp}.csv"
    report_path = output_dir / f"filter_replay_report_{stamp}.json"
    _write_role_csv(role_csv, quality["roles"])

    report = {
        "mode": "offline_filter_replay",
        "external_calls": {
            "jsearch": False,
            "apollo": False,
            "hunter": False,
            "airtable": False,
            "instantly": False,
        },
        **{k: v for k, v in input_meta.items() if k != "source_payload"},
        "new_kept": result.kept_count,
        "new_rejected": result.rejected_count,
        "kept_delta": (
            result.kept_count - input_meta["baseline_kept"]
            if input_meta["baseline_kept"] is not None
            else None
        ),
        "filter_stats": result.stats,
        "quality": quality,
        "outputs": {
            "kept": result.output_path,
            "rejected": result.rejected_path,
            "role_csv": str(role_csv),
            "report": str(report_path),
        },
        "success": result.success,
        "errors": result.errors,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
