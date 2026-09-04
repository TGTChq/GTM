"""title_advanced acquisition: ONE Boolean OR-expression over the whole role
catalog (benchmark parity). Verifies 118/118 coverage, single billed stream with
zero cross-query overlap, precedence over per-family title targeting, config
override, and single-stream continuation cursor reuse."""
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

from role_catalog import DEFAULT_ACQUISITION_ROLES


class _Resp:
    def __init__(self, rows):
        self.status_code = 200
        self.headers = {"x-api-jobs-remaining": "19000", "x-api-requests-remaining": "9000"}
        self._rows = rows

    def json(self):
        return self._rows


def _rec(i, title, dt):
    return {"id": str(i), "title": title, "organization": f"Co {i}", "source": "linkedin",
            "date_posted": dt, "countries_derived": ["United States"],
            "employment_type": ["FULL_TIME"], "org_linkedin_headcount": 100}


D = {n: f"2026-08-18T03:00:{n:02d}" for n in range(1, 40)}


def _run(http_get, *, cap=50, advanced=True, targeting=False, expression="",
         state_path=None, continuation=False):
    base = dict(
        FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k", FANTASTIC_JOBS_BASE_URL="https://x",
        FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_WELLFOUND_LIMIT=0, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
        FANTASTIC_JOBS_LINKEDIN_LIMIT=cap, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=cap,
        FANTASTIC_JOBS_TIME_FRAME="24h", FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=50,
        FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=90, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=20,
        FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=True,
        FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
        FANTASTIC_JOBS_HEADCOUNT_MAX=1000,
        FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME", FANTASTIC_JOBS_EXCLUDE_AGENCY=True,
        FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED=advanced,
        FANTASTIC_JOBS_TITLE_ADVANCED_EXPRESSION=expression,
        FANTASTIC_JOBS_TITLE_TARGETING_ENABLED=targeting,
        FANTASTIC_JOBS_TITLE_FAMILIES=("Account Executive", "Recruiter"),
        FANTASTIC_JOBS_CONTINUATION_ENABLED=continuation,
        FANTASTIC_JOBS_CONTINUATION_STATE_PATH=state_path or "",
    )
    with mock.patch.multiple(config, **base), mock.patch.object(fja, "datetime", _FrozenDT):
        return fja.run_fantastic_jobs_acquisition(http_get=http_get)


class TitleAdvancedExpressionTests(unittest.TestCase):
    def test_expression_covers_every_catalog_role_exactly_once(self):
        roles = sorted({str(r).strip() for r in DEFAULT_ACQUISITION_ROLES if str(r).strip()})
        expr = fja._title_advanced_expression()
        # every catalog role contributes its own OR term -> full coverage. Asserted
        # against the catalog rather than a pinned count, so the invariant holds
        # across catalog changes instead of going stale on each one.
        for r in roles:
            self.assertIn(fja._title_advanced_term(r), expr)
        self.assertEqual(expr.count("|") + 1, len(roles))   # one term per role, no dupes
        self.assertLess(len(expr), 3500)                    # fits API query-length budget

    def test_multiword_roles_are_single_quoted_phrases(self):
        self.assertEqual(fja._title_advanced_term("Account Executive"), "'account executive'")
        self.assertEqual(fja._title_advanced_term("Recruiter"), "recruiter")
        # slashes/hyphens normalized to spaces (then quoted)
        self.assertEqual(fja._title_advanced_term("AP/AR Specialist"), "'ap ar specialist'")


class TitleAdvancedAcquisitionTests(unittest.TestCase):
    def test_single_stream_sends_title_advanced_not_per_family_title(self):
        calls = []

        def hg(url, headers, params, timeout):
            calls.append(dict(params))
            off = int(params.get("offset", 0)); lim = int(params.get("limit", 100))
            rows = [(f"j-{n}", D[n]) for n in range(20, 0, -1)][off:off + lim]
            return _Resp([_rec(i, "Account Executive", dt) for i, dt in rows])

        res = _run(hg, cap=5)
        self.assertTrue(len(res.jobs) > 0)
        # exactly one logical stream: title_advanced present, per-family title absent
        self.assertTrue(all("title_advanced" in c for c in calls))
        self.assertTrue(all("title" not in c for c in calls))
        self.assertTrue(all(c["source"] == "linkedin" for c in calls))
        # the expression carries the full catalog union
        from role_catalog import DEFAULT_SEARCH_ROLES
        self.assertEqual(calls[0]["title_advanced"].count("|") + 1, len(DEFAULT_SEARCH_ROLES))
        # ICP filters + acquisition-time headcount bounds preserved
        self.assertEqual(calls[0]["organization_headcount_gte"], 25)
        self.assertEqual(calls[0]["organization_headcount_lt"], 1000)
        self.assertEqual(calls[0]["ai_employment_type"], "FULL_TIME")
        self.assertEqual(calls[0]["organization_agency"], "exclude")
        self.assertEqual(res.metadata["title_advanced"]["expression_chars"],
                         len(calls[0]["title_advanced"]))

    def test_takes_precedence_over_per_family_targeting(self):
        calls = []

        def hg(url, headers, params, timeout):
            calls.append(dict(params))
            return _Resp([_rec("j-1", "Account Executive", D[9])])

        # BOTH enabled -> title_advanced wins (single stream, no per-family title)
        _run(hg, cap=5, advanced=True, targeting=True)
        self.assertTrue(all("title_advanced" in c for c in calls))
        self.assertTrue(all("title" not in c for c in calls))

    def test_config_override_expression_is_used_verbatim(self):
        calls = []

        def hg(url, headers, params, timeout):
            calls.append(dict(params))
            return _Resp([])

        _run(hg, cap=5, expression="recruiter | 'account executive'")
        self.assertEqual(calls[0]["title_advanced"], "recruiter | 'account executive'")

    def test_single_stream_continuation_cursor_saved_and_resumed(self):
        def feed(http_recs):
            def hg(url, headers, params, timeout):
                lt = params.get("date_posted_lt")
                off = int(params.get("offset", 0)); lim = int(params.get("limit", 100))
                rows = [r for r in http_recs if lt is None or r[1] < lt]
                rows = sorted(rows, key=lambda r: (r[1], r[0]), reverse=True)[off:off + lim]
                return _Resp([_rec(i, "Account Executive", dt) for i, dt in rows])
            return hg

        recs = [(f"j-{n}", D[n]) for n in range(20, 0, -1)]
        with tempfile.TemporaryDirectory() as tmp:
            sp = str(Path(tmp) / "cont.json")
            r1 = _run(feed(recs), cap=4, state_path=sp, continuation=True)
            state = json.loads(Path(sp).read_text(encoding="utf-8"))
            self.assertEqual(state.get("source"), "linkedin")   # single-stream, not title_families
            self.assertNotIn("families", state)
            first = {j["_fantastic_internal_id"] for j in r1.jobs}
            # run 2 resumes strictly OLDER -> no overlap with run 1
            r2 = _run(feed(recs), cap=4, state_path=sp, continuation=True)
            second = {j["_fantastic_internal_id"] for j in r2.jobs}
            self.assertFalse(first & second)


if __name__ == "__main__":
    unittest.main()
