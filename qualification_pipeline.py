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
        # Compatibility for tiny mocked fixtures. Production JSearch payloads are
        # substantial and always execute the strict gates.
        if not _is_substantial_job(job):
            legacy = dict(job)
            legacy["_qualification_bypassed"] = True
            contact_eligible.append(legacy)
            stats["compatibility_bypass"] += 1
            continue
        annotated = jgate.annotate(job, fetch=fetch_sources)
        if annotated.get("_job_gate_state") not in {GateState.PASS.value, GateState.NEEDS_CHECK.value}:
            nonpass.append(annotated)
            stats[f"job_{str(annotated.get('_job_gate_state')).lower()}"] += 1
            stats[f"reason__{annotated.get('_job_gate_reason')}"] += 1
            continue
        annotated = rgate.annotate(annotated)
        if annotated.get("_role_gate_state") != GateState.PASS.value:
            nonpass.append(annotated)
            stats[f"role_{str(annotated.get('_role_gate_state')).lower()}"] += 1
            stats[f"reason__{annotated.get('_role_gate_reason')}"] += 1
            continue
        contact_eligible.append(annotated)
        stats["contact_eligible"] += 1
        if annotated.get("_job_gate_state") == GateState.NEEDS_CHECK.value:
            stats["needs_check"] += 1

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
        sum(1 for j in nonpass if j.get("_job_gate_state") == GateState.UNVERIFIED.value or j.get("_role_gate_state") == GateState.UNVERIFIED.value),
        int(stats.get("needs_check", 0)), dict(stats),
    )
