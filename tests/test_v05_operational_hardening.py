from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
from apollo_client import OrgEnrichment, PersonMatch
from approved_revalidation import revalidate_approved_record
from contact_gate import ContactGate
from decision_types import GateState
from job_gate import JobGate
from job_source_resolver import (
    JobSourceResolver,
    ResolvedJobSource,
    _extract_embedded_urls,
    _extract_links,
)
from pipeline_checkpoint import PipelineCheckpoint
from recovery_inventory import FinalPassInventory, RecoverableJobQueue
from reroute_state import RerouteRegistry
from validation_integrity import validation_fingerprint


class _Response:
    def __init__(self, url: str, status: int, text: str, content_type: str = "text/html"):
        self.url = url
        self.status_code = status
        self.text = text
        self.headers = {"content-type": content_type}


class _Session:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.mapping.get(url, _Response(url, 404, "not found"))


def _posting(*, title="Staff Accountant", employer="Example Corp", active=True, include_employer=True):
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": "Full-time remote role in the United States. Responsibilities and qualifications. " * 12,
        "employmentType": "FULL_TIME",
        "jobLocationType": "TELECOMMUTE",
        "datePosted": datetime.now(timezone.utc).date().isoformat(),
    }
    if include_employer:
        payload["hiringOrganization"] = {"name": employer}
    if active:
        payload["validThrough"] = "2099-12-31T23:59:59Z"
    return f'<script type="application/ld+json">{json.dumps(payload)}</script>'


def _job(**overrides):
    job = {
        "job_id": "j1",
        "job_title": "Staff Accountant",
        "employer_name": "Example Corp",
        "employer_website": "https://example.com",
        "job_description": "Full-time remote role serving the United States.",
        "job_employment_type": "Full-time",
        "job_is_remote": True,
        "job_location": "United States",
        "_employment_quality": "full_time",
        "_work_arrangement": "remote",
        "_remote_scope": "us_provider_confirmed",
        "_us_eligibility_reason": "provider_confirmed_us_remote",
    }
    job.update(overrides)
    return job


class PublicAtsAdapterTests(unittest.TestCase):
    def test_greenhouse_public_api_recovers_client_rendered_board(self):
        careers = '<script>window.board="https://boards.greenhouse.io/examplecorp";</script>'
        api = json.dumps({"jobs": [{
            "id": 987,
            "title": "Staff Accountant",
            "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/987",
        }]})
        session = _Session({
            "https://example.com/": _Response("https://example.com/", 200, "Example Corp"),
            "https://example.com/careers": _Response("https://example.com/careers", 200, careers),
            "https://boards.greenhouse.io/examplecorp": _Response("https://boards.greenhouse.io/examplecorp", 200, "<div id='app'></div>"),
            "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs?content=true": _Response(
                "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs?content=true", 200, api, "application/json"
            ),
            "https://boards.greenhouse.io/examplecorp/jobs/987": _Response(
                "https://boards.greenhouse.io/examplecorp/jobs/987", 200, _posting()
            ),
        })
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(session).resolve(_job(), fetch=True)
        self.assertEqual(result.state, "ACTIVE_VERIFIED")
        self.assertEqual(result.source_url, "https://boards.greenhouse.io/examplecorp/jobs/987")
        self.assertIn("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs?content=true", session.calls)

    def test_lever_public_api_recovers_client_rendered_board(self):
        careers = '<a href="https://jobs.lever.co/examplecorp">View openings</a>'
        api = json.dumps([{
            "id": "abc-123",
            "text": "Staff Accountant",
            "hostedUrl": "https://jobs.lever.co/examplecorp/abc-123",
        }])
        session = _Session({
            "https://example.com/": _Response("https://example.com/", 200, "Example Corp"),
            "https://example.com/careers": _Response("https://example.com/careers", 200, careers),
            "https://jobs.lever.co/examplecorp": _Response("https://jobs.lever.co/examplecorp", 200, "<div id='lever-jobs-container'></div>"),
            "https://api.lever.co/v0/postings/examplecorp?mode=json": _Response(
                "https://api.lever.co/v0/postings/examplecorp?mode=json", 200, api, "application/json"
            ),
            "https://jobs.lever.co/examplecorp/abc-123": _Response(
                "https://jobs.lever.co/examplecorp/abc-123", 200, _posting()
            ),
        })
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(session).resolve(_job(), fetch=True)
        self.assertEqual(result.state, "ACTIVE_VERIFIED")
        self.assertEqual(result.source_url, "https://jobs.lever.co/examplecorp/abc-123")

    def test_ashby_public_api_recovers_client_rendered_board(self):
        careers = '<a href="https://jobs.ashbyhq.com/examplecorp">View openings</a>'
        api = json.dumps({"apiVersion": "1", "jobs": [{
            "title": "Staff Accountant", "isListed": True,
            "jobUrl": "https://jobs.ashbyhq.com/examplecorp/abc-123",
        }]})
        session = _Session({
            "https://example.com/": _Response("https://example.com/", 200, "Example Corp"),
            "https://example.com/careers": _Response("https://example.com/careers", 200, careers),
            "https://jobs.ashbyhq.com/examplecorp": _Response("https://jobs.ashbyhq.com/examplecorp", 200, "<div id='app'></div>"),
            "https://api.ashbyhq.com/posting-api/job-board/examplecorp?includeCompensation=false": _Response(
                "https://api.ashbyhq.com/posting-api/job-board/examplecorp?includeCompensation=false", 200, api, "application/json"
            ),
            "https://jobs.ashbyhq.com/examplecorp/abc-123": _Response(
                "https://jobs.ashbyhq.com/examplecorp/abc-123", 200, _posting()
            ),
        })
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(session).resolve(_job(), fetch=True)
        self.assertEqual(result.state, "ACTIVE_VERIFIED")
        self.assertEqual(result.source_url, "https://jobs.ashbyhq.com/examplecorp/abc-123")


class SourceIdentityAndActivityTests(unittest.TestCase):
    def test_provider_ats_without_hiring_organization_is_not_official(self):
        url = "https://jobs.lever.co/not-example/abc"
        session = _Session({url: _Response(url, 200, _posting(include_employer=False))})
        job = _job(job_apply_link=url, employer_website="")
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(session).resolve(job, fetch=True)
        self.assertEqual(result.state, "SOURCE_UNRESOLVED")
        statuses = [attempt.status for attempt in result.attempts]
        self.assertTrue({"employer_identity_unverified", "job_identity_unverified"} & set(statuses))

    def test_missing_positive_activity_signal_is_retryable_not_active(self):
        url = "https://example.com/jobs/staff-accountant"
        body = _posting(active=False).replace(
            datetime.now(timezone.utc).date().isoformat(), "2020-01-01"
        ).replace("Full-time remote role", "Role")
        session = _Session({url: _Response(url, 200, body)})
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp), patch.object(
            JobSourceResolver, "_discover_company_job_urls", return_value=([], [])
        ):
            result = JobSourceResolver(session).resolve(_job(job_apply_link=url), fetch=True)
        self.assertEqual(result.state, "SOURCE_UNRESOLVED")
        self.assertTrue(result.retryable)
        self.assertIn("activity_unconfirmed", result.notes)

    def test_stale_first_official_url_does_not_hide_active_second_url(self):
        old = "https://example.com/jobs/old-staff-accountant"
        current = "https://example.com/jobs/current-staff-accountant"
        session = _Session({
            old: _Response(old, 410, "gone"),
            current: _Response(current, 200, _posting()),
        })
        job = _job(job_apply_link=old, apply_options=[{"apply_link": current}])
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp), patch.object(
            JobSourceResolver, "_discover_company_job_urls", return_value=([], [])
        ):
            result = JobSourceResolver(session).resolve(job, fetch=True)
        self.assertEqual(result.state, "ACTIVE_VERIFIED")
        self.assertEqual(result.source_url, current)

    def test_explicit_employer_mismatch_overrides_company_discovery_provenance(self):
        url = "https://jobs.lever.co/examplecorp/wrong-company-role"
        session = _Session({url: _Response(url, 200, _posting(employer="Different Company"))})
        resolver = JobSourceResolver(session)
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp), patch.object(
            resolver, "_discover_company_job_urls", return_value=([url], [])
        ):
            result = resolver.resolve(_job(), fetch=True)
        self.assertEqual(result.state, "SOURCE_UNRESOLVED")
        self.assertIn("employer_identity_mismatch", [attempt.status for attempt in result.attempts])

    def test_disabled_apply_button_is_not_positive_activity_evidence(self):
        url = "https://example.com/jobs/staff-accountant"
        body = _posting(active=False).replace(
            datetime.now(timezone.utc).date().isoformat(), "2020-01-01"
        ) + '<button disabled aria-disabled="true">Apply now</button>'
        session = _Session({url: _Response(url, 200, body)})
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp), patch.object(
            JobSourceResolver, "_discover_company_job_urls", return_value=([], [])
        ):
            result = JobSourceResolver(session).resolve(_job(job_apply_link=url), fetch=True)
        self.assertEqual(result.state, "SOURCE_UNRESOLVED")
        self.assertTrue(result.retryable)


class ProvenanceAndContactTests(unittest.TestCase):
    def test_explicit_hybrid_claim_remains_valid_demand(self):
        source = ResolvedJobSource(
            state="ACTIVE_VERIFIED", source_url="https://example.com/jobs/j1",
            source_type="company", http_status=200, active=True,
            canonical_title="Staff Accountant", canonical_employer="Example Corp",
            description="Support accounting operations and monthly close.",
            official=True, corroborated=True,
        )
        job = _job(
            job_description="Hybrid role requiring three days in the office.",
            job_is_remote=False,
            _work_arrangement="hybrid",
        )
        resolver = SimpleNamespace(resolve=lambda *_args, **_kwargs: source)
        decision = JobGate(resolver).evaluate(job)
        self.assertEqual(decision.state, GateState.PASS)

    def _person(self, **overrides):
        raw = {
            "current_organization": {"name": "Example Corp", "primary_domain": "example.com"},
            "country": "United States",
        }
        person = PersonMatch(
            person_found=True, person_id="p1", title="VP Finance",
            linkedin_url="https://linkedin.com/in/person",
            organization_name="Example Corp", organization_domain="example.com",
            country="United States", raw=raw,
        )
        for key, value in overrides.items():
            setattr(person, key, value)
        return person

    def test_contact_without_positive_current_employment_reroutes(self):
        person = self._person(raw={}, linkedin_url=None)
        decision = ContactGate().evaluate(
            person=person, target_titles=["VP Finance"], company_domains={"example.com"},
            company_name="Example Corp", intent_market="us_market",
        )
        self.assertEqual(decision.state, GateState.REROUTE)
        self.assertIn("NOT_CURRENT_EMPLOYEE", str(decision.primary_reason))

    def test_contact_without_explicit_territory_can_own_global_function(self):
        person = self._person(country=None)
        person.raw = {"current_organization": {"name": "Example Corp", "primary_domain": "example.com"}}
        decision = ContactGate().evaluate(
            person=person, target_titles=["VP Finance"], company_domains={"example.com"},
            company_name="Example Corp", intent_market="us_market",
        )
        self.assertEqual(decision.state, GateState.PASS)

    def test_global_c_level_can_own_us_hiring_from_outside_us(self):
        person = self._person(title="Chief Revenue Officer", country="United Kingdom")
        person.raw = {
            "current_organization": {"name": "Example Corp", "primary_domain": "example.com"},
            "country": "United Kingdom",
        }
        decision = ContactGate().evaluate(
            person=person, target_titles=["Chief Revenue Officer"], company_domains={"example.com"},
            company_name="Example Corp", intent_market="us_market",
        )
        self.assertEqual(decision.state, GateState.PASS)
        self.assertEqual(decision.evidence.facts["contact_territory"].value, "global_scope_verified")


class PersistenceAndIntegrityTests(unittest.TestCase):
    def test_recoverable_queue_reinjects_and_expires_by_attempt_limit(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "RECOVERABLE_JOB_MAX_ATTEMPTS", 2):
            path = str(Path(temp) / "recoverable.json")
            queue = RecoverableJobQueue(path)
            job = _job(_final_state="UNVERIFIED", _final_primary_reason="SOURCE_TEMP")
            queue.upsert([job])
            key = next(iter(queue.payload["jobs"]))
            queue.payload["jobs"][key]["next_retry_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            queue.save()
            self.assertEqual(len(queue.due_jobs()), 1)
            queue.payload["jobs"][key]["next_retry_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            queue.save()
            self.assertEqual(len(queue.due_jobs()), 1)
            self.assertEqual(queue.due_jobs(), [])
            self.assertNotIn(key, queue.payload["jobs"])

    def test_checkpoint_restores_jobs_and_query_progress_only_for_same_version(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "checkpoint.json")
            checkpoint = PipelineCheckpoint(path)
            checkpoint.append_jobs([_job()], query_metrics={"Staff Accountant": {"pages": [1, 2]}})
            restored = PipelineCheckpoint(path)
            self.assertEqual(len(restored.pending_jobs()), 1)
            self.assertEqual(restored.query_metrics()["Staff Accountant"]["pages"], [1, 2])
            with patch.object(config, "VALIDATION_VERSION", "different-version"):
                invalid = PipelineCheckpoint(path)
                self.assertEqual(invalid.pending_jobs(), [])

    def test_checkpoint_removes_processed_jobs_but_keeps_unprocessed_work(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "checkpoint.json")
            checkpoint = PipelineCheckpoint(path)
            first = _job(job_id="first")
            second = _job(job_id="second", employer_name="Second Corp")
            checkpoint.append_jobs([first, second])
            checkpoint.remove_jobs([first])
            remaining = PipelineCheckpoint(path).pending_jobs()
            self.assertEqual([item["job_id"] for item in remaining], ["second"])

    def test_final_pass_inventory_preserves_unpersisted_lead(self):
        with tempfile.TemporaryDirectory() as temp:
            inventory = FinalPassInventory(str(Path(temp) / "final.json"))
            lead = _job(_final_state="FINAL_PASS")
            inventory.stage([lead])
            self.assertEqual(len(FinalPassInventory(str(Path(temp) / "final.json")).valid_leads()), 1)
            inventory.remove([lead])
            self.assertEqual(inventory.valid_leads(), [])

    def test_fingerprint_detects_critical_airtable_edit(self):
        fields = {
            "Company": "Example Corp", "Website": "https://example.com",
            "Open Role": "Staff Accountant", "Job URL": "https://example.com/jobs/j1",
            "Job ID": "j1", "Hiring Manager": "Alex Smith", "HM Title": "VP Finance",
            "LinkedIn": "https://linkedin.com/in/alex", "Apollo Person ID": "p1",
            "Email": "alex@example.com", "Role Bucket": "Finance",
            "Final Decision": "FINAL_PASS", "Validation Version": config.VALIDATION_VERSION,
            "Validated At": datetime.now(timezone.utc).isoformat(),
        }
        fields["Validation Fingerprint"] = validation_fingerprint(fields)
        from validation_integrity import fingerprint_matches
        self.assertTrue(fingerprint_matches(fields))
        fields["Email"] = "different@example.com"
        self.assertFalse(fingerprint_matches(fields))

    def test_fingerprint_detects_campaign_or_personalization_edit(self):
        fields = {
            "Company": "Example Corp", "Website": "https://example.com",
            "Open Role": "Staff Accountant", "Open Roles": "Staff Accountant",
            "Role Focus": "monthly close", "Matched Role": "Staff Accountant",
            "Role Bucket": "Finance", "Campaign ID": "campaign-a", "Employees": 100,
            "Job URL": "https://example.com/jobs/j1", "Job URL Status": "verified",
            "Job URL Source": "company", "Job ID": "j1", "Location": "United States",
            "Employment Type": "Full-time", "Hiring Manager": "Alex Smith",
            "HM Title": "VP Finance", "LinkedIn": "https://linkedin.com/in/alex",
            "Apollo Person ID": "p1", "Email": "alex@example.com",
            "Final Decision": "FINAL_PASS", "Validation Version": config.VALIDATION_VERSION,
            "Validated At": datetime.now(timezone.utc).isoformat(),
        }
        fields["Validation Fingerprint"] = validation_fingerprint(fields)
        from validation_integrity import fingerprint_matches
        self.assertTrue(fingerprint_matches(fields))
        fields["Campaign ID"] = "campaign-b"
        self.assertFalse(fingerprint_matches(fields))


class ApprovedBoundaryTests(unittest.TestCase):
    def _fields(self):
        fields = {
            "Company": "Example Corp", "Website": "https://example.com",
            "Open Role": "Staff Accountant", "Job URL": "https://example.com/jobs/j1",
            "Job ID": "j1", "Hiring Manager": "Alex Smith", "HM Title": "VP Finance",
            "LinkedIn": "https://linkedin.com/in/alex", "Apollo Person ID": "p1",
            "Email": "alex@example.com", "Role Bucket": "Finance",
            "Final Decision": "FINAL_PASS", "Validation Version": config.VALIDATION_VERSION,
            "Validated At": datetime.now(timezone.utc).isoformat(),
        }
        fields["Validation Fingerprint"] = validation_fingerprint(fields)
        return fields

    def test_approved_record_is_blocked_when_fingerprint_was_tampered(self):
        fields = self._fields()
        fields["Email"] = "tampered@example.com"
        valid, reason = revalidate_approved_record({"fields": fields})
        self.assertFalse(valid)
        self.assertIn("fingerprint", reason.lower())

    def test_valid_approved_record_rechecks_all_boundaries(self):
        fields = self._fields()
        source = ResolvedJobSource(state="ACTIVE_VERIFIED", official=True, active=True)
        org = OrgEnrichment(found=True, name="Example Corp", domain="example.com", employee_count=100, industry="Software")
        person = PersonMatch(
            person_found=True, email_found=True, person_id="p1", first_name="Alex", last_name="Smith",
            title="VP Finance", linkedin_url="https://linkedin.com/in/alex",
            organization_name="Example Corp", organization_domain="example.com",
            email="alex@example.com", email_status="verified", country="United States",
            raw={
                "current_organization": {"name": "Example Corp", "primary_domain": "example.com"},
                "country": "United States",
            },
        )
        account_pass = SimpleNamespace(state_value=GateState.PASS.value, primary_reason="ACCOUNT_PASS")
        with (
            patch("approved_revalidation.JobSourceResolver") as resolver_cls,
            patch("approved_revalidation.apollo.enrich_organization", return_value=org),
            patch("approved_revalidation.AccountGate.evaluate", return_value=account_pass),
            patch("approved_revalidation.apollo.match_person", return_value=person),
        ):
            resolver_cls.return_value.resolve.return_value = source
            valid, reason = revalidate_approved_record({"fields": fields})
        self.assertTrue(valid, reason)


class RerouteExpirationTests(unittest.TestCase):
    def test_temporary_and_permanent_reroutes_receive_different_expirations(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "REROUTE_TEMPORARY_TTL_HOURS", 2), patch.object(
            config, "REROUTE_PERMANENT_TTL_DAYS", 30
        ):
            registry = RerouteRegistry(str(Path(temp) / "reroutes.json"))
            registry.record("temporary", ["p1"], "EMAIL_API_UNAVAILABLE")
            registry.record("permanent", ["p2"], "REROUTE_WRONG_ORGANIZATION")
            temporary = datetime.fromisoformat(registry.payload["accounts"]["temporary"]["people"]["p1"]["expires_at"])
            permanent = datetime.fromisoformat(registry.payload["accounts"]["permanent"]["people"]["p2"]["expires_at"])
            self.assertGreater(permanent - temporary, timedelta(days=20))


class SchedulerBoundaryTests(unittest.TestCase):
    def test_daily_main_returns_distinct_code_when_sla_is_missed(self):
        import run_daily
        with (
            patch.object(run_daily, "run_pipeline", return_value={"success": True, "sla_success": False}),
            patch.object(run_daily, "save_run_summary", return_value="summary.json"),
            patch.object(config, "PIPELINE_FAIL_PROCESS_ON_SLA_MISS", True),
        ):
            self.assertEqual(run_daily.main(), 2)

    def test_daily_main_exits_zero_after_technical_success_by_default(self):
        import run_daily
        with (
            patch.object(run_daily, "run_pipeline", return_value={"success": True, "sla_success": False}),
            patch.object(run_daily, "save_run_summary", return_value="summary.json"),
            patch.object(config, "PIPELINE_FAIL_PROCESS_ON_SLA_MISS", False),
        ):
            self.assertEqual(run_daily.main(), 0)

    def test_approved_sync_never_enrolls_failed_revalidation(self):
        import run_approved
        record = {"id": "rec1", "fields": {"Email": "alex@example.com"}}
        with (
            patch.object(run_approved.airtable_client, "get_approved_leads", return_value=[record]),
            patch.object(run_approved, "revalidate_approved_record", return_value=(False, "stale job")),
            patch.object(run_approved.airtable_client, "mark_error") as mark_error,
            patch.object(run_approved.instantly_client, "enroll_approved_leads") as enroll,
        ):
            enroll.return_value = {
                "enrolled": 0, "duplicates": 0, "failed": 0,
                "enrolled_record_ids": [], "failures": [],
            }
            result = run_approved.run()
        enroll.assert_called_once_with([])
        mark_error.assert_called_once_with(["rec1"], "stale job")
        self.assertEqual(result["revalidation_failed"], 1)
        self.assertEqual(result["failed"], 1)


class MalformedUrlResilienceTests(unittest.TestCase):
    def test_embedded_invalid_ipv6_url_is_skipped_without_losing_valid_candidate(self):
        body = (
            '<script>const broken="https://[invalid/careers/job";</script>'
            '<script>const valid="/careers/staff-accountant-123";</script>'
        )
        found = _extract_embedded_urls(body, "https://example.com/careers")
        self.assertIn((
            "https://example.com/careers/staff-accountant-123",
            "/careers/staff accountant 123",
        ), found)
        self.assertEqual(len(found), 1)

    def test_invalid_ipv6_href_is_skipped_without_aborting_link_extraction(self):
        body = (
            '<a href="https://[invalid/jobs/123">Broken</a>'
            '<a href="/careers/staff-accountant-123">Staff Accountant</a>'
        )
        found = _extract_links(body, "https://example.com/careers")
        self.assertEqual(found, [(
            "https://example.com/careers/staff-accountant-123",
            "Staff Accountant",
        )])


class CheckpointResumeTests(unittest.TestCase):
    def test_checkpoint_resume_creates_raw_artifact_without_query_units(self):
        import run_daily

        jobs = [
            {"job_id": "j1", "_search_role": "Staff Accountant"},
            {"job_id": "j2", "_search_role": "Data Analyst"},
        ]
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "OUTPUT_DIR", temp):
            result = run_daily._resume_scrape_from_checkpoint(jobs, {"Staff Accountant": {"raw_jobs": 10}})
            payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))

        self.assertEqual(result.total_jobs, 2)
        self.assertEqual(result.roles_with_results, 2)
        self.assertTrue(result.stats["checkpoint_resumed"])
        self.assertEqual(result.stats["estimated_request_units"], 0)
        self.assertEqual(payload["jobs"], jobs)


if __name__ == "__main__":
    unittest.main()
