"""Mechanism B -- employer-domain corroboration for HM recovery.

Covers the 9 required cases: legitimate rebrand/alternate first-party domain and
strongly-corroborated parent are allowed; clearly-unrelated and similar-but-different
identities are rejected; the recovered domain only unlocks the search (title/email
gates still apply, so wrong-function or unverified-email recoveries never become
send-safe); source/canonical identity is preserved; flag OFF is a no-op; and
send_safe_facts (the Approved-Sync gate) is untouched.
"""

import types
import unittest
from unittest.mock import patch

import config
import hiring_manager
import hm_domain_recovery
from hm_domain_recovery import corroborate_recovery_domain as corroborate


class CorroborationPolicyTests(unittest.TestCase):
    # 1 legitimate rebrand / alternate first-party domain -> allowed
    def test_rebrand_exact_name_alternate_domain_allowed(self):
        d = corroborate(source_domain="globexold.com", source_name="Globex",
                        candidate_domain="globex.com", candidate_name="Globex")
        self.assertTrue(d.accepted)
        self.assertEqual(d.recovered_domain, "globex.com")
        self.assertEqual(d.reason, "exact_name_and_domain_consistent")

    # 2 legitimate parent/relationship with strong corroboration -> allowed (LinkedIn)
    def test_strong_linkedin_corroboration_allowed(self):
        d = corroborate(source_domain="oldbrand.com", source_name="Kintsugi",
                        candidate_domain="kintsugi.ai", candidate_name="Kintsugi AI",
                        source_linkedin="https://www.linkedin.com/company/kintsugi",
                        candidate_linkedin="linkedin.com/company/kintsugi/")
        self.assertTrue(d.accepted)
        self.assertEqual(d.reason, "linkedin_identity_match")

    # 3 clearly unrelated Apollo organization -> rejected
    def test_unrelated_company_rejected(self):
        d = corroborate(source_domain="acme.com", source_name="Acme Foods",
                        candidate_domain="globex.com", candidate_name="Globex Industries")
        self.assertFalse(d.accepted)
        self.assertEqual(d.reason, "names_conflict")

    # 4 similar names but different identity -> rejected
    def test_similar_names_different_identity_rejected(self):
        d = corroborate(source_domain="initech.io", source_name="Initech",
                        candidate_domain="initrode.com", candidate_name="Initrode")
        self.assertFalse(d.accepted)

    def test_staffing_ambiguity_rejected(self):
        d = corroborate(source_domain="acmefoods.com", source_name="Acme Foods",
                        candidate_domain="premierstaffing.com", candidate_name="Premier Staffing")
        self.assertFalse(d.accepted)
        self.assertEqual(d.reason, "staffing_ambiguity")

    def test_no_alternate_when_same_domain(self):
        d = corroborate(source_domain="acme.com", source_name="Acme Corporation",
                        candidate_domain="acme.com", candidate_name="Acme Corporation")
        self.assertFalse(d.accepted)
        self.assertEqual(d.reason, "no_alternate_domain")

    # 7 canonical/source identity preserved (kept as separate evidence, never mutated)
    def test_source_identity_preserved_in_evidence(self):
        d = corroborate(source_domain="globexold.com", source_name="Globex",
                        candidate_domain="globex.com", candidate_name="Globex")
        self.assertEqual(d.evidence["source_domain"], "globexold.com")
        self.assertEqual(d.evidence["source_name"], "Globex")
        self.assertEqual(d.evidence["candidate_domain"], "globex.com")


class _Org:
    """Minimal OrgEnrichment stand-in."""
    def __init__(self, found, domain=None, name=None, raw=None, employee_count=200):
        self.found = found
        self.domain = domain
        self.name = name
        self.raw = raw or {}
        self.employee_count = employee_count


class WiringTests(unittest.TestCase):
    def _untrusted_org_with_corroborated_raw(self):
        # Apollo resolved a same-named org at a different (first-party) domain, so
        # enrich_organization returned found=False but kept the raw org.
        return _Org(found=False, domain="globexold.com", name=None, raw={
            "primary_domain": "globex.com", "name": "Globex",
            "linkedin_url": "linkedin.com/company/globex",
        })

    # 8 flag OFF -> exact current behavior (no recovery, org unchanged)
    def test_flag_off_is_noop(self):
        from collections import defaultdict
        stats = defaultdict(int)
        org = self._untrusted_org_with_corroborated_raw()
        with patch.object(config, "HM_DOMAIN_CORROBORATION_RECOVERY", False):
            out, decision = hiring_manager._recover_org_via_domain_corroboration(
                org, "globexold.com", "Globex", {}, stats)
        self.assertIs(out, org)
        self.assertIsNone(decision)
        self.assertEqual(stats.get("hm_domain_recovery_attempted", 0), 0)

    def test_flag_on_reenriches_on_corroborated_domain(self):
        from collections import defaultdict
        stats = defaultdict(int)
        org = self._untrusted_org_with_corroborated_raw()
        recovered = _Org(found=True, domain="globex.com", name="Globex", employee_count=300)
        calls = {"domains": []}

        def fake_enrich(domain=None, name=None, website=None):
            calls["domains"].append(domain)
            return recovered

        with (
            patch.object(config, "HM_DOMAIN_CORROBORATION_RECOVERY", True),
            patch.object(hiring_manager.apollo, "enrich_organization", side_effect=fake_enrich),
        ):
            out, decision = hiring_manager._recover_org_via_domain_corroboration(
                org, "globexold.com", "Globex", {}, stats)
        self.assertIs(out, recovered)                       # searched on recovered domain
        self.assertTrue(decision.accepted)
        self.assertEqual(calls["domains"], ["globex.com"])  # re-enriched on the alternate
        self.assertEqual(stats["hm_domain_recovery_attempted"], 1)
        self.assertEqual(stats["hm_domain_recovery_accepted"], 1)

    def test_flag_on_rejects_unrelated_and_does_not_reenrich(self):
        from collections import defaultdict
        stats = defaultdict(int)
        org = _Org(found=False, domain="acme.com", raw={
            "primary_domain": "globex.com", "name": "Globex Industries"})
        with (
            patch.object(config, "HM_DOMAIN_CORROBORATION_RECOVERY", True),
            patch.object(hiring_manager.apollo, "enrich_organization",
                         side_effect=AssertionError("must not re-enrich on reject")),
        ):
            out, decision = hiring_manager._recover_org_via_domain_corroboration(
                org, "acme.com", "Acme Foods", {}, stats)
        self.assertIs(out, org)
        self.assertFalse(decision.accepted)
        self.assertEqual(stats["hm_domain_recovery_rejected"], 1)


class DownstreamGatesPreservedTests(unittest.TestCase):
    # 5 recovered domain yields people but WRONG-FUNCTION titles -> still no HM
    def test_wrong_function_titles_still_rejected_by_ranker(self):
        # Corroboration only provides a domain; the title matcher is unchanged, so a
        # wrong-function person never ranks.
        people = [{"title": "Warehouse Associate", "id": "1"},
                  {"title": "Delivery Driver", "id": "2"}]
        ranked = hiring_manager.rank_candidates(people, ["VP Marketing", "Director of Marketing"])
        self.assertEqual(ranked, [])

    # 6 & 9 recovered HM with Apollo-unverified email -> not send-safe (same gate as
    # Approved Sync); recovery grants NO quality exception.
    def test_unverified_email_recovered_lead_is_not_send_safe(self):
        import airtable_client
        from validation_integrity import validation_fingerprint
        fields = {
            "Status": "Approved", "Final Decision": "NEEDS_CHECK",
            "Validation Version": config.VALIDATION_VERSION,
            "Email": "vp@globex.com", "Apollo Email Status": "unverified",
            "Email Validation": "PASS", "Contact Alignment": "PASS",
            "Firmographics Status": "PASS", "Company": "Globex",
            "Outbound Company": "Globex", "Outbound Company Confidence": "high",
            "Outbound Company Identity": "domain:globex.com", "Outbound Hold": False,
            "Outbound Role": "VP Marketing", "Outbound Role Confidence": "high",
            "Role Bucket": "marketing", "Campaign ID": "c1", "Website": "https://globex.com",
            "_hm_domain_recovered": True,
        }
        fields["Validation Fingerprint"] = validation_fingerprint(fields)
        ok, reason = airtable_client.send_safe_facts(fields)
        self.assertFalse(ok)                                   # unverified email blocks it
        self.assertEqual(reason, "apollo_email_not_verified")  # the EMAIL gate, not a signing gap


if __name__ == "__main__":
    unittest.main()
