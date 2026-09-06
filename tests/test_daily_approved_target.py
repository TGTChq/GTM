"""The output target is distinct NEW approved leads per business day.

Not per run, not postings reviewed, not Apollo calls, not leads attempted -- every one
of those is an input and every one has at some point been mistaken for the output.

Three properties carry the definition, and each is a test below: the count is DISTINCT
and durable across the several runs a day may take; it counts only leads NEW to the
system, so recycling the backlog cannot reach the number; and a "day" is a business
day in the reporting timezone, because a UTC day rolls over mid-afternoon Pacific and
splits a working day in two.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from orchestrator import daily_target as dt


class TheCountIsDistinctAndDurable(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_two_runs_on_one_day_accumulate(self):
        dt.record_approved(self.root, ["a@x.com", "b@x.com"])
        dt.record_approved(self.root, ["c@x.com"])
        self.assertEqual(dt.approved_today(self.root), 3)

    def test_the_same_lead_twice_is_one_lead(self):
        dt.record_approved(self.root, ["a@x.com"])
        out = dt.record_approved(self.root, ["a@x.com", "b@x.com"])
        self.assertEqual(out["added"], 1)
        self.assertEqual(dt.approved_today(self.root), 2)

    def test_case_and_whitespace_do_not_create_a_second_lead(self):
        dt.record_approved(self.root, ["A@X.com"])
        out = dt.record_approved(self.root, [" a@x.com "])
        self.assertEqual(out["added"], 0)

    def test_empty_keys_are_ignored(self):
        out = dt.record_approved(self.root, ["", None, "  ", "a@x.com"])
        self.assertEqual(out["added"], 1)


class OnlyNEWLeadsCount(unittest.TestCase):
    def test_a_lead_approved_on_an_earlier_day_is_not_todays_output(self):
        """Recycling the backlog is the one way to hit the target while delivering
        nothing, so it must be arithmetically impossible."""
        root = tempfile.mkdtemp()
        yesterday = datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc)
        today = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
        dt.record_approved(root, ["a@x.com", "b@x.com"], now=yesterday)
        out = dt.record_approved(root, ["a@x.com", "b@x.com", "c@x.com"], now=today)
        self.assertEqual(out["added"], 1)
        self.assertEqual(dt.approved_today(root, now=today), 1)

    def test_each_day_keeps_its_own_total(self):
        root = tempfile.mkdtemp()
        d1 = datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc)
        d2 = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
        dt.record_approved(root, ["a@x.com", "b@x.com"], now=d1)
        dt.record_approved(root, ["c@x.com"], now=d2)
        self.assertEqual(dt.approved_today(root, now=d1), 2)
        self.assertEqual(dt.approved_today(root, now=d2), 1)


class ADayIsABusinessDayInTheReportingTimezone(unittest.TestCase):
    def test_a_utc_evening_is_still_the_same_pacific_day(self):
        """2026-09-06T23:00Z is 16:00 Pacific -- the same working day, not the next
        one. Counting in UTC would roll the target over mid-afternoon."""
        afternoon = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)
        evening = datetime(2026, 9, 6, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(dt.business_day(afternoon), dt.business_day(evening))
        self.assertEqual(dt.business_day(evening), "2026-09-06")

    def test_after_pacific_midnight_it_is_the_next_day(self):
        self.assertEqual(dt.business_day(datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)),
                         "2026-09-07")


class TheGoalCarriesTheReserve(unittest.TestCase):
    """Inventory varies -- the measured weekend window held a fifth of a weekday's
    postings -- so a system that produces exactly 1,000 on its best day produces far
    fewer on its worst. A strong day banks the shortfall; a weak day draws it down."""

    def test_a_full_reserve_asks_for_the_target_only(self):
        root = tempfile.mkdtemp()
        goal = dt.goal_for_today(root, target=1000, reserve_floor=500,
                                 reserve_on_hand=500)
        self.assertEqual(goal["goal_today"], 1000)
        self.assertEqual(goal["remaining"], 1000)

    def test_a_short_reserve_asks_the_day_to_bank_the_difference(self):
        root = tempfile.mkdtemp()
        goal = dt.goal_for_today(root, target=1000, reserve_floor=500,
                                 reserve_on_hand=200)
        self.assertEqual(goal["reserve_shortfall"], 300)
        self.assertEqual(goal["goal_today"], 1300)

    def test_an_overfull_reserve_never_reduces_the_target(self):
        """The reserve covers weak days; it does not license a slow one."""
        root = tempfile.mkdtemp()
        goal = dt.goal_for_today(root, target=1000, reserve_floor=500,
                                 reserve_on_hand=5000)
        self.assertEqual(goal["goal_today"], 1000)

    def test_work_already_done_today_counts_toward_the_goal(self):
        root = tempfile.mkdtemp()
        dt.record_approved(root, [f"lead{i}@x.com" for i in range(40)])
        goal = dt.goal_for_today(root, target=100, reserve_floor=0, reserve_on_hand=0)
        self.assertEqual(goal["approved_today"], 40)
        self.assertEqual(goal["remaining"], 60)
        self.assertFalse(goal["met"])

    def test_met_is_true_only_when_the_whole_goal_including_reserve_is_reached(self):
        root = tempfile.mkdtemp()
        dt.record_approved(root, [f"l{i}@x.com" for i in range(120)])
        self.assertFalse(dt.goal_for_today(root, target=100, reserve_floor=50,
                                           reserve_on_hand=0)["met"])
        self.assertTrue(dt.goal_for_today(root, target=100, reserve_floor=50,
                                          reserve_on_hand=50)["met"])


class TheStoreStaysBounded(unittest.TestCase):
    def test_old_days_are_trimmed_but_recent_keys_survive(self):
        """Trimmed by DAY, never by size: dropping a recent key would let a lead be
        counted twice, which is the one error this store exists to prevent."""
        root = tempfile.mkdtemp()
        old = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
        now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
        dt.record_approved(root, ["ancient@x.com"], now=old)
        dt.record_approved(root, ["fresh@x.com"], now=now)
        days = {row["day"] for row in dt.history(root, days=99)}
        self.assertNotIn("2026-06-01", days)
        # The ancient key is gone, so it would count again -- correct after 45 days.
        self.assertEqual(dt.record_approved(root, ["fresh@x.com"], now=now)["added"], 0)


if __name__ == "__main__":
    unittest.main()
