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

    def test_arcadia_360_recruitment_business_model_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Arcadia",
                employer_website="https://arcadia.com",
                job_title="360 Recruiter (US Remote)",
                _matched_role="Recruiter",
                job_description=(
                    "Arcadia is a high-performance recruitment firm helping scale "
                    "teams across technology, finance, and executive leadership. "
                    "Own and grow a 360 desk. Win and manage client accounts. "
                    "Source, pitch, and close top-tier candidates and deliver placements."
                ),
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_staffing")

    def test_internal_saas_recruiter_is_not_mistaken_for_staffing(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Acme Software",
                employer_website="https://acme.com",
                job_title="Remote U.S. Technical Recruiter",
                _matched_role="Recruiter",
                job_description=(
                    "Join our internal people team. Partner with Acme hiring managers, "
                    "manage our candidate pipeline, and recruit engineers for our own product teams."
                ),
            )
        )
        self.assertTrue(assessment.eligible)

    def test_staffing_business_model_variations_are_rejected(self):
        descriptions = [
            "We are an executive search consultancy delivering placements to clients.",
            "Northstar is a boutique hiring consultancy serving clients and candidates.",
            "Build a 360 desk, grow client accounts, and close candidate placements.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                assessment = job_filter.assess_pre_enrichment_viability(
                    self._job(
                        employer_name="Northstar",
                        employer_website="https://northstar.example",
                        job_title="Remote U.S. Recruiter",
                        _matched_role="Recruiter",
                        job_description=description,
                    )
                )
                self.assertFalse(assessment.eligible)
                self.assertEqual(assessment.stat_name, "excluded_staffing")

    def test_veterans_affairs_project_delivery_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Mind Computing",
                employer_website="https://mindcomputing.com",
                job_title="Full Stack Developer (Remote Opportunity)",
                _matched_role="Full Stack Developer",
                job_description=(
                    "Mind Computing is seeking a fulltime, 100% remote Full Stack Developer "
                    "to support a project with the Department of Veterans Affairs."
                ),
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_incidental_government_customer_reference_is_kept(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Customer Success Manager",
                _matched_role="Customer Success Manager",
                job_description=(
                    "Own onboarding and renewals for commercial healthcare companies. "
                    "Our software is also used by some federal government customers."
                ),
            )
        )
        self.assertTrue(assessment.eligible)

    def test_named_federal_delivery_variations_are_rejected(self):
        descriptions = [
            "This contract supports the Veterans Health Administration.",
            "The engagement will deliver cloud services to the Department of Defense.",
            "Work on a program for the Department of Homeland Security.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                assessment = job_filter.assess_pre_enrichment_viability(
                    self._job(
                        job_title="Remote U.S. Full Stack Developer",
                        _matched_role="Full Stack Developer",
                        job_description=description,
                    )
                )
                self.assertFalse(assessment.eligible)
                self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_lowercase_va_state_reference_is_not_federal_agency(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. Customer Success Manager",
                _matched_role="Customer Success Manager",
                job_description="Support customers across Richmond, va and the Mid-Atlantic region.",
            )
        )
        self.assertTrue(assessment.eligible)

    def test_stampli_three_day_office_schedule_remains_valid_demand(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Stampli",
                employer_website="https://stampli.com",
                job_title="Strategic Finance & FP&A Analyst",
                _matched_role="Financial Analyst",
                job_location="Mountain View, CA",
                job_description=(
                    "This role offers the flexibility of working from our Mountain View, CA "
                    "office three days a week (Tuesday, Wednesday and Thursday), with the "
                    "option to work remotely for the remainder of the week."
                ),
            )
        )
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.work_arrangement.status, "hybrid")

    def test_optional_office_access_does_not_reject_fully_remote_role(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_description=(
                    "This is a fully remote role anywhere in the United States. "
                    "Employees may use our office whenever they choose."
                )
            )
        )
        self.assertTrue(assessment.eligible)

    def test_hybrid_requirement_variations_are_valid_demand(self):
        descriptions = [
            "Expected to work from our New York office two days per week.",
            "The role is remote Monday and Friday, with required in-office work Tuesday through Thursday.",
            "Mandatory in-office attendance three days per week.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                assessment = job_filter.assess_pre_enrichment_viability(
                    self._job(job_description=description)
                )
                self.assertTrue(assessment.eligible)
                self.assertIn(assessment.work_arrangement.status, {"hybrid", "onsite"})

    def test_four_month_coop_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="East Side Games",
                employer_website="https://eastsidegames.com",
                job_title="QA Analyst Co-op",
                _matched_role="QA Analyst",
                job_description=(
                    "This is a four-month co-op work term. Candidates must be currently "
                    "enrolled and return to school after the placement."
                ),
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_permanent_role_with_training_is_not_mistaken_for_coop(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. QA Analyst",
                _matched_role="QA Analyst",
                job_description="Permanent full-time role with mentorship and ongoing training.",
            )
        )
        self.assertTrue(assessment.eligible)

    def test_academic_work_term_hidden_in_description_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                job_title="Remote U.S. QA Analyst",
                _matched_role="QA Analyst",
                job_description=(
                    "This student work term lasts 4 months. Applicants must be currently "
                    "enrolled and return to university after the semester placement."
                ),
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_restricted_role")

    def test_call_center_service_provider_is_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Frontline Call Center",
                employer_website="https://frontlinecallcenter.com",
                job_title="Remote Customer Service Representative",
                _matched_role="Customer Support Representative",
                job_description="Employees needed for full-time remote customer service work.",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_outsourcing")

    def test_internal_customer_support_team_is_kept(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Acme Software",
                employer_website="https://acme.com",
                job_title="Remote U.S. Customer Support Specialist",
                _matched_role="Customer Support Specialist",
                job_description=(
                    "Support Acme customers directly, troubleshoot product tickets, "
                    "and collaborate with our engineering team."
                ),
            )
        )
        self.assertTrue(assessment.eligible)

    def test_bpo_service_model_variations_are_rejected(self):
        descriptions = [
            "We deliver managed customer support services for clients.",
            "Our contact center agents are assigned to multiple client accounts.",
            "We provide outsourced customer service and back-office services for our clients.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                assessment = job_filter.assess_pre_enrichment_viability(
                    self._job(
                        employer_name="Service Operations Group",
                        employer_website="https://serviceoperations.example",
                        job_title="Remote Customer Support Representative",
                        _matched_role="Customer Support Representative",
                        job_description=description,
                    )
                )
                self.assertFalse(assessment.eligible)
                self.assertEqual(assessment.stat_name, "excluded_outsourcing")

    def test_colonist_community_tournaments_are_valid_role_work(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Colonist",
                employer_website="https://colonist.io",
                job_title="Remote Community Manager - Board Games",
                _matched_role="Community Manager",
                job_description=(
                    "Fully remote role anywhere in the United States. Moderate Discord, "
                    "create community content, and run weekly player tournaments."
                ),
            )
        )
        self.assertTrue(assessment.eligible)

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
        self.assertEqual(
            [call.get("query_variant") for call in lookback_calls],
            ["linkedin", "indeed"],
        )
        self.assertEqual(
            result.stats["adaptive_lookback_variant_counts"],
            {"linkedin": 1, "indeed": 1},
        )
        self.assertLessEqual(result.stats["estimated_request_units"], 102)




class OverfilterRecoveryTests(unittest.TestCase):
    def _job(self, **overrides):
        job = {
            "job_id": "recovery-1",
            "job_title": "Remote Account Executive",
            "job_description": "Own quota, pipeline, prospecting, and revenue targets.",
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

    def test_provider_confirmed_us_remote_survives_without_body_phrase(self):
        with patch.object(config, "ALLOW_PROVIDER_CONFIRMED_US_REMOTE", True):
            assessment = job_filter.assess_pre_enrichment_viability(
                self._job(
                    job_title="Account Executive",
                    job_description="Own quota and the full sales cycle.",
                    job_location="Remote",
                    _jsearch_country_filter="us",
                    _jsearch_remote_filter_applied=True,
                )
            )
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.geography.scope, "us_provider_confirmed")

    def test_provider_confirmation_never_overrides_explicit_foreign_scope(self):
        with patch.object(config, "ALLOW_PROVIDER_CONFIRMED_US_REMOTE", True):
            assessment = job_filter.assess_pre_enrichment_viability(
                self._job(
                    job_description="This remote position is available only to candidates in Canada.",
                    job_location="Remote",
                    _jsearch_country_filter="us",
                    _jsearch_remote_filter_applied=True,
                )
            )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_us")

    def test_provider_confirmation_never_overrides_global_scope(self):
        with patch.object(config, "ALLOW_PROVIDER_CONFIRMED_US_REMOTE", True):
            assessment = job_filter.assess_pre_enrichment_viability(
                self._job(
                    job_description="This is a work from anywhere global remote opportunity.",
                    job_location="Anywhere",
                    _jsearch_country_filter="us",
                    _jsearch_remote_filter_applied=True,
                )
            )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_non_us")

    def test_company_careers_wrapper_is_repaired_with_owned_domain(self):
        job = self._job(
            employer_name="Church & Dwight Careers",
            employer_website="https://careers.churchdwight.com",
            job_publisher="Church & Dwight Careers",
            job_apply_link="https://careers.churchdwight.com/jobs/123",
            job_description=(
                "Church & Dwight is hiring a full-time remote account executive "
                "in the United States to own quota and pipeline."
            ),
        )
        job_quality.normalize_job_identity(job)
        self.assertEqual(job["employer_name"], "Church & Dwight")
        self.assertEqual(job["_employer_name_normalization"], "removed_careers_wrapper")
        assessment = job_filter.assess_pre_enrichment_viability(job)
        self.assertNotEqual(assessment.stat_name, "excluded_aggregator")

    def test_aggregator_employer_is_recovered_from_direct_organization_evidence(self):
        job = self._job(
            employer_name="Remote Jobs",
            employer_website=None,
            job_publisher="Learn4Good",
            job_apply_link="https://learn4good.com/jobs/example",
            job_description=(
                "The Implementation Specialist supports delivery within Tekion's "
                "Professional Services organization. Tekion builds automotive retail "
                "software. This is a full-time remote role in the United States."
            ),
            _matched_role="Implementation Specialist",
        )
        job_quality.normalize_job_identity(job)
        self.assertEqual(job["employer_name"], "Tekion")
        self.assertTrue(job["_employer_identity_repaired"])
        assessment = job_filter.assess_pre_enrichment_viability(job)
        self.assertNotEqual(assessment.stat_name, "excluded_aggregator")

    def test_unresolved_aggregator_remains_rejected(self):
        assessment = job_filter.assess_pre_enrichment_viability(
            self._job(
                employer_name="Jobgether",
                employer_website="https://jobgether.com",
                job_publisher="Jobgether",
                job_apply_link="https://jobgether.com/offer/123",
                job_description="Our client is looking for a remote account executive.",
            )
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_aggregator")

    def test_generic_hosting_careers_wrapper_is_not_trusted(self):
        job = self._job(
            employer_name="Remote Zest Jobs",
            employer_website="https://remotezest.up.railway.app",
            job_publisher="Remote Zest Jobs",
            job_apply_link="https://remotezest.up.railway.app/job/1",
        )
        job_quality.normalize_job_identity(job)
        self.assertEqual(job["employer_name"], "Remote Zest Jobs")
        assessment = job_filter.assess_pre_enrichment_viability(job)
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.stat_name, "excluded_aggregator")


if __name__ == "__main__":
    unittest.main()
