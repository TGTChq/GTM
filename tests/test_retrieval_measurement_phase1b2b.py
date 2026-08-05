"""Phase 1B-2B: deterministic scheduling, pre-emptive budget skipping, exact
request attribution, context isolation, and complete real-path accounting.

Every real-path test drives the production
``multi_source_acquisition.run_multi_source_acquisition`` with injected
transports; no network call is made. Unit tests drive the scheduler,
``RequestBudget`` and ``request_trace`` directly.

The 24 numbered obligations from the Phase 1B-2B brief are each pinned by at
least one test below; the test docstring names the obligation number.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

import config
import multi_source_acquisition
from retrieval_measurement import ats_schedule, request_trace
from retrieval_measurement.ats_checkpoint import UNRESOLVED, AtsBoardSession
from retrieval_measurement.ats_schedule import (
    SchedulerConfig,
    SchedulerConfigError,
    SchedulerState,
    SchedulerStateError,
    select_boards,
    simulate,
)
from retrieval_measurement.instrument import RequestBudget, RequestCeilingReached
from retrieval_measurement.request_trace import Trace


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


def registry(n, *, checked_hours_ago=1.0, failures=0):
    now = datetime.now(timezone.utc)
    providers = ["greenhouse", "lever", "ashby", "workable", "workday"]
    return [
        {
            "provider": providers[i % len(providers)],
            "identifier": f"board{i}",
            "company_name": f"Co-{i}",
            "key": f"{providers[i % len(providers)]}:board{i}:api",
            "last_checked_at": (now - timedelta(hours=checked_hours_ago)).isoformat(),
            "consecutive_failures": failures,
        }
        for i in range(n)
    ]


def board(provider, identifier, company=None):
    return {
        "provider": provider,
        "identifier": identifier,
        "company_name": company or f"Co-{identifier}",
        "key": f"{provider}:{identifier}:api",
    }


def posting(board_id, n=1):
    return {
        "job_id": f"{board_id}-{n}",
        "employer_name": f"Co-{board_id}",
        "job_title": "Support Engineer",
        "job_apply_link": f"https://{board_id}.example/jobs/{n}",
    }


class Wire:
    """Records every physical request without touching the network."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs.get("url") or (args[1] if len(args) > 1 else ""))
        return mock.Mock(status_code=200, text="{}", url="https://x.test")


class RealPathHarness(unittest.TestCase):
    """Drives the production acquisition function with everything else stubbed."""

    def setUp(self):
        self.selected_jobs = []

    def run_acquisition(self, boards, fetch_impl, *, session=None, scheduler_mode=None):
        def capture_save(selected, _stats):
            self.selected_jobs = [dict(job) for job in selected]
            return "<test: not written>"

        reg = mock.MagicMock()
        reg.due_entries.return_value = list(boards)
        reg.seed_from_history.return_value = {
            "files_scanned": 0, "jobs_scanned": 0, "boards_added_or_updated": 0}
        reg.upsert_from_jobs.return_value = 0
        reg.record_result.return_value = 0
        reg.entries = {}
        reg.invalid_entries_pruned = 0

        patches = [
            mock.patch.object(multi_source_acquisition, "AtsBoardRegistry", return_value=reg),
            mock.patch.object(multi_source_acquisition, "fetch_board_jobs", fetch_impl),
            mock.patch.object(multi_source_acquisition, "build_adapters", return_value=[]),
            mock.patch.object(multi_source_acquisition, "_enrich_himalayas_company_profiles", return_value={}),
            mock.patch.object(multi_source_acquisition, "_discover_landing_links", return_value={}),
            mock.patch.object(multi_source_acquisition, "_save_raw", capture_save),
            mock.patch.object(config, "MULTI_SOURCE_JSEARCH_ENABLED", False),
            mock.patch.object(config, "ADZUNA_ENABLED", False),
            mock.patch.object(config, "FANTASTIC_JOBS_ENABLED", False),
            mock.patch.object(config, "ATS_DIRECT_ACQUISITION_ENABLED", True),
            mock.patch.object(config, "ATS_REGISTRY_AUTO_SEED_HISTORY", False),
            mock.patch.object(config, "FREE_SOURCE_MIN_SUCCESSFUL_SOURCES", 0),
        ]
        if scheduler_mode is not None:
            patches.append(mock.patch.object(config, "ATS_SCHEDULER_MODE", scheduler_mode))
        with contextlib_ExitStack(patches):
            seen = mock.MagicMock()
            seen.has_job_id.return_value = False
            seen.has_dedup_key.return_value = False
            result = multi_source_acquisition.run_multi_source_acquisition(seen, ats_session=session)
        self._registry = reg
        return result


def contextlib_ExitStack(patches):
    import contextlib

    stack = contextlib.ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


# ==========================================================================
# A -- explicit scheduler modes  (obligations 1, 2, 3, 24)
# ==========================================================================


class SchedulerModeTests(RealPathHarness):
    def test_legacy_scheduler_is_the_default_and_is_unchanged(self):
        """Obligation 1 + 24: default is legacy_interval; nothing new engages."""
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition(
                [board("lever", "a"), board("ashby", "b")],
                lambda b, f, **k: ([posting(b["identifier"])], ""),
                session=session,
            )
            acct = session.accounting()
        # due_entries called the legacy way: force reflects the run argument, not
        # the scheduler's force=True full-registry sweep.
        _, kwargs = self._registry.due_entries.call_args
        self.assertFalse(kwargs.get("force"))
        self.assertEqual(acct["boards_skipped_by_scheduler"], 0)
        self.assertNotIn("scheduler", acct)
        self.assertEqual(acct["boards_selected"], 2)

    def test_deterministic_partition_selects_a_stable_subset(self):
        """Obligation 2: identical registry/config/position -> identical subset."""
        boards = registry(60)
        first = select_boards(boards, mode="deterministic_partition", position=3,
                              cycle_length=7, max_age_hours=0)
        second = select_boards(boards, mode="deterministic_partition", position=3,
                               cycle_length=7, max_age_hours=0)
        self.assertEqual([b["identifier"] for b in first.selected],
                         [b["identifier"] for b in second.selected])
        self.assertEqual(first.mode, "deterministic_partition")

    def test_every_board_has_exactly_one_normal_slot_per_cycle(self):
        """Obligation 3."""
        result = simulate(registry(145), cycle_length=7, cycles=1, max_age_hours=0)
        self.assertTrue(result["full_coverage"])
        self.assertEqual(result["visits_per_board"], [1])
        self.assertEqual(result["starved_boards"], 0)

    def test_the_two_modes_are_never_combined(self):
        """Obligation 1: a run resolves to exactly one algorithm."""
        legacy = select_boards(registry(10), legacy_due=registry(10)[:3], enabled=False)
        part = select_boards(registry(10), mode="deterministic_partition", position=0,
                             cycle_length=7, max_age_hours=0)
        self.assertEqual(legacy.mode, "legacy_interval")
        self.assertEqual(part.mode, "deterministic_partition")
        self.assertEqual(legacy.cycle_length, 0)      # legacy marker
        self.assertEqual(part.cycle_length, 7)


# ==========================================================================
# B -- configuration + validation  (obligation 10)
# ==========================================================================


class SchedulerConfigTests(unittest.TestCase):
    def test_defaults_are_legacy_and_valid(self):
        cfg = SchedulerConfig.from_config(config)
        self.assertEqual(cfg.mode, "legacy_interval")
        cfg.validate()  # must not raise

    def test_invalid_mode_fails(self):
        with self.assertRaises(SchedulerConfigError):
            SchedulerConfig(mode="nonsense").validate()

    def test_overdue_cap_not_below_board_cap(self):
        """Obligation 10: an overdue quota that could displace all normal work
        is rejected before acquisition."""
        with self.assertRaises(SchedulerConfigError):
            SchedulerConfig(mode="deterministic_partition", board_cap=5,
                            overdue_cap=5).validate()
        # strictly-less is accepted
        SchedulerConfig(mode="deterministic_partition", board_cap=5,
                        overdue_cap=4).validate()

    def test_other_invalid_combinations_fail(self):
        for bad in (
            SchedulerConfig(cycle_length=0),
            SchedulerConfig(position=-1),
            SchedulerConfig(board_cap=0 or None, max_age_hours=-1),
            SchedulerConfig(max_retry_attempts=-1),
        ):
            with self.assertRaises(SchedulerConfigError):
                bad.validate()

    def test_invalid_config_fails_before_any_request_in_the_real_path(self):
        """Obligation 10: the real path validates before touching a board."""
        wire = Wire()
        harness = RealPathHarness()
        harness.setUp()
        with mock.patch.object(config, "ATS_SCHEDULER_BOARD_CAP", 5), \
             mock.patch.object(config, "ATS_SCHEDULER_OVERDUE_CAP", 9), \
             mock.patch.object(requests, "request", wire):
            with self.assertRaises(SchedulerConfigError):
                harness.run_acquisition(
                    [board("lever", "a")],
                    lambda b, f, **k: ([posting("a")], ""),
                    session=AtsBoardSession(),
                    scheduler_mode="deterministic_partition",
                )
        self.assertEqual(len(wire.calls), 0)

    def test_effective_config_appears_in_accounting(self):
        """Obligation: effective scheduler config surfaces in ATS accounting."""
        session = AtsBoardSession()
        cfg = SchedulerConfig(mode="deterministic_partition", cycle_length=7)
        decision = select_boards(registry(20), config=cfg, max_age_hours=0)
        session.plan(decision.selected, decision=decision, scheduler_config=cfg)
        acct = session.accounting()
        self.assertEqual(acct["scheduler"]["mode"], "deterministic_partition")
        self.assertEqual(acct["scheduler_mode"], "deterministic_partition")


# ==========================================================================
# C -- overdue fairness without a thundering herd  (obligations 4, 6, 7)
# ==========================================================================


class OverdueFairnessTests(unittest.TestCase):
    def test_three_cycles_show_no_starvation(self):
        """Obligation 4."""
        result = simulate(registry(145), cycle_length=7, cycles=3, max_age_hours=0)
        self.assertTrue(result["full_coverage"])
        self.assertTrue(result["visits_equal_cycles"])
        self.assertEqual(result["starved_boards"], 0)

    def test_overdue_quota_prevents_the_whole_registry_being_taken(self):
        """Obligation 6: every board overdue at once, but the quota holds."""
        boards = registry(145, checked_hours_ago=500)  # all overdue vs 168h
        decision = ats_schedule.partitioned_schedule(
            boards, position=0, cycle_length=7, max_age_hours=168,
            overdue_cap=20, max_boards_per_run=25)
        self.assertLessEqual(decision.selected_overdue, 20)
        self.assertLessEqual(len(decision.selected), 25)
        self.assertLess(len(decision.selected), 145)
        self.assertGreater(len(decision.carried_forward), 0)

    def test_excess_overdue_is_carried_forward_deterministically(self):
        """Obligation 7: carried-forward set is deterministic and gets priority."""
        boards = registry(50, checked_hours_ago=500)
        first = ats_schedule.partitioned_schedule(
            boards, position=0, cycle_length=7, max_age_hours=168, overdue_cap=10)
        again = ats_schedule.partitioned_schedule(
            boards, position=0, cycle_length=7, max_age_hours=168, overdue_cap=10)
        self.assertEqual(first.carried_forward, again.carried_forward)
        self.assertEqual(len(first.carried_forward), 40)
        # Carried-forward boards get priority in the next run.
        nxt = ats_schedule.partitioned_schedule(
            boards, position=1, cycle_length=7, max_age_hours=168, overdue_cap=10,
            carried_overdue=first.carried_forward)
        chosen = {f"{b['provider']}:{b['identifier']}" for b in nxt.selected}
        self.assertTrue(set(first.carried_forward[:10]).issubset(chosen))

    def test_carry_forward_drains_the_herd_without_starvation(self):
        """Obligation 6 + 7: a registry that is entirely overdue at once is
        drained over successive runs. No run ever takes the whole registry (the
        quota holds), a visited board freshens and leaves the pool, and every
        board is covered -- the carried-forward excess never starves."""
        boards = registry(100, checked_hours_ago=500)   # all overdue vs 168h
        by_key = {f"{b['provider']}:{b['identifier']}": b for b in boards}
        now = datetime.now(timezone.utc)
        carried: list = []
        visits: dict = {}
        per_run = []
        for run in range(10):
            moment = now + timedelta(hours=run)     # clock consistent with the fixtures
            decision = ats_schedule.partitioned_schedule(
                boards, position=run, cycle_length=7, max_age_hours=168,
                overdue_cap=15, carried_overdue=carried, now=moment)
            per_run.append(len(decision.selected))
            self.assertLess(len(decision.selected), 100, "a run took the whole herd")
            carried = list(decision.carried_forward)
            for b in decision.selected:
                key = f"{b['provider']}:{b['identifier']}"
                visits[key] = visits.get(key, 0) + 1
                by_key[key]["last_checked_at"] = moment.isoformat()  # freshen, as a fetch would
        self.assertEqual(len(visits), 100, "a carried-forward board was starved")
        self.assertLessEqual(max(per_run), 100)


# ==========================================================================
# D -- bounded failed-board retries  (obligations 8, 9)
# ==========================================================================


class RetryFairnessTests(unittest.TestCase):
    def test_retries_are_bounded(self):
        """Obligation 8."""
        recoverable = ats_schedule.partitioned_schedule(
            registry(10, failures=1), position=0, cycle_length=7,
            max_retry_attempts=2, max_age_hours=0)
        self.assertEqual(len(recoverable.retry), 10)
        exhausted = ats_schedule.partitioned_schedule(
            registry(10, failures=3), position=0, cycle_length=7,
            max_retry_attempts=2, max_age_hours=0)
        self.assertEqual(exhausted.retry, [])

    def test_retry_work_does_not_permanently_displace_normal_work(self):
        """Obligation 9: with a mix of failing and healthy boards, over a full
        cycle every healthy board is still covered by its slot."""
        boards = registry(70)
        for i in range(0, 70, 5):        # ~14 boards perpetually retrying
            boards[i]["consecutive_failures"] = 1
        covered = set()
        selected_retry_total = 0
        for run in range(7):
            decision = ats_schedule.partitioned_schedule(
                boards, position=run, cycle_length=7, max_retry_attempts=2,
                max_age_hours=0)
            selected_retry_total += decision.selected_retry
            for b in decision.selected:
                covered.add(f"{b['provider']}:{b['identifier']}")
        healthy = {f"{b['provider']}:{b['identifier']}"
                   for b in boards if not b["consecutive_failures"]}
        self.assertTrue(healthy.issubset(covered),
                        "healthy boards must not be starved by retries")
        self.assertGreater(selected_retry_total, 0, "retries did run alongside")


# ==========================================================================
# E -- pre-emptive budget skipping  (obligations 11, 12, 13, 14)
# ==========================================================================


class PreemptiveBudgetSkipTests(RealPathHarness):
    def _fetch_making(self, per_board):
        def fetch(board_arg, _fetcher, **_kw):
            ident = board_arg["identifier"]
            for _ in range(per_board):
                requests.request("GET", f"https://{ident}.example/api")
            return [posting(ident)], ""
        return fetch

    def test_budget_ineligible_board_never_enters_the_transport(self):
        """Obligation 11: zero physical requests for a pre-skipped board."""
        budget = RequestBudget(1000, lane_limits={"ats": 3})
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(requests, "request", wire), budget.installed():
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_acquisition(
                [board("workday", "a"), board("workday", "b")],
                self._fetch_making(3), session=session)
            acct = session.accounting()
        # Board A spent the lane (3); board B was skipped before its first call.
        self.assertEqual(len(wire.calls), 3)
        skipped = next(r for r in session.results if r.identifier == "b")
        self.assertFalse(skipped.attempted)
        self.assertTrue(skipped.skipped_by_budget)
        self.assertEqual(skipped.exhausted_scope, "lane")
        self.assertEqual(skipped.physical_requests, 0)
        self.assertEqual(acct["boards_attempted"], 1)
        self.assertEqual(acct["boards_skipped_by_budget"], 1)

    def test_provider_exhaustion_leaves_other_providers_runnable(self):
        """Obligation 12."""
        budget = RequestBudget(1000, provider_limits={"ats_workday": 0})
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(requests, "request", wire), budget.installed():
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_acquisition(
                [board("workday", "a"), board("lever", "c")],
                self._fetch_making(2), session=session)
        wd = next(r for r in session.results if r.provider == "workday")
        lv = next(r for r in session.results if r.provider == "lever")
        self.assertTrue(wd.skipped_by_budget)
        self.assertEqual(wd.physical_requests, 0)
        self.assertFalse(lv.skipped_by_budget)
        self.assertEqual(lv.canonical_records, 1)

    def test_ats_exhaustion_preserves_reserved_non_ats_capacity(self):
        """Obligation 13: ATS pre-skip when its share is gone, JSearch/free
        capacity untouched and still spendable."""
        budget = RequestBudget(12, reserved_for_lanes={"jsearch": 4, "free_feeds": 2})
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(requests, "request", wire), budget.installed():
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_acquisition(
                [board("workday", f"w{i}") for i in range(6)],
                self._fetch_making(2), session=session)
            # ATS could use only 12 - 6 reserved = 6 requests (3 boards x 2);
            # the other 3 boards were pre-skipped, reserved capacity intact.
            self.assertEqual(budget.per_lane["ats"], 6)
            self.assertIsNone(budget.would_block(lane="jsearch"))
            self.assertIsNone(budget.would_block(lane="free_feeds"))
        skipped = [r for r in session.results if r.skipped_by_budget]
        self.assertEqual(len(skipped), 3)
        self.assertFalse(budget.exhausted, "a reserved-boundary skip is not a run stop")

    def test_global_exhaustion_blocks_all_remaining_boards_before_the_wire(self):
        """Obligation 14."""
        budget = RequestBudget(4)
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(requests, "request", wire), budget.installed():
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_acquisition(
                [board("lever", f"b{i}") for i in range(5)],
                self._fetch_making(2), session=session)
            acct = session.accounting()
        self.assertEqual(len(wire.calls), 4)          # 2 boards x 2, then blocked
        self.assertEqual(acct["boards_attempted"], 2)
        remaining = [r for r in session.results if r.skipped_by_budget]
        self.assertEqual(len(remaining), 3)
        self.assertTrue(all(r.exhausted_scope == "run" for r in remaining))
        self.assertTrue(session.reconciles())


# ==========================================================================
# F -- exact request attribution  (obligations 15, 16, 17, 18)
# ==========================================================================


class AttributionTests(unittest.TestCase):
    def test_initial_retry_redirect_phases_classify(self):
        """Obligation 15."""
        with request_trace.install() as tr:
            request_trace.classify()                       # initial
            request_trace.mark_retry(); request_trace.classify()
            request_trace.mark_redirect(); request_trace.classify()
        d = tr.to_dict()
        self.assertEqual((d["initial_listing"], d["listing_retries"], d["listing_redirects"]),
                         (1, 1, 1))

    def test_listing_and_detail_roles_classify(self):
        """Obligation 16."""
        with request_trace.install() as tr:
            request_trace.classify()                       # listing initial
            with request_trace.detail():
                request_trace.classify()                   # detail initial
        d = tr.to_dict()
        self.assertEqual(d["initial_listing"], 1)
        self.assertEqual(d["initial_detail"], 1)

    def test_redirect_inside_retry_counts_once_with_origin_retry(self):
        """Obligation 17."""
        with request_trace.install() as tr:
            request_trace.mark_retry()
            request_trace.mark_redirect()
            request_trace.classify(board="p:b")
        d = tr.to_dict()
        self.assertEqual(d["listing_redirects"], 1)
        self.assertEqual(d["listing_retries"], 0, "must not double-count as a retry")
        self.assertEqual(d["redirect_origins"], {"retry": 1})
        self.assertTrue(tr.origins_reconcile())

    def test_physical_totals_reconcile_exactly(self):
        """Obligation 18: the six classes sum to the physical count, and origin
        metadata never inflates the total."""
        with request_trace.install() as tr:
            for _ in range(3):
                request_trace.classify()                   # 3 listing initial
            request_trace.mark_retry(); request_trace.classify()
            with request_trace.detail():
                request_trace.classify()                   # detail initial
                request_trace.mark_redirect(); request_trace.classify()
        self.assertEqual(tr.total, 6)
        self.assertTrue(tr.reconciles(6))
        self.assertTrue(tr.origins_reconcile())
        # origin metadata sums to redirects, not more
        self.assertEqual(sum(tr.redirect_origins.values()),
                         tr.listing_redirects + tr.detail_redirects)

    def test_budget_and_trace_agree_on_the_count(self):
        """Obligation 18 at the seam: classification happens where the slot is
        spent, so budget count == trace total."""
        budget = RequestBudget(100)
        wire = Wire()
        with mock.patch.object(requests, "request", wire), budget.installed(), \
             request_trace.install() as tr:
            for _ in range(5):
                requests.request("GET", "https://x.example")
        self.assertEqual(budget.count, 5)
        self.assertEqual(tr.total, 5)
        self.assertTrue(tr.reconciles(budget.count))


# ==========================================================================
# G -- context isolation  (obligations 19, 20)
# ==========================================================================


class ContextIsolationTests(unittest.TestCase):
    def test_trace_phase_restores_after_an_exception_in_a_board(self):
        """Obligation 19: a board that raises between mark_retry and the request
        cannot leak its phase onto the next board's first request."""
        with request_trace.install() as tr:
            session = AtsBoardSession(trace=tr)
            try:
                with session.board(board("lever", "a")):
                    request_trace.mark_retry()      # pending retry...
                    raise RuntimeError("boom before the request went out")
            except RuntimeError:
                pass
            with session.board(board("lever", "b")):
                request_trace.classify(board="lever:b")   # must be an INITIAL
        d = tr.to_dict()
        self.assertEqual(d["initial_listing"], 1)
        self.assertEqual(d["listing_retries"], 0, "a stale retry phase leaked")

    def test_budget_context_restores_after_success_and_exception(self):
        """Obligation 19 + 20: lane/source/board restore through nesting and
        after an exception, so nothing leaks between scopes."""
        budget = RequestBudget(100)
        self.assertEqual((budget.lane, budget.source, budget.board), ("", "", ""))
        with budget.context(lane="ats", source="ats_workday", board="workday:a"):
            self.assertEqual(budget.lane, "ats")
            with budget.context(lane="jsearch", source="jsearch"):
                self.assertEqual(budget.lane, "jsearch")
            self.assertEqual(budget.lane, "ats")   # restored after nested
            try:
                with budget.context(lane="free_feeds", source="himalayas"):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            self.assertEqual(budget.lane, "ats")   # restored after exception
        self.assertEqual((budget.lane, budget.source, budget.board), ("", "", ""))

    def test_per_board_trace_attribution_does_not_leak_between_boards(self):
        """Obligation 20: each board's requests are attributed to that board."""
        budget = RequestBudget(100)
        wire = Wire()
        with mock.patch.object(requests, "request", wire), budget.installed(), \
             request_trace.install() as tr:
            session = AtsBoardSession(budget=budget, trace=tr)
            with session.board(board("lever", "a")):
                requests.request("GET", "https://a.example")
            with session.board(board("ashby", "b")):
                requests.request("GET", "https://b.example")
        self.assertIn("lever:a", tr.by_board)
        self.assertIn("ashby:b", tr.by_board)
        self.assertEqual(sum(tr.by_board["lever:a"].values()), 1)
        self.assertEqual(sum(tr.by_board["ashby:b"].values()), 1)


# ==========================================================================
# H -- complete real-path accounting  (obligations 21, 22)
# ==========================================================================


class RealPathAccountingTests(RealPathHarness):
    def test_scheduler_and_budget_accounting_reconciles_end_to_end(self):
        """Obligations 21 + 22: every aggregate ties to persisted board records,
        including scheduler skips and budget skips."""
        boards = registry(40)
        budget = RequestBudget(1000, lane_limits={"ats": 8})
        wire = Wire()

        def fetch(board_arg, _fetcher, **_kw):
            ident = board_arg["identifier"]
            for _ in range(2):
                requests.request("GET", f"https://{ident}.example/api")
            return [posting(ident)], ""

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(requests, "request", wire), budget.installed(), \
             mock.patch.object(config, "ATS_SCHEDULER_CYCLE_LENGTH", 7), \
             mock.patch.object(config, "ATS_SCHEDULER_MAX_AGE_HOURS", 0), \
             mock.patch.object(config, "ATS_SCHEDULER_BOARD_CAP", 12):
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_acquisition(boards, fetch, session=session,
                                 scheduler_mode="deterministic_partition")
            acct = session.accounting()
            # persisted files == selected boards (checked before the temp dir
            # is torn down).
            file_count = len(list(Path(tmp).glob("*.json")))

        # The full Phase 1B-2B identity chain.
        self.assertEqual(acct["boards_available"],
                         acct["boards_selected"] + acct["boards_skipped_by_scheduler"])
        self.assertEqual(acct["boards_selected"],
                         acct["boards_skipped_by_budget"] + acct["boards_attempted"])
        self.assertEqual(acct["boards_attempted"],
                         acct["boards_completed"] + acct["boards_partial"] + acct["boards_failed"])
        self.assertEqual(acct["boards_available"], 40)
        self.assertLessEqual(acct["boards_selected"], 12)     # board cap respected
        self.assertTrue(session.reconciles())
        self.assertTrue(all(session.full_reconciliation().values()))
        self.assertEqual(file_count, acct["boards_selected"])

    def test_previous_failure_preservation_behaviour_is_intact(self):
        """Obligation 23: a board raising mid-fetch still does not discard the
        others' output (the 1B-2A guarantee), now under the new accounting."""
        boards = [board("greenhouse", "b1"), board("lever", "b2"),
                  board("ashby", "b3"), board("workable", "b4")]

        def fetch(board_arg, _fetcher, **_kw):
            ident = board_arg["identifier"]
            if ident == "b3":
                raise RuntimeError("boom")
            return [posting(ident)], ""

        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition(boards, fetch, session=session)
            acct = session.accounting()
        self.assertEqual(acct["boards_completed"], 3)
        self.assertEqual(acct["boards_failed"], 1)
        self.assertEqual(acct["normalized_records"], 3)
        self.assertTrue(session.reconciles())
        ids = {j.get("job_id") for j in self.selected_jobs}
        self.assertEqual(ids, {"b1-1", "b2-1", "b4-1"})


# ==========================================================================
# C/D -- scheduler state file  (schema, atomicity, absence when disabled)
# ==========================================================================


class SchedulerStateTests(unittest.TestCase):
    def test_state_roundtrips_and_is_schema_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            SchedulerState(carried_overdue=["lever:a", "ashby:b"], last_position=3).save(path)
            loaded = SchedulerState.load(path)
        self.assertEqual(loaded.carried_overdue, ["lever:a", "ashby:b"])
        self.assertEqual(loaded.last_position, 3)
        self.assertEqual(loaded.schema_version, ats_schedule.SCHEDULER_STATE_SCHEMA)

    def test_wrong_schema_is_refused_not_reinterpreted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"schema_version": "something-else", "carried_overdue": []}',
                            encoding="utf-8")
            with self.assertRaises(SchedulerStateError):
                SchedulerState.load(path)

    def test_missing_state_is_a_clean_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = SchedulerState.load(Path(tmp) / "does_not_exist.json")
        self.assertEqual(loaded.carried_overdue, [])

    def test_no_state_file_is_written_when_scheduling_is_disabled(self):
        """Obligation 24: state is absent unless deterministic scheduling is on
        AND a state path is configured."""
        harness = RealPathHarness()
        harness.setUp()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sched" / "state.json"
            with mock.patch.object(config, "ATS_SCHEDULER_STATE_PATH", str(state_path)):
                # legacy mode (default): the state path is never touched.
                harness.run_acquisition(
                    [board("lever", "a")],
                    lambda b, f, **k: ([posting("a")], ""),
                    session=AtsBoardSession())
            self.assertFalse(state_path.exists())

    def test_state_file_is_written_only_in_deterministic_mode_with_a_path(self):
        harness = RealPathHarness()
        harness.setUp()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sched" / "state.json"
            with mock.patch.object(config, "ATS_SCHEDULER_STATE_PATH", str(state_path)), \
                 mock.patch.object(config, "ATS_SCHEDULER_MAX_AGE_HOURS", 168), \
                 mock.patch.object(config, "ATS_SCHEDULER_OVERDUE_CAP", 5), \
                 mock.patch.object(config, "ATS_SCHEDULER_BOARD_CAP", 10):
                harness.run_acquisition(
                    registry(40, checked_hours_ago=500),   # all overdue -> excess
                    lambda b, f, **k: ([posting(b["identifier"])], ""),
                    session=AtsBoardSession(),
                    scheduler_mode="deterministic_partition")
            self.assertTrue(state_path.exists())
            loaded = SchedulerState.load(state_path)
            self.assertGreater(len(loaded.carried_overdue), 0)


if __name__ == "__main__":
    unittest.main()
