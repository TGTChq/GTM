"""End-to-end: the real pipeline writes the compact reporting ledger.

Unit-testing ``RunLedger`` proves the store works. These tests prove the
*pipeline* actually populates it, on the path production runs (adaptive top-up,
``NET_NEW_SEND_SAFE_TARGET > 0``), which is the only thing that makes next week's
report reconstructible.

They also pin the second defect found on 2026-09-04: the top-up body rebuilt its
final report as ``EnrichmentReport(leads=all_leads, stages=[])``, discarding every
slice's funnel. ``enrichment.funnel`` was therefore ALWAYS ``{}`` on the
production path, so ``jobs_reviewed`` and ``qualified_opportunities`` were
unreportable no matter how productive the run had been. The zero-capture week hid
it: with nothing acquired, an empty funnel looked like a consequence, not a bug.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from orchestrator.enrichment import Disposition, EnrichmentReport, Lead
from orchestrator.lanes import LaneResult
from orchestrator.modes import ExecutionMode as EM, policy_for as pf
from orchestrator.pipeline import Orchestrator, OrchestratorPlan
from orchestrator.reasons import ReasonCode
from orchestrator.run_ledger import (
    LEDGER_STORE,
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_RUNNING,
    RunLedger,
)
from orchestrator.runcontrol import RunContext
from orchestrator.state import StateManager


class _Budget:
    lane = source = None

    def reserve(self, *a, **k):
        return True

    def to_dict(self):
        return {}


def _lead(n: int, *, email: str = "") -> Lead:
    return Lead(
        posting_id=f"p{n}",
        company={"name": f"Company {n}"},
        contact={"email": email} if email else {},
        disposition=Disposition.FINAL_PASS if email else Disposition.UNVERIFIED,
        primary_reason=ReasonCode.OK,
        contact_key=f"k{n}",
    )


class _Engine:
    """Returns a funnel per slice, the way the real enrichment adapter does."""

    def __init__(self, per_slice_leads: int = 2) -> None:
        self.calls = 0
        self.per_slice_leads = per_slice_leads

    def run(self, opportunities, **kwargs):
        self.calls += 1
        n = len(opportunities)
        if not n:
            # What the real adapter does with nothing to enrich: an empty report
            # with NO funnel at all. Verified against the 2026-09-03 production
            # artifacts, whose enrichment.funnel is {}.
            return EnrichmentReport(leads=[], stages=[])
        leads = [_lead(i, email=f"hm{i}@example.com") for i in range(min(n, self.per_slice_leads))]
        return EnrichmentReport(
            leads=leads,
            stages=[],
            loss_census={"hiring_manager_not_found": 3},
            funnel={
                "qualification_input": 10 * n,
                # what the hiring-manager stage emits at the people-search call
                "contact_discovery_entered": 4 * n,
                "target_role_eligible": 9 * n,
                "companies_considered": 2 * n,
            },
        )


class _Delivery:
    def deliver(self, leads, **kwargs):
        from orchestrator.adapters_real import RealDeliveryReport

        # `withheld_before_submit` is declared because the real adapter always
        # writes it and `reconciles()` no longer accepts its absence as a pass:
        # an unrecorded withheld count is an unverifiable identity, not a
        # satisfied one. Everything entered here was submitted.
        return RealDeliveryReport(entered=len(leads), reviewable_submitted=len(leads),
                                  created=len(leads), skipped_existing=1,
                                  detail={"withheld_before_submit": 0})


def _plan(runner, engine=None):
    return OrchestratorPlan(
        lanes=["fantastic"],
        lane_runners={"fantastic": runner},
        enrichment_engine=engine or _Engine(),
        delivery_manager=_Delivery(),
    )


def _two_slice_runner(seen):
    """Slice 1 acquires two postings, slice 2 acquires nothing and stops the loop."""

    def runner(_manager):
        i = len(seen)
        seen.append(i)
        jobs = (
            [{"job_id": "j1", "posting_id": "j1"}, {"job_id": "j2", "posting_id": "j2"}]
            if i == 0
            else []
        )
        return LaneResult(lane="fantastic", status="complete", jobs=jobs, physical_requests=1)

    return runner


TOPUP_CONFIG = dict(
    NET_NEW_SEND_SAFE_TARGET=5,
    FANTASTIC_JOBS_MAX_JOBS_PER_RUN=1000,
    FANTASTIC_TOPUP_SLICE_JOBS=500,
    FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0,
    TOPUP_RUNTIME_BUDGET_SECONDS=0,
    TOPUP_MAX_ITERATIONS=40,
    PRE_APOLLO_EXISTING_DEDUPE=False,
    FANTASTIC_MONTHLY_GOVERNOR_ENABLED=False,
    FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=False,
)


class PipelineWritesTheReportingLedgerTests(unittest.TestCase):
    def _run(self, engine=None, runner=None):
        tmp = tempfile.mkdtemp()
        policy = pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT)
        ctx = RunContext.create(
            EM.LIVE_ACQUISITION_AND_ENRICHMENT, {"mode": "live_acquisition_and_enrichment"},
            run_id="20260905T130000Z-ledger01",
        )
        state = StateManager(tmp, policy, run_id=ctx.run_id)
        seen: list = []
        plan = _plan(runner or _two_slice_runner(seen), engine)
        with mock.patch.multiple(config, **TOPUP_CONFIG):
            result = Orchestrator(ctx, state, _Budget()).run(plan, resume=False)
        entry = json.loads(
            (Path(tmp) / LEDGER_STORE / f"{ctx.run_id}.json").read_text(encoding="utf-8")
        )
        return result, entry, Path(tmp)

    def test_a_completed_run_leaves_a_finalized_ledger_entry(self):
        result, entry, root = self._run()

        self.assertEqual(entry["run_id"], "20260905T130000Z-ledger01")
        self.assertEqual(entry["state"], STATE_COMPLETE)
        self.assertTrue(entry["started_at"])
        self.assertTrue(entry["finished_at"])
        self.assertIn("final", entry["stages_recorded"])
        self.assertEqual(entry["mode"], "live_acquisition_and_enrichment")

    def test_the_ledger_carries_the_business_counters_the_weekly_report_reads(self):
        _, entry, _ = self._run()
        metrics = entry["metrics"]

        self.assertEqual(metrics["jobs_captured"], 2)
        # Two postings entered enrichment: the engine reports 10x/4x per posting.
        self.assertEqual(metrics["jobs_reviewed"], 20)
        self.assertEqual(metrics["qualified_opportunities"], 8)
        self.assertEqual(metrics["contacts_found"], 2)
        self.assertEqual(metrics["sent_to_airtable"], 2)
        self.assertTrue(entry["acquisition_entered"])

    def test_a_run_that_may_not_enroll_records_no_instantly_counter(self):
        """Absent, not zero: the run provably cannot answer that question."""
        _, entry, _ = self._run()
        self.assertNotIn("sent_to_instantly", entry["metrics"])
        self.assertIs(entry["policy"]["allow_instantly_enrollment"], False)

    def test_the_topup_path_now_preserves_the_enrichment_funnel(self):
        """Regression: the production path used to discard every slice's funnel."""
        result, _, _ = self._run()
        funnel = result["enrichment"]["funnel"]

        self.assertEqual(funnel.get("qualification_input"), 20)
        self.assertEqual(funnel.get("contact_discovery_entered"), 8)
        self.assertEqual(funnel.get("target_role_eligible"), 18,
                         "the loose role gate is still recorded, just not as 'qualified'")
        self.assertEqual(
            result["enrichment"]["loss_census"].get("hiring_manager_not_found"), 3,
            "the per-slice loss census is preserved too",
        )

    def test_funnels_are_summed_across_slices_not_overwritten(self):
        """Two productive slices must add up, not report only the last one."""
        seen: list = []

        def runner(_manager):
            i = len(seen)
            seen.append(i)
            jobs = [{"job_id": f"j{i}", "posting_id": f"j{i}"}] if i < 2 else []
            return LaneResult(lane="fantastic", status="complete", jobs=jobs,
                              physical_requests=1)

        _, entry, _ = self._run(runner=runner)
        # One posting per slice for two slices, at 10 reviewed per posting.
        self.assertEqual(entry["metrics"]["jobs_captured"], 2)
        self.assertEqual(entry["metrics"]["jobs_reviewed"], 20)

    def test_a_zero_capture_run_records_a_measured_zero_and_no_downstream_keys(self):
        """The 2026-W36 shape: acquisition returns nothing, enrichment never runs."""
        seen: list = []

        def runner(_manager):
            seen.append(1)
            return LaneResult(lane="fantastic", status="complete", jobs=[],
                              physical_requests=0)

        _, entry, _ = self._run(runner=runner)

        self.assertEqual(entry["metrics"]["jobs_captured"], 0, "a real, measured zero")
        self.assertNotIn("jobs_reviewed", entry["metrics"],
                         "qualification never ran; the key must be absent, not 0")
        self.assertFalse(entry["acquisition_entered"])
        self.assertEqual(entry["state"], STATE_COMPLETE)

    def test_a_failed_acquisition_lane_is_finalized_as_failed_not_left_running(self):
        """A lane crash is contained by LaneManager and becomes a FAILED run.

        The point for reporting is that the entry reaches a terminal state: a
        failed run must never be read as an interrupted one, and vice versa.
        """
        tmp = tempfile.mkdtemp()
        policy = pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT)
        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT,
                                {"mode": "live_acquisition_and_enrichment"},
                                run_id="20260905T130000Z-boom0001")
        state = StateManager(tmp, policy, run_id=ctx.run_id)

        def runner(_manager):
            raise RuntimeError("provider exploded")

        with mock.patch.multiple(config, **TOPUP_CONFIG):
            Orchestrator(ctx, state, _Budget()).run(_plan(runner), resume=False)

        entry = json.loads(
            (Path(tmp) / LEDGER_STORE / f"{ctx.run_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(entry["state"], STATE_FAILED)
        self.assertIn("acquisition_failed", entry["stop_reason"])
        self.assertNotEqual(entry["state"], STATE_RUNNING)

    def test_an_exception_inside_the_body_still_finalizes_the_entry(self):
        """The failure-safe ``finally`` closes the ledger even when the run raises."""
        tmp = tempfile.mkdtemp()
        policy = pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT)
        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT,
                                {"mode": "live_acquisition_and_enrichment"},
                                run_id="20260905T130000Z-boom0002")
        state = StateManager(tmp, policy, run_id=ctx.run_id)

        class _ExplodingDelivery:
            def deliver(self, leads, **kwargs):
                raise RuntimeError("airtable exploded")

        plan = OrchestratorPlan(
            lanes=["fantastic"],
            lane_runners={"fantastic": _two_slice_runner([])},
            enrichment_engine=_Engine(),
            delivery_manager=_ExplodingDelivery(),
        )
        with mock.patch.multiple(config, **TOPUP_CONFIG):
            with self.assertRaises(RuntimeError):
                Orchestrator(ctx, state, _Budget()).run(plan, resume=False)

        entry = json.loads(
            (Path(tmp) / LEDGER_STORE / f"{ctx.run_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(entry["state"], STATE_FAILED)
        self.assertIn("RuntimeError", entry["stop_reason"])
        # The counters it DID reach before the crash are preserved.
        self.assertEqual(entry["metrics"]["jobs_captured"], 2)

    def test_a_hard_killed_run_leaves_a_running_entry_that_still_reports(self):
        """No finally runs on SIGKILL. The entry created at begin() is what survives."""
        tmp = Path(tempfile.mkdtemp())
        ledger = RunLedger(tmp, "20260904T064411Z-66ea967e")
        ledger.begin(mode="live_acquisition_and_enrichment", lanes=["fantastic"])
        ledger.record("acquisition", {"jobs_captured": 6206})
        # process dies here -- no finalize()

        entry = json.loads(ledger.path.read_text(encoding="utf-8"))
        self.assertEqual(entry["state"], STATE_RUNNING)
        self.assertEqual(entry["metrics"]["jobs_captured"], 6206)

    def test_a_ledger_write_failure_never_breaks_the_run(self):
        """Reporting is not allowed to take down production.

        The real I/O is broken (not the guard around it), so this exercises the
        fail-open path the pipeline actually depends on.
        """
        tmp = tempfile.mkdtemp()
        policy = pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT)
        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT,
                                {"mode": "live_acquisition_and_enrichment"},
                                run_id="20260905T130000Z-nowrite1")
        state = StateManager(tmp, policy, run_id=ctx.run_id)
        seen: list = []
        orchestrator = Orchestrator(ctx, state, _Budget())

        def explode(*args, **kwargs):
            raise OSError("disk full")

        with mock.patch("orchestrator.run_ledger.tempfile.mkstemp", explode):
            with mock.patch.multiple(config, **TOPUP_CONFIG):
                result = orchestrator.run(_plan(_two_slice_runner(seen)), resume=False)

        self.assertTrue(result["all_reconcile"])
        self.assertEqual(result["waterfall"]["unit_totals"]["postings"], 2)
        self.assertFalse((Path(tmp) / LEDGER_STORE / f"{ctx.run_id}.json").exists())
        self.assertTrue(orchestrator.ledger.errors, "the failure is recorded, not swallowed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
