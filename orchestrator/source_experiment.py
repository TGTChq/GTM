"""JB-vs-ATS source experiment framework (Category 2; explicit opt-in ONLY).

Produces an apples-to-apples comparison of two acquisition ARMS:

    CONTROL    active-jb  (LinkedIn, exclude_ats_duplicate=true)
    TREATMENT  active-ats (first-party career-site dataset)

Both arms share the same role universe, the same server-side ICP filters (where
semantically equivalent), the same downstream enrichment, the same Apollo gates and
the same ``send_safe_facts`` evaluation. The experiment NEVER enrolls anyone and
persists its artifacts SEPARATELY (``config.SOURCE_EXPERIMENT_ARTIFACT_DIR``).

This module holds the PURE parts: arm accounting, the sequential-stopping
statistics helper, and the artifact schema. The runner (``run_source_experiment``)
is an explicit CLI/mode that must be invoked deliberately; the daily cron never
calls it. Nothing here spends credits by itself.

Sequential stopping (per ``StoppingRule``):
* minimum sample per arm before any decision;
* Wilson confidence intervals on the primary metric (send-safe / credit);
* EARLY STOP when one arm's CI lower bound exceeds the other's upper bound
  (clear dominance) -- so 300+300 is NOT assumed;
* hard maximum total experiment budget (credits) -- never exceeded.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

EXPERIMENT_SCHEMA = "source-experiment/1"

METRICS = (
    "jobs_billed", "unique_provider_jobs", "company_resolved", "icp_pass", "hm_found",
    "zero_apollo_people", "verified_email", "send_safe", "net_new_equivalent",
    "apollo_calls", "apollo_credits",
)


@dataclass
class ArmStats:
    arm: str
    jobs_billed: int = 0
    unique_provider_jobs: int = 0
    company_resolved: int = 0
    icp_pass: int = 0
    hm_found: int = 0
    zero_apollo_people: int = 0
    verified_email: int = 0
    send_safe: int = 0
    net_new_equivalent: int = 0
    apollo_calls: int = 0
    apollo_credits: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)

    # derived
    def send_safe_per_credit(self) -> float:
        return self.send_safe / self.jobs_billed if self.jobs_billed else 0.0

    def apollo_credits_per_send_safe(self) -> float:
        return self.apollo_credits / self.send_safe if self.send_safe else float("inf")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["send_safe_per_credit"] = round(self.send_safe_per_credit(), 4)
        d["apollo_credits_per_send_safe"] = (round(self.apollo_credits_per_send_safe(), 2)
                                             if self.send_safe else None)
        return d


def wilson_interval(successes: int, n: int, confidence: float = 0.90) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion (robust at small n / p~0)."""
    if n <= 0:
        return 0.0, 1.0
    z = {0.80: 1.2816, 0.90: 1.6449, 0.95: 1.9600, 0.99: 2.5758}.get(round(confidence, 2))
    if z is None:  # generic approximation via inverse normal for other levels
        z = math.sqrt(2) * _erfinv(confidence)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _erfinv(y: float) -> float:
    # Winitzki approximation; good to ~1e-3, sufficient for a stopping rule.
    a = 0.147
    ln = math.log(1 - y * y)
    t = 2 / (math.pi * a) + ln / 2
    return math.copysign(math.sqrt(math.sqrt(t * t - ln / a) - t), y)


@dataclass(frozen=True)
class StoppingRule:
    min_per_arm: int
    max_total_budget: int
    confidence: float = 0.90

    def decide(self, control: ArmStats, treatment: ArmStats) -> Dict[str, Any]:
        total = control.jobs_billed + treatment.jobs_billed
        out: Dict[str, Any] = {"continue": True, "reason": "", "winner": None,
                               "control_ci": None, "treatment_ci": None, "total_billed": total}
        c_lo, c_hi = wilson_interval(control.send_safe, control.jobs_billed, self.confidence)
        t_lo, t_hi = wilson_interval(treatment.send_safe, treatment.jobs_billed, self.confidence)
        out["control_ci"], out["treatment_ci"] = (round(c_lo, 4), round(c_hi, 4)), (round(t_lo, 4), round(t_hi, 4))
        # Dominance is informational once the sample is adequate ...
        if control.jobs_billed >= self.min_per_arm and treatment.jobs_billed >= self.min_per_arm:
            if t_lo > c_hi:
                out["winner"], out["reason"], out["continue"] = "treatment", "treatment_dominates", False
            elif c_lo > t_hi:
                out["winner"], out["reason"], out["continue"] = "control", "control_dominates", False
            else:
                out["reason"] = "overlapping"
        else:
            out["reason"] = "below_min_sample"
        # ... but the hard budget is TERMINAL and always wins (never exceeded).
        if total >= self.max_total_budget:
            out["continue"], out["reason"] = False, "max_budget"
        return out

    def next_allocation(self, control: ArmStats, treatment: ArmStats, slice_jobs: int) -> Dict[str, int]:
        """Alternate arms in equal slices, clamped to the remaining budget."""
        remaining = max(0, self.max_total_budget - control.jobs_billed - treatment.jobs_billed)
        per = min(slice_jobs, remaining // 2) if remaining >= 2 else 0
        return {"control": per, "treatment": per}


def write_artifact(directory: str, experiment_id: str, payload: Dict[str, Any]) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{experiment_id}.json")
    payload = dict(payload)
    payload["schema"] = EXPERIMENT_SCHEMA
    payload["written_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)
    return path


def experiment_enabled(cfg) -> bool:
    """Explicit opt-in only. The daily cron never sets this."""
    return bool(getattr(cfg, "SOURCE_EXPERIMENT_ENABLED", False))


def rule_from_config(cfg) -> StoppingRule:
    return StoppingRule(
        min_per_arm=int(getattr(cfg, "SOURCE_EXPERIMENT_MIN_PER_ARM", 100) or 100),
        max_total_budget=int(getattr(cfg, "SOURCE_EXPERIMENT_MAX_BUDGET", 600) or 600),
        confidence=float(getattr(cfg, "SOURCE_EXPERIMENT_CONFIDENCE", 0.90) or 0.90))
