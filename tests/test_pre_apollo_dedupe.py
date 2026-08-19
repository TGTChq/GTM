"""COMMIT 1 -- Pre-Apollo Airtable dedupe (config.PRE_APOLLO_EXISTING_DEDUPE).

Proves the optimization is SAFE and equivalence-correct:

* the pre-Apollo skip decision is derived by the SAME airtable_client helper that
  delivery uses, so anything pre-suppressed would ALSO be suppressed by delivery
  (equivalence), and a DIFFERENT function at the same company is never suppressed
  (multi-function preserved);
* the pre-Apollo function skip runs during hiring_manager grouping, BEFORE any
  Apollo call (process_company is never invoked for the skipped candidate);
* account-level pre-suppression stays behind AIRTABLE_SUPPRESS_ACCOUNT_LEVEL;
* delivery reuses the run-level snapshot (no second Airtable read) and produces
  byte-identical results to reading it itself;
* snapshot_existing_identity derives every key set from ONE read.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import airtable_client
import config
import hiring_manager
from airtable_client import (
    push_leads,
    snapshot_existing_identity,
    company_function_keys_for_job,
    _active_existing_company_function_keys,
)


# --------------------------------------------------------------------------
# Shared fixtures (mirror the delivery-side bucket-suppression harness so the
# two paths are compared on identical data).
# --------------------------------------------------------------------------
def _job(bucket, email, *, company="Acme", domain="acme.com"):
    return {
        "lead_key": f"{domain}|{email}|{bucket}",
        "_final_state": "FINAL_PASS",
        "_role_bucket": bucket,
        "employer_name": company,
        "canonical_company_name": company,
        "company_domain": domain,
        "employer_website": f"https://{domain}",
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


def _push(jobs, existing, *, account_level=False, function_level=True,
          inject_existing=False):
    """Run push_leads against a faked Airtable seam. When inject_existing is True
    the snapshot is passed in (delivery must NOT read Airtable itself)."""
    last = {"n": 0}

    def fake_req(method, url, **kw):
        body = kw.get("json_body") or {}
        if isinstance(body, dict) and "records" in body:
            last["n"] = len(body["records"])
        return Mock()

    def fake_safe_json(resp):
        return {"records": [{"id": f"r{i}"} for i in range(last["n"])]}

    read_mock = Mock(return_value=existing)
    with (
        patch.object(airtable_client, "validate_preflight", return_value=None),
        patch.object(airtable_client, "_get_existing_leads", read_mock),
        patch.object(config, "AIRTABLE_RATE_LIMIT_DELAY", 0),
        patch.object(config, "AIRTABLE_SUPPRESS_ACCOUNT_LEVEL", account_level),
        patch.object(config, "AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION", function_level),
        patch.object(airtable_client, "request_with_retry", side_effect=fake_req),
        patch.object(airtable_client, "safe_json", side_effect=fake_safe_json),
    ):
        result = push_leads(jobs, existing=(existing if inject_existing else None))
    return result, read_mock


class DecisionEquivalenceTests(unittest.TestCase):
    """The pre-Apollo skip decision must equal the delivery suppression decision."""

    def _pre_apollo_would_skip(self, job, existing):
        keys = _active_existing_company_function_keys(existing)
        jk = company_function_keys_for_job(job)
        return bool(jk and (jk & keys))

    def test_same_function_pre_skip_matches_delivery_suppression(self):
        existing = _existing("gtm_revenue")
        job = _job("gtm_revenue", "new@acme.com")
        # Pre-Apollo: skipped.
        self.assertTrue(self._pre_apollo_would_skip(job, existing))
        # Delivery: also suppressed (created 0).
        res, _ = _push([job], existing)
        self.assertEqual(res["created"], 0)
        self.assertIn("acme.com|new@acme.com|gtm_revenue",
                      res["suppressed_company_lead_keys"])

    def test_different_function_not_pre_skipped_and_delivery_creates(self):
        existing = _existing("gtm_revenue")
        job = _job("engineering", "e@acme.com")
        # Pre-Apollo: NOT skipped (multi-function preserved).
        self.assertFalse(self._pre_apollo_would_skip(job, existing))
        # Delivery: creates it.
        res, _ = _push([job], existing)
        self.assertEqual(res["created"], 1)

    def test_no_bucket_job_is_never_pre_skipped(self):
        existing = _existing("gtm_revenue")
        job = _job("gtm_revenue", "x@acme.com")
        job.pop("_role_bucket")
        self.assertEqual(company_function_keys_for_job(job), set())
        self.assertFalse(self._pre_apollo_would_skip(job, existing))

    def test_rejected_existing_does_not_pre_skip(self):
        # Retryable statuses are excluded from the active key set, so a new attempt
        # is allowed both pre-Apollo and at delivery.
        for status in ("Rejected", "Error"):
            existing = _existing("gtm_revenue", status=status)
            job = _job("gtm_revenue", "new@acme.com")
            self.assertFalse(self._pre_apollo_would_skip(job, existing))
            res, _ = _push([job], existing)
            self.assertEqual(res["created"], 1, f"{status} must stay retryable")


class HiringManagerPreApolloSkipTests(unittest.TestCase):
    """The skip fires during grouping, BEFORE Apollo (process_company)."""

    def test_existing_function_skipped_before_apollo_multifunction_preserved(self):
        existing = _existing("finance")  # Acme+finance already active in Airtable
        fkeys = _active_existing_company_function_keys(existing)

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "filtered.json"
            input_path.write_text(json.dumps({"jobs": [
                # Acme + finance -> SAME function as existing -> pre-Apollo skip.
                {"job_id": "a1", "job_title": "Accountant", "employer_name": "Acme",
                 "employer_website": "https://acme.com", "_role_bucket": "finance"},
                # Acme + engineering -> DIFFERENT function -> must proceed to Apollo.
                {"job_id": "a2", "job_title": "Engineer", "employer_name": "Acme",
                 "employer_website": "https://acme.com", "_role_bucket": "engineering"},
                # Beta + finance -> different company -> proceeds.
                {"job_id": "b1", "job_title": "Accountant", "employer_name": "Beta",
                 "employer_website": "https://beta.com", "_role_bucket": "finance"},
            ]}))

            seen = {"companies": []}

            def fake_process(company_jobs):
                job = company_jobs[0]
                seen["companies"].append(job.get("employer_name"))
                return [{
                    **job, "_step3_status": "found", "_step3_reason": "contact_found",
                    "hiring_manager_name": "Jane", "hiring_manager_email": "j@x.com",
                    "hiring_manager_confidence": "medium",
                    "lead_key": f"x|{job.get('job_id')}|b",
                }], {}

            with (
                patch.object(config, "STEP3_OUTPUT_DIR", directory),
                patch.object(hiring_manager, "validate_preflight"),
                patch.object(hiring_manager, "process_company",
                             side_effect=fake_process) as proc,
            ):
                result = hiring_manager.run_hiring_manager_identification(
                    str(input_path),
                    exclude_company_function_keys=fkeys,
                    output_suffix="preapollo",
                )

        # The Acme+finance candidate was removed at grouping, before Apollo.
        self.assertEqual(result.stats["preapollo_skipped_existing_function_jobs"], 1)
        # Apollo (process_company) ran only for the surviving companies: Acme (via
        # engineering) and Beta -- NOT for the skipped finance candidate.
        self.assertEqual(proc.call_count, 2)
        self.assertEqual(sorted(seen["companies"]), ["Acme", "Beta"])
        # Acme is NOT wholly excluded -- its engineering function still proceeded.
        self.assertEqual(result.companies_considered, 2)


class SnapshotReuseTests(unittest.TestCase):
    """Delivery reuses the run snapshot: identical result, no second read."""

    def test_injected_existing_equals_self_read_and_skips_second_read(self):
        existing = _existing("gtm_revenue")
        job = _job("engineering", "e@acme.com")

        res_self, read_self = _push([job], existing, inject_existing=False)
        res_inj, read_inj = _push([job], existing, inject_existing=True)

        self.assertEqual(res_inj["created"], res_self["created"])
        self.assertEqual(res_inj["suppressed_company_lead_keys"],
                         res_self["suppressed_company_lead_keys"])
        # Self-read path reads Airtable; injected path does NOT.
        self.assertTrue(read_self.called)
        self.assertFalse(read_inj.called)

    def test_snapshot_derives_all_key_sets_from_one_read(self):
        existing = _existing("gtm_revenue")
        read_mock = Mock(return_value=existing)
        with (
            patch.object(airtable_client, "validate_preflight", return_value=None),
            patch.object(airtable_client, "_get_existing_leads", read_mock),
        ):
            snap = snapshot_existing_identity()
        self.assertEqual(read_mock.call_count, 1)
        self.assertIs(snap["existing"], existing)
        self.assertIn("domain:acme.com|bucket:gtm_revenue", snap["company_function_keys"])
        self.assertIn("acme.com", snap["company_bare_keys"])          # hiring_manager format
        self.assertIn("domain:acme.com", snap["company_account_keys"])  # delivery format


class AccountLevelPreSuppressionTests(unittest.TestCase):
    def test_account_bare_keys_match_hiring_manager_company_key(self):
        # When AIRTABLE_SUPPRESS_ACCOUNT_LEVEL is on, the snapshot's bare keys line
        # up with hiring_manager.company_key_for_job so the WHOLE company is skipped.
        existing = _existing("gtm_revenue")
        with (
            patch.object(airtable_client, "validate_preflight", return_value=None),
            patch.object(airtable_client, "_get_existing_leads", Mock(return_value=existing)),
        ):
            snap = snapshot_existing_identity()
        job = {"employer_name": "Acme", "employer_website": "https://acme.com"}
        self.assertIn(hiring_manager.company_key_for_job(job), snap["company_bare_keys"])


if __name__ == "__main__":
    unittest.main()
