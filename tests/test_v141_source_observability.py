from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import final_pass_topup
import run_daily
from ats_board_registry import AtsBoardRegistry, detect_board_ref
from free_job_sources import FetchPayload
from hiring_manager import Step3Result
from jsearch_scraper import ScrapeResult
from job_quality import assess_posting_integrity
from multi_source_acquisition import _enrich_himalayas_company_profiles
from pipeline_state import SeenJobsRegistry
from run_free_source_shadow import (
    _jsearch_request_metrics,
    _shadow_funnel_diagnostics,
    _shadow_rejection_diagnostics,
)


class ShadowObservabilityV141Tests(unittest.TestCase):
    def test_shadow_reports_actual_jsearch_requests(self):
        metrics = _jsearch_request_metrics(
            {
                "source_metrics": {
                    "jsearch": {
                        "requests_attempted": 82,
                        "requests_succeeded": 82,
                        "normalized_jobs": 485,
                    }
                },
                "jsearch": {
                    "enabled": True,
                    "attempted": True,
                    "jobs": 485,
                    "stats": {"estimated_request_units": 82},
                },
            }
        )
        self.assertEqual(metrics["requests_attempted"], 82)
        self.assertEqual(metrics["requests_succeeded"], 82)
        self.assertEqual(metrics["estimated_request_units"], 82)
        self.assertEqual(metrics["jobs_normalized"], 485)

    def test_shadow_rejection_diagnostics_show_exact_reasons_and_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rejected.json"
            path.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "_filter_reason": "insufficient_direct_employer_evidence",
                                "_acquisition_source": "ats_ashby",
                                "employer_name": "Acme",
                                "job_title": "Customer Success Manager",
                            },
                            {
                                "_filter_reason": "insufficient_direct_employer_evidence",
                                "_acquisition_source": "himalayas",
                                "employer_name": "Beta",
                                "job_title": "Accountant",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            diagnostics = _shadow_rejection_diagnostics(str(path))
        top = diagnostics["top_exact_reasons"][0]
        self.assertEqual(top["jobs"], 2)
        self.assertEqual(top["sources"], {"ats_ashby": 1, "himalayas": 1})

    def test_shadow_explains_when_modality_removed_nothing(self):
        with patch.object(config, "TARGET_FINAL_PASS_LEADS_PER_RUN", 30):
            diagnostics = _shadow_funnel_diagnostics(
                acquired=2219,
                filter_stats={
                    "kept": 114,
                    "excluded_posting_integrity": 1138,
                    "excluded_stale": 388,
                    "excluded_role_mismatch": 309,
                    "excluded_in_person": 0,
                },
                filtered_company_metrics={"unique_companies": 102},
                qualified_company_metrics={
                    "unique_companies": 58,
                    "jobs_with_company_identity": 66,
                },
            )
        self.assertTrue(diagnostics["modality_was_not_the_volume_constraint"])
        self.assertTrue(diagnostics["precontact_unique_companies_above_minimum"])
        self.assertTrue(diagnostics["final_pass_not_computed_in_shadow"])
        self.assertEqual(
            diagnostics["top_filter_loss_families"][0]["reason"],
            "excluded_posting_integrity",
        )


class StructuredEmployerIntegrityV141Tests(unittest.TestCase):
    @staticmethod
    def _base_job() -> dict:
        return {
            "job_id": "job-1",
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "job_description": "Own onboarding and customer adoption.",
            "job_apply_link": "https://jobs.ashbyhq.com/acme/abc",
            "job_publisher": "Ashby",
            "_acquisition_source": "ats_ashby",
        }

    def test_verified_direct_ats_identity_bypasses_duplicate_description_proof(self):
        job = self._base_job()
        job.update({
            "job_apply_is_direct": True,
            "_ats_board_identity_verified": True,
            "_provider_record_structured": True,
        })
        assessment = assess_posting_integrity(job)
        self.assertTrue(assessment.eligible, assessment.reason)

    def test_structured_public_feed_employer_can_continue_to_later_source_gates(self):
        job = self._base_job()
        job.update({
            "job_apply_link": "https://himalayas.app/companies/acme/jobs/123",
            "job_publisher": "Himalayas",
            "_acquisition_source": "himalayas",
            "_provider_record_structured": True,
        })
        assessment = assess_posting_integrity(job)
        self.assertTrue(assessment.eligible, assessment.reason)

    def test_jsearch_record_without_direct_identity_proof_remains_guarded(self):
        job = self._base_job()
        job.update({
            "job_apply_link": "https://www.linkedin.com/jobs/view/123",
            "job_publisher": "LinkedIn",
            "_acquisition_source": "jsearch",
            "_provider_record_structured": True,
        })
        assessment = assess_posting_integrity(job)
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "insufficient_direct_employer_evidence")

    def test_unstructured_syndicated_identity_remains_rejected(self):
        job = self._base_job()
        assessment = assess_posting_integrity(job)
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "insufficient_direct_employer_evidence")

    def test_generic_structured_employer_remains_rejected(self):
        job = self._base_job()
        job.update({
            "employer_name": "Confidential",
            "_acquisition_source": "jsearch",
            "_provider_record_structured": True,
        })
        assessment = assess_posting_integrity(job)
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "unresolvable_generic_employer")


class WorkableRegistryV141Tests(unittest.TestCase):
    def test_generic_workable_hosts_are_not_company_boards(self):
        self.assertIsNone(detect_board_ref("https://jobs.workable.com/resources"))
        self.assertIsNone(detect_board_ref("https://careers.workable.com/openings"))
        self.assertIsNone(detect_board_ref("https://apply.workable.com/jobs/ABC"))
        valid = detect_board_ref("https://apply.workable.com/acme/j/ABC123/")
        self.assertIsNotNone(valid)
        self.assertEqual(valid.identifier, "acme")

    def test_legacy_invalid_workable_entry_is_pruned_on_load(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "boards.json"
            invalid_key = "workable:jobs:https://www.workable.com"
            valid_key = "workable:acme:https://www.workable.com"
            path.write_text(
                json.dumps(
                    {
                        "boards": {
                            invalid_key: {
                                "provider": "workable",
                                "identifier": "jobs",
                                "api_base": "https://www.workable.com",
                                "company_name": "Workable",
                            },
                            valid_key: {
                                "provider": "workable",
                                "identifier": "acme",
                                "api_base": "https://www.workable.com",
                                "company_name": "Acme",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = AtsBoardRegistry(path=str(path))
            reloaded = json.loads(path.read_text(encoding="utf-8"))["boards"]

        self.assertEqual(registry.invalid_entries_pruned, 1)
        self.assertNotIn(invalid_key, registry.entries)
        self.assertIn(valid_key, registry.entries)
        self.assertNotIn(invalid_key, reloaded)


class HimalayasProfileV141Tests(unittest.TestCase):
    HTML = """
        <html><head><meta property="og:title" content="Acme: Remote Jobs | Himalayas"></head>
        <body><p>Acme is a software company.</p><p>51-200 employees</p>
        <a href="https://acme.com/about">Visit acme.com</a></body></html>
    """

    @staticmethod
    def _job(index: int) -> dict:
        return {
            "job_id": f"h:{index}",
            "job_title": "Customer Success Manager",
            "employer_name": f"Acme {index}" if index else "Acme",
            "job_description": "Own customer onboarding for a remote US team.",
            "job_apply_link": f"https://himalayas.app/companies/acme-{index}/jobs/1",
            "job_location": "Remote - United States",
            "job_country": "US",
            "job_is_remote": True,
            "job_employment_type": "Full Time",
            "job_posted_at_datetime_utc": "2026-07-23T12:00:00Z",
            "_acquisition_source": "himalayas",
            "_provider_record_structured": True,
            "_source_company_slug": f"acme-{index}" if index else "acme",
        }

    def test_profile_fetch_uses_browser_headers_and_exposes_status(self):
        job = self._job(0)
        captured = {}

        def fetcher(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            return FetchPayload(200, url, self.HTML)

        with (
            patch.object(config, "HIMALAYAS_COMPANY_PROFILE_MAX_REQUESTS", 1),
            patch.object(config, "HIMALAYAS_COMPANY_PROFILE_MAX_CONSECUTIVE_FAILURES", 3),
        ):
            metrics = _enrich_himalayas_company_profiles([job], fetcher=fetcher)

        self.assertTrue(captured["url"].endswith("/companies/acme/"))
        self.assertIn("Mozilla/5.0", captured["headers"]["User-Agent"])
        self.assertEqual(metrics["http_status_counts"], {"200": 1})
        self.assertEqual(metrics["verified"], 1)

    def test_profile_access_circuit_breaker_stops_repeated_403s(self):
        jobs = [self._job(index) for index in range(1, 8)]
        calls = []

        def fetcher(url, **_kwargs):
            calls.append(url)
            return FetchPayload(403, url, "Forbidden")

        with (
            patch.object(config, "HIMALAYAS_COMPANY_PROFILE_MAX_REQUESTS", 10),
            patch.object(config, "HIMALAYAS_COMPANY_PROFILE_MAX_CONSECUTIVE_FAILURES", 3),
        ):
            metrics = _enrich_himalayas_company_profiles(jobs, fetcher=fetcher)

        self.assertEqual(len(calls), 3)
        self.assertTrue(metrics["circuit_breaker_triggered"])
        self.assertEqual(metrics["stop_reason"], "consecutive_profile_access_failures")
        self.assertEqual(metrics["failure_reasons"], {"http_403": 3})


class MinimumRecoveryV141Tests(unittest.TestCase):
    def _result(self, path: Path, *, final_pass: int, company: str) -> Step3Result:
        return Step3Result(
            output_path=str(path),
            total_input_jobs=1,
            total_output_leads=final_pass,
            company_criteria_excluded=0,
            hiring_manager_found=final_pass,
            hiring_manager_not_found=0,
            match_rate=1.0 if final_pass else 0.0,
            contactable_hiring_managers=final_pass,
            uncontactable_hiring_managers=0,
            contactable_rate=1.0 if final_pass else 0.0,
            companies_considered=1 if company else 0,
            eligible_companies=1 if company else 0,
            company_criteria_excluded_companies=0,
            final_pass_target=1,
            final_pass_leads=final_pass,
            final_pass_target_reached=final_pass >= 1,
            reviewable_leads=final_pass,
            reviewable_target_reached=final_pass >= 1,
            max_eligible_companies=None,
            stop_reason="candidate_pool_exhausted",
            processed_company_keys=[company] if company else [],
            stats={},
        )

    def test_multi_source_topup_ignores_legacy_rounds_zero(self):
        with (
            patch.object(config, "MULTI_SOURCE_JSEARCH_TOPUP_ENABLED", True),
            patch.object(config, "JSEARCH_TOPUP_MAX_ROUNDS", 0),
        ):
            self.assertTrue(
                run_daily._jsearch_topup_enabled(
                    "multi_source",
                    jsearch_available=True,
                    target_final_pass=30,
                )
            )

    def test_multi_source_recovery_can_continue_beyond_two_microbatches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.json"
            raw.write_text(json.dumps({"jobs": [{"job_id": "seed"}]}), encoding="utf-8")
            initial_path = root / "initial.json"
            initial_path.write_text(
                json.dumps(
                    {
                        "jobs": [],
                        "processed_job_refs": [{"job_id": "seed"}],
                        "processed_company_keys": [],
                    }
                ),
                encoding="utf-8",
            )
            initial_result = self._result(initial_path, final_pass=0, company="")
            initial_scrape = ScrapeResult(
                output_path=str(raw),
                total_jobs=1,
                roles_with_results=1,
                stats={"estimated_request_units": 1, "query_metrics": {}},
            )

            zero_scrapes = []
            for round_number in (1, 2):
                path = root / f"zero_{round_number}.json"
                path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
                zero_scrapes.append(
                    ScrapeResult(
                        output_path=str(path),
                        total_jobs=0,
                        roles_with_results=0,
                        stats={
                            "estimated_request_units": 3,
                            "queries_attempted": 1,
                            "queried_search_roles": [f"Role {round_number}"],
                            "topup_new_prefilter_viable": 0,
                            "topup_stop_reason": "topup_unit_budget_exhausted",
                            "query_metrics": {},
                        },
                    )
                )

            viable_raw = root / "viable.json"
            viable_raw.write_text(json.dumps({"jobs": [{"job_id": "winner"}]}), encoding="utf-8")
            viable_scrape = ScrapeResult(
                output_path=str(viable_raw),
                total_jobs=1,
                roles_with_results=1,
                stats={
                    "estimated_request_units": 3,
                    "queries_attempted": 1,
                    "queried_search_roles": ["Role 3"],
                    "topup_new_prefilter_viable": 1,
                    "topup_stop_reason": "target_reached",
                    "query_metrics": {},
                },
            )
            filtered = root / "filtered.json"
            filtered.write_text(json.dumps({"jobs": [{"job_id": "winner"}]}), encoding="utf-8")
            qualified = root / "qualified.json"
            qualified.write_text(json.dumps({"jobs": [{"job_id": "winner"}]}), encoding="utf-8")
            enriched_path = root / "enriched.json"
            enriched_path.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "job_id": "winner",
                                "lead_key": "winner",
                                "_final_state": "FINAL_PASS",
                                "_account_gate_state": "PASS",
                            }
                        ],
                        "processed_job_refs": [{"job_id": "winner"}],
                        "processed_company_keys": ["winner.com"],
                    }
                ),
                encoding="utf-8",
            )
            enriched = self._result(enriched_path, final_pass=1, company="winner.com")

            with (
                patch.object(config, "ACQUISITION_MODE", "multi_source"),
                patch.object(config, "STEP3_OUTPUT_DIR", str(root)),
                patch.object(config, "FILTERED_OUTPUT_DIR", str(root)),
                patch.object(config, "FINAL_PASS_MAX_TOPUP_ITERATIONS", 2),
                patch.object(config, "MULTI_SOURCE_FINAL_PASS_MAX_TOPUP_ITERATIONS", 0),
                patch.object(config, "MULTI_SOURCE_TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES", 4),
                patch.object(config, "FINAL_PASS_MAX_RUNTIME_SECONDS", 300),
                patch.object(config, "FINAL_PASS_MICROBATCH_QUERY_UNITS", 3),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 100),
                patch.object(
                    final_pass_topup,
                    "run_targeted_topup_scrape",
                    side_effect=[*zero_scrapes, viable_scrape],
                ) as scrape_mock,
                patch.object(
                    final_pass_topup,
                    "run_filter",
                    return_value=SimpleNamespace(
                        output_path=str(filtered),
                        kept_count=1,
                        rejected_count=0,
                        success=True,
                        errors=[],
                    ),
                ),
                patch.object(
                    final_pass_topup,
                    "run_precontact_qualification",
                    return_value=SimpleNamespace(
                        output_path=str(qualified),
                        nonpass_path="",
                        contact_eligible_jobs=1,
                        rejected_jobs=0,
                        unverified_jobs=0,
                    ),
                ),
                patch.object(
                    final_pass_topup,
                    "run_hiring_manager_identification",
                    return_value=enriched,
                ),
            ):
                combined, details = final_pass_topup.run_final_pass_topup(
                    initial_scrape=initial_scrape,
                    initial_enriched=initial_result,
                    registry=SeenJobsRegistry(path=str(root / "seen.json")),
                    target_final_pass_leads=1,
                    max_eligible_companies=0,
                )

        self.assertEqual(scrape_mock.call_count, 3)
        self.assertEqual(combined.final_pass_leads, 1)
        self.assertEqual(details["stop_reason"], "final_pass_target_reached")
        self.assertEqual(details["iteration_limit"], 0)


if __name__ == "__main__":
    unittest.main()
