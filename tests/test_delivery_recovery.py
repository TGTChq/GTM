"""Delivery recovery: only artifact holds are cleared, never real ambiguity."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import airtable_client
import config
import delivery_recovery as dr
import validation_integrity as vi


def _company_fields(name, slug="", domain="", reasons=("linkedin_slug_domain_disagreement",)):
    keys = [k for k in (f"linkedin:{slug}" if slug else "", f"domain:{domain}" if domain else "") if k]
    return {
        "Outbound Company": name,
        "Outbound Company Confidence": "low",
        "Outbound Hold": True,
        "Outbound Company Evidence": json.dumps({"identity_keys": keys, "reasons": list(reasons)}),
    }


def _role_fields(title, matched, rules=("competing_role_heads",)):
    return {
        "Outbound Role": title,
        "Outbound Role Confidence": "low",
        "Outbound Hold": True,
        "Matched Role": matched,
        "Outbound Role Evidence": json.dumps(
            {"normalized_title": title, "matched_role": matched, "rules": list(rules)}
        ),
    }


class CompanyRecoveryTests(unittest.TestCase):
    def test_exact_linkedin_slug_match_is_high_confidence(self):
        p = dr.classify_company_hold(
            _company_fields("Occidental Management", "occidentalmanagement", "occmgmt"))
        self.assertEqual(p.classification, dr.CANONICAL_EXACT_CORROBORATION)
        self.assertEqual(p.confidence, dr.HIGH)
        self.assertEqual(p.patch["Outbound Company Confidence"], "medium")

    def test_exact_domain_brand_match_is_high_confidence(self):
        p = dr.classify_company_hold(_company_fields("SmartTRAK", "biomedgps", "smarttrak.com"))
        self.assertEqual(p.classification, dr.LINKEDIN_DOMAIN_CORROBORATION)
        self.assertEqual(p.confidence, dr.HIGH)

    def test_ampersand_normalization_artifact_is_recovered(self):
        # "&" is dropped by _name_key but spelled "and" in the slug -- same company.
        p = dr.classify_company_hold(
            _company_fields("EKI Environment & Water, Inc.", "ekienvironmentandwater", "ekiconsult"))
        self.assertEqual(p.confidence, dr.HIGH)
        self.assertEqual(p.classification, dr.SAFE_SUFFIX_NORMALIZATION_CORROBORATED)

    def test_recovery_never_renames_the_company(self):
        fields = _company_fields("Occidental Management", "occidentalmanagement", "occmgmt")
        p = dr.classify_company_hold(fields)
        self.assertNotIn("Outbound Company", p.patch)

    def test_partial_match_is_medium_and_never_applied(self):
        p = dr.classify_company_hold(_company_fields("Mighty 8th", "mighty8thmedia", "m8th"))
        self.assertEqual(p.confidence, dr.MEDIUM)
        self.assertFalse(p.applicable)
        self.assertEqual(p.patch, {})

    def test_parent_subsidiary_name_is_never_recovered(self):
        p = dr.classify_company_hold(_company_fields(
            "IACMI - The Composites Institute", "iacmi", "iacmi.org",
            reasons=("unresolved_multi_entity_or_franchise_name",)))
        self.assertEqual(p.classification, dr.PARENT_SUBSIDIARY_AMBIGUOUS)
        self.assertFalse(p.applicable)

    def test_malformed_name_is_never_recovered(self):
        p = dr.classify_company_hold(_company_fields(
            "360 Fire & Flood", "360fireflood", "360fireflood.com",
            reasons=("malformed_or_coded_company_name",)))
        self.assertFalse(p.applicable)

    def test_unrelated_name_is_not_recovered(self):
        p = dr.classify_company_hold(_company_fields("STC, an Arcfield Company", "strategictech", "arcfield.com"))
        self.assertFalse(p.applicable)

    def test_missing_identity_anchors_is_not_recovered(self):
        p = dr.classify_company_hold(_company_fields("Anything", "", ""))
        self.assertEqual(p.classification, dr.MISSING_EVIDENCE)
        self.assertFalse(p.applicable)

    def test_evidence_is_always_valid_json(self):
        p = dr.classify_company_hold(_company_fields("Codal", "gocodal", "codal.com"))
        self.assertIsInstance(json.loads(p.patch["Outbound Company Evidence"]), dict)

    def test_oversized_evidence_stays_parseable(self):
        text = dr._dump_evidence({"candidates": ["x" * 200_000], "recovery_version": "v"})
        self.assertLessEqual(len(text), dr._EVIDENCE_MAX)
        self.assertIsInstance(json.loads(text), dict)


class RoleRecoveryTests(unittest.TestCase):
    def test_competing_head_is_extracted_from_the_catalog_anchor(self):
        p = dr.classify_role_hold(
            _role_fields("Inside Sales Representative / Relief Driver", "Inside Sales Representative"))
        self.assertEqual(p.confidence, dr.HIGH)
        self.assertEqual(p.patch["Outbound Role"], "Inside Sales Representative")

    def test_recovered_role_is_always_verbatim_from_the_posting_title(self):
        title = "Staff Accountant - Leasing Specialist"
        p = dr.classify_role_hold(_role_fields(title, "Staff Accountant"))
        self.assertIn(p.patch["Outbound Role"].lower(), title.lower())

    def test_cross_function_disagreement_is_never_recovered(self):
        p = dr.classify_role_hold(_role_fields(
            "Billing Support Specialist", "Customer Support",
            rules=("material_cross_function_disagreement",)))
        self.assertEqual(p.classification, dr.ROLE_CROSS_FUNCTION_AMBIGUOUS)
        self.assertFalse(p.applicable)

    def test_missing_catalog_role_is_never_recovered(self):
        p = dr.classify_role_hold(_role_fields("VIP & Upper Stories Host", ""))
        self.assertEqual(p.classification, dr.ROLE_NO_CATALOG_ANCHOR)
        self.assertFalse(p.applicable)

    def test_catalog_role_absent_from_the_title_is_never_recovered(self):
        p = dr.classify_role_hold(_role_fields("Warehouse Associate", "Account Executive"))
        self.assertFalse(p.applicable)

    def test_invented_render_is_discarded_never_written(self):
        """An invented render must never reach Airtable; fall back to the title text."""
        with mock.patch.object(dr._rdr, "_render_from_anchor", return_value=("Invented Title", [])):
            p = dr.classify_role_hold(_role_fields("Account Manager Team Lead", "Account Manager"))
        self.assertNotEqual(p.patch.get("Outbound Role"), "Invented Title")
        # "Account Manager" IS verbatim in the title, so extraction is safe.
        self.assertEqual(p.patch["Outbound Role"], "Account Manager")

    def test_invented_render_with_no_verbatim_catalog_role_is_refused(self):
        with mock.patch.object(dr._rdr, "_render_from_anchor", return_value=("Invented Title", [])):
            p = dr.classify_role_hold(_role_fields("Warehouse Ops Lead", "Account Manager"))
        self.assertFalse(p.applicable)
        self.assertEqual(p.patch, {})

    def test_render_that_drops_the_catalog_role_is_refused(self):
        with mock.patch.object(dr._rdr, "_render_from_anchor", return_value=("Team Lead", [])):
            p = dr.classify_role_hold(_role_fields("Account Manager Team Lead", "Account Manager"))
        self.assertFalse(p.applicable)


class ResignTests(unittest.TestCase):
    def setUp(self):
        self.patcher = mock.patch.object(config, "VALIDATION_SIGNING_KEY", "unit-test-key")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _signed(self):
        fields = {"Company": "Acme", "Outbound Company": "Acme",
                  "Outbound Company Confidence": "low", "Outbound Hold": True}
        fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
        return fields

    def test_resigned_row_verifies(self):
        fields = self._signed()
        patch = dr.resign_patch(fields, {"Outbound Company Confidence": "medium"})
        self.assertTrue(vi.fingerprint_matches({**fields, **patch}))

    def test_refuses_a_row_that_is_not_already_authentic(self):
        fields = self._signed()
        fields["Validation Fingerprint"] = "0" * 64
        with self.assertRaises(ValueError):
            dr.resign_patch(fields, {"Outbound Company Confidence": "medium"})

    def test_refuses_to_sign_a_field_recovery_may_not_touch(self):
        with self.assertRaises(ValueError):
            dr.resign_patch(self._signed(), {"Email": "someone@example.com"})

    def test_empty_patch_signs_nothing(self):
        self.assertEqual(dr.resign_patch(self._signed(), {}), {})


class StaleHoldTests(unittest.TestCase):
    def test_clears_only_when_both_sides_are_independently_safe(self):
        self.assertEqual(
            dr.stale_hold_patch({"Outbound Hold": True, "Outbound Company": "Acme",
                                 "Outbound Company Confidence": "high",
                                 "Outbound Role": "Account Executive",
                                 "Outbound Role Confidence": "high"}),
            {"Outbound Hold": False})

    def test_never_clears_a_genuinely_low_confidence_row(self):
        self.assertIsNone(
            dr.stale_hold_patch({"Outbound Hold": True, "Outbound Company": "Acme",
                                 "Outbound Company Confidence": "low",
                                 "Outbound Role": "Account Executive"}))

    def test_unheld_row_is_untouched(self):
        self.assertIsNone(dr.stale_hold_patch({"Outbound Hold": False}))


class EndToEndTests(unittest.TestCase):
    """A recovered row must clear the real send-safe gate, not merely change reason."""

    def setUp(self):
        self.patcher = mock.patch.object(config, "VALIDATION_SIGNING_KEY", "unit-test-key")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _row(self):
        fields = {
            "Final Decision": "FINAL_PASS", "Validation Version": str(config.VALIDATION_VERSION),
            "Email": "hm@occmgmt.com", "Apollo Email Status": "verified",
            "Email Validation": "PASS", "Contact Alignment": "PASS",
            "Role Focus": "general ledger accounting", "Role Bucket": "finance",
            "Campaign ID": "camp-1", "Outbound Role": "Cost Accountant",
            "Outbound Role Confidence": "high",
            **_company_fields("Occidental Management", "occidentalmanagement", "occmgmt"),
        }
        fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
        return fields

    def test_company_recovery_makes_the_row_send_safe(self):
        fields = self._row()
        self.assertEqual(airtable_client.send_safe_facts(fields)[1],
                         "outbound_company_held_for_review")
        patch = dict(dr.classify_company_hold(fields).patch)
        patch.update(dr.stale_hold_patch({**fields, **patch}) or {})
        patch = dr.resign_patch(fields, patch)
        self.assertEqual(airtable_client.send_safe_facts({**fields, **patch}),
                         (True, "send_safe"))

    def test_unrecovered_row_stays_blocked(self):
        fields = {**self._row(), **_company_fields("STC, an Arcfield Company",
                                                   "strategictech", "arcfield.com")}
        fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
        self.assertFalse(dr.classify_company_hold(fields).applicable)
        self.assertFalse(airtable_client.send_safe_facts(fields)[0])

    def test_recovery_without_resigning_would_not_deliver(self):
        """Guards the defect the dry replay caught: an unsigned patch just fails later."""
        fields = self._row()
        patch = dict(dr.classify_company_hold(fields).patch)
        patch.update(dr.stale_hold_patch({**fields, **patch}) or {})
        self.assertEqual(airtable_client.send_safe_facts({**fields, **patch})[1],
                         "validation_fingerprint_mismatch")


class GateParityTests(unittest.TestCase):
    """Every field DELIVERY requires must already be gated by send-safety.

    Without this, a row can be written, manually reviewed, approved and marked
    eligible -- and only then die inside ``airtable_record_to_lead``, wasting the
    reviewer's work and silently stalling the queue.
    """

    #: Required by instantly_client.airtable_record_to_lead.
    DELIVERY_REQUIRED = ("Email", "Outbound Company", "Outbound Role", "Role Focus")

    def setUp(self):
        self.patcher = mock.patch.object(config, "VALIDATION_SIGNING_KEY", "unit-test-key")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _send_safe_row(self):
        fields = {
            "Final Decision": "FINAL_PASS", "Validation Version": str(config.VALIDATION_VERSION),
            "Email": "hm@acme.com", "Apollo Email Status": "verified",
            "Email Validation": "PASS", "Contact Alignment": "PASS",
            "Outbound Company": "Acme", "Outbound Company Confidence": "high",
            "Outbound Role": "Account Executive", "Outbound Role Confidence": "high",
            "Role Focus": "pipeline development", "Role Bucket": "gtm_revenue",
            "Campaign ID": "camp-1", "Outbound Hold": False,
        }
        fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
        return fields

    def test_baseline_row_is_send_safe(self):
        self.assertEqual(airtable_client.send_safe_facts(self._send_safe_row()),
                         (True, "send_safe"))

    def test_every_delivery_required_field_is_gated_by_send_safety(self):
        for name in self.DELIVERY_REQUIRED:
            with self.subTest(field=name):
                fields = self._send_safe_row()
                fields[name] = ""
                fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
                ok, reason = airtable_client.send_safe_facts(fields)
                self.assertFalse(ok, f"{name} is required for delivery but not send-safety")
                self.assertNotEqual(reason, "send_safe")

    def test_unresolvable_campaign_is_gated_by_send_safety(self):
        fields = self._send_safe_row()
        fields["Campaign ID"] = ""
        fields["Role Bucket"] = "no_such_bucket"
        fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
        self.assertFalse(airtable_client.send_safe_facts(fields)[0])

    def test_held_row_is_gated_by_send_safety(self):
        fields = self._send_safe_row()
        fields["Outbound Hold"] = True
        fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
        self.assertFalse(airtable_client.send_safe_facts(fields)[0])


class RoleFocusRepairSigningTests(unittest.TestCase):
    """Backfilling the SIGNED Role Focus field must not invalidate the row."""

    def setUp(self):
        self.patcher = mock.patch.object(config, "VALIDATION_SIGNING_KEY", "unit-test-key")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _run_repair(self, fields):
        record = {"id": "rec1", "fields": fields}
        sent = {}

        def fake_patch(method, url, headers=None, json_body=None, **kw):
            sent["body"] = json_body
            return mock.Mock(status_code=200)

        with mock.patch.object(airtable_client, "_get_existing_leads", return_value={"k": record}), \
             mock.patch.object(airtable_client, "validate_preflight"), \
             mock.patch.object(airtable_client, "request_with_retry", side_effect=fake_patch), \
             mock.patch.object(airtable_client, "safe_json",
                               return_value={"records": [{"id": "rec1"}]}), \
             mock.patch.object(config, "AIRTABLE_RATE_LIMIT_DELAY", 0):
            airtable_client.repair_missing_role_focus()
        return sent.get("body", {}).get("records", [{}])[0].get("fields", {})

    def test_authentic_row_is_resigned_and_stays_valid(self):
        fields = {"Open Role": "Account Executive", "Matched Role": "Account Executive",
                  "Role Focus": "", "Outbound Hold": False}
        fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
        patched = self._run_repair(fields)
        if not patched.get("Role Focus"):
            self.skipTest("no role-focus mapping available for this catalog role")
        self.assertIn("Validation Fingerprint", patched)
        self.assertTrue(vi.fingerprint_matches({**fields, **patched}))

    def test_already_invalid_row_is_not_laundered_into_validity(self):
        fields = {"Open Role": "Account Executive", "Matched Role": "Account Executive",
                  "Role Focus": "", "Validation Fingerprint": "0" * 64}
        patched = self._run_repair(fields)
        if not patched.get("Role Focus"):
            self.skipTest("no role-focus mapping available for this catalog role")
        self.assertNotIn("Validation Fingerprint", patched)


if __name__ == "__main__":
    unittest.main()


class CatalogRoleVerbatimTests(unittest.TestCase):
    """Undelimited qualifier titles extract; DELIMITED ones must still hold."""

    def test_fallback_never_fires_for_a_delimited_title(self):
        """INVARIANT: a delimited title may advertise two DISTINCT roles, so the
        catalog-role fallback must not rescue one when the renderer declines.

        (The resolver's own renderer still handles delimited titles it can extract
        contiguously -- that path shipped and delivered 33 rows successfully.)"""
        for title, matched in (
                ("Sales Associate (Account Executive)", "Account Executive"),
                ("Payroll Administrator/General Ledger Accountant", "Accountant")):
            with self.subTest(title=title):
                with mock.patch.object(dr._rdr, "_render_from_anchor",
                                       return_value=("Invented Title", [])):
                    p = dr.classify_role_hold(_role_fields(title, matched))
                self.assertFalse(p.applicable)
                self.assertEqual(p.patch, {})

    def test_undelimited_qualifier_title_extracts_the_catalog_role(self):
        p = dr.classify_role_hold(_role_fields("Account Manager Team Lead", "Account Manager"))
        self.assertEqual(p.confidence, dr.HIGH)
        self.assertEqual(p.patch["Outbound Role"], "Account Manager")

    def test_catalog_role_absent_from_title_still_holds(self):
        p = dr.classify_role_hold(_role_fields("Digital Designer and Video Editor",
                                               "Graphic Designer"))
        self.assertFalse(p.applicable)

    def test_cross_function_still_holds_even_if_verbatim(self):
        p = dr.classify_role_hold(_role_fields(
            "Billing Support Specialist", "Customer Support",
            rules=("material_cross_function_disagreement",)))
        self.assertEqual(p.classification, dr.ROLE_CROSS_FUNCTION_AMBIGUOUS)
        self.assertFalse(p.applicable)

    def test_extraction_preserves_the_titles_own_casing(self):
        role = dr._rdr.catalog_role_verbatim("SENIOR ACCOUNT MANAGER Team Lead",
                                             "Account Manager")
        self.assertEqual(role, "ACCOUNT MANAGER")


class ResolverCatalogVerbatimTests(unittest.TestCase):
    """The production resolver stops accumulating UNDELIMITED qualifier cases."""

    def test_secondary_head_resolves_via_the_catalog_role(self):
        import role_display_resolver as rdr
        out = rdr.resolve_role_display(
            {"job_title": "Account Manager Team Lead", "_matched_role": "Account Manager"})
        self.assertFalse(out.hold)
        self.assertEqual(out.name, "Account Manager")

    def test_delimited_competing_heads_still_hold(self):
        import role_display_resolver as rdr
        out = rdr.resolve_role_display(
            {"job_title": "Payroll Administrator/General Ledger Accountant",
             "_matched_role": "Accountant"})
        self.assertTrue(out.hold)

    def test_absent_catalog_role_still_holds(self):
        import role_display_resolver as rdr
        out = rdr.resolve_role_display(
            {"job_title": "VIP & Upper Stories Host | Thompson Palm Springs",
             "_matched_role": "Graphic Designer"})
        self.assertTrue(out.hold)

    def test_verbatim_helper_requires_token_boundaries(self):
        import role_display_resolver as rdr
        self.assertEqual(rdr.catalog_role_verbatim("Reaccountant Manager", "Accountant"), "")
        self.assertEqual(rdr.catalog_role_verbatim("Staff Accountant - Leasing", "Accountant"),
                         "Accountant")
