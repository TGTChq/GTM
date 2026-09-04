"""Per-source watermark drain state + the multi-arm source experiment runner.

Two independent concerns, both required before Wellfound/YC can be measured:

  * ONE canonical window with PER-SOURCE drain flags, so a truncated source cannot
    make already-finished sources re-pay for the same window (the failure mode that
    made a shared window unsafe once more than two sources exist);
  * a bounded experiment runner whose arms can never outspend one explicit budget.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import config
import fantastic_jobs_adapter as fja
from orchestrator import source_experiment_runner as R
from orchestrator.source_experiment_runner import ArmSpec, ExperimentBudgetError, plan_arms

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
LI = "fantastic_jobs_linkedin"
ATS = "fantastic_jobs_ats"
WF = "fantastic_jobs_wellfound"
YC = "fantastic_jobs_ycombinator"


def _engine(state_path, metrics=None, *, lower="2026-08-18T00:00:00Z",
            upper="2026-08-18T09:00:00Z"):
    # The path must stay patched for the engine's LIFETIME: _save() re-reads it.
    config.FANTASTIC_WATERMARK_STATE_PATH = state_path
    e = fja.DateCreatedWatermarkEngine(result=SimpleNamespace(jobs=[]),
                                       quota=fja._QuotaState(), http_get=None,
                                       seen_ids=set(),
                                       metrics=metrics if metrics is not None
                                       else {"segments": {}}, run_cap=100)
    e.lower, e.upper, e.opened = lower, upper, True
    e.metrics.setdefault("watermark", {})
    return e


def _seg(engine, label, stop_reason="", error_code=""):
    engine.metrics.setdefault("segments", {})[label] = {
        "stop_reason": stop_reason, "error_code": error_code}


# --------------------------------------------------------------------------
# Per-source drain state machine
# --------------------------------------------------------------------------
class DrainStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sp = str(Path(self.tmp.name) / "wm.json")
        _orig = config.FANTASTIC_WATERMARK_STATE_PATH
        self.addCleanup(lambda: setattr(config, "FANTASTIC_WATERMARK_STATE_PATH", _orig))

    # -- A: all sources drain -> window advances ------------------------------
    def test_a_all_sources_drained_advances(self):
        e = _engine(self.sp)
        for lbl in (ATS, LI, WF, YC):
            _seg(e, lbl, "empty_page")
            e.mark_source_drained(lbl)
        self.assertTrue(e.window_drained((ATS, LI, WF, YC)))

    # -- B: one truncated -> no advance, only that source resumes -------------
    def test_b_one_truncated_blocks_advance_and_only_it_resumes(self):
        e = _engine(self.sp)
        for lbl in (ATS, LI, YC):
            _seg(e, lbl, "empty_page")
            e.mark_source_drained(lbl)
        _seg(e, WF, "cap_reached")
        e.mark_source_drained(WF)
        self.assertFalse(e.window_drained((ATS, LI, WF, YC)))
        # The finished sources are skipped next run; only WF still owes inventory.
        self.assertTrue(e.source_already_drained(ATS))
        self.assertTrue(e.source_already_drained(LI))
        self.assertTrue(e.source_already_drained(YC))
        self.assertFalse(e.source_already_drained(WF))

    # -- C/D: failures never mark drained -------------------------------------
    def test_c_d_failures_never_mark_drained(self):
        for stop, err in (("request_error", "network_error:Timeout"),
                          ("request_error", "http_500"),
                          ("request_error", "http_503"),
                          ("rate_limited", ""),
                          ("cap_reached", ""),
                          ("jobs_quota_reserve", ""),
                          ("page_cap", "")):
            with self.subTest(stop=stop, err=err):
                e = _engine(self.sp)
                _seg(e, WF, stop, err)
                e.mark_source_drained(WF)
                self.assertFalse(e.source_already_drained(WF))
                self.assertFalse(e.window_drained((WF,)))

    # -- E: empty + complete -> drained ---------------------------------------
    def test_e_natural_end_marks_drained(self):
        for stop in ("", "empty_page", "short_page", "no_new_ids"):
            with self.subTest(stop=stop):
                e = _engine(self.sp)
                _seg(e, WF, stop)
                e.mark_source_drained(WF)
                self.assertTrue(e.source_already_drained(WF))

    # -- F: disabled source does not block ------------------------------------
    def test_f_disabled_source_does_not_block_advancement(self):
        e = _engine(self.sp)
        for lbl in (ATS, LI):
            _seg(e, lbl, "empty_page")
            e.mark_source_drained(lbl)
        # WF/YC are not enabled this run -> not in the enabled set.
        self.assertTrue(e.window_drained((ATS, LI)))

    # -- G: a newly enabled source re-opens the window ------------------------
    def test_g_newly_enabled_source_does_not_inherit_drained(self):
        """Policy: enabling a source mid-window makes the window NOT drained until
        that source pages it. It must never inherit another source's completion."""
        e = _engine(self.sp)
        for lbl in (ATS, LI):
            _seg(e, lbl, "empty_page")
            e.mark_source_drained(lbl)
        self.assertTrue(e.window_drained((ATS, LI)))
        self.assertFalse(e.source_already_drained(WF))
        self.assertFalse(e.window_drained((ATS, LI, WF)),
                         "a newly enabled source re-opens the canonical window")

    # -- H: restart restores drain state --------------------------------------
    def test_h_drain_state_survives_restart(self):
        e = _engine(self.sp)
        for lbl in (ATS, LI):
            _seg(e, lbl, "empty_page")
            e.mark_source_drained(lbl)
        _seg(e, WF, "cap_reached")
        e.mark_source_drained(WF)
        e.checkpoint((ATS, LI, WF))
        e2 = _engine(self.sp)          # fresh process reads persisted state
        self.assertTrue(e2.source_already_drained(ATS))
        self.assertTrue(e2.source_already_drained(LI))
        self.assertFalse(e2.source_already_drained(WF))
        self.assertFalse(json.loads(Path(self.sp).read_text(encoding="utf-8"))["window_drained"])

    # -- I: legacy state without the map --------------------------------------
    def test_i_legacy_state_without_per_source_map_is_safe(self):
        Path(self.sp).write_text(json.dumps({
            "schema": fja._WATERMARK_SCHEMA, "window_start": "2026-08-18T00:00:00Z",
            "in_flight_window_end": "2026-08-18T09:00:00Z", "window_drained": True,
            "window_acquired_ids": ["1", "2"]}), encoding="utf-8")
        e = _engine(self.sp)
        self.assertEqual(e.drained_sources(), {})
        self.assertFalse(e.source_already_drained(LI), "no source is presumed drained")
        self.assertFalse(e.window_drained((LI,)), "legacy state re-pages, never skips")

    # -- J: stale entry for a removed source cannot block forever -------------
    def test_j_stale_drain_entry_cannot_block_advancement(self):
        e = _engine(self.sp)
        for lbl in (ATS, LI):
            _seg(e, lbl, "empty_page")
            e.mark_source_drained(lbl)
        e.state["window_drained_sources"]["fantastic_jobs_removed_source"] = False
        self.assertTrue(e.window_drained((ATS, LI)),
                        "advancement is scoped to ENABLED sources only")

    def test_new_window_clears_all_drain_flags(self):
        e = _engine(self.sp)
        _seg(e, LI, "empty_page")
        e.mark_source_drained(LI)
        e.checkpoint((LI,))
        st = json.loads(Path(self.sp).read_text(encoding="utf-8"))
        st["in_flight_window_end"] = ""           # window committed
        st["last_successful_watermark"] = "2026-08-18T09:00:00Z"
        Path(self.sp).write_text(json.dumps(st), encoding="utf-8")
        config.FANTASTIC_WATERMARK_STATE_PATH = self.sp
        e2 = fja.DateCreatedWatermarkEngine(
            result=SimpleNamespace(jobs=[]), quota=fja._QuotaState(), http_get=None,
            seen_ids=set(), metrics={"segments": {}}, run_cap=100, now=NOW)
        e2.open()
        self.assertEqual(e2.drained_sources(), {}, "a NEW window starts un-drained")

    def test_drained_source_is_never_downgraded_within_a_window(self):
        e = _engine(self.sp)
        _seg(e, LI, "empty_page")
        e.mark_source_drained(LI)
        _seg(e, LI, "cap_reached")      # a later pass records a truncation
        e.mark_source_drained(LI)
        self.assertTrue(e.source_already_drained(LI))

    def test_empty_interval_is_trivially_drained(self):
        e = _engine(self.sp, lower="2026-08-18T09:00:00Z", upper="2026-08-18T09:00:00Z")
        self.assertTrue(e.window_drained((LI, WF)))


# --------------------------------------------------------------------------
# Experiment budgeting
# --------------------------------------------------------------------------
class ExperimentBudgetTest(unittest.TestCase):
    def test_arms_never_outspend_the_budget(self):
        for budget in (100, 600, 1200, 5000):
            for n in (1, 2, 4, 9):
                with self.subTest(budget=budget, arms=n):
                    arms = [ArmSpec(f"a{i}", ("linkedin",)) for i in range(n)]
                    plans = plan_arms(arms, max_budget=budget, min_per_arm=1)
                    self.assertLessEqual(sum(p.budget for p in plans), budget)
                    self.assertEqual(len(plans), n)
                    self.assertTrue(all(p.budget > 0 for p in plans))

    def test_indivisible_remainder_is_deterministic(self):
        arms = [ArmSpec(f"a{i}", ("linkedin",)) for i in range(4)]
        plans = plan_arms(arms, max_budget=3165, min_per_arm=1)
        self.assertEqual([p.budget for p in plans], [792, 791, 791, 791])
        self.assertEqual(sum(p.budget for p in plans), 3165)

    def test_refuses_configurations_that_cannot_fit(self):
        arms = [ArmSpec(f"a{i}", ("linkedin",)) for i in range(9)]
        with self.assertRaises(ExperimentBudgetError):
            plan_arms(arms, max_budget=600, min_per_arm=100)   # 900 needed
        with self.assertRaises(ExperimentBudgetError):
            plan_arms(arms, max_budget=0, min_per_arm=100)

    def test_per_source_limit_splits_within_the_arm(self):
        plans = plan_arms([ArmSpec("all_four", ("ats", "linkedin", "wellfound", "ycombinator"))],
                          max_budget=800, min_per_arm=100)
        self.assertEqual(plans[0].budget, 800)
        self.assertEqual(plans[0].per_source_limit, 200)

    def test_default_arm_set_shape(self):
        names = [a.name for a in R.default_arms(candidate_title_expression="x | y")]
        self.assertIn("control_ats_linkedin", names)
        for expected in ("ats_only", "linkedin_only", "wellfound_only", "ycombinator_only",
                         "ats_linkedin_wellfound", "ats_linkedin_yc", "all_four",
                         "title_expanded"):
            self.assertIn(expected, names)
        self.assertEqual(sum(1 for a in R.default_arms() if a.is_control), 1)


# --------------------------------------------------------------------------
# Experiment runner
# --------------------------------------------------------------------------
class ExperimentRunnerTest(unittest.TestCase):
    def _cfg(self, **over):
        base = dict(SOURCE_EXPERIMENT_ENABLED=True, SOURCE_EXPERIMENT_MAX_BUDGET=800,
                    SOURCE_EXPERIMENT_MIN_PER_ARM=100, SOURCE_EXPERIMENT_ARTIFACT_DIR="",
                    FANTASTIC_ATS_SOURCE_ENABLED=False, FANTASTIC_JOBS_ATS_LIMIT=0,
                    FANTASTIC_JOBS_LINKEDIN_LIMIT=0,
                    FANTASTIC_WELLFOUND_SOURCE_ENABLED=False, FANTASTIC_JOBS_WELLFOUND_LIMIT=0,
                    FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=False, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
                    FANTASTIC_SOURCE_ALLOCATION="sequential",
                    FANTASTIC_JOBS_RUN_SLICE_CAP=0)
        base.update(over)
        return SimpleNamespace(**base)

    @staticmethod
    def _fake_acquire(cfg, per_arm_jobs):
        """Returns a result whose ids depend on which sources the arm enabled."""
        def acquire():
            enabled = []
            if cfg.FANTASTIC_ATS_SOURCE_ENABLED and cfg.FANTASTIC_JOBS_ATS_LIMIT:
                enabled.append("ats")
            if cfg.FANTASTIC_JOBS_LINKEDIN_LIMIT:
                enabled.append("linkedin")
            if cfg.FANTASTIC_WELLFOUND_SOURCE_ENABLED and cfg.FANTASTIC_JOBS_WELLFOUND_LIMIT:
                enabled.append("wellfound")
            if cfg.FANTASTIC_YCOMBINATOR_SOURCE_ENABLED and cfg.FANTASTIC_JOBS_YCOMBINATOR_LIMIT:
                enabled.append("ycombinator")
            jobs, per_source = [], {}
            for src in enabled:
                ids = [f"{src}-{i}" for i in range(per_arm_jobs)]
                jobs += [{"_fantastic_internal_id": i, "_acquisition_source": src} for i in ids]
                per_source[f"fantastic_jobs_{src}"] = {"returned_billed": per_arm_jobs}
            return SimpleNamespace(jobs=jobs, metadata={
                "jobs_quota_consumed": per_arm_jobs * len(enabled),
                "cross_source_duplicates": 0, "stop_reason": "",
                "source_attribution": {"per_source": per_source},
                "source_allocation": {"invariant_ok": True}})
        return acquire

    def test_disabled_by_default(self):
        cfg = self._cfg(SOURCE_EXPERIMENT_ENABLED=False)
        out = R.run_experiment(cfg=cfg, acquire=lambda: None)
        self.assertFalse(out["enabled"])

    def test_all_arms_run_and_respect_the_budget(self):
        cfg = self._cfg(SOURCE_EXPERIMENT_MAX_BUDGET=1600)
        out = R.run_experiment(cfg=cfg, acquire=self._fake_acquire(cfg, 10),
                               arms=R.default_arms())
        self.assertTrue(out["enabled"])
        self.assertEqual(len(out["arms"]), 8)
        self.assertLessEqual(out["total_budget_granted"], 1600)
        self.assertTrue(out["budget_invariant_ok"])

    def test_control_config_is_restored_after_every_arm(self):
        cfg = self._cfg()
        before = {k: getattr(cfg, k) for k in vars(cfg)}
        R.run_experiment(cfg=cfg, acquire=self._fake_acquire(cfg, 5), arms=R.default_arms())
        after = {k: getattr(cfg, k) for k in vars(cfg)}
        self.assertEqual(before, after, "an arm must never leak config into the next")

    def test_incremental_vs_control_distinguishes_yield_from_new_inventory(self):
        cfg = self._cfg(SOURCE_EXPERIMENT_MAX_BUDGET=1600)
        out = R.run_experiment(cfg=cfg, acquire=self._fake_acquire(cfg, 10),
                               arms=R.default_arms())
        comp = out["comparison"]["arms"]
        self.assertEqual(comp["control_ats_linkedin"]["incremental_vs_control"], 0)
        # wellfound_only shares nothing with control -> all of it is incremental
        self.assertEqual(comp["wellfound_only"]["incremental_vs_control"],
                         comp["wellfound_only"]["unique_jobs"])
        # all_four contains control's inventory plus WF+YC
        self.assertGreater(comp["all_four"]["overlap_with_control"], 0)
        self.assertGreater(comp["all_four"]["incremental_vs_control"], 0)
        for stats in comp.values():
            self.assertIsNotNone(stats["unique_per_100_credits"])

    def test_account_level_failure_aborts_the_experiment(self):
        cfg = self._cfg()
        calls = {"n": 0}

        def acquire():
            calls["n"] += 1
            return SimpleNamespace(jobs=[], metadata={
                "jobs_quota_consumed": 1, "stop_reason": "rate_limited",
                "source_attribution": {"per_source": {}}})
        out = R.run_experiment(cfg=cfg, acquire=acquire, arms=R.default_arms())
        self.assertEqual(out["aborted"], "rate_limited")
        self.assertEqual(calls["n"], 1, "no further arm may spend after an account failure")

    def test_downstream_metrics_absent_unless_supplied(self):
        cfg = self._cfg()
        out = R.run_experiment(cfg=cfg, acquire=self._fake_acquire(cfg, 5),
                               arms=[ArmSpec("linkedin_only", ("linkedin",), is_control=True)])
        arm = out["arms"]["linkedin_only"]
        for fabricated in ("verified_email", "send_safe", "apollo_credits"):
            self.assertEqual(arm[fabricated], 0,
                             "downstream rates must not be modelled without evidence")

    def test_downstream_evaluator_is_used_when_provided(self):
        cfg = self._cfg()
        out = R.run_experiment(cfg=cfg, acquire=self._fake_acquire(cfg, 5),
                               arms=[ArmSpec("linkedin_only", ("linkedin",), is_control=True)],
                               downstream=lambda jobs: {"icp_pass": len(jobs), "send_safe": 2})
        self.assertEqual(out["arms"]["linkedin_only"]["icp_pass"], 5)
        self.assertEqual(out["arms"]["linkedin_only"]["send_safe"], 2)

    def test_artifact_written_when_directory_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(SOURCE_EXPERIMENT_ARTIFACT_DIR=tmp)
            out = R.run_experiment(cfg=cfg, acquire=self._fake_acquire(cfg, 3),
                                   arms=[ArmSpec("linkedin_only", ("linkedin",), is_control=True)])
            self.assertTrue(out.get("artifact"))
            self.assertTrue(os.path.exists(out["artifact"]))


# --------------------------------------------------------------------------
# Title arm: CONTROL must stay byte-identical to production
# --------------------------------------------------------------------------
class TitleArmTest(unittest.TestCase):
    def test_control_expression_is_unchanged_by_the_candidate_set(self):
        base_before = fja.build_title_query_plan()["expression"]
        cand = fja.candidate_title_expression()
        base_after = fja.build_title_query_plan()["expression"]
        self.assertEqual(base_before, base_after,
                         "building the arm must not mutate the production expression")
        self.assertTrue(cand.startswith(base_before),
                        "CONTROL is a strict prefix of the candidate expression")

    def test_candidate_titles_add_only_new_terms(self):
        base = fja.build_title_query_plan()["expression"]
        cand = fja.candidate_title_expression()
        added = [c for c in cand.split(" | ") if c not in base.split(" | ")]
        self.assertEqual(len(added), len(config.FANTASTIC_CANDIDATE_TITLES))
        self.assertEqual(len(set(added)), len(added), "no duplicate candidate clause")

    def test_policy_questionable_titles_are_excluded(self):
        for banned in ("Controller", "Product Manager", "Technical Product Manager",
                       "Product Owner", "Territory Manager", "Renewals Manager",
                       "Demand Generation Manager"):
            self.assertNotIn(banned, config.FANTASTIC_CANDIDATE_TITLES,
                             f"{banned} needs a role-policy ruling before entering an arm")

    def test_every_candidate_maps_to_a_real_function_bucket(self):
        import role_catalog as rc
        buckets = {r.function_bucket for r in rc._ROLE_DEFINITIONS}
        for title, fam in config.FANTASTIC_CANDIDATE_TITLES.items():
            with self.subTest(title=title):
                self.assertIn(fam, buckets, f"{title} maps to unknown bucket {fam}")

    def test_candidates_are_not_already_covered_by_control(self):
        import re
        base = fja.build_title_query_plan()["expression"]
        terms = [t.strip().strip("'") for t in base.split(" | ")]
        for title in config.FANTASTIC_CANDIDATE_TITLES:
            with self.subTest(title=title):
                n = re.sub(r"[^a-z0-9 ]", " ", title.lower())
                n = re.sub(r"\s+", " ", n).strip()
                self.assertFalse(any(t and t in n for t in terms),
                                 f"{title} is already covered; it would add nothing")


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# 1. Checkpoint call path: the enabled set is authoritative
# --------------------------------------------------------------------------
class CheckpointCallPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sp = str(Path(self.tmp.name) / "wm.json")
        _orig = config.FANTASTIC_WATERMARK_STATE_PATH
        self.addCleanup(lambda: setattr(config, "FANTASTIC_WATERMARK_STATE_PATH", _orig))

    def test_single_checkpoint_call_site(self):
        import inspect
        src = inspect.getsource(fja)
        self.assertEqual(src.count("watermark_engine.checkpoint("), 1)
        self.assertEqual(src.count("def window_drained"), 1)
        self.assertEqual(src.count("def checkpoint"), 1)

    def test_enabled_set_is_required_no_metrics_fallback(self):
        import inspect
        sig = inspect.signature(fja.DateCreatedWatermarkEngine.window_drained)
        self.assertIs(sig.parameters["enabled_labels"].default, inspect.Parameter.empty,
                      "enabled_labels must be REQUIRED, never defaulted to metrics keys")
        body = inspect.getsource(fja.DateCreatedWatermarkEngine.window_drained)
        self.assertNotIn("metrics.get(\"segments\")", body)

    def test_one_undrained_source_blocks_the_global_window(self):
        """ATS/LinkedIn/YC drained, Wellfound enabled but NOT drained -> no advance."""
        e = _engine(self.sp)
        for lbl in (ATS, LI, YC):
            _seg(e, lbl, "empty_page")
            e.mark_source_drained(lbl)
        _seg(e, WF, "cap_reached")
        e.mark_source_drained(WF)
        e.checkpoint((ATS, LI, WF, YC))
        st = json.loads(Path(self.sp).read_text(encoding="utf-8"))
        self.assertFalse(st["window_drained"], "canonical window must NOT advance")
        self.assertTrue(st["window_drained_sources"][WF] is False)
        self.assertTrue(st["window_drained_sources"][ATS])

    def test_executed_segments_alone_cannot_advance_the_window(self):
        """Only ATS ran (LinkedIn never got budget) -> the window must stay open."""
        e = _engine(self.sp)
        _seg(e, ATS, "empty_page")
        e.mark_source_drained(ATS)
        e.checkpoint((ATS, LI))          # LinkedIn enabled but absent from metrics
        self.assertFalse(json.loads(Path(self.sp).read_text(encoding="utf-8"))["window_drained"])


# --------------------------------------------------------------------------
# 3. First-enablement bootstrap
# --------------------------------------------------------------------------
class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sp = str(Path(self.tmp.name) / "wm.json")
        _orig = config.FANTASTIC_WATERMARK_STATE_PATH
        self.addCleanup(lambda: setattr(config, "FANTASTIC_WATERMARK_STATE_PATH", _orig))

    def _advanced_engine(self, *, pre_existing=(LI, ATS)):
        """A watermark that has been advancing, with ``pre_existing`` sources already
        recorded as having no backfill debt -- i.e. the migration has happened and
        anything NOT listed is a genuine first enablement."""
        e = _engine(self.sp, lower="2026-08-18T06:00:00Z", upper="2026-08-18T09:00:00Z")
        e.state["last_successful_watermark"] = "2026-08-18T06:00:00Z"
        e.state["source_bootstrap"] = {
            lbl: {"lower": e.lower, "upper": e.lower, "drained": True,
                  "reason": "pre_existing_source_at_upgrade"} for lbl in pre_existing}
        return e

    def test_upgrade_grants_no_backfill_to_already_live_sources(self):
        """Regression: production state written before this code existed has an
        advanced watermark and NO ``source_bootstrap`` key. Treating that as "these
        sources are new" handed every LIVE source a full-lookback re-page of
        inventory it had already processed -- funded by the reserve, on every run."""
        with mock.patch.object(config, "FANTASTIC_JOBS_TIME_FRAME", "7d"):
            e = _engine(self.sp, lower="2026-08-18T06:00:00Z", upper="2026-08-18T09:00:00Z")
            e.state["last_successful_watermark"] = "2026-08-18T06:00:00Z"
            self.assertNotIn("source_bootstrap", e.state)      # pre-upgrade shape
            e.ensure_bootstraps((LI, ATS))
            for lbl in (LI, ATS):
                self.assertIsNone(e.bootstrap_pending(lbl),
                                  f"{lbl} was already live and owes no backfill")
                self.assertEqual(e.state["source_bootstrap"][lbl]["reason"],
                                 "pre_existing_source_at_upgrade")

    def test_source_enabled_after_the_upgrade_is_still_a_newcomer(self):
        """The migration must not swallow REAL first enablements: once the key
        exists, a label missing from it still earns its bounded backfill."""
        with mock.patch.object(config, "FANTASTIC_JOBS_TIME_FRAME", "7d"):
            e = _engine(self.sp, lower="2026-08-18T06:00:00Z", upper="2026-08-18T09:00:00Z")
            e.state["last_successful_watermark"] = "2026-08-18T06:00:00Z"
            e.ensure_bootstraps((LI, ATS))                     # the upgrade run
            e.ensure_bootstraps((LI, ATS, WF))                 # Wellfound enabled later
        self.assertIsNone(e.bootstrap_pending(LI))
        self.assertIsNone(e.bootstrap_pending(ATS))
        rec = e.bootstrap_pending(WF)
        self.assertIsNotNone(rec, "a genuine first enablement still gets a backfill")
        self.assertEqual(rec["upper"], "2026-08-18T06:00:00Z")
        self.assertEqual(rec["lower"], "2026-08-11T09:00:00Z")

    def test_first_ever_run_records_no_backfill_debt(self):
        e = _engine(self.sp)              # no last_successful_watermark
        e.ensure_bootstraps((LI, ATS))
        for lbl in (LI, ATS):
            self.assertIsNone(e.bootstrap_pending(lbl))

    def test_new_source_gets_a_bounded_historical_window(self):
        with mock.patch.object(config, "FANTASTIC_JOBS_TIME_FRAME", "7d"):
            e = self._advanced_engine()
            e.ensure_bootstraps((LI, WF))
            rec = e.bootstrap_pending(WF)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["upper"], "2026-08-18T06:00:00Z", "ends at the canonical lower")
        self.assertEqual(rec["lower"], "2026-08-11T09:00:00Z", "bounded by the 7d lookback")

    def test_bootstrap_never_moves_the_canonical_watermark(self):
        with mock.patch.object(config, "FANTASTIC_JOBS_TIME_FRAME", "7d"):
            e = self._advanced_engine()
            e.ensure_bootstraps((LI, WF))
            before = e.state["last_successful_watermark"]
            _seg(e, f"{WF}::bootstrap", "empty_page")
            e.mark_bootstrap_drained(WF)
            e.checkpoint((LI, WF))
        self.assertEqual(e.state["last_successful_watermark"], before)
        # A drained BOOTSTRAP does not make the canonical window drained.
        self.assertFalse(json.loads(Path(self.sp).read_text(encoding="utf-8"))["window_drained"])

    def test_interrupted_bootstrap_resumes_the_same_window(self):
        with mock.patch.object(config, "FANTASTIC_JOBS_TIME_FRAME", "7d"):
            e = self._advanced_engine()
            e.ensure_bootstraps((WF,))
            rec1 = dict(e.bootstrap_pending(WF))
            _seg(e, f"{WF}::bootstrap", "cap_reached")     # truncated
            e.mark_bootstrap_drained(WF)
            e.checkpoint((WF,))
            e2 = _engine(self.sp, lower="2026-08-18T06:00:00Z", upper="2026-08-18T09:00:00Z")
            e2.state["last_successful_watermark"] = "2026-08-18T06:00:00Z"
            e2.ensure_bootstraps((WF,))
            rec2 = e2.bootstrap_pending(WF)
        self.assertIsNotNone(rec2, "an interrupted bootstrap must resume")
        self.assertEqual((rec2["lower"], rec2["upper"]), (rec1["lower"], rec1["upper"]))

    def test_completed_bootstrap_is_never_repeated(self):
        with mock.patch.object(config, "FANTASTIC_JOBS_TIME_FRAME", "7d"):
            e = self._advanced_engine()
            e.ensure_bootstraps((WF,))
            _seg(e, f"{WF}::bootstrap", "empty_page")
            e.mark_bootstrap_drained(WF)
            e.checkpoint((WF,))
            e2 = _engine(self.sp, lower="2026-08-18T06:00:00Z", upper="2026-08-18T09:00:00Z")
            e2.state["last_successful_watermark"] = "2026-08-18T06:00:00Z"
            e2.ensure_bootstraps((WF,))
        self.assertIsNone(e2.bootstrap_pending(WF), "a drained bootstrap never re-runs")

    def test_disabled_mid_bootstrap_then_re_enabled_resumes(self):
        with mock.patch.object(config, "FANTASTIC_JOBS_TIME_FRAME", "7d"):
            e = self._advanced_engine()
            e.ensure_bootstraps((WF,))
            rec1 = dict(e.bootstrap_pending(WF))
            _seg(e, f"{WF}::bootstrap", "cap_reached")
            e.mark_bootstrap_drained(WF)
            e.checkpoint((WF,))
            # WF disabled: a run WITHOUT it must not disturb its record.
            e2 = _engine(self.sp, lower="2026-08-18T06:00:00Z", upper="2026-08-18T09:00:00Z")
            e2.state["last_successful_watermark"] = "2026-08-18T06:00:00Z"
            e2.ensure_bootstraps((LI,))
            e2.checkpoint((LI,))
            # WF re-enabled later.
            e3 = _engine(self.sp, lower="2026-08-18T06:00:00Z", upper="2026-08-18T09:00:00Z")
            e3.state["last_successful_watermark"] = "2026-08-18T06:00:00Z"
            e3.ensure_bootstraps((LI, WF))
            rec3 = e3.bootstrap_pending(WF)
        self.assertIsNotNone(rec3)
        self.assertEqual((rec3["lower"], rec3["upper"]), (rec1["lower"], rec1["upper"]))

    def test_bootstrap_can_be_disabled(self):
        with mock.patch.object(config, "FANTASTIC_SOURCE_BOOTSTRAP_ENABLED", False):
            e = self._advanced_engine()
            e.ensure_bootstraps((WF,))
        self.assertIsNone(e.bootstrap_pending(WF))


# --------------------------------------------------------------------------
# 4/5. Experiment state isolation + one shared window
# --------------------------------------------------------------------------
class ExperimentIsolationTest(unittest.TestCase):
    PROD_KEYS = ("FANTASTIC_WATERMARK_STATE_PATH", "FANTASTIC_JOBS_CONTINUATION_STATE_PATH",
                 "FANTASTIC_QUOTA_SNAPSHOT_PATH", "FANTASTIC_GOVERNOR_LEDGER_PATH",
                 "FANTASTIC_SLUG_CROSSWALK_PATH", "YIELD_LEDGER_PATH")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prod = Path(self.tmp.name) / "prod"
        self.prod.mkdir()
        self.paths = {k: str(self.prod / f"{k.lower()}.json") for k in self.PROD_KEYS}
        for p in self.paths.values():
            Path(p).write_text(json.dumps({"production": "state", "key": p}), encoding="utf-8")

    def _cfg(self, **over):
        base = dict(SOURCE_EXPERIMENT_ENABLED=True, SOURCE_EXPERIMENT_MAX_BUDGET=800,
                    SOURCE_EXPERIMENT_MIN_PER_ARM=100,
                    SOURCE_EXPERIMENT_ARTIFACT_DIR=str(Path(self.tmp.name) / "artifacts"),
                    FANTASTIC_ATS_SOURCE_ENABLED=False, FANTASTIC_JOBS_ATS_LIMIT=0,
                    FANTASTIC_JOBS_LINKEDIN_LIMIT=0,
                    FANTASTIC_WELLFOUND_SOURCE_ENABLED=False, FANTASTIC_JOBS_WELLFOUND_LIMIT=0,
                    FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=False, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
                    FANTASTIC_SOURCE_ALLOCATION="fair_share", FANTASTIC_JOBS_RUN_SLICE_CAP=0,
                    FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=False)
        base.update(self.paths)
        base.update(over)
        return SimpleNamespace(**base)

    def _snapshot(self):
        return {p: Path(p).read_bytes() for p in self.paths.values()}

    def test_production_state_is_byte_identical_after_an_experiment(self):
        cfg = self._cfg()
        before = self._snapshot()
        seen_paths = []

        def acquire():
            # An arm that WRITES to whatever paths config currently points at.
            for key in self.PROD_KEYS:
                target = getattr(cfg, key)
                seen_paths.append(target)
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                Path(target).write_text(json.dumps({"arm": "wrote-here"}), encoding="utf-8")
            return SimpleNamespace(jobs=[], metadata={
                "jobs_quota_consumed": 1, "stop_reason": "",
                "source_attribution": {"per_source": {}}})

        R.run_experiment(cfg=cfg, acquire=acquire, arms=R.default_arms(),
                         state_root=str(Path(self.tmp.name) / "exp_state"))
        self.assertEqual(self._snapshot(), before,
                         "production acquisition state must be BYTE-IDENTICAL")
        for p in self.paths.values():
            self.assertNotIn(p, seen_paths, "no arm may target a production state path")

    def test_each_arm_gets_its_own_state_namespace(self):
        cfg = self._cfg()
        per_arm = {}

        def acquire():
            per_arm.setdefault(getattr(cfg, "FANTASTIC_WATERMARK_STATE_PATH"), 0)
            per_arm[getattr(cfg, "FANTASTIC_WATERMARK_STATE_PATH")] += 1
            return SimpleNamespace(jobs=[], metadata={
                "jobs_quota_consumed": 1, "stop_reason": "",
                "source_attribution": {"per_source": {}}})

        arms = R.default_arms()
        R.run_experiment(cfg=cfg, acquire=acquire, arms=arms,
                         state_root=str(Path(self.tmp.name) / "exp_state"))
        self.assertEqual(len(per_arm), len(arms), "arm A must not inherit arm B's cursor")
        self.assertTrue(all(v == 1 for v in per_arm.values()))

    def test_state_paths_are_restored_after_every_arm(self):
        cfg = self._cfg()
        R.run_experiment(cfg=cfg, acquire=lambda: SimpleNamespace(
            jobs=[], metadata={"jobs_quota_consumed": 0, "stop_reason": "",
                               "source_attribution": {"per_source": {}}}),
            arms=R.default_arms(), state_root=str(Path(self.tmp.name) / "exp_state"))
        for key, expected in self.paths.items():
            self.assertEqual(getattr(cfg, key), expected,
                             "production state paths must be restored")

    def test_every_arm_uses_the_same_experiment_window(self):
        cfg = self._cfg()
        windows = []

        def acquire():
            wm = json.loads(Path(getattr(cfg, "FANTASTIC_WATERMARK_STATE_PATH")).read_text(
                encoding="utf-8"))
            windows.append((wm["window_start"], wm["in_flight_window_end"]))
            self.assertTrue(getattr(cfg, "FANTASTIC_DATE_CREATED_WATERMARK_ENABLED"))
            return SimpleNamespace(jobs=[], metadata={
                "jobs_quota_consumed": 1, "stop_reason": "",
                "source_attribution": {"per_source": {}}})

        win = ("2026-08-11T00:00:00Z", "2026-08-18T00:00:00Z")
        out = R.run_experiment(cfg=cfg, acquire=acquire, arms=R.default_arms(), window=win,
                               state_root=str(Path(self.tmp.name) / "exp_state"))
        self.assertEqual(len(set(windows)), 1, f"arms compared different windows: {set(windows)}")
        self.assertEqual(windows[0], win)
        self.assertEqual(out["window"], {"lower": win[0], "upper": win[1]},
                         "the compared window must be recorded in the artifact")

    def test_seeded_window_starts_with_no_drain_or_bootstrap_state(self):
        p = str(Path(self.tmp.name) / "seed" / "wm.json")
        R._seed_equal_window(p, "2026-08-11T00:00:00Z", "2026-08-18T00:00:00Z")
        st = json.loads(Path(p).read_text(encoding="utf-8"))
        self.assertEqual(st["window_drained_sources"], {})
        self.assertEqual(st["source_bootstrap"], {})
        self.assertEqual(st["last_successful_watermark"], "")


# --------------------------------------------------------------------------
# Experiment: no bootstrap contamination + ONE global credit ceiling
# --------------------------------------------------------------------------
class ExperimentBootstrapIsolationTest(unittest.TestCase):
    def test_every_arm_disables_first_enablement_bootstrap(self):
        """An arm's state starts empty, so ensure_bootstraps() would hand each
        source an EXTRA historical window outside the experiment window -- spending
        arm budget on non-comparable inventory. Arms must switch it off."""
        for arm in R.default_arms(candidate_title_expression="x"):
            with self.subTest(arm=arm.name):
                over = arm.config_overrides(100)
                self.assertIn("FANTASTIC_SOURCE_BOOTSTRAP_ENABLED", over)
                self.assertFalse(over["FANTASTIC_SOURCE_BOOTSTRAP_ENABLED"])

    def test_arm_sees_bootstrap_disabled_even_when_production_enables_it(self):
        cfg = SimpleNamespace(
            SOURCE_EXPERIMENT_ENABLED=True, SOURCE_EXPERIMENT_MAX_BUDGET=400,
            SOURCE_EXPERIMENT_MIN_PER_ARM=100, SOURCE_EXPERIMENT_ARTIFACT_DIR="",
            FANTASTIC_SOURCE_BOOTSTRAP_ENABLED=True,          # production value
            FANTASTIC_ATS_SOURCE_ENABLED=False, FANTASTIC_JOBS_ATS_LIMIT=0,
            FANTASTIC_JOBS_LINKEDIN_LIMIT=0,
            FANTASTIC_WELLFOUND_SOURCE_ENABLED=False, FANTASTIC_JOBS_WELLFOUND_LIMIT=0,
            FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=False, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
            FANTASTIC_SOURCE_ALLOCATION="sequential", FANTASTIC_JOBS_RUN_SLICE_CAP=0,
            FANTASTIC_WATERMARK_STATE_PATH="prod-wm.json",
            FANTASTIC_JOBS_CONTINUATION_STATE_PATH="prod-cont.json",
            FANTASTIC_QUOTA_SNAPSHOT_PATH="prod-quota.json",
            FANTASTIC_GOVERNOR_LEDGER_PATH="prod-ledger.json",
            FANTASTIC_SLUG_CROSSWALK_PATH="prod-slug.json",
            YIELD_LEDGER_PATH="prod-yield.jsonl")
        seen = []

        def acquire():
            seen.append(getattr(cfg, "FANTASTIC_SOURCE_BOOTSTRAP_ENABLED"))
            return SimpleNamespace(jobs=[], metadata={
                "jobs_quota_consumed": 0, "stop_reason": "",
                "source_attribution": {"per_source": {}}})

        with tempfile.TemporaryDirectory() as tmp:
            R.run_experiment(cfg=cfg, acquire=acquire,
                             arms=[ArmSpec("linkedin_only", ("linkedin",), is_control=True)],
                             state_root=tmp)
        self.assertEqual(seen, [False], "the arm must run with bootstrap OFF")
        self.assertTrue(cfg.FANTASTIC_SOURCE_BOOTSTRAP_ENABLED,
                        "production setting must be restored afterwards")

    def test_isolated_quota_snapshot_does_not_grant_each_arm_a_fresh_quota(self):
        """Per-arm state isolation protects production state; it must NOT let each
        arm believe it owns the whole monthly provider quota. The ONE authoritative
        ceiling stays SOURCE_EXPERIMENT_MAX_BUDGET across ALL arms."""
        max_budget = 800
        cfg = SimpleNamespace(
            SOURCE_EXPERIMENT_ENABLED=True, SOURCE_EXPERIMENT_MAX_BUDGET=max_budget,
            SOURCE_EXPERIMENT_MIN_PER_ARM=100, SOURCE_EXPERIMENT_ARTIFACT_DIR="",
            FANTASTIC_SOURCE_BOOTSTRAP_ENABLED=True,
            FANTASTIC_ATS_SOURCE_ENABLED=False, FANTASTIC_JOBS_ATS_LIMIT=0,
            FANTASTIC_JOBS_LINKEDIN_LIMIT=0,
            FANTASTIC_WELLFOUND_SOURCE_ENABLED=False, FANTASTIC_JOBS_WELLFOUND_LIMIT=0,
            FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=False, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
            FANTASTIC_SOURCE_ALLOCATION="sequential", FANTASTIC_JOBS_RUN_SLICE_CAP=0,
            FANTASTIC_WATERMARK_STATE_PATH="p1", FANTASTIC_JOBS_CONTINUATION_STATE_PATH="p2",
            FANTASTIC_QUOTA_SNAPSHOT_PATH="p3", FANTASTIC_GOVERNOR_LEDGER_PATH="p4",
            FANTASTIC_SLUG_CROSSWALK_PATH="p5", YIELD_LEDGER_PATH="p6")
        slices = []

        def acquire():
            # Each arm's isolated snapshot claims a FULL fresh quota; the arm must
            # still only be able to spend its own slice.
            slice_cap = int(getattr(cfg, "FANTASTIC_JOBS_RUN_SLICE_CAP"))
            slices.append(slice_cap)
            return SimpleNamespace(jobs=[], metadata={
                "jobs_quota_consumed": slice_cap,       # spends its entire slice
                "stop_reason": "", "source_attribution": {"per_source": {}}})

        with tempfile.TemporaryDirectory() as tmp:
            out = R.run_experiment(cfg=cfg, acquire=acquire, arms=R.default_arms(),
                                   state_root=tmp)
        self.assertTrue(all(s > 0 for s in slices), "every arm must be slice-capped")
        self.assertEqual(sum(slices), out["total_budget_granted"])
        self.assertLessEqual(out["total_billed"], max_budget,
                             "sum across ALL arms must respect the ONE experiment ceiling")
        self.assertTrue(out["budget_invariant_ok"])
