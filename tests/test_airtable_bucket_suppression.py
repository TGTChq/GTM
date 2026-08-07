"""Regression tests: Airtable review/delivery dedup is FUNCTION-aware.

The production bug (fixed here) was that a company already present in Airtable
suppressed EVERY new function for that company, because the suppression key was
company-only (domain/name, no role bucket). These tests lock in:

* same company + DIFFERENT function  -> preserved (created)
* same company + SAME function       -> suppressed
* exact lead_key already present      -> skipped_existing (unchanged)
* explicit ACCOUNT-LEVEL exclusion    -> still hard-blocks the whole company
* Error/Rejected rows                 -> retryable (never suppress)
* key-shape unit contracts
* delivery reconciliation holds with both suppression tiers
"""

import unittest
from unittest.mock import Mock, patch

import airtable_client
import config
from airtable_client import (
    push_leads,
    _company_function_keys_from_fields,
    _company_function_keys_from_job,
    _company_identity_keys_from_fields,
)


def _job(bucket, email, *, company="Acme", domain="acme.com"):
    return {
        "lead_key": f"{domain}|{email}|{bucket}",
        "_final_state": "FINAL_PASS",
        "_role_bucket": bucket,
        "employer_name": company,
        "canonical_company_name": company,
        "company_domain": domain,
        "hiring_manager_name": "HM " + bucket,
        "hiring_manager_email": email,
        "hiring_manager_confidence": "high",
        "job_title": "Role " + bucket,
        "_matched_role": "Account Executive",
    }


def _existing(bucket, *, company="Acme", domain="acme.com", email="old@acme.com",
              status="Pending"):
    key = f"{domain}|{email}|{bucket}"
    return {key: {"id": "rec_" + bucket, "fields": {
        "Lead Key": key, "Company": company, "Website": f"https://{domain}",
        "Role Bucket": bucket, "Status": status,
    }}}


def _push(jobs, existing, *, account_level=False, function_level=True):
    # The fake echoes back exactly the number of records each POST/PATCH submits,
    # so the client's "returned N records" reconciliation guard is satisfied
    # regardless of how many rows survived suppression.
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
        patch.object(airtable_client, "_get_existing_leads", return_value=existing),
        patch.object(config, "AIRTABLE_RATE_LIMIT_DELAY", 0),
        patch.object(config, "AIRTABLE_SUPPRESS_ACCOUNT_LEVEL", account_level),
        patch.object(config, "AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION", function_level),
        patch.object(airtable_client, "request_with_retry", side_effect=fake_req),
        patch.object(airtable_client, "safe_json", side_effect=fake_safe_json),
    ):
        return push_leads(jobs)


class BucketAwareSuppressionTests(unittest.TestCase):
    # 1
    def test_same_company_different_function_is_preserved(self):
        res = _push([_job("engineering", "e@acme.com")], _existing("gtm_revenue"))
        self.assertEqual(res["created"], 1)
        self.assertEqual(res["skipped_existing_company"], 0)
        self.assertNotIn("acme.com|e@acme.com|engineering",
                         res["suppressed_company_lead_keys"])

    # 2
    def test_same_company_same_function_is_suppressed(self):
        res = _push([_job("gtm_revenue", "new@acme.com")], _existing("gtm_revenue"))
        self.assertEqual(res["created"], 0)
        self.assertIn("acme.com|new@acme.com|gtm_revenue",
                      res["suppressed_company_lead_keys"])

    # 3 (Marketing existing does not suppress Sales, and vice-versa)
    def test_marketing_existing_does_not_suppress_sales_and_vice_versa(self):
        res = _push([_job("gtm_revenue", "s@acme.com")], _existing("marketing"))
        self.assertEqual(res["created"], 1)
        res2 = _push([_job("marketing", "m@acme.com")], _existing("gtm_revenue"))
        self.assertEqual(res2["created"], 1)

    # 4 exact lead_key present -> skipped_existing, not suppressed
    def test_exact_lead_key_present_is_skipped_existing(self):
        existing = _existing("gtm_revenue", email="dup@acme.com")
        res = _push([_job("gtm_revenue", "dup@acme.com")], existing)
        self.assertEqual(res["created"], 0)                     # not re-created
        self.assertEqual(res["suppressed_company_lead_keys"], [])  # not a suppression
        self.assertEqual(res["suppressed_account_lead_keys"], [])

    # 5 account-level exclusion still hard-blocks a different function
    def test_account_level_exclusion_blocks_whole_company(self):
        res = _push([_job("engineering", "e@acme.com")], _existing("gtm_revenue"),
                    account_level=True)
        self.assertEqual(res["created"], 0)
        self.assertIn("acme.com|e@acme.com|engineering",
                      res["suppressed_account_lead_keys"])
        self.assertEqual(res["suppressed_company_lead_keys"], [])

    # 6 retryable-status carve-out preserved (Rejected/Error never suppress)
    def test_rejected_or_error_status_does_not_suppress(self):
        for status in ("Rejected", "Error"):
            existing = _existing("gtm_revenue", status=status)
            res = _push([_job("gtm_revenue", "new@acme.com")], existing)
            self.assertEqual(res["created"], 1, f"{status} should be retryable")

    # 7 CRM/account exclusion is a SEPARATE, opt-in tier (default off)
    def test_account_level_defaults_off_so_functions_are_preserved(self):
        res = _push([_job("engineering", "e@acme.com")], _existing("gtm_revenue"))
        self.assertEqual(res["created"], 1)  # not blocked by default
        self.assertEqual(res["suppressed_account_lead_keys"], [])


class KeyShapeTests(unittest.TestCase):
    def test_function_key_includes_lowercased_bucket_both_tiers(self):
        keys = _company_function_keys_from_fields(
            {"Company": "Acme", "Website": "https://acme.com", "Role Bucket": "Sales"})
        self.assertEqual(keys, {"domain:acme.com|bucket:sales", "name:acme|bucket:sales"})

    def test_blank_bucket_yields_empty_set(self):
        self.assertEqual(_company_function_keys_from_fields(
            {"Company": "Acme", "Website": "https://acme.com", "Role Bucket": ""}), set())

    def test_function_key_is_strict_subset_qualifier_of_company_key(self):
        job = {"employer_name": "Acme", "company_domain": "acme.com",
               "_role_bucket": "marketing"}
        comp = _company_identity_keys_from_fields(
            {"Company": "Acme", "Website": "https://acme.com"})
        fn = _company_function_keys_from_job(job)
        # every function key extends a company key with a bucket qualifier
        for k in fn:
            self.assertTrue(any(k.startswith(c + "|bucket:") for c in comp))


class ReconciliationTests(unittest.TestCase):
    def test_delivery_reconciles_with_both_suppression_tiers(self):
        # A batch that mixes: 1 created (new function), 1 same-function suppressed.
        existing = {}
        existing.update(_existing("gtm_revenue"))          # Acme+sales exists
        jobs = [_job("engineering", "e@acme.com"),         # different function -> created
                _job("gtm_revenue", "n@acme.com")]         # same function -> suppressed
        res = _push(jobs, existing)
        self.assertEqual(res["created"], 1)
        self.assertEqual(res["skipped_existing_company"], 1)
        # reviewable == created + skipped_existing + failed + other must hold via the
        # orchestrator report; here we assert the push_leads accounting is consistent.
        self.assertEqual(res["reviewable"], 2)
        self.assertEqual(
            res["created"] + res["skipped_existing"] + res["failed"]
            + res["skipped_existing_company"] + res["skipped_existing_account"], 2)


if __name__ == "__main__":
    unittest.main()
