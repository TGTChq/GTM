from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import airtable_client
import apollo_client
import config
import hiring_manager
import job_filter
import jsearch_scraper
import role_mapping
from pipeline_state import SeenJobsRegistry
from role_catalog import DEFAULT_SEARCH_ROLES


class CompanyIdentityHardeningTests(unittest.TestCase):
    def test_job_board_domain_is_never_used_as_employer_domain(self):
        cases = [
            ("Kintsugi AI, Inc.", "builtin.com", "kintsugi ai"),
            ("Helios & Partners", "bebee.com", "helios partners"),
        ]
        for company_name, publisher_domain, expected_key in cases:
            with self.subTest(publisher_domain=publisher_domain):
                job = {
                    "employer_name": company_name,
                    "employer_website": f"https://{publisher_domain}",
                    "job_apply_link": f"https://jobs.{publisher_domain}/job/123",
                }
                self.assertEqual(
                    job_filter.get_safe_employer_domain(job),
                    ("", "employer_name_resolution_required"),
                )
                self.assertEqual(hiring_manager._best_input_domain(job), "")
                self.assertEqual(
                    job_filter.dedup_key({**job, "job_title": "GTM Engineer"})[0],
                    expected_key,
                )

    def test_name_only_apollo_resolution_rejects_wrong_organization(self):
        payload = {
            "organization": {
                "id": "org-built-in",
                "name": "Built In",
                "primary_domain": "builtin.com",
                "estimated_num_employees": 500,
            }
        }
        with patch.object(
            apollo_client, "_organization_enrichment_request", return_value=payload
        ):
            result = apollo_client.enrich_organization(name="Kintsugi AI, Inc.")
        self.assertFalse(result.found)
        self.assertIsNone(result.domain)

    def test_name_only_resolution_uses_real_company_domain_and_airtable_website(self):
        job = {
            "job_id": "kintsugi-job",
            "employer_name": "Kintsugi AI, Inc.",
            "employer_website": "https://builtin.com",
            "job_apply_link": "https://builtin.com/job/gtm-engineer/123",
            "job_title": "GTM Engineer (Tooling & Automations)",
            "job_description": "Build GTM systems, lead routing, enrichment, and outbound automation.",
            "_matched_role": "GTM Engineer",
            "_role_relevance_score": 100,
            "_role_relevance_status": "accept",
        }
        org = apollo_client.OrgEnrichment(
            found=True,
            name="Kintsugi AI, Inc.",
            domain="kintsugi.ai",
            employee_count=120,
            raw={"primary_domain": "kintsugi.ai"},
        )
        person = apollo_client.PersonMatch(
            person_found=True,
            email_found=True,
            person_id="p-kintsugi",
            first_name="Alex",
            last_name="Rivera",
            title="Director of Revenue Operations",
            organization_name="Kintsugi AI, Inc.",
            organization_domain="kintsugi.ai",
            email="alex@kintsugi.ai",
            email_status="verified",
            email_source="apollo",
        )
        people = [{
            "id": "p-kintsugi",
            "first_name": "Alex",
            "last_name": "Rivera",
            "title": "Director of Revenue Operations",
            "organization": {"name": "Kintsugi AI, Inc.", "primary_domain": "kintsugi.ai"},
        }]
        with (
            patch.object(hiring_manager.apollo, "enrich_organization", return_value=org) as enrich,
            patch.object(hiring_manager.apollo, "search_people_at_company", return_value=people) as search,
            patch.object(hiring_manager.apollo, "match_person", return_value=person),
            patch.object(hiring_manager.config, "VERIFY_WITH_HUNTER", False),
            patch.object(hiring_manager.time, "sleep", return_value=None),
        ):
            leads, stats = hiring_manager.process_company([job])

        enrich.assert_called_once_with(domain="", name="Kintsugi AI, Inc.", website="")
        search.assert_called_once()
        self.assertEqual(search.call_args.args[0], "kintsugi.ai")
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["company_domain"], "kintsugi.ai")
        self.assertEqual(leads[0]["hiring_manager_email"], "alex@kintsugi.ai")
        self.assertTrue(leads[0]["lead_key"].startswith("kintsugi.ai|"))
        self.assertEqual(stats["company_domain_resolved_by_name"], 1)
        fields = airtable_client._job_to_fields(leads[0])
        self.assertEqual(fields["Website"], "https://kintsugi.ai")

    def test_wrong_email_domain_is_skipped_and_next_ranked_manager_is_used(self):
        job = {
            "job_id": "qa-job",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_title": "QA Engineer",
            "job_description": "Own software quality, automation, and release testing.",
            "_matched_role": "QA Engineer",
            "_role_relevance_score": 100,
            "_role_relevance_status": "accept",
        }
        org = apollo_client.OrgEnrichment(
            found=True,
            name="Acme",
            domain="acme.com",
            employee_count=200,
            raw={"primary_domain": "acme.com"},
        )
        people = [
            {"id": "p1", "title": "QA Manager", "organization": {"primary_domain": "acme.com"}},
            {"id": "p2", "title": "Head of QA", "organization": {"primary_domain": "acme.com"}},
        ]
        wrong = apollo_client.PersonMatch(
            person_found=True,
            email_found=True,
            person_id="p1",
            first_name="Wrong",
            last_name="Board",
            title="QA Manager",
            organization_domain="acme.com",
            email="wrong@bebee.com",
            email_status="verified",
            email_source="apollo",
        )
        correct = apollo_client.PersonMatch(
            person_found=True,
            email_found=True,
            person_id="p2",
            first_name="Jamie",
            last_name="Lee",
            title="Head of QA",
            organization_domain="acme.com",
            email="jamie@acme.com",
            email_status="verified",
            email_source="apollo",
        )
        with (
            patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
            patch.object(hiring_manager.apollo, "search_people_at_company", return_value=people),
            patch.object(hiring_manager.apollo, "match_person", side_effect=[wrong, correct]),
            patch.object(hiring_manager.config, "VERIFY_WITH_HUNTER", False),
            patch.object(hiring_manager.config, "APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET", 3),
            patch.object(hiring_manager.time, "sleep", return_value=None),
        ):
            leads, stats = hiring_manager.process_company([job])

        self.assertEqual(leads[0]["hiring_manager_email"], "jamie@acme.com")
        self.assertEqual(leads[0]["hiring_manager_title"], "Head of QA")
        self.assertEqual(stats["candidate_email_domain_mismatch"], 1)
        self.assertEqual(stats["person_match_attempts"], 2)


    def test_person_with_mismatched_current_company_is_not_counted_as_identified(self):
        job = {
            "job_id": "product-job",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_title": "Product Designer",
            "job_description": "Design product experiences and interfaces.",
            "_matched_role": "Product Designer",
            "_role_relevance_score": 100,
            "_role_relevance_status": "accept",
        }
        org = apollo_client.OrgEnrichment(
            found=True, name="Acme", domain="acme.com", employee_count=200
        )
        people = [{
            "id": "wrong-person",
            "title": "Director of Product Design",
            "organization": {"name": "Other Company"},
        }]
        wrong_company = apollo_client.PersonMatch(
            person_found=True,
            email_found=True,
            person_id="wrong-person",
            first_name="Taylor",
            last_name="Wrong",
            title="Director of Product Design",
            organization_name="Other Company",
            email="taylor@acme.com",
            email_status="verified",
        )
        with (
            patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
            patch.object(hiring_manager.apollo, "search_people_at_company", return_value=people),
            patch.object(hiring_manager.apollo, "match_person", return_value=wrong_company),
            patch.object(hiring_manager.config, "VERIFY_WITH_HUNTER", False),
            patch.object(hiring_manager.time, "sleep", return_value=None),
        ):
            leads, stats = hiring_manager.process_company([job])

        self.assertIsNone(leads[0].get("hiring_manager_name"))
        self.assertIsNone(leads[0].get("hiring_manager_email"))
        self.assertEqual(leads[0]["_step3_reason"], "candidate_organization_domain_mismatch")
        self.assertEqual(stats["candidate_organization_domain_mismatch"], 1)

    def test_direct_manager_titles_precede_founder_even_for_small_company(self):
        titles = role_mapping.get_target_titles_for_job(
            {"_matched_role": "QA Engineer", "job_title": "QA Engineer"},
            employee_count=12,
        )
        self.assertLess(titles.index("QA Manager"), titles.index("Founder"))
        self.assertEqual(titles[-3:], ["Founder", "Co-Founder", "CEO"])


class AdaptiveJSearchTests(unittest.TestCase):
    def test_unused_budget_deepens_only_current_high_yield_roles(self):
        roles = list(DEFAULT_SEARCH_ROLES[:100])
        high_yield = set(roles[:2])
        calls: list[tuple[str, int]] = []

        def fake_fetch(role: str, *, page: int = 1, num_pages=None):
            calls.append((role, page))
            if role not in high_yield:
                return []
            return [{
                "job_id": f"{role}-{page}",
                "job_title": role,
                "job_description": f"Work as a {role} in a fully remote US role.",
                "employer_name": f"Employer {role}",
                "job_country": "US",
                "job_is_remote": True,
            }]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "ROLES", roles),
                patch.object(config, "OUTPUT_DIR", temp_dir),
                patch.object(config, "NUM_PAGES", 1),
                patch.object(config, "JSEARCH_MAX_QUERIES_PER_RUN", 0),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 102),
                patch.object(config, "JSEARCH_ADAPTIVE_DEEPENING", True),
                patch.object(config, "JSEARCH_MAX_EXTRA_PAGES_PER_ROLE", 1),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "MIN_JOBS_PER_RUN", 0),
                patch.object(config, "MIN_ROLES_WITH_RESULTS", 0),
                patch.object(config, "PRODUCTION", False),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
            ):
                result = jsearch_scraper.run_daily_scrape(
                    SeenJobsRegistry(path=str(Path(temp_dir) / "seen.json"))
                )

        self.assertEqual(result.stats["base_estimated_request_units"], 100)
        self.assertEqual(result.stats["adaptive_extra_queries"], 2)
        self.assertEqual(result.stats["estimated_request_units"], 102)
        self.assertEqual(set(result.stats["adaptive_deepened_roles"]), high_yield)
        self.assertEqual(len(calls), 102)
        self.assertEqual({role for role, page in calls if page == 2}, high_yield)


if __name__ == "__main__":
    unittest.main()
