import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import job_filter
import jsearch_scraper
from pipeline_state import SeenJobsRegistry
from role_catalog import DEFAULT_SEARCH_ROLES
from role_mapping import get_bucket_name


class RemoteSearchRequestTests(unittest.TestCase):
    def test_fetch_requests_remote_inventory_and_biases_query(self):
        response = SimpleNamespace(
            headers={},
            json=lambda: {"status": "OK", "data": []},
        )
        with (
            patch.object(config, "RAPIDAPI_KEY", "test-key"),
            patch.object(config, "JSEARCH_REMOTE_JOBS_ONLY", True),
            patch.object(config, "JSEARCH_REMOTE_QUERY_BIAS", True),
            patch.object(jsearch_scraper, "request_with_retry", return_value=response) as request,
        ):
            jsearch_scraper.fetch_jobs_for_role("Accountant")

        params = request.call_args.kwargs["params"]
        self.assertEqual(params["remote_jobs_only"], "true")
        self.assertEqual(params["query"], "remote Accountant in United States")

    def test_remote_role_is_not_double_prefixed(self):
        with patch.object(config, "JSEARCH_REMOTE_QUERY_BIAS", True):
            self.assertEqual(
                jsearch_scraper.build_search_query("Remote QA Engineer"),
                "Remote QA Engineer in United States",
            )


class SharedPrefilterTests(unittest.TestCase):
    def test_remote_us_job_survives_shared_prefilter(self):
        job = {
            "job_id": "remote-1",
            "job_title": "Accountant",
            "job_description": "This is a fully remote role anywhere in the United States.",
            "job_location": "Remote",
            "job_country": "US",
            "job_is_remote": True,
            "employer_name": "Acme",
            "_matched_role": "Accountant",
        }
        assessment = job_filter.assess_pre_enrichment_viability(job)
        self.assertTrue(assessment.eligible)

    def test_onsite_job_is_not_used_as_adaptive_yield(self):
        job = {
            "job_id": "onsite-1",
            "job_title": "Accountant",
            "job_description": "Work from our Dallas office five days per week.",
            "job_location": "Dallas, TX",
            "job_country": "US",
            "job_is_remote": False,
            "employer_name": "Acme",
            "_matched_role": "Accountant",
        }
        assessment = job_filter.assess_pre_enrichment_viability(job)
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_in_person")


class AdaptiveRemoteYieldTests(unittest.TestCase):
    def _registry(self, directory: str) -> SeenJobsRegistry:
        return SeenJobsRegistry(path=str(Path(directory) / "seen.json"))

    def _run(self, roles, fake_fetch, directory, budget, max_extra=32):
        with (
            patch.object(config, "RAPIDAPI_KEY", "test-key"),
            patch.object(config, "ROLES", roles),
            patch.object(config, "OUTPUT_DIR", directory),
            patch.object(config, "NUM_PAGES", 1),
            patch.object(config, "JSEARCH_MAX_QUERIES_PER_RUN", 0),
            patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", budget),
            patch.object(config, "JSEARCH_ADAPTIVE_DEEPENING", True),
            patch.object(config, "JSEARCH_MAX_EXTRA_PAGES_PER_ROLE", 1),
            patch.object(config, "JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES", max_extra),
            patch.object(config, "JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE", 1),
            patch.object(config, "JSEARCH_ADAPTIVE_BUCKET_BALANCING", True),
            patch.object(config, "SEARCH_DELAY_SECONDS", 0),
            patch.object(config, "MIN_JOBS_PER_RUN", 0),
            patch.object(config, "MIN_ROLES_WITH_RESULTS", 0),
            patch.object(config, "PRODUCTION", False),
            patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
        ):
            return jsearch_scraper.run_daily_scrape(self._registry(directory))

    def test_deepening_uses_step2_viability_not_raw_title_volume(self):
        roles = list(DEFAULT_SEARCH_ROLES[:100])
        calls = []

        def fake_fetch(role: str, *, page: int = 1, num_pages=None):
            calls.append((role, page))
            if role == "Accountant":
                return [
                    {
                        "job_id": f"onsite-{page}-{index}",
                        "job_title": "Accountant",
                        "job_description": "Work onsite in our Dallas office.",
                        "job_location": "Dallas, TX",
                        "job_country": "US",
                        "job_is_remote": False,
                        "employer_name": f"Onsite {index}",
                    }
                    for index in range(10)
                ]
            if role == "Backend Developer":
                return [{
                    "job_id": f"remote-{page}",
                    "job_title": "Backend Developer",
                    "job_description": "Fully remote role anywhere in the United States.",
                    "job_location": "Remote",
                    "job_country": "US",
                    "job_is_remote": True,
                    "employer_name": "RemoteCo",
                }]
            return []

        with tempfile.TemporaryDirectory() as directory:
            result = self._run(roles, fake_fetch, directory, budget=101)

        self.assertEqual(result.stats["adaptive_extra_queries"], 1)
        self.assertEqual(
            [(role, page) for role, page in calls if page == 2],
            [("Backend Developer", 2)],
        )
        self.assertEqual(
            result.stats["query_metrics"]["Accountant"]["prefilter_viable_candidates"],
            0,
        )

    def test_explicit_extra_query_cap_is_respected(self):
        roles = list(DEFAULT_SEARCH_ROLES[:100])

        def fake_fetch(role: str, *, page: int = 1, num_pages=None):
            return [{
                "job_id": f"{role}-{page}",
                "job_title": role,
                "job_description": "Fully remote role anywhere in the United States.",
                "job_location": "Remote",
                "job_country": "US",
                "job_is_remote": True,
                "employer_name": f"Employer {role}",
            }]

        with tempfile.TemporaryDirectory() as directory:
            result = self._run(roles, fake_fetch, directory, budget=110, max_extra=3)

        self.assertEqual(result.stats["adaptive_extra_queries"], 3)
        self.assertEqual(result.stats["estimated_request_units"], 103)

    def test_bucket_balancing_round_robins_before_bucket_overflow(self):
        roles = [
            "Accountant",
            "Staff Accountant",
            "Backend Developer",
            "Content Marketing Specialist",
            "Operations Analyst",
        ]
        metrics = {
            role: {
                "canonical_role": role,
                "new_prefilter_viable_candidates": 2,
                "prefilter_viable_candidates": 2,
                "accepted_candidates": 2,
                "review_candidates": 0,
                "new_unique_candidates": 2,
                "raw_jobs": 2,
            }
            for role in roles
        }
        role_order = {role: index for index, role in enumerate(roles)}
        with patch.object(config, "JSEARCH_ADAPTIVE_BUCKET_BALANCING", True):
            ordered = jsearch_scraper._balanced_adaptive_role_order(
                roles, metrics, role_order
            )

        first_four_buckets = [get_bucket_name(role) for role in ordered[:4]]
        self.assertEqual(len(set(first_four_buckets)), 4)
        self.assertEqual(get_bucket_name(ordered[-1]), "finance")


if __name__ == "__main__":
    unittest.main()
