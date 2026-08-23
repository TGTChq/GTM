"""Governor auto-arm: mid-cycle enable must not strand the running cycle, and the
next billing reset must take control with ZERO human action."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from orchestrator import fantastic_governor as G

CYCLE_A = datetime(2026, 9, 17, tzinfo=timezone.utc)
CYCLE_B = datetime(2026, 10, 17, tzinfo=timezone.utc)


def _cfg(path, **over):
    b = dict(FANTASTIC_MONTHLY_GOVERNOR_ENABLED=True, FANTASTIC_MONTHLY_JOBS_LIMIT=20000,
             FANTASTIC_MONTHLY_RESERVE_PCT=0.10, FANTASTIC_DAILY_MIN_JOBS=100,
             FANTASTIC_DAILY_MAX_JOBS=0, FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS=True,
             FANTASTIC_GOVERNOR_USE_COUNT_HINT=False, FANTASTIC_GOVERNOR_CARRY_CAP_DAYS=3.0,
             FANTASTIC_GOVERNOR_AUTO_ARM=True, FANTASTIC_BILLING_RESET_AT="",
             FANTASTIC_GOVERNOR_LEDGER_PATH=path, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=12000,
             FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=500)
    b.update(over)
    return SimpleNamespace(**b)


class AutoArmTransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "ledger.json")

    def test_mid_cycle_enable_does_not_strand_the_running_cycle(self):
        """The exact production situation: 336 credits left, reserve 2000, floor 500.
        Armed it would grant 0; unarmed it must impose NO cap so the drain continues."""
        ctx = G.build_context(_cfg(self.path), run_id="r1", provider_jobs_remaining=336,
                              provider_reset_at=CYCLE_A,
                              now=datetime(2026, 8, 23, 13, tzinfo=timezone.utc))
        self.assertTrue(ctx.enabled)
        self.assertFalse(ctx.armed)
        self.assertEqual(ctx.arm_state, "pre_arm_current_cycle_drains")
        self.assertIsNone(ctx.run_budget)              # NO cap -> legacy drain
        self.assertEqual(ctx.decision.run_budget, 0)   # what it WOULD have granted

    def test_arms_automatically_on_the_next_billing_cycle(self):
        cfg = _cfg(self.path)
        G.build_context(cfg, run_id="r1", provider_jobs_remaining=336,
                        provider_reset_at=CYCLE_A,
                        now=datetime(2026, 8, 23, 13, tzinfo=timezone.utc))
        # ... cycle rolls over; provider reports a fresh 20k and a new reset date.
        ctx = G.build_context(cfg, run_id="r2", provider_jobs_remaining=20000,
                              provider_reset_at=CYCLE_B,
                              now=datetime(2026, 9, 18, 13, tzinfo=timezone.utc))
        self.assertTrue(ctx.armed)
        self.assertEqual(ctx.arm_state, "armed_on_cycle_rollover")
        self.assertIsNotNone(ctx.run_budget)
        self.assertGreater(ctx.run_budget, 0)
        self.assertLess(ctx.run_budget, 2000)          # paced, never a burst

    def test_stays_armed_permanently_after_first_arming(self):
        cfg = _cfg(self.path)
        G.build_context(cfg, run_id="r1", provider_jobs_remaining=336,
                        provider_reset_at=CYCLE_A, now=datetime(2026, 8, 23, tzinfo=timezone.utc))
        G.build_context(cfg, run_id="r2", provider_jobs_remaining=20000,
                        provider_reset_at=CYCLE_B, now=datetime(2026, 9, 18, tzinfo=timezone.utc))
        ctx = G.build_context(cfg, run_id="r3", provider_jobs_remaining=15000,
                              provider_reset_at=CYCLE_B, now=datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.assertTrue(ctx.armed)
        self.assertEqual(ctx.arm_state, "already_armed")

    def test_arm_state_survives_the_cycle_rollover_reset(self):
        cfg = _cfg(self.path)
        G.build_context(cfg, run_id="r1", provider_jobs_remaining=336, provider_reset_at=CYCLE_A,
                        now=datetime(2026, 8, 23, tzinfo=timezone.utc))
        G.build_context(cfg, run_id="r2", provider_jobs_remaining=20000, provider_reset_at=CYCLE_B,
                        now=datetime(2026, 9, 18, tzinfo=timezone.utc))
        st = json.load(open(self.path))
        self.assertTrue(st["armed"])                    # carried across the reset
        self.assertEqual(st["used"], 0)                 # but spend counters DID reset
        self.assertEqual(st["arm_pending_cycle_key"], "2026-09-17")

    def test_corrupt_or_missing_arm_state_fails_ARMED(self):
        """Bounded spend is the conservative failure; failing open risks the plan."""
        with open(self.path, "w") as fh:
            json.dump({"schema": G.LEDGER_SCHEMA, "cycle_key": "2026-10-17",
                       "cycle_reset_at": CYCLE_B.isoformat(), "used": 0, "runs": [],
                       "carry_forward": 0, "last_allowance_day": "",
                       "arm_pending_cycle_key": "2026-09-17"}, fh)
        ctx = G.build_context(_cfg(self.path), run_id="r", provider_jobs_remaining=20000,
                              provider_reset_at=CYCLE_B,
                              now=datetime(2026, 9, 18, tzinfo=timezone.utc))
        self.assertTrue(ctx.armed)

    def test_restart_does_not_reset_the_monthly_spend_ledger(self):
        cfg = _cfg(self.path)
        now = datetime(2026, 9, 18, 13, tzinfo=timezone.utc)
        G.build_context(cfg, run_id="r1", provider_jobs_remaining=336, provider_reset_at=CYCLE_A,
                        now=datetime(2026, 8, 23, tzinfo=timezone.utc))
        ctx = G.build_context(cfg, run_id="r2", provider_jobs_remaining=20000,
                              provider_reset_at=CYCLE_B, now=now)
        G.commit_run(ctx, run_id="r2", billed=500, now=now)
        # Simulate a container restart: fresh objects, same file.
        ctx2 = G.build_context(cfg, run_id="r3", provider_jobs_remaining=19500,
                               provider_reset_at=CYCLE_B, now=now + timedelta(hours=2))
        self.assertEqual(ctx2.ledger.used, 500)         # spend survived the restart
        self.assertTrue(ctx2.armed)

    def test_auto_arm_disabled_arms_immediately(self):
        ctx = G.build_context(_cfg(self.path, FANTASTIC_GOVERNOR_AUTO_ARM=False),
                              run_id="r", provider_jobs_remaining=336, provider_reset_at=CYCLE_A,
                              now=datetime(2026, 8, 23, tzinfo=timezone.utc))
        self.assertTrue(ctx.armed)
        self.assertEqual(ctx.run_budget, 0)             # would strand -- opt-in only

    def test_flag_off_is_unchanged(self):
        ctx = G.build_context(_cfg(self.path, FANTASTIC_MONTHLY_GOVERNOR_ENABLED=False),
                              run_id="r")
        self.assertFalse(ctx.enabled)
        self.assertIsNone(ctx.run_budget)

    def test_twenty_k_bound_remains_authoritative_once_armed(self):
        cfg = _cfg(self.path)
        G.build_context(cfg, run_id="r1", provider_jobs_remaining=336, provider_reset_at=CYCLE_A,
                        now=datetime(2026, 8, 23, tzinfo=timezone.utc))
        ctx = G.build_context(cfg, run_id="r2", provider_jobs_remaining=20000,
                              provider_reset_at=CYCLE_B, now=datetime(2026, 9, 18, tzinfo=timezone.utc))
        self.assertLessEqual(ctx.run_budget, ctx.decision.spendable_credits)
        self.assertEqual(ctx.decision.reserve_credits, 2000)


if __name__ == "__main__":
    unittest.main()
