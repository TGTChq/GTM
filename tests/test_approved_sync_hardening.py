"""Approved-sync production hardening: legacy backlog can never enroll.

Locks in the eligibility partition (airtable_client.select_eligible_approved /
approved_row_eligibility): ONLY a Status=Approved row that also carries the
CURRENT validated authorization (actionable Final Decision + exact Validation
Version + valid fingerprint + email + resolvable campaign) is enrollable. Legacy
and invalid rows are skipped BEFORE any revalidation/Instantly call and WITHOUT
any Airtable write.
"""

from __future__ import annotations

import unittest
from unittest import mock

import airtable_client
import config
import instantly_client
import run_approved
from airtable_client import approved_row_eligibility
from validation_integrity import validation_fingerprint


def _fields(**overrides):
    f = {
        "Status": "Approved",
        "Final Decision": "FINAL_PASS",
        "Validation Version": config.VALIDATION_VERSION,
        "Email": "jane@acme.com",
        "Apollo Email Status": "verified",
        "Email Validation": "PASS",
        "Contact Alignment": "PASS",
        "Company": "Acme",
        "Outbound Company": "Acme",
        "Outbound Company Confidence": "high",
        "Outbound Company Identity": "domain:acme.com",
        "Outbound Hold": False,
        "Outbound Role": "Account Executive",
        "Role Bucket": "marketing",
        "Campaign ID": "camp-123",
        "Website": "https://acme.com",
        # Delivery requires Role Focus; a row without it is not truly send-safe.
        "Role Focus": "building the outbound motion",
    }
    f.update(overrides)
    f["Validation Fingerprint"] = validation_fingerprint(f)
    return f


def _rec(rid, **overrides):
    return {"id": rid, "fields": _fields(**overrides)}


class EligibilityPredicateTests(unittest.TestCase):
    # 4 — Approved + valid current metadata is eligible.
    def test_current_authorized_row_is_eligible(self):
        cat, _ = approved_row_eligibility(_fields())
        self.assertEqual(cat, "eligible")

    def test_airtable_omitted_unchecked_hold_remains_eligible(self):
        fields = _fields()
        fields.pop("Outbound Hold")
        cat, reason = approved_row_eligibility(fields)
        self.assertEqual((cat, reason), ("eligible", "eligible"))

    def test_explicit_hold_remains_blocked(self):
        # Hold with both confidences acceptable => stale workflow state; still
        # blocked, but attributed precisely instead of being reported as an
        # ambiguous company name.
        cat, reason = approved_row_eligibility(_fields(**{"Outbound Hold": True}))
        self.assertEqual((cat, reason), ("invalid", "outbound_hold_stale_no_current_condition"))

    def test_role_side_hold_is_attributed_to_the_role(self):
        cat, reason = approved_row_eligibility(_fields(**{
            "Outbound Hold": True, "Outbound Role Confidence": "low"}))
        self.assertEqual((cat, reason), ("invalid", "outbound_role_held_for_review"))

    def test_company_side_hold_is_attributed_to_the_company(self):
        cat, reason = approved_row_eligibility(_fields(**{
            "Outbound Hold": True, "Outbound Company Confidence": "low"}))
        self.assertEqual((cat, reason), ("invalid", "outbound_company_held_for_review"))

    # 1 — legacy: missing Final Decision.
    def test_missing_final_decision_is_legacy(self):
        cat, reason = approved_row_eligibility(_fields(**{"Final Decision": ""}))
        self.assertEqual(cat, "legacy")
        self.assertEqual(reason, "no_actionable_final_decision")

    # 2 — legacy: old Validation Version, even with a self-consistent fingerprint.
    def test_old_validation_version_is_legacy(self):
        # sign with an OLD version so the fingerprint is internally valid but stale
        stale = _fields(**{"Validation Version": "tgtc-ready-v1.3"})
        cat, reason = approved_row_eligibility(stale)
        self.assertEqual(cat, "legacy")
        self.assertEqual(reason, "validation_version_mismatch")

    # legacy: tampered fingerprint (field mutated after signing).
    def test_tampered_fingerprint_is_legacy(self):
        f = _fields()
        f["Email"] = "someone-else@acme.com"   # mutate AFTER signing
        cat, reason = approved_row_eligibility(f)
        self.assertEqual(cat, "legacy")
        self.assertEqual(reason, "validation_fingerprint_mismatch")

    # 8 — invalid: missing campaign fails closed (no bucket->campaign env either).
    def test_missing_campaign_is_invalid(self):
        with mock.patch.object(config, "resolve_campaign_id", return_value=""):
            f = _fields(**{"Campaign ID": ""})
            cat, reason = approved_row_eligibility(f)
        self.assertEqual(cat, "invalid")
        self.assertEqual(reason, "no_campaign_configured")

    def test_missing_email_is_invalid(self):
        cat, reason = approved_row_eligibility(_fields(**{"Email": ""}))
        self.assertEqual(cat, "invalid")
        self.assertEqual(reason, "missing_email")


class SelectionTests(unittest.TestCase):
    def _select(self, records):
        with (
            mock.patch.object(airtable_client, "validate_preflight", return_value=None),
            mock.patch.object(airtable_client, "fetch_status_records", return_value=records),
        ):
            return airtable_client.select_eligible_approved()

    # 1/3 — legacy rows skipped, counts correct, only eligible returned.
    def test_partition_counts(self):
        records = [
            _rec("ok1"),
            _rec("legacy1", **{"Final Decision": ""}),          # legacy
            _rec("legacy2", **{"Validation Version": "old"}),   # legacy
            _rec("invalid1", **{"Email": ""}),                  # invalid
        ]
        eligible, counts = self._select(records)
        self.assertEqual([r["id"] for r in eligible], ["ok1"])
        self.assertEqual(counts["approved_seen"], 4)
        self.assertEqual(counts["approved_eligible"], 1)
        self.assertEqual(counts["approved_skipped_legacy"], 2)
        self.assertEqual(counts["approved_skipped_invalid"], 1)
        # reconciliation invariant
        self.assertEqual(counts["approved_seen"],
                         counts["approved_eligible"] + counts["approved_skipped_legacy"]
                         + counts["approved_skipped_invalid"])

    # 9/10 — two functions at the SAME company are independently eligible; no
    # company-level cross-function suppression in Approved Sync selection.
    def test_same_company_two_functions_both_eligible(self):
        records = [
            _rec("mkt", **{"Role Bucket": "marketing", "Email": "m@acme.com",
                           "Campaign ID": "camp-mkt"}),
            _rec("sales", **{"Role Bucket": "gtm_revenue", "Email": "s@acme.com",
                             "Campaign ID": "camp-sales"}),
        ]
        eligible, counts = self._select(records)
        self.assertEqual(counts["approved_eligible"], 2)
        self.assertEqual({r["id"] for r in eligible}, {"mkt", "sales"})


class RunLevelTests(unittest.TestCase):
    # 1 — a run over ONLY legacy rows: no Instantly, no Airtable write.
    def test_legacy_only_run_makes_no_writes_or_instantly_calls(self):
        legacy = [_rec("legacy1", **{"Final Decision": ""}),
                  _rec("legacy2", **{"Validation Version": "old"})]
        marks = []
        with (
            mock.patch.object(airtable_client, "validate_preflight", return_value=None),
            mock.patch.object(airtable_client, "fetch_status_records", return_value=legacy),
            mock.patch.object(airtable_client, "mark_error",
                              side_effect=lambda ids, err: marks.append(("error", list(ids)))),
            mock.patch.object(airtable_client, "mark_enrolled",
                              side_effect=lambda ids: marks.append(("enrolled", list(ids)))),
            mock.patch.object(instantly_client, "enroll_approved_leads") as enroll,
        ):
            result = run_approved.run(revalidate_providers=False)
        enroll.assert_not_called()                 # no Instantly
        self.assertEqual(marks, [])                 # no Airtable writes
        self.assertEqual(result["approved_skipped_legacy"], 2)
        self.assertEqual(result["approved_eligible"], 0)
        self.assertEqual(result["airtable_mark_error"], 0)

    # 8 — empty active set -> zero Instantly calls (proven by non-invocation).
    def test_empty_eligible_set_no_instantly(self):
        with (
            mock.patch.object(airtable_client, "validate_preflight", return_value=None),
            mock.patch.object(airtable_client, "fetch_status_records", return_value=[]),
            mock.patch.object(instantly_client, "enroll_approved_leads") as enroll,
        ):
            result = run_approved.run(revalidate_providers=False)
        enroll.assert_not_called()
        self.assertEqual(result["approved_seen"], 0)


if __name__ == "__main__":
    unittest.main()
