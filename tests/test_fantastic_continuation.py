"""Cross-run Fantastic continuation cursor (date_posted keyset).

The Direct API feed is date_posted DESC with no stable `order`/id-keyset, and the
ONLY proven date filter is the `date_posted_lt` upper bound (there is NO
lower-bound param). Continuation tracks two edges of the single stream. These tests
pin:
  * disabled by default -> no cursor param, no state written (baseline unchanged);
  * first run sets cursor_date=oldest, high_water=newest, boundary ids;
  * a resumed run sends date_posted_lt = cursor + 1s for the DEEP backfill,
    re-includes and DEDUPES the boundary second (no re-count, no skip), and never
    re-acquires the deep prefix;
  * new jobs entering the top between runs are DISCOVERED by the fresh-edge HEAD
    pass (page from the top, stop client-side at the prior high_water) -- the fix
    for the production zero-acquisition bug where an exhausted deep crawl starved
    daily discovery.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import contextlib
from datetime import datetime as _dtclass, timezone as _tz

import config
import fantastic_jobs_adapter as fja


class _FrozenDT(_dtclass):
    """datetime whose .now() is fixed, so the stale-window check is evaluated
    against a deterministic clock instead of the wall clock (fromisoformat and all
    other behavior inherited unchanged). Keeps date-anchored fixtures stable
    regardless of the real date; production logic is untouched."""
    _fixed = _dtclass(2026, 8, 18, 4, 0, 0, tzinfo=_tz.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


@contextlib.contextmanager
def _frozen_clock(now=None):
    if now is None:
        yield
        return
    _FrozenDT._fixed = now
    with mock.patch.object(fja, "datetime", _FrozenDT):
        yield


class _Resp:
    def __init__(self, rows):
        self.status_code = 200
        self.headers = {"x-api-jobs-remaining": "1000000", "x-api-requests-remaining": "1000000"}
        self._rows = rows

    def json(self):
        return self._rows


def _rec(i, dt):
    return {"id": str(i), "title": "Software Engineer", "organization": f"Co {i}",
            "source": "linkedin", "date_posted": dt, "countries_derived": ["United States"],
            "employment_type": ["FULL_TIME"], "org_linkedin_headcount": 100}


def _feed(records):
    """A date_posted-DESC feed honoring date_posted_lt + offset/limit."""
    def http_get(url, headers, params, timeout):
        lt = params.get("date_posted_lt")
        rows = [r for r in records if lt is None or r["date_posted"] < lt]
        rows = sorted(rows, key=lambda r: (r["date_posted"], int(r["id"])), reverse=True)
        offset = int(params.get("offset", 0)); limit = int(params.get("limit", 100))
        return _Resp(rows[offset:offset + limit])
    return http_get


def _run(http_get, state_path, cap=3, enabled=True, time_frame="24h", now=None):
    base = dict(
        FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
        FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
        FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_WELLFOUND_LIMIT=0, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
        FANTASTIC_JOBS_LINKEDIN_LIMIT=cap, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=cap,
        FANTASTIC_JOBS_TIME_FRAME=time_frame, FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=50,
        FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=90, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=20,
        FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=True,
        FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
        FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME", FANTASTIC_JOBS_EXCLUDE_AGENCY=True,
        FANTASTIC_JOBS_CONTINUATION_ENABLED=enabled,
        FANTASTIC_JOBS_CONTINUATION_STATE_PATH=state_path,
    )
    captured = []
    orig = http_get

    def wrapped(url, headers, params, timeout):
        captured.append(dict(params))
        return orig(url, headers, params, timeout)
    with mock.patch.multiple(config, **base), _frozen_clock(now):
        res = fja.run_fantastic_jobs_acquisition(http_get=wrapped)
    return res, captured


# distinct-second feed, newest first. Anchored to a fixed date, so date-anchored
# tests freeze the clock (_FIXED_NOW) to keep these fixtures inside the window.
_FIXED_NOW = _dtclass(2026, 8, 18, 4, 0, 0, tzinfo=_tz.utc)
D = {n: f"2026-08-18T03:00:{n:02d}" for n in range(1, 21)}
FEED = [_rec(1000 + n, D[n]) for n in range(20, 0, -1)]  # id 1020@:20 (newest) .. 1001@:01


class FantasticContinuationTests(unittest.TestCase):
    def test_disabled_by_default_writes_no_cursor_and_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            _res, captured = _run(_feed(FEED), sp, cap=3, enabled=False)
            self.assertTrue(all("date_posted_lt" not in p for p in captured))
            self.assertFalse(Path(sp).exists())

    def test_first_run_sets_cursor_high_water_and_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            res, captured = _run(_feed(FEED), sp, cap=3)
            self.assertTrue(all("date_posted_lt" not in p for p in captured))  # no cursor yet
            got = sorted(int(j["_fantastic_internal_id"]) for j in res.jobs)
            self.assertEqual(got, [1018, 1019, 1020])                          # newest 3
            state = json.loads(Path(sp).read_text(encoding="utf-8"))
            self.assertEqual(state["cursor_date"], D[18])                       # oldest acquired
            self.assertEqual(state["high_water"], D[20])                        # newest acquired
            self.assertEqual(state["boundary_ids"], ["1018"])

    def test_resume_sends_cursor_dedupes_boundary_and_never_overlaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            r1, _ = _run(_feed(FEED), sp, cap=3, now=_FIXED_NOW)
            first_ids = {j["_fantastic_internal_id"] for j in r1.jobs}          # {1020,1019,1018}
            r2, captured2 = _run(_feed(FEED), sp, cap=3, now=_FIXED_NOW)
            # resumed with date_posted_lt = cursor(:18) + 1s = :19
            self.assertTrue(any(p.get("date_posted_lt") == D[19] for p in captured2))
            second_ids = {j["_fantastic_internal_id"] for j in r2.jobs}
            self.assertFalse(first_ids & second_ids)                            # no re-acquire (dedup+cursor)
            self.assertNotIn("1018", second_ids)                               # boundary re-fetched but deduped
            self.assertTrue(all(int(i) < 1018 for i in second_ids))            # strictly older
            state2 = json.loads(Path(sp).read_text(encoding="utf-8"))
            self.assertLess(state2["cursor_date"], D[18])                       # cursor advanced older
            self.assertEqual(state2["high_water"], D[20])                       # high_water preserved

    def test_new_jobs_at_top_between_runs_are_discovered_by_head_pass(self):
        """FIX (formerly ..._are_not_skipped_and_deferred): the fresh-edge HEAD pass
        pages from the top of the DESC feed and DISCOVERS jobs posted since the prior
        high_water. The old design deferred them forever -- the exact production
        zero-acquisition bug. New arrivals are now acquired AND the deep tail still
        advances, and high_water moves up to the newest arrival."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            r1, _ = _run(_feed(FEED), sp, cap=3, now=_FIXED_NOW)                # acquires :20,:19,:18
            # New jobs enter the TOP (newer than any prior) between runs.
            newer = [_rec(2001, "2026-08-18T03:05:00"), _rec(2002, "2026-08-18T03:06:00")]
            r2, _ = _run(_feed(newer + FEED), sp, cap=3, now=_FIXED_NOW)
            second_ids = {j["_fantastic_internal_id"] for j in r2.jobs}
            # The head pass grabs the brand-new top arrivals...
            self.assertIn("2001", second_ids)
            self.assertIn("2002", second_ids)
            # ...and high_water advanced to the newest arrival (with its boundary id).
            state2 = json.loads(Path(sp).read_text(encoding="utf-8"))
            self.assertEqual(state2["high_water"], "2026-08-18T03:06:00")
            self.assertIn("2002", state2["high_water_ids"])
            # The deep cursor must NOT regress newer (that would re-bill the prefix):
            # here the head pass filled the cap, so the deep floor is preserved.
            self.assertLessEqual(state2["cursor_date"], D[18])


def _iso(dt):
    return dt.isoformat()


class FantasticContinuationStaleAndQuotaTests(unittest.TestCase):
    """Scenario E (24h-window rollover / stale continuation resets safely) and
    scenario F (quota exhaustion during continuation stays resumable without
    re-billing the already-acquired prefix)."""

    def _recent_feed_records(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        # 12 fresh jobs within the last hour, newest first ids 3020..3009
        recs = []
        for k in range(12):
            dt = now - timedelta(minutes=(k + 1))
            recs.append(_rec(3020 - k, _iso(dt).replace("+00:00", "Z")))
        return recs

    def test_stale_cursor_resets_to_fresh_window_not_silent_suppression(self):
        from datetime import datetime, timezone, timedelta
        recs = self._recent_feed_records()
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            # Pre-seed a STALE cursor 48h old (older than the 24h window).
            stale = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
            Path(sp).write_text(json.dumps({
                "schema": fja._CONTINUATION_SCHEMA, "source": "linkedin",
                "cursor_date": stale, "high_water": stale, "boundary_ids": ["999999"],
            }), encoding="utf-8")
            res, captured = _run(_feed(recs), sp, cap=3)
            cont = res.metadata["continuation"]
            self.assertEqual(cont["reset_reason"], "stale_window")
            self.assertEqual(cont["resumed_from_cursor_date"], "")           # stale cursor dropped
            self.assertTrue(all("date_posted_lt" not in p for p in captured))  # fresh window, no stale lt
            self.assertEqual(len(res.jobs), 3)                                # acquired, NOT suppressed
            # A fresh, recent cursor was persisted (not the stale one).
            state = json.loads(Path(sp).read_text(encoding="utf-8"))
            self.assertNotEqual(state["cursor_date"], stale)
            self.assertGreater(state["cursor_date"], stale)

    def test_fresh_cursor_within_window_is_not_reset(self):
        from datetime import datetime, timezone, timedelta
        recs = self._recent_feed_records()
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            r1, _ = _run(_feed(recs), sp, cap=3)                              # sets a recent cursor
            r2, captured2 = _run(_feed(recs), sp, cap=3)                      # resume, cursor is fresh
            self.assertEqual(r2.metadata["continuation"]["reset_reason"], "")  # NOT reset
            self.assertTrue(any("date_posted_lt" in p for p in captured2))    # applied the fresh cursor
            self.assertFalse({j["_fantastic_internal_id"] for j in r1.jobs}
                             & {j["_fantastic_internal_id"] for j in r2.jobs})

    def _many_recent(self, n):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        # n fresh jobs, newest first, distinct seconds within the last ~3h.
        return [_rec(5000 + (n - k),
                     _iso(now - timedelta(seconds=(k + 1) * 30)).replace("+00:00", "Z"))
                for k in range(n)]

    def _low_remaining_feed(self, records, remaining):
        def http_get(url, headers, params, timeout):
            lt = params.get("date_posted_lt")
            rows = [r for r in records if lt is None or r["date_posted"] < lt]
            rows = sorted(rows, key=lambda r: (r["date_posted"], int(r["id"])), reverse=True)
            offset = int(params.get("offset", 0)); limit = int(params.get("limit", 100))
            resp = _Resp(rows[offset:offset + limit])
            resp.headers = {"x-api-jobs-remaining": str(remaining),
                            "x-api-requests-remaining": "1000000"}
            return resp
        return http_get

    def test_quota_exhaustion_mid_continuation_is_partial_and_resumable(self):
        recs = self._many_recent(150)  # enough for a FULL 100-job first page
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            # cap=200 wants a 2nd page; page 1 fills 100 and the provider reports
            # jobs_remaining=90 (== MIN), so page 2's quota breach stops PARTIAL.
            r1, _ = _run(self._low_remaining_feed(recs, remaining=90), sp, cap=200)
            self.assertEqual(len(r1.jobs), 100)                              # partial (quota-bounded)
            self.assertEqual(r1.metadata["stop_reason"], "jobs_quota_reserve")
            state = json.loads(Path(sp).read_text(encoding="utf-8"))
            acquired_dates = sorted(j["job_posted_at_datetime_utc"] for j in r1.jobs)
            self.assertEqual(state["cursor_date"], acquired_dates[0])         # cursor = oldest ACQUIRED
            # Resume (quota restored): continue STRICTLY OLDER, no re-bill of the prefix.
            r2, captured2 = _run(self._low_remaining_feed(recs, remaining=1000000), sp, cap=200)
            first = {j["_fantastic_internal_id"] for j in r1.jobs}
            second = {j["_fantastic_internal_id"] for j in r2.jobs}
            self.assertFalse(first & second)                                 # acquired prefix not re-billed
            self.assertTrue(any("date_posted_lt" in p for p in captured2))


class FantasticContinuationModeMatchTests(unittest.TestCase):
    """Regression: title_advanced (single stream) takes precedence over per-family
    title_targeting in acquisition, so the continuation cursor must be SAVED in the
    single-stream mode that actually ran. With both flags on, saving in
    title_families mode would persist an empty state and drop the cursor -> every
    run restarts from the top and re-bills. Production runs with BOTH flags on."""

    def _run_both_flags(self, sp, cap=3):
        base = dict(
            FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
            FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
            FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_WELLFOUND_LIMIT=0, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
            FANTASTIC_JOBS_LINKEDIN_LIMIT=cap, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=cap,
            FANTASTIC_JOBS_TIME_FRAME="24h", FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=50,
            FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=90, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=20,
            FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=True,
            FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
            FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME", FANTASTIC_JOBS_EXCLUDE_AGENCY=True,
            FANTASTIC_JOBS_CONTINUATION_ENABLED=True, FANTASTIC_JOBS_CONTINUATION_STATE_PATH=sp,
            FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED=True,   # both on (production config)
            FANTASTIC_JOBS_TITLE_TARGETING_ENABLED=True,
        )
        with mock.patch.multiple(config, **base), _frozen_clock(_FIXED_NOW):
            return fja.run_fantastic_jobs_acquisition(http_get=_feed(FEED))

    def test_title_advanced_precedence_persists_single_stream_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            r1 = self._run_both_flags(sp, cap=3)
            state = json.loads(Path(sp).read_text(encoding="utf-8"))
            # Saved as a single-stream cursor (NOT an empty title_families state).
            self.assertNotEqual(state.get("mode"), "title_families")
            self.assertEqual(state["cursor_date"], D[18])                    # oldest acquired
            self.assertFalse(r1.metadata["used_title_families"])             # single-stream ran
            # Resume actually advances (no re-bill of the acquired prefix).
            r2 = self._run_both_flags(sp, cap=3)
            self.assertTrue(any(int(i) < 1018 for i in
                                {j["_fantastic_internal_id"] for j in r2.jobs}))
            self.assertFalse({j["_fantastic_internal_id"] for j in r1.jobs}
                             & {j["_fantastic_internal_id"] for j in r2.jobs})


class FantasticContinuation7dWindowTests(unittest.TestCase):
    """A 7d acquisition window (production KPI config). Verifies the continuation
    cursor correctly (a) DEPLETES a multi-day backlog older-first with no re-bill,
    (b) DEFERS newly-arriving top-of-feed jobs (never lost), and (c) treats a
    within-window cursor as FRESH but a rolled-over (>7d) cursor as STALE so a
    reset re-acquires the fresh window (picking up the new arrivals)."""

    def _feed_recent(self, n, span_days=6.5):
        """n jobs newest-first spread across the last span_days (all inside 7d)."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        step = (span_days * 86400) / max(1, n)
        return [_rec(6000 + (n - k),
                     _iso(now - timedelta(seconds=(k + 1) * step)).replace("+00:00", "Z"))
                for k in range(n)]

    def test_backlog_depletion_older_first_no_rebill(self):
        recs = self._feed_recent(30)  # ~6.5-day backlog inside the 7d window
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            r1, _ = _run(_feed(recs), sp, cap=8, time_frame="7d")
            r2, cap2 = _run(_feed(recs), sp, cap=8, time_frame="7d")
            r3, _ = _run(_feed(recs), sp, cap=8, time_frame="7d")
            i1 = {j["_fantastic_internal_id"] for j in r1.jobs}
            i2 = {j["_fantastic_internal_id"] for j in r2.jobs}
            i3 = {j["_fantastic_internal_id"] for j in r3.jobs}
            self.assertEqual(r2.metadata["continuation"]["reset_reason"], "")   # within-window resume
            self.assertTrue(any("date_posted_lt" in p for p in cap2))
            self.assertFalse(i1 & i2)                                            # no re-bill
            self.assertFalse(i1 & i3)
            self.assertFalse(i2 & i3)                                            # strictly deeper each run
            self.assertGreaterEqual(len(i1 | i2 | i3), 20)                       # backlog actually draining

    def test_new_top_arrivals_discovered_by_head_pass(self):
        """FIX (formerly ..._deferred_not_lost): brand-new top-of-feed arrivals are
        DISCOVERED by the fresh-edge head pass on the very next run (they used to be
        deferred indefinitely), and high_water advances to the newest arrival."""
        recs = self._feed_recent(20)
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            r1, _ = _run(_feed(recs), sp, cap=6, time_frame="7d")
            hw1 = json.loads(Path(sp).read_text())["high_water"]
            # brand-new jobs enter the TOP (newer than anything acquired)
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            newer = [_rec(9001, _iso(now).replace("+00:00", "Z")),
                     _rec(9002, _iso(now - timedelta(seconds=1)).replace("+00:00", "Z"))]
            r2, _ = _run(_feed(newer + recs), sp, cap=6, time_frame="7d")
            i2 = {j["_fantastic_internal_id"] for j in r2.jobs}
            self.assertIn("9001", i2)             # head pass discovers the new arrivals
            self.assertIn("9002", i2)
            # high_water advanced past the prior mark up to the newest arrival.
            hw2 = json.loads(Path(sp).read_text())["high_water"]
            self.assertGreater(hw2, hw1)
            self.assertGreaterEqual(hw2, _iso(now - timedelta(seconds=1)).replace("+00:00", "Z"))

    def test_5day_cursor_fresh_but_8day_cursor_resets_under_7d_window(self):
        from datetime import datetime, timezone, timedelta
        recs = self._feed_recent(12)
        for age_days, expect_reset in ((5, False), (8, True)):
            with tempfile.TemporaryDirectory() as tmp:
                sp = str(Path(tmp) / "c.json")
                cur = _iso(datetime.now(timezone.utc) - timedelta(days=age_days))
                Path(sp).write_text(json.dumps({
                    "schema": fja._CONTINUATION_SCHEMA, "source": "linkedin",
                    "cursor_date": cur, "high_water": cur, "boundary_ids": ["1"]}),
                    encoding="utf-8")
                res, cap = _run(_feed(recs), sp, cap=5, time_frame="7d")
                cont = res.metadata["continuation"]
                if expect_reset:
                    self.assertEqual(cont["reset_reason"], "stale_window")   # rolled over -> fresh window
                    self.assertTrue(all("date_posted_lt" not in p for p in cap))
                    self.assertTrue(res.jobs)                                # new arrivals acquired
                else:
                    # Within 7d -> the cursor is HONORED (not reset): the head pass
                    # uses it as the fresh-edge stop, and jobs are still acquired.
                    self.assertEqual(cont["reset_reason"], "")
                    self.assertEqual(cont["head_from_high_water"], cur)
                    self.assertTrue(res.jobs)


class FantasticFreshEdgeDailyTests(unittest.TestCase):
    """Reproduces the production zero-acquisition incident (Aug 20/21: 0 raw_postings
    every day after the Aug 18 crawl) and pins the durable fix: the daily cron must
    discover jobs posted since the last run even after the historical DEEP crawl is
    exhausted, without re-billing the historical prefix."""

    from datetime import datetime as _dt, timedelta as _td
    _tzc = _tz.utc

    def _at(self, i, dt):
        return _rec(i, dt.isoformat().replace("+00:00", "Z"))

    def _day(self, sp, records, now, cap=6, mode="head_then_deep"):
        with mock.patch.object(config, "FANTASTIC_JOBS_ACQUIRE_MODE", mode):
            return _run(_feed(records), sp, cap=cap, time_frame="7d", now=now)

    def test_incident_repro_daily_fresh_edge_after_deep_exhausted(self):
        base = self._dt(2026, 8, 18, 13, 0, 0, tzinfo=self._tzc)
        d1 = [self._at(1000 + k, base - self._td(hours=6 * k)) for k in range(10)]  # ~2.5d backlog
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            # DAY 1 (Aug 18): acquire the newest slice of the backlog.
            r1, _ = self._day(sp, d1, base, cap=6)
            self.assertTrue(r1.jobs)
            hw1 = json.loads(Path(sp).read_text())["high_water"]
            # DAY 2 (Aug 19): 3 jobs posted since hw1 -> MUST be discovered (not 0).
            new2 = [self._at(2000 + k, base + self._td(days=1, minutes=-k)) for k in range(3)]
            r2, _ = self._day(sp, new2 + d1, base + self._td(days=1), cap=6)
            i2 = {j["_fantastic_internal_id"] for j in r2.jobs}
            for k in range(3):
                self.assertIn(str(2000 + k), i2)                    # fresh edge discovered
            hw2 = json.loads(Path(sp).read_text())["high_water"]
            self.assertGreater(hw2, hw1)                            # high_water advanced
            # DAY 3 (Aug 20): 2 more new jobs -> discovered, no re-bill of day-2 jobs.
            new3 = [self._at(3000 + k, base + self._td(days=2, minutes=-k)) for k in range(2)]
            r3, _ = self._day(sp, new3 + new2 + d1, base + self._td(days=2), cap=6)
            i3 = {j["_fantastic_internal_id"] for j in r3.jobs}
            for k in range(2):
                self.assertIn(str(3000 + k), i3)
            self.assertFalse(i2 & i3)                               # no re-bill across days
            self.assertGreater(json.loads(Path(sp).read_text())["high_water"], hw2)

    def test_valid_zero_day_then_discovery_next_day(self):
        base = self._dt(2026, 8, 18, 13, 0, 0, tzinfo=self._tzc)
        d1 = [self._at(1000 + k, base - self._td(hours=3 * k)) for k in range(4)]
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            r1, _ = self._day(sp, d1, base, cap=10)                 # drains the small backlog
            st1 = json.loads(Path(sp).read_text())
            # A quiet day: no new jobs, deep exhausted -> a VALID zero run...
            r2, _ = self._day(sp, d1, base + self._td(days=1), cap=10)
            self.assertEqual(len(r2.jobs), 0)
            st2 = json.loads(Path(sp).read_text())
            self.assertEqual(st2["high_water"], st1["high_water"])  # state preserved, not regressed
            self.assertEqual(st2["cursor_date"], st1["cursor_date"])
            # ...and the very next day a new job appears and IS discovered (not stuck).
            new = [self._at(9000, base + self._td(days=2))]
            r3, _ = self._day(sp, new + d1, base + self._td(days=2), cap=10)
            self.assertIn("9000", {j["_fantastic_internal_id"] for j in r3.jobs})

    def test_identical_boundary_timestamps_new_sibling_discovered_old_deduped(self):
        base = self._dt(2026, 8, 18, 13, 0, 0, tzinfo=self._tzc)
        ts = base
        # Two jobs share the exact high_water second on day 1.
        d1 = [self._at(1001, ts), self._at(1002, ts), self._at(1003, ts - self._td(hours=1))]
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            r1, _ = self._day(sp, d1, base, cap=10)
            st1 = json.loads(Path(sp).read_text())
            self.assertEqual(st1["high_water"], ts.isoformat())   # normalized to +00:00
            self.assertEqual(set(st1["high_water_ids"]), {"1001", "1002"})
            # A THIRD sibling appears at the SAME second next run -> discovered; the two
            # already-acquired siblings at that second are deduped (not re-acquired).
            sib = [self._at(1004, ts)]
            r2, _ = self._day(sp, sib + d1, base + self._td(hours=2), cap=10)
            i2 = {j["_fantastic_internal_id"] for j in r2.jobs}
            self.assertIn("1004", i2)
            self.assertNotIn("1001", i2)
            self.assertNotIn("1002", i2)

    def test_prefix_schema_without_high_water_ids_upgrades_in_place(self):
        # The CURRENTLY DEPLOYED (pre-fix) state file has cursor_date + high_water but
        # NO high_water_ids. First fixed run must load it, discover new jobs, and add
        # high_water_ids -- no crash, no wipe.
        base = self._dt(2026, 8, 18, 13, 0, 0, tzinfo=self._tzc)
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            Path(sp).write_text(json.dumps({
                "schema": fja._CONTINUATION_SCHEMA, "source": "linkedin",
                "cursor_date": (base - self._td(days=2)).isoformat().replace("+00:00", "Z"),
                "high_water": (base - self._td(hours=1)).isoformat().replace("+00:00", "Z"),
                "boundary_ids": ["777"],
            }), encoding="utf-8")  # note: NO high_water_ids key
            new = [self._at(4001, base), self._at(4002, base - self._td(minutes=1))]
            older = [self._at(500 + k, base - self._td(days=1, hours=k)) for k in range(3)]
            r, _ = self._day(sp, new + older, base, cap=10)
            i = {j["_fantastic_internal_id"] for j in r.jobs}
            self.assertIn("4001", i)                                # fresh edge discovered
            self.assertIn("4002", i)
            st = json.loads(Path(sp).read_text())
            self.assertIn("high_water_ids", st)                     # schema upgraded in place
            self.assertIn("4001", st["high_water_ids"])

    def test_high_water_monotonic_and_cursor_monotonic_backward(self):
        base = self._dt(2026, 8, 18, 13, 0, 0, tzinfo=self._tzc)
        d1 = [self._at(1000 + k, base - self._td(hours=4 * k)) for k in range(12)]
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            hw_prev, cur_prev = "", "9"
            feed = list(d1)
            for day in range(3):
                if day > 0:  # inject a new top job each subsequent day
                    feed = [self._at(8000 + day, base + self._td(days=day))] + feed
                _r, _ = self._day(sp, feed, base + self._td(days=day), cap=5)
                st = json.loads(Path(sp).read_text())
                if hw_prev:
                    self.assertGreaterEqual(st["high_water"], hw_prev)   # never decreases
                    self.assertLessEqual(st["cursor_date"], cur_prev)    # never increases
                hw_prev, cur_prev = st["high_water"], st["cursor_date"]

    def test_no_duplicate_opportunities_across_head_deep_overlap(self):
        # Head and deep share seen_ids: a job on the head/deep boundary is emitted once.
        base = self._dt(2026, 8, 18, 13, 0, 0, tzinfo=self._tzc)
        d1 = [self._at(1000 + k, base - self._td(hours=2 * k)) for k in range(8)]
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            self._day(sp, d1, base, cap=4)
            new = [self._at(7001, base + self._td(days=1))]
            r2, _ = self._day(sp, new + d1, base + self._td(days=1), cap=8)
            ids = [j["_fantastic_internal_id"] for j in r2.jobs]
            self.assertEqual(len(ids), len(set(ids)))              # no duplicates emitted


class FantasticAcquireModeGatingTests(unittest.TestCase):
    """The acquire mode gates the two passes so the top-up loop can bill the head
    (top-of-feed) query at most once per run: slice 1 = head_then_deep, later = deep."""

    def _seed(self, sp):
        Path(sp).write_text(json.dumps({
            "schema": fja._CONTINUATION_SCHEMA, "source": "linkedin",
            "cursor_date": "2026-08-18T03:00:10", "high_water": "2026-08-18T03:00:18",
            "high_water_ids": ["1018"], "boundary_ids": ["1010"],
        }), encoding="utf-8")

    def test_deep_mode_never_issues_a_top_of_feed_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            self._seed(sp)
            with mock.patch.object(config, "FANTASTIC_JOBS_ACQUIRE_MODE", "deep"):
                _r, captured = _run(_feed(FEED), sp, cap=3, now=_FIXED_NOW)
            # Every request is deep (carries date_posted_lt) -> no fresh-edge head query.
            self.assertTrue(captured)
            self.assertTrue(all("date_posted_lt" in p for p in captured))

    def test_head_mode_only_issues_top_of_feed_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "c.json")
            self._seed(sp)
            with mock.patch.object(config, "FANTASTIC_JOBS_ACQUIRE_MODE", "head"):
                _r, captured = _run(_feed(FEED), sp, cap=3, now=_FIXED_NOW)
            self.assertTrue(captured)
            self.assertTrue(all("date_posted_lt" not in p for p in captured))


if __name__ == "__main__":
    unittest.main()
