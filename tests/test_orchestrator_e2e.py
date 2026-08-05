"""End-to-end offline orchestration: lane selection, isolation, resume,
network isolation, and FINAL_PASS-only target satisfaction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

import requests

from retrieval_measurement.instrument import RequestBudget

from orchestrator.delivery import DeliveryManager, FakeAirtableAdapter, FakeInstantlyAdapter
from orchestrator.enrichment import EnrichmentEngine, FakeEnrichmentAdapter
from orchestrator.lanes import LaneManager, LaneResult
from orchestrator.modes import ExecutionMode, policy_for
from orchestrator.pipeline import Orchestrator, OrchestratorPlan
from orchestrator.runcontrol import RunContext
from orchestrator.state import StateManager


def _postings(n, prefix="P", good=True):
    out = []
    for i in range(n):
        out.append({
            "job_id": f"{prefix}-{i:04d}", "job_title": "Senior Engineer",
            "employer_name": f"{prefix}Co{i}", "company_size": 120 if good else 1,
            "job_posted_at_datetime_utc": "2026-08-05T00:00:00Z",
            "active_hiring": good, "same_day": good, "_acquisition_source": prefix,
        })
    return out


def _lane(name, jobs, *, raise_it=False):
    def runner(_mgr: LaneManager) -> LaneResult:
        if raise_it:
            raise RuntimeError(f"lane {name} blew up")
        return LaneResult(lane=name, status="complete", jobs=list(jobs))
    return runner


def _plan(lane_runners, *, adapter=None, target=5, write=True, approve=True, state=None):
    return OrchestratorPlan(
        lanes=list(lane_runners.keys()),
        lane_runners=lane_runners,
        enrichment_engine=EnrichmentEngine(adapter or FakeEnrichmentAdapter()),
        delivery_manager=DeliveryManager(
            state=state, airtable=FakeAirtableAdapter(), instantly=FakeInstantlyAdapter(),
            enable_airtable_write=write, auto_approve=approve, enable_instantly=True),
        target=target,
    )


def _mk(mode=ExecutionMode.FULL_DRY_RUN, run_id="RID"):
    tmp = tempfile.mkdtemp()
    ctx = RunContext.create(mode, {"t": 1}, run_id=run_id)
    state = StateManager(tmp, policy_for(mode), run_id=run_id)
    budget = RequestBudget(limit=500, lane_limits={"ats": 400},
                           reserved_for_lanes={"jsearch": 50, "free_feeds": 50})
    return ctx, state, budget, tmp


class E2ETests(unittest.TestCase):
    def test_all_lanes_and_isolation(self):
        ctx, state, budget, _ = _mk()
        runners = {
            "ats": _lane("ats", _postings(3, "A")),
            "jsearch": _lane("jsearch", _postings(2, "J"), raise_it=True),  # fails
            "free_feeds": _lane("free_feeds", _postings(4, "F")),
        }
        res = Orchestrator(ctx, state, budget).run(_plan(runners, state=state))
        # failing jsearch did not erase the others
        self.assertEqual(res["lanes"]["jsearch"]["status"], "failed")
        self.assertEqual(res["lanes"]["ats"]["status"], "complete")
        self.assertEqual(res["lanes"]["free_feeds"]["status"], "complete")
        self.assertEqual(res["waterfall"]["unit_totals"]["postings"], 7)  # 3 + 4
        self.assertTrue(res["all_reconcile"])

    def test_single_lane_selection(self):
        for lane in ("ats", "jsearch", "free_feeds"):
            ctx, state, budget, _ = _mk()
            res = Orchestrator(ctx, state, budget).run(
                _plan({lane: _lane(lane, _postings(3, lane[:1].upper()))}, state=state))
            self.assertEqual(list(res["lanes"].keys()), [lane])
            self.assertTrue(res["all_reconcile"])

    def test_target_reached_through_final_pass_only(self):
        ctx, state, budget, _ = _mk()
        res = Orchestrator(ctx, state, budget).run(
            _plan({"ats": _lane("ats", _postings(10, "A"))}, target=10, state=state))
        self.assertEqual(res["waterfall"]["final_pass_count"], 10)
        self.assertTrue(res["target_satisfied_by_final_pass_only"])
        self.assertEqual(res["delivery"]["created"], 10)

    def test_target_not_reached_is_honest(self):
        ctx, state, budget, _ = _mk()
        # size=1 => every company fails ICP size => 0 FINAL_PASS
        res = Orchestrator(ctx, state, budget).run(
            _plan({"ats": _lane("ats", _postings(10, "A", good=False))}, target=5, state=state))
        self.assertEqual(res["waterfall"]["final_pass_count"], 0)
        self.assertFalse(res["target_satisfied_by_final_pass_only"])
        self.assertTrue(res["all_reconcile"])          # not reaching target still reconciles
        self.assertIn("company_size_rejected", res["enrichment"]["loss_census"])

    def test_resume_after_interruption_makes_no_new_acquisition(self):
        ctx, state, budget, tmp = _mk(run_id="RESUME")
        Orchestrator(ctx, state, budget).run(
            _plan({"ats": _lane("ats", _postings(6, "A"))}, state=state))
        # Second run resumes: the lane runner would RAISE if called -- it must not be.
        ctx2 = RunContext.create(ExecutionMode.FULL_DRY_RUN, {"t": 1}, run_id="RESUME")
        state2 = StateManager(tmp, policy_for(ExecutionMode.FULL_DRY_RUN), run_id="RESUME")
        res = Orchestrator(ctx2, state2, budget).run(
            _plan({"ats": _lane("ats", [], raise_it=True)}, state=state2), resume=True)
        self.assertEqual(res["run"]["resumed_from"], "acquisition_checkpoint")
        self.assertEqual(res["waterfall"]["unit_totals"]["postings"], 6)  # from checkpoint
        self.assertTrue(res["all_reconcile"])

    def test_offline_modes_make_zero_network_calls(self):
        original = requests.request

        def blow_up(*a, **k):
            raise AssertionError("offline mode attempted a network request")

        requests.request = blow_up
        try:
            ctx, state, budget, _ = _mk()
            res = Orchestrator(ctx, state, budget).run(
                _plan({"ats": _lane("ats", _postings(5, "A"))}, state=state))
            self.assertTrue(res["all_reconcile"])
        finally:
            requests.request = original


if __name__ == "__main__":
    unittest.main()
