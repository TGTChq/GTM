from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import job_filter
import jsearch_scraper
import role_mapping
from pipeline_state import SeenJobsRegistry
from role_catalog import (
    DEFAULT_SEARCH_ROLES,
    REMOVED_ROLE_TITLES,
    ROLE_DEFINITIONS,
    get_fallback_focus,
)
from role_focus import extract_role_focus
from role_relevance import assess_role


class RoleCatalogV2Tests(unittest.TestCase):
    def test_catalog_is_full_scope_and_has_no_removed_titles(self):
        self.assertGreaterEqual(len(DEFAULT_SEARCH_ROLES), 100)
        for removed in REMOVED_ROLE_TITLES:
            self.assertNotIn(removed, DEFAULT_SEARCH_ROLES)
        self.assertEqual(DEFAULT_SEARCH_ROLES.count("Graphic Designer"), 1)

    def test_every_role_has_routing_and_safe_focus_fallback(self):
        for role, definition in ROLE_DEFINITIONS.items():
            with self.subTest(role=role):
                self.assertTrue(definition.function_bucket)
                self.assertTrue(definition.hiring_manager_bucket)
                self.assertTrue(get_fallback_focus(role))
                self.assertTrue(role_mapping.get_target_titles(role))
                result = extract_role_focus(
                    {"job_title": role, "job_description": "General responsibilities."},
                    role,
                )
                self.assertTrue(result.text)

    def test_new_catalog_role_gets_deterministic_relevance(self):
        assessment = assess_role(
            {
                "job_title": "Tax Accountant",
                "job_description": "Own tax compliance and financial reporting.",
            },
            "Tax Accountant",
        )
        self.assertEqual(assessment.status, "accept")
        self.assertEqual(assessment.score, 8)

    def test_ambiguous_community_manager_property_role_is_rejected(self):
        assessment = assess_role(
            {
                "job_title": "Community Manager",
                "job_description": "Manage an HOA residential property community.",
            },
            "Community Manager",
        )
        self.assertEqual(assessment.status, "reject")

    def test_specialized_hiring_manager_routing(self):
        self.assertEqual(role_mapping.get_bucket_name("Data Analyst"), "engineering")
        self.assertEqual(
            role_mapping.get_hiring_manager_bucket_name("Data Analyst"), "data"
        )
        self.assertIn("Chief Data Officer", role_mapping.get_target_titles("Data Analyst"))
        self.assertIn("CFO", role_mapping.get_target_titles("Tax Accountant"))
        self.assertIn(
            "Chief People Officer",
            role_mapping.get_target_titles("People Operations Specialist"),
        )


class IntentV2FilterTests(unittest.TestCase):
    def test_explicit_non_remote_job_is_rejected(self):
        matched, reason = job_filter.is_explicitly_in_person(
            {"job_title": "Data Analyst", "job_is_remote": False}
        )
        self.assertTrue(matched)
        self.assertIn("remote_false", reason)

    def test_unknown_work_arrangement_is_left_for_review(self):
        self.assertEqual(
            job_filter.is_explicitly_in_person(
                {"job_title": "Data Analyst", "job_description": "Analyze data."}
            ),
            (False, ""),
        )

    def test_hybrid_job_is_rejected(self):
        self.assertTrue(
            job_filter.is_explicitly_in_person(
                {
                    "job_title": "Marketing Analyst",
                    "job_description": "This is a hybrid role with three days in office.",
                }
            )[0]
        )

    def test_unpaid_role_is_rejected(self):
        self.assertTrue(
            job_filter.is_non_paying_role(
                {
                    "job_title": "Content Writer",
                    "job_description": "This is an unpaid volunteer opportunity.",
                }
            )[0]
        )


class ScraperV2Tests(unittest.TestCase):
    def _registry(self, temp_dir: str) -> SeenJobsRegistry:
        return SeenJobsRegistry(path=str(Path(temp_dir) / "seen_jobs.json"))

    def test_zero_results_are_not_counted_as_api_failures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job = {
                "job_id": "job-1",
                "job_title": "Backend Developer",
                "job_description": "Build backend APIs.",
            }

            def fake_fetch(role: str):
                return [job] if role == "Backend Developer" else []

            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "ROLES", ["Backend Developer", "Tax Accountant"]),
                patch.object(config, "OUTPUT_DIR", temp_dir),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "MIN_JOBS_PER_RUN", 1),
                patch.object(config, "MIN_ROLES_WITH_RESULTS", 1),
                patch.object(config, "MAX_ROLE_FAILURES", 0),
                patch.object(config, "PRODUCTION", True),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
            ):
                result = jsearch_scraper.run_daily_scrape(self._registry(temp_dir))

            self.assertTrue(result.success)
            self.assertEqual(result.failed_roles, [])
            self.assertEqual(result.stats["zero_result_roles"], ["Tax Accountant"])

    def test_more_specific_role_wins_duplicate_query_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job = {
                "job_id": "job-2",
                "job_title": "Tax Accountant",
                "job_description": "Own tax compliance and accounting.",
            }
            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "ROLES", ["Accountant", "Tax Accountant"]),
                patch.object(config, "OUTPUT_DIR", temp_dir),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "MIN_JOBS_PER_RUN", 1),
                patch.object(config, "MIN_ROLES_WITH_RESULTS", 1),
                patch.object(config, "MAX_ROLE_FAILURES", 0),
                patch.object(config, "PRODUCTION", True),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", return_value=[job]),
            ):
                result = jsearch_scraper.run_daily_scrape(self._registry(temp_dir))

            payload = Path(result.output_path).read_text(encoding="utf-8")
            self.assertIn('"_matched_role": "Tax Accountant"', payload)


if __name__ == "__main__":
    unittest.main()
