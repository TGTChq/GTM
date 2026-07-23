from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import config
from decision_types import GateState
from job_gate import JobGate
from job_source_resolver import JobSourceResolver
from job_signal import select_job_url
from qualification_pipeline import run_precontact_qualification


def _fresh_job(**overrides):
    description = (
        "Example Corp is hiring for this full-time fully remote role anywhere in the United States. "
        "You will own monthly reporting, analysis, reconciliations, stakeholder "
        "communication, systems improvement, documentation, and cross-functional "
        "delivery. " * 8
    )
    job = {
        "job_id": "fresh-direct-1",
        "job_title": "Staff Accountant",
        "employer_name": "Example Corp",
        "employer_website": "https://example.com",
        "job_description": description,
        "job_location": "United States - Remote",
        "job_country": "US",
        "job_employment_type": "FULLTIME",
        "job_is_remote": True,
        "job_posted_at_datetime_utc": datetime.now(timezone.utc).isoformat(),
        "job_apply_link": "https://example.com/jobs/staff-accountant-123",
        "job_apply_is_direct": True,
        "_matched_role": "Staff Accountant",
        "_employment_quality": "full_time",
        "_employment_quality_reason": "provider_full_time",
        "_work_arrangement": "remote",
        "_work_arrangement_reason": "provider_remote_true",
        "_remote_scope": "us_provider_confirmed",
        "_us_eligibility_reason": "provider_country_us",
    }
    job.update(overrides)
    return job


class SourceResilienceV11Tests(unittest.TestCase):
    def test_fresh_direct_company_timeout_uses_closed_structured_fallback(self):
        resolver = JobSourceResolver()
        with (
            patch.object(resolver, "_fetch", return_value={
                "status_code": None,
                "final_url": "https://example.com/jobs/staff-accountant-123",
                "text": "",
                "error": "timed out",
                "error_type": "timeout",
            }),
            patch.object(resolver, "_discover_company_job_urls") as discover,
        ):
            resolved = resolver.resolve(_fresh_job(), fetch=True)
        discover.assert_not_called()
        self.assertEqual(resolved.state, "ACTIVE_DIRECT_STRUCTURED")
        self.assertTrue(resolved.corroborated)
        self.assertIn("fresh_direct_structured_fallback", resolved.notes)
        self.assertIn("direct_fast_path", resolved.notes)

    def test_fresh_direct_structured_source_can_pass_job_gate(self):
        resolver = JobSourceResolver()
        with patch.object(resolver, "_fetch", return_value={
            "status_code": 403,
            "final_url": "https://example.com/jobs/staff-accountant-123",
            "text": "",
            "error": "forbidden",
        }):
            decision = JobGate(resolver).evaluate(_fresh_job(), fetch=True)
        self.assertEqual(decision.state, GateState.PASS)
        self.assertEqual(decision.metadata["signal_confidence"], "direct_structured")

    def test_prefilter_corroboration_can_recover_missing_provider_fields(self):
        resolver = JobSourceResolver()
        job = _fresh_job(
            job_employment_type="",
            job_is_remote=False,
            job_country="",
            _employment_quality_reason="explicit_full_time_description",
            _work_arrangement_reason="remote_title_or_location",
            _remote_scope="us_explicit",
            _us_eligibility_reason="explicit_us_scope",
        )
        with patch.object(resolver, "_fetch", return_value={
            "status_code": None,
            "final_url": job["job_apply_link"],
            "text": "",
            "error": "timed out",
            "error_type": "timeout",
        }):
            resolved = resolver.resolve(job, fetch=True)
        self.assertEqual(resolved.state, "ACTIVE_DIRECT_STRUCTURED")

    def test_raw_foreign_country_contradiction_blocks_structured_fallback(self):
        resolver = JobSourceResolver()
        job = _fresh_job(job_country="Canada")
        with (
            patch.object(resolver, "_fetch", return_value={
                "status_code": None,
                "final_url": job["job_apply_link"],
                "text": "",
                "error": "timed out",
            }),
            patch.object(resolver, "_discover_company_job_urls", return_value=([], [], {})),
        ):
            resolved = resolver.resolve(job, fetch=True)
        self.assertNotEqual(resolved.state, "ACTIVE_DIRECT_STRUCTURED")

    def test_raw_contract_label_blocks_structured_fallback(self):
        resolver = JobSourceResolver()
        job = _fresh_job(job_employment_type="CONTRACTOR")
        with (
            patch.object(resolver, "_fetch", return_value={
                "status_code": None,
                "final_url": job["job_apply_link"],
                "text": "",
                "error": "timed out",
            }),
            patch.object(resolver, "_discover_company_job_urls", return_value=([], [], {})),
        ):
            resolved = resolver.resolve(job, fetch=True)
        self.assertNotEqual(resolved.state, "ACTIVE_DIRECT_STRUCTURED")

    def test_contract_contradiction_remains_a_hard_reject(self):
        resolver = JobSourceResolver()
        job = _fresh_job(
            job_description=(
                "Example Corp is hiring a fully remote role in the United States. "
                "This is a six-month contract engagement with no permanent position. "
                + "Responsibilities include reporting and reconciliation. " * 30
            )
        )
        with patch.object(resolver, "_fetch", return_value={
            "status_code": 403,
            "final_url": job["job_apply_link"],
            "text": "",
            "error": "forbidden",
        }):
            decision = JobGate(resolver).evaluate(job, fetch=True)
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertIn("CONTRACT", str(decision.primary_reason))

    def test_access_block_is_not_written_to_persistent_cache(self):
        response = Mock(status_code=403, url="https://example.com/jobs/123", text="blocked")
        response.headers = {}
        session = Mock()
        session.get.return_value = response
        resolver = JobSourceResolver(session=session)
        resolver.cache = Mock()
        resolver.cache.get.return_value = None
        result = resolver._fetch("https://example.com/jobs/123")
        self.assertEqual(result["status_code"], 403)
        resolver.cache.set.assert_not_called()

    def test_direct_timeout_is_not_requested_twice_before_discovery(self):
        session = Mock()
        session.get.side_effect = requests.Timeout("timed out")
        resolver = JobSourceResolver(session=session)
        resolver.cache = Mock()
        resolver.cache.get.return_value = None
        stale = _fresh_job(
            job_posted_at_datetime_utc=(
                datetime.now(timezone.utc) - timedelta(days=12)
            ).isoformat()
        )
        with patch.object(
            resolver, "_discover_company_job_urls", return_value=([], [], {})
        ):
            resolved = resolver.resolve(stale, fetch=True)
        self.assertIn(resolved.state, {"SOURCE_TEMPORARILY_UNAVAILABLE", "SOURCE_UNRESOLVED"})
        self.assertEqual(session.get.call_count, 1)

    def test_direct_structured_url_is_exposed_as_revalidation_required(self):
        job = _fresh_job(
            canonical_source_url="https://example.com/jobs/staff-accountant-123",
            canonical_source_type="company",
            official_job_status="ACTIVE_DIRECT_STRUCTURED",
        )
        url, status, source_type, reason = select_job_url(job, probe=False)
        self.assertEqual(url, job["canonical_source_url"])
        self.assertEqual(status, "unverified_review")
        self.assertEqual(source_type, "company")
        self.assertEqual(reason, "structured_direct_source_requires_revalidation")

    def test_stale_direct_posting_cannot_use_structured_fallback(self):
        resolver = JobSourceResolver()
        stale = _fresh_job(
            job_posted_at_datetime_utc=(
                datetime.now(timezone.utc) - timedelta(days=12)
            ).isoformat()
        )
        with (
            patch.object(resolver, "_fetch", return_value={
                "status_code": None,
                "final_url": stale["job_apply_link"],
                "text": "",
                "error": "timed out",
            }),
            patch.object(resolver, "_discover_company_job_urls", return_value=([], [], {})),
        ):
            resolved = resolver.resolve(stale, fetch=True)
        self.assertNotEqual(resolved.state, "ACTIVE_DIRECT_STRUCTURED")
        self.assertIn(resolved.state, {"SOURCE_TEMPORARILY_UNAVAILABLE", "SOURCE_UNRESOLVED"})

    def test_aggregator_posting_never_uses_direct_structured_fallback(self):
        resolver = JobSourceResolver()
        job = _fresh_job(
            job_apply_link="https://www.indeed.com/viewjob?jk=123",
            job_apply_is_direct=False,
        )
        with (
            patch.object(resolver, "_fetch", return_value={
                "status_code": 403,
                "final_url": job["job_apply_link"],
                "text": "",
                "error": "forbidden",
            }),
            patch.object(resolver, "_discover_company_job_urls", return_value=([], [], {})),
        ):
            resolved = resolver.resolve(job, fetch=True)
        self.assertEqual(resolved.state, "SOURCE_UNRESOLVED")

    def test_explicit_non_direct_flag_blocks_fallback(self):
        resolver = JobSourceResolver()
        job = _fresh_job(job_apply_is_direct=False)
        with (
            patch.object(resolver, "_fetch", return_value={
                "status_code": 403,
                "final_url": job["job_apply_link"],
                "text": "",
                "error": "forbidden",
            }),
            patch.object(resolver, "_discover_company_job_urls", return_value=([], [], {})),
        ):
            resolved = resolver.resolve(job, fetch=True)
        self.assertNotEqual(resolved.state, "ACTIVE_DIRECT_STRUCTURED")

    def test_authoritative_404_cannot_use_fallback(self):
        resolver = JobSourceResolver()
        job = _fresh_job()
        with (
            patch.object(resolver, "_fetch", return_value={
                "status_code": 404,
                "final_url": job["job_apply_link"],
                "text": "not found",
                "error": "",
            }),
            patch.object(resolver, "_discover_company_job_urls", return_value=([], [], {})),
        ):
            resolved = resolver.resolve(job, fetch=True)
        self.assertEqual(resolved.state, "INACTIVE_VERIFIED")

    def test_accessible_but_unconfirmed_page_cannot_use_fallback(self):
        resolver = JobSourceResolver()
        job = _fresh_job()
        with (
            patch.object(resolver, "_fetch", return_value={
                "status_code": 200,
                "final_url": job["job_apply_link"],
                "text": "<html><h1>Staff Accountant</h1><p>" + "x" * 900 + "</p></html>",
                "error": "",
            }),
            patch.object(resolver, "_discover_company_job_urls", return_value=([], [], {})),
        ):
            resolved = resolver.resolve(job, fetch=True)
        self.assertNotEqual(resolved.state, "ACTIVE_DIRECT_STRUCTURED")
        self.assertIn("job_identity_unverified", [item.status for item in resolved.attempts])

    def test_discovery_budget_stops_guessed_page_walk(self):
        resolver = JobSourceResolver()
        with (
            patch.object(config, "JOB_SOURCE_DISCOVERY_BUDGET_SECONDS", 5),
            patch("job_source_resolver.time.monotonic", side_effect=[0.0, 6.0]),
            patch.object(resolver, "_fetch") as fetch,
        ):
            urls, attempts, records = resolver._discover_company_job_urls(
                _fresh_job(), "example.com"
            )
        fetch.assert_not_called()
        self.assertEqual(urls, [])
        self.assertEqual(records, {})
        self.assertIn("discovery_budget_exhausted", [item.status for item in attempts])

    def test_qualification_records_source_resolution_path(self):
        source = {
            "state": "ACTIVE_DIRECT_STRUCTURED",
            "notes": ["direct_fast_path", "fresh_direct_structured_fallback"],
            "attempts": [{"status": "access_blocked", "phase": "direct"}],
        }

        class FakeJobGate:
            def annotate(self, job, fetch=None):
                return {
                    **job,
                    "_job_gate_state": "PASS",
                    "_job_gate_reason": "JOB_PASS",
                    "_job_gate_decision": {
                        "state": "PASS",
                        "metadata": {"source": source},
                    },
                }

        class FakeRoleGate:
            def annotate(self, job):
                return {
                    **job,
                    "_role_gate_state": "PASS",
                    "_role_gate_reason": "ROLE_PASS",
                    "_role_gate_decision": {"state": "PASS"},
                }

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "jobs.json"
            path.write_text(json.dumps({"jobs": [_fresh_job()]}), encoding="utf-8")
            result = run_precontact_qualification(
                str(path),
                output_dir=temp,
                job_gate=FakeJobGate(),
                role_gate=FakeRoleGate(),
            )
        self.assertEqual(result.contact_eligible_jobs, 1)
        self.assertEqual(result.stats["source_state__ACTIVE_DIRECT_STRUCTURED"], 1)
        self.assertEqual(result.stats.get("source_retryable", 0), 0)
        self.assertEqual(result.stats["source_note__direct_fast_path"], 1)
        self.assertEqual(result.stats["source_attempt__access_blocked"], 1)
        self.assertEqual(result.stats["source_phase__direct"], 1)


if __name__ == "__main__":
    unittest.main()
