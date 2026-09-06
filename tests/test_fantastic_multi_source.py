"""Fantastic multi-source union: reachability, ONE shared run budget, attribution.

Before this architecture, Wellfound and Y Combinator lived in the final ``else`` of
a mutually-exclusive ``if title_advanced_active / elif used_title_families / else``
chain, so with title_advanced active -- i.e. always, in production -- their segments
were unreachable regardless of their configured limits. Sources are now independent
segments in a plan.

The load-bearing invariant is the credit one:

    SUM of provider-BILLED rows across ALL source segments  <=  governor run_cap

which is enforced by granting each segment a budget BEFORE any request is issued,
measured against ``quota.jobs_consumed`` (rows the provider actually bills) rather
than kept jobs. Measuring by kept jobs -- the previous behaviour -- let a dup-heavy
segment overspend, because the provider bills rows we later dedupe or reject.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import fantastic_jobs_adapter as fja
from fantastic_jobs_adapter import _SourceBudgetAllocator, _SourceSegment, build_source_plan


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, rows, status=200):
        self.status_code = status
        self.headers = {"x-api-jobs-remaining": "1000000", "x-api-requests-remaining": "1000000"}
        self._rows = rows

    def json(self):
        return self._rows


def _rec(i, source, dt="2026-08-18T03:00:10", org=None):
    return {"id": str(i), "title": "Software Engineer", "organization": org or f"Co {i}",
            "source": source, "date_posted": dt, "countries_derived": ["United States"],
            "employment_type": ["FULL_TIME"], "org_linkedin_headcount": 100}


def _multi_feed(rows_by_source, *, fail=None, status=None, malformed=()):
    """Serves rows per source. ``/v1/active-ats`` is keyed 'ats'; JB segments are
    keyed by their ``source`` request param. Records every dispatched call."""
    calls = []

    def http_get(url, headers, params, timeout):
        key = "ats" if url.endswith("/v1/active-ats") else str(params.get("source") or "")
        calls.append({"url": url, "source": key, "limit": int(params.get("limit", 0)),
                      "offset": int(params.get("offset", 0)), "params": dict(params)})
        if fail and key in fail:
            raise fail[key]
        if status and key in status:
            return _Resp([], status=status[key])
        if key in malformed:
            return _Resp([{"garbage": True}, {"also": "bad"}])
        rows = list(rows_by_source.get(key, []))
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 100))
        return _Resp(rows[off:off + lim])

    http_get.calls = calls
    return http_get


BASE_CFG = dict(
    FANTASTIC_WINDOW_SLICING_ENABLED=False,
    FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
    FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
    FANTASTIC_JOBS_TIME_FRAME="24h", FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=50,
    FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=0,
    FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=True,
    FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
    FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME", FANTASTIC_JOBS_EXCLUDE_AGENCY=True,
    FANTASTIC_JOBS_CONTINUATION_ENABLED=False,
    FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=False,
    FANTASTIC_MONTHLY_GOVERNOR_ENABLED=False,
    FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=False,
    FANTASTIC_FUNCTION_AWARE_UPSTREAM_DEDUPE_ENABLED=False,
    FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED=True,      # production mode
    FANTASTIC_JOBS_TITLE_TARGETING_ENABLED=False,
    FANTASTIC_ATS_SOURCE_ENABLED=False,
    FANTASTIC_WELLFOUND_SOURCE_ENABLED=False,
    FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=False,
    FANTASTIC_SOURCE_ALLOCATION="sequential",
    FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_LINKEDIN_LIMIT=0,
    FANTASTIC_JOBS_WELLFOUND_LIMIT=0, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
    FANTASTIC_JOBS_MAX_JOBS_PER_RUN=1000, FANTASTIC_JOBS_RUN_SLICE_CAP=0,
)


def run(http_get, **over):
    cfg = dict(BASE_CFG)
    cfg.update(over)
    with mock.patch.multiple(config, **cfg):
        return fja.run_fantastic_jobs_acquisition(http_get=http_get)


def _sources_called(http_get):
    return sorted({c["source"] for c in http_get.calls})


# --------------------------------------------------------------------------
# Allocator (pure unit tests -- no HTTP)
# --------------------------------------------------------------------------
class AllocatorTest(unittest.TestCase):
    @staticmethod
    def _segs(**limits):
        return [_SourceSegment(key=k, label=f"fantastic_jobs_{k}", endpoint="/e",
                               limit=v, accept=None) for k, v in limits.items()]

    def test_sequential_matches_legacy_first_come_semantics(self):
        segs = self._segs(ats=6000, linkedin=6000)
        a = _SourceBudgetAllocator(3164, segs, "sequential")
        self.assertEqual(a.grant(segs[0], 0), 3164)      # first segment may take it all
        a.settle(segs[0], 3164)
        self.assertEqual(a.grant(segs[1], 3164), 0)      # nothing left
        self.assertTrue(a.to_dict()["invariant_ok"])

    def test_fair_share_prevents_starvation(self):
        segs = self._segs(ats=6000, linkedin=6000, wellfound=6000, ycombinator=6000)
        a = _SourceBudgetAllocator(3164, segs, "fair_share")
        billed = 0
        grants = []
        for s in segs:
            g = a.grant(s, billed)
            grants.append(g)
            a.settle(s, g)
            billed += g
        self.assertTrue(all(g > 0 for g in grants), f"no source may be starved: {grants}")
        self.assertLessEqual(sum(grants), 3164)

    def test_fair_share_reclaims_unspent_allocation(self):
        segs = self._segs(ats=6000, linkedin=6000)
        a = _SourceBudgetAllocator(1000, segs, "fair_share")
        g1 = a.grant(segs[0], 0)
        self.assertEqual(g1, 500)
        a.settle(segs[0], 100)                            # spent far less than reserved
        g2 = a.grant(segs[1], 100)
        self.assertEqual(g2, 900, "unused reservation must cascade to the next source")
        a.settle(segs[1], 900)
        self.assertLessEqual(sum(a.spent.values()), 1000)

    def test_small_limit_source_releases_the_remainder(self):
        segs = self._segs(ats=10, linkedin=6000)
        a = _SourceBudgetAllocator(1000, segs, "fair_share")
        self.assertEqual(a.grant(segs[0], 0), 10)         # capped by its own limit
        a.settle(segs[0], 10)
        self.assertEqual(a.grant(segs[1], 10), 990)       # the rest is available

    def test_budget_matrix_invariant(self):
        """sum(grants) <= run_cap for every cap x policy x source combination."""
        for cap in (0, 1, 100, 3164, 5000, 8000, 12000):
            for policy in ("sequential", "fair_share"):
                for limits in ({"linkedin": 6000},
                               {"ats": 6000, "linkedin": 6000},
                               {"ats": 6000, "linkedin": 6000, "wellfound": 50},
                               {"ats": 6000, "linkedin": 6000, "wellfound": 50, "ycombinator": 40}):
                    with self.subTest(cap=cap, policy=policy, n=len(limits)):
                        segs = self._segs(**limits)
                        a = _SourceBudgetAllocator(cap, segs, policy)
                        billed = 0
                        for s in segs:
                            g = a.grant(s, billed)
                            self.assertLessEqual(g, s.limit)
                            self.assertGreaterEqual(g, 0)
                            a.settle(s, g)
                            billed += g
                        self.assertLessEqual(billed, cap,
                                             "TOTAL billed must never exceed run_cap")

    def test_zero_and_single_source(self):
        self.assertEqual(_SourceBudgetAllocator(100, [], "fair_share").to_dict()["granted_total"], 0)
        segs = self._segs(linkedin=6000)
        a = _SourceBudgetAllocator(500, segs, "fair_share")
        self.assertEqual(a.grant(segs[0], 0), 500, "one source may use the whole cap")


# --------------------------------------------------------------------------
# Source plan / reachability
# --------------------------------------------------------------------------
class SourcePlanTest(unittest.TestCase):
    def _plan(self, **over):
        cfg = dict(BASE_CFG)
        cfg.update(over)
        with mock.patch.multiple(config, **cfg):
            return [s.key for s in build_source_plan(title_advanced_active=True,
                                                     used_title_families=False)]

    def _segs(self, **over):
        cfg = dict(BASE_CFG)
        cfg.update(over)
        with mock.patch.multiple(config, **cfg):
            return {s.key: s for s in build_source_plan(title_advanced_active=True,
                                                        used_title_families=False)}

    def test_production_equivalent_plan(self):
        """ATS+LinkedIn config. LinkedIn IS planned (so it draws an allocator grant
        like every other source); `dispatch` records that the title_advanced mode
        owns its query shape and cursor."""
        segs = self._segs(FANTASTIC_ATS_SOURCE_ENABLED=True,
                          FANTASTIC_JOBS_ATS_LIMIT=6000,
                          FANTASTIC_JOBS_LINKEDIN_LIMIT=6000)
        self.assertEqual(sorted(segs), ["ats", "linkedin"])
        self.assertEqual(segs["linkedin"].dispatch, "title_advanced")
        self.assertEqual(segs["ats"].dispatch, "plan")

    def test_wellfound_and_yc_need_both_keys(self):
        self.assertEqual(self._plan(FANTASTIC_JOBS_WELLFOUND_LIMIT=50), [])      # limit only
        self.assertEqual(self._plan(FANTASTIC_WELLFOUND_SOURCE_ENABLED=True), [])  # flag only
        self.assertEqual(self._plan(FANTASTIC_WELLFOUND_SOURCE_ENABLED=True,
                                    FANTASTIC_JOBS_WELLFOUND_LIMIT=50), ["wellfound"])

    def test_all_four_sources_plannable(self):
        self.assertEqual(
            self._plan(FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=6000,
                       FANTASTIC_JOBS_LINKEDIN_LIMIT=6000,
                       FANTASTIC_WELLFOUND_SOURCE_ENABLED=True, FANTASTIC_JOBS_WELLFOUND_LIMIT=50,
                       FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=True, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=40),
            ["ats", "linkedin", "wellfound", "ycombinator"])

    def test_linkedin_dispatch_marker_tracks_the_active_mode(self):
        cfg = dict(BASE_CFG); cfg["FANTASTIC_JOBS_LINKEDIN_LIMIT"] = 6000
        with mock.patch.multiple(config, **cfg):
            for ta, fam, expected in ((False, False, "plan"),
                                      (True, False, "title_advanced"),
                                      (False, True, "title_families")):
                with self.subTest(title_advanced=ta, families=fam):
                    plan = build_source_plan(title_advanced_active=ta, used_title_families=fam)
                    li = next(s for s in plan if s.key == "linkedin")
                    self.assertEqual(li.dispatch, expected)
            cfg2 = dict(cfg); cfg2["FANTASTIC_JOBS_LINKEDIN_LIMIT"] = 0
        with mock.patch.multiple(config, **cfg2):
            self.assertEqual([s.key for s in build_source_plan(
                title_advanced_active=True, used_title_families=False)], [],
                "limit 0 keeps LinkedIn out of the plan entirely")


# --------------------------------------------------------------------------
# Reachability THROUGH the adapter (the actual regression)
# --------------------------------------------------------------------------
class ReachabilityTest(unittest.TestCase):
    def test_wellfound_yc_reachable_while_title_advanced_active(self):
        """The exact case that was dead code before this change."""
        feed = _multi_feed({"linkedin": [_rec(i, "linkedin") for i in range(1, 6)],
                            "wellfound": [_rec(100 + i, "wellfound") for i in range(1, 4)],
                            "ycombinator": [_rec(200 + i, "ycombinator") for i in range(1, 3)]})
        res = run(feed, FANTASTIC_JOBS_LINKEDIN_LIMIT=50,
                  FANTASTIC_WELLFOUND_SOURCE_ENABLED=True, FANTASTIC_JOBS_WELLFOUND_LIMIT=50,
                  FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=True, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=50)
        self.assertEqual(_sources_called(feed), ["linkedin", "wellfound", "ycombinator"])
        srcs = {j["_acquisition_source"] for j in res.jobs}
        self.assertTrue(any("wellfound" in s for s in srcs))
        self.assertTrue(any("ycombinator" in s for s in srcs))

    def test_each_source_alone(self):
        for key, flag, limit_key in (
                ("wellfound", "FANTASTIC_WELLFOUND_SOURCE_ENABLED", "FANTASTIC_JOBS_WELLFOUND_LIMIT"),
                ("ycombinator", "FANTASTIC_YCOMBINATOR_SOURCE_ENABLED", "FANTASTIC_JOBS_YCOMBINATOR_LIMIT")):
            with self.subTest(source=key):
                feed = _multi_feed({key: [_rec(i, key) for i in range(1, 4)]})
                res = run(feed, **{flag: True, limit_key: 50})
                self.assertEqual(_sources_called(feed), [key])
                self.assertEqual(len(res.jobs), 3)

    def test_all_limits_zero_issues_no_request(self):
        feed = _multi_feed({"linkedin": [_rec(1, "linkedin")]})
        res = run(feed)
        self.assertEqual(feed.calls, [])
        self.assertEqual(res.jobs, [])

    def test_one_source_zero_others_positive(self):
        feed = _multi_feed({"linkedin": [_rec(i, "linkedin") for i in range(1, 4)],
                            "wellfound": [_rec(100, "wellfound")]})
        run(feed, FANTASTIC_JOBS_LINKEDIN_LIMIT=50,
            FANTASTIC_WELLFOUND_SOURCE_ENABLED=True, FANTASTIC_JOBS_WELLFOUND_LIMIT=0)
        self.assertEqual(_sources_called(feed), ["linkedin"])


# --------------------------------------------------------------------------
# THE credit invariant, end to end
# --------------------------------------------------------------------------
class RunBudgetInvariantTest(unittest.TestCase):
    def test_total_billed_never_exceeds_run_cap_across_sources(self):
        for cap in (1, 5, 25, 100):
            with self.subTest(run_cap=cap):
                feed = _multi_feed({
                    "ats": [_rec(1000 + i, "ats") for i in range(60)],
                    "linkedin": [_rec(2000 + i, "linkedin") for i in range(60)],
                    "wellfound": [_rec(3000 + i, "wellfound") for i in range(60)],
                    "ycombinator": [_rec(4000 + i, "ycombinator") for i in range(60)]})
                res = run(feed, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=240,
                          FANTASTIC_JOBS_RUN_SLICE_CAP=cap,
                          FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=60,
                          FANTASTIC_JOBS_LINKEDIN_LIMIT=60,
                          FANTASTIC_WELLFOUND_SOURCE_ENABLED=True, FANTASTIC_JOBS_WELLFOUND_LIMIT=60,
                          FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=True, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=60,
                          FANTASTIC_SOURCE_ALLOCATION="fair_share")
                billed = res.metadata["jobs_quota_consumed"]
                self.assertLessEqual(billed, cap,
                                     f"billed {billed} exceeded run_cap {cap}")
                self.assertTrue(res.metadata["source_allocation"]["invariant_ok"])
                # And no single request may ask for more than the remaining budget.
                self.assertLessEqual(sum(c["limit"] for c in feed.calls), cap * 2)

    def test_duplicate_heavy_source_cannot_overspend_the_next(self):
        """The precise bug: billing is per RETURNED row, so a segment whose rows are
        all duplicates still consumes budget. Measuring by KEPT jobs let the next
        source spend the difference again."""
        dupes = [_rec(9000, "ats") for _ in range(20)]        # same id 20x -> 1 kept, 20 billed
        feed = _multi_feed({"ats": dupes,
                            "linkedin": [_rec(2000 + i, "linkedin") for i in range(40)]})
        res = run(feed, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=60, FANTASTIC_JOBS_RUN_SLICE_CAP=20,
                  FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=20,
                  FANTASTIC_JOBS_LINKEDIN_LIMIT=40)
        self.assertLessEqual(res.metadata["jobs_quota_consumed"], 20)
        self.assertTrue(res.metadata["source_allocation"]["invariant_ok"])

    def test_minimal_run_cap_bills_at_most_one_row(self):
        """run_cap=0 is handled upstream (the governor stops the run before acquire),
        so the smallest budget the adapter can actually receive is 1."""
        feed = _multi_feed({"linkedin": [_rec(i, "linkedin") for i in range(10)]})
        res = run(feed, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=50, FANTASTIC_JOBS_RUN_SLICE_CAP=1,
                  FANTASTIC_JOBS_LINKEDIN_LIMIT=50)
        self.assertLessEqual(res.metadata["jobs_quota_consumed"], 1)
        self.assertTrue(all(c["limit"] <= 1 for c in feed.calls))

    def test_large_run_caps_hold_the_invariant(self):
        """The architecture must not assume a ~3k run."""
        for cap in (3164, 5000, 8000, 12000):
            with self.subTest(run_cap=cap):
                segs = [_SourceSegment(key=k, label=k, endpoint="/e", limit=6000, accept=None)
                        for k in ("ats", "linkedin", "wellfound", "ycombinator")]
                a = _SourceBudgetAllocator(cap, segs, "fair_share")
                billed = 0
                for s in segs:
                    g = a.grant(s, billed)
                    a.settle(s, g)
                    billed += g
                self.assertLessEqual(billed, cap)
                self.assertGreater(billed, 0)


# --------------------------------------------------------------------------
# Cross-source dedupe + attribution
# --------------------------------------------------------------------------
class PagingOffsetTest(unittest.TestCase):
    """Paging must be CONTIGUOUS, because two things depend on it.

    Regression: the offset was ``(page - 1) * want``, but ``want`` shrinks on the
    final page (``min(cap - returned, 100)``). With a cap of 250 that paged 0,
    100, then 2 * 50 = 100 AGAIN -- the provider billed 250 rows to deliver 200
    distinct ones, and rows 200-249 were never inspected. A governor grant is
    almost never a round multiple of 100, so this fired on real runs.

    It also silently corrupted the bootstrap cursor: the resume offset is saved
    as ``start + rows returned``, which only points at unseen inventory while
    paging is contiguous. Overlapping pages made the next run skip the gap.
    """

    def _run_capped(self, cap, available=400):
        feed = _multi_feed({"wellfound": [_rec(5000 + i, "wellfound")
                            for i in range(available)]})
        res = run(feed, FANTASTIC_WELLFOUND_SOURCE_ENABLED=True,
                  FANTASTIC_JOBS_WELLFOUND_LIMIT=cap,
                  FANTASTIC_JOBS_MAX_JOBS_PER_RUN=cap)
        return res, feed

    def test_partial_final_page_does_not_re_request_an_earlier_offset(self):
        res, feed = self._run_capped(250)
        self.assertEqual([(c["offset"], c["limit"]) for c in feed.calls],
                         [(0, 100), (100, 100), (200, 50)])
        self.assertEqual(res.metadata["jobs_quota_consumed"], 250)
        self.assertEqual(len(res.jobs), 250, "every billed row must be a new row")
        self.assertEqual(
            res.metadata["segments"]["fantastic_jobs_wellfound"]["duplicates"], 0)

    def test_pages_never_overlap_for_any_cap(self):
        for cap in (1, 50, 99, 100, 101, 150, 199, 200, 250, 301, 399):
            with self.subTest(cap=cap):
                res, feed = self._run_capped(cap)
                spans = [(c["offset"], c["offset"] + c["limit"]) for c in feed.calls]
                for (a_lo, a_hi), (b_lo, _) in zip(spans, spans[1:]):
                    self.assertEqual(a_hi, b_lo, f"gap/overlap in {spans}")
                ids = [j["_fantastic_internal_id"] for j in res.jobs]
                self.assertEqual(len(ids), len(set(ids)))
                # Billed rows all landed: no credit paid for a re-read prefix.
                self.assertEqual(res.metadata["jobs_quota_consumed"], len(ids))


class CrossSourceDedupeTest(unittest.TestCase):
    PAIRS = [("ats", "linkedin"), ("ats", "wellfound"), ("ats", "ycombinator"),
             ("linkedin", "wellfound"), ("linkedin", "ycombinator"),
             ("wellfound", "ycombinator")]

    def _run_pair(self, a, b, shared_id=7777):
        rows = {a: [_rec(shared_id, a), _rec(1, a)], b: [_rec(shared_id, b), _rec(2, b)]}
        feed = _multi_feed(rows)
        over = {"FANTASTIC_JOBS_MAX_JOBS_PER_RUN": 100}
        for key in (a, b):
            if key == "ats":
                over.update(FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=50)
            elif key == "linkedin":
                over.update(FANTASTIC_JOBS_LINKEDIN_LIMIT=50)
            elif key == "wellfound":
                over.update(FANTASTIC_WELLFOUND_SOURCE_ENABLED=True,
                            FANTASTIC_JOBS_WELLFOUND_LIMIT=50)
            else:
                over.update(FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=True,
                            FANTASTIC_JOBS_YCOMBINATOR_LIMIT=50)
        return run(feed, **over), feed

    def test_same_posting_from_two_sources_is_one_opportunity(self):
        for a, b in self.PAIRS:
            with self.subTest(pair=f"{a}<->{b}"):
                res, feed = self._run_pair(a, b)
                ids = [j["_fantastic_internal_id"] for j in res.jobs]
                self.assertEqual(len(ids), len(set(ids)), "no duplicate job survives")
                self.assertIn("7777", ids)
                self.assertEqual(ids.count("7777"), 1)
                xs = res.metadata.get("cross_source_duplicates", 0)
                self.assertGreaterEqual(xs, 1, "the overlap must be ATTRIBUTED, not just dropped")

    def test_same_job_same_source_is_an_intra_segment_duplicate(self):
        feed = _multi_feed({"linkedin": [_rec(5, "linkedin"), _rec(5, "linkedin")]})
        res = run(feed, FANTASTIC_JOBS_LINKEDIN_LIMIT=50)
        self.assertEqual(len(res.jobs), 1)
        self.assertEqual(res.metadata.get("cross_source_duplicates", 0), 0)

    def test_distinct_jobs_same_company_both_survive(self):
        feed = _multi_feed({"linkedin": [_rec(11, "linkedin", org="Acme"),
                                         _rec(12, "linkedin", org="Acme")]})
        res = run(feed, FANTASTIC_JOBS_LINKEDIN_LIMIT=50)
        self.assertEqual(len(res.jobs), 2)

    def test_attribution_reports_per_source_counters(self):
        feed = _multi_feed({"linkedin": [_rec(i, "linkedin") for i in range(1, 4)],
                            "wellfound": [_rec(100 + i, "wellfound") for i in range(1, 3)]})
        res = run(feed, FANTASTIC_JOBS_LINKEDIN_LIMIT=50,
                  FANTASTIC_WELLFOUND_SOURCE_ENABLED=True, FANTASTIC_JOBS_WELLFOUND_LIMIT=50)
        per = res.metadata["source_attribution"]["per_source"]
        self.assertIn("fantastic_jobs_linkedin", per)
        self.assertIn("fantastic_jobs_wellfound", per)
        for stats in per.values():
            for field in ("returned_billed", "unique_kept", "duplicates",
                          "cross_source_duplicates", "requests", "stop_reason"):
                self.assertIn(field, stats)
        self.assertNotIn("_first_seen", res.metadata, "transient id map must not persist")

    def test_allocation_summary_is_reported(self):
        feed = _multi_feed({"linkedin": [_rec(i, "linkedin") for i in range(1, 4)]})
        res = run(feed, FANTASTIC_JOBS_LINKEDIN_LIMIT=50, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=100)
        alloc = res.metadata["source_allocation"]
        # Explicitly named: `budget` is the pool this allocator distributes (NOT the
        # run cap), `granted` is permission, `billed` is what the provider charged.
        for field in ("policy", "budget", "granted", "billed", "granted_total",
                      "billed_total", "unspent_budget", "invariant_ok"):
            self.assertIn(field, alloc)
        self.assertNotIn("run_cap", alloc, "must not call a sub-budget the run cap")
        self.assertTrue(alloc["invariant_ok"])
        # Run-level reconciliation is a separate object measured against run_cap.
        acc = res.metadata["run_budget_accounting"]
        self.assertTrue(acc["segments_reconcile"])
        self.assertTrue(acc["within_run_cap"])
        self.assertEqual(acc["total_billed"],
                         acc["steady_billed"] + acc["bootstrap_billed"])


# --------------------------------------------------------------------------
# Failure isolation
# --------------------------------------------------------------------------
class FailureIsolationTest(unittest.TestCase):
    def _four_source_over(self):
        return dict(FANTASTIC_JOBS_MAX_JOBS_PER_RUN=200, FANTASTIC_JOBS_RUN_SLICE_CAP=0,
                    FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=50,
                    FANTASTIC_JOBS_LINKEDIN_LIMIT=50,
                    FANTASTIC_WELLFOUND_SOURCE_ENABLED=True, FANTASTIC_JOBS_WELLFOUND_LIMIT=50,
                    FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=True, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=50)

    def _rows(self):
        return {k: [_rec(i + off, k) for i in range(3)]
                for off, k in ((1000, "ats"), (2000, "linkedin"),
                               (3000, "wellfound"), (4000, "ycombinator"))}

    def test_one_source_exception_does_not_lose_the_others(self):
        feed = _multi_feed(self._rows(), fail={"ats": TimeoutError("timeout")})
        res = run(feed, **self._four_source_over())
        srcs = {j["_acquisition_source"] for j in res.jobs}
        self.assertTrue(any("linkedin" in s for s in srcs))
        self.assertTrue(any("wellfound" in s for s in srcs))
        self.assertTrue(any("ycombinator" in s for s in srcs))
        self.assertTrue(res.metadata["source_allocation"]["invariant_ok"])

    def test_server_error_is_isolated_to_the_failing_source(self):
        """5xx is a SOURCE problem: contained, other sources keep their results."""
        for code in (500, 502, 503):
            with self.subTest(status=code):
                feed = _multi_feed(self._rows(), status={"wellfound": code})
                res = run(feed, **self._four_source_over())
                segs = res.metadata["segments"]
                wf = segs.get("fantastic_jobs_wellfound", {})
                self.assertTrue(wf.get("error_code") or wf.get("stop_reason"),
                                "the failing source must carry the error")
                self.assertFalse(segs.get("fantastic_jobs_linkedin", {}).get("error_code"),
                                 "a healthy source must stay clean")
                self.assertTrue(any("linkedin" in j["_acquisition_source"] for j in res.jobs))
                self.assertTrue(res.metadata["source_allocation"]["invariant_ok"])

    def test_auth_and_rate_limit_abort_the_whole_run_by_design(self):
        """401/403/429 are ACCOUNT-level, not source-level.

        _request raises FantasticAuthError / FantasticQuotaError, which the outer
        handler catches -- deliberately stopping every remaining source rather than
        retrying doomed credentials (or hammering an account-wide rate limit) once
        per source. Documented here because it means a 429 on an early source
        starves the later ones for that run; the budget invariant still holds.
        """
        for code, reason in ((401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited")):
            with self.subTest(status=code):
                feed = _multi_feed(self._rows(), status={"ats": code})
                res = run(feed, **self._four_source_over())
                self.assertEqual(res.metadata["stop_reason"], reason)
                self.assertLessEqual(res.metadata["jobs_quota_consumed"], 200)

    def test_malformed_rows_do_not_corrupt_other_sources(self):
        feed = _multi_feed(self._rows(), malformed=("ycombinator",))
        res = run(feed, **self._four_source_over())
        self.assertTrue(any("linkedin" in j["_acquisition_source"] for j in res.jobs))
        yc = res.metadata["segments"].get("fantastic_jobs_ycombinator", {})
        self.assertGreaterEqual(yc.get("schema_rejected", 0), 1)

    def test_failure_does_not_overspend_or_retry_forever(self):
        feed = _multi_feed(self._rows(), fail={"wellfound": ConnectionError("reset")})
        res = run(feed, **self._four_source_over())
        self.assertLessEqual(res.metadata["jobs_quota_consumed"], 200)
        wf_calls = [c for c in feed.calls if c["source"] == "wellfound"]
        self.assertLessEqual(len(wf_calls), 2, "no unbounded retry on a failing source")

    def test_exhausted_source_yields_to_the_next(self):
        rows = self._rows()
        rows["ats"] = [_rec(1000 + i, "ats") for i in range(50)]
        feed = _multi_feed(rows)
        over = self._four_source_over()
        over["FANTASTIC_JOBS_RUN_SLICE_CAP"] = 60
        over["FANTASTIC_SOURCE_ALLOCATION"] = "fair_share"
        res = run(feed, **over)
        self.assertLessEqual(res.metadata["jobs_quota_consumed"], 60)
        self.assertGreater(len(_sources_called(feed)), 1, "later sources still ran")


# --------------------------------------------------------------------------
# Production-equivalent replay + watermark interaction
# --------------------------------------------------------------------------
class ProductionEquivalenceTest(unittest.TestCase):
    PROD = dict(FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=6000,
                FANTASTIC_JOBS_LINKEDIN_LIMIT=6000,
                FANTASTIC_JOBS_WELLFOUND_LIMIT=0, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
                FANTASTIC_WELLFOUND_SOURCE_ENABLED=False,
                FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=False,
                FANTASTIC_JOBS_MAX_JOBS_PER_RUN=12000, FANTASTIC_JOBS_RUN_SLICE_CAP=500)

    def test_production_config_calls_exactly_ats_and_linkedin(self):
        feed = _multi_feed({"ats": [_rec(1000 + i, "ats") for i in range(10)],
                            "linkedin": [_rec(2000 + i, "linkedin") for i in range(10)]})
        res = run(feed, **self.PROD)
        self.assertEqual(_sources_called(feed), ["ats", "linkedin"],
                         "no new source may appear with production config")
        self.assertEqual(len(res.jobs), 20)
        self.assertTrue(res.metadata["source_allocation"]["invariant_ok"])

    def test_production_config_preserves_every_upstream_filter(self):
        feed = _multi_feed({"ats": [_rec(1000, "ats")], "linkedin": [_rec(2000, "linkedin")]})
        run(feed, **dict(self.PROD, FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=True))
        jb = next(c["params"] for c in feed.calls if c["source"] == "linkedin")
        ats = next(c["params"] for c in feed.calls if c["source"] == "ats")
        for p in (jb, ats):
            self.assertEqual(p["location"], "United States")
            self.assertEqual(p["organization_headcount_gte"], 25)
            self.assertEqual(p["ai_employment_type"], "FULL_TIME")
            self.assertEqual(p["organization_agency"], "exclude")
            self.assertIn("title_advanced", p)
            self.assertIn("exclude_organization_industry", p)
        self.assertEqual(jb["exclude_ats_duplicate"], "true")
        self.assertEqual(jb["source"], "linkedin")
        self.assertNotIn("exclude_ats_duplicate", ats)

    def test_wellfound_yc_stay_off_under_production_config(self):
        feed = _multi_feed({k: [_rec(i, k)] for i, k in
                            ((1, "ats"), (2, "linkedin"), (3, "wellfound"), (4, "ycombinator"))})
        run(feed, **self.PROD)
        self.assertNotIn("wellfound", _sources_called(feed))
        self.assertNotIn("ycombinator", _sources_called(feed))

    def test_watermark_window_is_shared_and_budget_still_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = _multi_feed({"ats": [_rec(1000 + i, "ats") for i in range(30)],
                                "linkedin": [_rec(2000 + i, "linkedin") for i in range(30)],
                                "wellfound": [_rec(3000 + i, "wellfound") for i in range(30)]})
            res = run(feed,
                      FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
                      FANTASTIC_WATERMARK_STATE_PATH=str(Path(tmp) / "wm.json"),
                      FANTASTIC_WATERMARK_AUDIT_ENABLED=False,
                      FANTASTIC_JOBS_MAX_JOBS_PER_RUN=90, FANTASTIC_JOBS_RUN_SLICE_CAP=40,
                      FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=30,
                      FANTASTIC_JOBS_LINKEDIN_LIMIT=30,
                      FANTASTIC_WELLFOUND_SOURCE_ENABLED=True, FANTASTIC_JOBS_WELLFOUND_LIMIT=30,
                      FANTASTIC_SOURCE_ALLOCATION="fair_share")
            self.assertLessEqual(res.metadata["jobs_quota_consumed"], 40)
            # Every source that ran queried the SAME window.
            windows = {(c["params"].get("date_created_gte"), c["params"].get("date_created_lt"))
                       for c in feed.calls}
            self.assertEqual(len(windows), 1, f"sources must share one window: {windows}")


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# Account-level failure x drain interaction (item 7)
# --------------------------------------------------------------------------
class AccountFailureDrainTest(unittest.TestCase):
    def _run_wm(self, tmp, status_map, **over):
        rows = {k: [_rec(off + i, k) for i in range(3)]
                for off, k in ((1000, "ats"), (2000, "linkedin"),
                               (3000, "wellfound"), (4000, "ycombinator"))}
        feed = _multi_feed(rows, status=status_map)
        cfg = dict(FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
                   FANTASTIC_WATERMARK_STATE_PATH=str(Path(tmp) / "wm.json"),
                   FANTASTIC_WATERMARK_AUDIT_ENABLED=False,
                   FANTASTIC_SOURCE_BOOTSTRAP_ENABLED=False,
                   FANTASTIC_JOBS_MAX_JOBS_PER_RUN=400, FANTASTIC_JOBS_RUN_SLICE_CAP=0,
                   FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=100,
                   FANTASTIC_JOBS_LINKEDIN_LIMIT=100,
                   FANTASTIC_WELLFOUND_SOURCE_ENABLED=True, FANTASTIC_JOBS_WELLFOUND_LIMIT=100,
                   FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=True, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=100)
        cfg.update(over)
        return run(feed, **cfg), feed

    def test_auth_failure_after_a_drain_preserves_only_that_drain(self):
        for code, reason in ((401, "auth_failed"), (429, "rate_limited")):
            with self.subTest(status=code):
                with tempfile.TemporaryDirectory() as tmp:
                    res, feed = self._run_wm(tmp, {"linkedin": code})
                    self.assertEqual(res.metadata["stop_reason"], reason)
                    st = json.loads((Path(tmp) / "wm.json").read_text(encoding="utf-8"))
                    drained = st.get("window_drained_sources", {})
                    # ATS ran first and finished -> stays drained (never re-billed).
                    self.assertTrue(drained.get("fantastic_jobs_ats"))
                    # The failing source and everything after it are NOT drained.
                    for lbl in ("fantastic_jobs_linkedin", "fantastic_jobs_wellfound",
                                "fantastic_jobs_ycombinator"):
                        self.assertFalse(drained.get(lbl), f"{lbl} must not be drained")
                    self.assertFalse(st.get("window_drained"),
                                     "global window must not advance after an account failure")

    def test_next_healthy_run_skips_ats_and_continues_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_wm(tmp, {"linkedin": 401})
            res2, feed2 = self._run_wm(tmp, {})          # healthy retry
            called = _sources_called(feed2)
            self.assertNotIn("ats", called, "a drained source must not be re-billed")
            for src in ("linkedin", "wellfound", "ycombinator"):
                self.assertIn(src, called, f"{src} must continue")


# --------------------------------------------------------------------------
# Source config change matrix (item 8)
# --------------------------------------------------------------------------
class SourceConfigTransitionTest(unittest.TestCase):
    def _drained(self, tmp):
        return json.loads((Path(tmp) / "wm.json").read_text(
            encoding="utf-8")).get("window_drained_sources", {})

    def _state(self, tmp):
        return json.loads((Path(tmp) / "wm.json").read_text(encoding="utf-8"))

    def _run_set(self, tmp, sources, *, slice_cap=0, rows_per=3):
        rows = {k: [_rec(1000 * (i + 1) + j, k) for j in range(rows_per)]
                for i, k in enumerate(("ats", "linkedin", "wellfound", "ycombinator"))}
        feed = _multi_feed(rows)
        cfg = dict(FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
                   FANTASTIC_WATERMARK_STATE_PATH=str(Path(tmp) / "wm.json"),
                   FANTASTIC_WATERMARK_AUDIT_ENABLED=False,
                   FANTASTIC_SOURCE_BOOTSTRAP_ENABLED=False,
                   FANTASTIC_JOBS_MAX_JOBS_PER_RUN=400,
                   FANTASTIC_JOBS_RUN_SLICE_CAP=slice_cap,
                   FANTASTIC_ATS_SOURCE_ENABLED="ats" in sources,
                   FANTASTIC_JOBS_ATS_LIMIT=100 if "ats" in sources else 0,
                   FANTASTIC_JOBS_LINKEDIN_LIMIT=100 if "linkedin" in sources else 0,
                   FANTASTIC_WELLFOUND_SOURCE_ENABLED="wellfound" in sources,
                   FANTASTIC_JOBS_WELLFOUND_LIMIT=100 if "wellfound" in sources else 0,
                   FANTASTIC_YCOMBINATOR_SOURCE_ENABLED="ycombinator" in sources,
                   FANTASTIC_JOBS_YCOMBINATOR_LIMIT=100 if "ycombinator" in sources else 0)
        return run(feed, **cfg), feed

    def test_a_adding_a_source_reopens_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_set(tmp, ("ats", "linkedin"))
            self.assertTrue(self._state(tmp)["window_drained"])
            # Wellfound joins: the canonical window is no longer complete.
            res, feed = self._run_set(tmp, ("ats", "linkedin", "wellfound"))
            self.assertIn("wellfound", _sources_called(feed))
            self.assertNotIn("ats", _sources_called(feed), "already-drained sources not re-billed")

    def test_b_removing_a_source_does_not_block_advancement(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_set(tmp, ("ats", "linkedin", "wellfound", "ycombinator"), slice_cap=4)
            self.assertFalse(self._state(tmp)["window_drained"], "truncated by the slice cap")
            # Drop back to two sources: the stale WF/YC entries must not block.
            self._run_set(tmp, ("ats", "linkedin"))
            self.assertTrue(self._state(tmp)["window_drained"],
                            "advancement is scoped to the ENABLED set")

    def test_c_d_disable_while_partially_drained_then_re_enable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_set(tmp, ("ats", "linkedin", "wellfound"), slice_cap=4)
            partial = dict(self._drained(tmp))
            self._run_set(tmp, ("ats", "linkedin"))          # WF disabled
            self.assertEqual(self._drained(tmp).get("fantastic_jobs_wellfound"),
                             partial.get("fantastic_jobs_wellfound"),
                             "a disabled source's drain flag is preserved verbatim")
            res, feed = self._run_set(tmp, ("ats", "linkedin", "wellfound"))  # re-enabled
            self.assertIn("wellfound", _sources_called(feed))

    def test_e_limit_zero_to_positive_and_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, feed0 = self._run_set(tmp, ("ats", "linkedin"))
            self.assertNotIn("wellfound", _sources_called(feed0))
            _, feed1 = self._run_set(tmp, ("ats", "linkedin", "wellfound"))
            self.assertIn("wellfound", _sources_called(feed1))
            _, feed2 = self._run_set(tmp, ("ats", "linkedin"))
            self.assertNotIn("wellfound", _sources_called(feed2))
            self.assertTrue(self._state(tmp)["window_drained"])
