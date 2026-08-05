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
    p.add_argument("--lanes", default="", help="Comma: ats,jsearch,free_feeds. Empty = ats.")
    p.add_argument("--boards", help="ATS board registry JSON (read-only).")
    p.add_argument("--corpus", action="append", default=[])
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
    return p


def _preflight_only(a) -> int:
    """Safe start command for the isolated validation service. Makes ZERO network
    requests, prints only PRESENT/ABSENT (never secret values), and exits 0."""
    import shutil, hashlib
    print("=== replacement-orchestrator preflight-only (zero network) ===")
    print(f"mode(requested)      {a.mode}")
    # Package integrity: verify committed files against the SHA-256 manifest.
    manifest = Path("orchestrator.MANIFEST.sha256")
    if manifest.is_file():
        miss = absent = checked = 0
        for line in manifest.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            sha, _bytes, fname = parts[0], parts[1], parts[2]
            fp = Path(fname)
            if not fp.is_file():
                absent += 1
                continue
            actual = hashlib.sha256(fp.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            checked += 1
            if actual != sha:
                miss += 1
        print(f"package_integrity    checked={checked} mismatch={miss} absent={absent} "
              f"({'OK' if miss == 0 and absent == 0 else 'FAILED'})")
    else:
        print("package_integrity    manifest absent (skipped)")
    for k in ("RAPIDAPI_KEY", "APOLLO_API_KEY", "HUNTER_API_KEY"):
        print(f"{k:20} {'PRESENT' if getattr(config, k, None) else 'ABSENT'}")
    for k in ("AIRTABLE_WRITE_ENABLED", "INSTANTLY_ENROLLMENT_ENABLED",
              "PRODUCTION_STATE_WRITE_ENABLED"):
        print(f"{k:32} {getattr(config, k, '0')} (delivery/prod-writes must be off)")
    boards_path = a.boards or "BOARDS_FINAL.json"
    try:
        from retrieval_measurement.drivers import load_boards_readonly
        boards, err = load_boards_readonly(boards_path)
        import collections
        by = collections.Counter(b.get("provider") for b in boards)
        print(f"boards               {len(boards)} from {boards_path} err={err or 'none'}")
        for prov, n in sorted(by.items()):
            print(f"  {prov:16} {n}")
    except Exception as exc:  # noqa: BLE001
        print(f"boards               ERROR {type(exc).__name__}: {exc}")
    root = Path(a.artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    writable = os.access(str(root), os.W_OK)
    print(f"artifact_root        {root} writable={writable}")
    print(f"disk_free_gb         {round(shutil.disk_usage(str(root)).free/1e9, 1)}")
    print("network_contacted    NONE")
    print("PREFLIGHT OK")
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


def _live_ats_runner(a: argparse.Namespace):
    from retrieval_measurement.drivers import load_boards_readonly
    from free_job_sources import default_fetcher
    from retrieval_measurement import ats_schedule
    boards, err = (load_boards_readonly(a.boards, limit=int(a.max_boards))
                   if a.boards else ([], "no --boards"))

    def runner(m: LaneManager) -> LaneResult:
        if err:
            return LaneResult(lane="ats", status="failed", errors=[err])
        cfg = ats_schedule.SchedulerConfig(mode=config.ATS_SCHEDULER_MODE)
        return real_ats_runner(boards, default_fetcher,
                               checkpoint_dir=str(Path(a.artifact_root) / "checkpoints_ats"),
                               scheduler_config=cfg,
                               detail_budgets={"greenhouse": 100, "workday": 100,
                                               "smartrecruiters": 100})(m)
    return runner


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
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

    ctx = RunContext.create(mode, vars(a), run_id=a.run_id)
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
        enrichment_engine=RealEnrichmentStage(target_final_pass=int(a.target)),
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

    with seam_ctx:
        result = Orchestrator(ctx, state, budget).run(plan, resume=bool(a.resume))

    print(f"run_id      {ctx.run_id}")
    print(f"mode        {mode.value}")
    print(f"status      {result['run']['status']}")
    print(f"postings    {result['waterfall']['unit_totals'].get('postings')}")
    print(f"final_pass  {result['waterfall'].get('final_pass_count')}")
    if result.get("delivery"):
        print(f"delivered   {result['delivery']['created']} "
              f"(reconciles={result['delivery']['airtable_reconciles']})")
    print(f"reconcile   {result['all_reconcile']}")
    print(f"artifacts   {state.run_dir()}")
    return 0 if result["all_reconcile"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
