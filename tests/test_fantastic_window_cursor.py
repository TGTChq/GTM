"""The canonical window must be drainable across runs.

Observed in production on 2026-09-04: ATS and LinkedIn each returned EXACTLY
3,000 rows -- their per-source cap -- so both stopped on ``cap_reached`` and the
window was correctly held open. The next run then re-paged that window from
offset 0, because the only "resume point" was the in-memory segment metrics of
the run that had just ended.

Two defects compounded:

* **No durable cursor.** Every run re-billed the same prefix, deduped all of it
  against ``window_acquired_ids``, and never reached the window's tail. The
  bootstrap path has carried a persisted offset since it was written, and its
  comment names this exact livelock; the canonical window simply never got one.
* **``no_new_ids`` counted as drained.** A FULL page of already-seen rows is what
  the replay produces, and it was classified as natural exhaustion -- so the
  watermark committed past inventory no request had ever inspected. The Gate-B 5A
  guarantee ("a partial window that commits loses every un-fetched in-window job
  permanently") was defeated by the replay that the missing cursor caused.

Together they capped recall at one cap's worth of rows per window and spent the
rest of the budget re-buying it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import config
import fantastic_jobs_adapter as fja
from test_fantastic_watermark_ats import NOW, _PROD, _Feed, _recs

WINDOW_INVENTORY = 1000
PER_RUN_CAP = 300


class WindowCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "wm.json")
        self.cfg = dict(
            _PROD,
            FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
            FANTASTIC_DATE_CREATED_LAG_MINUTES=180,
            FANTASTIC_DATE_CREATED_OVERLAP_MINUTES=60,
            FANTASTIC_WATERMARK_STATE_PATH=self.path,
            # Inventory far exceeds the cap: the production shape.
            FANTASTIC_JOBS_LINKEDIN_LIMIT=PER_RUN_CAP,
            FANTASTIC_JOBS_MAX_JOBS_PER_RUN=PER_RUN_CAP,
        )
        self.rows = _recs(WINDOW_INVENTORY, created_start=NOW - timedelta(hours=6), step_min=0)

    def _run(self, now):
        feed = _Feed(list(self.rows))
        with mock.patch.multiple(config, **self.cfg), \
                mock.patch.object(fja, "datetime", wraps=datetime) as dt:
            dt.now.return_value = now
            dt.fromisoformat = datetime.fromisoformat
            result = fja.run_fantastic_jobs_acquisition(http_get=feed)
        with mock.patch.multiple(config, **self.cfg):
            commit = fja.commit_watermark(success=True)
        offsets = [int(p.get("offset", 0)) for _url, p in feed.calls]
        billed = sum(int(s.get("returned", 0) or 0)
                     for s in result.metadata["segments"].values())
        return result, commit, offsets, billed

    # -- the cursor ---------------------------------------------------------

    def test_a_truncated_window_resumes_where_it_stopped(self):
        first, commit, offsets, billed = self._run(NOW)
        self.assertEqual(billed, PER_RUN_CAP)
        self.assertEqual(offsets[0], 0)
        self.assertFalse(commit["committed"], "a truncated window must not commit")

        _second, _c2, offsets2, _b2 = self._run(NOW + timedelta(days=1))
        self.assertEqual(offsets2[0], PER_RUN_CAP,
                         "the next run resumes at the cursor, not at the top")

    def test_the_cursor_is_persisted_and_never_rewinds(self):
        self._run(NOW)
        saved = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual(saved["window_offsets"]["fantastic_jobs_linkedin"], PER_RUN_CAP)

        engine_state = dict(saved)
        engine_state["window_offsets"]["fantastic_jobs_linkedin"] = 900
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(engine_state, fh)
        self._run(NOW + timedelta(days=1))
        saved = json.load(open(self.path, encoding="utf-8"))
        self.assertGreaterEqual(saved["window_offsets"]["fantastic_jobs_linkedin"], 900,
                                "a later pass must never rewind an advanced cursor")

    def test_a_new_window_starts_at_offset_zero(self):
        """A cursor belongs to the window it was measured in."""
        for day in range(4):                      # drains the window and commits
            self._run(NOW + timedelta(days=day))
        _r, _c, offsets, _b = self._run(NOW + timedelta(days=4))
        self.assertEqual(offsets[:1] or [0], [0],
                         "the fresh window must be paged from its head")

    # -- the whole point ----------------------------------------------------

    def test_the_window_drains_completely_instead_of_losing_its_tail(self):
        """End to end: every in-window row is inspected exactly once.

        Before the fix this reached 300 of 1,000 and then advanced the watermark,
        losing 700 jobs permanently while continuing to bill for the prefix.
        """
        seen: set = set()
        total_billed = 0
        committed_on = None
        for day in range(5):
            result, commit, _offsets, billed = self._run(NOW + timedelta(days=day))
            seen |= {j["job_id"] for j in result.jobs}
            total_billed += billed
            if commit["committed"] and committed_on is None:
                committed_on = day + 1

        self.assertEqual(len(seen), WINDOW_INVENTORY, "no in-window job is skipped")
        self.assertEqual(total_billed, WINDOW_INVENTORY,
                         "and none is billed twice: novelty is 100% per credit")
        self.assertEqual(committed_on, 4,
                         "the watermark advances only once the window is genuinely drained")

    def test_a_full_page_of_duplicates_does_not_count_as_drained(self):
        """``no_new_ids`` means 'all seen', never 'nothing left'.

        Classifying it as exhaustion is what let a replayed prefix commit the
        watermark past un-inspected inventory.
        """
        self.assertNotIn("no_new_ids", fja.DateCreatedWatermarkEngine._DRAINED_STOPS)
        self.assertIn("empty_page", fja.DateCreatedWatermarkEngine._DRAINED_STOPS)
        self.assertIn("short_page", fja.DateCreatedWatermarkEngine._DRAINED_STOPS)

    def test_a_source_that_genuinely_finishes_still_drains(self):
        """The guard must not hold a window open forever on a small source."""
        self.rows = _recs(50, created_start=NOW - timedelta(hours=6), step_min=0)
        result, commit, _offsets, _billed = self._run(NOW)
        self.assertTrue(result.metadata["watermark"]["drained"])
        self.assertTrue(commit["committed"])

    # -- cursor OBSERVABILITY ------------------------------------------------
    #
    # The cursor working and the cursor being PROVABLE are different things. The
    # acceptance question is "did this run resume from the offset the last run
    # left behind", and the run's own end state cannot answer it -- by the time
    # anyone reads the artifacts, this run has already moved the cursor. So the
    # run records where it found the cursor as well as where it left it.

    def test_the_run_records_where_it_resumed_and_where_it_stopped(self):
        first, _c, _o, _b = self._run(NOW)
        wm1 = first.metadata["watermark"]
        self.assertEqual(wm1["offsets_at_open"], {},
                         "a brand new window is opened with no cursor at all")
        self.assertEqual(wm1["offsets_at_close"]["fantastic_jobs_linkedin"], PER_RUN_CAP)
        cur1 = wm1["window_cursors"]["fantastic_jobs_linkedin"]
        self.assertEqual((cur1["offset_from"], cur1["offset_to"]), (0, PER_RUN_CAP))
        self.assertEqual(cur1["billed"], PER_RUN_CAP)
        self.assertFalse(cur1["drained"], "a capped source has not finished the window")

        second, _c2, _o2, _b2 = self._run(NOW + timedelta(days=1))
        wm2 = second.metadata["watermark"]
        self.assertEqual(wm2["offsets_at_open"]["fantastic_jobs_linkedin"], PER_RUN_CAP,
                         "the second run OPENS at the offset the first one saved")
        cur2 = wm2["window_cursors"]["fantastic_jobs_linkedin"]
        self.assertEqual(cur2["offset_from"], PER_RUN_CAP,
                         "...and its first request starts exactly there")
        self.assertEqual(cur2["offset_to"], 2 * PER_RUN_CAP)

    def test_undrained_sources_are_named_so_the_backlog_is_countable(self):
        result, _c, _o, _b = self._run(NOW)
        wm = result.metadata["watermark"]
        self.assertEqual(wm["undrained_sources"], ["fantastic_jobs_linkedin"])
        self.assertFalse(wm["drained"])

        self.rows = _recs(50, created_start=NOW - timedelta(hours=6), step_min=0)
        done, _c2, _o2, _b2 = self._run(NOW + timedelta(days=1))
        self.assertEqual(done.metadata["watermark"]["undrained_sources"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ConsumedPrefixTests(unittest.TestCase):
    """A window whose prefix a PREVIOUS run already bought must not cost a month.

    Measured in production 2026-09-05, run 20260905T030439Z-1d102bec. The first
    canonical-window run after the cursor shipped reused a window opened by the
    2026-09-04 run, which had bought 3,000 rows per source into it. That run
    predates the cursor, so it persisted no offset; this one opened at
    ``offsets_at_open = {}``, re-paged from 0, found page 1 entirely
    already-seen, and stopped on ``no_new_ids``.

        jobs_returned_billed  200
        jobs_unique_kept        0
        offsets_at_close      {ats: 100, linkedin: 100}
        run_cap              2839   <- budget was NOT the constraint

    One page per source per run, against a 3,000-row consumed prefix, is ~30 runs
    and ~6,000 credits to return to where the earlier run had already reached --
    with the window stale and nothing acquired the whole time.

    A full page of duplicates means "this query is exhausted" only when the offset
    does NOT survive the run. With a durable cursor it means "the prefix is
    consumed", and stopping discards the one mechanism that reaches the tail.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "wm.json")
        self.cfg = dict(
            _PROD,
            FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
            FANTASTIC_DATE_CREATED_LAG_MINUTES=180,
            FANTASTIC_DATE_CREATED_OVERLAP_MINUTES=60,
            FANTASTIC_WATERMARK_STATE_PATH=self.path,
            FANTASTIC_JOBS_LINKEDIN_LIMIT=1000,
            FANTASTIC_JOBS_MAX_JOBS_PER_RUN=1000,
        )
        # 1,000 rows in the window; a previous run consumed the first 300.
        self.rows = _recs(1000, created_start=NOW - timedelta(hours=6), step_min=0)

    def _run(self, now, cfg=None):
        feed = _Feed(list(self.rows))
        with mock.patch.multiple(config, **(cfg or self.cfg)), \
                mock.patch.object(fja, "datetime", wraps=datetime) as dt:
            dt.now.return_value = now
            dt.fromisoformat = datetime.fromisoformat
            result = fja.run_fantastic_jobs_acquisition(http_get=feed)
        billed = sum(int(s.get("returned", 0) or 0)
                     for s in result.metadata["segments"].values())
        return result, billed

    def _seed_consumed_prefix(self, count: int) -> None:
        """The production shape: ids already acquired, NO persisted offset."""
        first, _billed = self._run(NOW)
        saved = json.load(open(self.path, encoding="utf-8"))
        # REPLACE, never union: the point of the fixture is that only the first
        # ``count`` rows were consumed. Unioning with what this seeding run itself
        # acquired would mark the whole window seen and test nothing.
        saved["window_acquired_ids"] = sorted(
            {str(j["job_id"]).replace("fantastic_", "") for j in first.jobs[:count]})
        saved["window_offsets"] = {}          # the pre-cursor build persisted none
        saved["window_drained_sources"] = {}
        saved["window_drained"] = False
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)

    def test_a_durable_cursor_pages_past_the_consumed_prefix_in_one_run(self):
        self._seed_consumed_prefix(300)
        result, billed = self._run(NOW + timedelta(minutes=5))
        seg = result.metadata["segments"]["fantastic_jobs_linkedin"]

        self.assertGreater(len(result.jobs), 0,
                           "the tail past the consumed prefix must be reached")
        self.assertNotEqual(seg.get("stop_reason"), "no_new_ids")
        self.assertGreaterEqual(seg.get("duplicate_pages_skipped", 0), 1,
                                "the skipped duplicate pages are counted, not hidden")
        saved = json.load(open(self.path, encoding="utf-8"))
        self.assertGreaterEqual(
            saved["window_offsets"]["fantastic_jobs_linkedin"], 300,
            "the cursor moves past the prefix in ONE run, not one page per run")

    def test_the_duplicate_page_budget_is_bounded(self):
        """A window that is ENTIRELY overlap must not spend the whole run cap."""
        self._seed_consumed_prefix(1000)
        cfg = dict(self.cfg, FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES=2)
        result, billed = self._run(NOW + timedelta(minutes=5), cfg)
        seg = result.metadata["segments"]["fantastic_jobs_linkedin"]

        self.assertEqual(seg.get("stop_reason"), "duplicate_page_cap")
        self.assertLessEqual(billed, 2 * 100 + 100,
                             "spend is bounded by the duplicate-page cap")

    def test_a_duplicate_page_cap_never_counts_as_drained(self):
        """It is a budget stop, so the watermark must still not advance."""
        self.assertNotIn("duplicate_page_cap",
                         fja.DateCreatedWatermarkEngine._DRAINED_STOPS)

    def test_without_a_durable_cursor_the_old_stop_is_unchanged(self):
        """The head/deep path has no persisted offset: for it, a full duplicate
        page really does mean the query is exhausted, and re-paging would just
        re-bill the same rows next run."""
        metrics = {"segments": {}}
        calls = {"n": 0}

        def feed(url, params=None, **kw):
            calls["n"] += 1
            return _Feed(list(self.rows))(url, params=params, **kw)

        seen = {f"fantastic_{r['id']}" for r in self.rows[:100]}
        with mock.patch.multiple(config, **self.cfg):
            fja._fetch_segment("https://x/jb", {}, "fantastic_jobs_linkedin", 1000,
                               fja._QuotaState(), feed, set(seen), metrics,
                               durable_cursor=False)
        self.assertEqual(
            metrics["segments"]["fantastic_jobs_linkedin"].get("stop_reason"),
            "no_new_ids")
