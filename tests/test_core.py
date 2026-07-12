from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import instantly_client
import job_signal
import job_filter
import role_mapping
from pipeline_state import SeenJobsRegistry
from role_focus import extract_role_focus
from role_relevance import assess_role, normalize_relevance_score


class StaffingFilterTests(unittest.TestCase):
    def test_direct_employer_negation_is_not_staffing(self):
        job = {
            "employer_name": "Acme Software",
            "job_description": "No staffing agencies or third-party recruiter submissions, please.",
        }
        self.assertEqual(job_filter.is_staffing_company(job), (False, ""))

    def test_first_person_staffing_language_is_staffing(self):
        job = {
            "employer_name": "Acme Talent",
            "job_description": "We are a staffing agency working on behalf of our client.",
        }
        matched, _ = job_filter.is_staffing_company(job)
        self.assertTrue(matched)


class AggregatorFilterTests(unittest.TestCase):
    def test_known_aggregator_is_rejected(self):
        job = {
            "employer_name": "ChatGPT Jobs",
            "job_publisher": "ChatGPT Jobs",
            "employer_website": None,
            "job_description": "Find your next job.",
        }
        matched, reason = job_filter.is_job_aggregator_or_publisher(job)
        self.assertTrue(matched)
        self.assertIn("known_job_aggregator", reason)

    def test_generic_jobs_brand_requires_corroboration(self):
        job = {
            "employer_name": "Future AI Jobs",
            "job_publisher": "Future AI Jobs",
            "employer_website": None,
            "job_description": "Browse thousands of jobs on our job board.",
        }
        matched, reason = job_filter.is_job_aggregator_or_publisher(job)
        self.assertTrue(matched)
        self.assertIn("job", reason)

    def test_legitimate_company_with_jobs_word_is_not_rejected_on_name_alone(self):
        job = {
            "employer_name": "Good Jobs Company",
            "job_publisher": "LinkedIn",
            "employer_website": "https://goodjobscompany.com",
            "job_apply_link": "https://goodjobscompany.com/careers/123",
            "job_description": "We build workforce software for employers.",
        }
        self.assertEqual(job_filter.is_job_aggregator_or_publisher(job), (False, ""))


class GeographyTests(unittest.TestCase):
    def test_canada_coordinates_do_not_make_job_us(self):
        job = {
            "job_location": "Toronto, Canada",
            "job_latitude": 43.65,
            "job_longitude": -79.38,
        }
        self.assertFalse(job_filter.is_us_job(job)[0])

    def test_coordinates_alone_do_not_prove_us(self):
        job = {
            "job_location": "Remote",
            "job_latitude": 40.71,
            "job_longitude": -74.0,
        }
        self.assertFalse(job_filter.is_us_job(job)[0])

    def test_full_us_state_name_is_accepted(self):
        job = {"job_state": "California", "job_location": "Remote"}
        self.assertTrue(job_filter.is_us_job(job)[0])


class RoleRelevanceTests(unittest.TestCase):
    def test_industrial_automation_is_rejected(self):
        job = {
            "job_title": "Industrial Automation Specialist",
            "job_description": "PLC, SCADA and manufacturing controls.",
        }
        assessment = assess_role(job, "Automation Specialist")
        self.assertEqual(assessment.status, "reject")

    def test_n8n_automation_is_accepted(self):
        job = {
            "job_title": "Automation Specialist",
            "job_description": "Build n8n, Make and Zapier workflows and API integrations.",
        }
        assessment = assess_role(job, "Automation Specialist")
        self.assertEqual(assessment.status, "accept")

    def test_data_engineer_in_gtm_team_is_rejected(self):
        job = {
            "job_title": "Senior Data Engineer, GTM",
            "job_description": "Build analytics pipelines for the go-to-market organization.",
        }
        assessment = assess_role(job, "GTM Engineer")
        self.assertEqual(assessment.status, "reject")

    def test_customer_success_has_own_bucket(self):
        self.assertEqual(
            role_mapping.get_bucket_name("Customer Success Manager"),
            "customer_success",
        )
        self.assertIn(
            "Head of Customer Success",
            role_mapping.get_target_titles("Customer Success Manager"),
        )

    def test_relevance_points_are_presented_as_percentage(self):
        self.assertEqual(normalize_relevance_score(1), 13)
        self.assertEqual(normalize_relevance_score(3), 38)
        self.assertEqual(normalize_relevance_score(6), 75)
        self.assertEqual(normalize_relevance_score(7), 88)
        self.assertEqual(normalize_relevance_score(8), 100)


class RoleFocusTests(unittest.TestCase):
    def test_gtm_focus_is_canonical_fragment_not_raw_sentence(self):
        job = {
            "job_title": "GTM Engineer",
            "job_description": (
                "Own HubSpot CRM automation, lead routing, Clay enrichment, "
                "and our outbound sequencing infrastructure."
            ),
        }
        result = extract_role_focus(job, "GTM Engineer")
        self.assertEqual(result.quality, "specific")
        self.assertIn("CRM automation", result.text)
        self.assertIn("lead enrichment", result.text)
        self.assertFalse(result.text.endswith("."))
        self.assertNotIn("Own ", result.text)

    def test_no_evidence_requires_manual_review(self):
        job = {"job_title": "AI Engineer", "job_description": "General responsibilities."}
        result = extract_role_focus(job, "AI Engineer")
        self.assertEqual(result.quality, "manual_required")
        self.assertEqual(
            result.text,
            "AI systems, LLM integrations, and production automation",
        )
        self.assertEqual(result.evidence, ["fallback_from_role:AI Engineer"])

    def test_automation_routes_to_gtm_when_jd_is_revenue_focused(self):
        job = {
            "_matched_role": "Automation Specialist",
            "job_title": "Automation Specialist",
            "job_description": "Build HubSpot CRM automation, lead routing, and Clay enrichment workflows.",
        }
        self.assertEqual(role_mapping.get_bucket_name_for_job(job), "gtm_revenue")

    def test_automation_routes_to_engineering_when_jd_is_technical(self):
        job = {
            "_matched_role": "Automation Specialist",
            "job_title": "Automation Specialist",
            "job_description": "Build Python services and AI agents for production workflow automation.",
        }
        self.assertEqual(role_mapping.get_bucket_name_for_job(job), "engineering")


    def test_gtm_systems_job_adds_contextual_buyer_titles(self):
        job = {
            "_matched_role": "GTM Engineer",
            "job_title": "GTM Engineer",
            "job_description": "Own HubSpot CRM architecture and revenue systems integrations.",
        }
        titles = role_mapping.get_target_titles_for_job(job, employee_count=200)
        self.assertIn("Head of GTM Systems", titles)
        self.assertIn("Head of Revenue Systems", titles)
        self.assertIn("Head of RevOps", titles)


class InstantlyPayloadTests(unittest.TestCase):
    def test_payload_preserves_hiring_signal(self):
        record = {
            "id": "rec123",
            "fields": {
                "Campaign ID": "019f477c-c485-7ae2-ae15-d472df1ca09f",
                "Email": "jane@example.com",
                "Hiring Manager": "Jane Doe",
                "Company": "Example Inc",
                "HM Title": "Head of Marketing",
                "Website": "https://example.com",
                "Open Role": "Video Editor",
                "Open Roles": "Video Editor | Graphic Designer",
                "Role Focus": "short-form and social video, post-production, and motion graphics",
                "Matched Role": "Video Editor",
                "Role Bucket": "marketing",
                "Employees": 120,
                "Size Band": "mid",
                "Job URL": "https://example.com/job",
                "Posted At": datetime.now(timezone.utc).isoformat(),
                "Job Freshness": "fresh",
                "Job URL Status": "verified",
                "Job URL Source": "company",
            },
        }
        payload = instantly_client.airtable_record_to_lead(record)
        self.assertEqual(payload["company_name"], "Example Inc")
        self.assertEqual(payload["job_title"], "Head of Marketing")
        self.assertEqual(payload["custom_variables"]["open_role"], "Video Editor")
        self.assertEqual(
            payload["custom_variables"]["role_focus"],
            "short-form and social video, post-production, and motion graphics",
        )
        self.assertTrue(payload["skip_if_in_workspace"])
        self.assertTrue(payload["skip_if_in_campaign"])


class JobSignalTests(unittest.TestCase):
    def test_thirty_day_old_job_requires_review(self):
        now = datetime(2026, 7, 11, tzinfo=timezone.utc)
        job = {
            "job_posted_at_datetime_utc": (now - timedelta(days=30)).isoformat(),
        }
        freshness, age_days, _ = job_signal.classify_freshness(job, now=now)
        self.assertEqual(freshness, "stale_review")
        self.assertEqual(age_days, 30)

    def test_recent_job_is_fresh(self):
        now = datetime(2026, 7, 11, tzinfo=timezone.utc)
        job = {
            "job_posted_at_datetime_utc": (now - timedelta(days=3)).isoformat(),
        }
        freshness, age_days, _ = job_signal.classify_freshness(job, now=now)
        self.assertEqual(freshness, "fresh")
        self.assertEqual(age_days, 3)

    def test_conflicting_posted_dates_use_oldest_signal(self):
        now = datetime(2026, 7, 11, tzinfo=timezone.utc)
        job = {
            "job_posted_at_datetime_utc": (now - timedelta(days=3)).isoformat(),
            "job_posted_at": "30+ days ago",
        }
        freshness, age_days, reason = job_signal.classify_freshness(job, now=now)
        self.assertEqual(freshness, "stale_review")
        self.assertEqual(age_days, 30)
        self.assertIn("conflicting_posted_dates_used_oldest", reason)

    def test_broken_aggregator_uses_working_company_fallback(self):
        job = {
            "job_apply_link": "https://us.trabajo.org/job/123",
            "apply_options": [
                {"publisher": "Company", "apply_link": "https://careers.acme.com/jobs/123"}
            ],
            "employer_website": "https://acme.com",
        }

        def fake_probe(url, timeout=8.0):
            if "trabajo.org" in url:
                return "broken", "http_404"
            return "verified", "http_200"

        with patch.object(job_signal, "_probe_url", side_effect=fake_probe):
            selected, status, source, _ = job_signal.select_job_url(job, probe=True)

        self.assertEqual(selected, "https://careers.acme.com/jobs/123")
        self.assertEqual(status, "fallback_used")
        self.assertEqual(source, "company")

    def test_trabajo_is_never_used_as_review_url_even_when_http_200(self):
        job = {
            "job_apply_link": "https://us.trabajo.org/job/123",
            "employer_website": "https://acme.com",
        }
        with patch.object(job_signal, "_probe_url", return_value=("verified", "http_200")):
            selected, status, source, reason = job_signal.select_job_url(job, probe=True)
        self.assertEqual(selected, "")
        self.assertEqual(status, "unverified_review")
        self.assertEqual(source, "missing")
        self.assertIn("unreliable_partner_mirror", reason)

    def test_trabajo_is_skipped_when_stable_fallback_exists(self):
        job = {
            "job_apply_link": "https://us.trabajo.org/job/123",
            "apply_options": [
                {"publisher": "Company", "apply_link": "https://careers.acme.com/jobs/123"},
            ],
            "employer_website": "https://acme.com",
        }
        with patch.object(job_signal, "_probe_url", return_value=("verified", "http_200")):
            selected, status, source, _ = job_signal.select_job_url(job, probe=True)
        self.assertEqual(selected, "https://careers.acme.com/jobs/123")
        self.assertEqual(status, "fallback_used")
        self.assertEqual(source, "company")

    def test_other_working_aggregator_can_still_be_kept(self):
        job = {
            "job_apply_link": "https://www.jobleads.com/us/job/123",
            "employer_website": "https://acme.com",
        }
        with patch.object(job_signal, "_probe_url", return_value=("verified", "http_200")):
            selected, status, source, _ = job_signal.select_job_url(job, probe=True)
        self.assertEqual(selected, "https://www.jobleads.com/us/job/123")
        self.assertEqual(status, "verified")
        self.assertEqual(source, "aggregator")

    def test_legacy_aggregator_review_is_not_an_instantly_blocker(self):
        fields = {
            "Job Freshness": "fresh",
            "Job URL Status": "aggregator_review",
        }
        self.assertEqual(
            job_signal.enrollment_block_reason(fields, probe_missing=False),
            "",
        )

    def test_stale_job_is_not_an_instantly_blocker(self):
        fields = {
            "Job Freshness": "stale_review",
            "Job URL Status": "verified",
        }
        self.assertEqual(
            job_signal.enrollment_block_reason(fields, probe_missing=False),
            "",
        )

    def test_unknown_freshness_is_not_an_instantly_blocker(self):
        fields = {
            "Job Freshness": "unknown_review",
            "Job URL Status": "verified",
        }
        self.assertEqual(
            job_signal.enrollment_block_reason(fields, probe_missing=False),
            "",
        )

    def test_job_url_status_is_informational_not_an_instantly_blocker(self):
        fields = {
            "Job Freshness": "fresh",
            "Job URL Status": "broken",
        }
        self.assertEqual(
            job_signal.enrollment_block_reason(fields, probe_missing=False),
            "",
        )

    def test_google_jobs_viewer_is_never_selected_when_company_link_exists(self):
        google_url = (
            "https://www.google.com/search?ibp=htl;jobs&q&"
            "htidocid=o1Ho4HBr0v9yz_-CAAAAAA%3D%3D#fpstate=tldetail"
        )
        job = {
            "job_apply_link": google_url,
            "apply_options": [
                {
                    "publisher": "Acme Careers",
                    "apply_link": "https://careers.acme.com/jobs/123?utm_source=google_jobs_apply",
                    "is_direct": True,
                }
            ],
            "employer_website": "https://acme.com",
        }
        with patch.object(job_signal, "_probe_url", return_value=("verified", "http_200")):
            selected, status, source, _ = job_signal.select_job_url(job, probe=True)
        self.assertEqual(selected, "https://careers.acme.com/jobs/123")
        self.assertEqual(status, "fallback_used")
        self.assertEqual(source, "company")

    def test_google_jobs_viewer_is_not_saved_when_it_is_the_only_url(self):
        google_url = (
            "https://www.google.com/search?ibp=htl;jobs&q&"
            "htidocid=bWrID-CwG9u___hPAAAAAA%3D%3D#fpstate=tldetail"
        )
        selected, status, source, reason = job_signal.select_job_url(
            {"job_apply_link": google_url, "job_google_link": google_url},
            probe=False,
        )
        self.assertEqual(selected, "")
        self.assertEqual(status, "unverified_review")
        self.assertEqual(source, "missing")
        self.assertIn("only_google_jobs_viewer", reason)

    def test_official_company_link_beats_working_aggregator(self):
        job = {
            "job_apply_link": "https://us.trabajo.org/job/123",
            "apply_options": [
                {"publisher": "Acme Careers", "apply_link": "https://jobs.acme.com/123"}
            ],
            "employer_website": "https://acme.com",
        }
        with patch.object(job_signal, "_probe_url", return_value=("verified", "http_200")):
            selected, status, source, _ = job_signal.select_job_url(job, probe=True)
        self.assertEqual(selected, "https://jobs.acme.com/123")
        self.assertEqual(status, "fallback_used")
        self.assertEqual(source, "company")

    def test_tracking_parameters_are_removed_but_functional_parameters_remain(self):
        job = {
            "job_apply_link": (
                "https://careers.acme.com/job/123?ref=abc&"
                "utm_campaign=google_jobs_apply&utm_source=google_jobs_apply"
            ),
            "employer_website": "https://acme.com",
        }
        with patch.object(job_signal, "_probe_url", return_value=("verified", "http_200")):
            selected, _, _, _ = job_signal.select_job_url(job, probe=True)
        self.assertEqual(selected, "https://careers.acme.com/job/123?ref=abc")


class StateTests(unittest.TestCase):
    def test_registry_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "seen.json")
            registry = SeenJobsRegistry(path)
            registry.mark_jobs([
                {"job_id": "job1", "employer_name": "Acme", "job_title": "Video Editor"}
            ])
            reloaded = SeenJobsRegistry(path)
            self.assertTrue(reloaded.has_job_id("job1"))
            self.assertTrue(reloaded.has_dedup_key(("acme", "video editor")))


class DomainRecoveryTests(unittest.TestCase):
    def test_direct_careers_subdomain_recovers_company_domain(self):
        import hiring_manager

        job = {
            "job_apply_link": "https://careers.acme.com/jobs/123",
        }
        self.assertEqual(hiring_manager._domain_from_apply_link(job), "acme.com")

    def test_ats_domain_is_not_used_as_company_domain(self):
        import hiring_manager

        job = {
            "job_apply_link": "https://boards.greenhouse.io/acme/jobs/123",
        }
        self.assertEqual(hiring_manager._domain_from_apply_link(job), "")


class HiringManagerAggregationTests(unittest.TestCase):
    def test_same_company_bucket_becomes_one_lead_and_hunter_fallback_works(self):
        from unittest.mock import patch
        import apollo_client
        import hiring_manager
        import hunter_client

        jobs = [
            {
                "job_id": "1",
                "employer_name": "Acme",
                "employer_website": "https://acme.com",
                "job_title": "Video Editor",
                "job_description": "Edit short-form social video and motion graphics in Premiere Pro.",
                "_matched_role": "Video Editor",
                "_role_relevance_score": 8,
                "_role_relevance_status": "accept",
            },
            {
                "job_id": "2",
                "employer_name": "Acme",
                "employer_website": "https://acme.com",
                "job_title": "Graphic Designer",
                "job_description": "Create brand systems and campaign assets in Figma.",
                "_matched_role": "Graphic Designer",
                "_role_relevance_score": 7,
                "_role_relevance_status": "accept",
            },
        ]
        org = apollo_client.OrgEnrichment(
            found=True, domain="acme.com", employee_count=120, founded_year=2015
        )
        person = apollo_client.PersonMatch(
            person_found=True,
            email_found=False,
            person_id="p1",
            first_name="Jane",
            last_name="Doe",
            title="Head of Marketing",
            linkedin_url="https://linkedin.com/in/janedoe",
        )
        hunter_result = hunter_client.HunterResult(
            found=True,
            email="jane@acme.com",
            status="valid",
            score=95,
            source="hunter_finder",
        )
        with patch.object(hiring_manager.apollo, "enrich_organization", return_value=org), \
             patch.object(hiring_manager.apollo, "search_people_at_company", return_value=[{
                 "id": "p1", "first_name": "Jane", "last_name": "Doe",
                 "title": "Head of Marketing", "linkedin_url": "https://linkedin.com/in/janedoe",
                 "organization": {"primary_domain": "acme.com"},
             }]), \
             patch.object(hiring_manager.apollo, "match_person", return_value=person), \
             patch.object(hiring_manager.hunter, "find_email", return_value=hunter_result), \
             patch.object(hiring_manager.config, "HUNTER_API_KEY", "test-key"), \
             patch.object(hiring_manager.time, "sleep", return_value=None):
            leads, _ = hiring_manager.process_company(jobs)

        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["hiring_manager_email"], "jane@acme.com")
        self.assertEqual(leads[0]["hiring_manager_email_source"], "hunter")
        self.assertIn("short-form", leads[0]["role_focus"])
        self.assertEqual(leads[0]["role_focus_quality"], "specific")
        self.assertEqual(set(leads[0]["related_open_roles"]), {"Video Editor", "Graphic Designer"})


class JSearchRoleSelectionTests(unittest.TestCase):
    def test_duplicate_job_is_assigned_to_strongest_role(self):
        from unittest.mock import patch
        import jsearch_scraper

        shared = {
            "job_id": "abc",
            "job_title": "AI Engineer",
            "job_description": "Build LLM and RAG systems using Python and OpenAI.",
            "employer_name": "Acme",
            "job_country": "US",
        }

        def fake_fetch(role):
            return [dict(shared)] if role in {"GTM Engineer", "AI Engineer"} else []

        class EmptyRegistry:
            def has_job_id(self, _):
                return False

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(jsearch_scraper.config, "ROLES", ["GTM Engineer", "AI Engineer"]), \
             patch.object(jsearch_scraper.config, "RAPIDAPI_KEY", "test-key"), \
             patch.object(jsearch_scraper.config, "OUTPUT_DIR", directory), \
             patch.object(jsearch_scraper.config, "MIN_JOBS_PER_RUN", 0), \
             patch.object(jsearch_scraper.config, "MIN_ROLES_WITH_RESULTS", 0), \
             patch.object(jsearch_scraper.config, "PRODUCTION", False), \
             patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch), \
             patch.object(jsearch_scraper.time, "sleep", return_value=None):
            result = jsearch_scraper.run_daily_scrape(registry=EmptyRegistry())
            payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))

        self.assertEqual(len(payload["jobs"]), 1)
        self.assertEqual(payload["jobs"][0]["_matched_role"], "AI Engineer")


if __name__ == "__main__":
    unittest.main()
