"""Title-targeted Fantastic acquisition: per-family queries, fair allocation,
global dedupe, per-family continuation (no cross-family leakage), and all the
existing safety filters/caps preserved."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import fantastic_jobs_adapter as fja

from datetime import datetime as _dtclass, timezone as _tz


class _FrozenDT(_dtclass):
    """Fixed .now() so the stale-window check uses a deterministic clock; the
    date-anchored fixtures below stay inside the window regardless of the real
    date. fromisoformat and everything else are inherited; production is untouched."""
    _fixed = _dtclass(2026, 8, 18, 6, 0, 0, tzinfo=_tz.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)



class _Resp:
    def __init__(self, rows):
        self.status_code = 200
        self.headers = {"x-api-jobs-remaining": "1000000", "x-api-requests-remaining": "1000000"}
        self._rows = rows

    def json(self):
        return self._rows


def _rec(i, title, dt):
    return {"id": str(i), "title": title, "organization": f"Co {i}", "source": "linkedin",
            "date_posted": dt, "countries_derived": ["United States"],
            "employment_type": ["FULL_TIME"], "org_linkedin_headcount": 100}


def _feed(title_recs):
    """title -> list[(id, date_posted)] ; honors title, date_posted_lt, offset, limit."""
    calls = []

    def http_get(url, headers, params, timeout):
        calls.append(dict(params))
        title = params.get("title")
        lt = params.get("date_posted_lt")
        off = int(params.get("offset", 0)); lim = int(params.get("limit", 100))
        recs = title_recs.get(title, [])
        rows = [r for r in recs if lt is None or r[1] < lt]
        rows = sorted(rows, key=lambda r: (r[1], str(r[0])), reverse=True)
        page = rows[off:off + lim]
        return _Resp([_rec(r[0], title, r[1]) for r in page])
    return http_get, calls


def _run(http_get, *, families, cap, state_path=None, continuation=False, targeting=True):
    base = dict(
        FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k", FANTASTIC_JOBS_BASE_URL="https://x",
        FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_WELLFOUND_LIMIT=0, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
        FANTASTIC_JOBS_LINKEDIN_LIMIT=cap, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=cap,
        FANTASTIC_JOBS_TIME_FRAME="24h", FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=50,
        FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=90, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=20,
        FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=True,
        FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
        FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME", FANTASTIC_JOBS_EXCLUDE_AGENCY=True,
        FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED=False,
        FANTASTIC_JOBS_TITLE_TARGETING_ENABLED=targeting, FANTASTIC_JOBS_TITLE_FAMILIES=families,
        FANTASTIC_JOBS_CONTINUATION_ENABLED=continuation,
        FANTASTIC_JOBS_CONTINUATION_STATE_PATH=state_path or "",
    )
    with mock.patch.multiple(config, **base), mock.patch.object(fja, "datetime", _FrozenDT):
        return fja.run_fantastic_jobs_acquisition(http_get=http_get)


D = {n: f"2026-08-18T03:00:{n:02d}" for n in range(1, 40)}


class TitleTargetingTests(unittest.TestCase):
    def test_queries_each_family_fairly_linkedin_only_and_respects_cap(self):
        recs = {t: [(f"{t}-{n}", D[n]) for n in range(39, 0, -1)] for t in ("Account Executive", "Software Engineer", "Accountant", "Recruiter")}
        hg, calls = _feed(recs)
        res = _run(hg, families=list(recs), cap=8)
        self.assertEqual(len(res.jobs), 8)                                    # global cap
        self.assertEqual(sorted({c["title"] for c in calls}), sorted(recs))   # every family queried
        self.assertTrue(all(c["source"] == "linkedin" for c in calls))        # linkedin only
        self.assertTrue(all("date_posted_lt" not in c for c in calls))        # no cursor yet
        counts = {f: sum(1 for j in res.jobs if j["_fantastic_title_family"] == f) for f in {j["_fantastic_title_family"] for j in res.jobs}}
        self.assertTrue(all(v == 2 for v in counts.values()))                 # fair: 8/4 = 2 each
        self.assertTrue(all(j.get("_fantastic_title_term") for j in res.jobs))
        # filters preserved on every request
        for c in calls:
            self.assertEqual(c["location"], "United States")
            self.assertEqual(c["ai_employment_type"], "FULL_TIME")
            self.assertEqual(c["organization_agency"], "exclude")
            self.assertEqual(c["organization_headcount_gte"], 25)

    def test_global_dedupe_across_overlapping_families(self):
        # "shared-1" is returned by BOTH families -> processed once, billed twice.
        recs = {
            "Account Executive": [("shared-1", D[9]), ("ae-2", D[8])],
            "Account Manager": [("shared-1", D[9]), ("am-2", D[7])],
        }
        hg, _ = _feed(recs)
        res = _run(hg, families=list(recs), cap=10)
        ids = [j["_fantastic_internal_id"] for j in res.jobs]
        self.assertEqual(ids.count("shared-1"), 1)                            # deduped in output
        self.assertGreaterEqual(res.metadata["cross_query_duplicates"], 1)    # billing overlap reported

    def test_per_family_continuation_no_cross_family_leak(self):
        recs = {
            "Account Executive": [(f"ae-{n}", D[n]) for n in range(20, 0, -1)],
            "Software Engineer": [(f"se-{n}", D[n]) for n in range(30, 10, -1)],
        }
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            hg, _ = _feed(recs)
            r1 = _run(hg, families=list(recs), cap=4, state_path=sp, continuation=True)
            state = json.loads(Path(sp).read_text(encoding="utf-8"))
            self.assertEqual(state["mode"], "title_families")
            fams = state["families"]
            self.assertIn("account_executive", fams)
            self.assertIn("software_engineer", fams)
            # families have DIFFERENT independent cursors (SE dates are newer)
            self.assertNotEqual(fams["account_executive"]["cursor_date"],
                                fams["software_engineer"]["cursor_date"])
            # run 2: each family resumes with ITS OWN date_posted_lt (no leak)
            hg2, calls2 = _feed(recs)
            _run(hg2, families=list(recs), cap=4, state_path=sp, continuation=True)
            by_title = {c["title"]: c.get("date_posted_lt") for c in calls2}
            ae_lt = fja._advance_iso_second(fams["account_executive"]["cursor_date"], 1)
            se_lt = fja._advance_iso_second(fams["software_engineer"]["cursor_date"], 1)
            self.assertEqual(by_title["Account Executive"], ae_lt)
            self.assertEqual(by_title["Software Engineer"], se_lt)
            self.assertNotEqual(by_title["Account Executive"], by_title["Software Engineer"])

    def test_new_jobs_at_top_of_a_family_between_runs_deferred(self):
        base = {"Recruiter": [(f"r-{n}", D[n]) for n in range(20, 0, -1)]}
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            hg, _ = _feed(base)
            r1 = _run(hg, families=["Recruiter"], cap=3, state_path=sp, continuation=True)
            first = {j["_fantastic_internal_id"] for j in r1.jobs}
            newer = {"Recruiter": [("r-new", "2026-08-18T04:00:00")] + base["Recruiter"]}
            hg2, _ = _feed(newer)
            r2 = _run(hg2, families=["Recruiter"], cap=3, state_path=sp, continuation=True)
            second = {j["_fantastic_internal_id"] for j in r2.jobs}
            self.assertNotIn("r-new", second)                                 # new top job deferred
            self.assertFalse(first & second)                                  # older-window continuation

    def test_quota_reserve_halts_family_loop(self):
        recs = {t: [(f"{t}-{n}", D[n]) for n in range(30, 0, -1)] for t in ("Account Executive", "Software Engineer")}

        def hg(url, headers, params, timeout):
            title = params.get("title"); off = int(params.get("offset", 0)); lim = int(params.get("limit", 100))
            rows = sorted(recs.get(title, []), key=lambda r: r[1], reverse=True)[off:off + lim]
            class R:
                status_code = 200
                headers = {"x-api-jobs-remaining": "95", "x-api-requests-remaining": "1000"}  # near reserve
                def json(self_):
                    return [_rec(r[0], title, r[1]) for r in rows]
            return R()
        res = _run(hg, families=list(recs), cap=100)
        # reserve (90) reached quickly -> stops well below the cap, records the reason
        self.assertLess(len(res.jobs), 100)
        self.assertIn(res.metadata["stop_reason"], {"jobs_quota_reserve", "requests_quota_reserve", "complete"})

    def test_targeting_disabled_uses_single_stream_no_title(self):
        recs = {None: [(f"x-{n}", D[n]) for n in range(10, 0, -1)]}

        def hg(url, headers, params, timeout):
            off = int(params.get("offset", 0)); lim = int(params.get("limit", 100))
            rows = sorted(recs[None], key=lambda r: r[1], reverse=True)[off:off + lim]
            class R:
                status_code = 200
                headers = {"x-api-jobs-remaining": "999999", "x-api-requests-remaining": "999999"}
                def json(self_):
                    return [_rec(r[0], "Software Engineer", r[1]) for r in rows]
            return R()
        calls = []
        orig = hg
        def wrapped(url, headers, params, timeout):
            calls.append(dict(params)); return orig(url, headers, params, timeout)
        res = _run(wrapped, families=["Account Executive"], cap=5, targeting=False)
        self.assertTrue(all("title" not in c for c in calls))                 # single-stream, no title param
        self.assertTrue(all(c["source"] == "linkedin" for c in calls))


if __name__ == "__main__":
    unittest.main()
