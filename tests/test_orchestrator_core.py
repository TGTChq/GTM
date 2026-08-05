"""Modes, state ownership, waterfall reconciliation, capacity."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.modes import DEFAULT_MODE, ExecutionMode, policy_for
from orchestrator.state import StateManager, StateWriteViolation, StateSchemaError
from orchestrator.reasons import Disposition, ReasonCode, StageOutcome
from orchestrator.waterfall import (
    ReconciliationError,
    WaterfallReport,
    reconcile_stage,
)


class ModeTests(unittest.TestCase):
    def test_production_is_never_the_default(self):
        self.assertIsNot(DEFAULT_MODE, ExecutionMode.PRODUCTION)
        self.assertEqual(DEFAULT_MODE, ExecutionMode.FULL_DRY_RUN)

    def test_offline_modes_forbid_network_and_production_writes(self):
        for m in (ExecutionMode.OFFLINE_REPLAY, ExecutionMode.FULL_DRY_RUN):
            p = policy_for(m)
            self.assertFalse(p.allow_network)
            self.assertFalse(p.allow_production_state_write)
            self.assertFalse(p.allow_live_enrichment)

    def test_live_acquisition_only_is_acquisition_only(self):
        p = policy_for(ExecutionMode.LIVE_ACQUISITION_ONLY)
        self.assertTrue(p.allow_live_acquisition)
        self.assertFalse(p.allow_enrichment)
        self.assertFalse(p.allow_airtable_write)
        self.assertFalse(p.allow_production_state_write)

    def test_only_production_may_write_production_state(self):
        self.assertTrue(policy_for(ExecutionMode.PRODUCTION).allow_production_state_write)
        self.assertTrue(policy_for(ExecutionMode.PRODUCTION).requires_production_ack())


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.policy = policy_for(ExecutionMode.FULL_DRY_RUN)
        self.sm = StateManager(self.tmp, self.policy, run_id="RID")

    def test_atomic_versioned_write_and_read(self):
        self.sm.write_json("scheduler_state", "s.json", {"carried": ["a"]})
        got = self.sm.read_json("scheduler_state", "s.json")
        self.assertEqual(got["carried"], ["a"])
        self.assertIn("schema_version", got)

    def test_foreign_schema_is_refused(self):
        p = self.sm.store_path("scheduler_state") / "x.json"
        p.write_text('{"schema_version": "other/9"}', encoding="utf-8")
        with self.assertRaises(StateSchemaError):
            self.sm.read_json("scheduler_state", "x.json")

    def test_no_production_write_outside_root_in_offline(self):
        # A store path escaping the run root is refused before writing.
        outside = Path(self.tmp).parent / "escape.json"
        self.sm._paths["delivery_state"] = outside.parent  # force an out-of-root target
        with self.assertRaises(StateWriteViolation):
            self.sm.write_json("delivery_state", "escape.json", {"x": 1})

    def test_read_only_snapshot_never_opens_for_write(self):
        snap = self.sm.seen_snapshot()
        self.assertFalse(snap.describe()["write_capable"])

    def test_bounded_retention(self):
        base = self.sm.store_path("run_artifacts")
        for name in ("20260101T000000Z-a", "20260102T000000Z-b", "20260103T000000Z-c"):
            (base / name).mkdir()
        removed = self.sm.prune(keep=1)
        self.assertEqual(len(removed), 2)


class WaterfallTests(unittest.TestCase):
    def test_stage_identity_holds(self):
        d = [
            (StageOutcome.PASSED, ReasonCode.OK, None),
            (StageOutcome.REJECTED, ReasonCode.NOT_ICP, None),
            (StageOutcome.DEFERRED, ReasonCode.EMAIL_UNVERIFIED, None),
            (StageOutcome.ERRORED, ReasonCode.STAGE_ERROR, None),
        ]
        res = reconcile_stage("s", "u", d)
        self.assertTrue(res.reconciles())
        self.assertEqual(res.entered, 4)
        self.assertEqual(res.passed + res.rejected + res.deferred + res.errored, 4)

    def test_target_satisfied_only_by_final_pass(self):
        r = WaterfallReport()
        r.census([Disposition.FINAL_PASS] * 5 + [Disposition.NEEDS_CHECK] * 100
                 + [Disposition.UNVERIFIED] * 50)
        self.assertEqual(r.final_pass_count(), 5)
        self.assertEqual(r.reviewable_count(), 150)
        # 150 reviewable records can never satisfy a target of 5
        self.assertFalse(r.target_satisfied(5, delivered_final_pass=0))
        self.assertTrue(r.target_satisfied(5, delivered_final_pass=5))
        # cannot claim more delivered FINAL_PASS than exist
        self.assertFalse(r.target_satisfied(5, delivered_final_pass=6))

    def test_reviewable_cannot_be_counted_as_final_pass(self):
        r = WaterfallReport()
        r.census([Disposition.NEEDS_CHECK, Disposition.UNVERIFIED])
        self.assertEqual(r.final_pass_count(), 0)


if __name__ == "__main__":
    unittest.main()
