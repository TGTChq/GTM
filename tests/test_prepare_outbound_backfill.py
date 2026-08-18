from __future__ import annotations

import unittest
from unittest.mock import patch

import config
import prepare_outbound_backfill as backfill
import validation_integrity


class OutboundBackfillManifestTests(unittest.TestCase):
    def _record(self, *, status="Pending", company="Acme Inc."):
        return {
            "id": "rec123",
            "fields": {
                "Status": status,
                "Company": company,
                "Website": "https://acme.com",
                "Open Role": "Customer Support Specialist - Austin, TX",
                "Open Roles": "Customer Support Specialist - Austin, TX",
                "Matched Role": "Customer Support Specialist",
                "Location": "Austin, TX",
                "Firmographics Status": "PASS",
                "Final Decision": "FINAL_PASS",
                "Email": "person@example.com",
            },
        }

    def test_contract_matches_runtime_validation_boundary(self):
        self.assertEqual(backfill.VALIDATION_VERSION, config.VALIDATION_VERSION)
        self.assertEqual(backfill.SIGNED_FIELDS, validation_integrity.SIGNED_FIELDS)
        self.assertFalse(set(backfill.PATCH_FIELDS) & backfill.PROTECTED_CANONICAL_FIELDS)

    @patch("prepare_outbound_backfill._resolver_cache")
    def test_manifest_is_display_only_and_fingerprint_is_exact(self, cache_factory):
        cache_factory.return_value = None
        manifest = backfill.prepare_records(
            [self._record()],
            signing_key="test-signing-key",
            generated_at="2026-08-17T12:00:00+00:00",
        )
        self.assertEqual(manifest["summary"]["safe_backfill"], 1)
        self.assertEqual(manifest["summary"]["held"], 0)
        row = dict(zip(manifest["columns"], manifest["rows"][0]))
        self.assertEqual(row["proposed_outbound_company"], "Acme")
        self.assertEqual(row["proposed_outbound_role"], "Customer Support Specialist")
        self.assertFalse(row["outbound_hold"])
        self.assertEqual(row["patch_field_set"], backfill.PATCH_FIELD_SET)
        self.assertEqual(len(row["proposed_validation_fingerprint"]), 64)

    def test_refuses_enrolled_or_rejected_records(self):
        for status in ("Enrolled", "Rejected", "Approved"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "out-of-scope"):
                backfill.prepare_records(
                    [self._record(status=status)],
                    signing_key="test-signing-key",
                )

    def test_signing_key_is_required(self):
        with self.assertRaisesRegex(ValueError, "VALIDATION_SIGNING_KEY"):
            backfill.prepare_records([self._record()], signing_key="")

    def test_page_fetch_refuses_out_of_scope_status_without_network(self):
        with self.assertRaisesRegex(ValueError, "out-of-scope"):
            backfill.fetch_status_page("Enrolled")

    def test_ambiguous_role_forces_outbound_hold_without_canonical_changes(self):
        record = self._record()
        record["fields"]["Open Role"] = "Payroll Administrator/General Ledger Accountant"
        record["fields"]["Open Roles"] = record["fields"]["Open Role"]
        record["fields"]["Matched Role"] = "Accountant"
        proposed, meta = backfill.build_record_patch(
            record,
            signing_key="test-signing-key",
            generated_at="2026-08-17T12:00:00+00:00",
            cache=None,
        )
        self.assertTrue(meta["role_hold"])
        self.assertTrue(meta["hold"])
        self.assertFalse(meta["safe"])
        self.assertTrue(proposed["Outbound Hold"])
        self.assertEqual(
            proposed["Outbound Role"],
            "Payroll Administrator/General Ledger Accountant",
        )
        self.assertFalse(set(proposed) & backfill.PROTECTED_CANONICAL_FIELDS)
        self.assertEqual(record["fields"]["Open Role"], "Payroll Administrator/General Ledger Accountant")

    def test_role_change_regenerates_a_verifiable_fingerprint(self):
        record = self._record()
        record["fields"].update({
            "Open Role": "Account Executive — AI Readiness",
            "Open Roles": "Account Executive — AI Readiness",
            "Matched Role": "Account Executive",
            "Outbound Role": "Account Executive — AI Readiness",
            "Outbound Roles": "Account Executive — AI Readiness",
            "Outbound Role Confidence": "medium",
            "Outbound Role Evidence": "{}",
            "Validation Version": "legacy-role-display",
            "Validated At": "2026-08-16T12:00:00+00:00",
        })
        old_fingerprint = backfill._fingerprint(record["fields"], "test-signing-key")
        proposed, _ = backfill.build_record_patch(
            record,
            signing_key="test-signing-key",
            generated_at="2026-08-17T12:00:00+00:00",
            cache=None,
        )
        merged = dict(record["fields"])
        merged.update(proposed)
        self.assertEqual(merged["Open Role"], "Account Executive — AI Readiness")
        self.assertEqual(merged["Outbound Role"], "Account Executive")
        self.assertNotEqual(proposed["Validation Fingerprint"], old_fingerprint)
        with patch.object(validation_integrity.config, "VALIDATION_SIGNING_KEY", "test-signing-key"):
            self.assertTrue(validation_integrity.fingerprint_matches(merged))

    @patch("prepare_outbound_backfill._resolver_cache")
    def test_instantly_sent_guard_removes_otherwise_safe_row(self, cache_factory):
        cache_factory.return_value = None
        manifest = backfill.prepare_records(
            [self._record()],
            signing_key="test-signing-key",
            generated_at="2026-08-17T12:00:00+00:00",
        )
        guarded = backfill.apply_instantly_overlap_guards(manifest, {"overlaps": [{
            "airtable_record_id": "rec123",
            "sent_or_processed": True,
        }]})
        row = dict(zip(guarded["columns"], guarded["rows"][0]))
        self.assertFalse(row["safe_backfill"])
        self.assertEqual(row["instantly_overlap_state"], "sent_or_processed")
        self.assertEqual(row["backfill_eligibility"], "excluded_instantly_sent_or_processed")
        self.assertEqual(guarded["summary"]["sent_or_processed_resolver_safe_excluded"], 1)


if __name__ == "__main__":
    unittest.main()
