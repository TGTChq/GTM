from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import age_recovery
import config
import hiring_manager
import multi_source_acquisition
import run_daily
from ats_board_registry import detect_board_ref, fetch_board_jobs
from free_job_sources import FetchPayload, SourceResult
from hiring_manager import Step3Result
from job_fact_extractor import extract_job_facts
from job_filter import FilterResult, is_stale_job
from job_source_resolver import ResolvedJobSource
from jsearch_scraper import ScrapeResult
from pipeline_state import SeenJobsRegistry
from qualification_pipeline import QualificationResult


def _step3_result(
    path: str,
    *,
    final_pass: int = 0,
    eligible: int = 0,
    considered: int = 0,
    keys: list[str] | None = None,
) -> Step3Result:
    return Step3Result(
        output_path=path,
        total_input_jobs=considered,
        total_output_leads=final_pass,
        company_criteria_excluded=0,
        hiring_manager_found=final_pass,
        hiring_manager_not_found=0,
        match_rate=1.0 if final_pass else 0.0,
        contactable_hiring_managers=final_pass,
        uncontactable_hiring_managers=0,
        contactable_rate=1.0 if final_pass else 0.0,
        companies_considered=considered,
        eligible_companies=eligible,
        final_pass_target=30,
        final_pass_leads=final_pass,
        final_pass_target_reached=final_pass >= 30,
        target_reached=final_pass >= 30,
        processed_company_keys=list(keys or []),
    )


class DefinitiveAgeWindowTests(unittest.TestCase):
    @staticmethod
    def _job(age_days: int) -> dict:
        posted = datetime.now(timezone.utc) - timedelta(days=age_days, hours=1)
        return {
            "job_id": f"age-{age_days}",
            "job_posted_at_datetime_utc": posted.isoformat(),
        }

    def test_primary_window_accepts_recent_and_rejects_day_15(self):
        self.assertFalse(is_stale_job(self._job(13), max_age_days=14)[0])
        stale, reason = is_stale_job(self._job(15), max_age_days=14)
        self.assertTrue(stale)
        self.assertIn("stale_job", reason)

    def test_recovery_window_is_disjoint_and_bounded(self):
        too_new, reason = is_stale_job(
            self._job(10), max_age_days=30, min_age_days=15
        )
        self.assertTrue(too_new)
        self.assertIn("outside_recovery_window", reason)
        self.assertFalse(
            is_stale_job(self._job(20), max_age_days=30, min_age_days=15)[0]
        )
        self.assertTrue(
            is_stale_job(self._job(31), max_age_days=30, min_age_days=15)[0]
        )


class WorkdayPublicAcquisitionTests(unittest.TestCase):
    def test_workday_url_is_detected_and_public_cxs_job_is_normalized(self):
        public_url = (
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme_Careers/"
            "job/Dallas-TX/Staff-Accountant_R123"
        )
        ref = detect_board_ref(public_url)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.provider, "workday")
        self.assertEqual(ref.identifier, "acme|Acme_Careers")

        board = {
            **asdict(ref),
            "key": ref.key,
            "company_name": "Acme",
            "company_domain": "acme.com",
        }
        list_payload = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Staff Accountant",
                    "externalPath": (
                        "/en-US/Acme_Careers/job/Dallas-TX/"
                        "Staff-Accountant_R123"
                    ),
                    "locationsText": "Dallas, TX",
                    "postedOn": "Posted Today",
                    "bulletFields": ["R123"],
                }
            ],
        }
        detail_payload = {
            "jobPostingInfo": {
                "title": "Staff Accountant",
                "jobReqId": "R123",
                "jobDescription": (
                    "Own monthly close, reconciliations, and financial reporting "
                    "for a full-time accounting team."
                ),
                "location": "Dallas, TX, United States",
                "timeType": "Full time",
                "startDate": datetime.now(timezone.utc).isoformat(),
                "externalUrl": public_url,
            }
        }
        calls: list[tuple[str, str]] = []

        def fetcher(url, *, method="GET", **_kwargs):
            calls.append((method, url))
            if url.endswith("/jobs"):
                return FetchPayload(200, url, json.dumps(list_payload))
            return FetchPayload(200, url, json.dumps(detail_payload))

        with patch.object(config, "ATS_MAX_JOBS_PER_BOARD", 20):
            jobs, error = fetch_board_jobs(board, fetcher)

        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["_acquisition_source"], "ats_workday")
        self.assertEqual(jobs[0]["employer_name"], "Acme")
        self.assertEqual(jobs[0]["job_country"], "US")
        self.assertEqual(jobs[0]["job_employment_type"], "Full time")
        self.assertTrue(jobs[0]["job_apply_is_direct"])
        self.assertTrue(jobs[0]["_workday_detail_request_made"])
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[1][0], "GET")


class OptionalJSearchResilienceTests(unittest.TestCase):
    def test_jsearch_failure_does_not_stop_public_source_acquisition(self):
        now = datetime.now(timezone.utc).isoformat()
        public_job = {
            "job_id": "fixture:1",
            "job_title": "Staff Accountant",
            "employer_name": "Ledger Labs",
            "employer_website": "https://ledgerlabs.com",
            "job_description": (
                "Full-time accounting role owning close, reconciliations, and reporting."
            ),
            "job_apply_link": "https://ledgerlabs.com/careers/accountant",
            "job_location": "Austin, TX, United States",
            "job_country": "US",
            "job_is_remote": False,
            "job_employment_type": "Full Time",
            "job_posted_at_datetime_utc": now,
            "_acquisition_source": "fixture_public_feed",
            "_provider_record_structured": True,
        }

        class Adapter:
            def fetch(self, _fetcher):
                return SourceResult(
                    source="fixture_public_feed",
                    jobs=[public_job],
                    requests_attempted=1,
                    requests_succeeded=1,
                    raw_records=1,
                    success=True,
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = SeenJobsRegistry(str(root / "seen.json"))
            patches = (
                patch.object(config, "OUTPUT_DIR", str(root / "raw")),
                patch.object(config, "ATS_BOARD_REGISTRY_FILE", str(root / "ats.json")),
                patch.object(config, "ATS_REGISTRY_AUTO_SEED_HISTORY", False),
                patch.object(config, "ATS_DIRECT_ACQUISITION_ENABLED", False),
                patch.object(config, "HIMALAYAS_COMPANY_PROFILE_MAX_REQUESTS", 0),
                patch.object(config, "FREE_SOURCE_LANDING_DISCOVERY_MAX_REQUESTS", 0),
                patch.object(config, "FREE_SOURCE_MIN_SUCCESSFUL_SOURCES", 1),
                patch.object(config, "PRODUCTION", False),
                patch.object(config, "MULTI_SOURCE_JSEARCH_ENABLED", True),
                patch.object(config, "MULTI_SOURCE_JSEARCH_OPTIONAL", True),
                patch.object(config, "RAPIDAPI_KEY", "fixture-key"),
                patch.object(multi_source_acquisition, "build_adapters", return_value=[Adapter()]),
                patch.object(
                    multi_source_acquisition,
                    "run_daily_scrape",
                    side_effect=RuntimeError("monthly quota exhausted"),
                ),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
                result = multi_source_acquisition.run_multi_source_acquisition(
                    registry=registry,
                    fetcher=lambda url, **_kwargs: FetchPayload(404, url, ""),
                )

        self.assertTrue(result.success)
        self.assertEqual(result.total_jobs, 1)
        self.assertTrue(result.stats["jsearch"]["attempted"])
        self.assertFalse(result.stats["jsearch"]["success"])
        self.assertIn("monthly quota exhausted", result.stats["jsearch"]["errors"][0])
        self.assertIn("jsearch", result.failed_roles)


class MinimumWithoutMaximumTests(unittest.TestCase):
    def test_strict_final_pass_target_does_not_cap_valid_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / "qualified.json"
            jobs = [
                {
                    "job_id": f"job-{index}",
                    "job_title": "Staff Accountant",
                    "employer_name": f"Company {index}",
                    "employer_website": f"https://company{index}.com",
                    "_job_gate_state": "PASS",
                }
                for index in range(1, 4)
            ]
            input_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

            def process(company_jobs):
                job = company_jobs[0]
                domain = job["employer_website"].split("//", 1)[1]
                return [
                    {
                        **job,
                        "_step3_status": "found",
                        "_account_gate_state": "PASS",
                        "_final_state": "FINAL_PASS",
                        "hiring_manager_email": f"leader@{domain}",
                        "lead_key": f"{domain}:accounting",
                    }
                ], {}

            (root / "enriched").mkdir(parents=True, exist_ok=True)
            with (
                patch.object(config, "STEP3_OUTPUT_DIR", str(root / "enriched")),
                patch.object(config, "FINAL_PASS_PIPELINE_ENABLED", True),
                patch.object(config, "CONTINUE_AFTER_FINAL_PASS_TARGET", True),
                patch.object(config, "ENFORCE_HM_MATCH_RATE", False),
                patch.object(hiring_manager, "validate_preflight"),
                patch.object(hiring_manager, "process_company", side_effect=process),
            ):
                result = hiring_manager.run_hiring_manager_identification(
                    str(input_path), target_final_pass_leads=1
                )

        self.assertEqual(result.companies_considered, 3)
        self.assertEqual(result.final_pass_leads, 3)
        self.assertTrue(result.final_pass_target_reached)
        self.assertEqual(result.stop_reason, "candidate_pool_exhausted")

    def test_zero_delivery_limit_means_unlimited_with_sla_preserved(self):
        with (
            patch.object(config, "READY_DAILY_DELIVERY_LIMIT", 0),
            patch.object(config, "READY_INVENTORY_TARGET", 30),
            patch.object(config, "get_final_pass_target", return_value=30),
        ):
            acquisition_target, delivery_target = run_daily._ready_targets(0)
        self.assertEqual(acquisition_target, 30)
        self.assertEqual(delivery_target, 30)


class AdaptiveAgeRecoveryOrchestrationTests(unittest.TestCase):
    def test_recovery_uses_15_to_30_days_and_remaining_company_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.json"
            raw.write_text(json.dumps({"jobs": []}), encoding="utf-8")
            filtered = root / "filtered.json"
            recovery_job = {
                "job_id": "recovery-1",
                "job_title": "Staff Accountant",
                "employer_name": "Recovery Co",
                "employer_website": "https://recovery.co",
            }
            filtered.write_text(json.dumps({"jobs": [recovery_job]}), encoding="utf-8")
            qualified_path = root / "qualified.json"
            qualified_path.write_text(json.dumps({"jobs": [recovery_job]}), encoding="utf-8")
            recovered_path = root / "recovered.json"
            recovered_path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
            combined_path = root / "combined.json"
            combined_path.write_text(
                json.dumps({"jobs": [], "stop_reason": "age_recovery_completed"}),
                encoding="utf-8",
            )

            initial = _step3_result(
                str(root / "initial.json"),
                final_pass=1,
                eligible=2,
                considered=2,
                keys=["existing.co"],
            )
            recovered = _step3_result(
                str(recovered_path), final_pass=3, eligible=3, considered=3
            )
            combined = _step3_result(
                str(combined_path), final_pass=4, eligible=5, considered=5
            )
            filter_result = FilterResult(
                output_path=str(filtered),
                rejected_path=str(root / "rejected.json"),
                kept_count=1,
                rejected_count=0,
                stats={"kept": 1},
            )
            qualification = QualificationResult(
                output_path=str(qualified_path),
                nonpass_path=str(root / "nonpass.json"),
                input_jobs=1,
                contact_eligible_jobs=1,
                rejected_jobs=0,
                unverified_jobs=0,
                needs_check_jobs=0,
                stats={},
            )
            scrape = ScrapeResult(str(raw), 1, {})
            registry = SeenJobsRegistry(str(root / "seen.json"))

            with (
                patch.object(config, "AGE_RECOVERY_ENABLED", True),
                patch.object(config, "RECOVERY_MIN_JOB_AGE_DAYS", 15),
                patch.object(config, "RECOVERY_MAX_JOB_AGE_DAYS", 30),
                patch.object(age_recovery, "run_filter", return_value=filter_result) as run_filter_mock,
                patch.object(
                    age_recovery,
                    "run_precontact_qualification",
                    return_value=qualification,
                ),
                patch.object(
                    age_recovery,
                    "run_hiring_manager_identification",
                    return_value=recovered,
                ) as hm_mock,
                patch.object(
                    age_recovery,
                    "combine_step3_results",
                    return_value=combined,
                ),
            ):
                result, details = age_recovery.run_age_recovery(
                    initial_scrape=scrape,
                    initial_enriched=initial,
                    registry=registry,
                    target_final_pass_leads=3,
                    max_eligible_companies=10,
                )

        self.assertIs(result, combined)
        self.assertEqual(result.stop_reason, "final_pass_minimum_reached_after_age_recovery")
        self.assertEqual(details["combined_final_pass_leads"], 4)
        self.assertEqual(run_filter_mock.call_args.kwargs["min_age_days"], 15)
        self.assertEqual(run_filter_mock.call_args.kwargs["max_age_days"], 30)
        self.assertTrue(run_filter_mock.call_args.kwargs["allow_empty"])
        self.assertEqual(hm_mock.call_args.kwargs["target_final_pass_leads"], 2)
        self.assertEqual(hm_mock.call_args.kwargs["max_eligible_companies"], 8)

    def test_recovery_is_skipped_after_primary_sla_is_met(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.json"
            raw.write_text(json.dumps({"jobs": []}), encoding="utf-8")
            initial_path = root / "initial.json"
            initial_path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
            initial = _step3_result(str(initial_path), final_pass=30, eligible=30)
            scrape = ScrapeResult(str(raw), 0, {})
            registry = SeenJobsRegistry(str(root / "seen.json"))
            with (
                patch.object(config, "AGE_RECOVERY_ENABLED", True),
                patch.object(age_recovery, "run_filter") as run_filter_mock,
            ):
                result, details = age_recovery.run_age_recovery(
                    initial_scrape=scrape,
                    initial_enriched=initial,
                    registry=registry,
                    target_final_pass_leads=30,
                    max_eligible_companies=None,
                )
        self.assertIs(result, initial)
        self.assertFalse(details["attempted"])
        self.assertEqual(details["stop_reason"], "minimum_reached_in_primary_window")
        run_filter_mock.assert_not_called()


class SmartRecruitersPublicAcquisitionTests(unittest.TestCase):
    def test_public_company_board_is_detected_and_normalized(self):
        ref = detect_board_ref(
            "https://careers.smartrecruiters.com/Acme/"
            "743999999-staff-accountant"
        )
        self.assertIsNotNone(ref)
        self.assertEqual(ref.provider, "smartrecruiters")
        self.assertEqual(ref.identifier, "Acme")

        board = {
            **asdict(ref),
            "key": ref.key,
            "company_name": "Acme",
            "company_domain": "acme.com",
        }
        listing = {
            "totalFound": 1,
            "content": [
                {
                    "id": "743999999",
                    "uuid": "posting-uuid",
                    "name": "Staff Accountant",
                    "company": {"identifier": "Acme", "name": "Acme Inc"},
                    "releasedDate": datetime.now(timezone.utc).isoformat(),
                    "location": {
                        "city": "Austin",
                        "region": "TX",
                        "country": "us",
                        "remote": False,
                    },
                    "typeOfEmployment": {"label": "Full-time"},
                    "ref": (
                        "https://api.smartrecruiters.com/v1/companies/"
                        "Acme/postings/743999999"
                    ),
                }
            ],
        }
        detail = {
            **listing["content"][0],
            "active": True,
            "postingUrl": (
                "https://jobs.smartrecruiters.com/Acme/"
                "743999999-staff-accountant"
            ),
            "applyUrl": (
                "https://jobs.smartrecruiters.com/Acme/"
                "743999999-staff-accountant?oga=true"
            ),
            "jobAd": {
                "sections": {
                    "companyDescription": {
                        "title": "Company",
                        "text": "Acme builds finance software.",
                    },
                    "jobDescription": {
                        "title": "Job Description",
                        "text": (
                            "Own monthly close, reconciliations, and reporting "
                            "in this full-time role."
                        ),
                    },
                    "qualifications": {
                        "title": "Qualifications",
                        "text": "Three years of accounting experience.",
                    },
                }
            },
        }
        calls = []

        def fetcher(url, **kwargs):
            calls.append((url, kwargs))
            payload = detail if url.endswith("/743999999") else listing
            return FetchPayload(200, url, json.dumps(payload))

        with (
            patch.object(config, "ATS_MAX_JOBS_PER_BOARD", 20),
            patch.object(config, "ATS_SMARTRECRUITERS_MAX_PAGES_PER_BOARD", 2),
            patch.object(
                config,
                "ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_BOARD",
                5,
            ),
        ):
            jobs, error = fetch_board_jobs(board, fetcher)

        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["_acquisition_source"], "ats_smartrecruiters")
        self.assertEqual(job["employer_name"], "Acme Inc")
        self.assertEqual(job["job_country"], "US")
        self.assertEqual(job["job_employment_type"], "Full-time")
        self.assertFalse(job["job_is_remote"])
        self.assertTrue(job["job_apply_is_direct"])
        self.assertIn("monthly close", job["job_description"])
        self.assertTrue(job["_smartrecruiters_detail_request_made"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(
            any(
                str(key).lower() == "x-smarttoken"
                for _url, kwargs in calls
                for key in (kwargs.get("headers") or {})
            )
        )


class PhysicalRequirementPrecedenceTests(unittest.TestCase):
    def test_field_requirement_overrides_generic_onsite_wording(self):
        description = (
            "This is a full-time onsite customer success role in the United States. "
            "The employee must work on customer sites and travel 30% each month."
        )
        source = ResolvedJobSource(
            state="ACTIVE_DIRECT_STRUCTURED",
            source_url="https://example.com/jobs/field-csm",
            source_type="company",
            active=True,
            canonical_title="Customer Success Manager",
            canonical_employer="Example",
            description=description,
            location_text="Austin, TX, United States",
            employment_type="Full Time",
            official=True,
            corroborated=True,
        )
        facts = extract_job_facts(
            {
                "job_title": "Customer Success Manager",
                "employer_name": "Example",
                "job_description": description,
                "job_location": "Austin, TX, United States",
                "job_country": "US",
                "job_employment_type": "Full Time",
                "_employment_quality": "full_time",
                "_work_arrangement": "onsite",
                "_remote_scope": "us_explicit",
                "_us_eligibility_reason": "explicit_us_location",
            },
            source,
        )
        self.assertEqual(
            facts["work_arrangement"].value,
            "field_work_required",
        )
        self.assertEqual(facts["travel_requirement"].value, "substantial")


class MultiSourceTopupPolicyTests(unittest.TestCase):
    def test_multi_source_topup_ignores_stale_legacy_disabled_flag(self):
        with (
            patch.object(config, "MULTI_SOURCE_JSEARCH_TOPUP_ENABLED", True),
            patch.object(config, "FINAL_PASS_TOPUP_ENABLED", False),
            patch.object(config, "FINAL_PASS_PIPELINE_ENABLED", True),
            patch.object(config, "JSEARCH_TOPUP_MAX_ROUNDS", 3),
        ):
            self.assertTrue(
                run_daily._jsearch_topup_enabled(
                    "multi_source",
                    jsearch_available=True,
                    target_final_pass=30,
                )
            )

    def test_multi_source_topup_requires_jsearch_and_positive_deficit_target(self):
        with (
            patch.object(config, "MULTI_SOURCE_JSEARCH_TOPUP_ENABLED", True),
            patch.object(config, "JSEARCH_TOPUP_MAX_ROUNDS", 3),
        ):
            self.assertFalse(
                run_daily._jsearch_topup_enabled(
                    "multi_source",
                    jsearch_available=False,
                    target_final_pass=30,
                )
            )
            self.assertFalse(
                run_daily._jsearch_topup_enabled(
                    "multi_source",
                    jsearch_available=True,
                    target_final_pass=0,
                )
            )


if __name__ == "__main__":
    unittest.main()
