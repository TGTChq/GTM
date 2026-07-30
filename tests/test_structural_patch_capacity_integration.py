"""Phase 13 integration correction: the capacity controller must MATERIALLY
alter production execution (not just report) when enabled.

These tests exercise the real pre-contact wiring
(capacity_strategies.expand_precontact_capacity + build_precontact_strategy_runners),
mocking only the run_filter / run_precontact_qualification IO boundary so the
genuine strategy-runner reuse path executes. Includes a test that FAILS under
the v3 report-only behavior (where run_until_target was never invoked in
production and no pre-contact jobs were ever added).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import capacity_strategies
from capacity_strategies import expand_precontact_capacity, precontact_searchable_keys


def _job(company_domain, jid, employer="Acme Co"):
    return {
        "job_id": jid, "employer_name": employer, "employer_website": f"https://{company_domain}",
        "job_title": "Revenue Operations Analyst", "job_country": "US",
    }


def _write_jobs(tmp: Path, name, jobs):
    p = tmp / name
    p.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return str(p)


class _StrategyIOStub:
    """Stubs run_filter + run_precontact_qualification so each age window
    yields a scripted set of contact-eligible jobs (writing them to temp files,
    exactly as the real functions do)."""

    def __init__(self, tmp: Path, window_jobs):
        self.tmp = tmp
        self.window_jobs = window_jobs   # {suffix_substr: [jobs]}
        self.filter_calls = []
        self.qual_calls = []

    def run_filter(self, *, input_path, registry, max_age_days, min_age_days, output_suffix, allow_empty=False):
        self.filter_calls.append((min_age_days, max_age_days, output_suffix))
        jobs = self.window_jobs.get(output_suffix, [])
        path = _write_jobs(self.tmp, f"filtered_{output_suffix}.json", jobs)
        return SimpleNamespace(output_path=path, kept_count=len(jobs), rejected_count=0,
                               rejected_path="", success=True, errors=[], stats={})

    def run_precontact_qualification(self, input_path, suffix=""):
        self.qual_calls.append(suffix)
        jobs = json.loads(Path(input_path).read_text())["jobs"]
        path = _write_jobs(self.tmp, f"qualified_{suffix}.json", jobs)
        return SimpleNamespace(output_path=path, contact_eligible_jobs=len(jobs),
                               nonpass_path="", success=True, errors=[], stats={})


class CapacityIntegrationTests(unittest.TestCase):
    def _run(self, *, enabled, target, headroom, current_jobs, window_jobs, extended=False, deadline=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stub = _StrategyIOStub(Path(tmp.name), window_jobs)
        with (
            patch.object(config, "CAPACITY_CONTROLLER_ENABLED", enabled),
            patch.object(config, "SEARCHABLE_COMPANY_DAILY_TARGET", target),
            patch.object(config, "SEARCHABLE_COMPANY_HEADROOM_TARGET", headroom),
            patch.object(config, "EXTENDED_AGE_RECOVERY_ENABLED", extended),
            patch.object(capacity_strategies, "run_filter", stub.run_filter),
            patch.object(capacity_strategies, "run_precontact_qualification", stub.run_precontact_qualification),
        ):
            state, extra = expand_precontact_capacity(
                config_module=config, scrape_output_path="raw.json",
                registry=object(), current_jobs=current_jobs, runtime_deadline=deadline,
            )
        return state, extra, stub

    def test_disabled_is_strict_noop_no_strategy_calls(self):
        # This is the baseline; under v3 the ENABLED path behaved like this too.
        state, extra, stub = self._run(
            enabled=False, target=250, headroom=300,
            current_jobs=[_job("acme.com", "j1")], window_jobs={},
        )
        self.assertEqual(state["stop_reason"], "controller_disabled")
        self.assertEqual(extra, [])
        self.assertEqual(stub.filter_calls, [])   # no acquisition/recovery call

    def test_enabled_deficit_invokes_real_strategy_and_grows_pool(self):
        # THIS TEST FAILS UNDER v3: v3 never invoked run_until_target in
        # production and never added pre-contact jobs, so extra would be [].
        state, extra, stub = self._run(
            enabled=True, target=250, headroom=300,
            current_jobs=[_job("acme.com", "j1")],
            window_jobs={"capacity_age_recovery_15_30": [_job("beta.com", "j2"), _job("gamma.com", "j3")]},
        )
        self.assertTrue(stub.filter_calls)                       # real strategy invoked
        self.assertTrue(stub.qual_calls)                        # real qualification invoked
        self.assertEqual(len(extra), 2)                         # new jobs added to the pool
        self.assertEqual(state["searchable_companies_available"], 3)  # acme + beta + gamma

    def test_dry_first_strategy_then_next_executes(self):
        state, extra, stub = self._run(
            enabled=True, target=250, headroom=300, extended=True,
            current_jobs=[_job("acme.com", "j1")],
            window_jobs={
                "capacity_age_recovery_15_30": [],                                  # dry
                "capacity_age_recovery_31_60": [_job("beta.com", "j2")],            # yields
            },
        )
        # First window ran and was exhausted (0 new), the next window ran and added beta.
        suffixes = [c[2] for c in stub.filter_calls]
        self.assertIn("capacity_age_recovery_15_30", suffixes)
        self.assertIn("capacity_age_recovery_31_60", suffixes)
        self.assertEqual(len(extra), 1)
        self.assertIn("age_recovery_15_30", state["source_exhausted"])

    def test_target_headroom_reached_makes_no_further_source_request(self):
        # Already at headroom (3 companies, headroom 3): no strategy should run.
        state, extra, stub = self._run(
            enabled=True, target=2, headroom=3,
            current_jobs=[_job("a.com", "j1"), _job("b.com", "j2"), _job("c.com", "j3")],
            window_jobs={"capacity_age_recovery_15_30": [_job("d.com", "j4")]},
        )
        self.assertEqual(stub.filter_calls, [])          # no acquisition call at/above headroom
        self.assertEqual(extra, [])
        self.assertEqual(state["stop_reason"], "headroom_target_met")

    def test_duplicate_companies_across_windows_do_not_inflate_count(self):
        state, extra, stub = self._run(
            enabled=True, target=250, headroom=300, extended=True,
            current_jobs=[_job("acme.com", "j1")],
            window_jobs={
                "capacity_age_recovery_15_30": [_job("beta.com", "j2")],
                "capacity_age_recovery_31_60": [_job("beta.com", "j3")],   # same company again
            },
        )
        # beta counted once; second window adds no new company -> exhausted.
        self.assertEqual(state["searchable_companies_available"], 2)   # acme + beta
        self.assertIn("age_recovery_31_60", state["source_exhausted"])

    def test_runtime_guard_stops_with_exact_reason(self):
        import time
        past = time.monotonic() - 1  # deadline already passed
        state, extra, stub = self._run(
            enabled=True, target=250, headroom=300,
            current_jobs=[_job("acme.com", "j1")],
            window_jobs={"capacity_age_recovery_15_30": [_job("beta.com", "j2")]},
            deadline=past,
        )
        self.assertEqual(state["stop_reason"], "runtime_guard_reached")

    def test_only_safe_domain_companies_counted_as_searchable(self):
        # An aggregator/ATS host is not a safe domain -> not counted.
        jobs = [
            {"job_id": "a", "employer_name": "Acme", "employer_website": "https://acme.com"},
            {"job_id": "b", "employer_name": "X", "employer_website": "https://linkedin.com"},  # intermediary
            {"job_id": "c", "employer_name": "Y", "employer_website": ""},                       # no domain
        ]
        keys = precontact_searchable_keys(jobs, config.INTERMEDIARY_JOB_DOMAINS)
        self.assertEqual(len(keys), 1)


class RunDailyWiringTests(unittest.TestCase):
    def test_run_daily_imports_and_calls_expand_precontact_capacity(self):
        # Call-graph proof that the correction is wired into production, not
        # just defined. Under v3 run_daily never referenced this function.
        import run_daily
        import inspect
        src = inspect.getsource(run_daily.run_pipeline)
        self.assertIn("expand_precontact_capacity", src)
        self.assertIn("CAPACITY_CONTROLLER_ENABLED", src)

    def test_merge_helper_appends_and_dedupes(self):
        import run_daily
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "hiring.json"
            p.write_text(json.dumps({"jobs": [{"job_id": "j1"}]}))
            out = run_daily._merge_precontact_capacity_jobs(str(p), [{"job_id": "j2"}, {"job_id": "j1"}])
            merged = json.loads(Path(out).read_text())["jobs"]
            self.assertEqual({j["job_id"] for j in merged}, {"j1", "j2"})   # j1 not duplicated


if __name__ == "__main__":
    unittest.main()
