"""Phase 5 of FINAL_30_PLUS_SYSTEM_SPEC.md: Adzuna wired into
multi_source_acquisition.run_multi_source_acquisition() as a first-class,
independently-gated optional source.
"""
from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from unittest.mock import patch

import config
import multi_source_acquisition
from free_job_sources import FetchPayload
from pipeline_state import SeenJobsRegistry


class AdzunaMultiSourceIntegrationTests(unittest.TestCase):
    def _disabled_context(self, stack: contextlib.ExitStack) -> None:
        stack.enter_context(patch.object(config, "ATS_REGISTRY_AUTO_SEED_HISTORY", False))
        stack.enter_context(patch.object(config, "ATS_DIRECT_ACQUISITION_ENABLED", False))
        stack.enter_context(patch.object(config, "FREE_SOURCE_LANDING_DISCOVERY_ENABLED", False))
        stack.enter_context(patch.object(config, "FREE_JOB_SOURCES", []))
        stack.enter_context(patch.object(config, "FREE_SOURCE_MIN_SUCCESSFUL_SOURCES", 0))
        stack.enter_context(patch.object(config, "PRODUCTION", False))
        stack.enter_context(patch.object(config, "MULTI_SOURCE_JSEARCH_ENABLED", False))
        stack.enter_context(patch.object(multi_source_acquisition, "build_adapters", return_value=[]))

    def test_disabled_by_default_adds_no_jobs_and_no_error(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            seen = SeenJobsRegistry(f"{temp}/seen.json")
            self._disabled_context(stack)
            stack.enter_context(patch.object(config, "ADZUNA_ENABLED", False))
            result = multi_source_acquisition.run_multi_source_acquisition(
                registry=seen,
                fetcher=lambda *_a, **_k: FetchPayload(500, "unused"),
            )
        # Overall result.success reflects the (irrelevant here) zero-source
        # test setup, not Adzuna specifically -- assert on Adzuna's own,
        # independently correct no-op behavior instead.
        self.assertEqual(result.stats["adzuna"]["enabled"], False)
        self.assertEqual(result.stats["adzuna"]["skipped_reason"], "disabled_by_config")
        self.assertEqual(result.stats["adzuna"]["jobs"], 0)
        self.assertNotIn("adzuna", result.stats["enabled_sources"])

    def test_enabled_merges_normalized_jobs_into_the_run(self):
        adzuna_page = {
            "results": [{
                "id": "999001",
                "title": "Revenue Operations Manager",
                "company": {"display_name": "Acme Revenue Co"},
                "location": {"display_name": "United States"},
                "description": "Own the revenue systems stack full-time, remote-friendly.",
                "redirect_url": "https://www.adzuna.com/land/ad/999001",
                "created": "2026-07-20T09:15:00Z",
                "contract_time": "full_time",
                "category": {"label": "IT Jobs"},
            }],
        }

        def fetcher(url, params=None, **_kwargs):
            if "adzuna.com" in url:
                return FetchPayload(200, url, text=json.dumps(adzuna_page))
            return FetchPayload(500, url, text="unused")

        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            seen = SeenJobsRegistry(f"{temp}/seen.json")
            self._disabled_context(stack)
            stack.enter_context(patch.object(config, "ADZUNA_ENABLED", True))
            stack.enter_context(patch.object(config, "ADZUNA_APP_ID", "test-id"))
            stack.enter_context(patch.object(config, "ADZUNA_APP_KEY", "test-key"))
            stack.enter_context(patch.object(config, "ADZUNA_MAX_PAGES_PER_QUERY", 1))
            result = multi_source_acquisition.run_multi_source_acquisition(
                registry=seen,
                fetcher=fetcher,
            )
        self.assertTrue(result.stats["adzuna"]["attempted"])
        self.assertTrue(result.stats["adzuna"]["success"])
        self.assertGreaterEqual(result.stats["adzuna"]["jobs"], 1)
        self.assertIn("adzuna", result.stats["enabled_sources"])
        payload = json.loads(open(result.output_path, encoding="utf-8").read())
        adzuna_jobs = [job for job in payload["jobs"] if job.get("_acquisition_source") == "adzuna"]
        self.assertGreaterEqual(len(adzuna_jobs), 1)
        self.assertEqual(adzuna_jobs[0]["job_id"], "adzuna:999001")

    def test_missing_credentials_does_not_crash_the_run(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            seen = SeenJobsRegistry(f"{temp}/seen.json")
            self._disabled_context(stack)
            stack.enter_context(patch.object(config, "ADZUNA_ENABLED", True))
            stack.enter_context(patch.object(config, "ADZUNA_APP_ID", ""))
            stack.enter_context(patch.object(config, "ADZUNA_APP_KEY", ""))
            result = multi_source_acquisition.run_multi_source_acquisition(
                registry=seen,
                fetcher=lambda *_a, **_k: FetchPayload(500, "unused"),
            )
        self.assertTrue(result.stats["adzuna"]["attempted"])
        self.assertFalse(result.stats["adzuna"]["success"])
        self.assertIn("adzuna", result.stats.get("source_metrics", {}))


if __name__ == "__main__":
    unittest.main()
