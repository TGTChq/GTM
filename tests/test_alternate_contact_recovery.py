"""Alternate-HM recovery: rebuild the contact, or change nothing at all."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import airtable_client
import alternate_contact_recovery as ACR
import config
import validation_integrity as vi

TITLES = ["Sales Director", "VP Sales", "Head of Sales", "Sales Manager"]


def _person(email="hm@acme.com", status="verified", pid="p-new",
            title="Sales Director", org_domain="acme.com", org_name="Acme",
            first="Dana", last="Reed", org_id="org-1"):
    return SimpleNamespace(
        person_found=True, person_id=pid, first_name=first, last_name=last,
        title=title, linkedin_url="https://linkedin.com/in/dana-reed",
        organization_name=org_name, organization_domain=org_domain,
        email=email, email_status=status, email_source="apollo",
        headline="", city="", state="", country="", seniority="senior",
        departments=["sales"], functions=["sales"],
        raw={"organization": {"id": org_id, "name": org_name,
                              "primary_domain": org_domain}},
    )


def _row(**over):
    fields = {
        "Final Decision": "FINAL_PASS", "Validation Version": str(config.VALIDATION_VERSION),
        "Company": "Acme", "Website": "https://acme.com", "Role Bucket": "gtm_revenue",
        "Campaign ID": "camp-1", "Matched Role": "Account Executive",
        "Open Role": "Account Executive", "Employees": 300,
        "Hiring Manager": "Old Person", "HM Title": "Sales Director",
        "LinkedIn": "https://linkedin.com/in/old", "Apollo Person ID": "p-old",
        "Email": "old@acme.com", "Apollo Email Status": "extrapolated",
        "Email Validation": "NEEDS_CHECK", "Contact Alignment": "PASS",
        "Email Source": "apollo", "Lead Key": "acme.com|old@acme.com|gtm_revenue",
        "Outbound Company": "Acme", "Outbound Company Confidence": "high",
        "Outbound Role": "Account Executive", "Outbound Role Confidence": "high",
        "Role Focus": "pipeline development", "Outbound Hold": False,
    }
    fields.update(over)
    fields["Validation Fingerprint"] = vi.validation_fingerprint(fields)
    return fields


class AlignmentTests(unittest.TestCase):
    def test_exact_employer_domain_passes(self):
        r = ACR.classify_alignment(email="a@acme.com", canonical_domain="acme.com",
                                   canonical_company_name="Acme")
        self.assertEqual(r.alignment, ACR.EXACT_EMPLOYER_DOMAIN)
        self.assertTrue(r.passes)

    def test_corroborated_alternate_domain_passes(self):
        """Provider puts the person on that domain AND the identity corroborates it."""
        r = ACR.classify_alignment(
            email="a@acmegroup.com", canonical_domain="acme.com",
            canonical_company_name="Acme Group", apollo_org_domain="acmegroup.com",
            apollo_org_name="Acme Group")
        self.assertEqual(r.alignment, ACR.CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN)
        self.assertTrue(r.passes)

    def test_provider_domain_without_identity_is_parent_ambiguous(self):
        r = ACR.classify_alignment(
            email="a@holdingco.com", canonical_domain="acme.com",
            canonical_company_name="Acme", apollo_org_domain="holdingco.com",
            apollo_org_name="Holding Co International")
        self.assertEqual(r.alignment, ACR.PARENT_DOMAIN_AMBIGUOUS)
        self.assertFalse(r.passes)

    def test_name_match_without_provider_domain_is_brand_ambiguous(self):
        r = ACR.classify_alignment(
            email="a@acme.io", canonical_domain="acme.com",
            canonical_company_name="Acme", apollo_org_domain="somewhereelse.com",
            apollo_org_name="Acme")
        self.assertEqual(r.alignment, ACR.BRAND_DOMAIN_AMBIGUOUS)
        self.assertFalse(r.passes)

    def test_unrelated_domain_rejected(self):
        r = ACR.classify_alignment(email="a@totallyother.com", canonical_domain="acme.com",
                                   canonical_company_name="Acme")
        self.assertEqual(r.alignment, ACR.UNRELATED_DOMAIN)

    def test_shortener_email_domain_rejected(self):
        r = ACR.classify_alignment(email="a@bit.ly", canonical_domain="acme.com",
                                   canonical_company_name="Acme")
        self.assertEqual(r.alignment, ACR.UNRELATED_DOMAIN)

    def test_publisher_and_freemail_domains_rejected(self):
        for bad in ("a@linkedin.com", "a@gmail.com", "a@greenhouse.io"):
            with self.subTest(email=bad):
                r = ACR.classify_alignment(email=bad, canonical_domain="acme.com",
                                           canonical_company_name="Acme")
                self.assertFalse(r.passes)

    def test_name_similarity_alone_never_passes(self):
        r = ACR.classify_alignment(
            email="a@acmeholdings.com", canonical_domain="acme.com",
            canonical_company_name="Acme", apollo_org_domain="", apollo_org_name="Acme")
        self.assertFalse(r.passes)

    def test_trusted_domain_set_excludes_non_employer_hosts(self):
        trusted = ACR.build_trusted_domains(
            canonical_domain="acme.com", apollo_org_domain="bit.ly",
            identity_key="domain:linkedin.com", extra=["greenhouse.io", "acmegroup.com"])
        self.assertEqual(trusted, {"acme.com", "acmegroup.com"})


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(config, "VALIDATION_SIGNING_KEY", "unit-test-key")
        p.start()
        self.addCleanup(p.stop)

    def test_verified_exact_domain_is_recovered(self):
        out = ACR.build_recovery(_row(), _person(), target_titles=TITLES)
        self.assertTrue(out.recovered)
        self.assertEqual(out.outcome, ACR.VERIFIED_EXACT_DOMAIN)
        self.assertEqual(out.patch["Email"], "hm@acme.com")
        self.assertEqual(out.patch["Hiring Manager"], "Dana Reed")
        self.assertEqual(out.patch["Apollo Person ID"], "p-new")

    def test_verified_corroborated_domain_is_recovered(self):
        out = ACR.build_recovery(
            _row(Company="Acme Group"),
            _person(email="hm@acmegroup.com", org_domain="acmegroup.com",
                    org_name="Acme Group"),
            target_titles=TITLES)
        self.assertTrue(out.recovered)
        self.assertEqual(out.outcome, ACR.VERIFIED_CORROBORATED_DOMAIN)

    def test_verified_unrelated_domain_is_rejected(self):
        out = ACR.build_recovery(
            _row(), _person(email="hm@totallyother.com", org_domain="totallyother.com",
                            org_name="Totally Other Inc"),
            target_titles=TITLES)
        self.assertFalse(out.recovered)
        self.assertEqual(out.patch, {})

    def test_parent_ambiguous_is_rejected(self):
        out = ACR.build_recovery(
            _row(), _person(email="hm@holdingco.com", org_domain="holdingco.com",
                            org_name="Holding Co International"),
            target_titles=TITLES)
        self.assertEqual(out.outcome, ACR.VERIFIED_DOMAIN_AMBIGUOUS)
        self.assertFalse(out.recovered)

    def test_extrapolated_alternate_is_rejected(self):
        out = ACR.build_recovery(_row(), _person(status="extrapolated"), target_titles=TITLES)
        self.assertEqual(out.outcome, ACR.EXTRAPOLATED)
        self.assertEqual(out.patch, {})

    def test_no_email_is_rejected(self):
        out = ACR.build_recovery(_row(), _person(email="", status="unavailable"),
                                 target_titles=TITLES)
        self.assertEqual(out.outcome, ACR.NO_EMAIL)

    def test_identity_mismatch_is_rejected(self):
        out = ACR.build_recovery(_row(), _person(pid="someone-else"),
                                 target_titles=TITLES, expected_person_id="p-new")
        self.assertEqual(out.outcome, ACR.IDENTITY_MISMATCH)

    def test_person_employer_duplicate_is_rejected(self):
        out = ACR.build_recovery(
            _row(), _person(), target_titles=TITLES,
            active_person_employer_keys={"acme.com|hm@acme.com"})
        self.assertEqual(out.outcome, ACR.PERSON_EMPLOYER_DUPLICATE)
        self.assertEqual(out.patch, {})

    def test_hm_rank_rejection_blocks_recovery(self):
        out = ACR.build_recovery(_row(), _person(title="Warehouse Associate"),
                                 target_titles=TITLES)
        self.assertFalse(out.recovered)
        self.assertEqual(out.patch, {})

    def test_original_row_with_bad_fingerprint_is_never_signed(self):
        fields = _row()
        fields["Validation Fingerprint"] = "0" * 64
        out = ACR.build_recovery(fields, _person(), target_titles=TITLES)
        self.assertFalse(out.recovered)
        self.assertEqual(out.reason, "original_row_fingerprint_invalid")

    def test_every_refusal_returns_an_empty_patch(self):
        """Transactional: a refused recovery can never half-apply a contact."""
        cases = [
            _person(status="extrapolated"), _person(email="", status="unavailable"),
            _person(email="hm@totallyother.com", org_domain="totallyother.com",
                    org_name="Totally Other Inc"),
            _person(title="Warehouse Associate"), _person(email="hm@bit.ly"),
        ]
        for person in cases:
            with self.subTest(email=person.email, status=person.email_status):
                out = ACR.build_recovery(_row(), person, target_titles=TITLES)
                self.assertEqual(out.patch, {})
                self.assertFalse(out.recovered)

    def test_lead_key_is_regenerated_for_the_new_contact(self):
        out = ACR.build_recovery(_row(), _person(), target_titles=TITLES)
        self.assertEqual(out.patch["Lead Key"], "acme.com|hm@acme.com|gtm_revenue")
        self.assertNotEqual(out.patch["Lead Key"], _row()["Lead Key"])

    def test_fingerprint_is_regenerated_and_verifies(self):
        fields = _row()
        out = ACR.build_recovery(fields, _person(), target_titles=TITLES)
        merged = {**fields, **out.patch}
        self.assertTrue(vi.fingerprint_matches(merged))
        self.assertNotEqual(out.patch["Validation Fingerprint"],
                            fields["Validation Fingerprint"])

    def test_every_contact_field_is_rebuilt_not_partially_patched(self):
        out = ACR.build_recovery(_row(), _person(), target_titles=TITLES)
        for name in ACR.CONTACT_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, out.patch)

    def test_job_and_company_context_is_preserved(self):
        fields = _row()
        out = ACR.build_recovery(fields, _person(), target_titles=TITLES)
        for untouched in ("Company", "Website", "Open Role", "Role Bucket",
                          "Outbound Company", "Outbound Role", "Role Focus"):
            self.assertNotIn(untouched, out.patch)

    def test_provenance_is_recorded_without_pii(self):
        out = ACR.build_recovery(_row(), _person(), target_titles=TITLES)
        prov = out.provenance
        self.assertTrue(prov["alternate_contact_recovery_attempted"])
        self.assertEqual(prov["alternate_contact_previous_person_id"], "p-old")
        self.assertEqual(prov["alternate_contact_result"], ACR.VERIFIED_EXACT_DOMAIN)
        self.assertIn("alignment_evidence", prov)
        blob = str(prov)
        self.assertNotIn("hm@acme.com", blob)     # no raw address in provenance
        self.assertNotIn("Dana Reed", blob)

    def test_failure_reason_is_recorded_on_refusal(self):
        out = ACR.build_recovery(_row(), _person(status="extrapolated"), target_titles=TITLES)
        self.assertIn("alternate_contact_failure_reason", out.provenance)


class PostRecoveryStateTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(config, "VALIDATION_SIGNING_KEY", "unit-test-key")
        p.start()
        self.addCleanup(p.stop)

    def test_recovered_row_becomes_send_safe(self):
        fields = _row()
        out = ACR.build_recovery(fields, _person(), target_titles=TITLES)
        merged = {**fields, **out.patch}
        self.assertEqual(airtable_client.send_safe_facts(merged), (True, "send_safe"))

    def test_recovered_row_is_eligible_for_approved_sync(self):
        fields = _row()
        out = ACR.build_recovery(fields, _person(), target_titles=TITLES)
        merged = {**fields, **out.patch}
        category, reason = airtable_client.approved_row_eligibility(merged)
        self.assertEqual((category, reason), ("eligible", "eligible"))

    def test_status_follows_the_traced_fact_based_approval_policy(self):
        fields = _row()
        out = ACR.build_recovery(fields, _person(), target_titles=TITLES)
        merged = {**fields, **out.patch}
        with mock.patch.object(config, "FANTASTIC_AUTO_APPROVE_SEND_SAFE", True):
            self.assertEqual(ACR.recovered_status(merged), config.AIRTABLE_STATUS_APPROVED)

    def test_status_is_pending_when_auto_approval_is_disabled(self):
        fields = _row()
        out = ACR.build_recovery(fields, _person(), target_titles=TITLES)
        merged = {**fields, **out.patch}
        with mock.patch.object(config, "FANTASTIC_AUTO_APPROVE_SEND_SAFE", False):
            self.assertEqual(ACR.recovered_status(merged), config.AIRTABLE_STATUS_PENDING)

    def test_status_is_pending_when_the_row_is_not_send_safe(self):
        with mock.patch.object(config, "FANTASTIC_AUTO_APPROVE_SEND_SAFE", True):
            self.assertEqual(ACR.recovered_status(_row(**{"Role Focus": ""})),
                             config.AIRTABLE_STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()


class AttemptProvenanceTests(unittest.TestCase):
    """EVERY attempt must be persisted -- not just the ones that succeed.

    The first 42-attempt sweep persisted only successes, so its 28 failures could not
    later be segmented by failure class, and that information was unrecoverable without
    paying Apollo a second time.
    """

    def setUp(self):
        p = mock.patch.object(config, "VALIDATION_SIGNING_KEY", "unit-test-key")
        p.start()
        self.addCleanup(p.stop)

    def _attempt(self, person, **kw):
        out = ACR.build_recovery(_row(), person, target_titles=TITLES)
        return ACR.attempt_record(record_id="rec1", outcome=out,
                                  candidate_person_id="p-new", candidate_pool_depth=4,
                                  original_block_reason="apollo_email_not_verified",
                                  employer_domain="acme.com", **kw)

    def test_failure_records_its_reason_and_class(self):
        a = self._attempt(_person(email="", status="unavailable"))
        self.assertEqual(a["alternate_contact_result"], ACR.NO_EMAIL)
        self.assertEqual(a["email_outcome"], "no_email")
        self.assertTrue(a["alternate_contact_failure_reason"])
        self.assertFalse(a["recovered"])

    def test_extrapolated_failure_is_distinguishable_from_no_email(self):
        a = self._attempt(_person(status="extrapolated"))
        self.assertEqual(a["email_outcome"], "extrapolated")

    def test_domain_ambiguous_failure_is_distinguishable(self):
        a = self._attempt(_person(email="hm@holdingco.com", org_domain="holdingco.com",
                                  org_name="Holding Co International"))
        self.assertEqual(a["email_outcome"], "verified")
        self.assertEqual(a["alignment_class"], ACR.PARENT_DOMAIN_AMBIGUOUS)

    def test_success_is_recorded_too(self):
        a = self._attempt(_person())
        self.assertTrue(a["recovered"])
        self.assertEqual(a["alternate_contact_failure_reason"], "")

    def test_provenance_carries_no_raw_pii(self):
        blob = str(self._attempt(_person()))
        self.assertNotIn("hm@acme.com", blob)
        self.assertNotIn("Dana Reed", blob)
        self.assertIn("acme.com", blob)          # domain only

    def test_second_alternate_is_worthwhile_only_for_person_level_failures(self):
        cases = {
            ACR.NO_EMAIL: True, ACR.EXTRAPOLATED: True,
            ACR.VERIFIED_DOMAIN_AMBIGUOUS: False, ACR.VERIFIED_DOMAIN_MISMATCH: False,
            ACR.GATE_REJECTED: False, ACR.PERSON_EMPLOYER_DUPLICATE: False,
        }
        for result, expected in cases.items():
            with self.subTest(result=result):
                attempt = {"recovered": False, "alternate_contact_result": result,
                           "candidate_pool_depth": 5}
                self.assertEqual(ACR.second_alternate_eligible(attempt), expected)

    def test_no_second_candidate_means_not_eligible(self):
        attempt = {"recovered": False, "alternate_contact_result": ACR.NO_EMAIL,
                   "candidate_pool_depth": 1}
        self.assertFalse(ACR.second_alternate_eligible(attempt))

    def test_recovered_row_never_needs_a_second_alternate(self):
        attempt = {"recovered": True, "alternate_contact_result": ACR.VERIFIED_EXACT_DOMAIN,
                   "candidate_pool_depth": 9}
        self.assertFalse(ACR.second_alternate_eligible(attempt))

    def test_attempts_are_written_as_jsonl(self):
        import json as _json
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "sub", "attempts.jsonl")
        rows = [self._attempt(_person()), self._attempt(_person(status="extrapolated"))]
        self.assertEqual(ACR.write_attempts(path, rows), 2)
        with open(path, encoding="utf-8") as fh:
            parsed = [_json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["schema"], ACR.ATTEMPT_SCHEMA)

    def test_write_failure_never_raises_into_the_caller(self):
        self.assertEqual(ACR.write_attempts("/nonexistent\x00/bad.jsonl",
                                            [{"a": 1}]), 0)

    def test_empty_write_is_a_noop(self):
        self.assertEqual(ACR.write_attempts("unused.jsonl", []), 0)
