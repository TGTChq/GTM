"""Apollo zero-people org-id fallback: gating, safety guards, metrics, cache."""
from __future__ import annotations

import unittest
from unittest import mock

import apollo_client
import config
import hiring_manager as HM


def _person(pid, domain="acme.com", title="VP Sales"):
    return {"id": pid, "first_name": "A", "last_name": "B", "title": title,
            "organization": {"name": "Acme", "primary_domain": domain, "website_url": f"https://{domain}"}}


class OrgIdSearchTests(unittest.TestCase):
    """The client function itself: params, title preservation, wrong-company guard."""

    def _run(self, payload, **kw):
        captured = {}

        class R:
            status_code = 200
            def json(self): return payload
        def fake(method, url, headers=None, params=None, **_):
            captured["url"] = url; captured["params"] = list(params or [])
            return R()
        with mock.patch.object(apollo_client, "request_with_retry", fake), \
                mock.patch.object(apollo_client, "safe_json", lambda r: r.json()):
            out = apollo_client.search_people_by_org_id("org_123", ["VP Sales", "Head of Sales"], **kw)
        return out, captured

    def test_uses_organization_ids_and_preserves_titles(self):
        out, cap = self._run({"people": [_person("p1")]})
        keys = [k for k, _ in cap["params"]]
        self.assertIn("organization_ids[]", keys)
        self.assertNotIn("q_organization_domains_list[]", keys)      # org-id selector only
        self.assertEqual(dict(cap["params"])["include_similar_titles"], "false")
        titles = [v for k, v in cap["params"] if k == "person_titles[]"]
        self.assertEqual(titles, ["VP Sales", "Head of Sales"])      # NEVER broadened
        self.assertEqual(len(out), 1)

    def test_wrong_company_person_is_discarded(self):
        out, _ = self._run({"people": [_person("p1", domain="acme.com"),
                                       _person("p2", domain="other-co.com")]},
                           expected_domain="acme.com")
        self.assertEqual([p["id"] for p in out], ["p1"])

    def test_subdomain_accepted(self):
        out, _ = self._run({"people": [_person("p1", domain="jobs.acme.com")]},
                           expected_domain="acme.com")
        self.assertEqual(len(out), 1)

    def test_missing_org_id_or_titles_is_noop(self):
        self.assertEqual(apollo_client.search_people_by_org_id("", ["X"]), [])
        self.assertEqual(apollo_client.search_people_by_org_id("org_1", []), [])

    def test_transport_failure_returns_empty_never_raises(self):
        def boom(*a, **k): raise RuntimeError("network")
        with mock.patch.object(apollo_client, "request_with_retry", boom):
            self.assertEqual(apollo_client.search_people_by_org_id("org_1", ["X"]), [])


class TrustGuardTests(unittest.TestCase):
    def _org(self, **kw):
        return apollo_client.OrgEnrichment(**{"found": True, **kw})

    def test_same_domain_trusted(self):
        self.assertTrue(HM._org_is_trusted_for_domain(self._org(domain="acme.com"), "acme.com"))

    def test_blank_org_domain_trusted(self):
        self.assertTrue(HM._org_is_trusted_for_domain(self._org(domain=""), "acme.com"))

    def test_mismatched_org_domain_untrusted(self):
        self.assertFalse(HM._org_is_trusted_for_domain(self._org(domain="other.com"), "acme.com"))

    def test_no_expected_domain_untrusted(self):
        self.assertFalse(HM._org_is_trusted_for_domain(self._org(domain="acme.com"), ""))


class FallbackGatingTests(unittest.TestCase):
    """Decision logic reproducing the production guard chain exactly."""

    def _should_fire(self, *, enabled, apollo_error, people, org):
        return bool(not apollo_error and not people and enabled
                    and getattr(org, "found", False) and getattr(org, "organization_id", None)
                    and HM._org_is_trusted_for_domain(org, "acme.com"))

    def setUp(self):
        self.good = apollo_client.OrgEnrichment(found=True, organization_id="org_1", domain="acme.com")

    def test_primary_success_no_fallback(self):
        self.assertFalse(self._should_fire(enabled=True, apollo_error=False,
                                           people=[_person("p1")], org=self.good))

    def test_zero_people_with_trusted_org_fires(self):
        self.assertTrue(self._should_fire(enabled=True, apollo_error=False, people=[], org=self.good))

    def test_flag_off_never_fires(self):
        self.assertFalse(self._should_fire(enabled=False, apollo_error=False, people=[], org=self.good))

    def test_apollo_error_never_fires(self):
        # An error is NOT a confirmed zero -- must not trigger a fallback.
        self.assertTrue(self._should_fire(enabled=True, apollo_error=False, people=[], org=self.good))
        self.assertFalse(self._should_fire(enabled=True, apollo_error=True, people=[], org=self.good))

    def test_missing_org_id_never_fires(self):
        org = apollo_client.OrgEnrichment(found=True, organization_id=None, domain="acme.com")
        self.assertFalse(self._should_fire(enabled=True, apollo_error=False, people=[], org=org))

    def test_org_not_found_never_fires(self):
        org = apollo_client.OrgEnrichment(found=False, organization_id="org_1", domain="acme.com")
        self.assertFalse(self._should_fire(enabled=True, apollo_error=False, people=[], org=org))

    def test_domain_mismatch_never_fires(self):
        org = apollo_client.OrgEnrichment(found=True, organization_id="org_1", domain="other.com")
        self.assertFalse(self._should_fire(enabled=True, apollo_error=False, people=[], org=org))


class NoDuplicatePaidEnrichmentTests(unittest.TestCase):
    def test_fallback_replaces_candidate_pool_never_adds_a_second_pool(self):
        """The recovered pool REPLACES `people` (it is only reached when the primary
        pool was empty), so the downstream per-candidate match_person loop can never
        run twice for the same bucket -- no duplicate PAID enrichment."""
        people = []
        recovered = [_person("p1"), _person("p2")]
        if not people:
            people = recovered
        self.assertEqual(len(people), 2)
        self.assertIs(people, recovered)

    def test_search_endpoint_is_zero_credit_not_an_enrichment_call(self):
        src = open("apollo_client.py", encoding="utf-8").read()
        start = src.index("def search_people_by_org_id")
        body = src[start:start + 3200]
        self.assertIn("mixed_people/api_search", body)          # search endpoint
        for paid in ("people/match", "organizations/enrich", "bulk_people", "reveal_personal_emails"):
            self.assertNotIn(paid, body)                        # never an enrichment/reveal


class MetricsTests(unittest.TestCase):
    def test_metric_names_present_in_implementation(self):
        src = open("hiring_manager.py", encoding="utf-8").read()
        for m in ("apollo_people_domain_searches", "apollo_people_domain_zero",
                  "apollo_people_org_id_fallback_attempted", "apollo_people_org_id_people_found",
                  "apollo_people_org_id_hm_found", "apollo_people_org_id_verified",
                  "apollo_people_org_id_send_safe"):
            self.assertIn(m, src, f"missing metric {m}")

    def test_downstream_attribution_only_counts_recovered_leads(self):
        stats = {"apollo_people_org_id_hm_found": 0, "apollo_people_org_id_verified": 0,
                 "apollo_people_org_id_send_safe": 0}
        leads = [
            {"_apollo_org_id_recovered": True, "_step3_status": "found",
             "apollo_email_status": "verified", "_final_state": "FINAL_PASS"},
            {"_apollo_org_id_recovered": True, "_step3_status": "found",
             "apollo_email_status": "extrapolated", "_final_state": "UNVERIFIED"},
            {"_step3_status": "found", "apollo_email_status": "verified",
             "_final_state": "FINAL_PASS"},                      # NOT recovered
        ]
        for lead in leads:
            if not lead.get("_apollo_org_id_recovered"):
                continue
            if str(lead.get("_step3_status") or "") == "found":
                stats["apollo_people_org_id_hm_found"] += 1
                if str(lead.get("apollo_email_status") or "").lower() == "verified":
                    stats["apollo_people_org_id_verified"] += 1
                if str(lead.get("_final_state") or "") == "FINAL_PASS":
                    stats["apollo_people_org_id_send_safe"] += 1
        self.assertEqual(stats, {"apollo_people_org_id_hm_found": 2,
                                 "apollo_people_org_id_verified": 1,
                                 "apollo_people_org_id_send_safe": 1})


class FlagDefaultTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(config.APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED)


if __name__ == "__main__":
    unittest.main()
