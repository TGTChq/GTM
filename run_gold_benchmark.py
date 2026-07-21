"""Evaluate strict pipeline predictions against the TGTC gold dataset.

This runner never infers missing predictions. It scores only explicit persisted
final decisions, so a benchmark cannot appear successful merely because the
system abstained or omitted difficult rows.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

PASS = "FINAL_PASS"
NONPASS_STATES = {"NEEDS_CHECK", "REROUTE", "UNVERIFIED", "REJECT"}


def _load(path: str | Path) -> Dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _prediction_rows(payload: Dict) -> List[Dict]:
    if isinstance(payload, list):
        return payload
    for key in ("jobs", "predictions", "lote_2a_leads"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _id(row: Dict) -> str:
    return str(row.get("job_id") or row.get("canonical_job_id") or "").strip()


def _state(row: Dict) -> str:
    return str(row.get("_final_state") or row.get("final_state") or "").strip().upper()


def _reason(row: Dict) -> str:
    return str(row.get("_final_primary_reason") or row.get("primary_reason") or "").strip()


def _safe_div(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def evaluate(gold: Dict, predictions: Dict) -> Dict:
    expected_rows = list(gold.get("lote_2a_leads") or [])
    prediction_rows = _prediction_rows(predictions)
    by_id = {_id(row): row for row in prediction_rows if _id(row)}
    confusion = Counter()
    state_counts = Counter()
    reason_matches = 0
    reason_comparable = 0
    missing: List[str] = []
    cases: List[Dict] = []
    segments: Dict[str, Counter] = defaultdict(Counter)

    for expected in expected_rows:
        job_id = _id(expected)
        predicted = by_id.get(job_id)
        expected_state = _state(expected)
        if predicted is None:
            missing.append(job_id)
            predicted_state = "MISSING"
            predicted_reason = ""
        else:
            predicted_state = _state(predicted) or "MISSING_STATE"
            predicted_reason = _reason(predicted)
        state_counts[predicted_state] += 1

        expected_positive = expected_state == PASS
        predicted_positive = predicted_state == PASS
        if expected_positive and predicted_positive:
            confusion["tp"] += 1
        elif not expected_positive and predicted_positive:
            confusion["fp"] += 1
        elif expected_positive and not predicted_positive:
            confusion["fn"] += 1
        else:
            confusion["tn"] += 1
        if predicted_state in {"NEEDS_CHECK", "REROUTE", "UNVERIFIED", "MISSING", "MISSING_STATE"}:
            confusion["abstain"] += 1

        expected_reason = str(expected.get("primary_reason") or "")
        if predicted_reason:
            reason_comparable += 1
            if predicted_reason == expected_reason:
                reason_matches += 1

        segment = str(expected.get("publisher") or "unknown")
        segments[segment]["total"] += 1
        if not expected_positive and predicted_positive:
            segments[segment]["fp"] += 1
        if expected_positive and predicted_positive:
            segments[segment]["tp"] += 1
        if predicted_state in {"NEEDS_CHECK", "REROUTE", "UNVERIFIED", "MISSING", "MISSING_STATE"}:
            segments[segment]["abstain"] += 1

        cases.append({
            "job_id": job_id,
            "company": expected.get("company"),
            "publisher": expected.get("publisher"),
            "expected_state": expected_state,
            "predicted_state": predicted_state,
            "expected_reason": expected_reason,
            "predicted_reason": predicted_reason,
            "state_match": expected_state == predicted_state,
        })

    tp, fp, fn, tn = (confusion[name] for name in ("tp", "fp", "fn", "tn"))
    predicted_passes = tp + fp
    expected_passes = tp + fn
    report = {
        "generated_at": datetime.now().isoformat(),
        "gold_cases": len(expected_rows),
        "prediction_rows": len(prediction_rows),
        "matched_predictions": len(expected_rows) - len(missing),
        "missing_predictions": missing,
        "confusion": dict(confusion),
        "predicted_state_counts": dict(state_counts),
        "metrics": {
            "auto_pass_precision": _safe_div(tp, predicted_passes),
            "auto_pass_recall": _safe_div(tp, expected_passes),
            "false_positive_rate": _safe_div(fp, fp + tn),
            "false_negative_rate": _safe_div(fn, expected_passes),
            "abstention_rate": _safe_div(confusion["abstain"], len(expected_rows)),
            "coverage_rate": _safe_div(len(expected_rows) - len(missing), len(expected_rows)),
            "reason_accuracy_when_emitted": _safe_div(reason_matches, reason_comparable),
        },
        "segments_by_publisher": {
            name: {
                **dict(counts),
                "false_positive_rate": _safe_div(counts["fp"], counts["total"]),
                "abstention_rate": _safe_div(counts["abstain"], counts["total"]),
            }
            for name, counts in sorted(segments.items())
        },
        "release_checks": {
            "zero_fatal_false_positives": fp == 0,
            "all_gold_rows_predicted": not missing,
            "precision_at_least_99_percent": predicted_passes > 0 and (tp / predicted_passes) >= 0.99,
            "not_proven_by_empty_auto_pass_set": predicted_passes > 0,
        },
        "cases": cases,
    }
    report["release_ready"] = all(report["release_checks"].values())
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = evaluate(_load(args.gold), _load(args.predictions))
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"release_ready": report["release_ready"], **report["metrics"]}, indent=2))
    return 0 if report["release_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
