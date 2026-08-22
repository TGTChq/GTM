"""Regression for the production zero-acquisition incident (2026-08-20..21).

Root cause: the top-up loop's per-iteration slice cap was applied by MUTATING the
config-validated global FANTASTIC_JOBS_MAX_JOBS_PER_RUN down to the slice size (500),
so validate_fantastic_jobs_config() then rejected LINKEDIN_LIMIT=6000 > MAX=500 with a
ValueError BEFORE any provider request -> lane failed -> jobs=[] -> raw_postings=0,
silently reported status=complete. Fixed by DECOUPLING the runtime slice budget
(FANTASTIC_JOBS_RUN_SLICE_CAP) from the validated ceiling, plus failure propagation and
log observability.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import config
import fantastic_jobs_adapter as fja


def _feed(rows):
    def http_get(url, headers, params, timeout):
        lt = params.get("date_posted_lt")
        sel = [r for r in rows if lt is None or r["date_posted"] < lt]
        sel = sorted(sel, key=lambda r: r["date_posted"], reverse=True)
        o = int(params.get("offset", 0)); l = int(params.get("limit", 100))

        class R:
            status_code = 200
            headers = {"x-api-jobs-remaining": "7000", "x-api-requests-remaining": "9000"}
            def __init__(s, d): s._d = d
            def json(s): return s._d
        return R(sel[o:o + l])
    return http_get


def _recs(n):
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 8, 21, 22, 0, 0, tzinfo=timezone.utc)
    return [{"id": str(9000 + i), "title": "Account Executive", "organization": f"Co{i}",
             "source": "linkedin",
             "date_posted": (now - timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
             "countries_derived": ["United States"], "employment_type": ["FULL_TIME"],
             "org_linkedin_headcount": 100} for i in range(n)]


_PROD = dict(
    FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
    FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
    FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_WELLFOUND_LIMIT=0, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
    FANTASTIC_JOBS_LINKEDIN_LIMIT=6000, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000,   # PROD ceiling
    FANTASTIC_JOBS_TIME_FRAME="7d", FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=70,
    FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=500, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=20,
    FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=False,               # PROD: fail-closed
    FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
    FANTASTIC_JOBS_HEADCOUNT_MAX=1000, FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME",
    FANTASTIC_JOBS_EXCLUDE_AGENCY=True, FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED=True,
    FANTASTIC_JOBS_TITLE_TARGETING_ENABLED=True, FANTASTIC_JOBS_CONTINUATION_ENABLED=False,
)


class SliceBudgetDecoupledTests(unittest.TestCase):
    def test_exact_production_config_validates_and_clamps_to_slice(self):
        # slice 500 with LINKEDIN_LIMIT/MAX 6000 -- the exact live failing config.
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_JOBS_RUN_SLICE_CAP=500)):
            config.validate_fantastic_jobs_config()                    # must NOT raise
            res = fja.run_fantastic_jobs_acquisition(http_get=_feed(_recs(700)))
        self.assertTrue(res.success)
        self.assertEqual(res.errors, [])
        self.assertEqual(len(res.jobs), 500)                           # billed clamped to slice
        self.assertEqual(res.raw_records, 500)

    def test_no_slice_cap_uses_full_ceiling(self):
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_JOBS_RUN_SLICE_CAP=0,
                                                FANTASTIC_JOBS_MAX_JOBS_PER_RUN=300,
                                                FANTASTIC_JOBS_LINKEDIN_LIMIT=300)):
            res = fja.run_fantastic_jobs_acquisition(http_get=_feed(_recs(700)))
        self.assertEqual(len(res.jobs), 300)                           # bounded by ceiling

    def test_slice_smaller_than_inventory_clamps(self):
        # Slice sizes are page-aligned (multiples of 100) in production (500).
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_JOBS_RUN_SLICE_CAP=300)):
            res = fja.run_fantastic_jobs_acquisition(http_get=_feed(_recs(700)))
        self.assertEqual(len(res.jobs), 300)

    def test_zero_genuine_inventory_is_clean_zero_not_error(self):
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_JOBS_RUN_SLICE_CAP=500)):
            res = fja.run_fantastic_jobs_acquisition(http_get=_feed([]))
        self.assertEqual(len(res.jobs), 0)
        self.assertTrue(res.success)                                   # empty feed != failure
        self.assertEqual(res.errors, [])

    def test_validator_still_rejects_true_static_misconfig(self):
        # With NO slice cap, segment_total > ceiling is still a real misconfig.
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_JOBS_RUN_SLICE_CAP=0,
                                                FANTASTIC_JOBS_MAX_JOBS_PER_RUN=500,
                                                FANTASTIC_JOBS_LINKEDIN_LIMIT=6000)):
            with self.assertRaises(ValueError):
                config.validate_fantastic_jobs_config()

    def test_effective_run_cap(self):
        with mock.patch.multiple(config, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000,
                                 FANTASTIC_JOBS_RUN_SLICE_CAP=500):
            self.assertEqual(fja._effective_run_cap(), 500)
        with mock.patch.multiple(config, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000,
                                 FANTASTIC_JOBS_RUN_SLICE_CAP=0):
            self.assertEqual(fja._effective_run_cap(), 6000)
        with mock.patch.multiple(config, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=400,
                                 FANTASTIC_JOBS_RUN_SLICE_CAP=9000):
            self.assertEqual(fja._effective_run_cap(), 400)            # ceiling still wins


class TopupFailurePropagationTests(unittest.TestCase):
    """A FAILED acquisition lane must make the run FAILED, not a silent complete/0."""

    def _run(self, runner):
        from orchestrator.modes import ExecutionMode as EM, policy_for as pf
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from orchestrator.pipeline import Orchestrator, OrchestratorPlan
        from orchestrator.enrichment import EnrichmentReport

        class _Budget:
            lane = source = None
            def reserve(self, *a, **k): return True
            def to_dict(self): return {}

        class _Engine:
            def run(self, opportunities, **k):
                return EnrichmentReport(leads=[], stages=[], loss_census={})

        class _Delivery:
            def deliver(self, leads, **k):
                from orchestrator.adapters_real import RealDeliveryReport
                return RealDeliveryReport(entered=0)

        tmp = tempfile.mkdtemp()
        policy = pf(EM.FULL_DRY_RUN)
        ctx = RunContext.create(EM.FULL_DRY_RUN, {"mode": "full_dry_run"},
                                run_id="20260821T990000Z-fail")
        state = StateManager(tmp, policy, run_id=ctx.run_id)
        plan = OrchestratorPlan(lanes=["fantastic"], lane_runners={"fantastic": runner},
                                enrichment_engine=_Engine(), delivery_manager=_Delivery())
        with mock.patch.multiple(config, NET_NEW_SEND_SAFE_TARGET=5,
                                 FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000,
                                 FANTASTIC_TOPUP_SLICE_JOBS=500,
                                 FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0,
                                 TOPUP_RUNTIME_BUDGET_SECONDS=0, TOPUP_MAX_ITERATIONS=40,
                                 PRE_APOLLO_EXISTING_DEDUPE=False):
            return Orchestrator(ctx, state, _Budget()).run(plan, resume=False)

    def test_failed_lane_makes_run_failed_and_records_error(self):
        from orchestrator.lanes import LaneResult
        err = "ValueError: Fantastic.jobs segment limits (6000) exceed FANTASTIC_JOBS_MAX_JOBS_PER_RUN=500"

        def runner(_m):
            return LaneResult(lane="fantastic", status="failed", jobs=[], errors=[err])

        result = self._run(runner)
        self.assertEqual(result["run"]["status"], "failed")            # NOT "complete"
        self.assertIn("acquisition_failed", result["run"]["stop_reason"])
        self.assertIn("segment limits", result["run"]["stop_reason"])
        self.assertEqual(result["topup"]["final_stop_reason"], "acquisition_failed")
        self.assertIn("segment limits", result["topup"]["acquisition_error"])

    def test_genuine_empty_inventory_still_completes(self):
        from orchestrator.lanes import LaneResult

        def runner(_m):
            return LaneResult(lane="fantastic", status="complete", jobs=[], errors=[])

        result = self._run(runner)
        self.assertEqual(result["run"]["status"], "complete")          # empty != failed
        self.assertEqual(result["topup"]["final_stop_reason"], "inventory_exhausted")
        self.assertEqual(result["topup"]["acquisition_error"], "")


class RunSummaryObservabilityTests(unittest.TestCase):
    """A failed acquisition lane must be visible in stdout (Railway logs)."""

    def test_failed_lane_surfaces_in_summary(self):
        import run_orchestrator as R
        err = "ValueError: Fantastic.jobs segment limits (6000) exceed FANTASTIC_JOBS_MAX_JOBS_PER_RUN=500"
        result = {
            "run": {"status": "failed", "stop_reason": "acquisition_failed: fantastic: " + err},
            "lanes": {"fantastic": {"lane": "fantastic", "status": "failed", "jobs": 0,
                                    "errors": [err], "physical_requests": 0}},
            "waterfall": {}, "enrichment": {}, "delivery": {}, "all_reconcile": False,
        }
        ctx = SimpleNamespace(run_id="20260821T990000Z-fail")
        mode = SimpleNamespace(value="live_acquisition_and_enrichment")
        state = SimpleNamespace(run_dir=lambda: "/tmp/run")
        buf = io.StringIO()
        with redirect_stdout(buf):
            R._print_run_summary(ctx, mode, result, state)
        out = buf.getvalue()
        self.assertIn("Acquisition lanes", out)
        self.assertIn("FAILED", out)
        self.assertIn("segment limits", out)
        self.assertIn("acquisition_failed", out)


if __name__ == "__main__":
    unittest.main()
