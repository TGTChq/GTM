"""New acquisition must be measurable on its own, not by subtraction.

The acceptance guide requires the next execution to separate newly acquired work from
resumed custody "para medir qué produjo la adquisición nueva", and forbids inferring
the fresh result by subtracting aggregate totals. The pipeline attributed the RECOVERY
cohort and had no symmetric fresh one, so subtraction was the only available answer --
and it is wrong precisely when the company+bucket collapse merges a fresh posting with
a resumed one into a single lead.

That lead genuinely belongs to both cohorts and is counted in each. The overlap is
therefore reported explicitly, so nobody adds the two and counts the contact twice.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from orchestrator.pipeline import _account_recovery_cohort, _cohort_overlap


def _cohort(ids=(), keys=("postings_acquired", "opportunities_acquired")):
    base = {"leads": 0, "with_contact": 0, "final_pass": 0, "needs_check": 0,
            "rejected": 0, "other": 0, "delivered_lead_keys": [],
            "posting_ids": set(ids), "opportunity_keys": set(),
            "attempted_opportunity_keys": set()}
    for k in keys:
        base[k] = 0
    return base


def _lead(posting_id, *, email="", related=(), disposition="FINAL_PASS"):
    return SimpleNamespace(posting_id=posting_id, contact_key=email,
                           contact={"email": email},
                           disposition=SimpleNamespace(value=disposition),
                           related_posting_ids=list(related), company={})


class EachCohortIsAttributedOnItsOwn(unittest.TestCase):
    def test_a_fresh_lead_counts_only_in_the_fresh_cohort(self):
        fresh, rec = _cohort({"f1"}), _cohort({"r1"})
        leads = [_lead("f1", email="a@x.com")]
        _account_recovery_cohort(rec, leads, SimpleNamespace(delivered_lead_keys=[]))
        _account_recovery_cohort(fresh, leads, SimpleNamespace(delivered_lead_keys=[]))
        self.assertEqual(fresh["leads"], 1)
        self.assertEqual(rec["leads"], 0)

    def test_a_resumed_lead_counts_only_in_the_recovery_cohort(self):
        fresh, rec = _cohort({"f1"}), _cohort({"r1"})
        leads = [_lead("r1", email="b@x.com")]
        _account_recovery_cohort(rec, leads, SimpleNamespace(delivered_lead_keys=[]))
        _account_recovery_cohort(fresh, leads, SimpleNamespace(delivered_lead_keys=[]))
        self.assertEqual(rec["leads"], 1)
        self.assertEqual(fresh["leads"], 0)

    def test_contacts_are_attributed_per_cohort_not_pooled(self):
        fresh, rec = _cohort({"f1"}), _cohort({"r1"})
        leads = [_lead("f1", email="a@x.com"), _lead("r1", email="")]
        _account_recovery_cohort(rec, leads, SimpleNamespace(delivered_lead_keys=[]))
        _account_recovery_cohort(fresh, leads, SimpleNamespace(delivered_lead_keys=[]))
        self.assertEqual(fresh["with_contact"], 1)
        self.assertEqual(rec["with_contact"], 0)


class TheOverlapIsReportedSoTheTwoAreNeverSummed(unittest.TestCase):
    def test_a_collapsed_lead_spanning_both_is_counted_in_each(self):
        fresh, rec = _cohort({"f1"}), _cohort({"r1"})
        leads = [_lead("f1", email="a@x.com", related=["r1"])]
        _account_recovery_cohort(rec, leads, SimpleNamespace(delivered_lead_keys=[]))
        _account_recovery_cohort(fresh, leads, SimpleNamespace(delivered_lead_keys=[]))
        self.assertEqual(fresh["leads"], 1)
        self.assertEqual(rec["leads"], 1)
        self.assertEqual(_cohort_overlap(fresh, rec, leads), 1,
                         "summing the cohorts here would count one contact twice")

    def test_no_overlap_when_the_cohorts_are_disjoint(self):
        fresh, rec = _cohort({"f1"}), _cohort({"r1"})
        leads = [_lead("f1", email="a@x.com"), _lead("r1", email="b@x.com")]
        self.assertEqual(_cohort_overlap(fresh, rec, leads), 0)

    def test_an_empty_cohort_reports_no_overlap(self):
        self.assertEqual(_cohort_overlap(_cohort(), _cohort({"r1"}),
                                         [_lead("r1", email="b@x.com")]), 0)


class TheRunResultCarriesBoth(unittest.TestCase):
    def test_both_cohorts_and_the_overlap_are_published(self):
        import inspect

        from orchestrator import pipeline

        src = inspect.getsource(pipeline)
        self.assertIn('"fresh_cohort": fresh_block,', src)
        self.assertIn('"recovery_cohort": recovery_block,', src)
        self.assertIn('"cohort_overlap_leads"', src)

    def test_fresh_identities_are_taken_before_adoption_appends_resumed(self):
        """Recorded at the dedupe boundary, which is the only point where
        `opportunities` is exactly the newly acquired set."""
        import inspect

        from orchestrator import pipeline

        src = inspect.getsource(pipeline)
        fresh_at = src.index('fresh_cohort["postings_acquired"] += len(opportunities)')
        adopt_at = src.index("opportunities = list(opportunities) + resumed")
        self.assertLess(fresh_at, adopt_at)


if __name__ == "__main__":
    unittest.main()
