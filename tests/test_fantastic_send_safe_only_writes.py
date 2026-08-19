"""COMMIT 2 -- AIRTABLE_WRITE_SEND_SAFE_ONLY.

When ON, push_leads creates a lead row ONLY when send_safe_facts() passes
(disposition-label-INDEPENDENT -- never Final Decision alone), stamps genuine
send-safe Fantastic rows Approved via the unchanged auto-approval logic, and
withholds non-send-safe candidates from Airtable while preserving them as metrics.
Default OFF reproduces today's behavior exactly. Approved Sync is untouched.
"""

import unittest
from unittest.mock import Mock, patch

import airtable_client
import config
from validation_integrity import validation_fingerprint


def _fantastic_job(lead_key, **over):
    """A strict-state Fantastic lead whose _job_to_fields output is send-safe
    unless an override breaks a specific fact."""
    job = {
        "lead_key": lead_key,
        "_final_state": "FINAL_PASS",
        "_final_primary_reason": "FINAL_PASS",
        "_fantastic_internal_id": "2317600001",
        "_acquisition_source": "fantastic_jobs_linkedin",
        "canonical_company_name": "Acme",
        "company_domain": lead_key.split("|")[0],
        "outbound_company_name": "Acme",
        "outbound_company_confidence": "high",
        "outbound_company_identity_key": "domain:" + lead_key.split("|")[0],
        "outbound_role_name": "VP Information Technology",
        "outbound_role_confidence": "high",
        "canonical_job_title": "VP Information Technology",
        "_role_bucket": "engineering",
        "_matched_role": "VP Information Technology",
        "role_focus": "infrastructure",
        "hiring_manager_email": "jane@" + lead_key.split("|")[0],
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


def _push(jobs, *, send_safe_only):
    last = {"n": 0}

    def fake_req(method, url, **kw):
        body = kw.get("json_body") or {}
        if isinstance(body, dict) and "records" in body:
            last["n"] = len(body["records"])
        return Mock()

    def fake_safe_json(resp):
        return {"records": [{"id": f"r{i}"} for i in range(last["n"])]}

    with (
        patch.object(airtable_client, "validate_preflight", return_value=None),
        patch.object(airtable_client, "_get_existing_leads", return_value={}),
        patch.object(config, "AIRTABLE_RATE_LIMIT_DELAY", 0),
        patch.object(config, "AIRTABLE_WRITE_SEND_SAFE_ONLY", send_safe_only),
        patch.object(config, "FANTASTIC_AUTO_APPROVE_SEND_SAFE", True),
        patch.object(airtable_client, "request_with_retry", side_effect=fake_req),
        patch.object(airtable_client, "safe_json", side_effect=fake_safe_json),
    ):
        return airtable_client.push_leads(jobs)


def _status(job):
    return airtable_client._job_to_fields(job).get("Status")


def _is_send_safe(job):
    return airtable_client.send_safe_facts(airtable_client._job_to_fields(job))[0]


class SendSafeOnlyWriteTests(unittest.TestCase):
    def setUp(self):
        # A: genuine send-safe FINAL_PASS.
        self.safe = _fantastic_job("acme.com|jane@acme.com|engineering")
        # B: FINAL_PASS but low outbound-company confidence -> NOT send-safe. Proves
        #    the gate is send_safe_facts, NOT Final Decision alone.
        self.final_pass_not_safe = _fantastic_job(
            "beta.com|jane@beta.com|engineering",
            outbound_company_confidence="low")
        # C: NEEDS_CHECK label but all facts send-safe -> IS send-safe (the 39-row
        #    case). Must still be written + Approved when the policy is ON.
        self.needs_check_but_safe = _fantastic_job(
            "gamma.com|jane@gamma.com|engineering",
            _final_state="NEEDS_CHECK", _final_primary_reason="NEEDS_CHECK")

    def test_fixture_sanity(self):
        self.assertTrue(_is_send_safe(self.safe))
        self.assertFalse(_is_send_safe(self.final_pass_not_safe))
        self.assertTrue(_is_send_safe(self.needs_check_but_safe))
        self.assertEqual(_status(self.safe), config.AIRTABLE_STATUS_APPROVED)
        self.assertEqual(_status(self.final_pass_not_safe), config.AIRTABLE_STATUS_PENDING)
        self.assertEqual(_status(self.needs_check_but_safe), config.AIRTABLE_STATUS_APPROVED)

    def test_off_writes_everything_reviewable(self):
        res = _push([self.safe, self.final_pass_not_safe, self.needs_check_but_safe],
                    send_safe_only=False)
        self.assertEqual(res["created"], 3)
        self.assertEqual(res["not_written_not_send_safe"], 0)

    def test_on_writes_only_send_safe(self):
        res = _push([self.safe, self.final_pass_not_safe, self.needs_check_but_safe],
                    send_safe_only=True)
        # A and C written; B (FINAL_PASS but not send-safe) withheld.
        self.assertEqual(res["created"], 2)
        self.assertEqual(res["not_written_not_send_safe"], 1)
        self.assertIn("beta.com|jane@beta.com|engineering",
                      res["not_written_not_send_safe_lead_keys"])
        self.assertNotIn("acme.com|jane@acme.com|engineering",
                         res["not_written_not_send_safe_lead_keys"])

    def test_on_final_pass_not_send_safe_is_withheld_not_label_gated(self):
        # A FINAL_PASS row is NOT written when it fails send_safe_facts -- proving the
        # gate is factual send-safety, never the Final Decision label.
        res = _push([self.final_pass_not_safe], send_safe_only=True)
        self.assertEqual(res["created"], 0)
        self.assertEqual(res["not_written_not_send_safe"], 1)

    def test_on_non_final_pass_but_send_safe_is_written_and_approved(self):
        res = _push([self.needs_check_but_safe], send_safe_only=True)
        self.assertEqual(res["created"], 1)
        self.assertEqual(res["not_written_not_send_safe"], 0)
        # Label-independent auto-approval still stamps it Approved on create.
        self.assertEqual(_status(self.needs_check_but_safe), config.AIRTABLE_STATUS_APPROVED)

    def test_default_flag_is_off(self):
        self.assertFalse(config.AIRTABLE_WRITE_SEND_SAFE_ONLY)

    def test_approved_sync_gate_is_the_same_send_safe_facts(self):
        # Approved Sync's pre-enrollment guard delegates to send_safe_facts, so the
        # write policy and the enrollment gate can never diverge. A withheld row,
        # were it ever Approved, would still be re-checked by the identical rule.
        ok_write, reason_write = airtable_client.send_safe_facts(
            airtable_client._job_to_fields(self.final_pass_not_safe))
        verdict = airtable_client.approved_row_eligibility(
            airtable_client._job_to_fields(self.final_pass_not_safe))
        self.assertFalse(ok_write)
        self.assertNotEqual(verdict, "eligible")


if __name__ == "__main__":
    unittest.main()
