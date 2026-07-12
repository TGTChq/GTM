"""Build a manually labelable staffing validation CSV from recent pipeline outputs."""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

import config
import job_filter as jf


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default=config.STAFFING_GROUND_TRUTH_FILE)
    args = parser.parse_args()

    rows = []
    for path in sorted(Path(config.OUTPUT_DIR).glob("jobs_*.json"), reverse=True):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for job in payload.get("jobs", []):
            predicted, reason = jf.is_staffing_company(job)
            rows.append({
                "employer_name": job.get("employer_name", ""),
                "job_title": job.get("job_title", ""),
                "job_description": (job.get("job_description") or "")[:2000],
                "predicted_staffing": "yes" if predicted else "no",
                "prediction_reason": reason,
                "is_staffing": "",
                "review_notes": "",
            })

    random.Random(args.seed).shuffle(rows)
    rows = rows[: args.sample_size]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [
            "employer_name", "job_title", "job_description", "predicted_staffing",
            "prediction_reason", "is_staffing", "review_notes",
        ])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {output}")
    print("Label is_staffing as yes/no, then rerun the filter audit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
