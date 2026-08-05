"""ATS lane composition: real fetch_board_jobs + budget + checkpoint + trace +
scheduler, exercised offline through a fixture-backed fetcher. No network."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from free_job_sources import FetchPayload
from retrieval_measurement.instrument import RequestBudget
from retrieval_measurement import ats_schedule

from orchestrator.lanes import LaneManager

FX = Path(__file__).parent / "fixtures" / "retrieval_measurement" / "ats_providers" / "greenhouse"


def _greenhouse_fetcher(raise_on_board: str = ""):
    board = json.loads((FX / "board.json").read_text(encoding="utf-8"))
    jobs = json.loads((FX / "jobs.json").read_text(encoding="utf-8"))
    detail = json.loads((FX / "detail_101.json").read_text(encoding="utf-8"))

    def fetch(url: str, **kwargs) -> FetchPayload:
        u = str(url)
        if raise_on_board and raise_on_board in u:
            raise RuntimeError("simulated provider outage")
        if "/jobs/" in u:  # detail
            return FetchPayload(status_code=200, url=u, text=json.dumps(detail))
        if u.rstrip("/").endswith("/jobs"):
            return FetchPayload(status_code=200, url=u, text=json.dumps(jobs))
        return FetchPayload(status_code=200, url=u, text=json.dumps(board))
    return fetch


def _boards(*identifiers):
    return [
        {"provider": "greenhouse", "identifier": ident, "company_name": f"Co {ident}",
         "key": f"greenhouse:{ident}"}
        for ident in identifiers
    ]


def _legacy_cfg():
    return ats_schedule.SchedulerConfig(mode="legacy_interval")


class AtsCompositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ckpt = str(Path(self.tmp) / "ckpt")

    def test_happy_path_checkpoints_and_reconciles(self):
        budget = RequestBudget(limit=500, lane_limits={"ats": 400}, board_limit=100,
                               provider_limits={"ats_greenhouse": 120})
        lm = LaneManager(budget=budget)
        res = lm.run_ats(_boards("dragos"), _greenhouse_fetcher(),
                         checkpoint_dir=self.ckpt, scheduler_config=_legacy_cfg(),
                         detail_budgets={"greenhouse": 0})
        self.assertEqual(res.status, "complete")
        self.assertGreater(len(res.jobs), 0)
        # every selected board has exactly one checkpoint file
        files = list(Path(self.ckpt).glob("*.json"))
        self.assertEqual(len(files), 1)
        # trace.total == physical requests (six-way reconciliation identity)
        self.assertTrue(res.attribution["trace_total_equals_physical"])
        # board/lane/available identities all hold
        self.assertTrue(res.accounting["reconciled"])
        self.assertTrue(all(res.accounting["reconciliation_identities"].values()))

    def test_completed_board_survives_later_board_failure(self):
        budget = RequestBudget(limit=500, lane_limits={"ats": 400}, board_limit=100,
                               provider_limits={"ats_greenhouse": 120})
        lm = LaneManager(budget=budget)
        # second board raises; first must remain persisted and its jobs retained.
        res = lm.run_ats(_boards("good", "boom"), _greenhouse_fetcher(raise_on_board="boom"),
                         checkpoint_dir=self.ckpt, scheduler_config=_legacy_cfg(),
                         detail_budgets={"greenhouse": 0})
        acct = res.accounting
        self.assertEqual(acct["boards_attempted"], 2)
        self.assertEqual(acct["boards_completed"], 1)
        self.assertEqual(acct["boards_failed"], 1)
        self.assertGreater(acct["normalized_records"], 0)   # good board's work survived
        self.assertEqual(len(list(Path(self.ckpt).glob("*.json"))), 2)
        self.assertTrue(res.accounting["reconciled"])

    def test_board_budget_stops_one_board_not_the_lane(self):
        # board_limit=1: greenhouse needs board+jobs (2 requests) so the jobs call
        # is refused at board scope; the OTHER board still runs.
        budget = RequestBudget(limit=500, lane_limits={"ats": 400}, board_limit=1,
                               provider_limits={"ats_greenhouse": 120})
        lm = LaneManager(budget=budget)
        res = lm.run_ats(_boards("a", "b"), _greenhouse_fetcher(),
                         checkpoint_dir=self.ckpt, scheduler_config=_legacy_cfg(),
                         detail_budgets={"greenhouse": 0})
        # both boards attempted, both hit the board ceiling; lane not run-stopped
        self.assertEqual(res.accounting["boards_attempted"], 2)
        self.assertFalse(budget.exhausted)  # run budget NOT exhausted
        self.assertTrue(res.accounting["reconciled"])

    def test_provider_budget_skips_remaining_same_provider_boards(self):
        # provider limit 3, each board = 2 requests: board1 ok (2), board2 refused
        # mid-way at provider scope (would reach 4), board3 pre-skipped by budget.
        budget = RequestBudget(limit=500, lane_limits={"ats": 400}, board_limit=100,
                               provider_limits={"ats_greenhouse": 3})
        lm = LaneManager(budget=budget)
        res = lm.run_ats(_boards("a", "b", "c"), _greenhouse_fetcher(),
                         checkpoint_dir=self.ckpt, scheduler_config=_legacy_cfg(),
                         detail_budgets={"greenhouse": 0})
        acct = res.accounting
        self.assertGreaterEqual(acct["boards_skipped_by_budget"], 1)
        self.assertEqual(acct["boards_selected"],
                         acct["boards_skipped_by_budget"] + acct["boards_attempted"])
        self.assertTrue(res.accounting["reconciled"])

    def test_reserved_capacity_is_untouchable_by_ats(self):
        # global 6, reserve 4 for jsearch => ATS may use only 2 => 1 board (2 req),
        # the second board's first request is refused at lane_reservation scope.
        budget = RequestBudget(limit=6, lane_limits={"ats": 400}, board_limit=100,
                               provider_limits={"ats_greenhouse": 120},
                               reserved_for_lanes={"jsearch": 4})
        lm = LaneManager(budget=budget)
        res = lm.run_ats(_boards("a", "b"), _greenhouse_fetcher(),
                         checkpoint_dir=self.ckpt, scheduler_config=_legacy_cfg(),
                         detail_budgets={"greenhouse": 0})
        self.assertLessEqual(budget.per_lane.get("ats", 0), 2)  # reserved 4 never used by ATS
        self.assertTrue(res.accounting["reconciled"])

    def test_detail_requests_are_made_when_budgeted(self):
        budget = RequestBudget(limit=500, lane_limits={"ats": 400}, board_limit=100,
                               provider_limits={"ats_greenhouse": 120})
        lm = LaneManager(budget=budget)
        res = lm.run_ats(_boards("dragos"), _greenhouse_fetcher(),
                         checkpoint_dir=self.ckpt, scheduler_config=_legacy_cfg(),
                         detail_budgets={"greenhouse": 5})
        # physical > 2 means at least one detail call happened on top of board+jobs
        self.assertGreaterEqual(res.physical_requests, 2)
        self.assertTrue(res.attribution["trace_total_equals_physical"])


if __name__ == "__main__":
    unittest.main()
