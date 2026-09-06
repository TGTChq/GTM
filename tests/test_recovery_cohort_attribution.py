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


def _lead(posting_id, *, contact_key="", disposition="FINAL_PASS", related=(),
          email=None):
    # `email` defaults to following contact_key so existing cases keep their meaning;
    # a no-contact lead is written explicitly as email="".
    address = contact_key if email is None else email
    return SimpleNamespace(posting_id=posting_id, contact_key=contact_key,
                           contact={"email": address},
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


class TheLoopDrainsCustodyAcrossBATCHESNotJustOne(unittest.TestCase):
    """`PENDING_WORK_RESUME_MAX_PER_RUN` bounds a BATCH -- memory and blast radius --
    and must not bound the day. Adoption used to run once per run, so a 3,595-posting
    backlog needed two days at 2,000 a run for no reason but a one-shot guard.

    Executed rather than asserted from the source: a lane that acquires nothing, a
    custody store holding more than one batch, and a check that the run drains it."""

    def test_more_than_one_batch_is_adopted_in_a_single_run(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        import config
        from orchestrator import pending_work
        from orchestrator.lanes import LaneResult
        from orchestrator.modes import ExecutionMode as EM, policy_for as pf
        from orchestrator.pipeline import Orchestrator
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from tests.test_pipeline_run_ledger import TOPUP_CONFIG, _Budget, _Engine, _plan

        tmp = tempfile.mkdtemp()
        owed = [{"job_id": f"owed{i}", "posting_id": f"owed{i}",
                 "employer_name": f"Co{i}", "company_name": f"Co{i}",
                 "job_title": "Head of Sales"} for i in range(7)]
        pending_work.record(Path(tmp) / pending_work.STORE, "earlier-run", owed)

        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT,
                                {"mode": "live_acquisition_and_enrichment"},
                                run_id="20260907T030000Z-drain01")
        state = StateManager(tmp, pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT),
                             run_id=ctx.run_id)
        # A batch of THREE against seven owed: three batches, then exhaustion.
        cfg = dict(TOPUP_CONFIG, PENDING_WORK_ENABLED=True,
                   PENDING_WORK_RESUME_MAX_PER_RUN=3)
        with mock.patch.multiple(config, **cfg):
            result = Orchestrator(ctx, state, _Budget()).run(
                _plan(lambda _m: LaneResult(lane="fantastic", status="complete",
                                            jobs=[], physical_requests=0),
                      _Engine()), resume=False)

        acq = (result.get("acquisition") or {}).get("cumulative") or {}
        resumed = acq.get("pending_work_resumed") or {}
        self.assertGreater(resumed.get("batches", 0), 1,
                           "one batch per run is the bug, not the contract")
        self.assertEqual(resumed.get("adopted"), 7,
                         "every owed posting is handed back within the run")
        self.assertEqual(acq.get("net_new_jobs_captured"), 0,
                         "and none of it is counted as newly captured")

    def test_the_loop_stops_once_custody_is_actually_drained(self):
        """Not exhausted while work is owed -- and exhausted once it is not, or the
        run spins until the iteration guard."""
        import tempfile
        from pathlib import Path
        from unittest import mock

        import config
        from orchestrator import pending_work
        from orchestrator.lanes import LaneResult
        from orchestrator.modes import ExecutionMode as EM, policy_for as pf
        from orchestrator.pipeline import Orchestrator
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from tests.test_pipeline_run_ledger import TOPUP_CONFIG, _Budget, _Engine, _plan

        tmp = tempfile.mkdtemp()
        pending_work.record(Path(tmp) / pending_work.STORE, "earlier-run", [
            {"job_id": "owed1", "posting_id": "owed1", "employer_name": "Acme",
             "company_name": "Acme", "job_title": "Head of Sales"}])
        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT,
                                {"mode": "live_acquisition_and_enrichment"},
                                run_id="20260907T030000Z-drain02")
        state = StateManager(tmp, pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT),
                             run_id=ctx.run_id)
        cfg = dict(TOPUP_CONFIG, PENDING_WORK_ENABLED=True,
                   PENDING_WORK_RESUME_MAX_PER_RUN=2000)
        with mock.patch.multiple(config, **cfg):
            result = Orchestrator(ctx, state, _Budget()).run(
                _plan(lambda _m: LaneResult(lane="fantastic", status="complete",
                                            jobs=[], physical_requests=0),
                      _Engine()), resume=False)
        self.assertEqual(result["topup"]["final_stop_reason"], "inventory_exhausted")


class AZeroAcquisitionBudgetDoesNotEndARecoveryRun(unittest.TestCase):
    """Executed end to end, with the production shape my earlier offline test missed.

    That test gave the controller `FANTASTIC_JOBS_MAX_JOBS_PER_RUN=1000`, so the
    acquisition cap was never zero and the run reached adoption. Production has the
    governor at ZERO while acquisition is off -- and the real calibration on
    2026-09-06 stopped at `governor_zero_budget` with `acquisition_entered: false`,
    having adopted nothing and delivered nothing. The fixture was wrong, not the
    conclusion it supported."""

    def test_the_queue_is_drained_with_a_zero_acquisition_cap(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        import config
        from orchestrator import pending_work
        from orchestrator.lanes import LaneResult
        from orchestrator.modes import ExecutionMode as EM, policy_for as pf
        from orchestrator.pipeline import Orchestrator
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from tests.test_pipeline_run_ledger import TOPUP_CONFIG, _Budget, _Engine, _plan

        tmp = tempfile.mkdtemp()
        pending_work.record(Path(tmp) / pending_work.STORE, "earlier", [
            {"job_id": f"owed{i}", "posting_id": f"owed{i}",
             "employer_name": f"Co{i}", "company_name": f"Co{i}",
             "job_title": "Head of Sales"} for i in range(4)])

        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT,
                                {"mode": "live_acquisition_and_enrichment"},
                                run_id="20260907T030000Z-zerobudget")
        state = StateManager(tmp, pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT),
                             run_id=ctx.run_id)
        # THE PRODUCTION SHAPE: acquisition budget zero.
        cfg = dict(TOPUP_CONFIG, PENDING_WORK_ENABLED=True,
                   PENDING_WORK_RESUME_MAX_PER_RUN=2,
                   FANTASTIC_JOBS_MAX_JOBS_PER_RUN=0,
                   FANTASTIC_TOPUP_SLICE_JOBS=0)

        def never_called(_manager):
            raise AssertionError("a zero slice must not call the lanes")

        with mock.patch.multiple(config, **cfg):
            result = Orchestrator(ctx, state, _Budget()).run(
                _plan(never_called, _Engine()), resume=False)

        acq = (result.get("acquisition") or {}).get("cumulative") or {}
        resumed = acq.get("pending_work_resumed") or {}
        self.assertEqual(resumed.get("adopted"), 4,
                         "the queue is drained despite a zero acquisition budget")
        self.assertTrue(result["topup"].get("acquisition_suppressed"),
                        "and the suppression is recorded, not silent")
        self.assertEqual(acq.get("net_new_jobs_captured"), 0)


class AZeroGovernorGrantDoesNotEndARunWithQueuedWork(unittest.TestCase):
    """The SECOND place the same mistake lived. Fixing the top-up controller was not
    enough: the pipeline sets `stop_reason = governor_zero_budget` BEFORE the loop, so
    `while not stop_reason` never ran an iteration at all.

    The 2026-09-06 calibration exited there with `acquisition_entered: false`, having
    adopted none of the 3,595 paid-for postings custody was holding, because the
    FANTASTIC daily allowance was spent. A credit ceiling for the source the run is
    deliberately not using must not decide whether queued work gets done."""

    def _run(self, owed_count, *, run_id):
        import tempfile
        from pathlib import Path
        from unittest import mock

        import config
        from orchestrator import pending_work
        from orchestrator.lanes import LaneResult
        from orchestrator.modes import ExecutionMode as EM, policy_for as pf
        from orchestrator.pipeline import Orchestrator
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from tests.test_pipeline_run_ledger import TOPUP_CONFIG, _Budget, _Engine, _plan

        tmp = tempfile.mkdtemp()
        if owed_count:
            pending_work.record(Path(tmp) / pending_work.STORE, "earlier", [
                {"job_id": f"o{i}", "posting_id": f"o{i}", "employer_name": f"C{i}",
                 "company_name": f"C{i}", "job_title": "Head of Sales"}
                for i in range(owed_count)])
        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT,
                                {"mode": "live_acquisition_and_enrichment"},
                                run_id=run_id)
        state = StateManager(tmp, pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT),
                             run_id=ctx.run_id)
        cfg = dict(TOPUP_CONFIG, PENDING_WORK_ENABLED=True,
                   PENDING_WORK_RESUME_MAX_PER_RUN=100,
                   FANTASTIC_MONTHLY_GOVERNOR_ENABLED=True,
                   FANTASTIC_JOBS_MAX_JOBS_PER_RUN=0,
                   FANTASTIC_TOPUP_SLICE_JOBS=0)
        with mock.patch.multiple(config, **cfg):
            return Orchestrator(ctx, state, _Budget()).run(
                _plan(lambda _m: LaneResult(lane="fantastic", status="complete",
                                            jobs=[], physical_requests=0),
                      _Engine()), resume=False)

    def test_queued_work_is_processed_despite_a_zero_grant(self):
        result = self._run(3, run_id="20260907T030000Z-gov01")
        acq = (result.get("acquisition") or {}).get("cumulative") or {}
        self.assertEqual((acq.get("pending_work_resumed") or {}).get("adopted"), 3)
        self.assertNotEqual(result["topup"]["final_stop_reason"],
                            "governor_zero_budget")

    def test_with_nothing_owed_it_stops_cleanly_on_the_budget(self):
        """The guard is right when there is genuinely nothing to do: a run with no
        acquisition budget AND no queued work must stop on a budget reason -- cleanly,
        immediately, and never by looping to the iteration guard.

        The label is whichever budget authority bound first (the pre-loop governor
        grant, or the controller's cap); what must not happen is a run that spins."""
        result = self._run(0, run_id="20260907T030000Z-gov02")
        stop = result["topup"]["final_stop_reason"]
        self.assertIn(stop, {"governor_zero_budget", "acquisition_safety_cap",
                             "governor_run_budget"})
        self.assertLessEqual(result["topup"]["iterations"], 1,
                             "a starved run with no queue must not loop")
        self.assertEqual(result["run"]["status"], "complete",
                         "a zero-budget run is a clean stop, never a failure")


class AContactIsAnAddressNotAKey(unittest.TestCase):
    """The 50-call calibration printed `with_contact 26` and `opp->contact 1.0` -- a
    100% conversion -- on a run whose own funnel said `contacts_found 2`.

    `_build_no_contact_lead` still carries a `contact_key`, so counting the key
    counted every no-contact lead as a contact. The one number the calibration existed
    to produce was the one it got wrong, and it got it wrong in the flattering
    direction."""

    def test_a_no_contact_lead_is_not_counted_as_a_contact(self):
        cohort = _cohort({"p1", "p2"})
        _account_recovery_cohort(cohort, [
            _lead("p1", contact_key="acme.example|bucket", email=""),
            _lead("p2", contact_key="beta.example|bucket", email="hm@beta.example"),
        ], _delivery())
        self.assertEqual(cohort["leads"], 2)
        self.assertEqual(cohort["with_contact"], 1)

    def test_the_calibration_shape_no_longer_reports_full_conversion(self):
        """26 leads, 2 with an address -- not 26."""
        cohort = _cohort({f"p{i}" for i in range(26)})
        leads = [_lead(f"p{i}", contact_key=f"c{i}|b", email="") for i in range(24)]
        leads += [_lead(f"p{i}", contact_key=f"c{i}|b", email=f"hm{i}@x.com")
                  for i in (24, 25)]
        _account_recovery_cohort(cohort, leads, _delivery())
        self.assertEqual(cohort["leads"], 26)
        self.assertEqual(cohort["with_contact"], 2)
