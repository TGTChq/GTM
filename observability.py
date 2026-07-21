"""Auditable final-pass metrics and deterministic drift samples."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

import config


def _reason(lead: Dict) -> str:
    return str(lead.get("_final_primary_reason") or "UNSPECIFIED")


def _stable_sample(leads: Iterable[Dict], size: int) -> List[Dict]:
    ranked = sorted(
        leads,
        key=lambda lead: hashlib.sha256(
            "|".join(
                str(lead.get(key) or "")
                for key in ("job_id", "employer_name", "job_title", "_final_state")
            ).encode("utf-8")
        ).hexdigest(),
    )
    return [
        {
            "job_id": lead.get("job_id"),
            "company": lead.get("canonical_company_name") or lead.get("employer_name"),
            "role": lead.get("canonical_job_title") or lead.get("job_title"),
            "publisher": lead.get("job_publisher"),
            "final_state": lead.get("_final_state"),
            "reason": _reason(lead),
            "validation_version": lead.get("_validation_version"),
        }
        for lead in ranked[: max(0, size)]
    ]


def build_observability_report(
    *,
    enriched_payload: Dict,
    topup_summary: Dict | None = None,
    airtable_result: Dict | None = None,
) -> Dict:
    leads = list(enriched_payload.get("jobs") or [])
    state_counts = Counter(str(lead.get("_final_state") or "LEGACY") for lead in leads)
    reason_counts = Counter(_reason(lead) for lead in leads if lead.get("_final_state"))
    gate_counts: Dict[str, Counter] = defaultdict(Counter)
    for lead in leads:
        for gate, decision in (lead.get("_gate_decisions") or {}).items():
            gate_counts[str(gate)][str((decision or {}).get("state") or "UNKNOWN")] += 1

    final_pass = state_counts["FINAL_PASS"]
    target = int(enriched_payload.get("final_pass_target") or config.get_final_pass_target())
    topup = topup_summary or {}
    airtable = airtable_result or {}
    report = {
        "generated_at": datetime.now().isoformat(),
        "validation_version": enriched_payload.get("validation_version") or config.VALIDATION_VERSION,
        "strict_final_pass_mode": bool(enriched_payload.get("strict_final_pass_mode")),
        "target_final_pass": target,
        "final_pass": final_pass,
        "deficit_remaining": max(0, target - final_pass),
        "target_reached": final_pass >= target,
        "stop_reason": enriched_payload.get("stop_reason") or topup.get("stop_reason"),
        "state_counts": dict(state_counts),
        "reason_counts": dict(reason_counts),
        "gate_state_counts": {gate: dict(counts) for gate, counts in gate_counts.items()},
        "source_recovery": {
            "resolved": sum(
                str(((lead.get("_gate_decisions") or {}).get("job") or {}).get("state")) == "PASS"
                for lead in leads
            ),
            "unverified": sum(
                str(((lead.get("_gate_decisions") or {}).get("job") or {}).get("state")) == "UNVERIFIED"
                for lead in leads
            ),
        },
        "reroute": {
            "remaining": state_counts["REROUTE"],
            "rounds": list(topup.get("reroute_rounds") or []),
            "recovered": sum(
                int(item.get("final_pass_added") or 0)
                for item in topup.get("reroute_rounds") or []
            ),
        },
        "api_cost": {
            "jsearch_initial_units": int(topup.get("initial_query_units") or 0),
            "jsearch_topup_units": int(topup.get("topup_query_units") or 0),
            "jsearch_total_units": int(topup.get("total_query_units") or 0),
            "jsearch_units_per_final_pass": round(
                int(topup.get("total_query_units") or 0) / final_pass, 3
            ) if final_pass else None,
        },
        "airtable": {
            "created": int(airtable.get("created") or 0),
            "final_pass": int(airtable.get("final_pass") or 0),
            "needs_check": int(airtable.get("needs_check") or 0),
            "failed": int(airtable.get("failed") or 0),
        },
        "drift_audit_sample": _stable_sample(leads, config.DRIFT_AUDIT_SAMPLE_SIZE),
    }
    return report


def save_observability_report(report: Dict) -> str:
    path = Path(config.EVIDENCE_OUTPUT_DIR) / f"observability_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
