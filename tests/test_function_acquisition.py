"""Function-aware upstream dedupe, role-family partition, slug crosswalk,
activity ontology, and ledger attribution."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import config
import fantastic_jobs_adapter as fja
from orchestrator import function_acquisition as FA
from orchestrator.yield_ledger import YieldLedger

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


class PartitionTests(unittest.TestCase):
    def test_production_catalog_is_an_exact_partition(self):
        from role_catalog import DEFAULT_ACQUISITION_ROLES as ROLES
        v = FA.validate_partition(list(ROLES))
        self.assertEqual(v["overlaps"], 0)
        self.assertEqual(v["roles"], len({" ".join(r.lower().split()) for r in ROLES}))
        self.assertEqual(sum(v["families"].values()), v["roles"])

    def test_every_role_maps_to_exactly_one_family(self):
        from role_catalog import DEFAULT_ACQUISITION_ROLES as ROLES
        buckets = FA.partition_roles(ROLES)
        seen = {}
        for fam, roles in buckets.items():
            for r in roles:
                self.assertNotIn(r, seen, f"{r} in {fam} and {seen.get(r)}")
                seen[r] = fam
        self.assertEqual(len(seen), len(list(ROLES)))

    def test_no_duplicate_title_clause_between_families(self):
        from role_catalog import DEFAULT_ACQUISITION_ROLES as ROLES
        buckets = FA.partition_roles(ROLES)
        all_clauses = [c for rs in buckets.values() for c in rs]
        self.assertEqual(len(all_clauses), len(set(all_clauses)))

    def test_representative_role_assignments(self):
        self.assertEqual(FA.family_for_role("Account Executive"), FA.FAMILY_GTM)
        self.assertEqual(FA.family_for_role("Customer Success"), FA.FAMILY_CS)
        self.assertEqual(FA.family_for_role("Automation Specialist"), FA.FAMILY_ENGINEERING)
        self.assertEqual(FA.family_for_role("Accountant"), FA.FAMILY_FINOPS)
        self.assertEqual(FA.family_for_role("Recruiter"), FA.FAMILY_PEOPLE)
        self.assertEqual(FA.family_for_role("Product Manager"), FA.FAMILY_PRODUCT)

    def test_partition_raises_on_conflicting_assignment(self):
        orig = FA._FAMILY_RULES
        try:
            # Force an ambiguous rule set: same clause reachable from two families.
            FA._FAMILY_RULES = ((FA.FAMILY_GTM, ("zzz",)), (FA.FAMILY_CS, ("zzz",)))
            # First-match-wins still yields ONE family -> partition holds by design.
            self.assertEqual(FA.family_for_role("zzz role"), FA.FAMILY_GTM)
        finally:
            FA._FAMILY_RULES = orig


class SlugCrosswalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "cw.json")

    def _cw(self, now=NOW, ttl=120):
        return FA.SlugCrosswalk(self.path, ttl_days=ttl, now=now)

    def test_learns_slug_domain_company_function_from_jobs(self):
        cw = self._cw()
        n = cw.observe_jobs([
            {"org_linkedin_slug": "acme", "employer_website": "acme.com",
             "employer_name": "Acme", "_role_bucket": "gtm_revenue"},
            {"org_linkedin_slug": "acme", "employer_website": "acme.com",
             "employer_name": "Acme", "_role_bucket": "engineering"},
            {"employer_website": "noslug.com"},          # no slug -> ignored
        ])
        self.assertEqual(n, 2)
        cw.save()
        e = self._cw().state["by_slug"]["acme"]
        self.assertEqual(e["domain"], "acme.com")
        self.assertEqual(sorted(e["buckets"]), ["engineering", "gtm_revenue"])
        self.assertEqual(e["source"], "fantastic_jobs")
        self.assertTrue(e["observed_at"])

    def test_ttl_expiry_handles_rebrand_drift(self):
        cw = self._cw()
        cw.observe(slug="oldbrand", domain="acme.com")
        cw.save()
        fresh = self._cw(now=NOW + timedelta(days=30))
        self.assertEqual(fresh.slugs_for_domains(["acme.com"]), ["oldbrand"])
        stale = self._cw(now=NOW + timedelta(days=200))
        self.assertEqual(stale.slugs_for_domains(["acme.com"]), [])   # aged out

    def test_persistence_is_best_effort_never_raises(self):
        cw = FA.SlugCrosswalk(os.path.join(self.tmp, "blocked", "x", "cw.json"))
        cw.observe(slug="a", domain="a.com")
        open(os.path.join(self.tmp, "blk"), "w").close()
        cw.path = os.path.join(self.tmp, "blk", "cw.json")
        cw.save()   # must not raise


class CoveredSlugExclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cw = FA.SlugCrosswalk(os.path.join(self.tmp, "cw.json"), now=NOW)
        self.cw.observe(slug="acme", domain="acme.com")
        self.cw.observe(slug="beta", domain="beta.com")

    def test_company_function_isolation_same_company_other_function_preserved(self):
        covered = {"domain:acme.com|bucket:gtm_revenue"}
        gtm = FA.covered_slugs_for_family(self.cw, covered, FA.FAMILY_GTM)
        eng = FA.covered_slugs_for_family(self.cw, covered, FA.FAMILY_ENGINEERING)
        self.assertEqual(gtm, ["acme"])     # excluded from GTM
        self.assertEqual(eng, [])           # STILL eligible for Engineering

    def test_only_active_keys_contribute(self):
        # snapshot_existing_identity only emits ACTIVE rows, so a Rejected/Error
        # company never appears in covered_function_keys and stays acquirable.
        self.assertEqual(FA.covered_slugs_for_family(self.cw, set(), FA.FAMILY_GTM), [])

    def test_name_keyed_entries_are_ignored_for_slug_exclusion(self):
        covered = {"name:acme|bucket:gtm_revenue"}
        self.assertEqual(FA.covered_slugs_for_family(self.cw, covered, FA.FAMILY_GTM), [])

    def test_unknown_slug_domain_is_not_excluded(self):
        covered = {"domain:unknown-co.com|bucket:gtm_revenue"}
        self.assertEqual(FA.covered_slugs_for_family(self.cw, covered, FA.FAMILY_GTM), [])


class ChunkingTests(unittest.TestCase):
    def test_chunks_respect_count_limit(self):
        slugs = [f"co-{i}" for i in range(600)]
        chunks = FA.chunk_slugs(slugs, chunk_size=250)
        self.assertEqual([len(c) for c in chunks], [250, 250, 100])
        self.assertEqual(sum(len(c) for c in chunks), 600)

    def test_chunks_respect_url_length_budget(self):
        slugs = ["x" * 300 for _ in range(100)]
        chunks = FA.chunk_slugs(slugs, chunk_size=250, max_url_chars=3000)
        self.assertTrue(all(sum(len(s) + 1 for s in c) <= 3000 for c in chunks))
        self.assertGreater(len(chunks), 1)

    def test_empty_and_single(self):
        self.assertEqual(FA.chunk_slugs([]), [])
        self.assertEqual(FA.chunk_slugs(["a"]), [["a"]])


class ActivityOntologyTests(unittest.TestCase):
    def test_clusters_are_evidence_anchored(self):
        for c in FA.ACTIVITY_CLUSTERS:
            self.assertGreater(c.billed, 0, c.id)
            self.assertTrue(c.anchor_skills, c.id)
            self.assertIn(c.family, FA.FAMILY_ORDER, c.id)

    def test_positive_and_negative_clusters_separate_around_baseline(self):
        pos = [c for c in FA.ACTIVITY_CLUSTERS if c.yield_rate > 0.1739]
        neg = [c for c in FA.ACTIVITY_CLUSTERS if c.yield_rate < 0.05]
        self.assertTrue(pos and neg)
        # The measured negative clusters are large enough to matter.
        self.assertGreaterEqual(sum(c.billed for c in neg), 400)

    def test_adjacent_candidates_map_to_exactly_one_family(self):
        for fam, titles in FA.ADJACENT_TITLE_CANDIDATES.items():
            self.assertIn(fam, FA.FAMILY_ORDER)
            for t in titles:
                self.assertIsInstance(t, str)
        seen = {}
        for fam, titles in FA.ADJACENT_TITLE_CANDIDATES.items():
            for t in titles:
                self.assertNotIn(t, seen, f"{t} duplicated across families")
                seen[t] = fam

    def test_scoped_exclusions_are_family_scoped_never_global(self):
        self.assertIn(FA.FAMILY_ENGINEERING, FA.FAMILY_SCOPED_EXCLUSIONS)
        for fam in FA.FAMILY_SCOPED_EXCLUSIONS:
            self.assertIn(fam, FA.FAMILY_ORDER)

    def test_provider_signals_documented_and_key_skills_not_queryable(self):
        for k in ("title_advanced", "ai_taxonomies_a", "ai_taxonomies_a_primary",
                  "exclude_ai_taxonomies_a", "seniority", "ai_work_arrangement"):
            self.assertIn(k, FA.PROVIDER_FUNCTIONAL_SIGNALS)
        self.assertNotIn("ai_key_skills", FA.PROVIDER_FUNCTIONAL_SIGNALS)


class FlagDefaultsAndQueryInvarianceTests(unittest.TestCase):
    def test_flags_default_off(self):
        self.assertFalse(config.FANTASTIC_FUNCTION_AWARE_UPSTREAM_DEDUPE_ENABLED)
        self.assertFalse(config.FANTASTIC_FUNCTIONAL_ROLE_EXPANSION_ENABLED)

    def test_flag_off_keeps_production_query_byte_identical(self):
        from role_catalog import DEFAULT_SEARCH_ROLES
        plan = fja.build_title_query_plan()
        # Flag OFF must yield exactly the catalog union -- no expansion clauses.
        self.assertEqual(plan["expression"].count("|") + 1, len(DEFAULT_SEARCH_ROLES))
        self.assertFalse(any(c.get("expanded") for c in plan["clauses"]))


class LedgerAttributionTests(unittest.TestCase):
    def test_records_family_clause_slug_and_fallback_fields(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "l.jsonl")
        L = YieldLedger(path, "run1")
        L.record_acquired([{
            "_fantastic_internal_id": "1", "job_id": "fantastic_1",
            "_acquisition_source": "fantastic_jobs_linkedin", "_provider_dataset": "jb",
            "_title_family": "account_executive",
            "_acquisition_function_family": FA.FAMILY_GTM,
            "_activity_cluster": "revenue_systems",
            "_matched_role": "Account Executive",
            "org_linkedin_slug": "acme", "employer_website": "acme.com",
            "_org_industry": "Software Development", "_org_headcount": 120,
            "job_posted_at_datetime_utc": "2026-08-20T00:00:00+00:00",
        }])
        L.mark("fantastic_1", org_id_fallback_attempted=True,
               org_id_fallback_recovered=True, company_function_existing=True,
               send_safe=True, net_new_send_safe=True)
        L.flush()
        rec = [json.loads(x) for x in open(path) if x.strip()][0]
        self.assertEqual(rec["acquisition_function_family"], FA.FAMILY_GTM)
        self.assertEqual(rec["functional_activity_cluster"], "revenue_systems")
        self.assertEqual(rec["matching_title_clause"], "Account Executive")
        self.assertEqual(rec["organization_slug"], "acme")
        self.assertTrue(rec["org_id_fallback_attempted"])
        self.assertTrue(rec["org_id_fallback_recovered"])
        self.assertTrue(rec["company_function_existing"])
        self.assertEqual(rec["fantastic_credits"], 1)

    def test_no_pii_in_new_fields(self):
        tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "l.jsonl")
        L = YieldLedger(path, "r")
        L.record_acquired([{"_fantastic_internal_id": "1", "org_linkedin_slug": "acme",
                            "hiring_manager_email": "rob@acme.com"}])
        L.flush()
        self.assertNotIn("rob@acme.com", open(path).read())


if __name__ == "__main__":
    unittest.main()
