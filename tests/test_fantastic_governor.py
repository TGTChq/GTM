"""Monthly Fantastic credit governor: allocation invariants + persisted ledger."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from orchestrator import fantastic_governor as G


def _inp(**over):
    base = dict(monthly_limit=20000, now=datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc),
                cycle_reset_at=datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc),
                ledger_used_this_cycle=0, provider_jobs_remaining=None, reserve_pct=0.10,
                daily_min_jobs=100, daily_max_jobs=0, per_run_ceiling=6000, quota_floor=500,
                carry_forward=0, carry_forward_cap_days=3.0, inventory_hint=None, spent_today=0)
    base.update(over)
    return G.GovernorInputs(**base)


class DecideInvariants(unittest.TestCase):
    def test_fresh_cycle_paces_not_bursts(self):
        d = G.decide(_inp(provider_jobs_remaining=20000))
        # spendable = 18000 over ~26.5 days => ~679/day; never the 6000 ceiling
        self.assertLess(d.run_budget, 1000)
        self.assertGreaterEqual(d.run_budget, 100)
        self.assertEqual(d.reserve_credits, 2000)
        self.assertTrue(d.provider_authoritative)

    def test_mid_cycle_uses_remaining(self):
        d = G.decide(_inp(provider_jobs_remaining=10000,
                          now=datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)))
        # spendable 8000 over ~14.5d => ~550
        self.assertTrue(400 <= d.run_budget <= 700, d)

    def test_nearly_exhausted_below_floor_grants_zero(self):
        d = G.decide(_inp(provider_jobs_remaining=436))
        self.assertEqual(d.run_budget, 0)
        self.assertEqual(d.reason, "provider_quota_floor")

    def test_never_exceeds_provider_remaining_or_ceiling(self):
        d = G.decide(_inp(provider_jobs_remaining=900, carry_forward=100000, quota_floor=0,
                          reserve_pct=0.0))
        self.assertLessEqual(d.run_budget, 900)
        d2 = G.decide(_inp(provider_jobs_remaining=20000, per_run_ceiling=300, reserve_pct=0.0,
                           quota_floor=0, carry_forward=100000))
        self.assertLessEqual(d2.run_budget, 300)

    def test_reserve_is_held_back(self):
        d = G.decide(_inp(provider_jobs_remaining=2100, quota_floor=0,
                          now=datetime(2026, 10, 16, 13, 0, tzinfo=timezone.utc)))
        # remaining 2100, reserve 2000 => spendable 100 even on the last day
        self.assertLessEqual(d.run_budget, 100)

    def test_missing_headers_and_empty_ledger_fails_conservative(self):
        # Gate-A BUG 1: with NO provider data AND NO ledger evidence the true
        # remaining is unknown -> grant at most the daily MINIMUM, never full pace.
        d = G.decide(_inp(provider_jobs_remaining=None, cycle_reset_at=None, ledger_has_runs=False))
        self.assertFalse(d.provider_authoritative)
        self.assertTrue(d.detail["blind_mode"])
        self.assertEqual(d.run_budget, 100)
        self.assertEqual(d.reason, "blind_conservative")

    def test_missing_headers_with_ledger_evidence_paces_from_ledger(self):
        d = G.decide(_inp(provider_jobs_remaining=None, ledger_used_this_cycle=19000,
                          ledger_has_runs=True))
        self.assertFalse(d.detail["blind_mode"])
        self.assertEqual(d.remaining_credits, 1000)
        self.assertLessEqual(d.run_budget, 500)     # 1000 - floor 500

    def test_negative_header_means_exhausted_not_missing(self):
        # Gate-A BUG 3: a negative remaining is EXHAUSTED, never "ignore the header".
        d = G.decide(_inp(provider_jobs_remaining=-5, ledger_used_this_cycle=0))
        self.assertTrue(d.provider_authoritative)
        self.assertEqual(d.remaining_credits, 0)
        self.assertEqual(d.run_budget, 0)

    def test_past_reset_date_never_bursts(self):
        # Gate-A BUG 2: a stale/past reset must spread over 30d, not explode to the ceiling.
        d = G.decide(_inp(provider_jobs_remaining=20000,
                          cycle_reset_at=datetime(2026, 9, 1, tzinfo=timezone.utc)))
        self.assertLess(d.run_budget, 1000)
        self.assertEqual(d.days_remaining, 30.0)

    def test_last_day_of_cycle_bounded_to_one_day_pace(self):
        d = G.decide(_inp(provider_jobs_remaining=20000,
                          now=datetime(2026, 10, 16, 23, 0, tzinfo=timezone.utc)))
        # spendable 18000 over >=1 day => at most 18000, clamped by ceiling 6000
        self.assertLessEqual(d.run_budget, 6000)
        self.assertGreaterEqual(d.days_remaining, 1.0)

    def test_carry_forward_bounded(self):
        base = G.decide(_inp(provider_jobs_remaining=20000)).base_daily_allowance
        d = G.decide(_inp(provider_jobs_remaining=20000, carry_forward=10_000_000))
        self.assertLessEqual(d.carry_forward_applied, base * 3)

    def test_inventory_hint_caps_grant(self):
        d = G.decide(_inp(provider_jobs_remaining=20000, inventory_hint=50))
        self.assertEqual(d.run_budget, 50)
        self.assertTrue(d.inventory_capped)

    def test_same_day_prior_spend_reduces_grant(self):
        full = G.decide(_inp(provider_jobs_remaining=20000)).run_budget
        d = G.decide(_inp(provider_jobs_remaining=20000, spent_today=full))
        self.assertEqual(d.run_budget, 0)
        self.assertEqual(d.reason, "daily_allowance_spent")

    def test_daily_max_clamps(self):
        d = G.decide(_inp(provider_jobs_remaining=20000, daily_max_jobs=200))
        self.assertEqual(d.run_budget, 200)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "ledger.json")

    def test_rollover_on_reset_date_change(self):
        L = G.GovernorLedger(self.path)
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        L.ensure_cycle(datetime(2026, 9, 17, tzinfo=timezone.utc), now)
        L.record_run("r1", 500, 600, now)
        L.save()
        L2 = G.GovernorLedger(self.path)
        self.assertEqual(L2.used, 500)
        rolled = L2.ensure_cycle(datetime(2026, 10, 17, tzinfo=timezone.utc),
                                 datetime(2026, 9, 18, tzinfo=timezone.utc))
        self.assertTrue(rolled)
        self.assertEqual(L2.used, 0)

    def test_idempotent_per_run_id(self):
        L = G.GovernorLedger(self.path)
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        L.ensure_cycle(datetime(2026, 9, 17, tzinfo=timezone.utc), now)
        L.record_run("dup", 300, 300, now)
        L.record_run("dup", 300, 300, now)
        self.assertEqual(L.used, 300)

    def test_manual_plus_cron_same_day_share_allowance(self):
        L = G.GovernorLedger(self.path)
        now = datetime(2026, 9, 1, 13, tzinfo=timezone.utc)
        L.ensure_cycle(datetime(2026, 9, 17, tzinfo=timezone.utc), now)
        L.record_run("manual", 400, 600, now)
        self.assertEqual(L.spent_on_day("2026-09-01"), 400)
        L.record_run("cron", 200, 600, now + timedelta(hours=3))
        self.assertEqual(L.spent_on_day("2026-09-01"), 600)

    def test_carry_forward_accrues_once_per_day(self):
        L = G.GovernorLedger(self.path)
        d1 = datetime(2026, 9, 1, 13, tzinfo=timezone.utc)
        L.ensure_cycle(datetime(2026, 9, 17, tzinfo=timezone.utc), d1)
        L.accrue_carry_forward(600, d1)      # first day: nothing to carry yet
        L.record_run("a", 100, 600, d1)
        d2 = d1 + timedelta(days=1)
        L.accrue_carry_forward(600, d2)      # yesterday unused 500
        self.assertEqual(L.carry_forward, 500)
        L.accrue_carry_forward(600, d2)      # same day again: no double accrual
        self.assertEqual(L.carry_forward, 500)


class BuildContextTests(unittest.TestCase):
    def _cfg(self, path, **over):
        base = dict(FANTASTIC_MONTHLY_GOVERNOR_ENABLED=True, FANTASTIC_MONTHLY_JOBS_LIMIT=20000,
                    FANTASTIC_MONTHLY_RESERVE_PCT=0.10, FANTASTIC_DAILY_MIN_JOBS=100,
                    FANTASTIC_DAILY_MAX_JOBS=0, FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS=True,
                    FANTASTIC_GOVERNOR_USE_COUNT_HINT=False, FANTASTIC_GOVERNOR_CARRY_CAP_DAYS=3.0,
                    FANTASTIC_BILLING_RESET_AT="", FANTASTIC_GOVERNOR_LEDGER_PATH=path,
                    FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000, FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=500)
        base.update(over)
        return SimpleNamespace(**base)

    def test_disabled_returns_no_budget(self):
        ctx = G.build_context(self._cfg("", FANTASTIC_MONTHLY_GOVERNOR_ENABLED=False), run_id="x")
        self.assertFalse(ctx.enabled)
        self.assertIsNone(ctx.run_budget)

    def test_end_to_end_commit_persists_and_reduces_next_grant(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "l.json")
        cfg = self._cfg(path)
        now = datetime(2026, 9, 20, 13, tzinfo=timezone.utc)
        reset = datetime(2026, 10, 17, tzinfo=timezone.utc)
        ctx = G.build_context(cfg, run_id="r1", provider_jobs_remaining=20000,
                              provider_reset_at=reset, now=now)
        first = ctx.run_budget
        self.assertGreater(first, 0)
        G.commit_run(ctx, run_id="r1", billed=first, now=now)
        ctx2 = G.build_context(cfg, run_id="r2", provider_jobs_remaining=20000 - first,
                               provider_reset_at=reset, now=now + timedelta(hours=1))
        self.assertEqual(ctx2.run_budget, 0)   # today's allowance already spent
        self.assertEqual(ctx2.decision.reason, "daily_allowance_spent")


if __name__ == "__main__":
    unittest.main()
