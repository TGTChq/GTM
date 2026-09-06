#!/usr/bin/env python
"""CLI entry point for the replacement orchestrator (real adapter composition).

Modes (production is NEVER the default):

    python run_orchestrator.py --mode full_dry_run
    python run_orchestrator.py --mode offline_replay --corpus PATH
    python run_orchestrator.py --mode live_acquisition_only --lanes ats --boards BOARDS.json
    python run_orchestrator.py --mode live_acquisition_and_enrichment --lanes ats --boards BOARDS.json
    python run_orchestrator.py --mode production --production-ack I-UNDERSTAND ...

Downstream is wired to the repository's REAL adapters (qualification gates,
hiring-manager Apollo/Hunter, Airtable, Instantly). In offline modes the shared
``request_with_retry`` seam is faked so the real code runs with zero network.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import config

from retrieval_measurement.instrument import RequestBudget

from orchestrator.adapters_real import (
    FakeResponse,
    RealDelivery,
    RealEnrichmentStage,
    real_ats_runner,
    real_free_feeds_runner,
    real_jsearch_runner,
    seam_fake,
)
from orchestrator.lanes import LaneManager, LaneResult
from orchestrator.modes import DEFAULT_MODE, ExecutionMode, policy_for
from orchestrator.pipeline import Orchestrator, OrchestratorPlan
from orchestrator.runcontrol import RunContext
from orchestrator.state import StateManager

ATS_PROVIDERS = ("greenhouse", "lever", "ashby", "recruitee",
                 "workable", "personio", "smartrecruiters", "workday")
OFFLINE_MODES = {ExecutionMode.OFFLINE_REPLAY, ExecutionMode.FULL_DRY_RUN}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_orchestrator")
    p.add_argument("--mode", choices=[m.value for m in ExecutionMode], default=DEFAULT_MODE.value)
    p.add_argument("--lanes", default="", help="Comma: ats,jsearch,free_feeds,fantastic. Empty = ats.")
    p.add_argument("--boards", help="ATS board registry JSON (read-only).")
    p.add_argument("--corpus", action="append", default=[])
    p.add_argument("--external-batch", default="",
                   help="Already-acquired external Fantastic batch (Apify CSV). Adds the "
                        "'external_batch' lane, which issues no provider request and "
                        "consumes no Fantastic job credits.")
    p.add_argument("--artifact-root", default=str(Path(config.ARTIFACT_ROOT) / "orchestrator"))
    p.add_argument("--run-id", default=None)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--global-budget", type=int, default=500)
    p.add_argument("--ats-lane-budget", type=int, default=400)
    p.add_argument("--reserved-non-ats", type=int, default=100)
    p.add_argument("--board-budget", type=int, default=100)
    p.add_argument("--provider-budget", type=int, default=120)
    p.add_argument("--max-boards", type=int, default=8)
    p.add_argument("--airtable-write", action="store_true")
    p.add_argument("--auto-approve", action="store_true")
    p.add_argument("--instantly", action="store_true")
    p.add_argument("--target", type=int, default=300)
    p.add_argument("--production-ack", default="")
    p.add_argument("--dry-postings", type=int, default=30)
    p.add_argument("--preflight-only", action="store_true",
                   help="Zero-network readiness check: report PRESENT/ABSENT config, "
                        "boards, artifact root; contact nothing; exit without running.")
    p.add_argument("--inspect-run-lock", action="store_true",
                   help="Zero-network: print the run lock's redacted metadata, "
                        "classification and age; exit without running.")
    p.add_argument("--recover-stale-run-lock", action="store_true",
                   help="Zero-network audited recovery of a provably-stale lock. "
                        "Requires --expected-run-id and --expected-ownership-token to "
                        "match the on-disk lock exactly; refuses an active/changed lock.")
    p.add_argument("--expected-run-id", default="",
                   help="For --recover-stale-run-lock: the lock run_id you inspected.")
    p.add_argument("--expected-ownership-token", default="",
                   help="For --recover-stale-run-lock: the lock ownership token from "
                        "--inspect-run-lock (or LEGACY-NO-TOKEN for a pre-hotfix lock).")
    return p


#: A single definitive run's worst-case artifacts + margin. Preflight refuses if
#: the volume has less free space than this.
MIN_FREE_BYTES_FOR_RUN = 250 * 1024 * 1024

#: Maintenance-only CLI arguments that must NEVER enter the persisted run
#: identity: the lock inspect/recover flags and their recovery-authorization
#: values. ``expected_ownership_token`` in particular is secret-bearing, and even
#: its empty default carries a secret-looking NAME that the identity secret
#: detector refuses to persist. These do not describe an execution, so a normal
#: run must not pass them to ``RunContext.create``.
_MAINTENANCE_ARGS = frozenset({
    "inspect_run_lock",
    "recover_stale_run_lock",
    "expected_run_id",
    "expected_ownership_token",
})


def _identity_arguments(a: argparse.Namespace) -> Dict[str, Any]:
    """Execution-relevant, non-secret arguments only, for RunContext.create.

    Excludes the maintenance-only lock inspect/recover flags and, defensively,
    ANY argument whose name the identity secret-detector classifies as secret
    (so a future ``--*-token`` / ``--*-secret`` maintenance flag can never leak
    into the run identity either). The detector itself is unchanged and still
    guards everything that remains -- this only stops secret-bearing maintenance
    values from being handed to it in the first place.
    """
    from retrieval_measurement.identity import _is_secret
    out: Dict[str, Any] = {}
    for key, value in vars(a).items():
        if key in _MAINTENANCE_ARGS:
            continue
        if _is_secret(f"RUN_ARG_{key.upper()}"):
            continue
        out[key] = value
    return out


def _emit_reporting_ledger(state, keep: int = 30) -> None:
    """Print the durable reporting ledger so it can be captured from LOGS.

    gtm-volume is reachable only while a container is running, and on a cron
    service that is a window of minutes -- which made reporting acceptance depend
    on someone being awake at 03:00 UTC. Deployment logs have no such limit:
    `railway logs -d <id>` works for REMOVED deployments and well past 7 days.

    So the compact ledger -- the store the weekly report reads FIRST for every
    metric -- is written to stdout at the end of every run. That turns capture from
    a timing problem into a query. Bounded to the most recent `keep` runs (30), comfortably above
    the ~7 a weekly window holds; the heavy artifacts are deliberately NOT emitted.
    """
    try:
        from orchestrator.run_ledger import read_entries
        entries, problems = read_entries(state.root)
    except Exception as exc:  # noqa: BLE001 - observability is never fatal
        print(f"---- reporting ledger ---- unavailable: {type(exc).__name__}: {exc}")
        return
    recent = entries[-int(keep):] if keep else entries
    print(f"---- reporting ledger (last {len(recent)} of {len(entries)}) ----")
    if problems:
        print(f"  ledger_problems {problems}")
    for entry in recent:
        try:
            print("  LEDGER " + json.dumps(entry, sort_keys=True, default=str))
        except Exception:  # noqa: BLE001
            print(f"  LEDGER <unserializable {entry.get('run_id', '?')}>")


def _preflight_checks(a):
    """Zero-network integrity/config/space checks. Returns (results, lines);
    never prints or returns a secret value."""
    import shutil, hashlib, collections
    res: Dict[str, Any] = {}
    lines: List[str] = []
    manifest = Path("orchestrator.MANIFEST.sha256")
    if manifest.is_file():
        miss = absent = checked = 0
        for line in manifest.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            sha, _b, fname = parts[0], parts[1], parts[2]
            fp = Path(fname)
            if not fp.is_file():
                absent += 1
                continue
            checked += 1
            if hashlib.sha256(fp.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != sha:
                miss += 1
        res["integrity_ok"] = (miss == 0 and absent == 0)
        lines.append(f"package_integrity    checked={checked} mismatch={miss} absent={absent} "
                     f"({'OK' if res['integrity_ok'] else 'FAILED'})")
    else:
        res["integrity_ok"] = False
        lines.append("package_integrity    manifest ABSENT (FAILED)")
    for k in ("RAPIDAPI_KEY", "APOLLO_API_KEY", "HUNTER_API_KEY",
              "AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME",
              "FANTASTIC_JOBS_API_KEY"):
        res[k] = bool(getattr(config, k, None))
        lines.append(f"{k:20} {'PRESENT' if res[k] else 'ABSENT'}")
    # Definitive production path uses the FULL board registry (no --boards). A
    # static board file is a validation/offline-only override.
    try:
        if a.boards:
            from retrieval_measurement.drivers import load_boards_readonly
            boards, err = load_boards_readonly(a.boards)
            source = a.boards
        else:
            from ats_board_registry import AtsBoardRegistry
            reg = AtsBoardRegistry()
            if config.ATS_REGISTRY_AUTO_SEED_HISTORY:
                try:
                    reg.seed_from_history()
                except Exception:  # noqa: BLE001
                    pass
            boards = list(reg.due_entries(limit=int(config.ATS_MAX_BOARDS_PER_RUN), force=True))
            err = "" if boards else "ats_board_registry has no valid boards"
            source = f"ats_board_registry ({len(reg.entries)} tracked)"
        res["boards_ok"] = (len(boards) > 0 and not err)
        by = collections.Counter(b.get("provider") for b in boards)
        lines.append(f"boards               {len(boards)} from {source} err={err or 'none'}")
        for prov, n in sorted(by.items()):
            lines.append(f"  {prov:16} {n}")
    except Exception as exc:  # noqa: BLE001
        res["boards_ok"] = False
        lines.append(f"boards               ERROR {type(exc).__name__}")
    root = Path(a.artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    res["writable"] = os.access(str(root), os.W_OK)
    free = shutil.disk_usage(str(root)).free
    res["free_ok"] = free >= MIN_FREE_BYTES_FOR_RUN
    lines.append(f"artifact_root        {root} writable={res['writable']}")
    lines.append(f"disk_free_gb         {round(free/1e9, 1)} "
                 f"(min {round(MIN_FREE_BYTES_FOR_RUN/1e9, 2)} GB, "
                 f"{'OK' if res['free_ok'] else 'INSUFFICIENT'})")
    # Run lock: CLASSIFY, do not treat mere existence as a blocker. A stale lock
    # left by a gone container is auto-recovered at acquisition; only a live
    # (foreign_active) or unprovable (indeterminate) lock blocks a live run.
    from orchestrator.runlock import describe_lock
    lk = describe_lock(root / ".run.lock")
    res["lock"] = lk
    cls = lk["classification"] if lk["present"] else "absent"
    res["lock_class"] = cls
    res["lock_blocks"] = lk["present"] and cls in ("foreign_active", "indeterminate", "current")
    res["lock_free"] = not lk["present"]  # kept for back-compat readers
    if not lk["present"]:
        lines.append("run_lock             free (none)")
    else:
        lines.append(f"run_lock             {cls} run_id={lk['run_id']} pid={lk['pid']} "
                     f"age_s={None if lk['age_seconds'] is None else int(lk['age_seconds'])}")
        lines.append(f"  lock_identity      deployment={lk['deployment_id']} "
                     f"replica={lk['replica_id']} service={lk['service']} "
                     f"boot={(str(lk['boot_id'])[:8] + '...') if lk['boot_id'] else 'none'} "
                     f"token={'yes' if lk['has_ownership_token'] else 'legacy-none'}")
    lines.append("delivery             airtable=review-staging(Pending) auto_approve=OFF instantly=OFF")
    return res, lines


def _preflight_only(a) -> int:
    """Safe idle start command: ZERO network, PRESENT/ABSENT only, exit 0."""
    res, lines = _preflight_checks(a)
    print("=== replacement-orchestrator preflight-only (zero network) ===")
    for line in lines:
        print(line)
    print("network_contacted    NONE")
    print("PREFLIGHT OK")
    return 0


def _inspect_run_lock(a) -> int:
    """Zero-network: print the lock's redacted metadata, classification and age.
    Reveals the ownership token (needed for --recover-stale-run-lock) because this
    is an explicit operator diagnostic, not a normal run log."""
    from orchestrator.runlock import describe_lock, LEGACY_TOKEN_SENTINEL
    lock_path = Path(a.artifact_root) / ".run.lock"
    lk = describe_lock(lock_path, reveal_token=True)
    print("=== run-lock inspection (zero network) ===")
    print(f"lock_path            {lk['path']}")
    if not lk["present"]:
        print("present              NO (no lock; a run may start normally)")
        print("network_contacted    NONE")
        return 0
    print(f"present              YES")
    print(f"classification       {lk['classification']}")
    print(f"run_id               {lk['run_id']}")
    print(f"pid                  {lk['pid']}")
    print(f"created_at           {lk['created_at']}")
    print(f"age_seconds          {None if lk['age_seconds'] is None else int(lk['age_seconds'])}")
    print(f"deployment_id        {lk['deployment_id']}")
    print(f"replica_id           {lk['replica_id']}")
    print(f"service              {lk['service']}")
    print(f"boot_id              {lk['boot_id']}")
    print(f"owned_by_current     {lk['owned_by_current_invocation']}")
    tok = lk["ownership_token"] if lk["has_ownership_token"] else LEGACY_TOKEN_SENTINEL
    print(f"ownership_token      {tok}")
    root = Path(a.artifact_root)
    if lk["classification"] in ("stale",):
        print("recovery             not required: acquisition auto-recovers a stale lock.")
    elif lk["classification"] in ("indeterminate",):
        print("recovery             if you are CERTAIN the owner is dead, run:")
        print(f"  python run_orchestrator.py --recover-stale-run-lock --artifact-root {root} "
              f"--expected-run-id {lk['run_id']} --expected-ownership-token {tok}")
    elif lk["classification"] in ("foreign_active",):
        print("recovery             REFUSED: the owner is verifiably active; do not recover.")
    print("network_contacted    NONE")
    return 0


def _recover_run_lock(a) -> int:
    """Zero-network audited recovery of a stale lock with exact identity match."""
    from orchestrator.runlock import recover_stale_lock
    lock_path = Path(a.artifact_root) / ".run.lock"
    out = recover_stale_lock(
        lock_path,
        expected_run_id=a.expected_run_id,
        expected_token=a.expected_ownership_token,
        audit_dir=Path(a.artifact_root) / "run_lock_audit",
    )
    print("=== run-lock recovery (zero network) ===")
    print(f"lock_path            {lock_path}")
    print(f"recovered            {out.get('recovered')}")
    print(f"classification       {out.get('classification')}")
    print(f"reason               {out.get('reason', 'ok')}")
    if out.get("recovered"):
        print(f"audit_path           {out.get('audit_path')}")
        print("network_contacted    NONE")
        return 0
    print("network_contacted    NONE")
    return 2


def _strict_preflight(a, policy) -> int:
    """Strict gate for a live run: refuse (exit 2) BEFORE any external call if a
    mandatory dependency is missing. Hunter is optional (fallback-only)."""
    res, lines = _preflight_checks(a)
    print("=== strict preflight (before any external call) ===")
    for line in lines:
        print(line)
    lanes = [x.strip() for x in a.lanes.split(",") if x.strip()] or ["ats"]
    problems: List[str] = []
    if not res.get("integrity_ok"):
        problems.append("package integrity")
    # ATS boards are a dependency only when the ats lane is selected. A
    # fantastic/jsearch/free-only run must not be blocked by an empty registry.
    if "ats" in lanes and not res.get("boards_ok"):
        problems.append("BOARDS_FINAL.json")
    if not res.get("writable"):
        problems.append("artifact root not writable")
    if not res.get("free_ok"):
        problems.append("insufficient free volume space")
    # Run-lock: a stale lock is not a blocker (acquisition auto-recovers it); a
    # live or indeterminate lock is. Emit a rich, actionable diagnostic.
    lk = res.get("lock") or {}
    if res.get("lock_blocks"):
        root = Path(a.artifact_root)
        tok = ("LEGACY-NO-TOKEN" if not lk.get("has_ownership_token")
               else "<TOKEN-from --inspect-run-lock>")
        print(f"run_lock             BLOCKS: classification={lk.get('classification')} "
              f"owned_by_this_invocation=False", file=sys.stderr)
        print("  a run lock is held and cannot be proven gone. If you are certain the "
              "owner is dead, recover it explicitly:", file=sys.stderr)
        print(f"    python run_orchestrator.py --inspect-run-lock --artifact-root {root}",
              file=sys.stderr)
        print(f"    python run_orchestrator.py --recover-stale-run-lock --artifact-root {root} "
              f"--expected-run-id {lk.get('run_id')} --expected-ownership-token {tok}",
              file=sys.stderr)
        problems.append(f"run lock held ({lk.get('classification')})")
    elif lk.get("present") and lk.get("classification") == "stale":
        print("run_lock             stale -> acquisition will safely auto-recover it (audited)")
    if "jsearch" in lanes and not res.get("RAPIDAPI_KEY"):
        problems.append("RAPIDAPI_KEY (jsearch selected)")
    if "fantastic" in lanes and not res.get("FANTASTIC_JOBS_API_KEY"):
        problems.append("FANTASTIC_JOBS_API_KEY (fantastic selected)")
    if policy.allow_enrichment and not res.get("APOLLO_API_KEY"):
        problems.append("APOLLO_API_KEY (live enrichment)")
    if a.airtable_write and policy.allow_airtable_write and not (
            res.get("AIRTABLE_TOKEN") and res.get("AIRTABLE_BASE_ID") and res.get("AIRTABLE_TABLE_NAME")):
        problems.append("Airtable credentials (--airtable-write)")
    if not res.get("HUNTER_API_KEY"):
        print("hunter               ABSENT -> fallback disabled cleanly (does NOT block the run)")
    if problems:
        print("PREFLIGHT FAILED — refusing before any external request: "
              + "; ".join(problems), file=sys.stderr)
        return 2
    print("PREFLIGHT OK — proceeding to the definitive run.")
    return 0


def build_budget(a: argparse.Namespace) -> RequestBudget:
    reserved = int(a.reserved_non_ats)
    return RequestBudget(
        limit=int(a.global_budget),
        lane_limits={"ats": int(a.ats_lane_budget)},
        provider_limits={f"ats_{p}": int(a.provider_budget) for p in ATS_PROVIDERS},
        board_limit=int(a.board_budget),
        reserved_for_lanes={"jsearch": reserved // 2, "free_feeds": reserved - reserved // 2},
    )


def _synthetic(n: int) -> List[Dict[str, Any]]:
    return [{
        "job_id": f"SYN-{i:05d}", "job_title": "VP of Sales",
        "employer_name": f"Acme {i}", "employer_website": f"acme{i}.com",
        "job_description": "Own revenue.", "job_apply_link": f"https://acme{i}.com/careers/{i}",
        "_acquisition_source": "synthetic",
    } for i in range(n)]


def _offline_lane_runner(postings):
    def runner(_m: LaneManager) -> LaneResult:
        return LaneResult(lane="acquisition", status="complete", jobs=list(postings))
    return runner


def _ats_detail_budgets() -> Dict[str, int]:
    """Per-provider detail-fetch budgets, from config (no legacy hardcode)."""
    return {
        "greenhouse": int(getattr(config, "ATS_GREENHOUSE_DETAIL_BUDGET_PER_RUN", 100) or 100),
        "workday": int(getattr(config, "ATS_WORKDAY_DETAIL_BUDGET_PER_RUN", 100) or 100),
        "smartrecruiters": int(getattr(config, "ATS_SMARTRECRUITERS_DETAIL_BUDGET_PER_RUN", 100) or 100),
    }


def _record_ats_board_health(registry, result: LaneResult) -> None:
    """Persist per-board outcomes into the shared ATS registry so health-aware
    scheduling actually accrues across runs: a success resets consecutive_failures
    (recovery back into normal rotation); a failure increments it (bounded retry,
    then slot-only deprioritization). Without this the registry health goes stale
    and the scheduler degrades to blind rotation."""
    boards = (((result.accounting or {}).get("session") or {}).get("boards")) or []
    updated = 0
    for b in boards:
        if b.get("skipped_by_budget") or b.get("skipped_by_scheduler"):
            continue  # never attempted this run; don't penalize or credit
        key = b.get("key") or f"{b.get('provider') or ''}:{b.get('identifier') or ''}"
        if key == ":":
            continue
        error = str(b.get("error") or "")
        job_count = int(b.get("canonical_records") or 0)
        registry.record_result(key, success=(not error), job_count=job_count,
                               error=error, save=False)
        updated += 1
    if updated:
        try:
            registry.save()
        except Exception as exc:  # noqa: BLE001 - health persistence must never fail the run
            result.notes.append(f"ats_registry_health_save_failed:{type(exc).__name__}")
    result.attribution["ats_registry_health_updated"] = updated


def _live_ats_runner(a: argparse.Namespace):
    """Definitive production ATS lane: driven by the FULL production board
    registry with health-aware deterministic scheduling -- NOT the 6-board
    validation snapshot. A static ``--boards`` file is honoured only when
    explicitly supplied (offline/validation); the production command omits it and
    uses the registry."""
    from free_job_sources import default_fetcher
    from retrieval_measurement import ats_schedule

    sched_cfg = ats_schedule.SchedulerConfig.from_config(config)
    sched_cfg.validate()  # raises before any board is touched

    # Explicit static board file (offline/validation only).
    if a.boards:
        from retrieval_measurement.drivers import load_boards_readonly
        boards, err = load_boards_readonly(a.boards, limit=int(a.max_boards))

        def runner_static(m: LaneManager) -> LaneResult:
            if err:
                return LaneResult(lane="ats", status="failed", errors=[err])
            return real_ats_runner(boards, default_fetcher,
                                   checkpoint_dir=str(Path(a.artifact_root) / "checkpoints_ats"),
                                   scheduler_config=sched_cfg,
                                   detail_budgets=_ats_detail_budgets())(m)
        return runner_static

    # Definitive path: the full production registry (auto-seeded, health-tracked).
    # Force health-aware deterministic partitioning here regardless of the legacy
    # ATS_SCHEDULER_MODE env toggle -- the definitive production lane is always
    # health-scheduled (slot rotation + overdue backstop + bounded retry), never
    # the legacy age-interval herd. cycle_length / caps still come from config.
    if sched_cfg.mode != "deterministic_partition":
        sched_cfg.mode = "deterministic_partition"
        sched_cfg.validate()

    # Advance the rotation slot across runs. `SchedulerConfig.position` is read
    # once from the static ATS_SCHEDULER_POSITION (0), so without this every run
    # would cover slot 0 only and the other cycle_length-1 slots would be reached
    # ONLY via the 168h overdue backstop -- never by fair slot rotation. Persist
    # last_position in the scheduler state file and advance it deterministically so
    # every board's slot comes round exactly once per cycle_length runs. Best-
    # effort: an unreadable/absent state file just restarts the rotation at 0.
    sched_state = None
    if sched_cfg.state_path:
        try:
            sched_state = ats_schedule.SchedulerState.load(sched_cfg.state_path)
            prev = sched_state.last_position
            sched_cfg.position = (
                ((int(prev) + 1) if prev is not None else 0) % max(1, sched_cfg.cycle_length)
            )
            sched_cfg.carried_overdue = list(sched_state.carried_overdue)
        except Exception as exc:  # noqa: BLE001 - state is best-effort, never fatal
            print(f"ATS scheduler state unreadable ({exc}); starting rotation at "
                  "position 0")
            sched_cfg.position = 0
    print(f"ATS scheduler: mode={sched_cfg.mode} cycle_length={sched_cfg.cycle_length} "
          f"position={sched_cfg.position} (covers slot "
          f"{sched_cfg.position % max(1, sched_cfg.cycle_length)}; "
          f"full registry every {sched_cfg.cycle_length} runs)")

    from ats_board_registry import AtsBoardRegistry
    registry = AtsBoardRegistry()
    if config.ATS_REGISTRY_AUTO_SEED_HISTORY:
        try:
            registry.seed_from_history()
        except Exception:  # noqa: BLE001 - seeding is best-effort, never fatal
            pass
    # The partitioned scheduler rotates by stable slot over the WHOLE eligible
    # registry, not the age-due subset, so force=True returns every valid board;
    # the scheduler (inside run_ats) then applies slot/overdue/retry health logic.
    candidates = list(registry.due_entries(limit=int(config.ATS_MAX_BOARDS_PER_RUN), force=True))

    def runner_registry(m: LaneManager) -> LaneResult:
        if not candidates:
            return LaneResult(lane="ats", status="failed",
                              errors=["ATS registry has no valid boards; refusing to "
                                      "silently fall back to the validation snapshot"])
        result = real_ats_runner(candidates, default_fetcher,
                                 checkpoint_dir=str(Path(a.artifact_root) / "checkpoints_ats"),
                                 scheduler_config=sched_cfg,
                                 detail_budgets=_ats_detail_budgets())(m)
        _record_ats_board_health(registry, result)
        # Per-provider + registry-size reporting for the operator.
        import collections as _c
        by_provider = _c.Counter(str(b.get("provider") or "?") for b in candidates)
        result.attribution["ats_registry_size"] = len(registry.entries)
        result.attribution["ats_candidates"] = len(candidates)
        result.attribution["ats_candidates_by_provider"] = dict(by_provider)
        # Persist the slot we just covered so the NEXT run advances to the next
        # slot (fair rotation). Best-effort: a save failure never fails the run.
        if sched_cfg.state_path:
            try:
                st = sched_state or ats_schedule.SchedulerState()
                st.last_position = sched_cfg.position
                # carried_overdue is round-tripped unchanged: it stays empty while
                # ATS_SCHEDULER_OVERDUE_CAP is disabled (nothing is carried forward).
                st.save(sched_cfg.state_path)
            except Exception as exc:  # noqa: BLE001 - state persistence never fatal
                result.notes.append(f"ats_scheduler_state_save_failed:{type(exc).__name__}")
        return result
    return runner_registry


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.inspect_run_lock:
        return _inspect_run_lock(a)
    if a.recover_stale_run_lock:
        return _recover_run_lock(a)
    if a.preflight_only:
        return _preflight_only(a)
    mode = ExecutionMode(a.mode)
    policy = policy_for(mode)

    if mode is ExecutionMode.PRODUCTION and a.production_ack != "I-UNDERSTAND":
        print("production requires --production-ack I-UNDERSTAND; refusing.", file=sys.stderr)
        return 2
    if mode is ExecutionMode.PRODUCTION:
        print("production: inject real credentials + confirm out-of-band; refusing autorun.",
              file=sys.stderr)
        return 2

    # Strict preflight for any LIVE mode: refuse before constructing a live lane
    # or making a single external call if a mandatory dependency is missing.
    if mode not in OFFLINE_MODES:
        gate = _strict_preflight(a, policy)
        if gate != 0:
            return gate

    ctx = RunContext.create(mode, _identity_arguments(a), run_id=a.run_id)
    state = StateManager(a.artifact_root, policy, run_id=ctx.run_id)
    budget = build_budget(a)

    requested = [x.strip() for x in a.lanes.split(",") if x.strip()]
    lane_runners: Dict[str, Any] = {}
    if mode in OFFLINE_MODES:
        postings = _synthetic(a.dry_postings)
        if a.corpus:
            postings = []
            for c in a.corpus:
                d = json.loads(Path(c).read_text(encoding="utf-8"))
                postings.extend(r for r in (d.get("jobs") if isinstance(d, dict) else d) or []
                                if isinstance(r, dict))
        lanes = ["acquisition"]
        lane_runners["acquisition"] = _offline_lane_runner(postings)
    else:
        lanes = requested or ["ats"]
        if "ats" in lanes:
            lane_runners["ats"] = _live_ats_runner(a)
        if "fantastic" in lanes:
            from orchestrator.adapters_real import real_fantastic_runner
            lane_runners["fantastic"] = real_fantastic_runner()
        if "external_batch" in lanes:
            from orchestrator.adapters_real import real_external_batch_runner
            if not a.external_batch:
                print("--lanes external_batch requires --external-batch <csv>", file=sys.stderr)
                return 2
            lane_runners["external_batch"] = real_external_batch_runner(a.external_batch)
        if "free_feeds" in lanes:
            from free_job_sources import default_fetcher
            lane_runners["free_feeds"] = real_free_feeds_runner(
                list(config.FREE_JOB_SOURCES), default_fetcher)
        if "jsearch" in lanes:
            from retrieval_measurement.identity import NonWritingRegistry
            lane_runners["jsearch"] = real_jsearch_runner(
                output_dir=str(state.run_dir() / "jsearch_raw"),
                max_queries=None, registry=NonWritingRegistry(None), live=True)

    plan = OrchestratorPlan(
        lanes=lanes, lane_runners=lane_runners,
        enrichment_engine=RealEnrichmentStage(
            target_final_pass=int(a.target),
            workdir=str(state.run_dir() / "enrichment")),
        delivery_manager=RealDelivery(
            enable_airtable_write=bool(a.airtable_write) and policy.allow_airtable_write,
            auto_approve=bool(a.auto_approve),
            enable_instantly=bool(a.instantly) and policy.allow_instantly_enrollment),
        target=int(a.target),
    )

    # Offline modes: fake the shared client seam so the REAL downstream runs with
    # zero network, and stub the credentials its preflights require.
    seam_ctx = contextlib.nullcontext()
    if mode in OFFLINE_MODES:
        config.APOLLO_API_KEY = config.APOLLO_API_KEY or "OFFLINE-STUB"
        for k, v in (("AIRTABLE_TOKEN", "STUB"), ("AIRTABLE_BASE_ID", "appSTUB"),
                     ("AIRTABLE_TABLE_NAME", "Leads"), ("INSTANTLY_API_KEY", "STUB")):
            if not getattr(config, k, None):
                setattr(config, k, v)
        config.INSTANTLY_RATE_LIMIT_DELAY = 0
        seam_ctx = seam_fake(lambda method, url, **k: FakeResponse(
            {"records": [], "people": [], "contacts": [], "organizations": [], "data": {}}))

    from orchestrator.runlock import RunLockHeld
    try:
        with seam_ctx:
            result = Orchestrator(ctx, state, budget).run(plan, resume=bool(a.resume))
    except RunLockHeld as exc:
        print(f"run_lock             HELD\n{exc}", file=sys.stderr)
        return 2
    except Exception:
        # A FAILED run is precisely the one the weekly report must not lose,
        # and it never reaches the summary below. The pipeline finalizes its
        # ledger entry in its own `finally`, so the entry exists by now --
        # emit it before the traceback leaves the process, or the only copy
        # stays on a volume that is unreachable between cron runs.
        _emit_reporting_ledger(state)
        raise

    _print_run_summary(ctx, mode, result, state)
    return 0 if result["all_reconcile"] else 1


def _print_run_summary(ctx, mode, result, state) -> None:
    """Emit the full business funnel to stdout so a Railway operator can read the
    whole run from logs WITHOUT mounting the volume (Defect G). Every value is
    already computed in-memory; nothing here recomputes or contacts anything.
    No PII is printed -- counts and reason codes only."""
    wf = result.get("waterfall") or {}
    units = wf.get("unit_totals") or {}
    census = wf.get("disposition_census") or {}
    enr = result.get("enrichment") or {}
    funnel = (enr or {}).get("funnel") or {}
    deliv = result.get("delivery") or {}

    raw = units.get("postings")
    unique = units.get("opportunities")
    contacts = units.get("contacts")
    fp = wf.get("final_pass_count", census.get("FINAL_PASS", 0))
    nc = census.get("NEEDS_CHECK", 0)
    uv = census.get("UNVERIFIED", 0)
    # Email counters are EMAIL-PRESENCE / VERIFICATION based (Gate D), never a sum of
    # disposition labels: ``contacts`` == leads carrying a resolved email; verified ==
    # those whose Apollo status is "verified". (The old "usable_emails = FINAL_PASS +
    # UNVERIFIED" was a census sum unrelated to email presence and could exceed contacts.)
    emails = result.get("emails") or {}
    verified_emails = emails.get("verified")
    unverified_emails = emails.get("unverified")
    at_created = deliv.get("created")
    at_existing = deliv.get("skipped_existing")
    at_failed = deliv.get("failed")
    at_submitted = deliv.get("reviewable_submitted")
    skips = deliv.get("skip_breakdown") or {}
    acq = result.get("acquisition") or {}

    # Top rejection reasons across the boundaries the orchestrator can see:
    # cross-run dedup, pre-contact qualification, and enrichment loss.
    reasons: Dict[str, int] = {}
    for st in wf.get("stages") or []:
        for r, n in (st.get("primary_reasons") or {}).items():
            reasons[r] = reasons.get(r, 0) + int(n)
    for r, n in (funnel.get("qual_reason_counts") or {}).items():
        reasons[r] = reasons.get(r, 0) + int(n)
    for r, n in (enr.get("loss_census") or {}).items():
        reasons[r] = reasons.get(r, 0) + int(n)
    top5 = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:5]

    def line(k, v):
        print(f"{k:<26}{v}")

    print("================ RUN SUMMARY ================")
    line("run_id", ctx.run_id)
    line("mode", mode.value)
    line("status", result["run"]["status"])
    # Acquisition lanes -- surface a FAILED lane and its error (config/provider/parse
    # crash) directly in Railway logs, so a zero-acquisition failure is never hidden
    # behind status=complete/raw_postings=0 and never requires mounting the volume.
    lanes = result.get("lanes") or {}
    run_stop = (result.get("run") or {}).get("stop_reason") or ""
    failed_lanes = {ln: ld for ln, ld in lanes.items()
                    if (ld or {}).get("status") == "failed" or (ld or {}).get("errors")}
    if lanes:
        print("---- Acquisition lanes ----")
        for ln, ld in lanes.items():
            ld = ld or {}
            flag = "   <== FAILED" if ln in failed_lanes else ""
            line(f"  {ln}", f"status={ld.get('status')} jobs={ld.get('jobs')} "
                            f"requests={ld.get('physical_requests')}{flag}")
            for e in (ld.get("errors") or [])[:3]:
                line("    error", str(e)[:300])
    if run_stop:
        line("stop_reason", run_stop)
    if failed_lanes:
        line("ACQUISITION", f"FAILED ({', '.join(failed_lanes)}) -- see errors above")
    # CUMULATIVE top-up acquisition (all slices), never only the last slice.
    if acq:
        cum = acq.get("cumulative") or {}
        print("---- Acquisition (cumulative, all top-up slices) ----")
        line("topup_iterations", acq.get("iterations"))
        line("run_cap", f"{acq.get('run_cap')} (source={acq.get('budget_source')})")
        line("jobs_unique_kept", cum.get("jobs_unique_kept"))
        line("jobs_returned_billed", cum.get("jobs_returned_billed"))
        line("jobs_quota_consumed", cum.get("jobs_quota_consumed"))
        line("physical_requests", cum.get("physical_requests"))
        line("cross_query_duplicates", cum.get("cross_query_duplicates"))
        line("cross_source_duplicates", cum.get("cross_source_duplicates"))
        # The three dedupe exits, stated separately. They partition every posting
        # the lanes kept, so the identity below is checkable by eye:
        #   jobs_unique_kept = net_new + previously_seen + in_run_dup + no_identity
        line("net_new_jobs_captured", cum.get("net_new_jobs_captured"))
        line("historical_previously_seen", cum.get("historical_previously_seen_duplicates"))
        line("canonical_duplicates_in_run", cum.get("canonical_duplicates_in_run"))
        line("postings_missing_identity", cum.get("postings_missing_identity"))
        line("dedupe_reconciles", cum.get("dedupe_reconciles"))
        line("jobs_quota_remaining", cum.get("last_jobs_quota_remaining"))
        for src, s in sorted((cum.get("per_source") or {}).items()):
            novelty = s.get("novelty_pct")
            line(f"  {src}",
                 f"billed={s.get('returned_billed')} kept={s.get('jobs')} "
                 f"net_new={s.get('net_new')} seen={s.get('historical_previously_seen')} "
                 f"dup_in_run={s.get('canonical_duplicates_in_run')} "
                 f"x_source={s.get('cross_source_duplicates')} "
                 f"schema_rej={s.get('schema_rejected')} "
                 f"src_filtered={s.get('source_filtered_out')} "
                 f"requests={s.get('requests')} "
                 f"novelty={novelty if novelty is not None else 'n/a'}% "
                 f"offset {s.get('offset_from')}->{s.get('offset_to')} "
                 f"drained={s.get('drained')} stop={s.get('stop_reason') or '-'}")
        # WINDOW CURSOR: the acceptance evidence for the persisted per-source
        # offset. "Resumed from where the last run stopped" is only provable by
        # printing the offset read at open next to the one written at close.
        wm = ((result.get("lanes") or {}).get("fantastic") or {}).get(
            "attribution", {}).get("watermark") or {}
        if wm.get("enabled"):
            print("---- Acquisition window (canonical date_created cursor) ----")
            line("window_lower", wm.get("lower"))
            line("window_upper", wm.get("upper"))
            line("window_reused", wm.get("window_reused"))
            line("previous_watermark", wm.get("previous_watermark"))
            line("offsets_at_open", wm.get("offsets_at_open"))
            line("offsets_at_close", wm.get("offsets_at_close"))
            line("drained_at_open", wm.get("drained_at_open"))
            line("drained_sources", wm.get("drained_sources"))
            line("undrained_sources", wm.get("undrained_sources"))
            line("window_drained", wm.get("drained"))
            line("watermark_committed", (cum.get("watermark_commit") or {}).get("committed"))
        # PER-SOURCE ECONOMICS: net-new send-safe Airtable rows per 1,000 credits.
        by_source = ((result.get("yield_ledger") or {}).get("by_source") or {})
        if by_source:
            print("---- Yield per source (per 1,000 Fantastic credits) ----")
            for src, g in sorted(by_source.items()):
                line(f"  {src}",
                     f"credits={g.get('credits')} net_new={g.get('net_new')} "
                     f"icp_pass={g.get('icp_pass')} hm_found={g.get('hm_found')} "
                     f"send_safe={g.get('send_safe')} "
                     f"airtable={g.get('airtable_created')} "
                     f"net_new_send_safe={g.get('net_new_send_safe')} "
                     f"| net_new/1k={g.get('net_new_per_1k_credits')} "
                     f"airtable/1k={g.get('airtable_created_per_1k_credits')} "
                     f"nnss/1k={g.get('net_new_send_safe_per_1k_credits')}")
        line("final_stop_reason", acq.get("final_stop_reason"))
        govd = (result.get("governor") or {}).get("decision") or {}
        if govd:
            line("governor_budget", f"{govd.get('run_budget')} reason={govd.get('reason')} "
                                    f"remaining={govd.get('remaining_credits')} reserve={govd.get('reserve_credits')}")
        qr = (result.get("governor") or {}).get("quota_refresh") or {}
        if qr.get("attempted"):
            line("governor_quota_refresh",
                 f"refreshed={qr.get('refreshed')} reason={qr.get('reason')} "
                 f"requests={qr.get('requests_made')} "
                 f"budget {qr.get('budget_before')} -> {qr.get('budget_after')}")
    print("---- Brett's daily metrics ----")
    line("JOBS_ANALYZED", raw)
    line("QUALIFIED", funnel.get("target_role_eligible", funnel.get("icp_eligible_companies")))
    line("CONTACTS_FOUND", contacts)
    line("SENT_TO_AIRTABLE", at_created)
    print("---- Funnel ----")
    line("raw_postings", raw)
    line("unique_opportunities", unique)
    line("target_role_eligible", funnel.get("target_role_eligible"))
    line("companies_considered", funnel.get("companies_considered"))
    line("icp_eligible_companies", funnel.get("icp_eligible_companies"))
    line("icp_rejected_companies", funnel.get("icp_rejected_companies"))
    line("hiring_managers_found", funnel.get("hiring_managers_found"))
    line("contacts_with_email", contacts)
    line("verified_emails", verified_emails)
    line("unverified_emails", unverified_emails)
    line("FINAL_PASS", fp)
    line("NEEDS_CHECK", nc)
    line("UNVERIFIED", uv)
    print("---- Airtable (review-staging, Status=Pending) ----")
    line("airtable_submitted", at_submitted)
    line("airtable_created", at_created)
    line("airtable_existing", at_existing)
    line("airtable_failed", at_failed)
    if skips:
        # Mutually exclusive: submitted - created - failed == sum of these.
        for k in ("skipped_existing", "updated_existing", "company_function_suppressed",
                  "account_suppressed", "no_contact", "send_safe_withheld", "other"):
            line(f"  skip.{k}", skips.get(k))
        line("  person_employer_duplicate (withheld pre-submit)",
             (result.get("delivery") or {}).get("person_employer_duplicate"))
        # State it rather than leaving the reader to add up the columns: an
        # unexplained gap between submitted and created is the thing this line
        # exists to make impossible to miss.
        _unexplained = (int(at_submitted or 0) - int(at_created or 0) - int(at_failed or 0)
                        - sum(int(v or 0) for v in skips.values()))
        line("  skip.UNEXPLAINED", _unexplained)
        line("  reviewable_reconciles",
             (result.get("delivery") or {}).get("reviewable_reconciles"))
    ats = (result.get("lanes") or {}).get("ats") or {}
    if ats:
        attr = ats.get("attribution") or {}
        acct = ats.get("accounting") or {}
        print("---- ATS coverage ----")
        line("ats_registry_size", attr.get("ats_registry_size"))
        line("ats_candidates", attr.get("ats_candidates"))
        line("ats_boards_attempted", acct.get("boards_attempted"))
        line("ats_boards_completed", acct.get("boards_completed"))
        line("ats_boards_failed", acct.get("boards_failed"))
        # Correct key: the accounting dict exposes boards_skipped_by_budget (the
        # previous boards_skipped_budget/boards_skipped keys never existed, so this
        # line always printed blank).
        line("ats_boards_skipped_budget", acct.get("boards_skipped_by_budget"))
        # Rotation visibility: boards deferred to a future slot this run, and why.
        # boards_skipped_by_scheduler == candidates not selected == awaiting their
        # slot; with position now advancing they are covered within cycle_length
        # runs (they are NOT lost). Selection reason breakdown makes the 145->N
        # narrowing self-explaining.
        deferred = acct.get("boards_skipped_by_scheduler")
        line("ats_boards_deferred", deferred)
        line("ats_boards_remaining", deferred)  # not-yet-covered this cycle
        sched = acct.get("scheduler") or {}
        if sched:
            cyc = sched.get("cycle_length")
            print(f"  ats_deferred_reason = awaiting_slot_rotation "
                  f"(cycle_length={cyc}, position={sched.get('position')}; "
                  f"full registry covered every {cyc} runs)")
        line("ats_boards_selected_normal", acct.get("boards_selected_normal"))
        line("ats_boards_selected_overdue", acct.get("boards_selected_overdue"))
        line("ats_boards_selected_retry", acct.get("boards_selected_retry"))
        line("ats_jobs", ats.get("jobs"))
        line("ats_physical_requests", ats.get("physical_requests"))
        if attr.get("ats_candidates_by_provider"):
            for prov, n in sorted((attr.get("ats_candidates_by_provider") or {}).items()):
                print(f"  {prov} = {n}")
    print("---- Top rejection reasons ----")
    for r, n in top5:
        print(f"  {r} = {n}")
    # Hiring-manager coverage + multi-function handling (non-PII counts only).
    hm_obs = funnel.get("hm_observability") or {}
    if hm_obs:
        try:
            import hm_observability
            for ln in hm_observability.stdout_summary(
                    hm_obs.get("hiring_manager") or {}, hm_obs.get("multi_function") or {},
                    hm_obs.get("domain_resolution") or {}):
                print(ln)
        except Exception:  # noqa: BLE001 - summary is best-effort, never fatal
            pass
    pw = result.get("pending_work") or {}
    if pw.get("pending_postings"):
        # Postings already PAID FOR that no run has finished. Printed because the
        # 2026-09-06 loss was invisible: that run reported a clean stop and said
        # nothing about the 226 postings it had bought and then dropped.
        print("---- Acquired work still owed enrichment ----")
        line("  pending_postings", pw.get("pending_postings"))
        line("  pending_runs", pw.get("pending_runs"))
        for row in (pw.get("runs") or [])[:5]:
            print(f"    {row.get('run_id')}  {row.get('postings')} posting(s)")
    _emit_reporting_ledger(state)
    line("reconcile", result["all_reconcile"])
    line("artifacts", state.run_dir())
    print("=============================================")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
