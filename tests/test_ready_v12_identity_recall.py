from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import apollo_client
import config
import hiring_manager
import job_filter
import jsearch_scraper
from approved_revalidation import revalidate_approved_record
from decision_types import GateDecision, GateState
from job_gate import JobGate
from job_source_resolver import JobSourceResolver, ResolvedJobSource, SourceAttempt
from job_signal import select_job_url
from pipeline_state import SeenJobsRegistry
from validation_integrity import validation_fingerprint


def provider_job(**overrides):
    description = (
        "Example Corp is hiring a full-time fully remote employee anywhere in "
        "the United States. The person will own implementation, reporting, "
        "stakeholder communication, documentation, systems improvement, and "
        "cross-functional delivery. " * 7
    )
    job = {
        "job_id": "provider-1",
        "job_title": "Customer Success Manager",
        "employer_name": "Example Corp",
        "employer_website": "https://example.com",
        "job_description": description,
        "job_location": "United States - Remote",
        "job_country": "US",
        "job_employment_type": "FULLTIME",
        "job_is_remote": True,
        "job_posted_at_datetime_utc": datetime.now(timezone.utc).isoformat(),
        "job_apply_link": "https://www.jobleads.com/us/job/example-123",
        "job_apply_is_direct": False,
        "job_publisher": "JobLeads",
        "_matched_role": "Customer Success Manager",
        "_role_relevance_status": "accept",
        "_role_relevance_score": 100,
        "_employment_quality": "full_time",
        "_employment_quality_reason": "explicit_full_time_description",
        "_work_arrangement": "remote",
        "_work_arrangement_reason": "remote_title_or_location",
        "_remote_scope": "us_explicit",
        "_us_eligibility_reason": "explicit_us_scope",
    }
    job.update(overrides)
    return job


class EmployerIdentityV12Tests(unittest.TestCase):
    def test_non_direct_simplify_url_never_becomes_nvidia_domain(self):
        job = {
            "employer_name": "NVIDIA",
            "job_apply_link": "https://simplify.jobs/p/abc/system-engineer",
            "job_apply_is_direct": False,
            "apply_options": [{
                "apply_link": "https://simplify.jobs/p/abc/system-engineer",
                "is_direct": False,
            }],
        }
        self.assertEqual(
            job_filter.get_safe_employer_domain(job),
            ("", "employer_name_resolution_required"),
        )
        self.assertEqual(hiring_manager._best_input_domain(job), "")

    def test_explicit_direct_apply_option_can_supply_company_domain(self):
        job = {
            "employer_name": "Example Corp",
            "apply_options": [{
                "apply_link": "https://careers.example.com/jobs/123",
                "is_direct": True,
            }],
        }
        self.assertEqual(
            job_filter.get_safe_employer_domain(job),
            ("example.com", "direct_apply_option"),
        )


class AdaptiveAcquisitionV12Tests(unittest.TestCase):
    def test_full_50_role_catalog_uses_bounded_page2_when_global_budget_is_zero(self):
        roles = list(config.ROLES)
        self.assertEqual(len(roles), 50)
        productive = roles[0]
        calls = []

        def fake_fetch(role: str, *, page: int = 1, num_pages=None, **kwargs):
            calls.append((role, page))
            if role != productive:
                return []
            return [{
                "job_id": f"{role}-{page}",
                "job_title": role,
                "job_description": (
                    f"Example Corp is hiring a full-time fully remote {role} "
                    "anywhere in the United States. " * 8
                ),
                "job_location": "United States - Remote",
                "job_country": "US",
                "job_is_remote": True,
                "job_employment_type": "FULLTIME",
                "employer_name": "Example Corp",
                "employer_website": "https://example.com",
                "job_apply_link": f"https://example.com/jobs/{page}",
                "job_apply_is_direct": True,
            }]

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "ROLES", roles),
                patch.object(config, "OUTPUT_DIR", directory),
                patch.object(config, "NUM_PAGES", 1),
                patch.object(config, "JSEARCH_MAX_QUERIES_PER_RUN", 0),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 0),
                patch.object(config, "JSEARCH_ADAPTIVE_DEEPENING", True),
                patch.object(config, "JSEARCH_MAX_EXTRA_PAGES_PER_ROLE", 1),
                patch.object(config, "JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES", 32),
                patch.object(config, "JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES", 16),
                patch.object(config, "JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE", 1),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "MIN_JOBS_PER_RUN", 0),
                patch.object(config, "MIN_ROLES_WITH_RESULTS", 0),
                patch.object(config, "PRODUCTION", False),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
            ):
                result = jsearch_scraper.run_daily_scrape(
                    SeenJobsRegistry(path=str(Path(directory) / "seen.json"))
                )

        self.assertTrue(result.stats["adaptive_deepening_enabled"])
        self.assertEqual(result.stats["base_estimated_request_units"], 50)
        self.assertEqual(result.stats["adaptive_effective_extra_unit_cap"], 48)
        self.assertEqual(result.stats["adaptive_extra_queries"], 1)
        self.assertIn((productive, 2), calls)
        with (
            patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 0),
            patch.object(config, "JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES", 32),
            patch.object(config, "JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES", 16),
        ):
            self.assertEqual(
                jsearch_scraper._adaptive_budget_remaining(
                    estimated_units=82, base_units=50
                ),
                16,
            )


class ProviderReviewV12Tests(unittest.TestCase):
    def test_substantial_aggregator_record_can_reach_job_pass_for_review(self):
        resolver = JobSourceResolver()
        source = resolver.resolve(provider_job(), fetch=False)
        self.assertEqual(source.state, "ACTIVE_PROVIDER_STRUCTURED")
        self.assertFalse(source.official)
        self.assertTrue(source.corroborated)
        self.assertIn("approved_revalidation_required", source.notes)

        decision = JobGate(resolver).evaluate(provider_job(), fetch=False)
        self.assertEqual(decision.state, GateState.PASS)
        self.assertEqual(
            decision.metadata["signal_confidence"],
            "provider_structured_review",
        )

    def test_provider_url_is_exposed_as_unverified_review(self):
        annotated = JobGate().annotate(provider_job(), fetch=False)
        url, status, source_type, reason = select_job_url(annotated, probe=False)
        self.assertEqual(url, provider_job()["job_apply_link"])
        self.assertEqual(status, "unverified_review")
        self.assertEqual(source_type, "provider_structured")
        self.assertIn("requires_revalidation", reason)
        self.assertTrue(annotated["job_signal_review_required"])

    def test_thin_provider_record_remains_unverified(self):
        source = JobSourceResolver().resolve(
            provider_job(job_description="Short provider summary."),
            fetch=False,
        )
        self.assertEqual(source.state, "SOURCE_UNRESOLVED")

    def test_authoritative_absence_blocks_provider_review(self):
        resolver = JobSourceResolver()
        source = resolver._provider_structured_review_fallback(
            provider_job(),
            ["https://www.jobleads.com/us/job/example-123"],
            [SourceAttempt(
                "https://jobs.example.com/123",
                "ats",
                "official_ats_job_absent",
                200,
                authoritative=True,
            )],
            company_name="Example Corp",
            company_domain="example.com",
            authoritative_absence=True,
            inactive_candidates=[],
        )
        self.assertIsNone(source)


class AccountBoundaryV12Tests(unittest.TestCase):
    def test_large_provider_discovered_company_is_rejected_before_people_search(self):
        job = provider_job(
            employer_name="NVIDIA",
            employer_website=None,
            job_title="System Software Engineer",
            _matched_role="Software Engineer",
            job_apply_link="https://simplify.jobs/p/abc/system-engineer",
            apply_options=[{
                "apply_link": "https://simplify.jobs/p/abc/system-engineer",
                "is_direct": False,
            }],
            canonical_employer_name="NVIDIA",
            canonical_job_title="System Software Engineer",
            _job_gate_state="PASS",
            _job_gate_reason="JOB_PASS",
            _job_gate_decision=GateDecision(
                "job", GateState.PASS, "JOB_PASS"
            ).to_dict(),
            _role_gate_state="PASS",
            _role_gate_reason="ROLE_PASS",
            _role_gate_decision=GateDecision(
                "role", GateState.PASS, "ROLE_PASS"
            ).to_dict(),
        )
        org = apollo_client.OrgEnrichment(
            found=True,
            name="NVIDIA",
            domain="nvidia.com",
            employee_count=30000,
            industry="Computer Software",
            raw={"primary_domain": "nvidia.com"},
        )
        with (
            patch.object(hiring_manager.apollo, "enrich_organization", return_value=org) as enrich,
            patch.object(hiring_manager.apollo, "search_people_at_company") as people,
            patch.object(hiring_manager.time, "sleep"),
        ):
            leads, stats = hiring_manager._process_company_strict([job])

        self.assertEqual(enrich.call_args.kwargs["domain"], "")
        people.assert_not_called()
        self.assertEqual(stats["account_reject"], 1)
        self.assertEqual(leads[0]["_final_state"], "REJECT")
        self.assertIn("TOO_LARGE", str(leads[0]["_final_primary_reason"]))


class ApprovalBoundaryV12Tests(unittest.TestCase):
    def test_provider_only_source_cannot_enroll_without_live_revalidation(self):
        fields = {
            "Company": "Example Corp",
            "Website": "https://example.com",
            "Open Role": "Customer Success Manager",
            "Job URL": "https://www.jobleads.com/us/job/example-123",
            "Job ID": "provider-1",
            "Hiring Manager": "Alex Smith",
            "HM Title": "VP Customer Success",
            "LinkedIn": "https://linkedin.com/in/alex",
            "Apollo Person ID": "p1",
            "Email": "alex@example.com",
            "Role Bucket": "Customer Success",
            "Final Decision": "FINAL_PASS",
            "Validation Version": config.VALIDATION_VERSION,
            "Validated At": datetime.now(timezone.utc).isoformat(),
        }
        provider_source = ResolvedJobSource(
            state="ACTIVE_PROVIDER_STRUCTURED",
            source_type="provider_structured",
            active=True,
            corroborated=True,
        )
        with (
            patch.object(config, "VALIDATION_SIGNING_KEY", "offline-test-key"),
            patch("approved_revalidation.JobSourceResolver") as resolver_cls,
        ):
            fields["Validation Fingerprint"] = validation_fingerprint(fields)
            resolver_cls.return_value.resolve.return_value = provider_source
            valid, reason = revalidate_approved_record({"fields": fields})
        self.assertFalse(valid)
        self.assertIn("Job source revalidation failed", reason)


if __name__ == "__main__":
    unittest.main()
