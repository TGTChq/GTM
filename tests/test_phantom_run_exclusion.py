"""A maintenance pass must leave no trace a report can mistake for a run.

Found by running the maintenance pass against production on 2026-09-06. It
constructed a `StateManager` with a run id, which CREATES A RUN DIRECTORY; the
ledger backfill then lifted that empty directory in as an INTERRUPTED RUN, and the
census counted it as an eligible run that had failed to record its metrics.

The damage was not cosmetic. With the phantom included, Brett's report read:

    Jobs: captured not measured for the full period / reviewed not measured
    "1 of 4 runs in this period did not record net-new captured postings"

...while the census underneath it had `jobs_captured census=6431 reported=6431
agrees=True`. The one run that "did not record" its capture was the maintenance
pass itself. Every headline metric was degraded from `measured` to `partial` by a
process that acquires nothing and reviews nothing.

Two fixes, both pinned here: the entry point no longer creates a run directory,
and `drop_empty_run` removes the one already written -- refusing anything that
carries evidence.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import run_maintenance
import run_orchestrator


class TheEntryPointCreatesNoRunDirectory(unittest.TestCase):
    def test_delegation_precedes_state_manager_construction(self):
        """Ordering IS the fix: `StateManager(..., run_id=...)` is what creates the
        directory, so returning after it would still leave the phantom behind."""
        import inspect

        source = inspect.getsource(run_orchestrator)
        branch = source.index('getattr(config, "MAINTENANCE_ONLY"')
        state = source.index("state = StateManager(a.artifact_root, policy")
        ctx = source.index("ctx = RunContext.create(mode, _identity_arguments(a)")
        self.assertLess(branch, ctx, "must return before a RunContext exists")
        self.assertLess(branch, state, "must return before a run directory is created")


class DroppingAPhantomIsGuarded(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "reporting_ledger").mkdir(parents=True, exist_ok=True)

    def _phantom(self, run_id="20260906T102006Z-c4c91c8e", metrics=None, state="incomplete"):
        (self.root / "run_artifacts" / run_id).mkdir(parents=True, exist_ok=True)
        entry = {"run_id": run_id, "state": state}
        if metrics is not None:
            entry["metrics"] = metrics
        (self.root / "reporting_ledger" / f"{run_id}.json").write_text(
            json.dumps(entry), encoding="utf-8")
        return run_id

    def test_a_genuine_phantom_is_removed(self):
        run_id = self._phantom()
        out = run_maintenance.drop_empty_run(self.root, run_id)
        self.assertEqual(out["refused"], "")
        self.assertTrue(out["removed_dir"])
        self.assertTrue(out["removed_ledger"])
        self.assertFalse((self.root / "run_artifacts" / run_id).exists())
        self.assertFalse((self.root / "reporting_ledger" / f"{run_id}.json").exists())

    def test_a_directory_holding_any_artifact_is_refused(self):
        run_id = self._phantom()
        (self.root / "run_artifacts" / run_id / "orchestrator_result.json").write_text(
            "{}", encoding="utf-8")
        out = run_maintenance.drop_empty_run(self.root, run_id)
        self.assertIn("file(s)", out["refused"])
        self.assertTrue((self.root / "run_artifacts" / run_id).exists())

    def test_a_ledger_entry_with_metrics_is_refused(self):
        run_id = self._phantom(metrics={"net_new_jobs_captured": 226})
        out = run_maintenance.drop_empty_run(self.root, run_id)
        self.assertIn("metrics", out["refused"])
        self.assertTrue((self.root / "reporting_ledger" / f"{run_id}.json").exists())

    def test_a_completed_run_is_refused(self):
        run_id = self._phantom(state="complete")
        out = run_maintenance.drop_empty_run(self.root, run_id)
        self.assertIn("complete", out["refused"])
        self.assertTrue((self.root / "reporting_ledger" / f"{run_id}.json").exists())


if __name__ == "__main__":
    unittest.main()
