"""Top-up interaction with the two-phase Fantastic acquisition (incident fix).

Pins the Phase-3 contract: within one adaptive top-up run the FRESH-EDGE head query
is issued at most ONCE. The pipeline runs slice 1 as ``head_then_deep`` (discover new
jobs, then backfill) and every later slice as ``deep`` only, so top-up never re-bills
the top-of-feed head query while draining the historical backlog toward the target.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import config
from orchestrator.modes import ExecutionMode as EM, policy_for as pf
from orchestrator.runcontrol import RunContext
from orchestrator.state import StateManager
from orchestrator.lanes import LaneResult
from orchestrator.pipeline import Orchestrator, OrchestratorPlan
from orchestrator.enrichment import EnrichmentReport


class _Budget:
    lane = source = None
    def reserve(self, *a, **k): return True
    def to_dict(self): return {}


class _Engine:
    def run(self, opportunities, **k):
        return EnrichmentReport(leads=[], stages=[], loss_census={})


class _Delivery:
    def deliver(self, leads, **k):
        from orchestrator.adapters_real import RealDeliveryReport
        return RealDeliveryReport(entered=0)


class FantasticTopupAcquireModeTests(unittest.TestCase):
    def test_head_runs_once_then_deep_for_remaining_slices(self):
        tmp = tempfile.mkdtemp()
        policy = pf(EM.FULL_DRY_RUN)
        self.assertTrue(policy.allow_enrichment)  # topup path requires enrichment
        ctx = RunContext.create(EM.FULL_DRY_RUN, {"mode": "full_dry_run"},
                                run_id="20260821T130000Z-mode")
        state = StateManager(tmp, policy, run_id=ctx.run_id)

        seen_modes = []
        # Slice 1 returns a job (billed>0 -> keep going); slice 2 returns nothing
        # (billed==0 -> inventory_exhausted stops the loop after two iterations).
        def runner(_manager):
            i = len(seen_modes)
            seen_modes.append(getattr(config, "FANTASTIC_JOBS_ACQUIRE_MODE", "head_then_deep"))
            jobs = [{"job_id": f"j{i}", "posting_id": f"j{i}"}] if i == 0 else []
            return LaneResult(lane="fantastic", status="complete", jobs=jobs)

        plan = OrchestratorPlan(lanes=["fantastic"], lane_runners={"fantastic": runner},
                                enrichment_engine=_Engine(), delivery_manager=_Delivery())

        with mock.patch.multiple(
            config,
            NET_NEW_SEND_SAFE_TARGET=5,               # > 0 -> adaptive top-up path
            FANTASTIC_JOBS_MAX_JOBS_PER_RUN=1000,
            FANTASTIC_TOPUP_SLICE_JOBS=500,
            FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0,
            TOPUP_RUNTIME_BUDGET_SECONDS=0,
            TOPUP_MAX_ITERATIONS=40,
            PRE_APOLLO_EXISTING_DEDUPE=False,         # no Airtable snapshot in the test
            FANTASTIC_JOBS_ACQUIRE_MODE="head_then_deep",
        ):
            Orchestrator(ctx, state, _Budget()).run(plan, resume=False)

        self.assertGreaterEqual(len(seen_modes), 2)
        self.assertEqual(seen_modes[0], "head_then_deep")             # slice 1 = fresh edge
        self.assertTrue(all(m == "deep" for m in seen_modes[1:]))     # later slices = deep only
        # The head (top-of-feed) query is therefore issued in exactly one slice.
        self.assertEqual(seen_modes.count("head_then_deep"), 1)
        # And the mode is restored after the run.
        self.assertEqual(config.FANTASTIC_JOBS_ACQUIRE_MODE, "head_then_deep")


if __name__ == "__main__":
    unittest.main()
