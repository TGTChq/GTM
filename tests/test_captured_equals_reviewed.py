"""``jobs_captured`` and ``jobs_reviewed`` are one list measured twice.

The weekly report recovers a missing capture count from the review count. That is
only legitimate while nothing filters the postings between the two measurements, and
"nothing filters between them" is a property of two files that no test asserted.

The chain, end to end:

  pipeline._dedup(...)              -> opportunities
  net_new_jobs_captured += len(opportunities)          <- first measurement
  enrichment_engine.run(opportunities)                 <- the SAME list object
  RealEnrichmentStage.run: postings.json = {"jobs": opportunities}
  run_precontact_qualification(postings.json).input_jobs = len(jobs)  <- second

Insert a cap, a filter or a slice anywhere along it and the recovery starts
under-reporting captured work in silence. These two tests fail instead.

They pin the halves separately because the halves fail differently: the pipeline
half would drop postings before enrichment ever saw them, the adapter half would
drop them after it did.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from orchestrator.enrichment import EnrichmentReport
from orchestrator.modes import ExecutionMode as EM, policy_for as pf
from orchestrator.pipeline import Orchestrator
from orchestrator.run_ledger import LEDGER_STORE
from orchestrator.runcontrol import RunContext
from orchestrator.state import StateManager
from orchestrator.lanes import LaneResult
from test_pipeline_run_ledger import (
    TOPUP_CONFIG,
    _Budget,
    _lead,
    _plan,
    _two_slice_runner,
)
from weekly_report.metrics import build_run_metrics
from weekly_report.run_artifacts import discover_runs


class _FaithfulEngine:
    """Reports the funnel the way the REAL adapter does: one input job per posting
    handed in. The shared stub multiplies by ten so its other assertions can tell
    the funnel fields apart, which makes it useless for an identity."""

    def run(self, opportunities, **kwargs):
        n = len(opportunities)
        if not n:
            return EnrichmentReport(leads=[], stages=[])
        return EnrichmentReport(
            leads=[_lead(0, email="hm@example.com")],
            stages=[],
            funnel={"qualification_input": n, "contact_discovery_entered": n},
        )


class CapturedEqualsReviewedTests(unittest.TestCase):
    def test_the_pipeline_hands_enrichment_every_posting_it_counted(self):
        tmp = tempfile.mkdtemp()
        policy = pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT)
        ctx = RunContext.create(
            EM.LIVE_ACQUISITION_AND_ENRICHMENT,
            {"mode": "live_acquisition_and_enrichment"},
            run_id="20260905T140000Z-identity1",
        )
        state = StateManager(tmp, policy, run_id=ctx.run_id)
        plan = _plan(_two_slice_runner([]), _FaithfulEngine())
        with mock.patch.multiple(config, **TOPUP_CONFIG):
            Orchestrator(ctx, state, _Budget()).run(plan, resume=False)
        entry = json.loads(
            (Path(tmp) / LEDGER_STORE / f"{ctx.run_id}.json").read_text(encoding="utf-8"))

        metrics = entry["metrics"]
        self.assertEqual(metrics["jobs_captured"], metrics["jobs_reviewed"],
                         "a posting was counted as captured but never reached review")
        self.assertGreater(metrics["jobs_captured"], 0, "the run must have captured something")

    def test_the_adapter_qualifies_every_posting_it_was_given(self):
        """``RealEnrichmentStage`` writes its input verbatim, so qualification's
        input_jobs is the length it was handed."""
        from orchestrator.adapters_real import RealEnrichmentStage

        postings = [{"job_id": f"j{i}", "company": f"C{i}", "title": "Engineer"}
                    for i in range(7)]
        seen = {}

        def _fake_qualification(input_path, **kwargs):
            seen["written"] = json.loads(Path(input_path).read_text(encoding="utf-8"))["jobs"]
            raise _Stop()

        class _Stop(Exception):
            pass

        stage = RealEnrichmentStage(workdir=tempfile.mkdtemp())
        with mock.patch("qualification_pipeline.run_precontact_qualification",
                        _fake_qualification):
            with self.assertRaises(_Stop):
                stage.run(list(postings))

        self.assertEqual(len(seen["written"]), len(postings),
                         "the adapter must qualify every posting it was handed")
        self.assertEqual([j["job_id"] for j in seen["written"]],
                         [j["job_id"] for j in postings],
                         "and the same ones, in order -- not a filtered subset")


class AnIncompleteRunMayNotBeRecoveredTests(unittest.TestCase):
    """The equality is PER SLICE, so an interrupted run breaks it in one direction.

    Acquisition accumulates ``len(opportunities)`` for every slice it runs. The
    funnel accumulates ``qualification_input`` for every slice that FINISHES
    enrichment. A run that stops in between has run more acquisition slices than
    enrichment slices, so reviewed is a FLOOR -- and an aggregate has nowhere to
    record "this contribution is a floor". It would be summed as though exact, and
    the period would silently understate captured work.

    The weekly report therefore consults the review count for a capture count only
    on a run that COMPLETED. These tests hold that guard shut.
    """

    def _two_of_three_slices_enriched(self, root, run_id, *, state, status):
        """A three-slice acquisition where only the first two reach the funnel."""
        d = root / "run_artifacts" / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "run_manifest.json").write_text(json.dumps({
            "run_id": run_id, "started_at": "2026-09-08T03:00:00Z",
            "finished_at": "2026-09-08T05:00:00Z", "status": status,
            "mode": "live_acquisition_and_enrichment",
            "policy": {"allow_instantly_enrollment": False}}), encoding="utf-8")
        (d / "orchestrator_result.json").write_text(json.dumps({
            # three slices acquired 300; two slices reviewed 200.
            "acquisition": {"cumulative": {"jobs_unique_kept": 300}},
            "enrichment": {"funnel": {"qualification_input": 200}},
            "run": {"policy": {"allow_instantly_enrollment": False}}, "lanes": {}}),
            encoding="utf-8")

    def test_an_interrupted_run_does_not_donate_its_review_count(self):
        tmp = Path(tempfile.mkdtemp()) / "orchestrator_v2"
        self._two_of_three_slices_enriched(
            tmp, "20260908T030000Z-partial1", state="failed", status="failed")
        runs, _ = discover_runs([tmp])
        metric = build_run_metrics(runs)["jobs_captured"]
        self.assertIsNone(
            metric.value,
            "200 reviewed is a floor under 300 acquired; it must not stand in as captured")
        self.assertEqual(metric.runs_missing_field, ["20260908T030000Z-partial1"])

    def test_a_completed_run_still_donates_its_review_count(self):
        """The guard must not be so tight that it refuses the case it was built for."""
        tmp = Path(tempfile.mkdtemp()) / "orchestrator_v2"
        self._two_of_three_slices_enriched(
            tmp, "20260908T030000Z-whole001", state="complete", status="complete")
        runs, _ = discover_runs([tmp])
        metric = build_run_metrics(runs)["jobs_captured"]
        self.assertEqual(metric.value, 200)
        self.assertIn("qualification_input", metric.evidence[0])


if __name__ == "__main__":
    unittest.main()


class TheBackfillMustNotPersistProviderVolumeTests(unittest.TestCase):
    """What retention leaves behind has to be the right number.

    Heavy artifacts are kept for four runs; the ledger is kept for 180 days. So the
    backfill's reading is the LAST reading — after it, nothing on disk can
    contradict it. Writing provider volume under ``jobs_captured`` there is worse
    than printing it once, because the evidence that could correct it is gone.
    """

    def _artifacts(self, root, run_id, *, waterfall, acquisition):
        d = root / "run_artifacts" / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "run_manifest.json").write_text(json.dumps({
            "run_id": run_id, "started_at": "2026-09-04T13:01:30Z",
            "finished_at": "2026-09-04T16:21:24Z", "status": "complete",
            "mode": "live_acquisition_and_enrichment", "policy": {}}), encoding="utf-8")
        (d / "waterfall.json").write_text(json.dumps(waterfall), encoding="utf-8")
        (d / "orchestrator_result.json").write_text(json.dumps({
            "acquisition": {"cumulative": acquisition},
            "enrichment": {"funnel": {}}, "lanes": {}}), encoding="utf-8")

    def test_the_dedupe_stage_answers_and_provider_volume_never_does(self):
        """The shape of 20260904T130130Z-13b44a0c: no net-new counter, 6,205
        provider rows, 2,410 leads, and the real capture count sitting in the
        acquisition-dedupe stage where only the right unit lives."""
        from orchestrator.run_ledger import backfill_from_artifacts, read_entries

        root = Path(tempfile.mkdtemp()) / "orchestrator_v2"
        self._artifacts(
            root, "20260904T130130Z-13b44a0c",
            waterfall={"unit_totals": {"postings": 6205, "opportunities": 2410,
                                       "contacts": 1048},
                       "stages": [{"stage": "acquisition_dedup", "unit": "posting",
                                   "entered": 6205, "passed": 1830}],
                       "final_pass_count": 711},
            acquisition={"jobs_unique_kept": 6205, "jobs_returned_billed": 6205})

        backfill_from_artifacts(root)
        entry = read_entries(root)[0][0]

        self.assertEqual(entry["metrics"]["jobs_captured"], 1830)
        self.assertEqual(entry["metrics"]["net_new_jobs_captured"], 1830)
        self.assertNotEqual(entry["metrics"]["jobs_captured"], 6205,
                            "provider volume must never be written as captured work")
        self.assertNotEqual(entry["metrics"]["jobs_captured"], 2410,
                            "unit_totals.opportunities is leads on the top-up path")
        self.assertTrue(entry.get("backfilled_from_artifacts"),
                        "a reconstruction must say that it is one")

    def test_a_run_without_the_stage_stays_silent_rather_than_guessing(self):
        """No dedupe stage and no net-new counter means the run cannot answer. It
        must not be topped up from the nearest available number."""
        from orchestrator.run_ledger import backfill_from_artifacts, read_entries

        root = Path(tempfile.mkdtemp()) / "orchestrator_v2"
        self._artifacts(
            root, "20260901T130000Z-nostage1",
            waterfall={"unit_totals": {"postings": 6205, "opportunities": 2410,
                                       "contacts": 1048}},
            acquisition={"jobs_unique_kept": 6205, "jobs_returned_billed": 6205})

        backfill_from_artifacts(root)
        metrics = read_entries(root)[0][0]["metrics"]

        self.assertNotIn("jobs_captured", metrics)
        self.assertNotIn("net_new_jobs_captured", metrics)
        self.assertEqual(metrics.get("provider_jobs_returned"), 6205,
                         "provider volume is still recorded -- under its own name")
