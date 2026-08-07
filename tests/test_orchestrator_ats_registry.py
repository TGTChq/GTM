"""ATS registry wiring for the definitive orchestrator (Q2).

The production ATS lane is driven by the FULL board registry with health-aware
deterministic scheduling, not the 6-board validation snapshot. These tests lock
in the novel piece: per-board health is persisted back to the registry after a
run so quarantine (slot-only deprioritization) and recovery (reset on success)
accrue across runs.
"""

from __future__ import annotations

import unittest

import run_orchestrator
from orchestrator.lanes import LaneResult


class RecordBoardHealthTests(unittest.TestCase):
    def _result(self, boards):
        return LaneResult(lane="ats", status="complete",
                          accounting={"session": {"boards": boards}})

    def test_success_and_failure_recorded_budget_skips_ignored(self):
        calls = []

        class FakeRegistry:
            entries = {"x": {}, "y": {}}

            def record_result(self, key, *, success, job_count, error, save):
                calls.append((key, success, job_count, error, save))

            def save(self):
                calls.append(("SAVE", None, None, None, None))

        result = self._result([
            {"provider": "greenhouse", "identifier": "acme", "canonical_records": 7, "error": ""},
            {"provider": "lever", "identifier": "beta", "canonical_records": 0, "error": "HTTP 500"},
            {"provider": "ashby", "identifier": "gamma", "skipped_by_budget": True},
            {"provider": "workday", "identifier": "delta", "skipped_by_scheduler": True},
        ])
        run_orchestrator._record_ats_board_health(FakeRegistry(), result)

        recorded = {c[0]: c for c in calls if c[0] != "SAVE"}
        # Only the two attempted boards are recorded; skipped boards are not.
        self.assertEqual(set(recorded), {"greenhouse:acme", "lever:beta"})
        self.assertTrue(recorded["greenhouse:acme"][1])   # success
        self.assertEqual(recorded["greenhouse:acme"][2], 7)  # job_count
        self.assertFalse(recorded["lever:beta"][1])       # failure
        self.assertEqual(recorded["lever:beta"][3], "HTTP 500")
        self.assertTrue(any(c[0] == "SAVE" for c in calls))  # persisted once
        self.assertEqual(result.attribution["ats_registry_health_updated"], 2)

    def test_no_attempted_boards_saves_nothing(self):
        calls = []

        class FakeRegistry:
            entries = {}

            def record_result(self, *a, **k):
                calls.append(("record",))

            def save(self):
                calls.append(("SAVE",))

        result = self._result([
            {"provider": "greenhouse", "identifier": "acme", "skipped_by_budget": True},
        ])
        run_orchestrator._record_ats_board_health(FakeRegistry(), result)
        self.assertEqual(calls, [])  # nothing attempted -> no record, no save
        self.assertEqual(result.attribution["ats_registry_health_updated"], 0)

    def test_health_uses_real_registry_consecutive_failures(self):
        """End-to-end against a real AtsBoardRegistry: a failure increments
        consecutive_failures (bounded retry -> eventual slot-only quarantine); a
        later success resets it (recovery)."""
        import tempfile
        from pathlib import Path
        from ats_board_registry import AtsBoardRegistry

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "reg.json")
            reg = AtsBoardRegistry(path)
            key = "greenhouse:acme"
            reg.entries[key] = {
                "provider": "greenhouse", "identifier": "acme",
                "company_name": "Acme", "consecutive_failures": 0,
            }
            reg.save()

            fail = LaneResult(lane="ats", status="partial", accounting={"session": {"boards": [
                {"provider": "greenhouse", "identifier": "acme", "canonical_records": 0, "error": "HTTP 500"},
            ]}})
            run_orchestrator._record_ats_board_health(reg, fail)
            self.assertEqual(int(AtsBoardRegistry(path).entries[key]["consecutive_failures"]), 1)

            ok = LaneResult(lane="ats", status="complete", accounting={"session": {"boards": [
                {"provider": "greenhouse", "identifier": "acme", "canonical_records": 4, "error": ""},
            ]}})
            run_orchestrator._record_ats_board_health(reg, ok)
            reloaded = AtsBoardRegistry(path).entries[key]
            self.assertEqual(int(reloaded["consecutive_failures"]), 0)  # recovered
            self.assertEqual(int(reloaded["last_job_count"]), 4)


if __name__ == "__main__":
    unittest.main()
