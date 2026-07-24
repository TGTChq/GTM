"""Offline counterfactual replay for the v1.4.4 controlled-recall policies.

The replay never calls JSearch, Apollo, Hunter, Airtable, Instantly, or public
job sources. It compares the same raw inventory with the three review-lane
policies disabled and enabled, then lists every recovered and lost posting.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator

import config
from job_filter import assess_pre_enrichment_viability
from job_quality import normalize_job_identity

_POLICY_FLAGS = (
    "ALLOW_ACTIVE_GREENHOUSE_UNKNOWN_AGE_REVIEW",
    "ALLOW_GLOBAL_REMOTE_US_INCLUSIVE_REVIEW",
    "ALLOW_STRUCTURED_IDENTITY_CONFLICT_REVIEW",
)
_REVIEW_FLAGS = (
    "_freshness_review_required",
    "_global_remote_review_required",
    "_employer_identity_review_required",
    "_employer_identity_repaired",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", help="Raw jobs JSON file")
    parser.add_argument("--output", help="Output report path")
    parser.add_argument("--max-age-days", type=int, default=None)
    return parser.parse_args()


def _latest_shadow_raw() -> Path:
    history = Path(config.STATE_DIR) / "shadow" / "raw" / "history"
    candidates = sorted(history.glob("jobs_multisource_*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(
            "No shadow multi-source raw archive found; pass the JSON path explicitly"
        )
    return candidates[0]


def _load_jobs(path: Path) -> list[Dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        raise ValueError("Input JSON must be a jobs list or contain a jobs list")
    return [dict(job) for job in jobs if isinstance(job, dict)]


@contextmanager
def _policy_state(enabled: bool) -> Iterator[None]:
    original = {name: getattr(config, name) for name in _POLICY_FLAGS}
    try:
        for name in _POLICY_FLAGS:
            setattr(config, name, enabled)
        yield
    finally:
        for name, value in original.items():
            setattr(config, name, value)


def _key(job: Dict, index: int) -> str:
    return str(job.get("job_id") or "").strip() or "|".join(
        str(job.get(field) or "").strip().lower()
        for field in ("employer_name", "job_title", "job_apply_link")
    ) or f"row:{index}"


def _evaluate(jobs: Iterable[Dict], *, enabled: bool, max_age_days: int) -> Dict[str, Dict]:
    decisions: Dict[str, Dict] = {}
    with _policy_state(enabled):
        for index, source in enumerate(jobs):
            job = dict(source)
            normalize_job_identity(job)
            assessment = assess_pre_enrichment_viability(
                job, max_age_days=max_age_days
            )
            decisions[_key(job, index)] = {
                "eligible": bool(assessment.eligible),
                "stat": assessment.stat_name,
                "reason": assessment.reason,
                "company": job.get("employer_name"),
                "title": job.get("job_title"),
                "source": job.get("_acquisition_source") or job.get("job_publisher"),
                "url": job.get("job_apply_link") or job.get("official_job_url"),
                "review_flags": {
                    name: job.get(name) for name in _REVIEW_FLAGS if job.get(name)
                },
            }
    return decisions


def main() -> int:
    args = _parse_args()
    input_path = Path(args.input) if args.input else _latest_shadow_raw()
    jobs = _load_jobs(input_path)
    max_age = int(args.max_age_days or config.PRIMARY_MAX_JOB_AGE_DAYS)
    strict = _evaluate(jobs, enabled=False, max_age_days=max_age)
    recovery = _evaluate(jobs, enabled=True, max_age_days=max_age)

    recovered = []
    lost = []
    unchanged_eligible = 0
    for key, before in strict.items():
        after = recovery[key]
        row = {"key": key, "before": before, "after": after}
        if not before["eligible"] and after["eligible"]:
            recovered.append(row)
        elif before["eligible"] and not after["eligible"]:
            lost.append(row)
        elif before["eligible"] and after["eligible"]:
            unchanged_eligible += 1

    report = {
        "generated_at": datetime.now().isoformat(),
        "input": str(input_path),
        "input_jobs": len(jobs),
        "max_age_days": max_age,
        "external_calls": {
            "jsearch": 0,
            "apollo": 0,
            "hunter": 0,
            "airtable": 0,
            "instantly": 0,
            "public_job_sources": 0,
        },
        "strict_eligible": sum(item["eligible"] for item in strict.values()),
        "recovery_eligible": sum(item["eligible"] for item in recovery.values()),
        "recovered_count": len(recovered),
        "lost_count": len(lost),
        "unchanged_eligible": unchanged_eligible,
        "recovered": recovered,
        "lost": lost,
    }
    output = (
        Path(args.output)
        if args.output
        else input_path.with_name(input_path.stem + "_counterfactual_recall.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({**report, "recovered": recovered[:20], "lost": lost[:20]}, indent=2))
    print(f"Full report: {output}")
    return 0 if not lost else 2


if __name__ == "__main__":
    raise SystemExit(main())
