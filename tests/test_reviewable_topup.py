import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import hiring_manager
import jsearch_scraper
import reviewable_topup
from hiring_manager import Step3Result
from jsearch_scraper import ScrapeResult
from pipeline_state import SeenJobsRegistry


class InitialBudgetReservationTests(unittest.TestCase):
    def test_initial_topup_mode_uses_one_page_and_disables_blind_adaptive_calls(self):
        calls = []

        def fake_fetch(role, **kwargs):
            calls.append((role, kwargs))
            return [{
                "job_id": f"job-{role}",
                "job_title": role,
                "job_description": "Full-time remote role in the United States.",
                "job_location": "United States",
                "job_country": "US",
                "job_is_remote": True,
                "job_employment_type": "Full-time",
                "employer_name": f"Company {role}",
                "employer_website": f"https://{role.lower().replace(' ', '')}.com",
            }]

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "ROLES", ["Accountant", "Backend Developer"]),
                patch.object(config, "OUTPUT_DIR", directory),
                patch.object(config, "NUM_PAGES", 3),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 370),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "MIN_JOBS_PER_RUN", 0),
                patch.object(config, "MIN_ROLES_WITH_RESULTS", 0),
                patch.object(config, "PRODUCTION", False),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
            ):
                result = jsearch_scraper.run_daily_scrape(
                    registry=SeenJobsRegistry(path=str(Path(directory) / "seen.json")),
                    base_num_pages=1,
                    allow_adaptive=False,
                )

        self.assertEqual(result.stats["base_estimated_request_units"], 2)
        self.assertEqual(result.stats["estimated_request_units"], 2)
        self.assertFalse(result.stats["adaptive_deepening_enabled"])
        self.assertFalse(result.stats["adaptive_lookback_enabled"])
        self.assertEqual([kwargs["num_pages"] for _role, kwargs in calls], [1, 1])


class TargetedTopupScrapeTests(unittest.TestCase):
    def test_topup_uses_next_unused_page_window_and_excludes_initial_job_ids(self):
        calls = []

        def fake_fetch(role, **kwargs):
            calls.append((role, kwargs))
            return [
                {
                    "job_id": "already-seen",
                    "job_title": "Accountant",
                    "job_description": "Full-time remote role in the United States.",
                    "job_location": "United States",
                    "job_country": "US",
                    "job_is_remote": True,
                    "job_employment_type": "Full-time",
                    "employer_name": "Old Co",
                    "employer_website": "https://oldco.com",
                },
                {
                    "job_id": "new-job",
                    "job_title": "Accountant",
                    "job_description": "Full-time remote role in the United States.",
                    "job_location": "United States",
                    "job_country": "US",
                    "job_is_remote": True,
                    "job_employment_type": "Full-time",
                    "employer_name": "New Co",
                    "employer_website": "https://newco.com",
                },
            ]

        prior = {
            "Accountant": {
                "canonical_role": "Accountant",
                "raw_jobs": 10,
                "new_prefilter_viable_candidates": 2,
                "prefilter_viable_candidates": 2,
                "pages": [{
                    "page": 1,
                    "num_pages": 1,
                    "last_page": 1,
                    "query_variant": "base",
                }],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "ROLES", ["Accountant"]),
                patch.object(config, "OUTPUT_DIR", directory),
                patch.object(config, "JSEARCH_TOPUP_PAGES_PER_QUERY", 3),
                patch.object(config, "JSEARCH_TOPUP_MAX_PAGE", 10),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "PRODUCTION", False),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
            ):
                result = jsearch_scraper.run_targeted_topup_scrape(
                    registry=SeenJobsRegistry(path=str(Path(directory) / "seen.json")),
                    prior_query_metrics=prior,
                    exclude_job_ids={"already-seen"},
                    unit_budget=3,
                    target_prefilter_viable=10,
                    round_number=1,
                )

        self.assertEqual(calls[0][1]["page"], 2)
        self.assertEqual(calls[0][1]["num_pages"], 3)
        self.assertEqual(result.stats["estimated_request_units"], 3)
        self.assertEqual(result.stats["in_run_existing_job_ids_removed"], 1)
        self.assertEqual(result.total_jobs, 1)
        self.assertEqual(result.stats["topup_new_prefilter_viable"], 1)

    def test_query_planner_never_repeats_used_page_ranges(self):
        metric = {
            "pages": [
                {"page": 1, "num_pages": 1, "last_page": 1, "query_variant": "base"},
                {"page": 2, "num_pages": 3, "last_page": 4, "query_variant": "base", "mode": "topup_deep_page"},
            ]
        }
        with (
            patch.object(config, "JSEARCH_TOPUP_PAGES_PER_QUERY", 3),
            patch.object(config, "JSEARCH_TOPUP_MAX_PAGE", 10),
        ):
            spec = jsearch_scraper._next_topup_query_spec(
                metric, unit_budget_remaining=3
            )
        self.assertEqual(spec["page"], 5)
        self.assertEqual(spec["last_page"], 7)

    def test_query_planner_switches_to_diversified_lookback_after_base_pages(self):
        metric = {
            "pages": [
                {"page": 1, "num_pages": 1, "last_page": 1, "query_variant": "base"},
                {"page": 2, "num_pages": 3, "last_page": 4, "query_variant": "base", "mode": "topup_deep_page"},
            ]
        }
        with (
            patch.object(config, "JSEARCH_TOPUP_PAGES_PER_QUERY", 3),
            patch.object(config, "JSEARCH_TOPUP_MAX_PAGE", 4),
            patch.object(config, "JSEARCH_LOOKBACK_QUERY_VARIANTS", ["linkedin", "indeed"]),
        ):
            spec = jsearch_scraper._next_topup_query_spec(
                metric, unit_budget_remaining=3
            )
        self.assertEqual(spec["mode"], "topup_lookback")
        self.assertEqual(spec["query_variant"], "linkedin")
        self.assertEqual(spec["page"], 1)
        self.assertEqual(spec["last_page"], 3)


class HiringManagerTopupExclusionTests(unittest.TestCase):
    def test_previously_considered_company_is_skipped_before_apollo(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "filtered.json"
            input_path.write_text(json.dumps({"jobs": [
                {
                    "job_id": "a",
                    "job_title": "Accountant",
                    "employer_name": "Acme",
                    "employer_website": "https://acme.com",
                },
                {
                    "job_id": "b",
                    "job_title": "Accountant",
                    "employer_name": "Beta",
                    "employer_website": "https://beta.com",
                },
            ]}))

            def fake_process(company_jobs):
                job = company_jobs[0]
                return [{
                    **job,
                    "_role_bucket": "finance",
                    "_step3_status": "found",
                    "_step3_reason": "contact_found",
                    "hiring_manager_name": "Jane Doe",
                    "hiring_manager_email": "jane@beta.com",
                    "hiring_manager_confidence": "medium",
                    "lead_key": "beta.com|jane@beta.com|finance",
                }], {}

            with (
                patch.object(config, "STEP3_OUTPUT_DIR", directory),
                patch.object(hiring_manager, "validate_preflight"),
                patch.object(hiring_manager, "process_company", side_effect=fake_process),
            ):
                result = hiring_manager.run_hiring_manager_identification(
                    str(input_path),
                    exclude_company_keys={"acme.com"},
                    output_suffix="topup_r1",
                )

        self.assertEqual(result.companies_considered, 1)
        self.assertEqual(result.processed_company_keys, ["beta.com"])
        self.assertEqual(
            result.stats["topup_skipped_previously_considered_companies"], 1
        )
        self.assertIn("topup_r1", result.output_path)


class ClosedLoopOrchestrationTests(unittest.TestCase):
    def _step3_result(self, output_path, *, reviewable, company_key, job_count=1):
        return Step3Result(
            output_path=str(output_path),
            total_input_jobs=job_count,
            total_output_leads=1,
            company_criteria_excluded=0,
            hiring_manager_found=1,
            hiring_manager_not_found=0,
            match_rate=1.0,
            contactable_hiring_managers=1,
            uncontactable_hiring_managers=0,
            contactable_rate=1.0,
            companies_considered=1,
            eligible_companies=1,
            target_reviewable_leads=reviewable,
            reviewable_leads=1,
            reviewable_target_reached=True,
            max_eligible_companies=90,
            stop_reason="reviewable_lead_target_reached",
            processed_company_keys=[company_key],
            stats={},
        )

    def test_one_topup_round_stops_immediately_when_cumulative_target_is_reached(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            raw_initial = directory / "initial_raw.json"
            raw_initial.write_text(json.dumps({"jobs": [{"job_id": "job-1"}]}))
            initial_enriched_path = directory / "initial_enriched.json"
            initial_enriched_path.write_text(json.dumps({
                "jobs": [{
                    "job_id": "job-1",
                    "employer_name": "Acme",
                    "employer_website": "https://acme.com",
                    "_search_role": "Accountant",
                    "_role_bucket": "finance",
                    "_step3_status": "found",
                    "hiring_manager_name": "A One",
                    "hiring_manager_email": "a@acme.com",
                    "lead_key": "acme.com|a@acme.com|finance",
                }],
                "processed_job_refs": [{"job_id": "job-1", "employer_name": "Acme", "job_title": "Accountant"}],
                "processed_company_keys": ["acme.com"],
            }))
            initial_scrape = ScrapeResult(
                output_path=str(raw_initial),
                total_jobs=1,
                stats={
                    "estimated_request_units": 118,
                    "query_metrics": {"Accountant": {
                        "canonical_role": "Accountant",
                        "raw_jobs": 10,
                        "new_prefilter_viable_candidates": 1,
                        "pages": [{"page": 1, "num_pages": 1, "last_page": 1, "query_variant": "base"}],
                    }},
                },
            )
            initial_result = self._step3_result(
                initial_enriched_path, reviewable=2, company_key="acme.com"
            )

            topup_raw = directory / "topup_raw.json"
            topup_raw.write_text(json.dumps({"jobs": [{"job_id": "job-2"}]}))
            topup_scrape = ScrapeResult(
                output_path=str(topup_raw),
                total_jobs=1,
                stats={
                    "estimated_request_units": 3,
                    "queries_attempted": 1,
                    "queries_succeeded": 1,
                    "topup_new_prefilter_viable": 1,
                    "topup_stop_reason": "topup_role_plan_exhausted",
                    "query_metrics": {},
                },
            )
            filtered_path = directory / "filtered_topup.json"
            filtered_path.write_text(json.dumps({"jobs": [{"job_id": "job-2"}]}))
            topup_enriched_path = directory / "topup_enriched.json"
            topup_enriched_path.write_text(json.dumps({
                "jobs": [{
                    "job_id": "job-2",
                    "employer_name": "Beta",
                    "employer_website": "https://beta.com",
                    "_search_role": "Accountant",
                    "_role_bucket": "finance",
                    "_step3_status": "found",
                    "hiring_manager_name": "B Two",
                    "hiring_manager_email": "b@beta.com",
                    "lead_key": "beta.com|b@beta.com|finance",
                }],
                "processed_job_refs": [{"job_id": "job-2", "employer_name": "Beta", "job_title": "Accountant"}],
                "processed_company_keys": ["beta.com"],
            }))
            topup_result = self._step3_result(
                topup_enriched_path, reviewable=1, company_key="beta.com"
            )

            with (
                patch.object(config, "STEP3_OUTPUT_DIR", str(directory)),
                patch.object(config, "FILTERED_OUTPUT_DIR", str(directory)),
                patch.object(config, "JSEARCH_TOPUP_MAX_ROUNDS", 3),
                patch.object(config, "JSEARCH_TOPUP_MAX_UNITS_PER_ROUND", 84),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 370),
                patch.object(reviewable_topup, "run_targeted_topup_scrape", return_value=topup_scrape) as scrape_mock,
                patch.object(reviewable_topup, "run_filter", return_value=SimpleNamespace(
                    kept_count=1,
                    rejected_count=0,
                    output_path=str(filtered_path),
                    rejected_path="",
                    success=True,
                    errors=[],
                )),
                patch.object(reviewable_topup, "run_hiring_manager_identification", return_value=topup_result),
            ):
                combined, details = reviewable_topup.run_reviewable_topup(
                    initial_scrape=initial_scrape,
                    initial_enriched=initial_result,
                    registry=SeenJobsRegistry(path=str(directory / "seen.json")),
                    target_reviewable_leads=2,
                    max_eligible_companies=90,
                )

        self.assertEqual(scrape_mock.call_count, 1)
        self.assertEqual(combined.reviewable_leads, 2)
        self.assertTrue(combined.reviewable_target_reached)
        self.assertEqual(details["stop_reason"], "reviewable_lead_target_reached")
        self.assertEqual(len(details["rounds"]), 1)
        self.assertEqual(details["rounds"][0]["reviewable_added"], 1)


if __name__ == "__main__":
    unittest.main()
