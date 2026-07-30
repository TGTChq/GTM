"""Incident B regression tests: Airtable 422 diagnosis + reconciliation.

The 2026-07-30 validation run failed with `Airtable batch create failed: 422
Unprocessable Entity` on 20 of 21 records, but the field-level error body was
discarded (only the bare status was logged), the non-zero process exit let
Railway ON_FAILURE relaunch a full run, and the exact offending field could not
be identified afterwards.

These tests pin the provable corrections: a deterministic 422 is non-retryable
and its sanitized field-level context is retained; failed writes stay reconciled
as failed (attempted = created + skipped + failed); success carries no error
noise. (The exact payload-field serialization fix is gated on one bounded
validation write per AIRTABLE_422_ROOT_CAUSE.md.)
"""
from __future__ import annotations

import unittest
from unittest.mock import patch, Mock

import requests

import re

import airtable_client
import config
import http_utils
from airtable_client import _sanitize_airtable_error, push_leads, _job_to_fields
from http_utils import RETRYABLE_STATUS_CODES

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _http_error(status, body=None, text=""):
    resp = Mock()
    resp.status_code = status
    if body is not None:
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError("no json")
    resp.text = text
    err = requests.HTTPError(f"{status} Client Error")
    err.response = resp
    return err


def _lead(key):
    return {
        "lead_key": key,
        "_final_state": "FINAL_PASS",
        "employer_name": "Acme Robotics",
        "canonical_company_name": "Acme Robotics",
        "hiring_manager_name": "Test Person",
        "hiring_manager_email": f"{key}@example.com",
        "job_title": "Staff Accountant",
        "_matched_role": "Staff Accountant",
    }


class SanitizeAirtableErrorTests(unittest.TestCase):
    def test_422_extracts_field_context_and_is_non_retryable(self):
        exc = _http_error(422, {"error": {
            "type": "INVALID_VALUE_FOR_COLUMN",
            "message": 'Field "Employees" cannot accept the provided value.',
        }})
        d = _sanitize_airtable_error(exc)
        self.assertEqual(d["status"], 422)
        self.assertEqual(d["error_type"], "INVALID_VALUE_FOR_COLUMN")
        self.assertIn("Employees", d["message"])
        self.assertFalse(d["retryable"])

    def test_retryable_status_classified_retryable(self):
        self.assertTrue(_sanitize_airtable_error(_http_error(503, {"error": {"type": "X"}}))["retryable"])
        self.assertTrue(_sanitize_airtable_error(_http_error(429))["retryable"])

    def test_exception_without_response_is_handled(self):
        d = _sanitize_airtable_error(ValueError("boom"))
        self.assertIsNone(d["status"])
        self.assertFalse(d["retryable"])
        self.assertIn("boom", d["message"])

    def test_422_is_not_in_http_retryable_set(self):
        # A deterministic schema error must not be retried at the HTTP layer.
        self.assertNotIn(422, RETRYABLE_STATUS_CODES)


class PushLeadsReconciliationTests(unittest.TestCase):
    def _run(self, *, fail):
        jobs = [_lead("acme.com|a@example.com|finance"), _lead("beta.com|b@example.com|finance")]
        with (
            patch.object(airtable_client, "validate_preflight", return_value=None),
            patch.object(airtable_client, "_get_existing_leads", return_value={}),
            patch.object(config, "AIRTABLE_RATE_LIMIT_DELAY", 0),
            patch.object(config, "AIRTABLE_SUPPRESS_EXISTING_COMPANY", False),
        ):
            if fail:
                with patch.object(airtable_client, "request_with_retry",
                                  side_effect=_http_error(422, {"error": {
                                      "type": "INVALID_VALUE_FOR_COLUMN",
                                      "message": 'Field "Employees" cannot accept value'}})):
                    return push_leads(jobs)
            else:
                resp = Mock()
                with (
                    patch.object(airtable_client, "request_with_retry", return_value=resp),
                    patch.object(airtable_client, "safe_json",
                                 return_value={"records": [{"id": "r1"}, {"id": "r2"}]}),
                ):
                    return push_leads(jobs)

    def test_deterministic_422_records_field_context_and_stays_failed(self):
        result = self._run(fail=True)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["created_lead_keys"], [])
        self.assertEqual(len(result["error_details"]), 1)
        detail = result["error_details"][0]
        self.assertEqual(detail["status"], 422)
        self.assertEqual(detail["error_type"], "INVALID_VALUE_FOR_COLUMN")
        self.assertFalse(detail["retryable"])
        self.assertEqual(detail["operation"], "create")

    def test_reconciliation_attempted_equals_created_plus_failed(self):
        result = self._run(fail=True)
        attempted = len(result["failed_lead_keys"]) + len(result["created_lead_keys"])
        self.assertEqual(attempted, result["created"] + result["failed"])
        self.assertEqual(set(result["failed_lead_keys"]),
                         {"acme.com|a@example.com|finance", "beta.com|b@example.com|finance"})

    def test_success_creates_without_error_details(self):
        result = self._run(fail=False)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["error_details"], [])


class PayloadSchemaConformanceTests(unittest.TestCase):
    """The proven HTTP 422 root cause: `Posted At` (Airtable dateTime) received a
    raw `job_posted_at_timestamp` unix integer, which typecast cannot coerce.
    Confirmed against the live Leads schema (AIRTABLE_422_ROOT_CAUSE.md)."""

    def _fields(self, **over):
        job = {
            "lead_key": "replit.com|x@replit.com|gtm_revenue",
            "_final_state": "FINAL_PASS",
            "employer_name": "Replit", "company_domain": "replit.com",
            "job_title": "Head of GTM", "_matched_role": "GTM Engineer",
            "hiring_manager_name": "A B", "hiring_manager_email": "x@replit.com",
            "job_apply_link": "https://replit.com/jobs/1",
            "company_employee_count": 120, "company_founded_year": 2016,
            "_role_relevance_score": 80, "job_age_days": 5,
            "job_posted_at_datetime_utc": "2026-07-20T09:15:00Z",
        }
        job.update(over)
        return _job_to_fields(job)

    def test_failed_production_payload_posted_at_timestamp_now_iso(self):
        # Reproduces the failed-row shape: a contactable lead whose only posting
        # date is the raw unix `job_posted_at_timestamp` (no datetime_utc).
        fields = self._fields(job_posted_at_datetime_utc=None,
                              job_posted_at_timestamp=1753000000)
        self.assertIsInstance(fields["Posted At"], str)
        self.assertRegex(fields["Posted At"], _ISO)  # ISO string, not a bare int

    def test_iso_posted_at_passthrough(self):
        self.assertEqual(self._fields()["Posted At"], "2026-07-20T09:15:00Z")

    def test_non_numeric_employee_count_is_omitted_and_size_band_safe(self):
        fields = self._fields(company_employee_count="51-200")
        self.assertNotIn("Employees", fields)          # omitted, never fabricated
        self.assertEqual(fields["Size Band"], "unknown")  # no crash

    def test_non_numeric_founded_year_is_omitted(self):
        self.assertNotIn("Founded", self._fields(company_founded_year="N/A"))

    def test_clean_numeric_fields_preserved(self):
        fields = self._fields()
        self.assertEqual(fields["Employees"], 120)
        self.assertEqual(fields["Founded"], 2016)
        self.assertEqual(fields["Relevance Score"], 80)
        self.assertEqual(fields["Job Age Days"], 5)

    def test_all_typed_fields_schema_valid_for_failed_shape(self):
        # Every number/dateTime field is now numeric or ISO or omitted.
        fields = self._fields(job_posted_at_datetime_utc=None,
                              job_posted_at_timestamp=1753000000,
                              company_employee_count="unknown")
        for numeric in ("Job Age Days", "Relevance Score", "Employees", "Founded"):
            if numeric in fields:
                self.assertIsInstance(fields[numeric], (int, float))
        if "Posted At" in fields:
            self.assertIsInstance(fields["Posted At"], str)


if __name__ == "__main__":
    unittest.main()
