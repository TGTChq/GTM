"""Phase 13 section 5: the pre-contact capacity controller.

Verifies canonical (no-double-count) accounting, deficit/headroom, the
strategy ladder with exhaustion, explicit stop reasons, default-off behavior,
and the offline-injectable orchestration loop.
"""
from __future__ import annotations

import unittest

import capacity_controller
from capacity_controller import CapacityController, build_from_config


class AccountingTests(unittest.TestCase):
    def test_same_canonical_company_across_sources_not_double_counted(self):
        c = CapacityController(target=250, headroom_target=300, enabled=True)
        c.register_searchable(["domain:acme.com", "domain:beta.com"])
        c.register_searchable(["domain:acme.com"])            # same company again
        c.register_searchable(["domain:acme.com", "domain:gamma.com"])
        self.assertEqual(c.searchable_count, 3)               # acme, beta, gamma

    def test_deficit_and_headroom(self):
        c = CapacityController(target=250, headroom_target=300, enabled=True)
        c.register_searchable([f"domain:c{i}.com" for i in range(200)])
        self.assertEqual(c.searchable_count, 200)
        self.assertEqual(c.deficit, 50)
        self.assertEqual(c.headroom_deficit, 100)
        self.assertFalse(c.target_met())
        self.assertFalse(c.headroom_met())

    def test_target_and_headroom_met(self):
        c = CapacityController(target=10, headroom_target=12, enabled=True)
        c.register_searchable([f"domain:c{i}.com" for i in range(12)])
        self.assertTrue(c.target_met())
        self.assertTrue(c.headroom_met())
        self.assertEqual(c.deficit, 0)


class StrategyLadderTests(unittest.TestCase):
    def test_next_strategy_skips_exhausted(self):
        c = CapacityController(target=250, headroom_target=300, enabled=True)
        c.mark_exhausted("upstream_inventory")
        c.mark_exhausted("base_multi_source")
        self.assertEqual(c.next_strategy(), "direct_ats")

    def test_next_strategy_respects_budget(self):
        c = CapacityController(target=250, headroom_target=300, enabled=True)
        # Only allow domain_recovery to have budget.
        strat = c.next_strategy(budget_ok=lambda s: s == "domain_recovery")
        self.assertEqual(strat, "domain_recovery")

    def test_stops_with_reason_when_headroom_met(self):
        c = CapacityController(target=10, headroom_target=12, enabled=True)
        c.register_searchable([f"domain:c{i}.com" for i in range(12)])
        self.assertIsNone(c.next_strategy())
        self.assertEqual(c.stop_reason, "headroom_target_met")

    def test_stops_with_reason_when_all_strategies_exhausted(self):
        c = CapacityController(target=250, headroom_target=300, enabled=True)
        for s in capacity_controller.STRATEGY_LADDER:
            c.mark_exhausted(s)
        self.assertIsNone(c.next_strategy())
        self.assertEqual(c.stop_reason, "all_strategies_exhausted")


class OrchestrationTests(unittest.TestCase):
    def test_run_until_target_drives_strategies_and_dedupes(self):
        c = CapacityController(target=5, headroom_target=6, enabled=True)
        runners = {
            "base_multi_source": lambda: ["domain:a.com", "domain:b.com", "domain:c.com"],
            "direct_ats": lambda: ["domain:c.com", "domain:d.com"],   # c overlaps
            "public_feeds": lambda: ["domain:e.com", "domain:f.com"],
        }
        state = c.run_until_target(runners)
        self.assertEqual(state["searchable_companies_available"], 6)  # a-f, c once
        self.assertTrue(state["headroom_met"])
        self.assertEqual(state["stop_reason"], "headroom_target_met")

    def test_run_until_target_stops_when_strategies_dry(self):
        c = CapacityController(target=250, headroom_target=300, enabled=True)
        runners = {"base_multi_source": lambda: ["domain:a.com"]}  # only 1 company available
        state = c.run_until_target(runners)
        self.assertEqual(state["searchable_companies_available"], 1)
        self.assertEqual(state["stop_reason"], "all_strategies_exhausted")

    def test_guard_stops_the_loop_with_its_reason(self):
        c = CapacityController(target=250, headroom_target=300, enabled=True)
        runners = {"base_multi_source": lambda: ["domain:a.com", "domain:b.com"]}
        state = c.run_until_target(runners, guard=lambda: "runtime_guard_reached")
        self.assertEqual(state["stop_reason"], "runtime_guard_reached")

    def test_disabled_controller_is_a_noop(self):
        c = CapacityController(target=250, headroom_target=300, enabled=False)
        state = c.run_until_target({"base_multi_source": lambda: ["domain:a.com"]})
        self.assertEqual(state["stop_reason"], "controller_disabled")
        self.assertEqual(state["searchable_companies_available"], 0)


class ConfigDefaultTests(unittest.TestCase):
    def test_build_from_config_defaults_disabled(self):
        import config
        c = build_from_config(config)
        self.assertFalse(c.enabled)                 # default OFF
        self.assertEqual(c.target, 250)
        self.assertEqual(c.headroom_target, 300)


if __name__ == "__main__":
    unittest.main()
