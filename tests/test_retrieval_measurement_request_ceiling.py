"""The global outbound-request ceiling.

A ceiling that reports the overrun afterwards is not a ceiling. These tests pin
the one property that matters: the blocked request never reaches the network.

Counting happens at ``requests.request`` rather than at the measuring seam, and
that is deliberate. ``default_fetcher`` follows up to four redirects inside a
single seam call (``free_job_sources.py:121``) and ``request_with_retry`` retries
inside a single JSearch transport call (``http_utils.py:62``). Both are real
packets. Counting above them would undercount the wire by a factor of up to
four, which is exactly the kind of "close enough" that makes a budget useless.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import run_retrieval_measurement
from retrieval_measurement.artifacts import ARTIFACT_DIRNAME
from retrieval_measurement.instrument import (
    MeasuringFetcher,
    RequestBudget,
    RequestCeilingReached,
)
from retrieval_measurement.schema import TRUNCATION_KINDS

FIXTURE = Path(__file__).parent / "fixtures" / "retrieval_measurement" / "sources.json"


class Wire:
    """Stands in for the network. Records every call that actually reaches it."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs.get("url") or (args[1] if len(args) > 1 else ""))
        return mock.Mock(status_code=200, text="{}", url="https://example.test/x")


class CeilingTests(unittest.TestCase):
    def test_request_at_the_limit_executes(self):
        wire = Wire()
        budget = RequestBudget(1000)
        with mock.patch.object(requests, "request", wire):
            with budget.installed():
                for _ in range(1000):
                    requests.request("GET", "https://himalayas.app/jobs/api")
        self.assertEqual(budget.count, 1000)
        self.assertEqual(len(wire.calls), 1000, "request 1000 must be sent")
        self.assertFalse(budget.exhausted)

    def test_request_past_the_limit_never_reaches_the_transport(self):
        wire = Wire()
        budget = RequestBudget(1000)
        with mock.patch.object(requests, "request", wire):
            with budget.installed():
                for _ in range(1000):
                    requests.request("GET", "https://himalayas.app/jobs/api")
                with self.assertRaises(RequestCeilingReached):
                    requests.request("GET", "https://jsearch.p.rapidapi.com/search-v2")
        self.assertEqual(len(wire.calls), 1000, "request 1001 reached the network")
        self.assertEqual(budget.count, 1000, "a refused request must not be counted as spent")

    def test_the_blocked_request_is_recorded_with_lane_source_and_host_only(self):
        budget = RequestBudget(1)
        with mock.patch.object(requests, "request", Wire()):
            with budget.context(lane="ats", source="ats_workday"), budget.installed():
                requests.request("GET", "https://boards-api.greenhouse.io/v1/x")
                with self.assertRaises(RequestCeilingReached):
                    requests.request("GET", "https://api.smartrecruiters.com/v1/y?apiKey=zzz")
        blocked = budget.blocked_next_request
        self.assertEqual(budget.stop_reason, "request_ceiling_reached")
        self.assertEqual(blocked["sequence"], 2)
        self.assertEqual(blocked["lane"], "ats")
        self.assertEqual(blocked["source"], "ats_workday")
        self.assertEqual(blocked["hostname"], "api.smartrecruiters.com")
        self.assertNotIn("apiKey", json.dumps(blocked), "a query string reached the artifact")

    def test_the_count_is_global_across_lanes_and_fetcher_instances(self):
        wire = Wire()
        budget = RequestBudget(5)
        first = MeasuringFetcher(inner=lambda url, **kw: requests.request("GET", url))
        second = MeasuringFetcher(inner=lambda url, **kw: requests.request("GET", url))
        with mock.patch.object(requests, "request", wire):
            with budget.installed():
                with budget.context(lane="free_feeds"):
                    for _ in range(3):
                        first("https://himalayas.app/jobs/api")
                with budget.context(lane="ats"):
                    for _ in range(2):
                        second("https://api.ashbyhq.com/posting-api/job-board/x")
                    with self.assertRaises(RequestCeilingReached):
                        second("https://api.lever.co/v0/postings/y")
        self.assertEqual(budget.count, 5)
        self.assertEqual(len(wire.calls), 5)
        self.assertEqual(budget.blocked_next_request["lane"], "ats")

    def test_retries_below_the_seam_each_count_as_outbound_attempts(self):
        """One seam call, three physical attempts, three slots consumed."""
        wire = Wire()
        budget = RequestBudget(10)

        def retrying_transport(url, **_kw):
            for _attempt in range(3):
                requests.request("GET", url)
            return None

        fetcher = MeasuringFetcher(inner=retrying_transport)
        with mock.patch.object(requests, "request", wire):
            with budget.installed():
                fetcher("https://jsearch.p.rapidapi.com/search-v2")
        self.assertEqual(len(fetcher.requests), 1, "one seam call")
        self.assertEqual(budget.count, 3, "three physical attempts must each count")

    def test_redirects_below_the_seam_each_count_as_outbound_attempts(self):
        wire = Wire()
        budget = RequestBudget(10)

        def redirecting_transport(url, **_kw):
            for _hop in range(4):
                requests.request("GET", url)
            return None

        fetcher = MeasuringFetcher(inner=redirecting_transport)
        with mock.patch.object(requests, "request", wire):
            with budget.installed():
                fetcher("https://weworkremotely.com/remote-jobs.rss")
        self.assertEqual(budget.count, 4)

    def test_omitting_the_ceiling_leaves_requests_request_untouched(self):
        budget = RequestBudget(None)
        original = requests.request
        with budget.installed():
            self.assertIs(requests.request, original, "no ceiling must mean no patching")
            budget.reserve("https://himalayas.app")  # counts, never refuses
        self.assertIs(requests.request, original)
        self.assertIsNone(budget.limit)
        self.assertFalse(budget.exhausted)
        self.assertFalse(budget.to_dict()["enforced"])

    def test_the_original_transport_is_restored_even_when_the_ceiling_fires(self):
        budget = RequestBudget(0)
        original = requests.request
        with self.assertRaises(RequestCeilingReached):
            with budget.installed():
                requests.request("GET", "https://himalayas.app")
        self.assertIs(requests.request, original)

    def test_a_ceiling_stop_is_not_reachable_through_the_truncation_vocabulary(self):
        for forbidden in ("provider_exhaustion", "empty_page", "quota_guard", "error_stop"):
            self.assertIn(forbidden, TRUNCATION_KINDS)
        self.assertNotIn("request_ceiling_reached", TRUNCATION_KINDS)


class CeilingEndToEndTests(unittest.TestCase):
    """The ceiling firing mid-run must still leave a usable, honest artifact."""

    def _run_with_ceiling(self, tmp, limit):
        # Fixture mode drives the real dispatcher; the fixture transport is
        # routed through requests.request so the budget sees it.
        wire = Wire()
        with mock.patch.object(requests, "request", wire):
            with mock.patch.object(
                run_retrieval_measurement, "run_free_source_lane",
                side_effect=lambda _f, _s: (_ for _ in ()).throw(
                    RequestCeilingReached("global request ceiling of 0 reached")),
            ):
                return run_retrieval_measurement.main([
                    "--mode", "fixture", "--fixture", str(FIXTURE),
                    "--lanes", "free_feeds", "--max-requests", str(limit),
                    "--artifact-root", tmp, "--sources", "himalayas",
                ])

    def test_ceiling_exhaustion_exits_non_zero_and_persists_partial_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = self._run_with_ceiling(tmp, 0)
            self.assertNotEqual(code, 0)
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            for name in ("run_manifest.json", "coverage_summary.json", "coverage_summary.md",
                         "lane_failures.json", "run_status.json"):
                self.assertTrue((run / name).is_file(), name)
            failures = json.loads((run / "lane_failures.json").read_text(encoding="utf-8"))
            self.assertEqual(failures[0]["exception_type"], "RequestCeilingReached")
            self.assertEqual(failures[0]["stage"], "request_ceiling")

    def test_the_budget_is_recorded_in_the_manifest_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_retrieval_measurement.main([
                "--mode", "fixture", "--fixture", str(FIXTURE),
                "--lanes", "free_feeds", "--max-requests", "1000",
                "--artifact-root", tmp, "--sources", "himalayas",
            ])
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
            status = json.loads((run / "run_status.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["request_budget"]["limit"], 1000)
            self.assertTrue(manifest["request_budget"]["enforced"])
            self.assertEqual(manifest["request_budget"]["stop_reason"], "")
            self.assertEqual(status["request_budget"]["limit"], 1000)
            self.assertEqual(manifest["status"], "complete")

    def test_omitting_max_requests_preserves_prior_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = run_retrieval_measurement.main([
                "--mode", "fixture", "--fixture", str(FIXTURE),
                "--lanes", "free_feeds",
                "--artifact-root", tmp, "--sources", "himalayas",
            ])
            self.assertEqual(code, 0)
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertIsNone(manifest["request_budget"]["limit"])
            self.assertFalse(manifest["request_budget"]["enforced"])
            self.assertEqual(manifest["status"], "complete")

    def test_no_provider_request_occurs_before_preconditions_pass(self):
        """A preflight refusal must precede the budget ever being installed."""
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(requests, "request", wire):
                code = run_retrieval_measurement.main([
                    "--mode", "live_acquisition",
                    "--lanes", "free_feeds,ats",   # ats has no --boards
                    "--max-requests", "1000",
                    "--artifact-root", tmp,
                ])
        self.assertEqual(code, 2)
        self.assertEqual(wire.calls, [], "a request was made despite a failed precondition")


if __name__ == "__main__":
    unittest.main()
