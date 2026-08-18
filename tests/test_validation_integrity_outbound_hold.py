from __future__ import annotations

import unittest
from unittest.mock import patch

import prepare_outbound_backfill as backfill
import validation_integrity


class OutboundHoldFingerprintSemanticsTests(unittest.TestCase):
    signing_key = "outbound-hold-test-key"

    def _fields(self, hold=...):
        fields = {
            "Company": "Acme",
            "Outbound Company": "Acme",
            "Outbound Company Confidence": "high",
            "Outbound Role": "Recruiter",
            "Final Decision": "FINAL_PASS",
            "Validation Version": "test-version",
            "Validated At": "2026-08-17T23:03:01+00:00",
        }
        if hold is not ...:
            fields["Outbound Hold"] = hold
        return fields

    def test_outbound_hold_payload_semantics_are_checkbox_specific(self):
        cases = (
            ("absent", ..., False),
            ("none", None, False),
            ("false", False, False),
            ("true", True, True),
        )
        for label, value, expected in cases:
            with self.subTest(label=label):
                payload = validation_integrity.fingerprint_payload(self._fields(value))
                self.assertIs(payload["Outbound Hold"], expected)
                self.assertIsNone(payload["Website"])

    def test_semantic_false_fingerprint_verifies_when_airtable_omits_checkbox(self):
        signed = self._fields(False)
        supplied = backfill._fingerprint(signed, self.signing_key)
        airtable_readback = self._fields()
        airtable_readback["Validation Fingerprint"] = supplied

        with patch.object(validation_integrity.config, "VALIDATION_SIGNING_KEY", self.signing_key):
            self.assertTrue(validation_integrity.fingerprint_matches(airtable_readback))

    def test_explicit_true_held_record_continues_verifying(self):
        held = self._fields(True)
        held["Validation Fingerprint"] = backfill._fingerprint(held, self.signing_key)

        with patch.object(validation_integrity.config, "VALIDATION_SIGNING_KEY", self.signing_key):
            self.assertTrue(validation_integrity.fingerprint_matches(held))


if __name__ == "__main__":
    unittest.main()
