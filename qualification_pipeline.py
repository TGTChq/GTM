"""Pre-contact Job and Role Gate runner.

This step is deliberately before Apollo person/email enrichment.  It writes
separate artifacts for pass/needs-check candidates and nonpass diagnostics.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import config
from decision_types import GateState
from job_gate import JobGate
from role_gate import RoleGate


@dataclass
class QualificationResult:
    output_path: str
    nonpass_path: str
    input_jobs: int
    contact_eligible_jobs: int
    rejected_jobs: int
    unverified_jobs: int
    needs_check_jobs: int
    stats: Dict[str, int] = field(default_factory=dict)
    success: bool = True
    errors: List[str] = field(default_factory=list)


def _is_substantial_job(job: Dict) -> bool:
    return bool(job.get("job_title") and job.get("employer_name") and (job.get("job_description") or job.get("job_apply_link") or job.get("apply_options")))


def run_precontact_qualification(
    input_path: str,
    *,
    output_dir: Optional[str] = None,
    suffix: str = "",
    fetch_sources: Optional[bool] = None,
    job_gate: Optional[JobGate] = None,
    role_gate: Optional[RoleGate] = None,
) -> QualificationResult:
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    jobs = payload.get("jobs", [])
    output_root = Path(output_dir or config.FILTERED_OUTPUT_DIR)
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    tag = f"_{suffix}" if suffix else ""
    output_path = output_root / f"jobs_contact_eligible_{timestamp}{tag}.json"
    nonpass_path = output_root / f"jobs_precontact_nonpass_{timestamp}{tag}.json"
    jgate = job_gate or JobGate()
    rgate = role_gate or RoleGate()
    contact_eligible: List[Dict] = []
    nonpass: List[Dict] = []
    stats: Counter = Counter()

    for job in jobs:
        annotated = jgate.annotate(job, fetch=fetch_sources)
        source = (
            ((annotated.get("_job_gate_decision") or {}).get("metadata") or {}).get("source")
            or {}
        )
        source_state = str(source.get("state") or "MISSING")
        stats[f"source_state__{source_state}"] += 1
        if source.get("retryable"):
            stats["source_retryable"] += 1
        for note in source.get("notes") or []:
            stats[f"source_note__{str(note).split(':', 1)[0]}"] += 1
        for attempt in source.get("attempts") or []:
            if not isinstance(attempt, dict):
                continue
            stats[f"source_attempt__{attempt.get('status') or 'unknown'}"] += 1
            if attempt.get("phase"):
                stats[f"source_phase__{attempt.get('phase')}"] += 1
        if annotated.get("_job_gate_state") == GateState.REJECT.value:
            nonpass.append(annotated)
            stats[f"job_{str(annotated.get('_job_gate_state')).lower()}"] += 1
            stats[f"reason__{annotated.get('_job_gate_reason')}"] += 1
            continue
        if annotated.get("_job_gate_state") != GateState.PASS.value:
            stats[f"job_{str(annotated.get('_job_gate_state')).lower()}_reviewable"] += 1
            annotated.setdefault("_precontact_review_reasons", []).append(
                str(annotated.get("_job_gate_reason") or "UNVERIFIED_JOB_GATE")
            )
        annotated = rgate.annotate(annotated)
        if annotated.get("_role_gate_state") == GateState.REJECT.value:
            nonpass.append(annotated)
            stats[f"role_{str(annotated.get('_role_gate_state')).lower()}"] += 1
            stats[f"reason__{annotated.get('_role_gate_reason')}"] += 1
            continue
        if annotated.get("_role_gate_state") != GateState.PASS.value:
            stats[f"role_{str(annotated.get('_role_gate_state')).lower()}_reviewable"] += 1
            annotated.setdefault("_precontact_review_reasons", []).append(
                str(annotated.get("_role_gate_reason") or "UNVERIFIED_ROLE_GATE")
            )
        contact_eligible.append(annotated)
        stats["contact_eligible"] += 1

    output_payload = {
        **{key: value for key, value in payload.items() if key != "jobs"},
        "qualification_version": config.VALIDATION_VERSION,
        "source_file": str(input_path),
        "jobs": contact_eligible,
        "stats": dict(stats),
    }
    nonpass_payload = {
        "qualification_version": config.VALIDATION_VERSION,
        "source_file": str(input_path),
        "jobs": nonpass,
        "stats": dict(stats),
    }
    output_path.write_text(json.dumps(output_payload, indent=2), encoding="utf-8")
    nonpass_path.write_text(json.dumps(nonpass_payload, indent=2), encoding="utf-8")
    return QualificationResult(
        str(output_path), str(nonpass_path), len(jobs), len(contact_eligible),
        sum(1 for j in nonpass if j.get("_job_gate_state") == GateState.REJECT.value or j.get("_role_gate_state") == GateState.REJECT.value),
        sum(
            1
            for j in [*contact_eligible, *nonpass]
            if j.get("_job_gate_state") == GateState.UNVERIFIED.value
            or j.get("_role_gate_state") == GateState.UNVERIFIED.value
        ),
        sum(
            1
            for job in [*contact_eligible, *nonpass]
            if job.get("_job_gate_state") == GateState.NEEDS_CHECK.value
            or job.get("_role_gate_state") == GateState.NEEDS_CHECK.value
        ),
        dict(stats),
    )
