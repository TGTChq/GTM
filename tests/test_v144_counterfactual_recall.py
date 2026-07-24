import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from job_filter import assess_us_eligibility, is_stale_job
from job_quality import assess_posting_integrity, normalize_job_identity
from job_gate import JobGate
from job_source_resolver import ResolvedJobSource
from run_free_source_shadow import _shadow_recall_recovery_metrics
from run_counterfactual_recall_replay import _evaluate


class V144CounterfactualRecallTests(unittest.TestCase):
    def _greenhouse_job(self, **overrides):
        job = {
            "job_id": "ats:greenhouse:acme:123",
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "job_description": "Acme builds workflow software for modern teams. " * 20,
            "job_apply_link": "https://job-boards.greenhouse.io/acme/jobs/123",
            "official_job_url": "https://job-boards.greenhouse.io/acme/jobs/123",
            "job_apply_is_direct": True,
            "_acquisition_source": "ats_greenhouse",
            "_ats_provider": "greenhouse",
            "_ats_board_identity_verified": True,
            "_provider_record_structured": True,
            "_greenhouse_detail_request_made": True,
            "_ats_source_updated_at": datetime.now(timezone.utc).isoformat(),
            "job_posted_at_datetime_utc": "",
            "job_offer_expiration_datetime_utc": "",
        }
        job.update(overrides)
        return job

    def test_active_greenhouse_unknown_age_enters_review_lane(self):
        job = self._greenhouse_job()
        stale, reason = is_stale_job(job, max_age_days=14)
        self.assertFalse(stale)
        self.assertEqual(reason, "")
        self.assertTrue(job["_freshness_review_required"])
        self.assertEqual(
            job["_freshness_review_reason"],
            "greenhouse_recently_updated_active_listing_unknown_first_published",
        )
        self.assertTrue(job["_approved_revalidation_required"])


    def test_old_greenhouse_update_does_not_bypass_age_policy(self):
        job = self._greenhouse_job(
            _ats_source_updated_at=(
                datetime.now(timezone.utc) - timedelta(days=45)
            ).isoformat()
        )
        stale, reason = is_stale_job(job, max_age_days=14)
        self.assertTrue(stale)
        self.assertEqual(reason, "greenhouse_first_published_unavailable")

    def test_unverified_greenhouse_unknown_age_still_rejects(self):
        job = self._greenhouse_job(_ats_board_identity_verified=False)
        stale, reason = is_stale_job(job, max_age_days=14)
        self.assertTrue(stale)
        self.assertEqual(reason, "greenhouse_first_published_unavailable")

    def test_expired_greenhouse_never_enters_review_lane(self):
        job = self._greenhouse_job(
            job_offer_expiration_datetime_utc=(
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat()
        )
        stale, reason = is_stale_job(job, max_age_days=14)
        self.assertTrue(stale)
        self.assertEqual(reason, "expired_job_posting")
        self.assertNotIn("_freshness_review_required", job)

    def test_structured_global_remote_includes_us_for_review(self):
        job = {
            "job_title": "Commercial Account Executive",
            "employer_name": "Common Room",
            "job_description": "Full-time remote role open worldwide.",
            "job_location": "Anywhere in the World",
            "job_country": "",
            "job_apply_link": "https://weworkremotely.com/remote-jobs/common-room-commercial-account-executive",
            "_acquisition_source": "weworkremotely",
            "_provider_record_structured": True,
            "job_is_remote": True,
        }
        result = assess_us_eligibility(job)
        self.assertTrue(result.eligible)
        self.assertEqual(result.scope, "global_includes_us")
        self.assertEqual(result.reason, "global_remote_includes_us_review")
        self.assertTrue(job["_global_remote_review_required"])
        self.assertTrue(job["_approved_revalidation_required"])

    def test_global_remote_with_foreign_only_restriction_still_rejects(self):
        job = {
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "job_description": "Remote worldwide, but candidates must be based in LATAM only.",
            "job_location": "Anywhere in the World",
            "job_apply_link": "https://himalayas.app/companies/acme/jobs/csm",
            "_acquisition_source": "himalayas",
            "_provider_record_structured": True,
            "job_is_remote": True,
        }
        result = assess_us_eligibility(job)
        self.assertFalse(result.eligible)
        self.assertEqual(result.scope, "foreign")
        self.assertTrue(result.reason.startswith("foreign_only_eligibility:"))

    def test_unstructured_global_remote_without_us_evidence_still_rejects(self):
        job = {
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "job_description": "Remote worldwide.",
            "job_location": "Anywhere in the World",
            "job_apply_link": "https://example-job-board.test/jobs/123",
            "job_is_remote": True,
        }
        result = assess_us_eligibility(job)
        self.assertFalse(result.eligible)
        self.assertEqual(result.scope, "global")

    def test_jsearch_us_market_global_remote_remains_rejected_without_structured_source(self):
        job = {
            "job_title": "Customer Success Manager",
            "employer_name": "Acme",
            "job_description": "Remote worldwide.",
            "job_location": "Anywhere in the World",
            "job_country": "US",
            "job_apply_link": "https://example.com/jobs/123",
            "job_is_remote": True,
            "_jsearch_country_filter": "us",
            "_jsearch_remote_filter_applied": True,
        }
        result = assess_us_eligibility(job)
        self.assertFalse(result.eligible)
        self.assertEqual(result.scope, "global")


    def test_global_remote_review_can_pass_job_gate_with_official_source(self):
        class Resolver:
            def resolve(self, job, fetch=None):
                return ResolvedJobSource(
                    state="ACTIVE_VERIFIED",
                    source_url="https://jobs.example.com/123",
                    source_type="company",
                    active=True,
                    canonical_title="Commercial Account Executive",
                    canonical_employer="Common Room",
                    description=(
                        "This is a full-time commercial account executive role. "
                        "The position is remote worldwide and does not require travel. "
                    ) * 12,
                    location_text="Anywhere in the World",
                    employment_type="Full-time",
                    date_posted=datetime.now(timezone.utc).isoformat(),
                    official=True,
                    corroborated=True,
                    retryable=False,
                )

        job = {
            "job_title": "Commercial Account Executive",
            "employer_name": "Common Room",
            "employer_website": "https://commonroom.io",
            "job_description": (
                "This is a full-time commercial account executive role. "
                "The position is remote worldwide and does not require travel. "
            ) * 12,
            "job_apply_link": "https://jobs.example.com/123",
            "job_apply_is_direct": True,
            "job_location": "Anywhere in the World",
            "job_employment_type": "Full-time",
            "_work_arrangement": "remote",
            "_work_arrangement_reason": "provider_remote",
            "_employment_quality": "full_time",
            "_employment_quality_reason": "structured_full_time",
            "_remote_scope": "global_includes_us",
            "_us_eligibility_reason": "global_remote_includes_us_review",
        }
        decision = JobGate(resolver=Resolver()).evaluate(job, fetch=False)
        self.assertEqual(decision.state_value, "PASS")



    def test_offline_counterfactual_replay_recovers_three_and_loses_zero(self):
        now = datetime.now(timezone.utc).isoformat()
        base_description = (
            "We build workflow software for growing businesses. "
            "This full-time role works with customers and internal teams. "
        ) * 12
        jobs = [
            {
                "job_id": "gh-1",
                "job_title": "Customer Success Manager",
                "employer_name": "Acme Systems",
                "employer_website": "https://acmesystems.com",
                "job_publisher": "Greenhouse",
                "job_description": base_description,
                "job_apply_link": "https://job-boards.greenhouse.io/acme/jobs/1",
                "official_job_url": "https://job-boards.greenhouse.io/acme/jobs/1",
                "job_apply_is_direct": True,
                "job_location": "United States",
                "job_country": "US",
                "job_is_remote": True,
                "job_employment_type": "Full-time",
                "_acquisition_source": "ats_greenhouse",
                "_ats_provider": "greenhouse",
                "_ats_board_identity_verified": True,
                "_provider_record_structured": True,
                "_greenhouse_detail_request_made": True,
                "_ats_source_updated_at": now,
                "job_posted_at_datetime_utc": "",
            },
            {
                "job_id": "wwr-1",
                "job_title": "Commercial Account Executive",
                "employer_name": "Common Room",
                "employer_website": "https://commonroom.io",
                "job_publisher": "We Work Remotely",
                "job_description": base_description + " Remote worldwide.",
                "job_apply_link": "https://weworkremotely.com/remote-jobs/common-room-commercial-account-executive",
                "job_location": "Anywhere in the World",
                "job_is_remote": True,
                "job_employment_type": "Full-time",
                "job_posted_at_datetime_utc": now,
                "_acquisition_source": "weworkremotely",
                "_provider_record_structured": True,
            },
            {
                "job_id": "id-1",
                "job_title": "Data Analyst",
                "employer_name": "Acme Labs",
                "employer_website": "https://acmelabs.com",
                "job_publisher": "Himalayas",
                "job_description": (
                    "About the company Beta Holdings is a software company. "
                    "Beta Holdings helps customers automate work. "
                    "Join Beta Holdings to improve customer outcomes. "
                ) * 6,
                "job_apply_link": "https://himalayas.app/companies/acme-labs/jobs/data-analyst",
                "job_location": "Remote - United States",
                "job_country": "US",
                "job_is_remote": True,
                "job_employment_type": "Full-time",
                "job_posted_at_datetime_utc": now,
                "_acquisition_source": "himalayas",
                "_provider_record_structured": True,
            },
            {
                "job_id": "foreign-1",
                "job_title": "Customer Success Manager",
                "employer_name": "ForeignCo",
                "employer_website": "https://foreignco.com",
                "job_publisher": "Himalayas",
                "job_description": base_description
                + " Remote worldwide, candidates must be based in LATAM only.",
                "job_apply_link": "https://himalayas.app/companies/foreignco/jobs/csm",
                "job_location": "Anywhere in the World",
                "job_is_remote": True,
                "job_employment_type": "Full-time",
                "job_posted_at_datetime_utc": now,
                "_acquisition_source": "himalayas",
                "_provider_record_structured": True,
            },
        ]
        strict = _evaluate(jobs, enabled=False, max_age_days=14)
        recovery = _evaluate(jobs, enabled=True, max_age_days=14)
        recovered = [
            key
            for key, before in strict.items()
            if not before["eligible"] and recovery[key]["eligible"]
        ]
        lost = [
            key
            for key, before in strict.items()
            if before["eligible"] and not recovery[key]["eligible"]
        ]
        self.assertEqual(set(recovered), {"gh-1", "wwr-1", "id-1"})
        self.assertEqual(lost, [])
        self.assertFalse(recovery["foreign-1"]["eligible"])

    def test_shadow_reports_recall_review_lanes(self):
        jobs = [
            {
                "employer_name": "Acme",
                "job_title": "Customer Success Manager",
                "_freshness_review_required": True,
                "_approved_revalidation_required": True,
            },
            {
                "employer_name": "Common Room",
                "job_title": "Commercial Account Executive",
                "_global_remote_review_required": True,
            },
            {
                "employer_name": "Beta",
                "job_title": "Data Analyst",
                "_employer_identity_review_required": True,
                "_employer_identity_repaired": True,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            metrics = _shadow_recall_recovery_metrics(str(path))
        self.assertEqual(metrics["review_lane_total"], 4)
        self.assertEqual(
            metrics["review_lane_counts"]["greenhouse_unknown_age_review"], 1
        )
        self.assertEqual(
            metrics["review_lane_counts"]["global_remote_includes_us_review"], 1
        )
        self.assertEqual(
            metrics["review_lane_counts"]["structured_identity_conflict_review"], 1
        )
        self.assertEqual(metrics["review_lane_counts"]["ats_identity_repaired"], 1)

    def _identity_conflict_job(self, structured=True):
        return {
            "job_title": "Customer Success Manager",
            "employer_name": "Acme Labs",
            "job_description": (
                "About the company Beta Holdings is a software company. "
                "Beta Holdings helps customers automate work. "
                "Join Beta Holdings to improve customer outcomes. "
            ) * 4,
            "job_apply_link": "https://himalayas.app/companies/acme-labs/jobs/csm",
            "employer_website": "https://acmelabs.com",
            "job_publisher": "Himalayas",
            "job_apply_is_direct": False,
            "_acquisition_source": "himalayas",
            "_provider_record_structured": structured,
        }

    def test_structured_identity_conflict_moves_to_review(self):
        job = self._identity_conflict_job(structured=True)
        result = assess_posting_integrity(job)
        self.assertTrue(result.eligible)
        self.assertTrue(job["_employer_identity_review_required"])
        self.assertEqual(
            job["_employer_identity_conflict_claimed_name"], "Beta Holdings"
        )
        self.assertTrue(job["_approved_revalidation_required"])

    def test_unstructured_identity_conflict_still_rejects(self):
        job = self._identity_conflict_job(structured=False)
        result = assess_posting_integrity(job)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "description_employer_identity_conflict")

    def test_verified_ats_board_identity_repairs_placeholder(self):
        job = {
            "job_title": "Data Analyst",
            "employer_name": "name",
            "_ats_board_company_name": "Acme Corporation",
            "job_apply_is_direct": True,
            "_ats_board_identity_verified": True,
            "job_description": "Acme Corporation builds analytics software.",
        }
        normalize_job_identity(job)
        self.assertEqual(job["employer_name"], "Acme Corporation")
        self.assertEqual(
            job["_employer_name_normalization"],
            "restored_verified_ats_board_company",
        )

    def test_unverified_ats_board_cannot_repair_placeholder(self):
        job = {
            "job_title": "Data Analyst",
            "employer_name": "name",
            "_ats_board_company_name": "Acme Corporation",
            "job_apply_is_direct": True,
            "_ats_board_identity_verified": False,
            "job_description": "Acme Corporation builds analytics software.",
        }
        normalize_job_identity(job)
        self.assertEqual(job["employer_name"], "name")


if __name__ == "__main__":
    unittest.main()
