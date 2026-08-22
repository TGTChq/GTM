"""Yield ledger, Apollo cache, segment allocator, experiment stats: pure-module tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from orchestrator.yield_ledger import YieldLedger, aggregate_yield
from orchestrator.apollo_cache import ApolloCache, normalize_domain
from orchestrator.segment_allocator import Segment, allocate, BROAD_SEGMENT
from orchestrator.source_experiment import ArmStats, StoppingRule, wilson_interval


def _job(i, **over):
    j = {"_fantastic_internal_id": str(i), "job_id": f"fantastic_{i}",
         "_acquisition_source": "fantastic_jobs_linkedin", "_provider_dataset": "jb",
         "_title_family": "account_executive", "employer_website": "acme.com",
         "_org_industry": "Software Development", "_org_headcount": 120,
         "job_posted_at_datetime_utc": "2026-08-20T00:00:00+00:00"}
    j.update(over)
    return j


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "ledger.jsonl")

    def test_one_row_per_paid_job_and_idempotent(self):
        L = YieldLedger(self.path, "run1", now=datetime(2026, 8, 22, tzinfo=timezone.utc))
        self.assertEqual(L.record_acquired([_job(1), _job(2), _job(2)]), 3)
        self.assertEqual(len(L.rows), 2)                      # same id => one row
        L.mark("fantastic_1", send_safe=True, airtable_created=True, net_new_send_safe=True)
        L.flush()
        L2 = YieldLedger(self.path, "run1")
        L2.record_acquired([_job(1), _job(2)])
        L2.flush()                                             # re-run: no duplicates
        with open(self.path) as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len(rows), 2)

    def test_collapse_attributes_credit_once(self):
        L = YieldLedger(self.path, "r")
        L.record_acquired([_job(1), _job(2), _job(3)])
        L.mark_collapsed("fantastic_1", ["fantastic_2", "fantastic_3"])
        L.mark("fantastic_1", send_safe=True, net_new_send_safe=True)
        L.flush()
        agg = aggregate_yield(self.path, by="title_family")["account_executive"]
        self.assertEqual(agg["credits"], 3)
        self.assertEqual(agg["net_new_send_safe"], 1)
        self.assertAlmostEqual(agg["yield"], 1 / 3)
        self.assertEqual(L.rows["r|2"].exit_stage, "collapsed")

    def test_failure_never_raises(self):
        L = YieldLedger(os.path.join(self.tmp, "nodir", "x", "ledger.jsonl"), "r")
        L.record_acquired([_job(1)])
        # make the directory un-creatable by using a file as parent
        open(os.path.join(self.tmp, "blocker"), "w").close()
        L.path = os.path.join(self.tmp, "blocker", "ledger.jsonl")
        self.assertEqual(L.flush(), 0)
        self.assertGreaterEqual(L.errors, 1)

    def test_no_pii_fields(self):
        L = YieldLedger(self.path, "r")
        L.record_acquired([_job(1, hiring_manager_email="x@y.com")])
        L.flush()
        with open(self.path) as fh:
            text = fh.read()
        self.assertNotIn("x@y.com", text)

    def test_disabled_is_noop(self):
        L = YieldLedger(self.path, "r", enabled=False)
        self.assertEqual(L.record_acquired([_job(1)]), 0)
        self.assertEqual(L.flush(), 0)
        self.assertFalse(os.path.exists(self.path))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "cache.json")
        self.t0 = datetime(2026, 8, 22, tzinfo=timezone.utc)

    def _c(self, now=None, fp="v1", enabled=True):
        return ApolloCache(self.path, enabled=enabled, rules_fingerprint=fp, now=now or self.t0,
                           ttl_days={"org": 60, "people_pos": 45, "zero_people": 21,
                                     "zero_title": 14, "person_match": 45})

    def test_normalize_domain_rejects_social(self):
        self.assertEqual(normalize_domain("https://www.Acme.com/x"), "acme.com")
        self.assertEqual(normalize_domain("linkedin.com/company/acme"), "")
        self.assertEqual(normalize_domain("nodots"), "")

    def test_positive_hit_and_ttl_expiry(self):
        c = self._c()
        c.put("org", "acme.com", {"employee_count": 100})
        self.assertEqual(c.get("org", "acme.com")["employee_count"], 100)
        c.save()
        late = self._c(now=self.t0 + timedelta(days=61))
        self.assertIsNone(late.get("org", "acme.com"))
        self.assertEqual(late.metrics["expired"], 1)

    def test_negative_hit_never_permanent_and_cleared_by_positive(self):
        c = self._c()
        c.put("zero_people", "acme.com", {"reason": "zero_apollo_people"})
        self.assertIsNotNone(c.get("zero_people", "acme.com"))
        self.assertEqual(c.metrics["negative_cache_hit"], 1)
        c.put("people_pos", ApolloCache.people_key("acme.com", "gtm"), {"person_ids": ["p1"]})
        self.assertIsNone(c.get("zero_people", "acme.com"))     # cleared
        c.save()
        late = self._c(now=self.t0 + timedelta(days=22))
        late.put("zero_people", "beta.com", {})
        self.assertIsNotNone(late.get("zero_people", "beta.com"))
        later = self._c(now=self.t0 + timedelta(days=44))
        self.assertIsNone(later.get("zero_people", "beta.com"))  # expired, not forever

    def test_fingerprint_invalidation(self):
        c = self._c(fp="icp-v1")
        c.put("org", "acme.com", {"employee_count": 100})
        c.save()
        c2 = self._c(fp="icp-v2")
        self.assertIsNone(c2.get("org", "acme.com", fingerprint_sensitive=True))
        self.assertEqual(c2.metrics["invalidated_fingerprint"], 1)

    def test_untrusted_not_cached(self):
        c = self._c()
        c.put("org", "acme.com", {"x": 1}, trusted=False)
        self.assertIsNone(c.get("org", "acme.com"))
        self.assertEqual(c.metrics["untrusted_skipped"], 1)

    def test_domain_change_invalidates(self):
        c = self._c()
        c.put("org", "acme.com", {})
        c.put("people_pos", ApolloCache.people_key("acme.com", "gtm"), {})
        self.assertEqual(c.invalidate_domain("acme.com"), 2)
        self.assertIsNone(c.get("org", "acme.com"))

    def test_disabled_noop(self):
        c = self._c(enabled=False)
        c.put("org", "acme.com", {})
        self.assertIsNone(c.get("org", "acme.com"))
        c.save()
        self.assertFalse(os.path.exists(self.path))


class AllocatorTests(unittest.TestCase):
    def test_default_broad_preserves_current_behavior(self):
        a = allocate(700, [Segment("a", 0.2, sample_credits=1000)], enabled=False)
        self.assertEqual(a.mode, "broad")
        self.assertEqual(a.grants, {BROAD_SEGMENT: 700})

    def test_insufficient_evidence_stays_broad(self):
        a = allocate(700, [Segment("a", 0.2, sample_credits=10)], enabled=True)
        self.assertEqual(a.mode, "broad")

    def test_weighted_respects_budget_and_hints(self):
        segs = [Segment("hi", 0.3, inventory_hint=100, sample_credits=1000),
                Segment("lo", 0.1, sample_credits=1000)]
        a = allocate(1000, segs, enabled=True)
        self.assertEqual(sum(a.grants.values()), 1000)
        self.assertLessEqual(a.grants["hi"], 100)


class ExperimentStatsTests(unittest.TestCase):
    def test_wilson_bounds(self):
        lo, hi = wilson_interval(30, 200, 0.90)
        self.assertTrue(0.10 < lo < 0.15 < hi < 0.20)
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_sequential_stop_rules(self):
        rule = StoppingRule(min_per_arm=100, max_total_budget=600, confidence=0.90)
        c, t = ArmStats("control"), ArmStats("treatment")
        self.assertEqual(rule.decide(c, t)["reason"], "below_min_sample")
        c.jobs_billed, c.send_safe = 150, 15          # 10%
        t.jobs_billed, t.send_safe = 150, 60          # 40% -> dominates
        d = rule.decide(c, t)
        self.assertFalse(d["continue"]); self.assertEqual(d["winner"], "treatment")
        c.jobs_billed, t.jobs_billed = 300, 300
        self.assertEqual(rule.decide(c, t)["reason"], "max_budget")
        self.assertEqual(rule.next_allocation(c, t, 100), {"control": 0, "treatment": 0})


if __name__ == "__main__":
    unittest.main()
