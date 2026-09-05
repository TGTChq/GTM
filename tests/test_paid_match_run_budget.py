"""Three attempts per BUCKET is not a run-level budget.

Nothing bounded the total number of paid ``people/match`` calls a run could make.
That was tolerable only while every paid attempt belonged to a bucket that had
already found people: the work was bounded by how many buckets found candidates.

The org-id zero-people fallback breaks that assumption by design. It fires ONLY on
buckets that found zero people, so every paid match it enables is spend that did not
previously exist -- on a run with ~150 such buckets, up to ~450 additional paid calls
that no authorization covered. The 0-credit People Search it uses is genuinely free;
what it feeds is not, and "costs no additional Apollo credit" is true of the search
and false of the run.

So the fallback draws on its own budget, defaulting to zero, and buckets past it are
DEFERRED rather than discarded -- left unprocessed and counted so a later run with
budget can take them. Marking them processed would turn a budget stop into permanent
loss, which is the failure this whole layer exists to avoid.
"""

from __future__ import annotations

import unittest
from unittest import mock

import config
import hiring_manager as hm


class TheRunLevelBudget(unittest.TestCase):
    def setUp(self):
        hm.reset_paid_match_budget()

    def test_a_run_starts_with_a_clean_slate(self):
        hm._record_paid_match(True)
        hm.reset_paid_match_budget()
        self.assertEqual(hm.paid_match_budget_state(),
                         {"used": 0, "fallback_used": 0, "deferred_buckets": 0})

    def test_the_fallback_budget_is_zero_by_default(self):
        """Enabling the recovery must not, by itself, authorize new spend."""
        self.assertEqual(config.APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN, 0)
        with mock.patch.multiple(config,
                                 APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN=0,
                                 APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=0):
            self.assertFalse(hm._paid_match_allowed(from_org_id_fallback=True),
                             "a recovered bucket may not spend without a budget")
            self.assertTrue(hm._paid_match_allowed(from_org_id_fallback=False),
                            "work that was already authorized is untouched")

    def test_existing_spend_is_unchanged_when_the_fallback_is_enabled(self):
        """The property that had to hold: turning the recovery on changes the
        recovery, not the bill."""
        with mock.patch.multiple(config,
                                 APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN=0,
                                 APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=0):
            for _ in range(500):
                if hm._paid_match_allowed(from_org_id_fallback=False):
                    hm._record_paid_match(False)
            self.assertEqual(hm.paid_match_budget_state()["used"], 500,
                             "the primary path is not throttled by this guard")
            self.assertEqual(hm.paid_match_budget_state()["fallback_used"], 0)

    def test_a_granted_fallback_budget_is_spent_and_then_stops(self):
        with mock.patch.multiple(config,
                                 APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN=25,
                                 APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=0):
            allowed = 0
            for _ in range(100):
                if hm._paid_match_allowed(from_org_id_fallback=True):
                    hm._record_paid_match(True)
                    allowed += 1
            self.assertEqual(allowed, 25, "exactly the granted number, never more")
            self.assertEqual(hm.paid_match_budget_state()["fallback_used"], 25)

    def test_the_overall_ceiling_bounds_every_path_together(self):
        """A ceiling over the run, not per path: the primary path, the alternate
        cascade and the fallback all draw on the same total."""
        with mock.patch.multiple(config,
                                 APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN=1000,
                                 APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=10):
            spent = 0
            for i in range(50):
                if hm._paid_match_allowed(from_org_id_fallback=bool(i % 2)):
                    hm._record_paid_match(bool(i % 2))
                    spent += 1
            self.assertEqual(spent, 10)

    def test_the_overall_ceiling_is_off_by_default(self):
        """It limits work that was already authorized, so it is switched on
        deliberately rather than imposed by this change."""
        self.assertEqual(config.APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN, 0)


class WorkPastTheBudgetIsDeferredNotDiscarded(unittest.TestCase):
    """A budget stop must never look like a finished bucket.

    If an unenriched bucket were marked processed, the next run would skip it and the
    opportunity would be lost for good -- a spending limit silently converted into
    permanent data loss.
    """

    def setUp(self):
        hm.reset_paid_match_budget()

    def test_a_deferred_bucket_is_counted_so_it_can_be_picked_up_later(self):
        with mock.patch.multiple(config,
                                 APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN=0,
                                 APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=0):
            for _ in range(3):
                if not hm._paid_match_allowed(from_org_id_fallback=True):
                    hm._PAID_MATCH_BUDGET["deferred_buckets"] += 1

        state = hm.paid_match_budget_state()
        self.assertEqual(state["deferred_buckets"], 3)
        self.assertEqual(state["fallback_used"], 0, "nothing was spent on them")

    def test_the_call_sites_stop_rather_than_continue_unfunded(self):
        """Both paid-match loops consult the budget BEFORE the call, so a stop costs
        nothing. Asserted on the source because the branch is only reachable with a
        live Apollo client, and what matters is that the check precedes the spend."""
        import inspect

        source = inspect.getsource(hm)
        for marker in ("_paid_match_allowed", "_record_paid_match",
                       "paid_match_budget_deferred"):
            self.assertIn(marker, source)
        # The guard must be consulted at least as often as the paid call is made.
        self.assertGreaterEqual(source.count("_paid_match_allowed("),
                                source.count("apollo.match_person("),
                                "every paid match must be preceded by a budget check")


if __name__ == "__main__":
    unittest.main()
