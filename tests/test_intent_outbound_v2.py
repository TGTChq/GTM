from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import job_filter
import jsearch_scraper
import role_mapping
import run_filter_replay
import run_scrape_test
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
    def test_explicit_non_remote_job_is_accepted(self):
        matched, reason = job_filter.is_explicitly_in_person(
            {"job_title": "Data Analyst", "job_is_remote": False}
        )
        self.assertFalse(matched)
        self.assertEqual(reason, "")
        self.assertEqual(
            job_filter.classify_work_arrangement(
                {"job_title": "Data Analyst", "job_is_remote": False}
            ).status,
            "onsite",
        )

    def test_unknown_work_arrangement_is_left_for_review(self):
        self.assertEqual(
            job_filter.is_explicitly_in_person(
                {"job_title": "Data Analyst", "job_description": "Analyze data."}
            ),
            (False, ""),
        )

    def test_hybrid_job_is_accepted(self):
        job = {
            "job_title": "Marketing Analyst",
            "job_description": "This is a hybrid role with three days in office.",
        }
        self.assertEqual(job_filter.is_explicitly_in_person(job), (False, ""))
        self.assertEqual(job_filter.classify_work_arrangement(job).status, "hybrid")

    def test_unpaid_role_is_rejected(self):
        self.assertTrue(
            job_filter.is_non_paying_role(
                {
                    "job_title": "Content Writer",
                    "job_description": "This is an unpaid volunteer opportunity.",
                }
            )[0]
        )

    def test_remote_title_overrides_false_provider_flag(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "Remote Customer Success Manager",
                "job_location": "Chicago, IL",
                "job_is_remote": False,
            }
        )
        self.assertEqual(evidence.status, "remote")
        self.assertIn("remote_title", evidence.reason)

    def test_precise_remote_description_overrides_false_provider_flag(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "Customer Success Manager",
                "job_location": "United States",
                "job_is_remote": False,
                "job_description": "This is a fully remote position for US candidates.",
            }
        )
        self.assertEqual(evidence.status, "remote")
        self.assertIn("remote_description", evidence.reason)

    def test_hybrid_title_overrides_true_provider_flag(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "Remote/Hybrid Implementation Specialist",
                "job_is_remote": True,
            }
        )
        self.assertEqual(evidence.status, "hybrid")
        self.assertIn("title_or_location", evidence.reason)

    def test_onsite_word_in_channel_context_is_not_a_requirement(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "Copywriter",
                "job_is_remote": True,
                "job_description": "Write email, social media, and onsite platform copy.",
            }
        )
        self.assertEqual(evidence.status, "remote")

    def test_field_based_remote_listing_is_rejected(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "Implementation Specialist",
                "job_is_remote": True,
                "job_description": (
                    "This is a field-based position and not a traditional "
                    "work-from-home role. Travel approximately 50% to client sites."
                ),
            }
        )
        self.assertEqual(evidence.status, "physical_required")

    def test_hybrid_schedule_beats_work_from_home_phrase(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "Sales Development Representative",
                "job_is_remote": False,
                "job_description": (
                    "Hybrid schedule: work from home Mondays and Fridays, "
                    "in office Tuesday through Thursday."
                ),
            }
        )
        self.assertEqual(evidence.status, "hybrid")

    def test_onsite_day_shift_beats_work_from_home_benefit(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "Financial Analyst",
                "job_is_remote": False,
                "job_description": (
                    "This is an onsite, day-shift role based in one of our hubs. "
                    "There is some flexibility to work from home a few days per month."
                ),
            }
        )
        self.assertEqual(evidence.status, "onsite")

    def test_in_office_role_beats_hybrid_benefit_language(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "Marketing Operations Analyst",
                "job_is_remote": False,
                "job_description": (
                    "This is an in-office role. Eligible employees may later "
                    "participate in a hybrid work from home program."
                ),
            }
        )
        self.assertEqual(evidence.status, "onsite")

    def test_little_to_no_work_from_home_is_rejected(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "DevOps Engineer",
                "job_is_remote": False,
                "job_description": (
                    "The position allows little to no work from home and requires "
                    "all work to be performed in a SCIF."
                ),
            }
        )
        self.assertEqual(evidence.status, "physical_required")

    def test_hybrid_remote_location_is_rejected(self):
        evidence = job_filter.classify_work_arrangement(
            {
                "job_title": "Remote Bookkeeper",
                "job_is_remote": False,
                "job_description": "Work Location: Hybrid remote in Fresno, CA 93711",
            }
        )
        self.assertEqual(evidence.status, "hybrid")

    def test_foreign_only_eligibility_overrides_noisy_us_country(self):
        ok, reason = job_filter.is_us_job(
            {
                "job_title": "Remote Marketplace Specialist",
                "job_country": "US",
                "job_description": "This is a remote role for EU residents.",
            }
        )
        self.assertFalse(ok)
        self.assertIn("foreign_only_eligibility", reason)

    def test_multi_country_role_that_includes_us_is_not_foreign_only(self):
        ok, reason = job_filter.is_us_job(
            {
                "job_title": "Remote Tax Accountant",
                "job_country": "US",
                "job_description": "The offer is available from United States and Colombia.",
            }
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "country_field")

    def test_remote_role_based_with_philippines_team_is_foreign_only(self):
        ok, reason = job_filter.is_us_job(
            {
                "job_title": "Remote PPC Specialist",
                "job_country": "US",
                "job_description": (
                    "We are hiring for a fully remote role based with teams in the Philippines."
                ),
            }
        )
        self.assertFalse(ok)
        self.assertIn("foreign_only_eligibility", reason)

    def test_manufacturing_automation_leak_is_rejected(self):
        matched, reason = job_filter.is_obvious_role_mismatch(
            {
                "_matched_role": "AI Automation Engineer",
                "job_title": "AI-Driven Manufacturing Automation Engineer",
                "job_description": "Build manufacturing automation systems.",
            }
        )
        self.assertTrue(matched)
        self.assertIn("obvious_role_mismatch", reason)

    def test_healthcare_brand_is_excluded(self):
        matched, reason = job_filter.is_excluded_industry(
            {"employer_name": "GE HealthCare"}
        )
        self.assertTrue(matched)
        self.assertIn("healthcare", reason.lower())

    def test_first_party_nonprofit_description_is_excluded(self):
        matched, reason = job_filter.is_excluded_industry(
            {
                "employer_name": "Arts Alliance",
                "job_description": "We are a nonprofit organization serving artists.",
            }
        )
        self.assertTrue(matched)
        self.assertTrue(reason.startswith("excluded_industry_"))

    def test_observed_intermediary_is_excluded_without_substring_collision(self):
        self.assertTrue(job_filter.is_staffing_company({"employer_name": "Paired"})[0])
        self.assertTrue(job_filter.is_staffing_company({"employer_name": "Venraro"})[0])
        self.assertFalse(job_filter.is_staffing_company({"employer_name": "Impaired Labs"})[0])


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


    def test_fetch_result_captures_rapidapi_quota_headers(self):
        response = SimpleNamespace(
            headers={
                "x-ratelimit-requests-limit": "10000",
                "x-ratelimit-requests-remaining": "9876",
                "x-ratelimit-requests-reset": "1200",
            },
            json=lambda: {"status": "OK", "data": []},
        )
        with (
            patch.object(config, "RAPIDAPI_KEY", "test-key"),
            patch.object(jsearch_scraper, "request_with_retry", return_value=response),
        ):
            result = jsearch_scraper.fetch_jobs_for_role("Tax Accountant")

        self.assertEqual(result.jobs, [])
        self.assertEqual(result.quota["limit"], 10000)
        self.assertEqual(result.quota["remaining"], 9876)
        self.assertEqual(result.quota["reset"], 1200)

    def test_max_queries_is_an_optional_runtime_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def fake_fetch(role: str):
                return [{
                    "job_id": f"job-{role}",
                    "job_title": role,
                    "job_description": f"Work as a {role}.",
                }]

            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "ROLES", ["Backend Developer", "Tax Accountant", "Copywriter"]),
                patch.object(config, "OUTPUT_DIR", temp_dir),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "MIN_JOBS_PER_RUN", 1),
                patch.object(config, "MIN_ROLES_WITH_RESULTS", 1),
                patch.object(config, "MAX_ROLE_FAILURES", 0),
                patch.object(config, "MAX_ROLE_FAILURE_RATE", 0),
                patch.object(config, "PRODUCTION", True),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
            ):
                result = jsearch_scraper.run_daily_scrape(
                    self._registry(temp_dir), max_queries=2
                )

            self.assertTrue(result.success)
            self.assertEqual(result.stats["queries_planned"], 3)
            self.assertEqual(result.stats["queries_attempted"], 2)
            self.assertTrue(result.stats["query_plan_truncated"])
            self.assertEqual(set(result.stats["raw_role_counts"]), {"Backend Developer", "Tax Accountant"})

    def test_failure_allowance_scales_for_large_catalogs(self):
        with (
            patch.object(config, "MAX_ROLE_FAILURES", 3),
            patch.object(config, "MAX_ROLE_FAILURE_RATE", 0.10),
        ):
            self.assertEqual(jsearch_scraper._allowed_role_failures(8), 3)
            self.assertEqual(jsearch_scraper._allowed_role_failures(118), 12)

    def test_daily_catalog_one_page_fits_default_unit_budget(self):
        with (
            patch.object(config, "NUM_PAGES", 1),
            patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 150),
        ):
            self.assertEqual(jsearch_scraper.validate_query_budget(118), 118)

    def test_three_page_full_catalog_is_blocked_before_network_calls(self):
        with (
            patch.object(config, "NUM_PAGES", 3),
            patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 150),
        ):
            with self.assertRaisesRegex(ValueError, "354 units"):
                jsearch_scraper.validate_query_budget(118)


class FilterReplayTests(unittest.TestCase):
    def test_summary_counts_recovered_remote_flag_conflict(self):
        summary = run_filter_replay._summarize(
            [
                {
                    "_matched_role": "Customer Success Manager",
                    "_work_arrangement": "remote",
                    "job_is_remote": False,
                }
            ],
            [
                {
                    "_matched_role": "Customer Success Manager",
                    "_work_arrangement": "in_person",
                    "_filter_reason": "jsearch_remote_false_without_remote_evidence",
                }
            ],
        )
        self.assertEqual(summary["remote_flag_conflicts_kept"], 1)
        self.assertEqual(summary["roles"][0]["kept"], 1)
        self.assertEqual(summary["roles"][0]["rejected"], 1)


class ScrapeTestHarnessTests(unittest.TestCase):
    def test_explicit_positive_query_cap_is_diagnostic_for_single_role_catalog(self):
        args = SimpleNamespace(max_queries=1, roles=None)
        stats = {"query_plan_truncated": False}
        self.assertTrue(run_scrape_test._is_diagnostic_scope(args, stats))

    def test_explicit_zero_query_cap_keeps_production_scope(self):
        args = SimpleNamespace(max_queries=0, roles=None)
        stats = {"query_plan_truncated": False}
        self.assertFalse(run_scrape_test._is_diagnostic_scope(args, stats))

    def test_limited_zero_yield_is_api_smoke_success(self):
        report = {
            "queries_succeeded": 1,
            "queries_failed": 0,
            "selected_jobs": 0,
            "scrape_health_errors": ["Only 0 role-relevant jobs scraped"],
        }
        api_requests_ok = (
            report["queries_succeeded"] > 0 and report["queries_failed"] == 0
        )
        diagnostic_scope = True
        exit_code = 0 if diagnostic_scope and api_requests_ok else 1
        self.assertEqual(exit_code, 0)

    def test_full_catalog_zero_yield_remains_failure(self):
        report = {
            "queries_succeeded": 118,
            "queries_failed": 0,
            "selected_jobs": 0,
            "scrape_health_errors": ["Only 0 role-relevant jobs scraped"],
        }
        api_requests_ok = (
            report["queries_succeeded"] > 0 and report["queries_failed"] == 0
        )
        production_catalog_health_ok = bool(
            api_requests_ok
            and report["selected_jobs"] > 0
            and not report["scrape_health_errors"]
        )
        self.assertFalse(production_catalog_health_ok)

    def test_env_query_cap_is_honored_when_runtime_override_is_omitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def fake_fetch(role: str):
                return [{
                    "job_id": f"job-{role}",
                    "job_title": role,
                    "job_description": f"Work as a {role}.",
                }]

            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "ROLES", ["Backend Developer", "Tax Accountant"]),
                patch.object(config, "JSEARCH_MAX_QUERIES_PER_RUN", 1),
                patch.object(config, "OUTPUT_DIR", temp_dir),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "MIN_JOBS_PER_RUN", 1),
                patch.object(config, "MIN_ROLES_WITH_RESULTS", 1),
                patch.object(config, "MAX_ROLE_FAILURES", 0),
                patch.object(config, "MAX_ROLE_FAILURE_RATE", 0),
                patch.object(config, "PRODUCTION", True),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
            ):
                result = jsearch_scraper.run_daily_scrape(
                    SeenJobsRegistry(path=str(Path(temp_dir) / "seen_jobs.json")),
                    max_queries=None,
                )

            self.assertEqual(result.stats["queries_attempted"], 1)
            self.assertTrue(result.stats["query_plan_truncated"])


if __name__ == "__main__":
    unittest.main()
