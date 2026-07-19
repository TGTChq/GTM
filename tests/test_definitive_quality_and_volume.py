from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import job_filter
import job_quality
import jsearch_scraper
from pipeline_state import SeenJobsRegistry
from role_catalog import DEFAULT_SEARCH_ROLES


class DefinitiveQualityGuardTests(unittest.TestCase):
    def _job(self, **overrides):
        job = {
            "job_id": "job-1",
            "job_title": "Remote U.S. Account Executive",
            "job_description": "Own pipeline, prospecting, and revenue targets.",
            "job_location": "Remote",
            "job_country": "US",
            "job_is_remote": True,
            "job_employment_type": "Full-time",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_publisher": "LinkedIn",
            "job_apply_link": "https://linkedin.com/jobs/view/1",
            "_matched_role": "Account Executive",
        }
        job.update(overrides)
        return job

    def test_skillbridge_family_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="SkillBridge Extern - Recruiting Coordinator",
                _matched_role="Recruiting Coordinator",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_apprenticeship_eeo_boilerplate_does_not_reject_normal_role(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Data Scientist",
                _matched_role="Data Scientist",
                job_description=(
                    "Build predictive models. Equal opportunity applies to selection "
                    "for training, including apprenticeship."
                ),
            )
        )
        self.assertTrue(assessment.eligible)

    def test_actual_apprenticeship_title_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Data Analyst Apprenticeship", _matched_role="Data Analyst")
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_security_clearance_family_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Customer Success Manager (Top Secret)",
                _matched_role="Customer Success Manager",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_federal_agency_delivery_role_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Automation Engineer",
                _matched_role="Automation Specialist",
                job_description="This role supports a federal agency with Azure automation.",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_broad_federal_program_support_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Technical Recruiter",
                _matched_role="Recruiter",
                job_description=(
                    "This position supports a growing portfolio of federal government "
                    "healthcare technology programs."
                ),
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_direct_support_for_federal_agency_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Azure AI Automation Engineer",
                _matched_role="AI Automation Engineer",
                job_description="Manage Azure resources to support AI initiatives for a federal agency.",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_mission_driven_nonprofit_dot_org_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Graphic Designer",
                _matched_role="Graphic Designer",
                employer_website="https://mission.example.org",
                job_description=(
                    "Join a faith-based start-up in the nonprofit space. "
                    "Employment requires adherence to our statement of faith."
                ),
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_industry")

    def test_commercial_dot_org_without_mission_signal_is_not_auto_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(employer_website="https://product.example.org")
        )
        self.assertTrue(assessment.eligible)

    def test_pr_account_executive_is_not_treated_as_sales(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Public Relations Account Executive",
                job_description="Manage press releases, media relations, and client communications.",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_contextual_mismatch")

    def test_sales_account_executive_with_pr_customer_is_kept(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Account Executive",
                job_description=(
                    "Own quota, prospecting, pipeline, and the full sales cycle for PR agencies."
                ),
            )
        )
        self.assertTrue(assessment.eligible)

    def test_inventory_role_is_not_product_support(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Inventory Optimization & Product Support Specialist",
                job_description="Manage warehouse inventory and product data accuracy.",
                _matched_role="Product Support Specialist",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_contextual_mismatch")

    def test_true_customer_product_support_is_kept(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Product Support Specialist",
                job_description="Troubleshoot customer tickets and provide product education.",
                _matched_role="Product Support Specialist",
            )
        )
        self.assertTrue(assessment.eligible)

    def test_graphic_designer_mislabeled_as_video_editor_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Video Editor",
                job_description="Create logos, typography, and still designs in Photoshop and Illustrator.",
                _matched_role="Video Editor",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_contextual_mismatch")

    def test_real_video_editor_is_kept(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Video Editor",
                job_description="Edit footage in Premiere Pro and After Effects for social video.",
                _matched_role="Video Editor",
            )
        )
        self.assertTrue(assessment.eligible)

    def test_freelance_hidden_in_description_overrides_full_time_label(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Content Writer",
                job_description="We are seeking a Freelance Content Writer for client projects.",
                _matched_role="Content Writer",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_full_time")

    def test_multi_job_roundup_is_rejected(self):
        description = "\n".join(
            [
                "Company: Alpha\nLocation: Remote\nJob Title: Accountant",
                "Company: Beta\nLocation: Remote\nJob Title: Designer",
                "Company: Gamma\nLocation: Remote\nJob Title: Recruiter",
            ]
        )
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_description=description)
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_posting_integrity")

    def test_expired_embedded_application_deadline_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_description="Application Deadline: Friday, Feb 21, 2025")
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_posting_integrity")

    def test_outsourcing_company_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(employer_name="Concentrix")
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_outsourcing")

    def test_staffing_company_added_from_production_is_rejected(self):
        matched, reason = job_filter.is_staffing_company(
            {"employer_name": "INSPYR Solutions", "job_description": ""}
        )
        self.assertTrue(matched)
        self.assertIn("known_staffing_employer", reason)

    def test_ats_wrapper_is_repaired_without_using_ats_as_employer(self):
        job = self._job(
            employer_name="Travelopia Group ATS",
            job_publisher="Travelopia Group",
        )
        job_quality.normalize_job_identity(job)
        self.assertEqual(job["employer_name"], "Travelopia Group")


class DefinitiveGeographyTests(unittest.TestCase):
    def _job(self, **overrides):
        job = {
            "job_id": "geo-1",
            "job_title": "Remote Account Executive",
            "job_description": "Full-time remote role.",
            "job_location": "Remote",
            "job_country": "US",
            "job_is_remote": True,
            "job_employment_type": "Full-time",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "_matched_role": "Account Executive",
        }
        job.update(overrides)
        return job

    def test_remote_ok_is_not_oklahoma(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Strategic Account Manager (REMOTE OK)", _matched_role="Account Manager")
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_us")

    def test_pr_in_role_title_is_not_puerto_rico(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote-First PR Account Executive for Bold Brands",
                job_description="Manage media relations and press releases.",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertNotEqual(assessment.geography.display_location, "Remote, PR")

    def test_explicit_city_state_in_title_is_recovered(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Remote Go-to-Market Engineer - Los Angeles, CA, USA", _matched_role="GTM Engineer")
        )
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.geography.display_location, "Los Angeles, CA")

    def test_delimiter_bounded_us_title_scope_is_valid_evidence(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Applied AI Engineer - US", _matched_role="AI Engineer")
        )
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.geography.reason, "explicit_us_title")

    def test_lowercase_us_pronoun_is_not_geographic_evidence(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_title="Join us - Remote Account Executive")
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_us")

    def test_structured_united_states_location_is_valid_evidence(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(job_location="United States")
        )
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.geography.reason, "explicit_us_location")


class AdaptiveLookbackVolumeTests(unittest.TestCase):
    def test_full_catalog_uses_reserved_week_lookback_without_weakening_filters(self):
        roles = list(DEFAULT_SEARCH_ROLES[:100])
        calls = []

        def viable(job_id, role, company):
            return {
                "job_id": job_id,
                "job_title": role,
                "job_description": "Fully remote role anywhere in the United States.",
                "job_location": "Remote",
                "job_country": "US",
                "job_is_remote": True,
                "job_employment_type": "Full-time",
                "employer_name": company,
                "employer_website": f"https://{company.lower().replace(' ', '')}.com",
            }

        def fake_fetch(role: str, **kwargs):
            calls.append((role, kwargs))
            if kwargs.get("date_posted") == "week":
                return [viable(f"week-{role}", role, f"Week {role}")]
            if role == roles[0]:
                return [viable("today-1", role, "Today Co")]
            return []

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "ROLES", roles),
                patch.object(config, "OUTPUT_DIR", directory),
                patch.object(config, "NUM_PAGES", 1),
                patch.object(config, "JSEARCH_MAX_QUERIES_PER_RUN", 0),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 102),
                patch.object(config, "JSEARCH_ADAPTIVE_DEEPENING", True),
                patch.object(config, "JSEARCH_MAX_EXTRA_PAGES_PER_ROLE", 1),
                patch.object(config, "JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES", 0),
                patch.object(config, "JSEARCH_ADAPTIVE_LOOKBACK", True),
                patch.object(config, "JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES", 2),
                patch.object(config, "JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED", "week"),
                patch.object(config, "JSEARCH_TARGET_PREFILTER_VIABLE", 3),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "MIN_JOBS_PER_RUN", 0),
                patch.object(config, "MIN_ROLES_WITH_RESULTS", 0),
                patch.object(config, "MAX_ROLE_FAILURES", 100),
                patch.object(config, "PRODUCTION", True),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
            ):
                result = jsearch_scraper.run_daily_scrape(
                    SeenJobsRegistry(path=str(Path(directory) / "seen.json"))
                )

        lookback_calls = [kwargs for _role, kwargs in calls if kwargs.get("date_posted") == "week"]
        self.assertEqual(result.stats["adaptive_lookback_queries"], 2)
        self.assertEqual(result.stats["adaptive_lookback_prefilter_viable_added"], 2)
        self.assertEqual(len(lookback_calls), 2)
        self.assertTrue(all(call.get("intent_variant") for call in lookback_calls))
        self.assertLessEqual(result.stats["estimated_request_units"], 102)


if __name__ == "__main__":
    unittest.main()
