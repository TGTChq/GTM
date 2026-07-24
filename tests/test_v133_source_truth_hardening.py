from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from ats_board_registry import fetch_board_jobs
from free_job_sources import FetchPayload
from job_fact_extractor import extract_job_facts
from job_filter import (
    assess_pre_enrichment_viability,
    is_excluded_industry,
    is_provider_firmographics_outside_target,
)
from job_quality import assess_outsourcing_intermediary
from job_source_resolver import ResolvedJobSource
from multi_source_acquisition import (
    _dedupe,
    _enrich_himalayas_company_profiles,
    _parse_himalayas_company_profile,
)


class AshbySourceTruthTests(unittest.TestCase):
    def _board(self):
        return {
            "provider": "ashby",
            "identifier": "replit",
            "company_name": "Replit",
            "company_domain": "replit.com",
        }

    def test_hybrid_workplace_overrides_is_remote_true(self):
        payload = {
            "jobs": [{
                "id": "job-1",
                "title": "Data Scientist, Trust & Safety",
                "location": "Foster City, CA",
                "isRemote": True,
                "workplaceType": "Hybrid",
                "employmentType": "FullTime",
                "publishedAt": "2026-07-20T23:41:31Z",
                "descriptionPlain": (
                    "This is a full-time role that can be held from our Foster City, CA office. "
                    "The role has an in-office requirement of Monday, Wednesday, and Friday."
                ),
                "jobUrl": "https://jobs.ashbyhq.com/replit/job-1",
                "isListed": True,
            }]
        }

        def fetcher(*_args, **_kwargs):
            return FetchPayload(200, "https://api.ashbyhq.com/test", json.dumps(payload))

        jobs, error = fetch_board_jobs(self._board(), fetcher)
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertFalse(job["job_is_remote"])
        self.assertEqual(job["work_arrangement"], "Hybrid")
        self.assertTrue(job["_provider_is_remote"])
        self.assertEqual(job["_provider_workplace_type"], "Hybrid")
        assessment = assess_pre_enrichment_viability(job)
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.work_arrangement.status, "hybrid")
        facts = extract_job_facts(job, ResolvedJobSource(
            state="ACTIVE_DIRECT_STRUCTURED",
            source_url=job["job_apply_link"],
            source_type="ashby",
            active=True,
            canonical_title=job["job_title"],
            canonical_employer=job["employer_name"],
            description=job["job_description"],
            location_text=job["job_location"],
            employment_type=job["job_employment_type"],
            official=True,
            corroborated=True,
        ))
        self.assertEqual(facts["work_arrangement"].value, "hybrid_required")

    def test_pure_remote_ashby_record_remains_remote(self):
        payload = {
            "jobs": [{
                "id": "job-2",
                "title": "Remote Software Engineer",
                "location": "Remote - United States",
                "isRemote": True,
                "workplaceType": "Remote",
                "employmentType": "FullTime",
                "publishedAt": "2026-07-23T12:00:00Z",
                "descriptionPlain": "This role is fully remote within the United States.",
                "jobUrl": "https://jobs.ashbyhq.com/replit/job-2",
                "isListed": True,
            }]
        }

        def fetcher(*_args, **_kwargs):
            return FetchPayload(200, "https://api.ashbyhq.com/test", json.dumps(payload))

        jobs, error = fetch_board_jobs(self._board(), fetcher)
        self.assertEqual(error, "")
        self.assertTrue(jobs[0]["job_is_remote"])
        self.assertEqual(jobs[0]["work_arrangement"], "Remote")


class HimalayasCompanyProfileTests(unittest.TestCase):
    SWBC_HTML = """
        <html><head><title>SWBC: Remote Jobs & Careers | Himalayas</title></head>
        <body><h1>SWBC</h1><p>SWBC is a diversified financial services company.</p>
        <h3>Company size</h3><p>1001-5000 employees</p>
        <a href="https://swbc.com/about">Visit swbc.com</a></body></html>
    """

    SHARECARE_HTML = """
        <html><head><title>Sharecare: Remote Jobs | Himalayas</title></head>
        <body><h1>Sharecare</h1>
        <p>Sharecare is a health and wellness engagement platform that helps people
        manage their healthcare and improve their well-being.</p>
        <p>501-1000 employees</p><a href="https://sharecare.com">Visit sharecare.com</a>
        </body></html>
    """

    def test_parser_extracts_verified_identity_website_and_size(self):
        profile = _parse_himalayas_company_profile(
            self.SWBC_HTML,
            company_name="SWBC",
            profile_url="https://himalayas.app/companies/swbc",
        )
        self.assertIsNotNone(profile)
        self.assertEqual(profile["website"], "https://swbc.com/")
        self.assertEqual(profile["employee_range"], "1001-5000")
        self.assertEqual(profile["employee_min"], 1001)
        self.assertEqual(profile["employee_max"], 5000)

    def test_profile_identity_mismatch_is_not_accepted(self):
        self.assertIsNone(_parse_himalayas_company_profile(
            self.SWBC_HTML,
            company_name="Unrelated Company",
            profile_url="https://himalayas.app/companies/swbc",
        ))

    def test_enrichment_groups_unique_slug_and_respects_request_cap(self):
        jobs = [
            {
                "job_id": "h:1",
                "job_title": "Payroll Specialist",
                "employer_name": "SWBC",
                "job_description": "Own payroll operations for a remote US team.",
                "job_apply_link": "https://himalayas.app/companies/swbc/jobs/1",
                "job_location": "Remote - United States",
                "job_country": "US",
                "job_is_remote": True,
                "job_employment_type": "Full Time",
                "job_posted_at_datetime_utc": "2026-07-23T12:00:00Z",
                "_acquisition_source": "himalayas",
                "_provider_record_structured": True,
                "_source_company_slug": "swbc",
            },
            {
                "job_id": "h:2",
                "job_title": "Customer Success Manager",
                "employer_name": "SWBC",
                "job_description": "Own customer onboarding for a remote US team.",
                "job_apply_link": "https://himalayas.app/companies/swbc/jobs/2",
                "job_location": "Remote - United States",
                "job_country": "US",
                "job_is_remote": True,
                "job_employment_type": "Full Time",
                "job_posted_at_datetime_utc": "2026-07-23T12:00:00Z",
                "_acquisition_source": "himalayas",
                "_provider_record_structured": True,
                "_source_company_slug": "swbc",
            },
        ]
        calls = []

        def fetcher(url, **_kwargs):
            calls.append(url)
            return FetchPayload(200, url, self.SWBC_HTML)

        with patch("config.HIMALAYAS_COMPANY_PROFILE_MAX_REQUESTS", 1):
            metrics = _enrich_himalayas_company_profiles(jobs, fetcher=fetcher)
        self.assertEqual(len(calls), 1)
        self.assertEqual(metrics["verified"], 1)
        self.assertEqual(metrics["jobs_enriched"], 2)
        self.assertTrue(all(job["employer_website"] == "https://swbc.com/" for job in jobs))

    def test_verified_range_rejects_company_clearly_above_icp(self):
        job = {
            "_provider_company_profile_verified": True,
            "_provider_employee_min": 1001,
            "_provider_employee_max": 5000,
        }
        rejected, reason = is_provider_firmographics_outside_target(job)
        self.assertTrue(rejected)
        self.assertEqual(reason, "provider_employee_range_above_max:1001-5000")

    def test_overlapping_employee_range_is_not_rejected(self):
        job = {
            "_provider_company_profile_verified": True,
            "_provider_employee_min": 11,
            "_provider_employee_max": 50,
        }
        self.assertEqual(is_provider_firmographics_outside_target(job), (False, ""))

    def test_verified_sharecare_profile_rejects_healthcare(self):
        profile = _parse_himalayas_company_profile(
            self.SHARECARE_HTML,
            company_name="Sharecare",
            profile_url="https://himalayas.app/companies/sharecare",
        )
        job = {
            "employer_name": "Sharecare",
            "job_title": "Customer Service Manager",
            "job_description": "Manage a customer service team.",
            "employer_website": profile["website"],
            "_provider_company_profile_verified": True,
            "_provider_company_profile_text": profile["profile_text"],
        }
        rejected, reason = is_excluded_industry(job)
        self.assertTrue(rejected)
        self.assertTrue(reason.startswith("excluded_industry_provider_profile:"))

    def test_profile_evidence_survives_when_official_ats_wins_dedupe(self):
        feed = {
            "job_id": "h:1",
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "employer_website": "https://acme.com/",
            "job_apply_link": "https://himalayas.app/companies/acme/jobs/csm",
            "_acquisition_source": "himalayas",
            "_provider_company_profile_verified": True,
            "_provider_company_profile_url": "https://himalayas.app/companies/acme",
            "_provider_company_profile_text": "Acme is a software company.",
            "_provider_employee_range": "1001-5000",
            "_provider_employee_min": 1001,
            "_provider_employee_max": 5000,
        }
        ats = {
            "job_id": "ats:ashby:acme:1",
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_apply_link": "https://jobs.ashbyhq.com/acme/1",
            "job_apply_is_direct": True,
            "_acquisition_source": "ats_ashby",
            "job_description": "Own onboarding and renewals.",
        }
        jobs, duplicates = _dedupe([feed, ats])
        self.assertEqual(duplicates, 1)
        self.assertEqual(jobs[0]["_acquisition_source"], "ats_ashby")
        self.assertTrue(jobs[0]["_provider_company_profile_verified"])
        self.assertEqual(jobs[0]["_provider_employee_min"], 1001)


class FreeRejectionRuleTests(unittest.TestCase):
    def test_camel_case_health_brand_is_detected(self):
        rejected, reason = is_excluded_industry({
            "employer_name": "UnitedHealth Group",
            "job_title": "Customer Service Representative",
            "job_description": "Support members.",
        })
        self.assertTrue(rejected)
        self.assertEqual(reason, "excluded_industry_employer:health")

    def test_peo_service_delivery_title_is_rejected(self):
        result = assess_outsourcing_intermediary({
            "employer_name": "Example Services",
            "job_title": "PEO Payroll Specialist I",
            "job_description": "Administer payroll services for client accounts.",
        })
        self.assertFalse(result.eligible)
        self.assertEqual(result.stat_name, "excluded_outsourcing")
        self.assertEqual(result.reason, "peo_service_delivery_role")


if __name__ == "__main__":
    unittest.main()
