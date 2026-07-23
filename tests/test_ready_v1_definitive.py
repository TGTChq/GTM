from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
import run_daily
from apollo_client import PersonMatch
from ats_public_adapters import discover_public_ats_urls
from decision_engine import annotate_final_decision
from decision_types import GateDecision, GateState
from email_gate import EmailGate
from job_gate import JobGate
from job_source_resolver import JobSourceResolver, ResolvedJobSource
from jsearch_scraper import _ingest_query_jobs
from qualification_pipeline import run_precontact_qualification
import recovery_inventory
from recovery_inventory import FinalPassInventory
from pipeline_lock import PipelineAlreadyRunningError, PipelineRunLock
from role_catalog import DEFAULT_ACQUISITION_ROLES


class ReadyV1SourceTests(unittest.TestCase):
    def test_company_discovery_always_tries_root_domain_after_subdomain_input(self):
        seen = []
        resolver = JobSourceResolver()

        def fetch(url, **_kwargs):
            seen.append(url)
            return {"status_code": 404, "final_url": url, "text": ""}

        resolver._fetch = fetch
        resolver._discover_company_job_urls(
            {
                "employer_website": "https://trainings.softwareone.com",
                "job_title": "Staff Accountant",
            },
            "softwareone.com",
        )
        self.assertIn("https://trainings.softwareone.com/", seen)
        self.assertIn("https://softwareone.com/", seen)

    def test_workday_direct_url_resolves_through_cxs_api(self):
        public = (
            "https://worldpay.wd5.myworkdayjobs.com/en-US/"
            "Worldpay_External_Careers_Site/job/Remote/Staff-Accountant_JR123"
        )
        calls = []

        def fetch(url, *, method="GET", json_body=None):
            calls.append((url, method, json_body))
            if "/job/Remote/Staff-Accountant_JR123" in url:
                return {
                    "status_code": 200,
                    "final_url": url,
                    "text": json.dumps({
                        "jobPostingInfo": {
                            "title": "Staff Accountant",
                            "jobDescription": "Full-time remote accounting role.",
                            "location": "United States - Remote",
                            "timeType": "Full time",
                            "jobReqId": "JR123",
                            "externalUrl": public,
                        }
                    }),
                }
            return {
                "status_code": 200,
                "final_url": url,
                "text": json.dumps({"jobPostings": []}),
            }

        result = discover_public_ats_urls(public, "Staff Accountant", fetch)
        self.assertIn(public, result.urls)
        self.assertEqual(result.records[public]["provider"], "workday")
        self.assertTrue(any(method == "POST" for _url, method, _body in calls))

    def test_two_independent_publishers_can_corroborate_active_signal(self):
        urls = [
            "https://www.jobleads.com/us/job/staff-accountant-example-corp--abc",
            "https://www.remoterocketship.com/company/example-corp/jobs/staff-accountant-us-remote/",
        ]
        bodies = {
            urls[0]: (
                "<html><head><title>Staff Accountant - Example Corp</title></head>"
                "<body><h1>Staff Accountant</h1><p>Example Corp is hiring a full-time "
                "remote Staff Accountant in the United States.</p></body></html>"
            ),
            urls[1]: (
                "<html><head><title>Remote Staff Accountant at Example Corp</title></head>"
                "<body><h1>Staff Accountant</h1><p>Join Example Corp to own month-end "
                "accounting in a fully remote United States position.</p></body></html>"
            ),
        }

        class Session:
            max_redirects = 5

            def get(self, url, **_kwargs):
                class Response:
                    status_code = 200
                    headers = {"content-type": "text/html"}

                    def __init__(self, value):
                        self.url = value
                        self.text = bodies[value]

                return Response(url)

        job = {
            "job_id": "j1",
            "job_title": "Staff Accountant",
            "employer_name": "Example Corp",
            "job_description": "Full-time remote accounting role.",
            "job_location": "United States - Remote",
            "job_employment_type": "FULLTIME",
            "job_apply_link": urls[0],
            "apply_options": [{"apply_link": urls[1]}],
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(Session()).resolve(job, fetch=True)
        self.assertEqual(result.state, "ACTIVE_CORROBORATED")
        self.assertEqual(result.source_type, "corroborated")
        self.assertTrue(result.corroborated)

    def test_single_publisher_never_corroborates(self):
        url = "https://www.jobleads.com/us/job/staff-accountant-example-corp--abc"

        class Session:
            max_redirects = 5

            def get(self, value, **_kwargs):
                class Response:
                    status_code = 200
                    text = "<h1>Staff Accountant</h1><p>Example Corp is hiring.</p>"
                    headers = {"content-type": "text/html"}

                    def __init__(self):
                        self.url = value

                return Response()

        job = {
            "job_id": "j1",
            "job_title": "Staff Accountant",
            "employer_name": "Example Corp",
            "job_apply_link": url,
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(Session()).resolve(job, fetch=True)
        self.assertEqual(result.state, "SOURCE_UNRESOLVED")

    def test_identical_syndicated_copies_do_not_count_as_independent_publishers(self):
        urls = [
            "https://www.jobleads.com/us/job/staff-accountant-example-corp--abc",
            "https://www.remoterocketship.com/company/example-corp/jobs/staff-accountant-us-remote/",
        ]
        body = (
            "<html><head><title>Staff Accountant - Example Corp</title></head>"
            "<body><h1>Staff Accountant</h1><p>Example Corp is hiring a full-time "
            "remote Staff Accountant in the United States.</p></body></html>"
        )

        class Session:
            max_redirects = 5

            def get(self, value, **_kwargs):
                class Response:
                    status_code = 200
                    text = body
                    headers = {"content-type": "text/html"}

                    def __init__(self):
                        self.url = value

                return Response()

        job = {
            "job_id": "duplicate-publishers",
            "job_title": "Staff Accountant",
            "employer_name": "Example Corp",
            "job_description": "Full-time remote accounting role.",
            "job_location": "United States - Remote",
            "job_employment_type": "FULLTIME",
            "job_apply_link": urls[0],
            "apply_options": [{"apply_link": urls[1]}],
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(Session()).resolve(job, fetch=True)
        self.assertEqual(result.state, "SOURCE_UNRESOLVED")
        self.assertIn("publisher_duplicate_content", [item.status for item in result.attempts])

    def test_direct_ats_requires_compatible_tenant_when_employer_is_missing(self):
        public = "https://jobs.lever.co/not-example/abc"
        endpoint = "https://api.lever.co/v0/postings/not-example/abc"
        posting = json.dumps({
            "id": "abc",
            "text": "Staff Accountant",
            "hostedUrl": public,
            "descriptionPlain": "Full-time remote accounting role in the United States.",
        })

        class Session:
            max_redirects = 5

            def get(self, value, **_kwargs):
                class Response:
                    headers = {"content-type": "application/json"}

                    def __init__(self):
                        self.url = value
                        self.status_code = 200
                        self.text = posting if value == endpoint else (
                            "<html><h1>Staff Accountant</h1><p>Full-time remote role in the "
                            "United States with an apply button.</p></html>"
                        )

                return Response()

        job = {
            "job_id": "wrong-tenant",
            "job_title": "Staff Accountant",
            "employer_name": "Example Corp",
            "employer_website": "",
            "job_apply_link": public,
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(Session()).resolve(job, fetch=True)
        self.assertEqual(result.state, "SOURCE_UNRESOLVED")

    def test_observed_legacy_workday_tenant_alias_can_corroborate_employer(self):
        public = (
            "https://magnitudesoftware.wd1.myworkdayjobs.com/en-US/External/job/"
            "USA---Remote/Account-Executive--Equity-Management_REQ000789"
        )
        cxs_job = (
            "https://magnitudesoftware.wd1.myworkdayjobs.com/wday/cxs/"
            "magnitudesoftware/External/job/USA---Remote/"
            "Account-Executive--Equity-Management_REQ000789"
        )

        class Response:
            def __init__(self, value, status, text):
                self.url = value
                self.status_code = status
                self.text = text
                self.headers = {"content-type": "application/json"}

        class Session:
            max_redirects = 5

            def get(self, value, **_kwargs):
                if value == cxs_job:
                    return Response(value, 200, json.dumps({"jobPostingInfo": {
                        "title": "Account Executive, Equity Management",
                        "jobDescription": "Full-time remote sales role in the United States.",
                        "location": "USA - Remote",
                        "timeType": "Full time",
                        "jobReqId": "REQ000789",
                        "externalUrl": public,
                    }}))
                if value == public:
                    return Response(value, 200, "<html><h1>Account Executive, Equity Management</h1></html>")
                return Response(value, 404, "")

            def post(self, value, **_kwargs):
                return Response(value, 200, json.dumps({"jobPostings": []}))

        job = {
            "job_id": "REQ000789",
            "job_title": "Account Executive, Equity Management",
            "employer_name": "insightsoftware",
            "employer_website": "https://insightsoftware.com",
            "job_apply_link": public,
            "job_location": "United States - Remote",
            "job_employment_type": "FULL_TIME",
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(Session()).resolve(job, fetch=True)
        self.assertEqual(result.state, "ACTIVE_CORROBORATED")
        self.assertEqual(result.source_url, public)

    def test_official_lever_inventory_without_target_title_remains_unresolved(self):
        lever = "https://jobs.lever.co/masterborn-2"
        api = "https://api.lever.co/v0/postings/masterborn-2?mode=json"

        class Session:
            max_redirects = 5

            def get(self, value, **_kwargs):
                class Response:
                    status_code = 200
                    headers = {"content-type": "application/json"}

                    def __init__(self):
                        self.url = value
                        self.text = json.dumps([
                            {"id": "1", "text": "Junior Product Owner", "hostedUrl": lever + "/1"},
                            {"id": "2", "text": "Solutions Architect", "hostedUrl": lever + "/2"},
                        ]) if value == api else "<html></html>"

                return Response()

        resolver = JobSourceResolver(Session())
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp), patch.object(
            resolver, "_discover_company_job_urls", return_value=([], [], {})
        ):
            result = resolver.resolve({
                "job_id": "stale",
                "job_title": "Junior Frontend Developer (React.js)",
                "employer_name": "MasterBorn",
                "employer_website": "https://masterborn.com",
                "job_apply_link": lever,
            }, fetch=True)
        self.assertEqual(result.state, "INACTIVE_VERIFIED")
        self.assertIn("official_ats_inventory_does_not_contain_job", result.notes)

    def test_official_ats_absence_overrides_two_aggregator_copies(self):
        lever = "https://jobs.lever.co/masterborn-2"
        api = "https://api.lever.co/v0/postings/masterborn-2?mode=json"
        publishers = [
            "https://www.jobleads.com/us/job/frontend-developer-masterborn--abc",
            "https://www.remoterocketship.com/company/masterborn/jobs/frontend-developer-remote/",
        ]
        publisher_body = (
            "<html><h1>Junior Frontend Developer React.js</h1>"
            "<p>MasterBorn is hiring this full-time remote role.</p></html>"
        )

        class Session:
            max_redirects = 5

            def get(self, value, **_kwargs):
                class Response:
                    headers = {"content-type": "text/html"}

                    def __init__(self):
                        self.url = value
                        self.status_code = 200
                        if value == api:
                            self.headers = {"content-type": "application/json"}
                            self.text = json.dumps([
                                {"id": "1", "text": "Junior Product Owner", "hostedUrl": lever + "/1"},
                                {"id": "2", "text": "Solutions Architect", "hostedUrl": lever + "/2"},
                            ])
                        elif value.startswith("https://masterborn.com"):
                            self.text = f'<a href="{lever}">Open positions</a>'
                        elif value == lever:
                            self.text = "<html><h1>Open positions</h1></html>"
                        elif value in publishers:
                            self.text = publisher_body
                        else:
                            self.status_code = 404
                            self.text = ""

                return Response()

        job = {
            "job_id": "stale-two-copies",
            "job_title": "Junior Frontend Developer React.js",
            "employer_name": "MasterBorn",
            "employer_website": "https://masterborn.com",
            "job_apply_link": publishers[0],
            "apply_options": [{"apply_link": publishers[1]}],
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(Session()).resolve(job, fetch=True)
        self.assertEqual(result.state, "INACTIVE_VERIFIED")

    def test_corroborated_publishers_pass_the_job_gate(self):
        source = ResolvedJobSource(
            state="ACTIVE_CORROBORATED",
            source_url="https://www.jobleads.com/us/job/staff-accountant-example--abc",
            source_type="corroborated",
            http_status=200,
            active=True,
            canonical_title="Staff Accountant",
            canonical_employer="Example Corp",
            description="This is a full-time fully remote role anywhere in the United States.",
            location_text="United States - Remote",
            employment_type="FULL_TIME",
            official=False,
            corroborated=True,
        )

        class Resolver:
            def resolve(self, _job, fetch=None):
                return source

        decision = JobGate(Resolver()).evaluate({
            "job_id": "corroborated",
            "job_title": "Staff Accountant",
            "employer_name": "Example Corp",
            "employer_website": "https://example.com",
            "job_description": "Full-time remote accounting role in the United States.",
            "job_location": "United States - Remote",
            "job_country": "US",
            "job_employment_type": "FULLTIME",
            "job_is_remote": True,
            "_matched_role": "Staff Accountant",
            "_employment_quality": "full_time",
            "_work_arrangement": "remote",
            "_remote_scope": "us_provider_confirmed",
            "_us_eligibility_reason": "provider_country_us",
        })
        self.assertEqual(decision.state, GateState.PASS)


class ReadyV1BoundaryTests(unittest.TestCase):
    def _lead(self, key: str) -> dict:
        return {
            "job_id": key,
            "lead_key": key,
            "_final_state": "FINAL_PASS",
            "_airtable_relevance": "accept",
            "_validation_timestamp": "2026-07-22T12:00:00+00:00",
            "priority_score": 10,
        }

    def test_inventory_retains_failed_and_marks_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            inventory = FinalPassInventory(str(Path(temp) / "inventory.json"))
            first = self._lead("first")
            second = self._lead("second")
            inventory.stage([first, second])
            selected = inventory.available(limit=2)
            inventory.reserve(selected)
            inventory.mark_persisted(["first"])
            inventory.release_failed(["second"])
            self.assertEqual([lead["lead_key"] for lead in inventory.available()], ["second"])

    def test_inventory_expires_signal_that_ages_out(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            recovery_inventory, "_now", return_value=datetime(2026, 7, 22, tzinfo=timezone.utc)
        ), patch.object(config, "MAX_JOB_AGE_DAYS", 8):
            inventory = FinalPassInventory(str(Path(temp) / "inventory.json"))
            lead = self._lead("old")
            lead["job_age_days"] = 7
            lead["_validation_timestamp"] = "2026-07-21T00:00:00+00:00"
            inventory.stage([lead])
            self.assertEqual(inventory.available(), [])

    def test_inventory_ranks_official_and_deduplicates_company(self):
        with tempfile.TemporaryDirectory() as temp:
            inventory = FinalPassInventory(str(Path(temp) / "inventory.json"))
            corroborated = self._lead("corroborated")
            corroborated.update({
                "employer_website": "https://example.com",
                "job_signal_confidence": "corroborated",
                "job_age_days": 1,
            })
            official = self._lead("official")
            official.update({
                "employer_website": "https://example.com",
                "job_signal_confidence": "official",
                "job_age_days": 1,
            })
            other = self._lead("other")
            other.update({
                "employer_website": "https://other.com",
                "job_signal_confidence": "corroborated",
                "job_age_days": 1,
            })
            inventory.stage([corroborated, official, other])
            selected = inventory.available(limit=30)
            self.assertEqual([lead["lead_key"] for lead in selected], ["official", "other"])
            self.assertGreater(selected[0]["priority_score"], selected[1]["priority_score"])
            inventory.reserve([selected[0]])
            inventory.mark_persisted(["official"])
            self.assertEqual([lead["lead_key"] for lead in inventory.available()], ["other"])

    def test_operational_state_maps_final_pass_to_ready(self):
        gates = {
            name: GateDecision(name, GateState.PASS, f"{name.upper()}_PASS")
            for name in ("job", "account", "role", "contact", "email")
        }
        lead = annotate_final_decision({}, gates)
        self.assertEqual(lead["_final_state"], "FINAL_PASS")
        self.assertEqual(lead["_operational_state"], "READY")

    def test_generic_mailbox_never_passes_email_gate(self):
        person = PersonMatch(
            person_found=True,
            email_found=True,
            email="info@example.com",
            email_status="verified",
        )
        decision = EmailGate().evaluate(
            person=person,
            hunter_result=None,
            company_domains={"example.com"},
        )
        self.assertEqual(decision.state, GateState.REROUTE)
        self.assertTrue(decision.metadata["generic_mailbox"])

    def test_tiny_payload_cannot_bypass_job_and_role_gates(self):
        class JobGate:
            def annotate(self, job, fetch=None):
                return {**job, "_job_gate_state": "REJECT", "_job_gate_reason": "TEST_REJECT"}

        class RoleGate:
            def annotate(self, job):
                raise AssertionError("Role gate must not run after a job rejection")

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "jobs.json"
            source.write_text(json.dumps({"jobs": [{"job_id": "tiny"}]}))
            result = run_precontact_qualification(
                str(source), output_dir=temp, job_gate=JobGate(), role_gate=RoleGate()
            )
        self.assertEqual(result.contact_eligible_jobs, 0)
        self.assertEqual(result.rejected_jobs, 1)

    def test_known_publisher_employer_is_removed_before_enrichment(self):
        from job_filter import assess_pre_enrichment_viability

        for employer, domain in (
            ("GradeBuzz", "https://gradebuzz.com/jobs/seo-specialist"),
            ("Cosmoquick", "https://cosmoquick.com/remote-jobs/devops"),
        ):
            result = assess_pre_enrichment_viability({
                "job_title": "SEO Specialist" if employer == "GradeBuzz" else "DevOps Engineer",
                "job_description": "A full-time remote role in the United States.",
                "job_employment_type": "Full-time",
                "job_is_remote": True,
                "job_country": "US",
                "employer_name": employer,
                "job_publisher": employer,
                "job_apply_link": domain,
                "_matched_role": "SEO Specialist" if employer == "GradeBuzz" else "DevOps Engineer",
            })
            self.assertFalse(result.eligible)
            self.assertEqual(result.stat_name, "excluded_aggregator")

    def test_remote_rocketship_domain_is_never_company_identity(self):
        from company_identity import safe_company_domain

        self.assertEqual(
            safe_company_domain(
                "https://www.remoterocketship.com/us/company/cna/jobs/example",
                config.INTERMEDIARY_JOB_DOMAINS,
            ),
            "",
        )

    def test_prefilter_rejects_explicit_part_time_despite_provider_full_time(self):
        from job_filter import assess_employment_quality

        result = assess_employment_quality({
            "job_title": "Remote HR Generalist & Talent Recruiter (Part‑Time)",
            "job_employment_type": "Full-time",
            "job_description": "Join the team remotely on a part-time basis.",
        })
        self.assertFalse(result.eligible)
        self.assertEqual(result.classification, "non_full_time")

    def test_integrity_rejects_compensation_placeholder_as_employer(self):
        from job_quality import assess_posting_integrity

        result = assess_posting_integrity({
            "job_title": "Virtual Assistant - Video Editor",
            "employer_name": "700 / month",
            "job_description": "Remote assistant role.",
        })
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "untrustworthy_employer_identity")

    def test_acquisition_catalog_is_bounded_and_covers_distinct_supply_families(self):
        self.assertEqual(len(DEFAULT_ACQUISITION_ROLES), 50)
        for role in (
            "Customer Success Associate",
            "Technical Support Specialist",
            "Full Stack Developer",
            "DevOps Engineer",
            "Systems Administrator",
            "Data Scientist",
            "Accountant",
            "Billing Specialist",
            "Brand Manager",
            "UX/UI Designer",
            "Podcast Producer",
        ):
            self.assertIn(role, DEFAULT_ACQUISITION_ROLES)

    def test_scraper_does_not_preemptively_reject_senior_ic(self):
        from jsearch_scraper import is_excluded_title

        self.assertFalse(is_excluded_title("Senior Staff Accountant"))
        self.assertFalse(is_excluded_title("Sr. Backend Developer"))
        self.assertTrue(is_excluded_title("VP of Engineering"))
        self.assertTrue(is_excluded_title("Marketing Director"))

    def test_representative_query_classifies_against_full_catalog(self):
        class Registry:
            def has_job_id(self, _job_id):
                return False

        raw = [{
            "job_id": "backend-1",
            "job_title": "Backend Developer",
            "job_description": "Build APIs and backend services for a software product.",
            "employer_name": "Example Corp",
            "job_employment_type": "FULLTIME",
            "job_is_remote": True,
            "job_country": "US",
        }]
        candidates = {}
        _ingest_query_jobs(
            raw_jobs=raw,
            search_role="Software Engineer",
            canonical_role="Software Engineer",
            registry=Registry(),
            candidates_by_job_id=candidates,
            stats={},
        )
        selected = next(iter(candidates.values()))[0]
        self.assertEqual(selected["_matched_role"], "Backend Developer")

    def test_ready_targets_build_reserve_without_overdelivering(self):
        with (
            patch.object(config, "READY_INVENTORY_TARGET", 60),
            patch.object(config, "READY_DAILY_DELIVERY_LIMIT", 30),
            patch.object(config, "get_final_pass_target", return_value=30),
        ):
            self.assertEqual(run_daily._ready_targets(0), (60, 30))
            self.assertEqual(run_daily._ready_targets(30), (60, 30))
            self.assertEqual(run_daily._ready_targets(60), (30, 30))

    def test_pipeline_lock_blocks_concurrent_run_and_releases_cleanly(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "pipeline.lock")
            first = PipelineRunLock(path, stale_hours=1)
            first.acquire()
            with self.assertRaises(PipelineAlreadyRunningError):
                PipelineRunLock(path, stale_hours=1).acquire()
            first.release()
            with PipelineRunLock(path, stale_hours=1):
                self.assertTrue(Path(path).exists())
            self.assertFalse(Path(path).exists())


if __name__ == "__main__":
    unittest.main()
