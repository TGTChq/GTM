"""Every posting we pay for has exactly one recorded fate.

Three defects are pinned here, all of them found by reading the 2026-09-04
production run summary rather than by any test:

1. ``historical_duplicates`` was ``kept - len(opportunities)``. ``_dedup`` rejects
   THREE different things -- a posting a previous run already worked
   (``previously_seen``), a posting this run bought twice (``duplicate_in_run``)
   and a row with no usable identity -- so one subtraction reported three
   unrelated problems as one number, and the two that are acquisition-efficiency
   problems were indistinguishable from the one that is not.

2. The rich per-source attribution the Fantastic adapter builds
   (``source_attribution.per_source``) was never forwarded; only the shallow
   three-field ``per_source`` reached the orchestrator. Per-source novelty, drain
   state and window cursor were therefore unanswerable downstream.

3. The top-up loop's delivery aggregator summed a hand-picked subset of the
   delivery counters, so the mutually exclusive skip breakdown was dropped: the
   production run reported "1,681 submitted, 781 created" with every loss reason
   at zero. 900 rows with no explanation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from orchestrator.adapters_real import RealDeliveryReport
from orchestrator.enrichment import Disposition, EnrichmentReport, Lead
from orchestrator.lanes import LaneResult
from orchestrator.modes import ExecutionMode as EM, policy_for as pf
from orchestrator.pipeline import Orchestrator, OrchestratorPlan, _merge_source_counts
from orchestrator.reasons import ReasonCode
from orchestrator.run_ledger import LEDGER_STORE
from orchestrator.runcontrol import RunContext
from orchestrator.state import StateManager


class _Budget:
    lane = source = None

    def reserve(self, *a, **k):
        return True

    def to_dict(self):
        return {}


def _lead(n: int) -> Lead:
    return Lead(
        posting_id=f"p{n}",
        company={"name": f"Company {n}"},
        contact={"email": f"hm{n}@example.com"},
        disposition=Disposition.FINAL_PASS,
        primary_reason=ReasonCode.OK,
        contact_key=f"k{n}",
    )


class _Engine:
    def run(self, opportunities, **kwargs):
        leads = [_lead(i) for i in range(len(opportunities))]
        return EnrichmentReport(leads=leads, stages=[], funnel={}, loss_census={})


class _SkipHeavyDelivery:
    """A delivery adapter that reports a REAL skip breakdown, like production."""

    def deliver(self, leads, **kwargs):
        n = len(leads)
        return RealDeliveryReport(
            mode="review_staging",
            entered=n,
            reviewable_submitted=n,
            created=1 if n else 0,
            company_function_suppressed=n - 1 if n else 0,
            no_contact=0,
            other_unreconciled=0,
            failed=0,
            detail={"withheld_before_submit": 0},
        )


def _job(job_id: str, source: str) -> dict:
    return {"job_id": job_id, "posting_id": job_id, "_acquisition_source": source}


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
    YIELD_LEDGER_ENABLED=False,
)


def _runner(slices, attribution=None):
    """Yield one slice of postings per call, then nothing (which stops the loop).

    The trailing empty slice carries an EMPTY attribution, the way a real lane
    that made no request does -- otherwise the fixture would re-report the same
    per-source counts and the aggregation would look like it double-counted.
    """
    calls = {"n": 0}

    def runner(_manager):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(slices):
            return LaneResult(lane="fantastic", status="complete", jobs=[],
                              physical_requests=0)
        return LaneResult(
            lane="fantastic", status="complete", jobs=list(slices[i]),
            physical_requests=1, attribution=dict(attribution or {}),
        )

    return runner


class _Harness(unittest.TestCase):
    def _run(self, slices, *, attribution=None, delivery=None, engine=None,
             seed_seen=()):
        tmp = tempfile.mkdtemp()
        policy = pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT)
        ctx = RunContext.create(
            EM.LIVE_ACQUISITION_AND_ENRICHMENT,
            {"mode": "live_acquisition_and_enrichment"},
            run_id="20260905T030000Z-attrib01",
        )
        state = StateManager(tmp, policy, run_id=ctx.run_id)
        if seed_seen:
            from orchestrator.suppression import SuppressionStore

            SuppressionStore(state).commit_postings(list(seed_seen))
        plan = OrchestratorPlan(
            lanes=["fantastic"],
            lane_runners={"fantastic": _runner(slices, attribution)},
            enrichment_engine=engine or _Engine(),
            delivery_manager=delivery or _SkipHeavyDelivery(),
        )
        with mock.patch.multiple(config, **TOPUP_CONFIG):
            result = Orchestrator(ctx, state, _Budget()).run(plan, resume=False)
        entry = json.loads(
            (Path(tmp) / LEDGER_STORE / f"{ctx.run_id}.json").read_text(encoding="utf-8")
        )
        return result, entry


class DedupeExitsArePartitionedTests(_Harness):
    def test_the_dedupe_exits_are_counted_separately_and_sum_exactly(self):
        """One slice carrying every kind of loss at once.

        j1 is new. j2 is new. j1 again is an in-run duplicate. j9 was worked by a
        previous run. The last row carries no provider id, so it is identified by
        the content-digest rung of ``posting_identity`` -- which is why
        ``postings_missing_identity`` stays 0 here and, in practice, always: the
        identity ladder has no "no identity" outcome. It is instrumented anyway so
        that a future rung that CAN fail is counted rather than silently folded
        into the duplicate counts, which is exactly what used to happen.

        Before this change all three losses were reported as
        ``historical_duplicates``.
        """
        slices = [[
            _job("j1", "fantastic_jobs_ats"),
            _job("j2", "fantastic_jobs_ats"),
            _job("j1", "fantastic_jobs_linkedin"),
            _job("j9", "fantastic_jobs_linkedin"),
            {"_acquisition_source": "fantastic_jobs_linkedin"},
        ]]
        result, _ = self._run(slices, seed_seen=["j9"])
        cum = result["acquisition"]["cumulative"]

        self.assertEqual(cum["net_new_jobs_captured"], 3)
        self.assertEqual(cum["historical_previously_seen_duplicates"], 1)
        self.assertEqual(cum["canonical_duplicates_in_run"], 1)
        self.assertEqual(cum["postings_missing_identity"], 0)
        self.assertTrue(cum["dedupe_reconciles"])
        self.assertEqual(
            cum["jobs_unique_kept"],
            cum["net_new_jobs_captured"]
            + cum["historical_previously_seen_duplicates"]
            + cum["canonical_duplicates_in_run"]
            + cum["postings_missing_identity"],
            "the four fates must partition every posting the lanes kept",
        )

    def test_historical_duplicates_no_longer_absorbs_in_run_duplicates(self):
        """The exact regression: a run with ONLY in-run duplicates.

        The old subtraction reported 2 historical duplicates here, which reads as
        "a previous run already did this work". Nothing of the sort happened: we
        bought the same two rows twice inside one run.
        """
        slices = [[
            _job("j1", "ats"), _job("j1", "ats"),
            _job("j2", "ats"), _job("j2", "linkedin"),
        ]]
        result, entry = self._run(slices)
        cum = result["acquisition"]["cumulative"]

        self.assertEqual(cum["canonical_duplicates_in_run"], 2)
        self.assertEqual(cum["historical_previously_seen_duplicates"], 0)
        self.assertEqual(entry["metrics"]["historical_duplicates"], 0)
        self.assertEqual(entry["metrics"]["canonical_duplicates_in_run"], 2)

    def test_counters_accumulate_across_topup_slices(self):
        slices = [
            [_job("a1", "ats"), _job("seen1", "ats")],
            [_job("a2", "ats"), _job("seen2", "ats"), _job("a2", "ats")],
        ]
        result, _ = self._run(slices, seed_seen=["seen1", "seen2"])
        cum = result["acquisition"]["cumulative"]

        self.assertEqual(cum["net_new_jobs_captured"], 2)
        self.assertEqual(cum["historical_previously_seen_duplicates"], 2)
        self.assertEqual(cum["canonical_duplicates_in_run"], 1)


class PerSourceAttributionReachesTheLedgerTests(_Harness):
    def test_rich_source_attribution_is_forwarded_not_only_the_shallow_block(self):
        attribution = {
            "per_source": {
                "fantastic_jobs_ats": {"jobs": 2, "returned_billed": 10, "requests": 1},
            },
            "source_attribution": {
                "per_source": {
                    "fantastic_jobs_ats": {
                        "returned_billed": 10, "unique_kept": 4, "duplicates": 3,
                        "cross_source_duplicates": 1, "schema_rejected": 2,
                        "source_filtered_out": 1, "requests": 1,
                        "stop_reason": "cap_reached",
                    },
                },
            },
            "watermark": {
                "window_cursors": {
                    "fantastic_jobs_ats": {"offset_from": 3000, "offset_to": 3010},
                },
                "drained_sources": {"fantastic_jobs_ats": False},
            },
        }
        result, entry = self._run(
            [[_job("a1", "fantastic_jobs_ats"), _job("a2", "fantastic_jobs_ats")]],
            attribution=attribution,
        )
        src = result["acquisition"]["cumulative"]["per_source"]["fantastic_jobs_ats"]

        # Fields that only the RICH block carries...
        self.assertEqual(src["unique_kept"], 4)
        self.assertEqual(src["duplicates"], 3)
        self.assertEqual(src["schema_rejected"], 2)
        self.assertEqual(src["source_filtered_out"], 1)
        self.assertEqual(src["stop_reason"], "cap_reached")
        # ...the shallow one's kept count is preserved...
        self.assertEqual(src["jobs"], 2)
        # ...the cursor is visible, and the source is NOT drained...
        self.assertEqual(src["offset_from"], 3000)
        self.assertEqual(src["offset_to"], 3010)
        self.assertIs(src["drained"], False)
        # ...and the pipeline's own dedupe verdict lands on the same row.
        self.assertEqual(src["net_new"], 2)
        self.assertEqual(src["novelty_pct"], 20.0)
        self.assertEqual(entry["source_counts"]["fantastic_jobs_ats"]["net_new"], 2)

    def test_the_first_requested_offset_survives_a_second_slice(self):
        """``offset_from`` is the RESUME point, so a later slice must not overwrite
        it -- otherwise the cursor acceptance test compares a run against itself."""
        into: dict = {}
        for offsets in ((100, 200), (200, 350)):
            _merge_source_counts(into, {"fantastic": LaneResult(
                lane="fantastic", status="complete", jobs=[],
                attribution={
                    "per_source": {"ats": {"jobs": 0, "returned_billed": 0, "requests": 0}},
                    "watermark": {"window_cursors": {
                        "ats": {"offset_from": offsets[0], "offset_to": offsets[1]}}},
                },
            )})
        self.assertEqual(into["ats"]["offset_from"], 100)
        self.assertEqual(into["ats"]["offset_to"], 350)

    def test_a_lane_with_no_rich_block_still_reports_what_it_has(self):
        result, _ = self._run(
            [[_job("a1", "free_himalayas")]],
            attribution={"per_source": {
                "free_himalayas": {"jobs": 1, "returned_billed": 1, "requests": 1}}},
        )
        src = result["acquisition"]["cumulative"]["per_source"]["free_himalayas"]
        self.assertEqual(src["jobs"], 1)
        self.assertEqual(src["net_new"], 1)
        self.assertNotIn("stop_reason", src)


class DeliverySkipBreakdownSurvivesAggregationTests(_Harness):
    def test_every_submitted_row_is_accounted_for_after_the_slices_are_summed(self):
        """The 2026-09-04 hole: submitted - created - failed == sum(skips)."""
        slices = [[_job(f"a{i}", "ats") for i in range(4)],
                  [_job(f"b{i}", "ats") for i in range(3)]]
        result, entry = self._run(slices)
        deliv = result["delivery"]

        self.assertEqual(deliv["reviewable_submitted"], 7)
        self.assertEqual(deliv["created"], 2)
        self.assertEqual(deliv["skip_breakdown"]["company_function_suppressed"], 5)
        self.assertEqual(
            deliv["reviewable_submitted"] - deliv["created"] - deliv["failed"],
            sum(deliv["skip_breakdown"].values()),
            "no submitted row may disappear between the slices and the total",
        )
        self.assertTrue(deliv["reviewable_reconciles"])
        self.assertEqual(entry["delivery_skip_breakdown"]["company_function_suppressed"], 5)
        self.assertEqual(entry["metrics"]["airtable_candidates"], 7)


class InterimLedgerSnapshotUsesNetNewTests(_Harness):
    def test_the_mid_slice_checkpoint_never_reports_provider_volume_as_captured(self):
        """A run interrupted between acquisition and delivery must still say
        net-new. The checkpoint used to write ``jobs_unique_kept``, so an
        interrupted run permanently reported duplicates as work delivered."""

        class _Boom:
            def run(self, opportunities, **kwargs):
                raise RuntimeError("enrichment died after acquisition")

        tmp = tempfile.mkdtemp()
        policy = pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT)
        ctx = RunContext.create(
            EM.LIVE_ACQUISITION_AND_ENRICHMENT,
            {"mode": "live_acquisition_and_enrichment"},
            run_id="20260905T030000Z-interrupt",
        )
        state = StateManager(tmp, policy, run_id=ctx.run_id)
        from orchestrator.suppression import SuppressionStore

        SuppressionStore(state).commit_postings(["old1", "old2", "old3"])
        plan = OrchestratorPlan(
            lanes=["fantastic"],
            lane_runners={"fantastic": _runner([[
                _job("new1", "ats"), _job("old1", "ats"),
                _job("old2", "ats"), _job("old3", "ats"),
            ]])},
            enrichment_engine=_Boom(),
            delivery_manager=_SkipHeavyDelivery(),
        )
        with mock.patch.multiple(config, **TOPUP_CONFIG):
            with self.assertRaises(RuntimeError):
                Orchestrator(ctx, state, _Budget()).run(plan, resume=False)
        entry = json.loads(
            (Path(tmp) / LEDGER_STORE / f"{ctx.run_id}.json").read_text(encoding="utf-8")
        )

        self.assertEqual(entry["metrics"]["jobs_captured"], 1,
                         "1 net-new posting, not the 4 rows the provider billed")
        self.assertEqual(entry["metrics"]["net_new_jobs_captured"], 1)
        self.assertEqual(entry["metrics"]["historical_duplicates"], 3)


class SinglePassPathUsesBillingAccurateCountersTests(unittest.TestCase):
    def test_provider_jobs_returned_is_billed_rows_not_kept_rows(self):
        """``NET_NEW_SEND_SAFE_TARGET=0`` takes the single-pass body. It reported
        ``len(postings)`` -- the unique-KEPT lane output -- as provider rows
        returned, understating what we paid for by every row the provider's own
        schema/source filter dropped."""
        tmp = tempfile.mkdtemp()
        policy = pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT)
        ctx = RunContext.create(
            EM.LIVE_ACQUISITION_AND_ENRICHMENT,
            {"mode": "live_acquisition_and_enrichment"},
            run_id="20260905T030000Z-singlepass",
        )
        state = StateManager(tmp, policy, run_id=ctx.run_id)
        plan = OrchestratorPlan(
            lanes=["fantastic"],
            lane_runners={"fantastic": _runner(
                [[_job("s1", "ats"), _job("s2", "ats")]],
                attribution={"raw_records": 50, "jobs_quota_consumed": 50,
                             "per_source": {"ats": {"jobs": 2, "returned_billed": 50,
                                                    "requests": 1}}},
            )},
            enrichment_engine=_Engine(),
            delivery_manager=_SkipHeavyDelivery(),
        )
        with mock.patch.multiple(config, **{**TOPUP_CONFIG, "NET_NEW_SEND_SAFE_TARGET": 0}):
            Orchestrator(ctx, state, _Budget()).run(plan, resume=False)
        entry = json.loads(
            (Path(tmp) / LEDGER_STORE / f"{ctx.run_id}.json").read_text(encoding="utf-8")
        )

        self.assertEqual(entry["metrics"]["provider_jobs_returned"], 50)
        self.assertEqual(entry["metrics"]["provider_jobs_billed"], 50)
        self.assertEqual(entry["metrics"]["jobs_captured"], 2)
        self.assertEqual(entry["source_counts"]["ats"]["net_new"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class SendSafeWithholdingIsNamedTests(_Harness):
    """The 2026-09-04 gap, identified.

    ``AIRTABLE_WRITE_SEND_SAFE_ONLY=1`` is set in production, so a candidate that
    fails ``send_safe_facts`` is NOT WRITTEN AT ALL -- not written as Pending, not
    written in any status. On 2026-09-04 that was the largest single category:
    1,681 submitted, 781 created. It reached the summary as an unnamed residual,
    which reads as "900 rows lost" rather than "900 rows deliberately withheld
    because their stored facts are not send-safe".
    """

    def test_withheld_rows_are_their_own_counter_not_an_unnamed_residual(self):
        class _WithholdingDelivery:
            def deliver(self, leads, **kwargs):
                n = len(leads)
                return RealDeliveryReport(
                    mode="review_staging", entered=n, reviewable_submitted=n,
                    created=1 if n else 0,
                    send_safe_withheld=n - 1 if n else 0,
                    detail={"withheld_before_submit": 0})

        slices = [[_job(f"a{i}", "ats") for i in range(5)],
                  [_job(f"b{i}", "ats") for i in range(3)]]
        result, entry = self._run(slices, delivery=_WithholdingDelivery())
        deliv = result["delivery"]

        self.assertEqual(deliv["reviewable_submitted"], 8)
        self.assertEqual(deliv["created"], 2)
        self.assertEqual(deliv["skip_breakdown"]["send_safe_withheld"], 6)
        self.assertEqual(deliv["skip_breakdown"]["other"], 0,
                         "the residual is now empty because the reason is named")
        self.assertEqual(
            deliv["reviewable_submitted"] - deliv["created"] - deliv["failed"],
            sum(deliv["skip_breakdown"].values()))
        self.assertTrue(deliv["reviewable_reconciles"])
        self.assertEqual(entry["delivery_skip_breakdown"]["send_safe_withheld"], 6)
