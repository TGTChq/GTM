from __future__ import annotations

import unittest
from unittest.mock import patch

import airtable_client
import apollo_client
import config
import hiring_manager
import job_filter


class GeographyQualityTests(unittest.TestCase):
    def _job(self, **overrides):
        job = {
            "job_title": "Accountant",
            "job_description": "",
            "job_location": "Anywhere",
            "job_country": "US",
            "job_is_remote": True,
            "job_employment_type": "Full-time",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_apply_link": "https://example.com/jobs/123",
            "_matched_role": "Accountant",
        }
        job.update(overrides)
        return job

    def test_anywhere_country_echo_is_not_us_evidence(self):
        assessment = job_filter.assess_pre_enrichment_viability(self._job())
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_us")
        self.assertEqual(
            assessment.reason, "ambiguous_remote_location_without_us_evidence"
        )

    def test_remote_us_title_is_accepted_and_location_is_normalized(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Technical Support Representative (Remote U.S.)")
        )
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.geography.display_location, "Remote, United States")

    def test_state_in_title_recovers_original_location(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title=(
                    "DevOps Engineer III, AI and Business Automation job at "
                    "TrueNAS in Campbell, CA"
                ),
                _matched_role="DevOps Engineer",
            )
        )
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.geography.display_location, "Campbell, CA")

    def test_georgia_without_us_corroboration_is_ambiguous(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Junior SEO Specialist [Georgia]")
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_us")

    def test_foreign_city_in_url_overrides_country_echo(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Go/Python Backend Engineer - Remote & High-Load",
                job_apply_link=(
                    "https://www.jobleads.com/us/job/go-python-backend-engineer-"
                    "remote-high-load--warsaw--abc"
                ),
                _matched_role="Backend Developer",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertIn("warsaw", assessment.reason)


class EmploymentQualityTests(unittest.TestCase):
    def _job(self, **overrides):
        job = {
            "job_title": "SEO Specialist",
            "job_description": "",
            "job_location": "Remote",
            "job_country": "US",
            "job_is_remote": True,
            "job_employment_type": "Full-time",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "_matched_role": "SEO Specialist",
        }
        job.update(overrides)
        return job

    def test_part_time_provider_label_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Accountant",
                job_employment_type="Part-time",
                _matched_role="Accountant",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_full_time")

    def test_contractor_provider_label_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Product Analyst",
                job_employment_type="Contractor",
                _matched_role="Product Analyst",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_full_time")

    def test_hidden_weekly_hours_override_full_time_label(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Remote U.S. SEO Specialist, 15+ hrs/wk")
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_full_time")

    def test_future_opening_is_not_current_hiring_intent(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Client Success Manager - Future Openings",
                _matched_role="Customer Success Manager",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_active")


class EmployerQualityTests(unittest.TestCase):
    def test_observed_recruiting_platforms_are_rejected_before_apollo(self):
        for company in ("RecXchange", "Qureos Inc", "Zillion Technologies, Inc."):
            with self.subTest(company=company):
                rejected, reason = job_filter.is_staffing_company(
                    {"employer_name": company, "job_description": ""}
                )
                self.assertTrue(rejected)
                self.assertIn("known_staffing_employer", reason)

    def test_mental_health_industry_is_rejected_after_apollo(self):
        org = apollo_client.OrgEnrichment(
            found=True,
            name="The Treetop ABA",
            domain="thetreetop.com",
            employee_count=200,
            industry="mental health care",
        )
        eligible, reason, needs_review = hiring_manager.passes_company_criteria(
            org, "The Treetop ABA"
        )
        self.assertFalse(eligible)
        self.assertIn("excluded_apollo_industry", reason)
        self.assertFalse(needs_review)

    def test_founder_fallback_is_blocked_for_mid_market_company(self):
        job = {
            "job_id": "ops-1",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_title": "Remote U.S. Operations Analyst",
            "job_description": "Improve business operations and reporting.",
            "job_location": "Remote",
            "job_country": "US",
            "job_is_remote": True,
            "job_employment_type": "Full-time",
            "_matched_role": "Operations Analyst",
            "_role_relevance_score": 100,
            "_role_relevance_status": "accept",
        }
        org = apollo_client.OrgEnrichment(
            found=True,
            name="Acme",
            domain="acme.com",
            employee_count=200,
            industry="software",
        )
        people = [{
            "id": "founder-1",
            "title": "Co-Founder and CEO",
            "organization": {"name": "Acme", "primary_domain": "acme.com"},
        }]
        with (
            patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
            patch.object(hiring_manager.apollo, "search_people_at_company", return_value=people),
            patch.object(hiring_manager.apollo, "match_person") as match_person,
            patch.object(hiring_manager.time, "sleep", return_value=None),
        ):
            leads, stats = hiring_manager.process_company([job])

        match_person.assert_not_called()
        self.assertEqual(leads[0]["_step3_status"], "not_found")
        self.assertEqual(
            leads[0]["_step3_reason"],
            "founder_fallback_disallowed_for_company_size",
        )
        self.assertEqual(stats["candidate_founder_fallback_disallowed"], 1)


class AirtableQualityTests(unittest.TestCase):
    def test_existing_company_is_suppressed_even_with_new_lead_key(self):
        existing = {
            "rubylabs.com|old@rubylabs.com|marketing": {
                "id": "rec1",
                "fields": {
                    "Lead Key": "rubylabs.com|old@rubylabs.com|marketing",
                    "Company": "Ruby Labs",
                    "Website": "https://rubylabs.com",
                    "Status": "Enrolled",
                },
            }
        }
        existing_keys = airtable_client._active_existing_company_keys(existing)
        new_job_keys = airtable_client._company_identity_keys_from_job(
            {
                "employer_name": "Ruby Labs",
                "company_domain": "rubylabs.com",
            }
        )
        self.assertTrue(existing_keys & new_job_keys)

    def test_rejected_company_can_reenter_with_new_qualified_job(self):
        existing = {
            "acme.com|old@acme.com|marketing": {
                "id": "rec2",
                "fields": {
                    "Lead Key": "acme.com|old@acme.com|marketing",
                    "Company": "Acme",
                    "Website": "https://acme.com",
                    "Status": "Rejected",
                },
            }
        }
        existing_keys = airtable_client._active_existing_company_keys(existing)
        new_job_keys = airtable_client._company_identity_keys_from_job(
            {
                "employer_name": "Acme",
                "company_domain": "acme.com",
            }
        )
        self.assertFalse(existing_keys & new_job_keys)

    def test_airtable_location_uses_recovered_location(self):
        fields = airtable_client._job_to_fields(
            {
                "lead_key": "acme.com|manager@acme.com|engineering",
                "employer_name": "Acme",
                "company_domain": "acme.com",
                "job_title": "Remote DevOps Engineer in Campbell, CA",
                "job_location": "Anywhere",
                "_normalized_location": "Campbell, CA",
                "_us_eligibility_reason": "explicit_us_state",
                "_employment_quality_reason": "explicit_full_time",
                "job_employment_type": "Full-time",
                "hiring_manager_name": "Jamie Lee",
                "hiring_manager_title": "Engineering Manager",
                "hiring_manager_email": "jamie@acme.com",
                "hiring_manager_confidence": "high",
            }
        )
        self.assertEqual(fields["Location"], "Campbell, CA")
        self.assertIn("us_evidence=explicit_us_state", fields["Job Signal Notes"])


if __name__ == "__main__":
    unittest.main()
