"""A lane can be switched on without editing the deployed start command.

145 direct ATS boards have been registered, refreshed and scheduled for weeks while
contributing nothing, because the lane is built only when `"ats"` is in `--lanes`
and the deployed command passes `--lanes fantastic`. The boards are scraped
directly, so the wasted capacity cost nothing to hold and nothing to use -- the only
thing standing between them and production was a string in a service setting.

`ACQUISITION_EXTRA_LANES` adds to that string. The two properties that make it safe
to hand to an operator are pinned here:

* it can only WIDEN the set, so the deployed command remains the floor -- an
  operator cannot silence a lane the deployment asked for, only add to it;
* it is resolved BEFORE the strict preflight, so an added lane is held to the same
  dependency checks as a requested one. An `ats` lane added after preflight would
  start a run with no board registry.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

import config
import run_orchestrator


def _args(lanes: str):
    return argparse.Namespace(lanes=lanes)


class ExtraLanesWidenTheSet(unittest.TestCase):
    def test_empty_changes_nothing(self):
        with mock.patch.object(config, "ACQUISITION_EXTRA_LANES", ""):
            self.assertEqual(run_orchestrator._resolved_lanes(_args("fantastic")),
                             ["fantastic"])

    def test_a_lane_is_added_to_what_the_start_command_asked_for(self):
        with mock.patch.object(config, "ACQUISITION_EXTRA_LANES", "ats"):
            self.assertEqual(run_orchestrator._resolved_lanes(_args("fantastic")),
                             ["fantastic", "ats"])

    def test_it_cannot_remove_a_lane(self):
        """The deployed command is the floor. Whatever this is set to, every lane it
        names still runs."""
        for extra in ("", "ats", "ats,free_feeds", "  ", ",,"):
            with mock.patch.object(config, "ACQUISITION_EXTRA_LANES", extra):
                self.assertIn("fantastic",
                              run_orchestrator._resolved_lanes(_args("fantastic")))

    def test_a_duplicate_is_not_added_twice(self):
        with mock.patch.object(config, "ACQUISITION_EXTRA_LANES", "fantastic,ats"):
            self.assertEqual(run_orchestrator._resolved_lanes(_args("fantastic")),
                             ["fantastic", "ats"])

    def test_whitespace_and_empty_entries_are_ignored(self):
        with mock.patch.object(config, "ACQUISITION_EXTRA_LANES", " ats , , "):
            self.assertEqual(run_orchestrator._resolved_lanes(_args("fantastic")),
                             ["fantastic", "ats"])

    def test_order_is_deployment_first(self):
        """So the run summary reads in the order an operator set things up."""
        with mock.patch.object(config, "ACQUISITION_EXTRA_LANES", "ats,free_feeds"):
            self.assertEqual(run_orchestrator._resolved_lanes(_args("fantastic")),
                             ["fantastic", "ats", "free_feeds"])


class TheAddedLaneFacesTheSamePreflight(unittest.TestCase):
    def test_preflight_and_the_runner_read_the_same_resolver(self):
        """Pinned on the source: two independent parses of `--lanes` is precisely how
        a lane could be built without its dependency ever being checked."""
        import inspect

        source = inspect.getsource(run_orchestrator)
        self.assertEqual(source.count('a.lanes.split(",")'), 0,
                         "lanes must be resolved in exactly one place")
        self.assertIn('lanes = _resolved_lanes(a) or ["ats"]', source)
        self.assertIn("requested = _resolved_lanes(a)", source)

    def test_an_added_ats_lane_is_refused_when_the_registry_is_empty(self):
        """The check that would have been skipped had the lane been added later."""
        policy = mock.Mock(allow_enrichment=False, allow_airtable_write=False)
        a = argparse.Namespace(lanes="fantastic", airtable_write=False,
                               artifact_root=".", boards=None)
        checks = {"integrity_ok": True, "writable": True, "free_ok": True,
                  "boards_ok": False, "FANTASTIC_JOBS_API_KEY": True, "lock": {}}
        with mock.patch.object(run_orchestrator, "_preflight_checks",
                               return_value=(checks, [])):
            with mock.patch.object(config, "ACQUISITION_EXTRA_LANES", ""):
                self.assertEqual(run_orchestrator._strict_preflight(a, policy), 0)
            with mock.patch.object(config, "ACQUISITION_EXTRA_LANES", "ats"):
                self.assertEqual(run_orchestrator._strict_preflight(a, policy), 2,
                                 "an added ats lane must still need its boards")


if __name__ == "__main__":
    unittest.main()
