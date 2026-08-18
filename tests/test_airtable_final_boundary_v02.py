from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import airtable_client
import config
import instantly_client


class AirtableFinalBoundaryTests(unittest.TestCase):
    def _lead(self, state: str, key: str) -> dict:
        relevance = {
            "FINAL_PASS": "accept",
            "NEEDS_CHECK": "review",
            "UNVERIFIED": "review",
        }.get(state)
        return {
            "lead_key": key,
            "employer_name": "Example Inc",
            "canonical_company_name": "Example Inc",
            "outbound_company_name": "Example",
            "outbound_company_confidence": "medium",
            "outbound_company_identity_key": "domain:example.com",
            "_outbound_company_hold": False,
            "company_domain": "example.com",
            "job_title": "Staff Accountant",
            "canonical_job_title": "Staff Accountant",
            "outbound_role_name": "Staff Accountant",
            "related_outbound_roles": ["Staff Accountant"],
            "outbound_role_confidence": "medium",
            "job_id": f"job-{key}",
            "_role_bucket": "finance",
            "_matched_role": "Staff Accountant",
            "hiring_manager_name": "Jane Doe",
            "hiring_manager_title": "Controller",
            "hiring_manager_email": f"{key}@example.com",
            "hiring_manager_confidence": "verified",
            "_final_state": state,
            "_final_primary_reason": state,
            "_final_secondary_reasons": [],
            "_airtable_relevance": relevance,
            "_validation_version": config.VALIDATION_VERSION,
            "_gate_decisions": {
                "job": {"state": "PASS"},
                "account": {"state": "PASS"},
                "role": {"state": "PASS"},
                "contact": {"state": "PASS"},
                "email": {"state": "PASS"},
            },
        }

    @patch.object(airtable_client, "validate_preflight")
    @patch.object(airtable_client, "_get_existing_leads", return_value={})
    @patch.object(airtable_client, "request_with_retry")
    def test_push_surfaces_actionable_review_states(self, request_mock, _existing, _preflight):
        response = Mock()
        response.json.return_value = {"records": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        response.text = ""
        request_mock.return_value = response
        jobs = [
            self._lead("FINAL_PASS", "pass"),
            self._lead("NEEDS_CHECK", "review"),
            self._lead("REROUTE", "reroute"),
            self._lead("UNVERIFIED", "unknown"),
            self._lead("REJECT", "reject"),
        ]
        result = airtable_client.push_leads(jobs)
        self.assertTrue(result["strict_mode"])
        self.assertEqual(result["reviewable"], 3)
        self.assertEqual(result["final_pass"], 1)
        self.assertEqual(result["needs_check"], 2)
        submitted = request_mock.call_args.kwargs["json_body"]["records"]
        decisions = {row["fields"]["Final Decision"] for row in submitted}
        self.assertEqual(decisions, {"FINAL_PASS", "NEEDS_CHECK", "UNVERIFIED"})
        relevance = {row["fields"]["Relevance"] for row in submitted}
        self.assertEqual(relevance, {"accept", "review"})

    @patch.object(airtable_client, "validate_preflight")
    @patch.object(airtable_client, "request_with_retry")
    def test_approved_poll_accepts_actionable_validated_rows(self, request_mock, _preflight):
        from validation_integrity import validation_fingerprint

        def _signed(rid, decision, email):
            fields = {
                "Status": "Approved", "Final Decision": decision,
                "Validation Version": config.VALIDATION_VERSION, "Email": email,
                "Apollo Email Status": "verified", "Email Validation": "PASS",
                "Contact Alignment": "PASS",
                "Company": "Example", "Role Bucket": "finance",
                "Outbound Company": "Example",
                "Outbound Company Confidence": "high",
                "Outbound Company Identity": "domain:example.com",
                "Outbound Hold": False,
                "Outbound Role": "Staff Accountant",
                "Campaign ID": "camp-1", "Website": "https://example.com",
            }
            fields["Validation Fingerprint"] = validation_fingerprint(fields)
            return {"id": rid, "fields": fields}

        response = Mock()
        response.json.return_value = {
            "records": [
                _signed("pass", "FINAL_PASS", "a@example.com"),
                _signed("review", "NEEDS_CHECK", "b@example.com"),
                # legacy: no current validation metadata -> must be dropped (no write)
                {"id": "legacy", "fields": {"Status": "Approved"}},
                # stale version, even if otherwise shaped -> dropped
                {"id": "stale", "fields": {"Status": "Approved", "Final Decision": "FINAL_PASS",
                                           "Validation Version": "v0", "Email": "c@example.com"}},
            ]
        }
        response.text = ""
        request_mock.return_value = response
        records = airtable_client.get_approved_leads()
        # Only current, correctly-signed, actionable rows are accepted.
        self.assertEqual([row["id"] for row in records], ["pass", "review"])

    def test_instantly_rejects_terminal_decision(self):
        record = {
            "id": "rec",
            "fields": {
                "Final Decision": "REROUTE",
                "Validation Version": "v",
            },
        }
        with self.assertRaisesRegex(ValueError, "not actionable"):
            instantly_client.airtable_record_to_lead(record)

    def test_airtable_fields_include_auditable_decision(self):
        fields = airtable_client._job_to_fields(self._lead("FINAL_PASS", "pass"))
        self.assertEqual(fields["Final Decision"], "FINAL_PASS")
        self.assertEqual(fields["Relevance"], "accept")
        self.assertEqual(fields["Validation Version"], config.VALIDATION_VERSION)
        self.assertIn("gate_decisions", fields["Evidence Bundle"])

    def test_airtable_prefers_verified_official_job_source(self):
        lead = self._lead("FINAL_PASS", "pass")
        lead.update({
            "job_apply_link": "https://aggregator.example/job/123",
            "job_url_selected": "https://aggregator.example/job/123",
            "job_url_status": "unverified_review",
            "job_url_source": "aggregator",
            "official_job_url": "https://jobs.example.com/positions/123",
            "official_job_source_type": "ats",
            "official_job_status": "ACTIVE_VERIFIED",
        })
        fields = airtable_client._job_to_fields(lead)
        self.assertEqual(fields["Job URL"], "https://jobs.example.com/positions/123")
        self.assertEqual(fields["Official Source"], "https://jobs.example.com/positions/123")
        self.assertEqual(fields["Job URL Source"], "ats")
        self.assertEqual(fields["Job URL Status"], "verified")


if __name__ == "__main__":
    unittest.main()
