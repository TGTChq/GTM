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

    A full page of duplicates never proves exhaustion -- it is evidence about one
    offset, not about the tail, which is why ``no_new_ids`` is not a drained stop.
    What the cursor changes is the COST of stopping: without a persisted offset
    the spend is repeated from zero next run either way, so stopping is the
    cheaper option; with one, stopping discards the only mechanism that reaches
    the tail.
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
        """The head/deep path persists no offset, so paging past a duplicate page
        buys rows the next run must buy again from zero. Stopping is a budget
        choice there, not a claim that the query is exhausted -- and it is
        deliberately left as it was."""
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


class FrameHorizonTests(unittest.TestCase):
    """The feed's ``time_frame`` is a hard floor under every window.

    PROVEN 2026-09-05 against the live provider with two ``/v1/active-jb-count``
    requests (zero Jobs credits, zero rows): a window lying entirely below the frame
    returned **0** rows while ``time_frame=7d`` was sent, and **24,784** for the same
    window with the parameter removed. The provider INTERSECTS the two.

    Removing ``time_frame`` is not an available fix: without it the feed could not
    serve the production query at all -- a 45s read timeout, then HTTP 504 at 240s.
    It is what makes the query answerable, so the window has to live inside it.

    Two consequences, and this class pins both:

    * A window whose lower bound is below ``now - time_frame`` has a dead zone that
      no request can reach, and the watermark advances past it silently.
    * A window whose UPPER bound has fallen below the horizon can return nothing at
      all -- and because nothing marks a source drained on an empty interval,
      ``window_drained`` never becomes true and the watermark never advances. The
      window stays open and acquisition stops. That is the ten-day zero-acquisition
      outage of 2026-08, reproduced below and ended by the abandon branch.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "wm.json")
        self.cfg = dict(_PROD,
                        FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
                        FANTASTIC_DATE_CREATED_LAG_MINUTES=180,
                        FANTASTIC_DATE_CREATED_OVERLAP_MINUTES=60,
                        FANTASTIC_TIME_FRAME_MARGIN_MINUTES=30,
                        FANTASTIC_WATERMARK_STATE_PATH=self.path)

    def _open(self, now, state=None):
        """Open a window at ``now`` and return its watermark metrics."""
        if state is not None:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(dict(state, schema="fantastic-watermark/1"), fh)
        metrics = {"segments": {}}
        with mock.patch.multiple(config, **self.cfg), \
                mock.patch.object(fja, "datetime", wraps=datetime) as dt:
            dt.now.return_value = now
            dt.fromisoformat = datetime.fromisoformat
            eng = fja.DateCreatedWatermarkEngine(
                result=None, quota=fja._QuotaState(), http_get=None,
                seen_ids=set(), metrics=metrics, run_cap=100, now=now)
            eng.open()
        return eng, metrics["watermark"]

    def test_a_window_is_never_born_below_the_horizon(self):
        _eng, wm = self._open(NOW)
        self.assertGreaterEqual(wm["lower"], wm["frame_horizon"])
        self.assertIsNotNone(wm["lower_clamped_to_frame"])
        self.assertIsNone(wm["window_abandoned_below_frame"])

    def test_the_conceded_span_is_recorded_not_absorbed(self):
        """The rows below the horizon are lost either way. What changes is whether
        anyone can see that they were."""
        _eng, wm = self._open(NOW)
        clamp = wm["lower_clamped_to_frame"]
        self.assertLess(clamp["was"], clamp["now"])
        self.assertGreater(clamp["unreachable_days"], 0)

    def test_clamping_never_rewinds_the_cursor(self):
        """The feed serves from max(lower, horizon) whether or not the request says
        so, so raising the lower bound returns the same rows and costs the cursor
        nothing. Resetting offsets here would rewind every run whose window sits near
        the horizon -- exactly the livelock the durable cursor exists to end."""
        state = {"last_successful_watermark": _iso_at(NOW - timedelta(days=9)),
                 "window_start": _iso_at(NOW - timedelta(days=9)),
                 "window_end": _iso_at(NOW - timedelta(days=1)),
                 "in_flight_window_end": _iso_at(NOW - timedelta(days=1)),
                 "window_offsets": {"fantastic_jobs_linkedin": 2400},
                 "window_acquired_ids": []}
        eng, wm = self._open(NOW, state)
        self.assertTrue(wm["window_reused"])
        self.assertIsNotNone(wm["lower_clamped_to_frame"])
        self.assertEqual(eng.window_offsets()["fantastic_jobs_linkedin"], 2400)
        self.assertEqual(wm["offsets_at_open"]["fantastic_jobs_linkedin"], 2400)

    def test_a_window_below_the_horizon_is_abandoned_not_replayed(self):
        """The 2026-08 outage: an in-flight window everyone kept replaying, which
        could not return a row and therefore could never drain or commit."""
        dead_lower = _iso_at(NOW - timedelta(days=20))
        dead_upper = _iso_at(NOW - timedelta(days=11))
        state = {"last_successful_watermark": dead_lower,
                 "window_start": dead_lower, "window_end": dead_upper,
                 "in_flight_window_end": dead_upper,
                 "window_offsets": {"fantastic_jobs_linkedin": 3000},
                 "window_drained_sources": {"fantastic_jobs_linkedin": True},
                 "window_acquired_ids": ["a", "b"]}
        _eng, wm = self._open(NOW, state)
        gone = wm["window_abandoned_below_frame"]
        self.assertIsNotNone(gone, "a window that can return nothing must not be reused")
        self.assertEqual((gone["lower"], gone["upper"]), (dead_lower, dead_upper))
        self.assertFalse(wm["window_reused"], "the run must open a fresh window instead")
        # ...and the fresh window is one the feed can actually answer, starting from
        # where the dead one ended rather than from the dead one's start.
        self.assertGreaterEqual(wm["lower"], wm["frame_horizon"])
        self.assertLess(wm["lower"], wm["upper"], "a live window, not an empty interval")
        self.assertEqual(wm["offsets_at_open"], {}, "a new window pages from its head")

    def test_slippage_is_reported_so_the_effect_can_be_measured(self):
        """Whether a rising floor SHIFTS the rows a saved offset indexes into depends
        on an ordering the provider does not document. The movement is reported so a
        real run can measure it; it is not assumed in either direction."""
        _e1, wm1 = self._open(NOW)
        self.assertEqual(wm1["frame_slippage_minutes"], 0.0)
        state = json.load(open(self.path, encoding="utf-8"))
        _e2, wm2 = self._open(NOW + timedelta(hours=6), state)
        self.assertTrue(wm2["window_reused"])
        self.assertEqual(wm2["frame_slippage_minutes"], 360.0)


def _iso_at(dt):
    return fja._iso_z(dt)


class _AscendingFeed(_Feed):
    """The same fake provider, returning OLDEST first.

    ``_Feed`` sorts newest-first, which is what the head/deep path has always
    assumed. Nothing in the request asks for either, so the other direction is
    equally consistent with the contract -- and it is the one under which a saved
    offset stops meaning what it meant."""

    def __call__(self, url, headers, params, timeout):
        response = super().__call__(url, headers, params, timeout)
        # Re-select in the opposite order: paging must be applied AFTER the sort,
        # so reversing the returned page would not model this.
        gte, lt = params.get("date_created_gte"), params.get("date_created_lt")
        sel = [r for r in self.rows if (gte is None or r["date_created"] >= gte)
               and (lt is None or r["date_created"] < lt)]
        sel = [r for r in sel if r.get("source_type") != "ats"]
        if params.get("source"):
            sel = [r for r in sel if r.get("source") == params["source"]]
        sel.sort(key=lambda r: r["date_created"])
        o, l = int(params.get("offset", 0)), int(params.get("limit", 100))
        response._d = sel[o:o + l]
        return response


class CoverageAfterTheFloorMovesTests(unittest.TestCase):
    """Running out of rows is only proof of coverage if the offset still points
    where it pointed when it was saved.

    ``time_frame`` is intersected with ``date_created`` (#87), so the floor under an
    open window rises between runs and rows leave the result set an offset indexes
    into. Whether that MOVES the remaining rows depends on the order the feed
    returns them in -- which nothing requests and the provider does not document, so
    it is observed from real rows rather than assumed.

    The consequence is not a wasted page. ``empty_page`` is a drained stop, so an
    over-shot offset reaches the end of a shrunken set, the window commits, and the
    watermark advances past rows no request ever inspected. See
    ``tests/test_offset_continuity_under_a_moving_horizon.py`` for the mechanism in
    isolation; these run it through the real engine.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "wm.json")
        self.cfg = dict(_PROD,
                        FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
                        FANTASTIC_DATE_CREATED_LAG_MINUTES=180,
                        FANTASTIC_DATE_CREATED_OVERLAP_MINUTES=60,
                        FANTASTIC_TIME_FRAME_MARGIN_MINUTES=0,
                        FANTASTIC_WATERMARK_STATE_PATH=self.path,
                        FANTASTIC_JOBS_LINKEDIN_LIMIT=150,
                        FANTASTIC_JOBS_MAX_JOBS_PER_RUN=150)

    def _run(self, rows, now, ascending=False):
        feed = _AscendingFeed(list(rows)) if ascending else _Feed(list(rows))
        with mock.patch.multiple(config, **self.cfg), \
                mock.patch.object(fja, "datetime", wraps=datetime) as dt:
            dt.now.return_value = now
            dt.fromisoformat = datetime.fromisoformat
            return fja.run_fantastic_jobs_acquisition(http_get=feed)

    @staticmethod
    def _seg(result):
        return result.metadata["segments"]["fantastic_jobs_linkedin"]

    def test_an_ascending_feed_is_observed_and_the_drain_is_refused(self):
        """Oldest-first: rows age out of the HEAD, so a saved offset lands past rows
        nobody inspected. The drain is refused and the source is sent back to the
        head of the window rather than allowed to close it."""
        rows = _recs(300, created_start=NOW - timedelta(hours=20), step_min=2)
        first = self._run(rows, NOW, ascending=True)
        self.assertEqual(self._seg(first)["order_probe"]["order_observed"], "asc")
        saved = json.load(open(self.path, encoding="utf-8"))
        self.assertGreater(saved["window_offsets"]["fantastic_jobs_linkedin"], 0)

        second = self._run(rows[:150], NOW + timedelta(days=1), ascending=True)
        seg = self._seg(second)
        self.assertIn("coverage_uncertain", seg)
        self.assertEqual(seg["coverage_uncertain"]["resolution"], "rewound")
        saved = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual(saved["window_offsets"]["fantastic_jobs_linkedin"], 0,
                         "the source restarts at the head of the window")
        self.assertFalse(
            second.metadata["watermark"]["drained_sources"]["fantastic_jobs_linkedin"],
            "a refused drain must leave the window open")

    def test_the_rewind_happens_at_most_once_so_a_window_still_closes(self):
        """A prefix longer than the duplicate-page cap cannot be re-paged in one run,
        so an unbounded refuse -> rewind cycle would never terminate. The second time
        the drain is accepted and the doubt is carried into the record instead."""
        rows = _recs(300, created_start=NOW - timedelta(hours=20), step_min=2)
        self._run(rows, NOW, ascending=True)
        self._run(rows[:150], NOW + timedelta(days=1), ascending=True)
        self._run(rows[:150], NOW + timedelta(days=2), ascending=True)
        third = self._run(rows[:150], NOW + timedelta(days=3), ascending=True)

        seg = self._seg(third)
        self.assertEqual(seg["coverage_uncertain"]["resolution"], "accepted_after_rewind")
        self.assertTrue(
            third.metadata["watermark"]["drained_sources"]["fantastic_jobs_linkedin"],
            "acquisition must not stall on a window it cannot prove it covered")

    def test_an_unobserved_ordering_does_not_buy_a_speculative_re_pass(self):
        """A re-pass costs a whole window of billed rows. Buying that against a
        hazard nobody has shown to exist is as unevidenced as assuming it away, so
        the exposure is recorded and the window is allowed to close."""
        flat = _recs(300, created_start=NOW - timedelta(hours=20), step_min=0)
        first = self._run(flat, NOW)
        # Every row shares one timestamp, so the pass shows no direction at all.
        self.assertEqual(self._seg(first)["order_probe"]["order_observed"], "constant")

        second = self._run(flat[:150], NOW + timedelta(days=1))
        seg = self._seg(second)
        self.assertNotEqual(seg.get("coverage_uncertain", {}).get("resolution"), "rewound")
        saved = json.load(open(self.path, encoding="utf-8"))
        self.assertGreater(saved["window_offsets"]["fantastic_jobs_linkedin"], 0,
                           "no rewind was bought")
