from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import final_pass_topup
import multi_source_acquisition
import validate_setup
from ats_board_registry import AtsBoardRegistry, detect_board_ref, fetch_board_jobs
from free_job_sources import (
    FetchPayload,
    default_fetcher,
    HimalayasAdapter,
    JobicyAdapter,
    RemoteOkAdapter,
    RemotiveAdapter,
    SourceResult,
    WeWorkRemotelyAdapter,
)
from hiring_manager import Step3Result
from jsearch_scraper import ScrapeResult
from pipeline_state import SeenJobsRegistry


class FreeSourceAdapterTests(unittest.TestCase):
    def test_default_fetcher_blocks_private_network_targets(self):
        result = default_fetcher("http://127.0.0.1:8080/internal")
        self.assertIsNone(result.status_code)
        self.assertEqual(result.error, "unsafe_or_unresolvable_url")

    def test_himalayas_paginates_and_normalizes_us_job(self):
        pages = {
            0: {
                "totalCount": 2,
                "jobs": [{
                    "guid": "h1",
                    "title": "Customer Success Manager",
                    "companyName": "Acme",
                    "companySlug": "acme",
                    "description": "<p>Own onboarding, retention, and renewals.</p>",
                    "applicationLink": "https://himalayas.app/companies/acme/jobs/csm",
                    "locationRestrictions": [{"alpha2": "US", "name": "United States"}],
                    "employmentType": "Full Time",
                    "pubDate": 1784764800000,
                    "expiryDate": 1787356800000,
                    "categories": ["Customer Success"],
                    "parentCategories": ["Operations"],
                }],
            },
            1: {
                "totalCount": 2,
                "jobs": [{
                    "guid": "h2",
                    "title": "Staff Accountant",
                    "companyName": "Ledger Labs",
                    "description": "Close books and reconcile accounts.",
                    "applicationLink": "https://himalayas.app/companies/ledger/jobs/accountant",
                    "locationRestrictions": [],
                    "employmentType": "Full Time",
                }],
            },
        }

        def fetcher(_url, *, params=None, **_kwargs):
            offset = int((params or {}).get("offset", 0))
            return FetchPayload(200, _url, json.dumps(pages[offset]))

        with (
            patch.object(config, "HIMALAYAS_PAGE_SIZE", 1),
            patch.object(config, "HIMALAYAS_MAX_PAGES", 3),
            patch.object(config, "FREE_SOURCE_MAX_RECORDS_PER_SOURCE", 10),
        ):
            result = HimalayasAdapter().fetch(fetcher)

        self.assertTrue(result.success)
        self.assertEqual(result.requests_attempted, 2)
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.jobs[0]["job_country"], "US")
        self.assertEqual(result.jobs[0]["job_employment_type"], "Full Time")
        self.assertIn("onboarding", result.jobs[0]["job_description"])
        self.assertEqual(result.jobs[1]["job_location"], "Remote - Worldwide")

    def test_jobicy_normalizes_us_job(self):
        payload = {
            "jobs": [{
                "id": 99,
                "jobSlug": "customer-success-manager",
                "jobTitle": "Customer Success Manager",
                "companyName": "Customer Co",
                "jobDescription": "<p>Own onboarding, retention, renewals, and expansion.</p>",
                "url": "https://jobicy.com/jobs/99-customer-success-manager",
                "jobGeo": "USA",
                "jobType": ["full-time"],
                "pubDate": "2026-07-23T10:00:00+00:00",
                "jobIndustry": ["SaaS"],
                "jobLevel": "mid",
            }]
        }
        result = JobicyAdapter().fetch(
            lambda *_args, **_kwargs: FetchPayload(200, "https://jobicy.com/api/v2/remote-jobs", json.dumps(payload))
        )
        self.assertTrue(result.success)
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0]["job_country"], "US")
        self.assertEqual(result.jobs[0]["job_employment_type"], "Full Time")
        self.assertEqual(result.jobs[0]["employer_name"], "Customer Co")

    def test_remotive_invalid_payload_fails_closed(self):
        result = RemotiveAdapter().fetch(
            lambda *_args, **_kwargs: FetchPayload(200, "https://remotive.com", "[]")
        )
        self.assertFalse(result.success)
        self.assertEqual(result.errors, ["invalid_json_object"])

    def test_remoteok_skips_legal_header_record(self):
        payload = [
            {"legal": "Please attribute Remote OK"},
            {
                "id": "r1",
                "position": "AI Engineer",
                "company": "Model Co",
                "description": "Build production LLM systems.",
                "url": "https://remoteok.com/remote-jobs/r1",
                "location": "United States",
                "tags": ["full time", "ai"],
            },
        ]
        result = RemoteOkAdapter().fetch(
            lambda *_args, **_kwargs: FetchPayload(200, "https://remoteok.com/api", json.dumps(payload))
        )
        self.assertTrue(result.success)
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0]["employer_name"], "Model Co")
        self.assertEqual(result.jobs[0]["job_country"], "US")

    def test_wwr_rss_extracts_company_and_role(self):
        xml = """<?xml version='1.0'?><rss><channel><item>
        <title>Signal Co: Revenue Operations Analyst</title>
        <link>https://weworkremotely.com/remote-jobs/signal-co-revops</link>
        <description><![CDATA[<p>Full-time remote US role owning HubSpot reporting.</p>]]></description>
        <region>United States</region><pubDate>Thu, 23 Jul 2026 12:00:00 GMT</pubDate>
        </item></channel></rss>"""
        result = WeWorkRemotelyAdapter().fetch(
            lambda *_args, **_kwargs: FetchPayload(200, "https://weworkremotely.com/remote-jobs.rss", xml)
        )
        self.assertTrue(result.success)
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0]["employer_name"], "Signal Co")
        self.assertEqual(result.jobs[0]["job_title"], "Revenue Operations Analyst")
        self.assertEqual(result.jobs[0]["job_country"], "US")


class AtsRegistryTests(unittest.TestCase):
    def test_detects_supported_public_ats_urls(self):
        cases = {
            "https://boards.greenhouse.io/acme/jobs/123": ("greenhouse", "acme"),
            "https://jobs.lever.co/acme/abc": ("lever", "acme"),
            "https://jobs.eu.lever.co/euroco/abc": ("lever", "euroco"),
            "https://jobs.ashbyhq.com/rocket/abc": ("ashby", "rocket"),
            "https://example.recruitee.com/o/accountant": ("recruitee", "example"),
            "https://apply.workable.com/acme/j/ABC123/": ("workable", "acme"),
            "https://acme.jobs.personio.de/job/123": ("personio", "acme"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                ref = detect_board_ref(url)
                self.assertIsNotNone(ref)
                self.assertEqual((ref.provider, ref.identifier), expected)

    def test_registry_is_auto_populated_from_job_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "boards.json"
            registry = AtsBoardRegistry(str(path))
            changed = registry.upsert_from_jobs([{
                "employer_name": "Acme",
                "employer_website": "https://acme.com",
                "job_apply_link": "https://boards.greenhouse.io/acme/jobs/123",
                "_acquisition_source": "himalayas",
            }])
            self.assertEqual(changed, 1)
            self.assertTrue(path.exists())
            entry = next(iter(registry.entries.values()))
            self.assertEqual(entry["company_name"], "Acme")
            self.assertEqual(entry["company_domain"], "acme.com")

    def test_workable_direct_fetch_normalizes_official_job(self):
        board = {
            "provider": "workable", "identifier": "acme",
            "company_name": "Acme", "company_domain": "acme.com",
        }
        response = {
            "name": "Acme",
            "jobs": [{
                "shortcode": "ABC123",
                "title": "Staff Accountant",
                "description": "<p>Remote US full-time accounting role.</p>",
                "url": "https://apply.workable.com/acme/j/ABC123/",
                "country": "United States",
                "telecommuting": True,
                "published_on": "2026-07-23",
                "employment_type": "Full-time",
            }],
        }
        jobs, error = fetch_board_jobs(
            board, lambda url, **_kwargs: FetchPayload(200, url, json.dumps(response))
        )
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["_acquisition_source"], "ats_workable")
        self.assertTrue(jobs[0]["job_is_remote"])
        self.assertEqual(jobs[0]["job_country"], "US")

    def test_personio_direct_fetch_normalizes_official_job(self):
        board = {
            "provider": "personio", "identifier": "acme",
            "company_name": "Acme", "company_domain": "acme.com",
            "api_base": "https://acme.jobs.personio.de",
        }
        xml = """<?xml version='1.0'?><workzag-jobs><position>
        <id>123</id><name>Revenue Operations Analyst</name><office>Remote, United States</office>
        <employmentType>permanent</employmentType><createdAt>2026-07-23</createdAt>
        <jobDescriptions><jobDescription><name>Role</name><value><![CDATA[Own HubSpot, reporting, and automation.]]></value></jobDescription></jobDescriptions>
        </position></workzag-jobs>"""
        jobs, error = fetch_board_jobs(
            board, lambda url, **_kwargs: FetchPayload(200, url, xml)
        )
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["_acquisition_source"], "ats_personio")
        self.assertTrue(jobs[0]["job_is_remote"])
        self.assertEqual(jobs[0]["job_country"], "US")
        self.assertIn("HubSpot", jobs[0]["job_description"])

    def test_greenhouse_direct_fetch_normalizes_official_job(self):
        board = {
            "provider": "greenhouse",
            "identifier": "acme",
            "company_name": "Acme",
            "company_domain": "acme.com",
        }
        response = {"jobs": [{
            "id": 123,
            "title": "Customer Success Manager",
            "content": "<p>Remote US. Own onboarding and renewals.</p>",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
            "location": {"name": "Remote, United States"},
            "updated_at": "2026-07-23T10:00:00Z",
        }]}

        def fetcher(url, **_kwargs):
            self.assertIn("boards-api.greenhouse.io", url)
            return FetchPayload(200, url, json.dumps(response))

        jobs, error = fetch_board_jobs(board, fetcher)
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["job_apply_is_direct"])
        self.assertEqual(jobs[0]["employer_website"], "https://acme.com")
        self.assertEqual(jobs[0]["_acquisition_source"], "ats_greenhouse")
        self.assertEqual(jobs[0]["job_country"], "US")


class MultiSourceAcquisitionTests(unittest.TestCase):
    def test_cross_source_dedupe_prefers_direct_ats_and_merges_provenance(self):
        provider = {
            "job_id": "himalayas:1",
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "job_description": "Own customer onboarding and retention.",
            "job_apply_link": "https://himalayas.app/jobs/1",
            "apply_options": [{"publisher": "Himalayas", "apply_link": "https://himalayas.app/jobs/1"}],
            "_acquisition_source": "himalayas",
        }
        ats = {
            "job_id": "ats:greenhouse:acme:1",
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_description": "Own customer onboarding, retention, renewals, and expansion.",
            "job_apply_link": "https://boards.greenhouse.io/acme/jobs/1",
            "job_apply_is_direct": True,
            "apply_options": [{"publisher": "Greenhouse", "apply_link": "https://boards.greenhouse.io/acme/jobs/1"}],
            "_acquisition_source": "ats_greenhouse",
        }
        jobs, duplicates = multi_source_acquisition._dedupe([provider, ats])
        self.assertEqual(duplicates, 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["_acquisition_source"], "ats_greenhouse")
        self.assertEqual(set(jobs[0]["_discovery_sources"]), {"himalayas", "ats_greenhouse"})
        self.assertEqual(len(jobs[0]["apply_options"]), 2)

    def test_full_runner_uses_free_sources_without_jsearch(self):
        source_job = {
            "job_id": "himalayas:h1",
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_publisher": "Himalayas",
            "job_description": "Remote United States full-time customer success role owning onboarding, retention, renewals and customer health.",
            "job_apply_link": "https://boards.greenhouse.io/acme/jobs/123",
            "job_apply_is_direct": False,
            "job_location": "Remote - United States",
            "job_country": "US",
            "job_is_remote": True,
            "job_employment_type": "Full Time",
            "job_posted_at_datetime_utc": "2026-07-23T10:00:00+00:00",
            "apply_options": [{"publisher": "Himalayas", "apply_link": "https://boards.greenhouse.io/acme/jobs/123"}],
            "_acquisition_source": "himalayas",
        }
        fake_adapter = SimpleNamespace(fetch=lambda _fetcher: SourceResult(
            source="himalayas", jobs=[source_job], requests_attempted=1,
            requests_succeeded=1, raw_records=1, pages=1, success=True,
        ))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            seen = SeenJobsRegistry(path=str(root / "seen.json"))
            with (
                patch.object(config, "OUTPUT_DIR", str(root / "raw")),
                patch.object(config, "FILTERED_OUTPUT_DIR", str(root / "filtered")),
                patch.object(config, "STEP3_OUTPUT_DIR", str(root / "step3")),
                patch.object(config, "ATS_BOARD_REGISTRY_FILE", str(root / "boards.json")),
                patch.object(config, "ATS_REGISTRY_AUTO_SEED_HISTORY", False),
                patch.object(config, "ATS_DIRECT_ACQUISITION_ENABLED", False),
                patch.object(config, "FREE_SOURCE_LANDING_DISCOVERY_ENABLED", False),
                patch.object(config, "FREE_JOB_SOURCES", ["himalayas"]),
                patch.object(config, "FREE_SOURCE_MIN_SUCCESSFUL_SOURCES", 1),
                patch.object(config, "PRODUCTION", False),
                patch.object(multi_source_acquisition, "build_adapters", return_value=[fake_adapter]),
            ):
                result = multi_source_acquisition.run_multi_source_acquisition(
                    registry=seen,
                    fetcher=lambda *_args, **_kwargs: FetchPayload(500, "unused"),
                )
                payload = json.loads(Path(result.output_path).read_text())

        self.assertTrue(result.success)
        self.assertEqual(result.total_jobs, 1)
        self.assertEqual(payload["acquisition_mode"], "free_multi_source")
        self.assertEqual(payload["jobs"][0]["_matched_role"], "Customer Success Manager")
        self.assertEqual(result.stats["estimated_request_units"], 0)
        self.assertEqual(result.stats["source_outcomes"]["himalayas"]["selected_as_primary"], 1)
        self.assertEqual(result.stats["source_outcomes"]["himalayas"]["prefilter_viable"], 1)


class AcquisitionConfigTests(unittest.TestCase):
    def test_free_mode_does_not_require_rapidapi_key(self):
        with (
            patch.object(config, "ACQUISITION_MODE", "free_multi_source"),
            patch.object(config, "RAPIDAPI_KEY", ""),
            patch.object(config, "FREE_JOB_SOURCES", ["himalayas", "jobicy"]),
            patch.object(config, "FINAL_PASS_TOPUP_ENABLED", False),
            patch.object(config, "PRODUCTION", False),
        ):
            result = validate_setup.static_checks()
        self.assertFalse(any("RAPIDAPI_KEY" in error for error in result["errors"]))

    def test_jsearch_rollback_mode_still_requires_rapidapi_key(self):
        with (
            patch.object(config, "ACQUISITION_MODE", "jsearch"),
            patch.object(config, "RAPIDAPI_KEY", ""),
            patch.object(config, "PRODUCTION", False),
        ):
            result = validate_setup.static_checks()
        self.assertIn("Missing RAPIDAPI_KEY for ACQUISITION_MODE=jsearch", result["errors"])


class TopupZeroYieldRegressionTests(unittest.TestCase):
    def test_all_rejected_microbatch_continues_instead_of_filter_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initial_raw = root / "initial_raw.json"
            initial_raw.write_text(json.dumps({"jobs": [{"job_id": "base"}]}))
            initial_enriched_path = root / "initial_enriched.json"
            initial_enriched_path.write_text(json.dumps({
                "jobs": [], "processed_job_refs": [], "processed_company_keys": []
            }))
            initial_enriched = Step3Result(
                output_path=str(initial_enriched_path), total_input_jobs=0, total_output_leads=0,
                company_criteria_excluded=0, hiring_manager_found=0, hiring_manager_not_found=0,
                match_rate=0.0, contactable_hiring_managers=0, uncontactable_hiring_managers=0,
                contactable_rate=0.0, companies_considered=0, eligible_companies=0,
                company_criteria_excluded_companies=0, final_pass_target=1, final_pass_leads=0,
                final_pass_target_reached=False, reviewable_leads=0, reviewable_target_reached=False,
                max_eligible_companies=90, stop_reason="candidate_pool_exhausted",
                processed_company_keys=[], stats={},
            )
            initial_scrape = ScrapeResult(
                output_path=str(initial_raw), total_jobs=1, roles_with_results=1,
                stats={"estimated_request_units": 1, "query_metrics": {}},
            )
            topup_files = []
            topup_scrapes = []
            for index in (1, 2):
                path = root / f"topup_{index}.json"
                path.write_text(json.dumps({"jobs": [{"job_id": f"j{index}"}]}))
                topup_files.append(path)
                topup_scrapes.append(ScrapeResult(
                    output_path=str(path), total_jobs=1, roles_with_results=1,
                    stats={
                        "estimated_request_units": 1, "queries_attempted": 1,
                        "queried_search_roles": ["Staff Accountant"],
                        "topup_new_prefilter_viable": 1,
                        "topup_stop_reason": "topup_unit_budget_exhausted",
                        "query_metrics": {},
                    },
                ))
            filtered_ok = root / "filtered_ok.json"
            filtered_ok.write_text(json.dumps({"jobs": [{"job_id": "j2"}]}))
            qualified = root / "qualified.json"
            qualified.write_text(json.dumps({"jobs": [{"job_id": "j2"}]}))
            enriched_path = root / "enriched.json"
            enriched_path.write_text(json.dumps({
                "jobs": [{"job_id": "j2", "lead_key": "pass", "_final_state": "FINAL_PASS", "_account_gate_state": "PASS"}],
                "processed_job_refs": [{"job_id": "j2"}],
                "processed_company_keys": ["acme.com"],
            }))
            enriched = Step3Result(
                output_path=str(enriched_path), total_input_jobs=1, total_output_leads=1,
                company_criteria_excluded=0, hiring_manager_found=1, hiring_manager_not_found=0,
                match_rate=1.0, contactable_hiring_managers=1, uncontactable_hiring_managers=0,
                contactable_rate=1.0, companies_considered=1, eligible_companies=1,
                company_criteria_excluded_companies=0, final_pass_target=1, final_pass_leads=1,
                final_pass_target_reached=True, reviewable_leads=1, reviewable_target_reached=True,
                max_eligible_companies=90, stop_reason="final_pass_target_reached",
                processed_company_keys=["acme.com"], stats={},
            )
            zero_filter = SimpleNamespace(
                output_path=str(root / "filtered_zero.json"), kept_count=0, rejected_count=1,
                success=False, errors=["Filter kept zero jobs from a non-empty scrape"],
            )
            good_filter = SimpleNamespace(
                output_path=str(filtered_ok), kept_count=1, rejected_count=0, success=True, errors=[],
            )
            checkpoint = SimpleNamespace(append_jobs=lambda *args, **kwargs: None)
            with (
                patch.object(config, "STEP3_OUTPUT_DIR", str(root)),
                patch.object(config, "FILTERED_OUTPUT_DIR", str(root)),
                patch.object(config, "FINAL_PASS_MAX_TOPUP_ITERATIONS", 3),
                patch.object(config, "FINAL_PASS_MAX_RUNTIME_SECONDS", 300),
                patch.object(config, "FINAL_PASS_MICROBATCH_QUERY_UNITS", 1),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 20),
                patch.object(config, "TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES", 3),
                patch.object(final_pass_topup, "PipelineCheckpoint", return_value=checkpoint),
                patch.object(final_pass_topup, "run_targeted_topup_scrape", side_effect=topup_scrapes) as scrape_mock,
                patch.object(final_pass_topup, "run_filter", side_effect=[zero_filter, good_filter]),
                patch.object(final_pass_topup, "run_precontact_qualification", return_value=SimpleNamespace(
                    output_path=str(qualified), contact_eligible_jobs=1, rejected_jobs=0,
                    unverified_jobs=0, nonpass_path="",
                )),
                patch.object(final_pass_topup, "run_hiring_manager_identification", return_value=enriched),
            ):
                combined, details = final_pass_topup.run_final_pass_topup(
                    initial_scrape=initial_scrape, initial_enriched=initial_enriched,
                    registry=SeenJobsRegistry(path=str(root / "seen.json")),
                    target_final_pass_leads=1, max_eligible_companies=90,
                )

        self.assertEqual(scrape_mock.call_count, 2)
        self.assertEqual(combined.final_pass_leads, 1)
        self.assertEqual(details["stop_reason"], "final_pass_target_reached")
        self.assertTrue(details["rounds"][0]["filter_zero_yield"])
        self.assertEqual(details["errors"], [])


if __name__ == "__main__":
    unittest.main()
