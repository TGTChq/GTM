"""First-enablement bootstrap: guaranteed progress, real continuation, and the
provider-id-dedupe vs candidate-collision distinction.

Two failure modes are pinned here because both would silently defeat a recall
expansion:

  * STARVATION -- a backfill funded only by leftover budget never runs when the
    steady-state sources can fill the whole run_cap, so historical inventory is
    never inspected;
  * LIVELOCK -- a truncated backfill that re-pages from offset 0 every run
    re-bills the same prefix, dedupes it entirely, and never reaches the tail.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import config
import fantastic_jobs_adapter as fja
from unittest import mock
from test_fantastic_multi_source import _multi_feed, _rec, _sources_called, run


class BootstrapBudgetTest(unittest.TestCase):
    def _cfg(self, tmp, sources, *, slice_cap, rows_per=4000, **over):
        rows = {k: [_rec(100000 * (i + 1) + j, k) for j in range(rows_per)]
                for i, k in enumerate(("ats", "linkedin", "wellfound", "ycombinator"))}
        feed = _multi_feed(rows)
        cfg = dict(FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
                   FANTASTIC_WATERMARK_STATE_PATH=str(Path(tmp) / "wm.json"),
                   FANTASTIC_WATERMARK_AUDIT_ENABLED=False,
                   FANTASTIC_SOURCE_BOOTSTRAP_ENABLED=True,
                   FANTASTIC_JOBS_TIME_FRAME="7d",
                   FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=500,
                   FANTASTIC_JOBS_MAX_JOBS_PER_RUN=12000,
                   FANTASTIC_JOBS_RUN_SLICE_CAP=slice_cap,
                   FANTASTIC_ATS_SOURCE_ENABLED="ats" in sources,
                   FANTASTIC_JOBS_ATS_LIMIT=3000 if "ats" in sources else 0,
                   FANTASTIC_JOBS_LINKEDIN_LIMIT=3000 if "linkedin" in sources else 0,
                   FANTASTIC_WELLFOUND_SOURCE_ENABLED="wellfound" in sources,
                   FANTASTIC_JOBS_WELLFOUND_LIMIT=3000 if "wellfound" in sources else 0,
                   FANTASTIC_YCOMBINATOR_SOURCE_ENABLED="ycombinator" in sources,
                   FANTASTIC_JOBS_YCOMBINATOR_LIMIT=3000 if "ycombinator" in sources else 0)
        cfg.update(over)
        res = run(feed, **cfg)
        # The PIPELINE commits the watermark after processing+persistence, never the
        # adapter. Simulate that so the canonical window can actually advance and a
        # later source enablement is a genuine FIRST enablement.
        with mock.patch.object(config, "FANTASTIC_WATERMARK_STATE_PATH",
                               str(Path(tmp) / "wm.json")):
            fja.commit_watermark(success=True)
        return res, feed

    def _seed(self, tmp):
        """Run ATS+LinkedIn to COMPLETION so the canonical watermark advances; only
        then is enabling a third source a genuine FIRST enablement."""
        self._cfg(tmp, ("ats", "linkedin"), slice_cap=0, rows_per=5)

    @staticmethod
    def _boot(tmp, label="fantastic_jobs_wellfound"):
        st = json.loads((Path(tmp) / "wm.json").read_text(encoding="utf-8"))
        return (st.get("source_bootstrap") or {}).get(label, {})

    def test_bootstrap_progresses_under_saturated_steady_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            res, _ = self._cfg(tmp, ("ats", "linkedin", "wellfound"), slice_cap=300)
            self.assertGreater(res.metadata.get("bootstrap_reserve", 0), 0)
            self.assertGreater(self._boot(tmp).get("offset", 0), 0,
                               "no progress despite saturated steady state")
            self.assertLessEqual(res.metadata["jobs_quota_consumed"], 300)

    def test_reserve_is_derived_from_the_fair_share_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            res, _ = self._cfg(tmp, ("ats", "linkedin", "wellfound"), slice_cap=400)
            # 3 sources + 1 bootstrap share -> 400 * 1 / 4 = 100
            self.assertEqual(res.metadata["bootstrap_reserve"], 100)

    def test_reserve_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            res, _ = self._cfg(tmp, ("ats", "linkedin", "wellfound"), slice_cap=300,
                               FANTASTIC_BOOTSTRAP_RESERVE_SHARES=0)
            self.assertEqual(res.metadata.get("bootstrap_reserve", 0), 0)

    def test_offset_advances_and_never_restarts_at_page_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            offsets, starts = [], []
            for _ in range(4):
                res, _ = self._cfg(tmp, ("ats", "linkedin", "wellfound"), slice_cap=300)
                offsets.append(self._boot(tmp).get("offset", 0))
                b = res.metadata.get("bootstrap", {}).get("fantastic_jobs_wellfound")
                if b:
                    starts.append(b["offset_from"])
                self.assertLessEqual(res.metadata["jobs_quota_consumed"], 300)
            self.assertEqual(offsets, sorted(offsets), "offset must be monotonic")
            self.assertGreater(offsets[-1], offsets[0], "must reach deeper pages")
            self.assertNotEqual(starts, [0] * len(starts), "must not restart at page 1")

    def test_bootstrap_eventually_drains_then_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            drained = False
            for _ in range(12):
                res, _ = self._cfg(tmp, ("ats", "linkedin", "wellfound"),
                                   slice_cap=300, rows_per=40)
                self.assertLessEqual(res.metadata["jobs_quota_consumed"], 300)
                if self._boot(tmp).get("drained"):
                    drained = True
                    break
            self.assertTrue(drained, "bootstrap never drained across repeated runs")
            res2, _ = self._cfg(tmp, ("ats", "linkedin", "wellfound"),
                                slice_cap=300, rows_per=40)
            self.assertNotIn("fantastic_jobs_wellfound", res2.metadata.get("bootstrap", {}),
                             "a drained bootstrap must issue no further requests")

    def test_full_multi_run_state_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)

            r2, f2 = self._cfg(tmp, ("ats", "linkedin", "wellfound"),
                               slice_cap=300, rows_per=200)
            self.assertIn("wellfound", _sources_called(f2))
            off2 = self._boot(tmp).get("offset", 0)
            self.assertGreater(off2, 0)
            self.assertLessEqual(r2.metadata["jobs_quota_consumed"], 300)

            r3, _ = self._cfg(tmp, ("ats", "linkedin", "wellfound"),
                              slice_cap=300, rows_per=200)
            off3 = self._boot(tmp).get("offset", 0)
            self.assertGreaterEqual(off3, off2, "continuation must not rewind")
            self.assertLessEqual(r3.metadata["jobs_quota_consumed"], 300)

            r4, f4 = self._cfg(tmp, ("ats", "linkedin"), slice_cap=300, rows_per=200)
            self.assertNotIn("wellfound", _sources_called(f4), "disabled: spends nothing")
            self.assertEqual(self._boot(tmp).get("offset", 0), off3, "progress persists")

            r5, f5 = self._cfg(tmp, ("ats", "linkedin", "wellfound"),
                               slice_cap=300, rows_per=200)
            self.assertIn("wellfound", _sources_called(f5))
            b5 = r5.metadata.get("bootstrap", {}).get("fantastic_jobs_wellfound")
            if b5:
                self.assertEqual(b5["offset_from"], off3, "resumes the same continuation")
            self.assertLessEqual(r5.metadata["jobs_quota_consumed"], 300)


class DedupeVersusCollisionTest(unittest.TestCase):
    BASE = dict(FANTASTIC_JOBS_MAX_JOBS_PER_RUN=200,
                FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=50,
                FANTASTIC_JOBS_LINKEDIN_LIMIT=50)

    def test_case_a_same_provider_id_collapses(self):
        shared = _rec(4242, "ats", org="Acme")
        feed = _multi_feed({"ats": [shared], "linkedin": [dict(shared, source="linkedin")]})
        res = run(feed, **self.BASE)
        ids = [j["_fantastic_internal_id"] for j in res.jobs]
        self.assertEqual(ids.count("4242"), 1, "same provider id must collapse")
        self.assertEqual(len(res.jobs), 1)
        self.assertGreaterEqual(res.metadata["cross_source_duplicates"], 1,
                                "both observations must still be attributed")

    @staticmethod
    def _with_domain(i, source):
        r = _rec(i, source, org="Acme")
        r["organization_url"] = "https://acme.com/careers"
        return r

    def test_case_b_same_candidate_key_different_ids_stay_separate(self):
        feed = _multi_feed({"ats": [self._with_domain(5001, "ats")],
                            "linkedin": [self._with_domain(6001, "linkedin")]})
        res = run(feed, **self.BASE)
        self.assertEqual(sorted(j["_fantastic_internal_id"] for j in res.jobs),
                         ["5001", "6001"], "distinct ids must NOT be collapsed")
        attrib = res.metadata["source_attribution"]
        self.assertGreaterEqual(attrib.get("candidate_key_collisions", 0), 1)
        detail = attrib.get("candidate_key_detail") or []
        self.assertTrue(detail, "a collision must be recorded for manual review")
        for field in ("candidate_key", "source_a", "provider_id_a", "source_b",
                      "provider_id_b", "title", "company", "domain", "location",
                      "posted_at", "url_a", "url_b"):
            self.assertIn(field, detail[0])
        self.assertNotEqual(detail[0]["provider_id_a"], detail[0]["provider_id_b"])

    def test_distinct_openings_same_company_are_never_merged(self):
        feed = _multi_feed({"linkedin": [_rec(7001, "linkedin", org="Acme"),
                                         _rec(7002, "linkedin", org="Acme")]})
        res = run(feed, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=200,
                  FANTASTIC_JOBS_LINKEDIN_LIMIT=50)
        self.assertEqual(len(res.jobs), 2)


if __name__ == "__main__":
    unittest.main()


class SimultaneousBootstrapTest(BootstrapBudgetTest):
    """Wellfound AND Y Combinator enabled for the first time TOGETHER.

    The bootstrap reserve proves the POOL is funded; this proves the pool is
    shared. Handing it out first-come would let the first pending bootstrap in
    plan order consume the whole reserve every run and starve the second forever.
    """

    ALL4 = ("ats", "linkedin", "wellfound", "ycombinator")

    def test_neither_new_source_is_starved(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            res, _ = self._cfg(tmp, self.ALL4, slice_cap=400)
            wf = self._boot(tmp, "fantastic_jobs_wellfound")
            yc = self._boot(tmp, "fantastic_jobs_ycombinator")
            self.assertGreater(wf.get("offset", 0), 0, "Wellfound bootstrap starved")
            self.assertGreater(yc.get("offset", 0), 0, "YC bootstrap starved")
            self.assertLessEqual(res.metadata["jobs_quota_consumed"], 400)

    def test_reserve_is_split_evenly_between_two_pending_bootstraps(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            res, _ = self._cfg(tmp, self.ALL4, slice_cap=500)
            alloc = res.metadata["bootstrap_allocation"]
            # 4 sources + 1 bootstrap share -> reserve = 500 // 5 = 100, split 50/50
            self.assertEqual(alloc["reserve"], 100)
            self.assertEqual(sorted(alloc["granted"].values()), [50, 50])
            self.assertLessEqual(alloc["billed_total"], alloc["reserve"])
            self.assertTrue(alloc["invariant_ok"])

    def test_single_pending_bootstrap_receives_the_whole_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            res, _ = self._cfg(tmp, ("ats", "linkedin", "wellfound"), slice_cap=400)
            alloc = res.metadata["bootstrap_allocation"]
            self.assertEqual(list(alloc["granted"].values()), [alloc["reserve"]])

    def test_zero_pending_bootstraps_reserves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            res, _ = self._cfg(tmp, ("ats", "linkedin"), slice_cap=400)
            self.assertEqual(res.metadata.get("bootstrap_reserve", 0), 0)
            self.assertNotIn("bootstrap_allocation", res.metadata)

    def test_both_progress_monotonically_then_both_drain(self):
        rows = {"wf": [], "yc": []}
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            drained = {"wf": False, "yc": False}
            for _ in range(14):
                res, _ = self._cfg(tmp, self.ALL4, slice_cap=400, rows_per=30)
                self.assertLessEqual(res.metadata["jobs_quota_consumed"], 400)
                wf = self._boot(tmp, "fantastic_jobs_wellfound")
                yc = self._boot(tmp, "fantastic_jobs_ycombinator")
                rows["wf"].append(wf.get("offset", 0))
                rows["yc"].append(yc.get("offset", 0))
                drained["wf"] = bool(wf.get("drained"))
                drained["yc"] = bool(yc.get("drained"))
                if drained["wf"] and drained["yc"]:
                    break
            self.assertEqual(rows["wf"], sorted(rows["wf"]), "WF offset must be monotonic")
            self.assertEqual(rows["yc"], sorted(rows["yc"]), "YC offset must be monotonic")
            self.assertTrue(drained["wf"] and drained["yc"],
                            f"both bootstraps must drain (wf={rows['wf']}, yc={rows['yc']})")
            # Once both are drained the reserve disappears entirely.
            res2, _ = self._cfg(tmp, self.ALL4, slice_cap=400, rows_per=30)
            self.assertEqual(res2.metadata.get("bootstrap_reserve", 0), 0)
            self.assertNotIn("bootstrap_allocation", res2.metadata)

    def test_reserve_cascades_when_one_bootstrap_drains_first(self):
        """WF drains early; its unused share must flow to YC, not be wasted."""
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            seen_grants = []
            for _ in range(10):
                res, _ = self._cfg(tmp, self.ALL4, slice_cap=400, rows_per=30)
                alloc = res.metadata.get("bootstrap_allocation")
                if alloc:
                    seen_grants.append(dict(alloc["granted"]))
                wf = self._boot(tmp, "fantastic_jobs_wellfound")
                yc = self._boot(tmp, "fantastic_jobs_ycombinator")
                if wf.get("drained") and not yc.get("drained"):
                    self.assertEqual(list(alloc["granted"]), ["ycombinator"])
                    self.assertEqual(alloc["granted"]["ycombinator"],
                                     alloc["reserve"], "sole remaining bootstrap gets it all")
                    return
            self.assertTrue(seen_grants, "no bootstrap allocation was ever made")

    def test_both_disabled_mid_bootstrap_then_only_yc_re_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            self._cfg(tmp, self.ALL4, slice_cap=400)
            wf0 = self._boot(tmp, "fantastic_jobs_wellfound").get("offset", 0)
            yc0 = self._boot(tmp, "fantastic_jobs_ycombinator").get("offset", 0)
            # F: both disabled mid-bootstrap -> no budget consumed, progress kept
            res, feed = self._cfg(tmp, ("ats", "linkedin"), slice_cap=400)
            self.assertNotIn("wellfound", _sources_called(feed))
            self.assertNotIn("ycombinator", _sources_called(feed))
            self.assertEqual(res.metadata.get("bootstrap_reserve", 0), 0)
            self.assertEqual(self._boot(tmp, "fantastic_jobs_wellfound").get("offset", 0), wf0)
            self.assertEqual(self._boot(tmp, "fantastic_jobs_ycombinator").get("offset", 0), yc0)
            # G: only YC re-enabled -> YC alone claims the reserve and resumes
            res2, feed2 = self._cfg(tmp, ("ats", "linkedin", "ycombinator"), slice_cap=400)
            self.assertNotIn("wellfound", _sources_called(feed2))
            alloc = res2.metadata.get("bootstrap_allocation")
            self.assertEqual(list(alloc["granted"]), ["ycombinator"])
            self.assertGreaterEqual(
                self._boot(tmp, "fantastic_jobs_ycombinator").get("offset", 0), yc0)
            self.assertEqual(self._boot(tmp, "fantastic_jobs_wellfound").get("offset", 0), wf0,
                             "a disabled bootstrap must not advance")


class RunBudgetAccountingTest(BootstrapBudgetTest):
    """The reported numbers must reconcile mechanically with what was billed.

    Regression: metrics were finalised BEFORE the bootstrap loop ran, so the
    reported jobs_quota_consumed excluded every bootstrap row. Enforcement was
    still correct (the live quota object bounded spend), but the run UNDER-REPORTED
    consumption -- and the governor ledger is charged from that number, so a cycle
    would have been allowed to overspend.
    """

    ALL4 = ("ats", "linkedin", "wellfound", "ycombinator")

    def test_reported_consumption_includes_bootstrap_billing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            res, _ = self._cfg(tmp, self.ALL4, slice_cap=400, rows_per=100000)
            acc = res.metadata["run_budget_accounting"]
            self.assertGreater(acc["bootstrap_billed"], 0, "bootstrap must have billed")
            self.assertEqual(acc["total_billed"],
                             acc["steady_billed"] + acc["bootstrap_billed"],
                             "reported consumption must include bootstrap rows")
            self.assertEqual(res.metadata["jobs_quota_consumed"], acc["total_billed"])
            self.assertTrue(acc["segments_reconcile"])
            self.assertTrue(acc["within_run_cap"])
            self.assertLessEqual(acc["total_billed"], 400)

    def test_allocator_budget_is_not_mislabelled_as_run_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            res, _ = self._cfg(tmp, self.ALL4, slice_cap=400, rows_per=100000)
            acc = res.metadata["run_budget_accounting"]
            sa = res.metadata["source_allocation"]
            self.assertNotIn("run_cap", sa)
            # The steady allocator's pool is run_cap MINUS the bootstrap reserve.
            self.assertEqual(sa["budget"], acc["run_cap"] - acc["bootstrap_reserve"])
            self.assertLessEqual(sa["billed_total"], sa["budget"])
            ba = res.metadata["bootstrap_allocation"]
            self.assertLessEqual(ba["billed_total"], ba["reserve"])

    def test_saturated_run_spends_exactly_the_run_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            for _ in range(3):
                res, _ = self._cfg(tmp, self.ALL4, slice_cap=400, rows_per=100000)
                acc = res.metadata["run_budget_accounting"]
                self.assertEqual(acc["total_billed"], 400,
                                 "unlimited inventory must consume the whole run budget")
                self.assertEqual(acc["unused_run_budget"], 0)
                self.assertTrue(acc["segments_reconcile"])
