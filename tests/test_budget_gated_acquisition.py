"""Paid acquisition stands down when nothing authorizes the enrichment it needs.

This is the property that lets the service be left ARMED. Without it, running on a
schedule with no grant means every night buys Fantastic rows, stops at the first
chargeable Apollo call and files the rows in custody -- nothing lost, nothing
produced, and the next authorization spent on inventory bought at a worse moment.
With it, `FANTASTIC_JOBS_ENABLED` can stay on permanently, so resuming after a
top-up is one action instead of three.

The gate is narrow on purpose. It stops PAID lanes only: custody, enrichment,
delivery and free lanes are untouched, and a run that stands down is a recoverable
wait rather than a failure.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from orchestrator import apollo_budget as budget
from tests.test_throughput_contract import RecoveryProductionLoop


class TheGateItself(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "budget.json"

    def _cfg(self, **over):
        base = dict(APOLLO_RECOVERY_BUDGET_ENABLED=True,
                    APOLLO_RECOVERY_BUDGET_ID="grant-1",
                    APOLLO_RECOVERY_BUDGET_CALLS=5,
                    APOLLO_RECOVERY_BUDGET_STATE_PATH=str(self.path))
        base.update(over)
        return mock.patch.multiple(config, **base)

    def test_an_unset_grant_stands_paid_acquisition_down(self):
        with self._cfg(APOLLO_RECOVERY_BUDGET_ID="", APOLLO_RECOVERY_BUDGET_CALLS=0):
            out = budget.paid_acquisition_allowed(str(self.path))
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], "no_enrichment_authorization")

    def test_a_zero_call_grant_stands_paid_acquisition_down(self):
        with self._cfg(APOLLO_RECOVERY_BUDGET_CALLS=0):
            out = budget.paid_acquisition_allowed(str(self.path))
        self.assertFalse(out["allowed"])
        self.assertIn("nothing is authorized", out["detail"])

    def test_a_spent_grant_stands_it_down_and_says_a_new_id_is_needed(self):
        with self._cfg(APOLLO_RECOVERY_BUDGET_CALLS=1):
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))
            out = budget.paid_acquisition_allowed(str(self.path))
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], "enrichment_authorization_spent")
        self.assertIn("NEW id", out["detail"])

    def test_a_live_grant_allows_it(self):
        with self._cfg():
            out = budget.paid_acquisition_allowed(str(self.path))
        self.assertTrue(out["allowed"])
        self.assertEqual(out["reason"], "authorized")
        self.assertEqual(out["remaining"], 5)

    def test_switching_the_ceiling_off_does_not_become_a_second_stop(self):
        """No authorization model in force must not silently halt acquisition.

        The gate exists to protect a grant. Where there is no grant mechanism at
        all, it has nothing to protect and must not invent a reason to stop.
        """
        with self._cfg(APOLLO_RECOVERY_BUDGET_ENABLED=False,
                       APOLLO_RECOVERY_BUDGET_ID="", APOLLO_RECOVERY_BUDGET_CALLS=0):
            out = budget.paid_acquisition_allowed(str(self.path))
        self.assertTrue(out["allowed"])
        self.assertEqual(out["reason"], "budget_not_enabled")

    def test_free_lanes_are_recognised_and_paid_ones_are_not(self):
        self.assertTrue(budget.is_free_lane("ats"))
        self.assertTrue(budget.is_free_lane("ats_greenhouse"))
        self.assertFalse(budget.is_free_lane("fantastic"))
        self.assertFalse(budget.is_free_lane("atsomething"))


class TheRunStandsDownWithoutLosingAnything(RecoveryProductionLoop):
    """Through the real orchestrator, with the boundaries faked."""

    def _run(self, *, authorized):
        path = Path(tempfile.mkdtemp()) / "budget.json"
        cfg = dict(APOLLO_RECOVERY_BUDGET_ENABLED=True,
                   APOLLO_RECOVERY_BUDGET_ID="grant-live" if authorized else "",
                   APOLLO_RECOVERY_BUDGET_CALLS=50 if authorized else 0,
                   APOLLO_RECOVERY_BUDGET_STATE_PATH=str(path))
        with mock.patch.multiple(config, **cfg):
            return self.exercise(60, 20, 100000, continue_after=True)

    def test_an_unauthorized_run_buys_nothing_and_still_drains_custody(self):
        """No acquisition attempt, and the queued work is still done.

        Both halves matter. Standing acquisition down must not become "the run does
        nothing": postings already paid for are exactly the work that should proceed.
        """
        result, reached, acquisitions, root = self._run(authorized=False)
        cumulative = result["acquisition"]["cumulative"]
        self.assertFalse(cumulative["paid_acquisition_authorization"]["allowed"])
        stood_down = cumulative.get("paid_acquisition_stood_down")
        self.assertIsNotNone(stood_down, "the stand-down must be recorded, not silent")
        self.assertIn("fantastic", stood_down["lanes"])
        self.assertEqual(acquisitions, [], "no paid lane was invoked")
        self.assertEqual(len(set(reached)), 60, "custody still drained")

    def test_an_authorized_run_acquires_as_before(self):
        result, reached, _, _ = self._run(authorized=True)
        cumulative = result["acquisition"]["cumulative"]
        self.assertTrue(cumulative["paid_acquisition_authorization"]["allowed"])
        self.assertNotIn("paid_acquisition_stood_down", cumulative)
        self.assertEqual(len(set(reached)), 60)

    def test_the_stand_down_is_not_a_failed_run(self):
        """A recoverable wait, not an error: nothing to page anyone about."""
        result, _, _, _ = self._run(authorized=False)
        self.assertNotEqual(result.get("status"), "failed")
        self.assertNotEqual(result["topup"].get("final_stop_reason"),
                            "acquisition_failed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
