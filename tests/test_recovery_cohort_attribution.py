"""Recovered work must be followable to the end, not merely re-entered.

The 3,595 postings in custody -- 2,998 company x function opportunities -- are the
first thing that should be processed when Apollo returns, and "we reprocessed the
backlog" is a claim with no measurement behind it unless the cohort can be traced
through enrichment, approval and the Approved Sync that enrols it from a DIFFERENT
service on a different schedule.

Attribution is by POSTING IDENTITY, the one key the pending store, the enrichment
leads and the delivery record all share. Two properties matter and are pinned here:

* a recovered posting COLLAPSED into a lead alongside fresh work is still recovered
  work. Attributing only on `posting_id` would drop it every time the company+bucket
  collapse fired, understating the cohort exactly where the pipeline works hardest;
* delivered lead KEYS are kept, not just counted. Approved Sync runs later, in
  another service; a count cannot be joined to what it enrolled, a key can.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from orchestrator.pipeline import _account_recovery_cohort


def _cohort(ids=()):
    return {"opportunities_resumed": 0, "leads": 0, "with_contact": 0,
            "final_pass": 0, "needs_check": 0, "rejected": 0, "other": 0,
            "delivered_lead_keys": [], "posting_ids": set(ids)}


def _lead(posting_id, *, contact_key="", disposition="FINAL_PASS", related=()):
    return SimpleNamespace(posting_id=posting_id, contact_key=contact_key,
                           disposition=SimpleNamespace(value=disposition),
                           related_posting_ids=list(related))


def _delivery(delivered=()):
    return SimpleNamespace(delivered_lead_keys=list(delivered))


class TheCohortIsFollowedThroughEveryStage(unittest.TestCase):
    def test_a_recovered_lead_is_counted_and_a_fresh_one_is_not(self):
        cohort = _cohort({"p1"})
        _account_recovery_cohort(cohort, [_lead("p1", contact_key="a@x.com"),
                                          _lead("p9", contact_key="b@y.com")],
                                 _delivery())
        self.assertEqual(cohort["leads"], 1)
        self.assertEqual(cohort["with_contact"], 1)

    def test_a_collapsed_recovered_posting_still_counts(self):
        """The company+bucket collapse folds N postings into one lead. If the lead's
        own posting is fresh but a recovered one collapsed into it, the recovered
        work was still done."""
        cohort = _cohort({"p_recovered"})
        _account_recovery_cohort(
            cohort, [_lead("p_fresh", contact_key="a@x.com",
                           related=["p_recovered"])], _delivery())
        self.assertEqual(cohort["leads"], 1)

    def test_dispositions_are_split_not_totalled(self):
        cohort = _cohort({"p1", "p2", "p3", "p4"})
        _account_recovery_cohort(cohort, [
            _lead("p1", contact_key="a@x", disposition="FINAL_PASS"),
            _lead("p2", disposition="NEEDS_CHECK"),
            _lead("p3", disposition="REJECT"),
            _lead("p4", disposition="UNVERIFIED"),
        ], _delivery())
        self.assertEqual((cohort["final_pass"], cohort["needs_check"],
                          cohort["rejected"], cohort["other"]), (1, 1, 1, 1))

    def test_a_lead_with_no_contact_is_a_lead_but_not_a_contact(self):
        cohort = _cohort({"p1"})
        _account_recovery_cohort(cohort, [_lead("p1", contact_key="")], _delivery())
        self.assertEqual(cohort["leads"], 1)
        self.assertEqual(cohort["with_contact"], 0)

    def test_delivered_keys_are_kept_so_a_later_sync_can_be_joined(self):
        cohort = _cohort({"p1", "p2"})
        _account_recovery_cohort(cohort, [
            _lead("p1", contact_key="a@x.com"),
            _lead("p2", contact_key="b@y.com"),
        ], _delivery(["a@x.com"]))
        self.assertEqual(cohort["delivered_lead_keys"], ["a@x.com"])

    def test_a_key_delivered_for_a_lead_outside_the_cohort_is_not_claimed(self):
        cohort = _cohort({"p1"})
        _account_recovery_cohort(cohort, [_lead("p9", contact_key="z@z.com")],
                                 _delivery(["z@z.com"]))
        self.assertEqual(cohort["delivered_lead_keys"], [])

    def test_an_empty_cohort_does_no_work_and_claims_nothing(self):
        cohort = _cohort()
        _account_recovery_cohort(cohort, [_lead("p1", contact_key="a@x")],
                                 _delivery(["a@x"]))
        self.assertEqual(cohort["leads"], 0)
        self.assertEqual(cohort["delivered_lead_keys"], [])

    def test_it_accumulates_across_slices(self):
        """Top-up runs the loop repeatedly; the cohort is a run-level total."""
        cohort = _cohort({"p1", "p2"})
        _account_recovery_cohort(cohort, [_lead("p1", contact_key="a@x")], _delivery())
        _account_recovery_cohort(cohort, [_lead("p2", contact_key="b@y")], _delivery())
        self.assertEqual(cohort["leads"], 2)


class TheRunResultCarriesItInAJoinableShape(unittest.TestCase):
    def test_the_identity_set_is_not_serialised_but_its_size_is(self):
        import inspect

        from orchestrator import pipeline

        source = inspect.getsource(pipeline)
        self.assertIn('recovery_block = {k: v for k, v in recovery_cohort.items() if k != "posting_ids"}',
                      source)
        self.assertIn('recovery_block["cohort_postings"] = len(', source)

    def test_it_is_published_under_acquisition(self):
        import inspect

        from orchestrator import pipeline

        self.assertIn('"recovery_cohort": recovery_block,',
                      inspect.getsource(pipeline))


if __name__ == "__main__":
    unittest.main()
