"""The allocator must actually control acquisition grants -- and never outrank the governor."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import config
import fantastic_jobs_adapter as FJA
from orchestrator.segment_allocator import BROAD_SEGMENT, Segment, allocate

FAMILIES = ["Account Executive", "Software Engineer", "Accountant", "Recruiter"]


class AllocateSafetyTests(unittest.TestCase):
    def _seg(self, sid, y, credits=1000, inv=None):
        return Segment(id=sid, yield_estimate=y, sample_credits=credits, inventory_hint=inv)

    def test_disabled_is_one_broad_grant(self):
        a = allocate(1000, [self._seg("a", 0.5)], enabled=False)
        self.assertEqual(a.mode, "broad")
        self.assertEqual(a.grants, {BROAD_SEGMENT: 1000})

    def test_insufficient_evidence_stays_broad(self):
        a = allocate(1000, [self._seg("a", 0.9, credits=10)], enabled=True,
                     min_evidence_credits=500)
        self.assertEqual(a.mode, "broad")
        self.assertEqual(a.detail["reason"], "insufficient_evidence")

    def test_never_exceeds_budget(self):
        for budget in (0, 1, 37, 1000, 9999):
            with self.subTest(budget=budget):
                a = allocate(budget, [self._seg("a", 0.9), self._seg("b", 0.1)],
                             enabled=True, max_segment_share=0.4, exploration_floor=0.2)
                self.assertLessEqual(sum(a.grants.values()), budget)

    def test_one_segment_cannot_capture_the_whole_budget(self):
        a = allocate(1000, [self._seg("a", 0.9)], enabled=True,
                     max_segment_share=0.4, exploration_floor=0.2)
        self.assertLessEqual(a.grants.get("a", 0), 400)
        self.assertGreater(a.grants.get(BROAD_SEGMENT, 0), 0)

    def test_exploration_floor_is_always_reserved(self):
        a = allocate(1000, [self._seg("a", 0.5), self._seg("b", 0.5)],
                     enabled=True, max_segment_share=1.0, exploration_floor=0.25)
        self.assertGreaterEqual(a.grants.get(BROAD_SEGMENT, 0), 250)

    def test_inventory_hint_clamps_a_segment(self):
        a = allocate(1000, [self._seg("a", 0.9, inv=50)], enabled=True,
                     max_segment_share=1.0, exploration_floor=0.0)
        self.assertEqual(a.grants["a"], 50)

    def test_corrupt_yield_table_yields_no_segments(self):
        from orchestrator.segment_allocator import load_yield_table
        tmp = tempfile.mkdtemp()
        bad = os.path.join(tmp, "bad.json")
        open(bad, "w").write("{not json")
        self.assertEqual(load_yield_table(bad), {})
        self.assertEqual(load_yield_table(os.path.join(tmp, "missing.json")), {})


class FamilyGrantWiringTests(unittest.TestCase):
    """Proof the allocation actually becomes the per-family acquisition cap."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "yields.json")

    def _write(self, table):
        json.dump(table, open(self.path, "w", encoding="utf-8"))

    def _cfg(self, enabled=True, **over):
        base = dict(SEGMENT_ALLOCATOR_ENABLED=enabled,
                    SEGMENT_ALLOCATOR_YIELD_TABLE_PATH=self.path,
                    # No ledger => the refresh is a no-op and the seeded table stands.
                    YIELD_LEDGER_PATH=os.path.join(self.tmp, "no_ledger.jsonl"),
                    SEGMENT_ALLOCATOR_MIN_EVIDENCE_CREDITS=500,
                    SEGMENT_ALLOCATOR_MAX_SEGMENT_SHARE=0.40,
                    SEGMENT_ALLOCATOR_EXPLORATION_FLOOR=0.20)
        base.update(over)
        return mock.patch.multiple(config, **base)

    def test_disabled_reproduces_the_historical_equal_split(self):
        with self._cfg(enabled=False):
            grants, mode = FJA._family_grants(FAMILIES, 1000, {})
        self.assertEqual(mode, "equal_split")
        self.assertEqual(set(grants.values()), {250})

    def test_no_evidence_falls_back_to_equal_split(self):
        self._write({})
        with self._cfg():
            grants, mode = FJA._family_grants(FAMILIES, 1000, {})
        self.assertEqual(mode, "broad")
        self.assertEqual(set(grants.values()), {250})

    def test_evidence_actually_changes_the_grants(self):
        self._write({"Account Executive": {"yield": 0.9, "credits": 2000},
                     "Accountant": {"yield": 0.1, "credits": 2000}})
        metrics = {}
        with self._cfg():
            grants, mode = FJA._family_grants(FAMILIES, 1000, metrics)
        self.assertEqual(mode, "weighted")
        self.assertGreater(grants["Account Executive"], grants["Accountant"])
        self.assertIn("segment_allocator", metrics)

    def test_grants_never_exceed_the_governor_cap(self):
        """HARD INVARIANT: the allocator distributes, it never raises the budget."""
        self._write({f: {"yield": 0.9, "credits": 5000} for f in FAMILIES})
        for cap in (10, 100, 1000):
            with self.subTest(cap=cap), self._cfg():
                grants, _mode = FJA._family_grants(FAMILIES, cap, {})
                self.assertLessEqual(sum(grants.values()), cap)

    def test_no_family_is_ever_starved_to_zero(self):
        self._write({"Account Executive": {"yield": 0.99, "credits": 5000}})
        with self._cfg():
            grants, _mode = FJA._family_grants(FAMILIES, 1000, {})
        for fam in FAMILIES:
            self.assertGreaterEqual(grants[fam], 1)

    def test_corrupt_table_falls_back_without_raising(self):
        open(self.path, "w").write("{broken")
        with self._cfg():
            grants, mode = FJA._family_grants(FAMILIES, 1000, {})
        self.assertEqual(set(grants.values()), {250})

    def test_allocator_failure_degrades_to_equal_split(self):
        with self._cfg(), mock.patch("orchestrator.segment_allocator.allocate",
                                     side_effect=RuntimeError("boom")):
            grants, mode = FJA._family_grants(FAMILIES, 1000, {})
        self.assertEqual(mode, "error_fallback_broad")
        self.assertEqual(set(grants.values()), {250})

    def test_grants_are_consumed_as_the_real_family_cap(self):
        import inspect
        src = inspect.getsource(FJA.run_fantastic_jobs_acquisition)
        self.assertIn("_grants, _alloc_mode = _family_grants(families, global_cap, metrics)", src)
        # Budget accounting is BILLED (quota.jobs_consumed), not KEPT: the provider
        # charges every returned row, so kept-based caps let dup-heavy families and
        # sources overspend the shared run budget.
        self.assertIn('cap = min(int(_grants.get(term, 1)),\n                          global_cap - (quota.jobs_consumed - _fam_before))', src)


if __name__ == "__main__":
    unittest.main()


class YieldEvidenceProducerTests(unittest.TestCase):
    """The allocator must have a real evidence producer, not a decorative flag."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = os.path.join(self.tmp, "ledger.jsonl")
        self.table = os.path.join(self.tmp, "yields.json")

    def _write_ledger(self, rows):
        with open(self.ledger, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_table_is_built_from_the_ledger(self):
        from orchestrator.segment_allocator import refresh_yield_table
        self._write_ledger([
            {"title_family": "Account Executive", "fantastic_credits": 1,
             "send_safe": True, "net_new_send_safe": True},
            {"title_family": "Account Executive", "fantastic_credits": 1,
             "send_safe": False, "net_new_send_safe": False},
            {"title_family": "Accountant", "fantastic_credits": 1,
             "send_safe": False, "net_new_send_safe": False},
        ])
        self.assertEqual(refresh_yield_table(self.ledger, self.table), 2)
        table = json.load(open(self.table, encoding="utf-8"))
        self.assertAlmostEqual(table["Account Executive"]["yield"], 0.5)
        self.assertAlmostEqual(table["Accountant"]["yield"], 0.0)

    def test_metric_is_net_new_per_billed_job_not_gross(self):
        """North star: never optimise raw FINAL_PASS."""
        from orchestrator.segment_allocator import refresh_yield_table
        self._write_ledger([
            {"title_family": "F", "fantastic_credits": 4,
             "send_safe": True, "net_new_send_safe": False},
        ])
        refresh_yield_table(self.ledger, self.table)
        self.assertEqual(json.load(open(self.table, encoding="utf-8"))["F"]["yield"], 0.0)

    def test_missing_ledger_leaves_the_table_untouched(self):
        from orchestrator.segment_allocator import refresh_yield_table
        self.assertEqual(refresh_yield_table(os.path.join(self.tmp, "nope"), self.table), 0)
        self.assertFalse(os.path.exists(self.table))

    def test_producer_is_invoked_before_allocating(self):
        import inspect
        src = inspect.getsource(FJA._family_grants)
        self.assertIn("refresh_yield_table(", src)
        self.assertLess(src.index("refresh_yield_table("), src.index("allocate("))
