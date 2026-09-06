"""Maintenance runs on the existing schedule, and cannot become a pipeline run.

The production volume is only reachable from inside a scheduled container, and the
only scheduled container ran the pipeline -- which buys jobs. `MAINTENANCE_ONLY`
lets the EXISTING cron carry the recovery/reporting pass instead, with no change to
the schedule and no change to the start command.

The two properties that make that safe:

  * it is REFUSED while acquisition is armed, so it can never be switched on in a
    way that hides a live run or is confused for one;
  * it returns before any lane runner, enrichment engine or delivery manager is
    constructed, so no acquisition, enrichment or delivery code is reachable --
    structurally, not by convention.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

import config
import run_maintenance
import run_orchestrator


class MaintenanceOnlyIsRefusedWhileAcquisitionIsArmed(unittest.TestCase):
    def test_it_will_not_run_with_the_paid_source_enabled(self):
        with mock.patch.multiple(config, MAINTENANCE_ONLY=True,
                                 FANTASTIC_JOBS_ENABLED=True):
            import run_maintenance
            with self.assertRaises(SystemExit) as caught:
                run_maintenance._refuse_if_acquisition_is_live()
            self.assertEqual(caught.exception.code, 2)

    def test_the_flag_is_off_by_default(self):
        self.assertFalse(config.MAINTENANCE_ONLY)


class MaintenanceOnlyReachesNoPipelineCode(unittest.TestCase):
    def test_delegation_happens_before_any_lane_runner_is_built(self):
        """The branch must precede `lane_runners` construction, not merely the
        pipeline entry.

        The first version of this test asserted only that the branch came before the
        run-lock import -- which it did, while sitting AFTER
        `lane_runners["fantastic"] = real_fantastic_runner()`. The runners are lazy
        closures so nothing was contacted, but "no acquisition code is reachable" was
        not what the code did. The assertion now matches the claim.
        """
        import inspect

        source = inspect.getsource(run_orchestrator)
        branch = source.index('getattr(config, "MAINTENANCE_ONLY"')
        lanes = source.index("lane_runners: Dict[str, Any] = {}")
        entry = source.index("from orchestrator.runlock import RunLockHeld")
        self.assertLess(branch, lanes,
                        "maintenance must return before any lane runner is built")
        self.assertLess(branch, entry)

    def test_it_delegates_to_the_narrow_entry_point(self):
        import inspect

        source = inspect.getsource(run_orchestrator)
        head = source[:source.index("from orchestrator.runlock import RunLockHeld")]
        self.assertIn("import run_maintenance", head)
        self.assertIn("run_maintenance.main(", head)

    def test_the_maintenance_module_reaches_no_provider(self):
        """Structural: no acquisition adapter, no Apollo, no Hunter, no delivery."""
        import run_maintenance

        source = open(run_maintenance.__file__, encoding="utf-8").read()
        for forbidden in ("fantastic_jobs_adapter", "apollo_client", "hunter",
                          "instantly_client", "real_fantastic_runner", "RealDelivery"):
            self.assertNotIn(forbidden, source,
                             f"maintenance must not reach {forbidden}")

    def test_it_calls_no_write_entry_point(self):
        """`airtable_client` IS imported -- for `_company_identity_keys_from_job`,
        the production key the suppression rule uses, which capacity measurement must
        not re-implement. What must never appear is a CALL that writes."""
        import ast

        import run_maintenance

        tree = ast.parse(open(run_maintenance.__file__, encoding="utf-8").read())
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called.add(getattr(fn, "id", None) or getattr(fn, "attr", None) or "")
        for forbidden in ("push_leads", "enroll_record", "enroll_approved_leads",
                          "create_leads", "deliver"):
            self.assertNotIn(forbidden, called,
                             f"maintenance must never call {forbidden}")

    def test_only_pure_helpers_are_taken_from_airtable_client(self):
        import ast

        import run_maintenance

        tree = ast.parse(open(run_maintenance.__file__, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "airtable_client":
                imported.update(a.name for a in node.names)
        self.assertTrue(imported <= {"_company_identity_keys_from_job"},
                        f"unexpected airtable_client imports: {imported}")

    def test_it_never_sends_slack(self):
        """Checked as CODE, not as prose: no slack module, no webhook read, no
        delivery call. The docstring is allowed to mention it; the module is not
        allowed to do it."""
        import ast

        import run_maintenance

        tree = ast.parse(open(run_maintenance.__file__, encoding="utf-8").read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        offenders = {n for n in names if "slack" in n.lower() or "webhook" in n.lower()}
        self.assertEqual(offenders, set(), f"maintenance must not reach {offenders}")


class TheMaintenancePassIsOrderedBackupFirst(unittest.TestCase):
    def test_backup_precedes_adoption_in_the_entry_point(self):
        import inspect

        import run_maintenance

        source = inspect.getsource(run_maintenance.main)
        self.assertLess(source.index("BACKUP"), source.index("ADOPT"),
                        "evidence is copied before anything mutates it")
        self.assertLess(source.index("ADOPT"), source.index("REPORTING"),
                        "custody is taken before the report reads the store")


if __name__ == "__main__":
    unittest.main()


class TheProvenanceProbeExplainsAPartialMetric(unittest.TestCase):
    """A `partial` status says a contribution is missing; it cannot say whether the
    field is RECOVERABLE from an unread payload or was never recorded. Those need
    opposite responses -- read it, or say why it cannot be read -- and guessing
    between them is how a reporting gap gets reported as a data gap."""

    def _root(self, **runs):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        for run_id, result in runs.items():
            d = root / "run_artifacts" / run_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "orchestrator_result.json").write_text(_json.dumps(result),
                                                        encoding="utf-8")
        return root

    def test_a_recorded_field_is_reported_present(self):
        root = self._root(**{"r1": {"enrichment": {"funnel": {
            "qualification_input": 226, "contact_discovery_entered": 100}}}})
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertEqual(row["funnel_qualification_input"], 226)
        self.assertEqual(row["funnel_contact_discovery_entered"], 100)

    def test_an_absent_field_is_reported_absent_with_what_the_run_does_carry(self):
        """`funnel_keys` is the actionable half: it distinguishes "the funnel was
        never written" from "the funnel was written without this counter"."""
        root = self._root(**{"r1": {"enrichment": {"funnel": {"captured": 6205}}}})
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertIsNone(row["funnel_qualification_input"])
        self.assertEqual(row["funnel_keys"], ["captured"])

    def test_a_run_with_no_result_at_all_still_reports_a_row(self):
        root = self._root()
        (root / "run_artifacts" / "empty").mkdir(parents=True, exist_ok=True)
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertEqual(row["run_id"], "empty")
        self.assertIsNone(row["funnel_qualification_input"])

    def test_zero_only_skip_buckets_are_flagged_not_listed(self):
        root = self._root(**{"r1": {"delivery": {"skip_breakdown": {
            "account_suppressed": 0, "not_final_pass": 0}}}})
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertEqual(row["skip_breakdown_nonzero"], {})
        self.assertTrue(row["skip_breakdown_all_zero"])

    def test_a_firing_skip_bucket_is_listed(self):
        root = self._root(**{"r1": {"delivery": {"skip_breakdown": {
            "account_suppressed": 0, "company_function_suppressed": 200}}}})
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertEqual(row["skip_breakdown_nonzero"],
                         {"company_function_suppressed": 200})
        self.assertFalse(row["skip_breakdown_all_zero"])
