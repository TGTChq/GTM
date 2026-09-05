"""A persisted offset addresses a POSITION. Does it still address the same rows?

#87 proved the feed intersects ``time_frame`` with ``date_created``, so the floor
under every window rises with the clock. #83 made the per-source offset durable so a
truncated window resumes instead of re-paging its head. Those two facts have to be
checked against each other, and storing an offset correctly is not that check:
resuming at offset N proves the CURSOR persisted, not that row N is the row it was.

What decides it is the order the provider returns rows in. Nothing in the request
asks for one -- ``build_jb_params`` sends no ``order_by`` or ``sort`` -- and the
provider documents none, so it is theirs to choose and ours to observe.

These are OFFLINE replays against a modelled feed. No provider request is made and
no credit is spent. Each models the same real sequence: a window is paged part-way,
a day passes, rows age out under the rising floor, late rows become visible inside
the fixed window (the reason ``FANTASTIC_DATE_CREATED_LAG_MINUTES`` exists), and the
next run resumes at the saved offset.

The question each asks is the only one that matters: after resuming, was any row that
was never inspected stepped over?
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

PAGE = 100


def _rows(n, *, start, step_minutes=10):
    """A window's inventory, oldest first, each row carrying its date_created."""
    return [{"id": f"j{i:05d}",
             "date_created": (start + timedelta(minutes=i * step_minutes))
             .strftime("%Y-%m-%dT%H:%M:%SZ")}
            for i in range(n)]


class _Feed:
    """A modelled provider: a fixed date window intersected with a moving floor.

    ``order`` is the only thing that varies, because it is the only thing in doubt.
    """

    def __init__(self, inventory, *, order):
        self.inventory = list(inventory)
        self.order = order

    def visible(self, floor):
        rows = [r for r in self.inventory if r["date_created"] >= floor]
        rows.sort(key=lambda r: r["date_created"], reverse=(self.order == "desc"))
        return rows

    def page(self, floor, offset, limit=PAGE):
        return self.visible(floor)[offset:offset + limit]


def _run(feed, floor, offset, pages=1):
    """One run: page from the saved offset, return (rows seen, new offset)."""
    seen = []
    for _ in range(pages):
        got = feed.page(floor, offset + len(seen))
        if not got:
            break
        seen.extend(got)
    return seen, offset + len(seen)


class OffsetContinuityTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 8, 29, tzinfo=timezone.utc)
        # 500 rows across ~3.5 days, the shape of a real multi-day window.
        self.inventory = _rows(500, start=self.start)
        self.day1 = "2026-08-29T00:00:00Z"
        # A day later the floor has risen a day: 144 rows at 10-minute spacing.
        self.day2 = "2026-08-30T00:00:00Z"

    def _expired(self):
        return [r for r in self.inventory
                if self.day1 <= r["date_created"] < self.day2]

    def test_the_model_is_the_situation_we_are_actually_in(self):
        """Guard the fixture: a real day of inventory must leave the set."""
        self.assertEqual(len(self._expired()), 144)
        self.assertEqual(len(self.inventory), 500)

    def test_oldest_first_ordering_steps_over_rows_nobody_inspected(self):
        """THE UNSAFE CASE. Rows leave the HEAD, so everything after them shifts down
        by the number that left, and an offset saved yesterday lands that far past
        where it was pointing."""
        feed = _Feed(self.inventory, order="asc")

        day1_seen, offset = _run(feed, self.day1, 0, pages=2)
        self.assertEqual(offset, 200)

        day2_seen, _ = _run(feed, self.day2, offset, pages=2)

        inspected = {r["id"] for r in day1_seen} | {r["id"] for r in day2_seen}
        still_reachable = {r["id"] for r in feed.visible(self.day2)}
        skipped = still_reachable - inspected

        self.assertEqual(len(skipped), 144, "exactly the rows the floor shifted past")
        self.assertTrue(skipped.isdisjoint({r["id"] for r in self._expired()}),
                        "these are not the expired rows -- they are still reachable")

    def test_newest_first_ordering_keeps_the_consumed_prefix_meaningful(self):
        """THE SAFE CASE. Rows leave the TAIL, which is inventory the offset had not
        reached, so the prefix already consumed still sits where it was."""
        feed = _Feed(self.inventory, order="desc")

        day1_seen, offset = _run(feed, self.day1, 0, pages=2)
        day2_seen, _ = _run(feed, self.day2, offset, pages=3)

        inspected = {r["id"] for r in day1_seen} | {r["id"] for r in day2_seen}
        skipped = {r["id"] for r in feed.visible(self.day2)} - inspected
        self.assertEqual(skipped, set(), "a newest-first feed loses nothing to the floor")

    def test_a_late_arrival_inside_the_window_is_missed_under_either_ordering(self):
        """Rows become visible INSIDE a fixed window after we paged past their
        position -- the provider's own visibility lag, which is why the engine holds
        a lag buffer at all. An offset cannot see behind itself, so a row inserted
        into the consumed prefix is never inspected. This is a separate hazard from
        the moving floor and it does not depend on the ordering."""
        for order in ("asc", "desc"):
            with self.subTest(order=order):
                feed = _Feed(self.inventory, order=order)
                day1_seen, offset = _run(feed, self.day1, 0, pages=2)

                late = {"id": "late-1",
                        "date_created": day1_seen[10]["date_created"]}
                feed.inventory.append(late)

                day2_seen, _ = _run(feed, self.day1, offset, pages=5)
                inspected = {r["id"] for r in day1_seen} | {r["id"] for r in day2_seen}
                self.assertNotIn(
                    "late-1", inspected,
                    "a row that appears inside the consumed prefix is never revisited")

    def test_an_empty_page_after_a_resume_is_not_proof_of_coverage(self):
        """The consequence that makes the unsafe case permanent rather than merely
        wasteful: after the offset over-shoots, paging reaches the end of the
        SHRUNKEN set and returns nothing. ``empty_page`` is a drained stop, so the
        window commits and the watermark advances past rows no request ever saw."""
        feed = _Feed(self.inventory, order="asc")
        _day1, offset = _run(feed, self.day1, 0, pages=2)

        # Jump the floor far enough that the saved offset lands beyond the tail.
        far = "2026-09-01T00:00:00Z"
        remaining = feed.visible(far)
        self.assertTrue(offset > len(remaining), "the offset now points past the set")

        got = feed.page(far, offset)
        self.assertEqual(got, [], "an empty page")
        self.assertTrue(remaining, "...while inventory is still there, uninspected")


if __name__ == "__main__":
    unittest.main()
