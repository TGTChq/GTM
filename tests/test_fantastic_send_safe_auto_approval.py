"""SEND-SAFE auto-approval for Fantastic leads (disposition-label-independent).

A genuine Fantastic lead auto-approves to Status=Approved at creation iff its
stored FACTS are all send-safe (airtable_client.send_safe_facts). Approved Sync
(approved_row_eligibility) independently re-checks the same facts, so an
accidentally-Approved unsafe row can never enroll. The FINAL_PASS/NEEDS_CHECK/
UNVERIFIED label is NOT the criterion.
"""
from __future__ import annotations

import unittest

import config
import airtable_client
from validation_integrity import validation_fingerprint


def _fields(**over):
    """A fully send-safe row (facts, not label). Fingerprint signed last."""
    f = {
        "Status": "Approved",
        "Final Decision": "FINAL_PASS",
        "Validation Version": config.VALIDATION_VERSION,
        "Email": "jane@acme.com",
        "Apollo Email Status": "verified",
        "Email Validation": "PASS",
        "Contact Alignment": "PASS",
        "Firmographics Status": "PASS",
        "Company": "Acme",
        "Outbound Company": "Acme",
        "Outbound Company Confidence": "high",
        "Outbound Company Identity": "domain:acme.com",
        "Outbound Hold": False,
        "Outbound Role": "VP Information Technology",
        "Outbound Role Confidence": "high",
        "Role Bucket": "engineering",
        "Campaign ID": "camp-1",
        "Website": "https://acme.com",
        # REQUIRED for delivery (instantly_client.airtable_record_to_lead treats
        # Role Focus as a required outreach variable). A row without it is not
        # actually send-safe -- it dies at the delivery precheck.
        "Role Focus": "scaling the internal automation stack",
    }
    f.update(over)
    f["Validation Fingerprint"] = validation_fingerprint(f)
    return f


def _fantastic_job(**over):
    """A strict-state Fantastic lead whose _job_to_fields output is send-safe."""
    job = {
        "_final_state": "FINAL_PASS",
        "_final_primary_reason": "FINAL_PASS",
        "_fantastic_internal_id": "2317600001",
        "_acquisition_source": "fantastic_jobs_linkedin",
        "canonical_company_name": "Acme",
        "company_domain": "acme.com",
        "outbound_company_name": "Acme",
        "outbound_company_confidence": "high",
        "outbound_company_identity_key": "domain:acme.com",
        "outbound_role_name": "VP Information Technology",
        "outbound_role_confidence": "high",
        "canonical_job_title": "VP Information Technology",
        "_role_bucket": "engineering",
        "_matched_role": "VP Information Technology",
        "role_focus": "infrastructure",
        "hiring_manager_email": "jane@acme.com",
        "hiring_manager_name": "Jane Doe",
        "hiring_manager_title": "VP IT",
        "apollo_email_status": "verified",
        "_email_gate_state": "PASS",
        "_contact_gate_state": "PASS",
        "_account_gate_state": "PASS",
        "_job_gate_state": "PASS",
        "campaign_id": "camp-1",
        "company_employee_count": 300,
        "_validation_version": config.VALIDATION_VERSION,
    }
    job.update(over)
    return job


class SendSafeFactsTests(unittest.TestCase):
    def _safe(self, **over):
        ok, reason = airtable_client.send_safe_facts(_fields(**over))
        return ok, reason

    def test_final_pass_send_safe_is_safe(self):
        ok, _ = self._safe(**{"Final Decision": "FINAL_PASS"})
        self.assertTrue(ok)

    def test_needs_check_with_all_facts_is_safe(self):
        ok, _ = self._safe(**{"Final Decision": "NEEDS_CHECK"})
        self.assertTrue(ok)

    def test_unverified_with_apollo_verified_is_safe(self):
        ok, _ = self._safe(**{"Final Decision": "UNVERIFIED"})
        self.assertTrue(ok)

    def test_apollo_unverified_is_not_safe(self):
        ok, reason = self._safe(**{"Apollo Email Status": "unverified"})
        self.assertFalse(ok)
        self.assertEqual(reason, "apollo_email_not_verified")

    def test_no_contact_email_is_not_safe(self):
        ok, reason = self._safe(**{"Email": ""})
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_email")

    def test_email_gate_not_pass_is_not_safe(self):
        ok, reason = self._safe(**{"Email Validation": "NEEDS_CHECK"})
        self.assertFalse(ok)
        self.assertEqual(reason, "email_gate_not_pass")

    def test_outbound_hold_is_not_safe(self):
        # Hold set while BOTH confidences are high => no current qualifying
        # condition, i.e. stale workflow state. Still not safe, but now named.
        ok, reason = self._safe(**{"Outbound Hold": True})
        self.assertFalse(ok)
        self.assertEqual(reason, "outbound_hold_stale_no_current_condition")

    def test_hold_reason_attributes_the_company_side(self):
        ok, reason = self._safe(**{"Outbound Hold": True,
                                   "Outbound Company Confidence": "low"})
        self.assertFalse(ok)
        self.assertEqual(reason, "outbound_company_held_for_review")

    def test_hold_reason_attributes_the_role_side(self):
        # Previously reported as a COMPANY hold -- the mislabel that made 43 of the
        # 375 production holds look like ambiguous company names.
        ok, reason = self._safe(**{"Outbound Hold": True,
                                   "Outbound Role Confidence": "low"})
        self.assertFalse(ok)
        self.assertEqual(reason, "outbound_role_held_for_review")

    def test_hold_reason_attributes_both_sides(self):
        ok, reason = self._safe(**{"Outbound Hold": True,
                                   "Outbound Company Confidence": "low",
                                   "Outbound Role Confidence": "low"})
        self.assertFalse(ok)
        self.assertEqual(reason, "outbound_company_and_role_held_for_review")

    def test_missing_role_focus_is_not_send_safe(self):
        # Gate parity with delivery: a row that cannot build an Instantly payload
        # must never be marked send-safe and sent for human approval.
        ok, reason = self._safe(**{"Role Focus": ""})
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_role_focus")

    def test_ambiguous_company_is_not_safe(self):
        ok, reason = self._safe(**{"Outbound Company Confidence": "low"})
        self.assertFalse(ok)
        self.assertEqual(reason, "outbound_company_confidence_not_send_safe")

    def test_ambiguous_role_is_not_safe(self):
        ok, reason = self._safe(**{"Outbound Role Confidence": "low"})
        self.assertFalse(ok)
        self.assertEqual(reason, "outbound_role_confidence_not_send_safe")

    def test_firmographic_reject_is_not_safe(self):
        ok, reason = self._safe(**{"Firmographics Status": "REJECT"})
        self.assertFalse(ok)
        self.assertEqual(reason, "firmographic_contradiction")

    def test_missing_contact_alignment_is_not_safe(self):
        ok, reason = self._safe(**{"Contact Alignment": "NEEDS_CHECK"})
        self.assertFalse(ok)
        self.assertEqual(reason, "contact_not_aligned")

    def test_invalid_fingerprint_is_not_safe(self):
        f = _fields()
        f["Validation Fingerprint"] = "tampered"
        ok, reason = airtable_client.send_safe_facts(f)
        self.assertFalse(ok)
        self.assertEqual(reason, "validation_fingerprint_mismatch")

    def test_stale_validation_version_is_not_safe(self):
        ok, reason = self._safe(**{"Final Decision": "FINAL_PASS", "Validation Version": "v0.0.0"})
        self.assertFalse(ok)
        # (fingerprint is re-signed over the stale version in _fields, so the
        # version mismatch is the surfaced reason)
        self.assertEqual(reason, "validation_version_mismatch")


class ApprovedSyncFailCloseTests(unittest.TestCase):
    """Approved Sync must independently block an unsafe row even if its Status is
    accidentally Approved."""

    def test_accidentally_approved_unverified_is_blocked(self):
        cat, reason = airtable_client.approved_row_eligibility(
            _fields(**{"Apollo Email Status": "unverified"}))
        self.assertEqual(cat, "invalid")
        self.assertEqual(reason, "apollo_email_not_verified")

    def test_accidentally_approved_held_is_blocked(self):
        cat, _ = airtable_client.approved_row_eligibility(_fields(**{"Outbound Hold": True}))
        self.assertEqual(cat, "invalid")

    def test_send_safe_row_is_eligible(self):
        cat, _ = airtable_client.approved_row_eligibility(_fields())
        self.assertEqual(cat, "eligible")


class AutoApproveAtCreationTests(unittest.TestCase):
    def _status(self, job):
        return airtable_client._job_to_fields(job).get("Status")

    def test_final_pass_fantastic_send_safe_auto_approves(self):
        self.assertEqual(self._status(_fantastic_job(_final_state="FINAL_PASS")),
                         config.AIRTABLE_STATUS_APPROVED)

    def test_needs_check_fantastic_send_safe_auto_approves(self):
        self.assertEqual(self._status(_fantastic_job(_final_state="NEEDS_CHECK")),
                         config.AIRTABLE_STATUS_APPROVED)

    def test_unverified_fantastic_apollo_verified_auto_approves(self):
        self.assertEqual(self._status(_fantastic_job(_final_state="UNVERIFIED")),
                         config.AIRTABLE_STATUS_APPROVED)

    def test_apollo_unverified_fantastic_stays_pending(self):
        self.assertEqual(self._status(_fantastic_job(apollo_email_status="unverified")),
                         config.AIRTABLE_STATUS_PENDING)

    def test_held_fantastic_stays_pending(self):
        self.assertEqual(self._status(_fantastic_job(_outbound_company_hold=True)),
                         config.AIRTABLE_STATUS_PENDING)

    def test_non_fantastic_send_safe_stays_pending(self):
        job = _fantastic_job()
        job.pop("_fantastic_internal_id", None)
        job["_acquisition_source"] = "ats"
        self.assertEqual(self._status(job), config.AIRTABLE_STATUS_PENDING)

    def test_flag_off_disables_auto_approval(self):
        from unittest.mock import patch
        with patch.object(config, "FANTASTIC_AUTO_APPROVE_SEND_SAFE", False):
            self.assertEqual(self._status(_fantastic_job()), config.AIRTABLE_STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()
