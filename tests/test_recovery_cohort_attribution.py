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
    return {"postings_resumed": 0, "opportunities_resumed": 0, "leads": 0,
            "with_contact": 0, "final_pass": 0, "needs_check": 0, "rejected": 0,
            "other": 0, "delivered_lead_keys": [], "posting_ids": set(ids),
            "opportunity_keys": set(), "attempted_opportunity_keys": set()}


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
        self.assertIn('recovery_block = {k: v for k, v in recovery_cohort.items() if k not in _sets}',
                      source)
        self.assertIn('recovery_block["cohort_postings"] = len(', source)

    def test_it_is_published_under_acquisition(self):
        import inspect

        from orchestrator import pipeline

        self.assertIn('"recovery_cohort": recovery_block,',
                      inspect.getsource(pipeline))


if __name__ == "__main__":
    unittest.main()


class ARunThatBuysNOTHINGStillDrainsCustody(unittest.TestCase):
    """The recovery-first acceptance depends on one behaviour: a run with acquisition
    DISABLED must still hand back what custody is owed and enrich it.

    That is the whole shape of the first post-Apollo run -- `FANTASTIC_JOBS_ENABLED=0`
    keeps it from buying anything, `MAINTENANCE_ONLY=0` lets the pipeline run, and the
    3,595 recovered postings are processed against a budget instead of new inventory.
    It was asserted from reading the code; this executes it.
    """

    def _pipeline_bits(self):
        import tempfile
        from unittest import mock

        from orchestrator.lanes import LaneResult
        from orchestrator.modes import ExecutionMode as EM
        from orchestrator.modes import policy_for as pf
        from orchestrator.pipeline import Orchestrator
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from tests.test_pipeline_run_ledger import (TOPUP_CONFIG, _Budget, _Engine,
                                                    _plan)
        return (tempfile, mock, EM, LaneResult, pf, RunContext, Orchestrator,
                StateManager, TOPUP_CONFIG, _Budget, _Engine, _plan)

    def test_custody_is_handed_back_when_the_lane_acquires_nothing(self):
        import json
        from pathlib import Path

        (tempfile, mock, EM, LaneResult, pf, RunContext, Orchestrator,
         StateManager, TOPUP_CONFIG, _Budget, _Engine, _plan) = self._pipeline_bits()
        import config
        from orchestrator import pending_work

        tmp = tempfile.mkdtemp()
        # Work an EARLIER run is still owed, exactly as the production store holds it.
        pending_work.record(Path(tmp) / pending_work.STORE, "20260904T130130Z-earlier", [
            {"job_id": "owed1", "posting_id": "owed1", "employer_name": "Acme",
             "company_name": "Acme", "job_title": "Head of Sales"},
            {"job_id": "owed2", "posting_id": "owed2", "employer_name": "Beta",
             "company_name": "Beta", "job_title": "Head of Sales"},
        ])

        def acquires_nothing(_manager):
            return LaneResult(lane="fantastic", status="complete", jobs=[],
                              physical_requests=0)

        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT,
                                {"mode": "live_acquisition_and_enrichment"},
                                run_id="20260907T030000Z-recovery1")
        state = StateManager(tmp, pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT),
                             run_id=ctx.run_id)
        engine = _Engine()
        cfg = dict(TOPUP_CONFIG, PENDING_WORK_ENABLED=True,
                   PENDING_WORK_RESUME_MAX_PER_RUN=2000)
        with mock.patch.multiple(config, **cfg):
            result = Orchestrator(ctx, state, _Budget()).run(
                _plan(acquires_nothing, engine), resume=False)

        acq = (result.get("acquisition") or {}).get("cumulative") or {}
        self.assertEqual(acq.get("net_new_jobs_captured"), 0,
                         "a run that bought nothing must capture nothing")
        self.assertEqual((acq.get("pending_work_resumed") or {}).get("adopted"), 2,
                         "...and must still be handed what custody is owed")
        # The resumed rows reached the enrichment engine rather than being counted
        # and dropped -- the failure that would make the whole exercise theatre.
        self.assertGreaterEqual(engine.calls, 1)

    def test_the_cohort_is_reported_on_such_a_run(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        (tempfile_, mock_, EM, LaneResult, pf, RunContext, Orchestrator,
         StateManager, TOPUP_CONFIG, _Budget, _Engine, _plan) = self._pipeline_bits()
        import config
        from orchestrator import pending_work

        tmp = tempfile.mkdtemp()
        pending_work.record(Path(tmp) / pending_work.STORE, "earlier", [
            {"job_id": "owed1", "posting_id": "owed1", "employer_name": "Acme",
             "company_name": "Acme", "job_title": "Head of Sales"}])

        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT,
                                {"mode": "live_acquisition_and_enrichment"},
                                run_id="20260907T030000Z-recovery2")
        state = StateManager(tmp, pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT),
                             run_id=ctx.run_id)
        cfg = dict(TOPUP_CONFIG, PENDING_WORK_ENABLED=True,
                   PENDING_WORK_RESUME_MAX_PER_RUN=2000)
        with mock.patch.multiple(config, **cfg):
            result = Orchestrator(ctx, state, _Budget()).run(
                _plan(lambda _m: LaneResult(lane="fantastic", status="complete",
                                            jobs=[], physical_requests=0),
                      _Engine()), resume=False)

        cohort = (result.get("acquisition") or {}).get("recovery_cohort") or {}
        self.assertEqual(cohort.get("postings_resumed"), 1)
        self.assertGreaterEqual(cohort.get("cohort_postings", 0), 1)
        for internal in ("posting_ids", "opportunity_keys",
                         "attempted_opportunity_keys"):
            self.assertNotIn(internal, cohort, "identity sets are not serialised")


class ThreeUnitsAreThreeNumbers(unittest.TestCase):
    """Custody stores POSTINGS; approvals are capped per company x function
    OPPORTUNITY; the hiring-manager stage emits LEADS. The first version of this
    block called the posting count `opportunities_resumed` -- the same conflation
    that produced every bad capacity figure this week -- so the three are now three
    fields and the rate names its own denominator."""

    def test_the_posting_count_is_not_called_an_opportunity_count(self):
        import inspect

        from orchestrator import pipeline

        source = inspect.getsource(pipeline)
        self.assertIn('recovery_cohort["postings_resumed"] += len(resumed)', source)
        self.assertIn('recovery_cohort["opportunities_resumed"] = len(', source)

    def test_the_rate_divides_by_attempted_not_by_resumed(self):
        """Work with no outcome must not sit in the bottom of a fraction."""
        import inspect

        from orchestrator import pipeline

        source = inspect.getsource(pipeline)
        self.assertIn('recovery_block["with_contact"] / attempted', source)
        self.assertIn('recovery_block["rate_denominator"]', source)

    def test_unreconciled_work_is_reported_separately_and_not_as_never_attempted(self):
        import inspect

        from orchestrator import pipeline

        source = inspect.getsource(pipeline)
        self.assertIn("opportunities_without_reconciled_outcome", source)
        self.assertNotIn('recovery_block["never_attempted"]', source,
                         "an unreconciled outcome is not evidence of no attempt")

    def test_attempted_is_recorded_when_a_lead_carries_an_outcome(self):
        cohort = _cohort({"p1"})
        _account_recovery_cohort(cohort, [_lead("p1", contact_key="a@x")], _delivery())
        self.assertEqual(len(cohort["attempted_opportunity_keys"]), 1)

    def test_two_leads_for_one_opportunity_count_that_opportunity_once(self):
        """The denominator is DISTINCT eligible opportunities."""
        cohort = _cohort({"p1", "p2"})
        lead_a = _lead("p1", contact_key="a@x")
        lead_b = _lead("p2", contact_key="b@y")
        lead_a.company = lead_b.company = {"employer_name": "Acme",
                                           "company_name": "Acme",
                                           "job_title": "Head of Sales",
                                           "_employer_domain_input": "acme.example"}
        _account_recovery_cohort(cohort, [lead_a, lead_b], _delivery())
        self.assertEqual(cohort["leads"], 2)
        self.assertEqual(len(cohort["attempted_opportunity_keys"]), 1)
