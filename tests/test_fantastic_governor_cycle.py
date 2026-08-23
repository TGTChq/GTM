"""Whole-billing-cycle governor simulation (deployment-blocking invariants).

Invariants asserted for every scenario:
  I1  SUM(billed) over the cycle  <=  spendable  (= limit - reserve)   [reserve never spent by pacing]
      NOTE: the sum of OFFERED grants is not the right quantity -- on thin days an
      unused grant is re-offered via (capped) carry-forward, so offered grants
      legitimately double-count the same allowance. What leaves the budget is billed.
  I2  under consistently abundant inventory, the governor does NOT exhaust the usable
      allocation substantially before the reset (>= 60% of the cycle elapses before
      90% of spendable is granted; spending is spread, never front-loaded)
  I3  a cycle's FIRST grant carries zero carry-forward (no cross-cycle leak)
  I4  no single grant exceeds carry_cap_days x base + base (bounded catch-up)
  I5  same-day manual+cron share one allowance
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from orchestrator import fantastic_governor as G

LIMIT = 20000
RESERVE = 0.10
SPENDABLE = int(LIMIT * (1 - RESERVE))


def _cfg(path, **over):
    b = dict(FANTASTIC_MONTHLY_GOVERNOR_ENABLED=True, FANTASTIC_MONTHLY_JOBS_LIMIT=LIMIT,
             FANTASTIC_MONTHLY_RESERVE_PCT=RESERVE, FANTASTIC_DAILY_MIN_JOBS=100,
             FANTASTIC_DAILY_MAX_JOBS=0, FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS=True,
             FANTASTIC_GOVERNOR_USE_COUNT_HINT=False, FANTASTIC_GOVERNOR_CARRY_CAP_DAYS=3.0,
             # This suite exercises GOVERNED pacing, not the mid-cycle arming
             # transition (that is test_governor_auto_arm.py), so arm immediately.
             FANTASTIC_GOVERNOR_AUTO_ARM=False,
             FANTASTIC_BILLING_RESET_AT="", FANTASTIC_GOVERNOR_LEDGER_PATH=path,
             FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000, FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=500)
    b.update(over)
    return SimpleNamespace(**b)


class _Provider:
    """Authoritative provider counter: bills what the run actually consumes."""
    def __init__(self, remaining=LIMIT):
        self.remaining = remaining

    def bill(self, n):
        self.remaining -= n


def simulate(days, *, inventory, runs_per_day=lambda d: 1, skip_days=(), start=None):
    """Run `days` daily cron cycles. inventory(d) = jobs available that day.
    Returns per-run records and the provider's final remaining."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "ledger.json")
    start = start or datetime(2026, 9, 18, 13, tzinfo=timezone.utc)
    reset = start + timedelta(days=days)
    prov = _Provider()
    recs = []
    for d in range(days):
        if d in skip_days:
            continue
        for r in range(runs_per_day(d)):
            now = start + timedelta(days=d, hours=3 * r)
            ctx = G.build_context(_cfg(path), run_id=f"d{d}r{r}", provider_jobs_remaining=prov.remaining,
                                  provider_reset_at=reset, now=now)
            grant = ctx.run_budget
            billed = min(grant, inventory(d))
            prov.bill(billed)
            G.commit_run(ctx, run_id=f"d{d}r{r}", billed=billed, now=now)
            recs.append({"day": d, "run": r, "grant": grant, "billed": billed,
                         "carry": ctx.decision.carry_forward_applied, "base": ctx.decision.base_daily_allowance,
                         "reason": ctx.decision.reason})
    return recs, prov, path, reset


class FullCycleInvariants(unittest.TestCase):
    def _assert_core(self, recs, days, label):
        billed = sum(r["billed"] for r in recs)
        self.assertLessEqual(billed, SPENDABLE, f"{label}: I1 sum(billed)={billed} > spendable {SPENDABLE}")
        # Under abundant inventory every grant is fully billed, so the OFFERED total
        # is bounded too (no re-offer of the same allowance).
        if all(r["billed"] == r["grant"] for r in recs):
            self.assertLessEqual(sum(r["grant"] for r in recs), SPENDABLE, f"{label}: I1 offered")
        # I3: the very first grant of the cycle has zero carry.
        self.assertEqual(recs[0]["carry"], 0, f"{label}: I3 first grant carried {recs[0]['carry']}")
        # I4: bounded catch-up.
        for r in recs:
            self.assertLessEqual(r["grant"], r["base"] * 4 + 1, f"{label}: I4 burst grant {r}")

    def _assert_spread(self, recs, days, label):
        # I2: under abundant inventory, 90% of spendable must not be granted before
        # 60% of the cycle has elapsed.
        cum = 0
        for r in recs:
            cum += r["grant"]
            if cum >= 0.9 * SPENDABLE:
                frac = r["day"] / days
                self.assertGreaterEqual(frac, 0.60, f"{label}: I2 90% granted by day {r['day']}/{days}")
                break

    def test_26_day_cycle_abundant(self):
        recs, prov, _, _ = simulate(26, inventory=lambda d: 10_000)
        self._assert_core(recs, 26, "26d"); self._assert_spread(recs, 26, "26d")
        self.assertGreaterEqual(prov.remaining, LIMIT * RESERVE - 1)   # reserve intact

    def test_28_day_cycle_abundant(self):
        recs, prov, _, _ = simulate(28, inventory=lambda d: 10_000)
        self._assert_core(recs, 28, "28d"); self._assert_spread(recs, 28, "28d")
        # Day-1 grant equals base pace (~643), NOT double (the 1264 defect).
        self.assertLessEqual(recs[0]["grant"], int(SPENDABLE / 28) + 1)
        self.assertGreaterEqual(recs[0]["grant"], int(SPENDABLE / 29))
        # Uses most of the spendable budget by the reset (not starved).
        self.assertGreaterEqual(sum(r["billed"] for r in recs), 0.9 * SPENDABLE)

    def test_30_day_cycle_abundant(self):
        recs, prov, _, _ = simulate(30, inventory=lambda d: 10_000)
        self._assert_core(recs, 30, "30d"); self._assert_spread(recs, 30, "30d")

    def test_low_inventory_then_catch_up_is_bounded(self):
        # 10 thin days (50 jobs) then abundance: unused pace carries, capped at 3 days.
        recs, prov, _, _ = simulate(28, inventory=lambda d: 50 if d < 10 else 10_000)
        self._assert_core(recs, 28, "catchup")
        first_rich = next(r for r in recs if r["day"] == 10)
        self.assertGreater(first_rich["grant"], first_rich["base"])            # catch-up happened
        self.assertLessEqual(first_rich["grant"], first_rich["base"] * 4)      # but bounded (3d carry + base)
        # Thin days preserved budget: exactly the spendable amount was billed over
        # the cycle and the 10% reserve is untouched at the reset.
        self.assertEqual(sum(r["billed"] for r in recs), SPENDABLE)
        self.assertEqual(prov.remaining, LIMIT - SPENDABLE)
        # After catch-up the pace re-equilibrates (carry consumed, no runaway).
        self.assertEqual(next(r for r in recs if r["day"] == 11)["carry"], 0)

    def test_missed_cron_days_do_not_burst(self):
        recs, prov, _, _ = simulate(28, inventory=lambda d: 10_000, skip_days=(5, 6, 7, 8, 9, 10, 11))
        self._assert_core(recs, 28, "missed")
        after_gap = next(r for r in recs if r["day"] == 12)
        self.assertLessEqual(after_gap["grant"], after_gap["base"] * 4)        # 7 idle days -> <= 3d carry

    def test_manual_plus_cron_same_day_share_allowance(self):
        recs, prov, _, _ = simulate(28, inventory=lambda d: 10_000, runs_per_day=lambda d: 2)
        self._assert_core(recs, 28, "2/day")
        day0 = [r for r in recs if r["day"] == 0]
        self.assertEqual(day0[1]["grant"], 0)
        self.assertEqual(day0[1]["reason"], "daily_allowance_spent")
        self.assertLessEqual(sum(r["grant"] for r in day0), day0[0]["base"] + 1)

    def test_reset_transition_clears_carry_and_ledger(self):
        recs, prov, path, reset = simulate(28, inventory=lambda d: 50)       # thin cycle -> carry accrues
        led = G.GovernorLedger(path)
        self.assertGreater(led.carry_forward, 0)
        # First run of the NEW cycle: provider reset to 20000, new reset date.
        new_reset = reset + timedelta(days=30)
        ctx = G.build_context(_cfg(path), run_id="new1", provider_jobs_remaining=LIMIT,
                              provider_reset_at=new_reset, now=reset + timedelta(hours=13))
        self.assertTrue(ctx.cycle_rolled)
        self.assertEqual(ctx.decision.carry_forward_applied, 0)                # I3: no cross-cycle leak
        self.assertEqual(ctx.ledger.used, 0)
        self.assertLessEqual(ctx.decision.run_budget, int(SPENDABLE / 29) + 1)

    def test_carry_cap_enforced_by_ledger(self):
        tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "l.json")
        L = G.GovernorLedger(path)
        d0 = datetime(2026, 9, 18, 13, tzinfo=timezone.utc)
        L.ensure_cycle(d0 + timedelta(days=28), d0)
        L.accrue_carry_forward(600, d0, cap_days=3.0)      # first day: zero
        self.assertEqual(L.carry_forward, 0)
        L.accrue_carry_forward(600, d0 + timedelta(days=20), cap_days=3.0)   # 20 idle days
        self.assertEqual(L.carry_forward, 1800)            # capped at 3 x 600, not 12000


if __name__ == "__main__":
    unittest.main()
