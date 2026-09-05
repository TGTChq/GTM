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
from test_pipeline_run_ledger import (
    TOPUP_CONFIG,
    _Budget,
    _lead,
    _plan,
    _two_slice_runner,
)


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


if __name__ == "__main__":
    unittest.main()
