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
    def test_delegation_happens_before_any_engine_is_built(self):
        """Asserted on the source: the branch must precede the run-lock import, which
        is the last statement before the pipeline is entered."""
        import inspect

        source = inspect.getsource(run_orchestrator)
        branch = source.index('getattr(config, "MAINTENANCE_ONLY"')
        entry = source.index("from orchestrator.runlock import RunLockHeld")
        self.assertLess(branch, entry,
                        "maintenance must return before the pipeline is entered")

    def test_it_delegates_to_the_narrow_entry_point(self):
        import inspect

        source = inspect.getsource(run_orchestrator)
        head = source[:source.index("from orchestrator.runlock import RunLockHeld")]
        self.assertIn("import run_maintenance", head)
        self.assertIn("run_maintenance.main(", head)

    def test_the_maintenance_module_imports_no_provider_or_write_client(self):
        """Structural: the module never imports acquisition, Apollo, Hunter, the
        Airtable writer or the Instantly writer at any level."""
        import run_maintenance

        source = open(run_maintenance.__file__, encoding="utf-8").read()
        for forbidden in ("fantastic_jobs_adapter", "apollo_client", "hunter",
                          "instantly_client", "airtable_client",
                          "real_fantastic_runner", "RealDelivery"):
            self.assertNotIn(forbidden, source,
                             f"maintenance must not reach {forbidden}")

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
