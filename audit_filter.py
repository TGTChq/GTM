"""Step 2 quality audit.

The old audit measured staffing recall against labels produced by the same rule,
which can look perfect while missing unknown staffing companies. This version
separates deterministic consistency checks from an optional independent,
manually labeled ground-truth set.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import config
import job_filter as jf

logger = logging.getLogger(__name__)


@dataclass
class AuditResult:
    report_path: str
    passed: bool
    failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    summary: Dict = field(default_factory=dict)


def _load_json(path: str) -> Dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _bool_label(value: str) -> Optional[bool]:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "staffing"}:
        return True
    if normalized in {"0", "false", "no", "n", "non_staffing", "not staffing"}:
        return False
    return None


def evaluate_staffing_ground_truth(path: str) -> Dict:
    source = Path(path)
    if not source.exists():
        return {"available": False}

    tp = fp = tn = fn = skipped = 0
    examples = []
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"employer_name", "is_staffing"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(
                f"Ground truth CSV must contain columns {sorted(required)}; "
                f"found {reader.fieldnames}"
            )
        for row in reader:
            actual = _bool_label(row.get("is_staffing", ""))
            if actual is None:
                skipped += 1
                continue
            job = {
                "employer_name": row.get("employer_name", ""),
                "job_title": row.get("job_title", ""),
                "job_description": row.get("job_description", ""),
            }
            predicted, reason = jf.is_staffing_company(job)
            if predicted and actual:
                tp += 1
            elif predicted and not actual:
                fp += 1
                examples.append(("false_positive", row.get("employer_name"), reason))
            elif not predicted and actual:
                fn += 1
                examples.append(("false_negative", row.get("employer_name"), reason))
            else:
                tn += 1

    total = tp + fp + tn + fn
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    accuracy = (tp + tn) / total if total else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "available": True,
        "labeled_rows": total,
        "skipped_unlabeled": skipped,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "accuracy": accuracy,
        "f1": f1,
        "examples": examples[:20],
        "passes_95_accuracy": total > 0 and accuracy >= 0.95,
    }


def audit_staffing_consistency(kept: List[Dict], rejected: List[Dict]) -> Dict:
    rule_leaks = [job for job in kept if jf.is_staffing_company(job)[0]]
    staffing_rejections = [
        job for job in rejected
        if str(job.get("_filter_reason", "")).startswith(
            ("known_staffing", "staffing_", "freelance_marketplace")
        )
    ]
    return {
        "rule_leaks": len(rule_leaks),
        "leak_examples": [
            (job.get("employer_name"), job.get("job_title"), jf.is_staffing_company(job)[1])
            for job in rule_leaks[:15]
        ],
        "staffing_rejections": len(staffing_rejections),
        "reject_reason_breakdown": dict(
            Counter(str(job.get("_filter_reason", "")).split(":")[0] for job in staffing_rejections)
        ),
        "passes": not rule_leaks,
    }


def audit_kept_records(kept: List[Dict], crm_norm: set, crm_compact: set) -> Dict:
    failures = []
    seen = set()
    for job in kept:
        staffing, reason = jf.is_staffing_company(job)
        if staffing:
            failures.append(("staffing_leak", job.get("employer_name"), reason))
        industry, reason = jf.is_excluded_industry(job)
        if industry:
            failures.append(("industry_leak", job.get("employer_name"), reason))
        us_ok, reason = jf.is_us_job(job)
        if not us_ok:
            failures.append(("non_us_leak", job.get("employer_name"), reason))
        in_crm, reason = jf.is_in_crm(job, crm_norm, crm_compact)
        if in_crm:
            failures.append(("crm_leak", job.get("employer_name"), reason))
        key = jf.dedup_key(job)
        if key in seen:
            failures.append(("duplicate_leak", job.get("employer_name"), key))
        seen.add(key)
        if job.get("_role_relevance_status") == "reject":
            failures.append(("role_relevance_leak", job.get("employer_name"), job.get("_matched_role")))
    return {"passes": not failures, "failures": failures[:50], "total_failures": len(failures)}


def run_audit(
    kept_path: str,
    rejected_path: str,
    source_path: Optional[str] = None,
) -> AuditResult:
    kept_payload = _load_json(kept_path)
    rejected_payload = _load_json(rejected_path)
    kept = kept_payload.get("jobs", [])
    rejected = rejected_payload.get("jobs", [])
    crm_norm, crm_compact = jf.load_crm_companies(config.CRM_EXCLUSION_FILE)

    staffing = audit_staffing_consistency(kept, rejected)
    kept_checks = audit_kept_records(kept, crm_norm, crm_compact)
    ground_truth = evaluate_staffing_ground_truth(config.STAFFING_GROUND_TRUTH_FILE)

    failures: List[str] = []
    warnings: List[str] = []
    if not staffing["passes"]:
        failures.append(f"Staffing rule leaked {staffing['rule_leaks']} records into kept output")
    if not kept_checks["passes"]:
        failures.append(f"Kept-output audit found {kept_checks['total_failures']} failures")

    if ground_truth.get("available"):
        if not ground_truth.get("passes_95_accuracy"):
            message = (
                f"Independent staffing accuracy is {ground_truth['accuracy']:.1%}, "
                "below the 95% target"
            )
            if config.REQUIRE_STAFFING_GROUND_TRUTH:
                failures.append(message)
            else:
                warnings.append(message)
    else:
        message = (
            "Independent staffing accuracy is not yet measured. Create and label "
            "data/validation/staffing_ground_truth.csv to prove the 95% criterion."
        )
        if config.REQUIRE_STAFFING_GROUND_TRUTH:
            failures.append(message)
        else:
            warnings.append(message)

    lines = [
        f"TGTC filter audit — {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Source: {source_path or kept_payload.get('source_file', '')}",
        f"Kept: {len(kept)} | Rejected: {len(rejected)}",
        f"Overall: {'PASS' if not failures else 'FAIL'}",
        "",
        "=== DETERMINISTIC CONSISTENCY ===",
        f"Staffing leaks in kept: {staffing['rule_leaks']}",
        f"Staffing rejects: {staffing['staffing_rejections']}",
        f"Reject reasons: {staffing['reject_reason_breakdown']}",
        f"All kept-record checks: {'PASS' if kept_checks['passes'] else 'FAIL'}",
    ]
    for failure in kept_checks["failures"]:
        lines.append(f"  {failure}")

    lines.extend(["", "=== INDEPENDENT STAFFING VALIDATION ==="])
    if ground_truth.get("available"):
        lines.extend([
            f"Labeled rows: {ground_truth['labeled_rows']}",
            f"Accuracy: {ground_truth['accuracy']:.1%}",
            f"Precision: {ground_truth['precision']:.1%}",
            f"Recall: {ground_truth['recall']:.1%}",
            f"F1: {ground_truth['f1']:.1%}",
            f"95% accuracy target: {'PASS' if ground_truth['passes_95_accuracy'] else 'FAIL'}",
        ])
        for example in ground_truth["examples"]:
            lines.append(f"  {example}")
    else:
        lines.append("Not available — run build_staffing_validation_sample.py, then label the CSV.")

    if warnings:
        lines.extend(["", "=== WARNINGS ===", *warnings])
    if failures:
        lines.extend(["", "=== FAILURES ===", *failures])

    report_path = str(Path(config.LOG_DIR) / f"filter_audit_{datetime.now():%Y-%m-%d_%H%M%S}.txt")
    Path(report_path).write_text("\n".join(lines), encoding="utf-8")

    return AuditResult(
        report_path=report_path,
        passed=not failures,
        failures=failures,
        warnings=warnings,
        summary={
            "kept": len(kept),
            "rejected": len(rejected),
            "staffing_consistency_pass": staffing["passes"],
            "kept_checks_pass": kept_checks["passes"],
            "ground_truth_available": ground_truth.get("available", False),
            "staffing_accuracy": ground_truth.get("accuracy"),
        },
    )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python audit_filter.py <kept.json> <rejected.json> [source.json]")
        sys.exit(2)
    result = run_audit(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print(Path(result.report_path).read_text(encoding="utf-8"))
    if config.PRODUCTION and not result.passed:
        sys.exit(1)
