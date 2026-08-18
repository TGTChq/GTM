"""Cross-run Fantastic continuation cursor (date_posted keyset).

The Direct API feed is date_posted DESC with no stable `order`/id-keyset, but
`date_posted_lt` (proven live) yields non-overlapping windows. These tests pin:
  * disabled by default -> no cursor param, no state written (baseline unchanged);
  * first run sets cursor_date=oldest, high_water=newest, boundary ids;
  * a resumed run sends date_posted_lt = cursor + 1s, re-includes and DEDUPES the
    boundary second (no re-count, no skip), and never re-acquires the deep prefix;
  * new jobs entering the top between runs are NOT skipped/lost -- the older-window
    resume ignores them and high_water preserves them for an incremental run.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import fantastic_jobs_adapter as fja


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


def _run(http_get, state_path, cap=3, enabled=True):
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
        FANTASTIC_JOBS_CONTINUATION_ENABLED=enabled,
        FANTASTIC_JOBS_CONTINUATION_STATE_PATH=state_path,
    )
    captured = []
    orig = http_get

    def wrapped(url, headers, params, timeout):
        captured.append(dict(params))
        return orig(url, headers, params, timeout)
    with mock.patch.multiple(config, **base):
        res = fja.run_fantastic_jobs_acquisition(http_get=wrapped)
    return res, captured


# distinct-second feed, newest first
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
            r1, _ = _run(_feed(FEED), sp, cap=3)
            first_ids = {j["_fantastic_internal_id"] for j in r1.jobs}          # {1020,1019,1018}
            r2, captured2 = _run(_feed(FEED), sp, cap=3)
            # resumed with date_posted_lt = cursor(:18) + 1s = :19
            self.assertTrue(any(p.get("date_posted_lt") == D[19] for p in captured2))
            second_ids = {j["_fantastic_internal_id"] for j in r2.jobs}
            self.assertFalse(first_ids & second_ids)                            # no re-acquire (dedup+cursor)
            self.assertNotIn("1018", second_ids)                               # boundary re-fetched but deduped
            self.assertTrue(all(int(i) < 1018 for i in second_ids))            # strictly older
            state2 = json.loads(Path(sp).read_text(encoding="utf-8"))
            self.assertLess(state2["cursor_date"], D[18])                       # cursor advanced older
            self.assertEqual(state2["high_water"], D[20])                       # high_water preserved

    def test_new_jobs_at_top_between_runs_are_not_skipped_and_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            r1, _ = _run(_feed(FEED), sp, cap=3)                                # acquires :20,:19,:18
            # New jobs enter the TOP (newer than any prior) between runs.
            newer = [_rec(2001, "2026-08-18T03:05:00"), _rec(2002, "2026-08-18T03:06:00")]
            r2, _ = _run(_feed(newer + FEED), sp, cap=3)
            second_ids = {j["_fantastic_internal_id"] for j in r2.jobs}
            # The older-window resume must NOT grab the new top jobs...
            self.assertNotIn("2001", second_ids)
            self.assertNotIn("2002", second_ids)
            # ...and they are preserved for a future incremental (high_water < their date).
            state2 = json.loads(Path(sp).read_text(encoding="utf-8"))
            self.assertLess(state2["high_water"], "2026-08-18T03:05:00")
            self.assertTrue(all(int(i) < 1018 for i in second_ids))            # resume continued older


if __name__ == "__main__":
    unittest.main()
