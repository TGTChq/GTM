import unittest
from unittest.mock import patch
import requests

import config
import hunter_client as hunter
import job_filter


class FinalRootCausePatchTests(unittest.TestCase):
    def _job(self, **overrides):
        job = {
            "job_title": "Customer Success Manager",
            "job_description": "Acme is a software company. Acme is hiring a customer success manager for its US customers.",
            "job_location": "Anywhere",
            "job_country": None,
            "job_is_remote": True,
            "job_employment_type": "Full-time",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_publisher": "LinkedIn",
            "job_apply_link": "https://linkedin.com/jobs/1",
            "_matched_role": "Customer Success Manager",
            "_role_relevance_status": "accept",
            "_role_relevance_points": 6,
            "_jsearch_country_filter": "us",
            "_jsearch_remote_filter_applied": True,
        }
        job.update(overrides)
        return job

    def test_missing_country_is_accepted_only_with_us_remote_query_evidence(self):
        self.assertTrue(job_filter.assess_pre_enrichment_viability(self._job()).eligible)

    def test_explicit_foreign_scope_overrides_provider_us_query(self):
        result = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Customer Success Manager - APAC")
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.stat_name, "excluded_non_us")

    def test_fixed_term_contract_is_rejected(self):
        result = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Customer Success Manager - 12 month fixed-term contract")
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.stat_name, "excluded_non_full_time")

    def test_description_only_role_match_is_not_sent_to_enrichment(self):
        result = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title=".NET Web Developer",
                _matched_role="Technical Recruiter",
                _role_relevance_status="review",
                _role_relevance_points=2,
            )
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.stat_name, "excluded_role_mismatch")

    def test_corrupted_syndication_placeholder_is_rejected(self):
        result = job_filter.assess_pre_enrichment_viability(
            self._job(job_description="reputed company reputed company reputed company")
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.stat_name, "excluded_posting_integrity")

    def test_description_company_conflict_is_rejected(self):
        result = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Torentify",
                employer_website="https://torentify.com",
                job_description=(
                    "About the Company Great American Insurance Group is a leading insurer. "
                    "Great American Insurance Group supports specialty insurance operations."
                ),
            )
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "description_employer_identity_conflict")

    def test_talent_placement_business_model_is_rejected(self):
        result = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Smart Working",
                employer_website="https://smartworking.io",
                job_description=(
                    "Smart Working connects skilled professionals with global teams and helps "
                    "candidates find the right remote role."
                ),
            )
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.stat_name, "excluded_outsourcing")

    def test_hunter_billing_quota_opens_run_circuit(self):
        hunter._hunter_quota_exhausted_for_run = False
        response = requests.Response()
        response.status_code = 429
        response._content = b'billing period request limit exceeded'
        exc = requests.HTTPError("429", response=response)
        with (
            patch.object(config, "HUNTER_API_KEY", "test-key"),
            patch.object(hunter, "request_with_retry", side_effect=exc),
        ):
            first = hunter.verify_email("a@acme.com")
        self.assertFalse(first.found)
        self.assertTrue(hunter._hunter_quota_exhausted_for_run)
        with (
            patch.object(config, "HUNTER_API_KEY", "test-key"),
            patch.object(hunter, "request_with_retry") as request,
        ):
            second = hunter.find_email("A", "B", "acme.com")
        self.assertFalse(second.found)
        request.assert_not_called()
        hunter._hunter_quota_exhausted_for_run = False


if __name__ == "__main__":
    unittest.main()
