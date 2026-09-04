"""Multi-arm source/title acquisition experiment RUNNER.

``source_experiment.py`` supplies the statistics (ArmStats, Wilson intervals, the
stopping rule) but has no runner and no call sites. This is the missing piece: it
turns an arm SPECIFICATION into bounded acquisition calls against the existing
Fantastic adapter, so arms reuse production acquisition machinery rather than a
parallel implementation.

Two safety properties are structural, not conventions:

1. CREDIT CEILING. Every arm is granted a slice of ONE explicit experiment budget
   (``SOURCE_EXPERIMENT_MAX_BUDGET``) that is separate from -- and never added to
   -- the production governor grant. ``plan_arms`` REFUSES a configuration whose
   per-arm minimum cannot fit the budget rather than silently shrinking arms.
2. DEFAULT OFF. ``SOURCE_EXPERIMENT_ENABLED`` gates execution, and the runner
   never touches production configuration: each arm's source flags/limits are
   applied to a COPY of config for the duration of that arm only.

The runner measures acquisition-stage evidence -- billed, raw, unique, overlap,
incremental-vs-control. It does NOT invent downstream rates: usable-email,
send-safe and enrollment stay absent unless a caller supplies a real evaluator.
"""
from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from orchestrator.source_experiment import ArmStats, write_artifact


# --------------------------------------------------------------------------
# Arm specification
# --------------------------------------------------------------------------
@dataclass
class ArmSpec:
    """One acquisition configuration to measure.

    ``sources`` names the segments to enable; ``title_expression`` optionally
    overrides title_advanced (the title arm). Everything else stays at the
    production value so an arm differs from CONTROL in exactly one dimension.
    """
    name: str
    sources: Sequence[str] = ()                  # ats | linkedin | wellfound | ycombinator
    title_expression: Optional[str] = None       # None => production title_advanced
    is_control: bool = False

    def state_overrides(self, arm_dir: str) -> Dict[str, Any]:
        """Redirect EVERY mutable acquisition artifact into this arm's own directory.

        Without this an arm would advance the production canonical watermark, mark
        production sources drained, overwrite the continuation cursor and the quota
        snapshot, and the NEXT production run would skip inventory it never fetched.
        Isolation is per ARM, so one arm also cannot inherit another's cursor.
        """
        return {
            "FANTASTIC_WATERMARK_STATE_PATH": os.path.join(arm_dir, "watermark.json"),
            "FANTASTIC_JOBS_CONTINUATION_STATE_PATH": os.path.join(arm_dir, "continuation.json"),
            "FANTASTIC_QUOTA_SNAPSHOT_PATH": os.path.join(arm_dir, "quota_snapshot.json"),
            "FANTASTIC_GOVERNOR_LEDGER_PATH": os.path.join(arm_dir, "governor_ledger.json"),
            "FANTASTIC_SLUG_CROSSWALK_PATH": os.path.join(arm_dir, "slug_crosswalk.json"),
            "YIELD_LEDGER_PATH": os.path.join(arm_dir, "yield_ledger.jsonl"),
        }

    def config_overrides(self, per_source_limit: int) -> Dict[str, Any]:
        src = {s.lower() for s in self.sources}
        over: Dict[str, Any] = {
            "FANTASTIC_ATS_SOURCE_ENABLED": "ats" in src,
            "FANTASTIC_JOBS_ATS_LIMIT": per_source_limit if "ats" in src else 0,
            "FANTASTIC_JOBS_LINKEDIN_LIMIT": per_source_limit if "linkedin" in src else 0,
            "FANTASTIC_WELLFOUND_SOURCE_ENABLED": "wellfound" in src,
            "FANTASTIC_JOBS_WELLFOUND_LIMIT": per_source_limit if "wellfound" in src else 0,
            "FANTASTIC_YCOMBINATOR_SOURCE_ENABLED": "ycombinator" in src,
            "FANTASTIC_JOBS_YCOMBINATOR_LIMIT": per_source_limit if "ycombinator" in src else 0,
            # Arms are measured on a level field: fair share, so a high-volume
            # source cannot starve the others and distort per-source yield.
            # (Production keeps "sequential"; this is an experiment-only policy.)
            "FANTASTIC_SOURCE_ALLOCATION": "fair_share",
            # NO first-enablement backfill inside an arm. Every arm's state starts
            # empty, so ensure_bootstraps() would hand each source an extra
            # historical window OUTSIDE [experiment_lower, experiment_upper) --
            # spending arm budget on non-comparable inventory and contaminating
            # per-source yield. Arms must fetch the experiment window and nothing else.
            "FANTASTIC_SOURCE_BOOTSTRAP_ENABLED": False,
        }
        if self.title_expression is not None:
            over["FANTASTIC_JOBS_TITLE_ADVANCED_EXPRESSION"] = self.title_expression
        return over


class ExperimentBudgetError(ValueError):
    """Raised when the requested arms cannot fit the experiment credit budget."""


# --------------------------------------------------------------------------
# Standard arm set
# --------------------------------------------------------------------------
def default_arms(*, candidate_title_expression: Optional[str] = None) -> List[ArmSpec]:
    """CONTROL + source isolation + combinations + (optional) title arm."""
    arms = [
        ArmSpec("control_ats_linkedin", ("ats", "linkedin"), is_control=True),
        # isolation
        ArmSpec("ats_only", ("ats",)),
        ArmSpec("linkedin_only", ("linkedin",)),
        ArmSpec("wellfound_only", ("wellfound",)),
        ArmSpec("ycombinator_only", ("ycombinator",)),
        # combinations
        ArmSpec("ats_linkedin_wellfound", ("ats", "linkedin", "wellfound")),
        ArmSpec("ats_linkedin_yc", ("ats", "linkedin", "ycombinator")),
        ArmSpec("all_four", ("ats", "linkedin", "wellfound", "ycombinator")),
    ]
    if candidate_title_expression:
        arms.append(ArmSpec("title_expanded", ("ats", "linkedin"),
                            title_expression=candidate_title_expression))
    return arms


# --------------------------------------------------------------------------
# Budgeting
# --------------------------------------------------------------------------
@dataclass
class ArmPlan:
    arm: ArmSpec
    budget: int
    per_source_limit: int


def plan_arms(arms: Sequence[ArmSpec], *, max_budget: int, min_per_arm: int) -> List[ArmPlan]:
    """Split ONE experiment budget across arms, or refuse.

    HARD INVARIANT: ``sum(plan.budget) <= max_budget``. There is no implicit
    unlimited arm -- an arm with no budget is a configuration error, not a
    silently-skipped one.
    """
    arms = list(arms)
    if not arms:
        return []
    if max_budget <= 0:
        raise ExperimentBudgetError("SOURCE_EXPERIMENT_MAX_BUDGET must be > 0")
    if min_per_arm <= 0:
        raise ExperimentBudgetError("SOURCE_EXPERIMENT_MIN_PER_ARM must be > 0")
    need = min_per_arm * len(arms)
    if need > max_budget:
        raise ExperimentBudgetError(
            f"{len(arms)} arms x min {min_per_arm} = {need} exceeds the experiment "
            f"budget {max_budget}; reduce arms or raise SOURCE_EXPERIMENT_MAX_BUDGET")
    base = max_budget // len(arms)
    remainder = max_budget - base * len(arms)
    plans: List[ArmPlan] = []
    for i, arm in enumerate(arms):
        # Deterministic remainder: the first `remainder` arms get one extra credit.
        budget = base + (1 if i < remainder else 0)
        n_src = max(1, len(arm.sources))
        plans.append(ArmPlan(arm=arm, budget=budget,
                             per_source_limit=max(1, budget // n_src)))
    assert sum(p.budget for p in plans) <= max_budget
    return plans


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
@contextlib.contextmanager
def _applied(cfg, overrides: Dict[str, Any]):
    """Apply overrides to a config module for the arm's duration, then restore."""
    sentinel = object()
    old = {k: getattr(cfg, k, sentinel) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(cfg, k, v)
        yield
    finally:
        for k, v in old.items():
            if v is sentinel:
                delattr(cfg, k)
            else:
                setattr(cfg, k, v)


def _stats_from_result(name: str, result: Any, *,
                       downstream: Optional[Callable[[List[Dict[str, Any]]], Dict[str, int]]] = None
                       ) -> ArmStats:
    meta = dict(getattr(result, "metadata", {}) or {})
    jobs = list(getattr(result, "jobs", []) or [])
    per_source = (meta.get("source_attribution") or {}).get("per_source") or {}
    st = ArmStats(arm=name)
    st.jobs_billed = int(meta.get("jobs_quota_consumed", 0) or 0)
    st.unique_provider_jobs = len(jobs)
    st.detail = {
        "raw_returned": sum(int(v.get("returned_billed", 0) or 0) for v in per_source.values()),
        "cross_source_duplicates": int(meta.get("cross_source_duplicates", 0) or 0),
        "candidate_key_collisions": int(
            (meta.get("source_attribution") or {}).get("candidate_key_collisions", 0) or 0),
        "per_source": per_source,
        "allocation": meta.get("source_allocation", {}),
        "stop_reason": meta.get("stop_reason", ""),
        "job_ids": sorted(str(j.get("_fantastic_internal_id")) for j in jobs
                          if j.get("_fantastic_internal_id")),
    }
    # Downstream evidence is only recorded when a real evaluator supplies it.
    # Absent one, these stay 0 rather than being modelled.
    if downstream is not None:
        for key, value in (downstream(jobs) or {}).items():
            if hasattr(st, key):
                setattr(st, key, int(value))
    return st


def _seed_equal_window(path: str, lower: str, upper: str) -> None:
    """Pin an arm's isolated watermark to the SHARED experiment window.

    Comparing LinkedIn over an already-advanced production window against Wellfound
    over a fresh lookback would measure the WINDOWS, not the sources. Writing the
    window as an in-flight marker makes the engine reuse it verbatim.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema": "fantastic-watermark/1", "window_start": lower,
                   "window_end": upper, "in_flight_window_end": upper,
                   "overlap_start": lower, "last_successful_watermark": "",
                   "window_acquired_ids": [], "window_drained_sources": {},
                   "source_bootstrap": {}}, fh)


def run_experiment(*, cfg, acquire: Callable[[], Any], arms: Optional[Sequence[ArmSpec]] = None,
                   downstream: Optional[Callable[[List[Dict[str, Any]]], Dict[str, int]]] = None,
                   artifact_dir: Optional[str] = None,
                   experiment_id: str = "source_experiment",
                   window: Optional[Tuple[str, str]] = None,
                   state_root: Optional[str] = None) -> Dict[str, Any]:
    """Execute each arm under its own bounded budget and return comparable stats.

    ``acquire`` is injected (the adapter's acquisition entry point in production,
    a fake in tests) so the runner never imports a transport of its own.
    """
    if not bool(getattr(cfg, "SOURCE_EXPERIMENT_ENABLED", False)):
        return {"enabled": False, "skipped_reason": "SOURCE_EXPERIMENT_ENABLED is off"}

    arms = list(arms if arms is not None else default_arms())
    plans = plan_arms(arms,
                      max_budget=int(getattr(cfg, "SOURCE_EXPERIMENT_MAX_BUDGET", 0) or 0),
                      min_per_arm=int(getattr(cfg, "SOURCE_EXPERIMENT_MIN_PER_ARM", 0) or 0))

    results: Dict[str, ArmStats] = {}
    spent = 0
    aborted = ""
    root = state_root or os.path.join(
        str(getattr(cfg, "SOURCE_EXPERIMENT_ARTIFACT_DIR", "") or "."), experiment_id, "state")
    for plan in plans:
        arm_dir = os.path.join(root, plan.arm.name)
        os.makedirs(arm_dir, exist_ok=True)
        over = plan.arm.config_overrides(plan.per_source_limit)
        over.update(plan.arm.state_overrides(arm_dir))
        if window:
            _seed_equal_window(over["FANTASTIC_WATERMARK_STATE_PATH"], window[0], window[1])
            over["FANTASTIC_DATE_CREATED_WATERMARK_ENABLED"] = True
        # The arm's slice is expressed as the RUNTIME slice cap, exactly as the
        # top-up loop bounds a production slice; the validated per-run ceiling is
        # left alone so config validation semantics are unchanged.
        over["FANTASTIC_JOBS_RUN_SLICE_CAP"] = plan.budget
        with _applied(cfg, over):
            result = acquire()
        st = _stats_from_result(plan.arm.name, result, downstream=downstream)
        st.detail["arm_budget"] = plan.budget
        results[plan.arm.name] = st
        spent += st.jobs_billed
        stop = str(st.detail.get("stop_reason") or "")
        if stop in ("auth_failed", "rate_limited"):
            # Account-level failure: stop the WHOLE experiment rather than spend the
            # remaining arms against a dead credential or an active rate limit.
            aborted = stop
            break

    control = next((p.arm.name for p in plans if p.arm.is_control), None)
    summary = _summarize(results, control_name=control)
    payload = {
        "enabled": True, "experiment_id": experiment_id,
        "max_budget": int(getattr(cfg, "SOURCE_EXPERIMENT_MAX_BUDGET", 0) or 0),
        "min_per_arm": int(getattr(cfg, "SOURCE_EXPERIMENT_MIN_PER_ARM", 0) or 0),
        "arms_planned": [{"arm": p.arm.name, "budget": p.budget,
                          "sources": list(p.arm.sources),
                          "per_source_limit": p.per_source_limit} for p in plans],
        "total_budget_granted": sum(p.budget for p in plans),
        "total_billed": spent,
        "budget_invariant_ok": spent <= int(getattr(cfg, "SOURCE_EXPERIMENT_MAX_BUDGET", 0) or 0),
        "aborted": aborted,
        "window": {"lower": window[0], "upper": window[1]} if window else None,
        "state_root": root,
        "arms": {k: v.to_dict() for k, v in results.items()},
        "comparison": summary,
    }
    directory = artifact_dir or getattr(cfg, "SOURCE_EXPERIMENT_ARTIFACT_DIR", "")
    if directory:
        try:
            payload["artifact"] = write_artifact(directory, experiment_id, payload)
        except OSError:
            payload["artifact"] = ""
    return payload


def _summarize(results: Dict[str, ArmStats], *, control_name: Optional[str]) -> Dict[str, Any]:
    """Yield-per-credit and INCREMENTAL unique inventory versus CONTROL.

    The two are deliberately separate: an arm can win by being more efficient
    (better yield per credit) or by surfacing inventory CONTROL never sees. Raw
    volume alone is never the verdict.
    """
    out: Dict[str, Any] = {"control": control_name, "arms": {}}
    control_ids = set()
    if control_name and control_name in results:
        control_ids = set(results[control_name].detail.get("job_ids") or [])
    for name, st in results.items():
        ids = set(st.detail.get("job_ids") or [])
        billed = st.jobs_billed or 0
        incremental = ids - control_ids
        out["arms"][name] = {
            "jobs_billed": billed,
            "unique_jobs": len(ids),
            "unique_per_100_credits": round(100.0 * len(ids) / billed, 2) if billed else None,
            "incremental_vs_control": len(incremental),
            "incremental_per_100_credits": (round(100.0 * len(incremental) / billed, 2)
                                            if billed else None),
            "overlap_with_control": len(ids & control_ids),
            "cross_source_duplicates": st.detail.get("cross_source_duplicates", 0),
            "candidate_key_collisions": st.detail.get("candidate_key_collisions", 0),
        }
    return out
