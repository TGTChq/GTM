"""One-run lock: atomic acquire, second-invocation refusal, finally-release,
explicit stale recovery, and lock state recorded in run artifacts."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from orchestrator.runlock import RunLock, RunLockHeld


class RunLockTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "orchestrator_v2" / ".run.lock"

    def test_acquire_records_pid_and_run_id(self):
        with RunLock(self.path, "RID-1") as lock:
            self.assertTrue(lock.acquired)
            info = json.loads(self.path.read_text())
            self.assertEqual(info["run_id"], "RID-1")
            self.assertEqual(info["pid"], os.getpid())
        # released in __exit__
        self.assertFalse(self.path.exists())

    def test_second_invocation_refuses_while_active(self):
        first = RunLock(self.path, "RID-1").acquire()
        try:
            with self.assertRaises(RunLockHeld):
                RunLock(self.path, "RID-2").acquire()
        finally:
            first.release()
        # after release, a new run may acquire
        second = RunLock(self.path, "RID-2").acquire()
        self.assertTrue(second.acquired)
        second.release()

    def test_release_in_finally_frees_lock(self):
        lock = RunLock(self.path, "RID")
        try:
            lock.acquire()
            raise ValueError("boom")
        except ValueError:
            pass
        finally:
            lock.release()
        self.assertFalse(self.path.exists())

    def test_explicit_stale_recovery(self):
        # Write a lock with an ancient timestamp -> treated as stale.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "run_id": "OLD", "pid": 999999, "acquired_at": "2000-01-01T00:00:00+00:00",
            "acquired_at_epoch": time.time() - 10 * 3600}))
        lock = RunLock(self.path, "RID-NEW", stale_seconds=6 * 3600).acquire()
        self.assertTrue(lock.recovered_stale)
        self.assertEqual(json.loads(self.path.read_text())["run_id"], "RID-NEW")
        lock.release()

    def test_fresh_lock_not_recovered(self):
        held = RunLock(self.path, "RID-A").acquire()
        try:
            lock = RunLock(self.path, "RID-B", stale_seconds=6 * 3600)
            with self.assertRaises(RunLockHeld):
                lock.acquire()
            self.assertFalse(lock.recovered_stale)
        finally:
            held.release()

    def test_to_dict_has_lock_state(self):
        with RunLock(self.path, "RID") as lock:
            d = lock.to_dict()
            self.assertTrue(d["acquired"])
            self.assertEqual(d["holder"]["run_id"], "RID")
            self.assertIn("lock_path", d)


class OrchestratorLockIntegrationTests(unittest.TestCase):
    def test_run_records_lock_and_refuses_concurrent(self):
        import tempfile as _t
        from orchestrator.modes import ExecutionMode, policy_for
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from orchestrator.pipeline import Orchestrator, OrchestratorPlan
        from orchestrator.enrichment import EnrichmentEngine, FakeEnrichmentAdapter
        from orchestrator.delivery import DeliveryManager, FakeAirtableAdapter, FakeInstantlyAdapter
        from orchestrator.lanes import LaneManager, LaneResult
        from retrieval_measurement.instrument import RequestBudget

        tmp = _t.mkdtemp()
        ctx = RunContext.create(ExecutionMode.FULL_DRY_RUN, {}, run_id="LOCKRUN")
        state = StateManager(tmp, policy_for(ExecutionMode.FULL_DRY_RUN), run_id="LOCKRUN")
        budget = RequestBudget(limit=100)

        def runner(_m: LaneManager) -> LaneResult:
            # while this lane runs, a concurrent orchestrator must refuse
            held = RunLock(state.root / ".run.lock", "OTHER")
            with self.assertRaises(RunLockHeld):
                held.acquire()
            return LaneResult(lane="ats", status="complete", jobs=[])

        plan = OrchestratorPlan(
            lanes=["ats"], lane_runners={"ats": runner},
            enrichment_engine=EnrichmentEngine(FakeEnrichmentAdapter()),
            delivery_manager=DeliveryManager(
                state=state, airtable=FakeAirtableAdapter(), instantly=FakeInstantlyAdapter(),
                enable_airtable_write=False, auto_approve=False, enable_instantly=False))
        res = Orchestrator(ctx, state, budget).run(plan)
        self.assertIsNotNone(res["run_lock"])
        self.assertFalse((state.root / ".run.lock").exists())   # released in finally


if __name__ == "__main__":
    unittest.main()
