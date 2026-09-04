"""Round-based budget reclamation + source-aware server-side firmographics.

Two defects, one root cause: a source that cannot consume its fair share.

  * BUDGET. fair_share reserved an equal floor per source and cascaded only
    FORWARD, so a sparse source released its reservation after the sources able
    to spend it had already run. Measured live 2026-09-04: ATS 791, LinkedIn 791,
    Wellfound 0, YC 0 -- 1582 of a 3164 run_cap stranded, HALF the run.
  * FILTERS. Wellfound/YC carry no provider firmographics, and
    `organization_headcount_gte` (a >=) and `exclude_organization_industry` (a
    NOT-IN) each drop nulls, so each independently took both sources to exactly
    zero rows. The plan loop also sent no `title_advanced` at all, so those
    sources would have returned their whole unfiltered feed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import config
import fantastic_jobs_adapter as fja
from fantastic_jobs_adapter import _SourceBudgetAllocator, _SourceSegment
from test_fantastic_multi_source import _multi_feed, _rec, run

RUN_CAP = 3164
LABELS = {"ats": "fantastic_jobs_ats", "linkedin": "fantastic_jobs_linkedin",
          "wellfound": "fantastic_jobs_wellfound", "ycombinator": "fantastic_jobs_ycombinator"}


def _rows(**counts):
    """Per-source inventory; each source's ids live in their own numeric band so a
    row can always be attributed and cross-source dedupe is observable."""
    base = {"ats": 100000, "linkedin": 200000, "wellfound": 300000, "ycombinator": 400000}
    return {k: [_rec(base[k] + i, k) for i in range(n)] for k, n in counts.items()}


def _run4(tmp, rows, *, cap=RUN_CAP, policy="fair_share", limits=3000,
          fail=None, status=None, **over):
    """Production shape: watermark ON (so every source pages the shared window by
    offset), all four sources enabled, one governor run_cap."""
    lim = limits if isinstance(limits, dict) else {k: limits for k in LABELS}
    cfg = dict(
        FANTASTIC_SOURCE_ALLOCATION=policy,
        FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
        FANTASTIC_WATERMARK_STATE_PATH=str(Path(tmp) / "wm.json"),
        FANTASTIC_WATERMARK_AUDIT_ENABLED=False,
        FANTASTIC_SOURCE_BOOTSTRAP_ENABLED=False,
        FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=lim["ats"],
        FANTASTIC_JOBS_LINKEDIN_LIMIT=lim["linkedin"],
        FANTASTIC_WELLFOUND_SOURCE_ENABLED=True, FANTASTIC_JOBS_WELLFOUND_LIMIT=lim["wellfound"],
        FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=True, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=lim["ycombinator"],
        FANTASTIC_JOBS_MAX_JOBS_PER_RUN=12000, FANTASTIC_JOBS_RUN_SLICE_CAP=cap,
        FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=400)
    cfg.update(over)
    feed = _multi_feed(rows, fail=fail, status=status)
    return run(feed, **cfg), feed


def _billed(res):
    return {k: int(v.get("returned", 0) or 0) for k, v in res.metadata["segments"].items()}


def _calls_by_source(feed):
    out = {}
    for c in feed.calls:
        out.setdefault(c["source"], []).append((c["offset"], c["limit"]))
    return out


# --------------------------------------------------------------------------
# The defect, pinned as a unit so a regression is unambiguous
# --------------------------------------------------------------------------
class ForwardOnlyReclamationTest(unittest.TestCase):
    """One round of fair_share cannot reclaim from a sparse source that runs LATE.

    Kept as an explicit unit test because it is the exact arithmetic that stranded
    half a production run, and because the fix must not be mistaken for "the
    allocator now over-grants".
    """

    @staticmethod
    def _segs():
        return [_SourceSegment(key=k, label=LABELS[k], endpoint="/e", limit=3000, accept=None)
                for k in ("ats", "linkedin", "wellfound", "ycombinator")]

    def test_single_round_strands_the_sparse_sources_reservation(self):
        segs = self._segs()
        a = _SourceBudgetAllocator(RUN_CAP, segs, "fair_share")
        inventory = {"ats": 10 ** 9, "linkedin": 10 ** 9, "wellfound": 0, "ycombinator": 0}
        billed = 0
        for s in segs:
            got = min(a.grant(s, billed), inventory[s.key])
            a.settle(s, got)
            billed += got
        self.assertEqual(billed, 1582, "the measured live under-spend")
        self.assertEqual(a.pool, 1582, "released only after the spenders had run")

    def test_reopening_a_round_returns_that_budget_to_the_spenders(self):
        segs = self._segs()
        a = _SourceBudgetAllocator(RUN_CAP, segs, "fair_share")
        inventory = {"ats": 10 ** 9, "linkedin": 10 ** 9, "wellfound": 0, "ycombinator": 0}
        billed = 0
        for s in segs:
            got = min(a.grant(s, billed), inventory[s.key])
            a.settle(s, got)
            billed += got
        still_productive = [s for s in segs if inventory[s.key] > 0]
        a.open_round(still_productive)
        for s in still_productive:
            got = min(a.grant(s, billed), inventory[s.key])
            a.settle(s, got)
            billed += got
        self.assertEqual(billed, RUN_CAP, "the whole run budget is now usable")
        self.assertEqual(a.spent["ats"], 1582)
        self.assertEqual(a.spent["linkedin"], 1582)
        self.assertTrue(a.to_dict()["invariant_ok"])

    def test_cumulative_limit_survives_multiple_rounds(self):
        seg = _SourceSegment(key="ats", label=LABELS["ats"], endpoint="/e", limit=900, accept=None)
        a = _SourceBudgetAllocator(5000, [seg], "fair_share")
        total = 0
        for _ in range(5):
            a.open_round([seg])
            g = a.grant(seg, total)
            a.settle(seg, g)
            total += g
        self.assertEqual(total, 900, "a source may never exceed its configured limit")

    def test_sequential_is_untouched_by_the_round_machinery(self):
        segs = self._segs()
        a = _SourceBudgetAllocator(RUN_CAP, segs, "sequential")
        # First-come semantics: bounded only by its own limit and the run budget,
        # never by a per-source share.
        self.assertEqual(a.grant(segs[0], 0), 3000)
        a.settle(segs[0], 3000)
        self.assertEqual(a.grant(segs[1], 3000), RUN_CAP - 3000)


# --------------------------------------------------------------------------
# A-C: the end-to-end budget matrix
# --------------------------------------------------------------------------
class ReclamationMatrixTest(unittest.TestCase):
    def test_a_empty_sparse_sources_release_the_whole_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, feed = _run4(tmp, _rows(ats=20000, linkedin=20000, wellfound=0, ycombinator=0))
            b = _billed(res)
            acc = res.metadata["run_budget_accounting"]
            self.assertEqual(acc["total_billed"], RUN_CAP, "full run_cap must be consumed")
            self.assertEqual(acc["unused_run_budget"], 0)
            self.assertEqual(b[LABELS["wellfound"]], 0)
            self.assertEqual(b[LABELS["ycombinator"]], 0)
            self.assertEqual(b[LABELS["ats"]] + b[LABELS["linkedin"]], RUN_CAP)
            # ...and fairly: neither productive source was starved by the other.
            self.assertEqual(b[LABELS["ats"]], b[LABELS["linkedin"]])

    def test_b_every_source_gets_its_floor_then_residual_recycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, feed = _run4(tmp, _rows(ats=20000, linkedin=1500, wellfound=200, ycombinator=40))
            b = _billed(res)
            acc = res.metadata["run_budget_accounting"]
            floor = RUN_CAP // 4                                     # 791
            for key in ("wellfound", "ycombinator"):
                self.assertGreater(b[LABELS[key]], 0,
                                   f"{key} must get its initial opportunity BEFORE recycling")
            self.assertEqual(b[LABELS["wellfound"]], 200, "capped by real inventory")
            self.assertEqual(b[LABELS["ycombinator"]], 40)
            self.assertLessEqual(b[LABELS["linkedin"]], 1500)
            self.assertGreaterEqual(b[LABELS["linkedin"]], min(1500, floor))
            self.assertEqual(acc["total_billed"], RUN_CAP,
                             "aggregate inventory exceeds run_cap, so it must all be used")
            for key, lbl in LABELS.items():
                self.assertLessEqual(b[lbl], 3000, f"{key} exceeded its configured limit")

    def test_c_all_sparse_bills_only_what_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, feed = _run4(tmp, _rows(ats=120, linkedin=80, wellfound=30, ycombinator=10))
            acc = res.metadata["run_budget_accounting"]
            self.assertEqual(acc["total_billed"], 240, "exactly the inventory that exists")
            self.assertEqual(acc["unused_run_budget"], RUN_CAP - 240,
                             "unused budget is legitimate when nothing is left to buy")
            self.assertTrue(acc["segments_reconcile"] and acc["within_run_cap"])


# --------------------------------------------------------------------------
# D-G: never re-poll a dead source; isolate failures; abort on account errors
# --------------------------------------------------------------------------
class ResumeDisciplineTest(unittest.TestCase):
    def test_d_an_empty_source_is_asked_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, feed = _run4(tmp, _rows(ats=20000, linkedin=20000, wellfound=0, ycombinator=0))
            calls = _calls_by_source(feed)
            self.assertEqual(len(calls.get("wellfound", [])), 1,
                             "empty_page means exhausted -- never re-poll it this run")
            self.assertEqual(len(calls.get("ycombinator", [])), 1)

    def test_e_a_resumed_source_continues_instead_of_re_paging(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, feed = _run4(tmp, _rows(ats=2500, linkedin=2500, wellfound=0, ycombinator=0))
            for src in ("ats", "linkedin"):
                spans = [(o, o + l) for o, l in _calls_by_source(feed)[src]]
                for (a_lo, a_hi), (b_lo, _) in zip(spans, spans[1:]):
                    self.assertEqual(a_hi, b_lo,
                                     f"{src} paging must stay contiguous across rounds: {spans}")
            ids = [j["_fantastic_internal_id"] for j in res.jobs]
            self.assertEqual(len(ids), len(set(ids)), "no page may be billed twice")

    def test_f_a_failing_source_is_isolated_and_others_still_consume(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = _rows(ats=20000, linkedin=20000, wellfound=5000, ycombinator=0)
            res, feed = _run4(tmp, rows, status={"wellfound": 500})
            segs = res.metadata["segments"]
            self.assertTrue(segs[LABELS["wellfound"]].get("error_code"))
            acc = res.metadata["run_budget_accounting"]
            self.assertEqual(acc["total_billed"], RUN_CAP,
                             "a 5xx source must not strand the budget it was holding")
            self.assertEqual(len(_calls_by_source(feed)["wellfound"]), 1,
                             "a failed source is never re-dispatched")

    def test_g_account_level_failure_aborts_before_any_recycling(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = _rows(ats=20000, linkedin=20000, wellfound=5000, ycombinator=0)
            res, feed = _run4(tmp, rows, status={"wellfound": 401})
            self.assertFalse(res.success)
            self.assertEqual(res.metadata.get("stop_reason"), "auth_failed")
            self.assertNotIn("ycombinator", _calls_by_source(feed),
                             "no segment may be dispatched after an account-level abort")
            acc = res.metadata["run_budget_accounting"]
            self.assertTrue(acc["segments_reconcile"] and acc["within_run_cap"])


# --------------------------------------------------------------------------
# H-J: accounting across multiple passes
# --------------------------------------------------------------------------
class MultiPassAccountingTest(unittest.TestCase):
    def test_h_attribution_sums_across_every_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, feed = _run4(tmp, _rows(ats=20000, linkedin=20000, wellfound=0, ycombinator=0))
            per = (res.metadata["source_attribution"] or {})["per_source"]
            for src in ("ats", "linkedin"):
                lbl = LABELS[src]
                requested = sum(l for _, l in _calls_by_source(feed)[src])
                self.assertEqual(per[lbl]["returned_billed"], requested,
                                 "per-source billing must include every pass")
            self.assertEqual(sum(v["returned_billed"] for v in per.values()),
                             res.metadata["jobs_quota_consumed"])

    def test_i_provider_id_dedupe_holds_across_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            # ATS and LinkedIn serve the SAME provider ids: one opportunity each.
            shared = [_rec(700000 + i, "ats") for i in range(2000)]
            rows = {"ats": shared,
                    "linkedin": [_rec(700000 + i, "linkedin") for i in range(2000)],
                    "wellfound": [], "ycombinator": []}
            res, feed = _run4(tmp, rows)
            ids = [j["_fantastic_internal_id"] for j in res.jobs]
            self.assertEqual(len(ids), len(set(ids)), "a provider id is one opportunity")
            self.assertGreater(res.metadata["cross_source_duplicates"], 0,
                               "the overlap must still be COUNTED, across passes")

    def test_j_run_budget_accounting_reconciles_exactly(self):
        for inv in (dict(ats=20000, linkedin=20000, wellfound=0, ycombinator=0),
                    dict(ats=20000, linkedin=1500, wellfound=200, ycombinator=40),
                    dict(ats=120, linkedin=80, wellfound=30, ycombinator=10)):
            with self.subTest(**inv), tempfile.TemporaryDirectory() as tmp:
                res, _ = _run4(tmp, _rows(**inv))
                m = res.metadata
                acc = m["run_budget_accounting"]
                seg_sum = sum(int(v.get("returned", 0) or 0) for v in m["segments"].values())
                self.assertEqual(seg_sum, m["jobs_quota_consumed"])
                self.assertEqual(m["jobs_quota_consumed"], acc["total_billed"])
                self.assertTrue(acc["segments_reconcile"])
                self.assertTrue(acc["within_run_cap"])
                self.assertLessEqual(acc["total_billed"], acc["run_cap"])

    def test_watermark_drain_state_reflects_the_last_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Inventory smaller than the budget: every source genuinely drains, and a
            # round-1 "cap_reached" must not mask the later real drain.
            res, _ = _run4(tmp, _rows(ats=120, linkedin=80, wellfound=30, ycombinator=10))
            drained = res.metadata["watermark"]["drained_sources"]
            for lbl in LABELS.values():
                self.assertTrue(drained.get(lbl), f"{lbl} exhausted its inventory")
            self.assertTrue(res.metadata["watermark"]["drained"])


# --------------------------------------------------------------------------
# Source-aware server-side firmographics
# --------------------------------------------------------------------------
class SourceAwareFilterTest(unittest.TestCase):
    NULL_EXCLUDING = ("organization_headcount_gte", "organization_headcount_lt",
                      "exclude_organization_industry")

    def setUp(self):
        self.expr = fja.build_title_query_plan().get("expression", "")

    def test_firmographic_sources_keep_every_filter(self):
        for src in ("linkedin",):
            p = fja.build_jb_params(src, title_advanced_expr=self.expr)
            self.assertEqual(p["organization_headcount_gte"], config.FANTASTIC_JOBS_HEADCOUNT_MIN)
            self.assertTrue(fja.source_supports_provider_firmographics(src))

    def test_incompatible_sources_drop_only_the_null_excluding_predicates(self):
        for src in ("wellfound", "ycombinator"):
            with self.subTest(src=src):
                p = fja.build_jb_params(src, title_advanced_expr=self.expr)
                for key in self.NULL_EXCLUDING:
                    self.assertNotIn(key, p, f"{key} drops 100% of {src} rows")
                # ...and KEEPS everything proven compatible.
                self.assertEqual(p["source"], src)
                self.assertEqual(p["location"], config.FANTASTIC_JOBS_LOCATION)
                self.assertEqual(p["ai_employment_type"], config.FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE)
                self.assertEqual(p["organization_agency"], "exclude")
                self.assertEqual(p["time_frame"], config.FANTASTIC_JOBS_TIME_FRAME)
                self.assertEqual(p["exclude_ats_duplicate"], "true")

    def test_role_targeting_is_applied_to_plan_dispatched_sources(self):
        """The plan loop used to send NO title_advanced, so a sparse source would
        have spent its credits on its whole unfiltered feed."""
        for src in ("wellfound", "ycombinator"):
            p = fja.build_jb_params(src, title_advanced_expr=self.expr)
            self.assertEqual(p.get("title_advanced"), self.expr)

    def test_no_title_expression_means_no_title_parameter(self):
        p = fja.build_jb_params("wellfound", title_advanced_expr="")
        self.assertNotIn("title_advanced", p)

    def test_incompatible_set_is_configuration_not_hardcoded(self):
        from unittest import mock
        with mock.patch.object(config, "FANTASTIC_FIRMOGRAPHIC_INCOMPATIBLE_SOURCES", []):
            p = fja.build_jb_params("wellfound", title_advanced_expr=self.expr)
            self.assertIn("organization_headcount_gte", p)
        with mock.patch.object(config, "FANTASTIC_FIRMOGRAPHIC_INCOMPATIBLE_SOURCES", ["linkedin"]):
            self.assertFalse(fja.source_supports_provider_firmographics("LinkedIn"))

    def test_live_requests_carry_the_source_aware_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, feed = _run4(tmp, _rows(ats=200, linkedin=200, wellfound=200, ycombinator=200),
                            FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=True)
            by = {c["source"]: c["params"] for c in feed.calls}
            for src in ("wellfound", "ycombinator"):
                for key in self.NULL_EXCLUDING:
                    self.assertNotIn(key, by[src])
                self.assertIn("title_advanced", by[src])
            self.assertIn("organization_headcount_gte", by["linkedin"])
            self.assertIn("organization_headcount_gte", by["ats"])

    def test_production_two_source_request_shape_is_unchanged(self):
        """ATS + LinkedIn only -- today's live configuration -- must be byte-identical."""
        with tempfile.TemporaryDirectory() as tmp:
            _, feed = _run4(tmp, _rows(ats=200, linkedin=200),
                            FANTASTIC_WELLFOUND_SOURCE_ENABLED=False,
                            FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=False,
                            limits={"ats": 3000, "linkedin": 3000, "wellfound": 0, "ycombinator": 0},
                            FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=True)
            called = sorted({c["source"] for c in feed.calls})
            self.assertEqual(called, ["ats", "linkedin"])
            for src in called:
                p = next(c["params"] for c in feed.calls if c["source"] == src)
                self.assertEqual(p["organization_headcount_gte"], config.FANTASTIC_JOBS_HEADCOUNT_MIN)
                self.assertIn("exclude_organization_industry", p)


if __name__ == "__main__":
    unittest.main()
