from __future__ import annotations

import unittest
from unittest.mock import patch

import prepare_role_display_v2_migration as migration
import validation_integrity


def _record(record_id, email, title, matched, *, status="Pending"):
    return {
        "id": record_id,
        "fields": {
            "Status": status,
            "Email": email,
            "Campaign ID": "campaign-1",
            "Company": "Acme",
            "Website": "https://acme.com",
            "Open Role": title,
            "Open Roles": title,
            "Matched Role": matched,
            "Role Bucket": "gtm_revenue",
            "Role Focus": "Structured vacancy context",
            "Outbound Role": title,
            "Outbound Roles": title,
            "Outbound Role Confidence": "medium",
            "Outbound Role Evidence": "{}",
            "Final Decision": "FINAL_PASS",
            "Validation Version": "old-version",
            "Validated At": "2026-08-16T00:00:00+00:00",
        },
    }


def _lead(lead_id, email, *, contacted=False):
    return {
        "id": lead_id,
        "email": email,
        "campaign": "campaign-1",
        "status": 1,
        "timestamp_last_contact": "2026-08-17T00:00:00Z" if contacted else None,
        "payload": {"open_role": "Old Role", "open_roles": "Old Role", "other": "preserve"},
    }


class RoleDisplayV2MigrationManifestTests(unittest.TestCase):
    def test_exact_airtable_patch_is_role_only_and_fingerprint_verifies(self):
        record = _record(
            "rec-safe",
            "safe@example.com",
            "Account Executive — AI Readiness",
            "Account Executive",
        )
        patch_fields, metadata = migration.build_airtable_patch(
            record,
            generated_at="2026-08-17T12:00:00+00:00",
            signing_key="migration-key",
        )
        self.assertEqual(patch_fields["Outbound Role"], "Account Executive")
        self.assertEqual(patch_fields["Outbound Roles"], "Account Executive")
        self.assertFalse(set(patch_fields) & set(migration.PROTECTED_CANONICAL_FIELDS))
        self.assertEqual(metadata["current_open_role"], "Account Executive — AI Readiness")
        merged = dict(record["fields"])
        merged.update(patch_fields)
        with patch.object(validation_integrity.config, "VALIDATION_SIGNING_KEY", "migration-key"):
            self.assertTrue(validation_integrity.fingerprint_matches(merged))

    def test_build_manifests_separates_safe_hold_and_contacted_rows(self):
        records = [
            _record("rec-safe", "safe@example.com", "Account Executive — AI Readiness", "Account Executive"),
            _record(
                "rec-hold",
                "hold@example.com",
                "Payroll Administrator/General Ledger Accountant",
                "Accountant",
            ),
            _record("rec-contacted", "sent@example.com", "Staff Accountant- REMOTE", "Staff Accountant"),
        ]
        campaigns = [{"id": "campaign-1", "name": "GTM", "status": 2}]
        leads = {"campaign-1": [
            _lead("lead-safe", "safe@example.com"),
            _lead("lead-hold", "hold@example.com"),
            _lead("lead-contacted", "sent@example.com", contacted=True),
        ]}
        result = migration.build_manifests(
            records,
            campaigns,
            leads,
            generated_at="2026-08-17T12:00:00+00:00",
            signing_key="migration-key",
        )
        summary = result["summary"]
        self.assertEqual(summary["airtable_safe_role_updates"], 1)
        self.assertEqual(summary["airtable_role_holds"], 1)
        self.assertEqual(summary["airtable_contacted_protected"], 1)
        self.assertEqual(summary["instantly_unsent_safe_updates"], 1)
        self.assertEqual(summary["instantly_unsent_ambiguous"], 1)
        self.assertEqual(summary["instantly_contacted"], 1)
        self.assertEqual(result["instantly_safe_updates"][0]["changed_fields"], [
            "custom_variables.open_role",
            "custom_variables.open_roles",
        ])
        self.assertEqual(result["instantly_holds"][0]["display_patch"], {})


if __name__ == "__main__":
    unittest.main()
