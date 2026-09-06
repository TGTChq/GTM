"""A `date_created` slice cursor, measured against the offset cursor it replaces.

WHY THE CURSOR SHAPE CHANGED. The provider documents offset paging for draining a
result set in ONE pass -- "keep making requests until the API returns less jobs than
the limit". Nothing in the documentation says an index still addresses the same row
on a later day, and the rising 7-day frame floor guarantees it does not: rows leave
the set by `date_created` while pages are returned in some other order, so a resumed
offset both re-reads rows we hold and steps over rows nobody inspected.

A `date_created` boundary has no such problem. A row's `date_created` never changes,
so a drained slice is drained forever, a slice never revisits another slice's rows,
and the only way a row escapes is the whole slice dropping below the floor -- at
which point it was unreachable by any cursor.

MEASURED, on the same moving-floor replay that exposed the loss (12 runs, 420 rows
entering the frame, `MovingFloorFeed` with `date_posted` deliberately unaligned to
`date_created`):

    cursor   cap  acquired  expired  billed  useful
    offset   120       315      105     480   65.6%
    slice    120       360       60     360   100.0%
    offset   180       365       55     540   67.6%
    slice    180       420        0     420   100.0%
    offset   240       376       44     600   62.7%
    slice    240       420        0     420   100.0%

Every row the slice cursor buys is a row it did not have. At a budget that can keep
up it reaches the whole window, loses nothing to expiry, and spends 30% less doing
it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from tests.test_continuation_multirun_replay import (BASE, LABEL, MovingFloorFeed,
                                                     _run)


def _sweep(sliced, *, days=12, cap=120):
    state = os.path.join(tempfile.mkdtemp(), "wm.json")
    feed = MovingFloorFeed()
    seen: set = set()
    acquired = expired = billed = 0
    prev = None
    for day in range(days):
        before = set(seen)
        _e, metrics, _ = _run(state, feed, BASE + timedelta(days=day),
                              cap=cap, seen=seen,
                              FANTASTIC_WINDOW_SLICING_ENABLED=sliced)
        frame = {f"fantastic_{r['id']}" for r in feed.visible()}
        acquired += len(seen - before)
        billed += metrics["segments"][LABEL].get("returned", 0)
        if prev is not None:
            expired += len((prev - frame) - before)
        prev = frame
    return {"acquired": acquired, "expired": expired, "billed": billed,
            "pending": len(prev - seen), "state": state}


class TheSliceCursorBuysOnlyWhatItDoesNotHave(unittest.TestCase):
    def test_every_billed_row_is_a_row_we_did_not_have(self):
        out = _sweep(True, cap=240)
        self.assertEqual(out["acquired"], out["billed"],
                         "a slice is queried once, so nothing is re-bought")

    def test_the_offset_cursor_spends_a_third_of_its_budget_on_rows_it_holds(self):
        out = _sweep(False, cap=240)
        self.assertLess(out["acquired"], out["billed"])
        self.assertLess(out["acquired"] / out["billed"], 0.75)

    def test_at_an_adequate_budget_nothing_expires_unacquired(self):
        out = _sweep(True, cap=240)
        self.assertEqual(out["expired"], 0)
        self.assertEqual(out["pending"], 0)

    def test_the_offset_cursor_loses_rows_at_the_same_budget(self):
        out = _sweep(False, cap=240)
        self.assertGreater(out["expired"], 0,
                           "this is the loss the slice cursor removes")

    def test_it_is_better_on_both_axes_even_when_the_budget_is_starved(self):
        lean, rich = _sweep(True, cap=120), _sweep(False, cap=120)
        self.assertGreater(lean["acquired"], rich["acquired"], "more inventory")
        self.assertLess(lean["billed"], rich["billed"], "for less money")


class SliceStateIsStableWhereAnOffsetIsNot(unittest.TestCase):
    def _state(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_what_persists_is_which_slices_are_finished(self):
        out = _sweep(True, cap=240)
        state = self._state(out["state"])
        done = (state.get("window_slices") or {}).get(LABEL) or []
        self.assertTrue(done, "the cursor is a set of drained date ranges")
        for key in done:
            lo, _, hi = key.partition("|")
            self.assertTrue(lo < hi, f"a slice is a half-open date range: {key}")

    def test_an_unfinished_slice_resumes_instead_of_restarting(self):
        """Without this a starved budget re-buys the same prefix every run and never
        finishes the slice -- which is exactly how the offset cursor stalled."""
        out = _sweep(True, cap=60, days=3)
        state = self._state(out["state"])
        self.assertIn("window_slice_offsets", state)

    def test_a_slice_is_only_marked_done_when_the_feed_ran_out_inside_it(self):
        """Stopping on budget proves nothing about the slice; marking it done would
        strand whatever it still holds."""
        import inspect

        import fantastic_jobs_adapter as fja

        source = inspect.getsource(fja.DateCreatedWatermarkEngine._run_sliced)
        self.assertIn('slice_stop in ("empty_page", "short_page")', source)

    def test_slicing_can_be_switched_off_for_the_old_cursor(self):
        off = _sweep(False, cap=240)
        self.assertGreater(off["billed"], 0, "the offset path still runs")


class TheWindowStillClosesHonestly(unittest.TestCase):
    """`commit_watermark` only advances when every enabled source is drained, so
    what "drained" means decides whether the watermark can step over rows nobody
    inspected. Under the slice cursor it means every `date_created` sub-range was
    paged to exhaustion -- a far stronger claim than one index ceasing to return
    rows, which is all the offset path could ever assert."""

    def test_a_starved_run_does_not_declare_the_source_drained(self):
        out = _sweep(True, cap=30, days=1)
        with open(out["state"], encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertFalse((state.get("window_drained_sources") or {}).get(LABEL),
                         "a budget stop must never read as full coverage")

    def test_full_coverage_does_declare_it_drained(self):
        out = _sweep(True, cap=240)
        with open(out["state"], encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertTrue((state.get("window_drained_sources") or {}).get(LABEL))
        self.assertGreater(len((state.get("window_slices") or {}).get(LABEL) or []), 0)

    def test_drained_is_never_set_while_slices_remain(self):
        for cap in (30, 60, 120, 240):
            out = _sweep(True, cap=cap, days=2)
            with open(out["state"], encoding="utf-8") as fh:
                state = json.load(fh)
            drained = bool((state.get("window_drained_sources") or {}).get(LABEL))
            if drained:
                self.assertEqual(out["pending"], 0,
                                 f"cap={cap}: drained but rows remain unseen")


if __name__ == "__main__":
    unittest.main()


class SliceProgressIsNotDurableUntilCustodyIsTaken(unittest.TestCase):
    """Slice completion is continuation state, so it obeys the same ordering rule as
    the offset it replaces: it must not become durable before the rows it advances
    past are held.

    `_run_sliced` originally called `self._save()` at the end of its loop, which
    persisted `window_slices` BEFORE `checkpoint()` ran the custody hook -- reopening
    the exact gap the hook exists to close. A run that died in between would have
    recorded slices as drained while nothing held their rows.
    """

    def test_the_sliced_pass_does_not_persist_state_itself(self):
        import inspect

        import fantastic_jobs_adapter as fja

        # As a CALL, not as text -- the comment explaining the absence names it.
        import ast
        import textwrap

        source = textwrap.dedent(
            inspect.getsource(fja.DateCreatedWatermarkEngine._run_sliced))
        calls = {getattr(n.func, "attr", "") for n in ast.walk(ast.parse(source))
                 if isinstance(n, ast.Call)}
        self.assertNotIn("_save", calls,
                         "continuation must be saved by checkpoint(), after custody")

    def test_a_failed_custody_hook_leaves_no_slice_recorded(self):
        import json as _json
        import os as _os
        import tempfile as _tempfile

        import fantastic_jobs_adapter as fja
        from tests.test_continuation_multirun_replay import MovingFloorFeed as _Feed

        state = _os.path.join(_tempfile.mkdtemp(), "wm.json")
        feed = _Feed()
        fja.set_custody_hook(lambda _rows: False)
        try:
            _run(state, feed, BASE, cap=240, seen=set(),
                 FANTASTIC_WINDOW_SLICING_ENABLED=True)
        finally:
            fja.set_custody_hook(None)

        persisted = _json.load(open(state, encoding="utf-8")) if _os.path.exists(state) else {}
        self.assertFalse((persisted.get("window_slices") or {}).get(LABEL),
                         "custody failed, so no slice may be recorded as drained")

    def test_checkpoint_does_persist_them_when_custody_succeeds(self):
        out = _sweep(True, cap=240, days=3)
        with open(out["state"], encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertTrue((state.get("window_slices") or {}).get(LABEL),
                        "the normal path still records progress")
