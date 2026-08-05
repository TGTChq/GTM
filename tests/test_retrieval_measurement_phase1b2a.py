"""Phase 1B-2A: per-board checkpointing and scoped budgets in the REAL path.

Every test here drives ``multi_source_acquisition.run_multi_source_acquisition``
-- the function production calls -- with injected transports. No parallel ATS
implementation is exercised and no network call is made.

The scenario being pinned shut is run 20260805T015708Z-1ad3ef58: 968 requests
across 30 boards, zero records retained, because the lane raised and the
accumulated ``ats_jobs`` list went with it.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import config
import multi_source_acquisition
from retrieval_measurement import request_trace
from retrieval_measurement.ats_checkpoint import UNRESOLVED, AtsBoardSession
from retrieval_measurement.instrument import RequestBudget, RequestCeilingReached
from retrieval_measurement.schema import TRUNCATION_KINDS


def board(provider, identifier, company=None):
    return {"provider": provider, "identifier": identifier,
            "company_name": company or f"Co-{identifier}",
            "key": f"{provider}:{identifier}:api"}


def posting(board_id, n=1):
    return {"job_id": f"{board_id}-{n}", "employer_name": f"Co-{board_id}",
            "job_title": "Support Engineer",
            "job_apply_link": f"https://{board_id}.example/jobs/{n}"}


class RealPathHarness(unittest.TestCase):
    """Drives the production acquisition function with everything else stubbed."""

    def setUp(self):
        # Postings the real path selected and handed to persistence. This is
        # the acquisition output downstream stages consume.
        self.selected_jobs = []

    def run_acquisition(self, boards, fetch_impl, *, session=None, budget=None):
        def capture_save(selected, _stats):
            self.selected_jobs = [dict(job) for job in selected]
            return "<test: not written>"

        registry = mock.MagicMock()
        registry.due_entries.return_value = list(boards)
        registry.seed_from_history.return_value = {
            "files_scanned": 0, "jobs_scanned": 0, "boards_added_or_updated": 0}
        registry.upsert_from_jobs.return_value = 0
        registry.record_result.return_value = 0
        registry.entries = {}
        registry.invalid_entries_pruned = 0

        with mock.patch.object(multi_source_acquisition, "AtsBoardRegistry",
                               return_value=registry), \
             mock.patch.object(multi_source_acquisition, "fetch_board_jobs", fetch_impl), \
             mock.patch.object(multi_source_acquisition, "build_adapters", return_value=[]), \
             mock.patch.object(multi_source_acquisition, "_enrich_himalayas_company_profiles",
                               return_value={}), \
             mock.patch.object(multi_source_acquisition, "_discover_landing_links",
                               return_value={}), \
             mock.patch.object(multi_source_acquisition, "_save_raw", capture_save), \
             mock.patch.object(config, "MULTI_SOURCE_JSEARCH_ENABLED", False), \
             mock.patch.object(config, "ADZUNA_ENABLED", False), \
             mock.patch.object(config, "FANTASTIC_JOBS_ENABLED", False), \
             mock.patch.object(config, "ATS_DIRECT_ACQUISITION_ENABLED", True), \
             mock.patch.object(config, "ATS_REGISTRY_AUTO_SEED_HISTORY", False), \
             mock.patch.object(config, "FREE_SOURCE_MIN_SUCCESSFUL_SOURCES", 0):
            seen = mock.MagicMock()
            seen.has_job_id.return_value = False
            seen.has_dedup_key.return_value = False
            return multi_source_acquisition.run_multi_source_acquisition(
                seen, ats_session=session,
            )


# --------------------------------------------------------------------------
# A / D -- per-board checkpointing and partial-output preservation
# --------------------------------------------------------------------------


class RealPathFailureInjectionTests(RealPathHarness):
    BOARDS = [board("greenhouse", "b1"), board("lever", "b2"),
              board("ashby", "b3"), board("workable", "b4")]

    @staticmethod
    def _fetch(board_arg, _fetcher, **_kw):
        ident = board_arg["identifier"]
        if ident == "b3":
            raise RuntimeError("board 3 exploded mid-fetch")
        return [posting(ident), posting(ident, 2)], ""

    def test_boards_one_two_and_four_survive_board_three_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            result = self.run_acquisition(self.BOARDS, self._fetch, session=session)

            files = sorted(p.stem for p in Path(tmp).glob("*.json"))
            self.assertEqual(len(files), 4, "every attempted board must be persisted")

            acct = session.accounting()
            self.assertEqual(acct["boards_planned"], 4)
            self.assertEqual(acct["boards_attempted"], 4)
            self.assertEqual(acct["boards_completed"], 3)
            self.assertEqual(acct["boards_failed"], 1)
            self.assertEqual(acct["boards_partial"], 0)
            self.assertEqual(acct["normalized_records"], 6)   # 3 boards x 2
            self.assertEqual(acct["lane_status"], "partial")
            self.assertTrue(session.reconciles())
            self.assertGreater(len(self.selected_jobs), 0)

    def test_the_surviving_postings_remain_in_the_returned_acquisition_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition(self.BOARDS, self._fetch, session=session)
            ids = {job.get("job_id") for job in self.selected_jobs}
            for ident in ("b1", "b2", "b4"):
                self.assertIn(f"{ident}-1", ids, f"{ident} postings were discarded")
            self.assertNotIn("b3-1", ids)

    def test_board_three_gets_its_own_failure_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition(self.BOARDS, self._fetch, session=session)
            failed = next(r for r in session.results if r.identifier == "b3")
            self.assertIn("RuntimeError", failed.error)
            self.assertEqual(failed.canonical_records, 0)
            self.assertTrue(Path(failed.checkpoint_path).is_file())
            payload = json.loads(Path(failed.checkpoint_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["jobs"], [])

    def test_board_four_continues_after_board_three_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition(self.BOARDS, self._fetch, session=session)
            fourth = next(r for r in session.results if r.identifier == "b4")
            self.assertEqual(fourth.error, "")
            self.assertEqual(fourth.canonical_records, 2)

    def test_a_checkpoint_is_written_before_the_next_board_is_attempted(self):
        order = []

        def fetch(board_arg, _fetcher, **_kw):
            ident = board_arg["identifier"]
            order.append(("fetch", ident))
            return [posting(ident)], ""

        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            original = session.record

            def watching(state, **kwargs):
                order.append(("persist", state["identifier"]))
                return original(state, **kwargs)

            session.record = watching
            self.run_acquisition(self.BOARDS[:3], fetch, session=session)
        self.assertEqual(
            order,
            [("fetch", "b1"), ("persist", "b1"),
             ("fetch", "b2"), ("persist", "b2"),
             ("fetch", "b3"), ("persist", "b3")],
            "persistence must follow each board, not the whole lane",
        )

    def test_no_real_network_call_occurs(self):
        wire = mock.MagicMock(side_effect=AssertionError("a real request escaped"))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(requests, "request", wire):
            self.run_acquisition(self.BOARDS, self._fetch,
                                 session=AtsBoardSession(checkpoint_dir=tmp))
        wire.assert_not_called()

    def test_without_a_session_an_exception_still_propagates_unchanged(self):
        # Compatibility: the legacy path is not silently altered.
        with self.assertRaises(RuntimeError):
            self.run_acquisition(self.BOARDS, self._fetch, session=None)


# --------------------------------------------------------------------------
# A -- checkpoint content
# --------------------------------------------------------------------------


class CheckpointContentTests(RealPathHarness):
    def test_the_persisted_record_carries_every_required_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition(
                [board("greenhouse", "acme")],
                lambda b, f, **k: ([posting("acme"), posting("acme", 2)], ""),
                session=session,
            )
            record = session.results[0].to_dict()
            for field in ("provider", "identifier", "company_name", "started_at",
                          "completed_at", "physical_requests", "pages",
                          "listing_records", "detail_records", "canonical_records",
                          "unique_posting_identity", "unique_production_equivalent",
                          "truncation", "stop_reason", "error", "checkpoint_path",
                          "retries", "redirects"):
                self.assertIn(field, record, field)
            self.assertEqual(record["canonical_records"], 2)
            self.assertEqual(record["unique_posting_identity"], 2)

    def test_ambiguous_attribution_is_marked_unresolved_not_estimated(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)   # no trace supplied
            self.run_acquisition([board("lever", "x")],
                                 lambda b, f, **k: ([posting("x")], ""), session=session)
            record = session.results[0]
            self.assertEqual(record.retries, UNRESOLVED)
            self.assertEqual(record.redirects, UNRESOLVED)
            payload = json.loads(Path(record.checkpoint_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["attribution_status"], UNRESOLVED)

    def test_with_a_trace_the_attribution_is_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            with request_trace.install() as trace:
                session = AtsBoardSession(checkpoint_dir=tmp, trace=trace)
                self.run_acquisition([board("lever", "x")],
                                     lambda b, f, **k: ([posting("x")], ""), session=session)
            record = session.results[0]
            self.assertIsInstance(record.retries, int)
            self.assertIsInstance(record.redirects, int)
            payload = json.loads(Path(record.checkpoint_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["attribution_status"], "exact")
            self.assertIn("request_trace_delta", payload)

    def test_a_board_returning_records_and_an_error_is_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition([board("workday", "w")],
                                 lambda b, f, **k: ([posting("w")], "page_2_timeout"),
                                 session=session)
            acct = session.accounting()
            self.assertEqual(acct["boards_partial"], 1)
            self.assertEqual(acct["boards_failed"], 0)
            self.assertEqual(acct["normalized_records"], 1)


# --------------------------------------------------------------------------
# B -- scoped budgets and reserved capacity
# --------------------------------------------------------------------------


class Wire:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs.get("url") or (args[1] if len(args) > 1 else ""))
        return mock.Mock(status_code=200, text="{}", url="https://x.test")


class ScopedBudgetIntegrationTests(RealPathHarness):
    def _budgeted_fetch(self, per_board):
        def fetch(board_arg, _fetcher, **_kw):
            ident = board_arg["identifier"]
            for _ in range(per_board):
                requests.request("GET", f"https://{ident}.example/api")
            return [posting(ident)], ""
        return fetch

    def test_board_budget_stops_one_board_and_spares_its_siblings(self):
        budget = RequestBudget(1000, board_limit=2)
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(requests, "request", wire), budget.installed():
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_acquisition(
                [board("workday", "a"), board("workday", "b")],
                self._budgeted_fetch(5), session=session, budget=budget,
            )
        self.assertEqual(budget.per_board["workday:a"], 2)
        self.assertEqual(budget.per_board["workday:b"], 2)
        self.assertEqual(len(wire.calls), 4)
        self.assertFalse(budget.exhausted, "a board budget is not a run stop")
        self.assertEqual(len(session.results), 2, "both boards were still attempted")

    def test_provider_budget_stops_one_provider_and_spares_another(self):
        budget = RequestBudget(1000, provider_limits={"ats_workday": 2})
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(requests, "request", wire), budget.installed():
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_acquisition(
                [board("workday", "a"), board("lever", "c")],
                self._budgeted_fetch(3), session=session, budget=budget,
            )
        self.assertEqual(budget.per_provider["ats_workday"], 2)
        self.assertEqual(budget.per_provider["ats_lever"], 3)
        self.assertFalse(budget.exhausted)

    def test_ats_cannot_consume_capacity_reserved_for_jsearch_and_free_feeds(self):
        # The exact 20260805T015708Z-1ad3ef58 failure: ATS took 971 of 1000
        # and JSearch never ran.
        budget = RequestBudget(12, reserved_for_lanes={"jsearch": 4, "free_feeds": 2})
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(requests, "request", wire), budget.installed():
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_acquisition([board("workday", "a")], self._budgeted_fetch(20),
                                 session=session, budget=budget)
            # ATS is capped at 12 - 6 reserved = 6.
            self.assertEqual(budget.per_lane["ats"], 6)
            with budget.context(lane="jsearch"):
                for _ in range(4):
                    requests.request("GET", "https://jsearch.p.rapidapi.com/search-v2")
            with budget.context(lane="free_feeds"):
                for _ in range(2):
                    requests.request("GET", "https://himalayas.app/jobs/api")
        self.assertEqual(budget.per_lane["jsearch"], 4)
        self.assertEqual(budget.per_lane["free_feeds"], 2)
        self.assertEqual(len(wire.calls), 12)

    def test_completed_records_survive_ats_lane_exhaustion(self):
        budget = RequestBudget(1000, lane_limits={"ats": 3})
        wire = Wire()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(requests, "request", wire), budget.installed():
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_acquisition(
                [board("greenhouse", "a"), board("greenhouse", "b")],
                self._budgeted_fetch(2), session=session, budget=budget,
            )
            acct = session.accounting()
        self.assertGreaterEqual(acct["normalized_records"], 1,
                                "the first board's records must survive")
        self.assertTrue(session.reconciles())

    def test_global_exhaustion_blocks_before_the_wire(self):
        budget = RequestBudget(2)
        wire = Wire()
        with mock.patch.object(requests, "request", wire), budget.installed():
            requests.request("GET", "https://a.example")
            requests.request("GET", "https://a.example")
            with self.assertRaises(RequestCeilingReached):
                requests.request("GET", "https://a.example")
        self.assertEqual(len(wire.calls), 2)
        self.assertTrue(budget.exhausted)

    def test_a_budget_stop_is_never_provider_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            record = session.skip_for_budget(board("workday", "z"), "provider")
        self.assertIn("budget_exhausted", record.stop_reason)
        for forbidden in ("provider_exhaustion", "empty_page", "error_stop"):
            self.assertNotIn(forbidden, record.stop_reason)
            self.assertIn(forbidden, TRUNCATION_KINDS)
        self.assertEqual(session.accounting()["boards_skipped_by_budget"], 1)
        self.assertEqual(session.accounting()["boards_attempted"], 0)


# --------------------------------------------------------------------------
# C -- accounting reconciliation
# --------------------------------------------------------------------------


class AccountingReconciliationTests(RealPathHarness):
    def test_every_aggregate_reconciles_to_the_board_records(self):
        boards = [board("greenhouse", "b1"), board("lever", "b2"),
                  board("ashby", "b3"), board("workable", "b4")]

        def fetch(board_arg, _fetcher, **_kw):
            ident = board_arg["identifier"]
            if ident == "b3":
                raise RuntimeError("boom")
            if ident == "b4":
                return [posting(ident)], "partial_page_error"
            return [posting(ident), posting(ident, 2)], ""

        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition(boards, fetch, session=session)
            acct = session.accounting()

        self.assertEqual(
            acct["boards_attempted"],
            acct["boards_completed"] + acct["boards_partial"] + acct["boards_failed"],
        )
        self.assertEqual(acct["boards_completed"], 2)
        self.assertEqual(acct["boards_partial"], 1)
        self.assertEqual(acct["boards_failed"], 1)
        self.assertEqual(acct["normalized_records"], 5)     # 2 + 2 + 0 + 1
        self.assertEqual(acct["unique_posting_identity"], 5)
        self.assertTrue(session.reconciles())

    def test_lane_totals_equal_the_union_of_persisted_board_results(self):
        boards = [board("lever", f"b{i}") for i in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition(
                boards, lambda b, f, **k: ([posting(b["identifier"])], ""), session=session)
            persisted = 0
            for path in Path(tmp).glob("*.json"):
                persisted += len(json.loads(path.read_text(encoding="utf-8"))["jobs"])
        self.assertEqual(session.accounting()["normalized_records"], persisted)
        self.assertEqual(len(session.jobs()), persisted)

    def test_skipped_boards_sit_outside_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            self.run_acquisition([board("lever", "a")],
                                 lambda b, f, **k: ([posting("a")], ""), session=session)
            session.skip_for_budget(board("workday", "z"), "lane")
            acct = session.accounting()
        self.assertEqual(acct["boards_attempted"], 1)
        self.assertEqual(acct["boards_skipped_by_budget"], 1)


# --------------------------------------------------------------------------
# E -- default-off compatibility
# --------------------------------------------------------------------------


class DisabledCompatibilityTests(RealPathHarness):
    def test_no_session_writes_nothing_and_requires_no_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = set(Path(tmp).rglob("*"))
            self.run_acquisition(
                [board("lever", "a"), board("ashby", "b")],
                lambda b, f, **k: ([posting(b["identifier"])], ""), session=None)
            self.assertEqual(set(Path(tmp).rglob("*")), before, "a file was written")
        ids = {job.get("job_id") for job in self.selected_jobs}
        self.assertEqual(ids, {"a-1", "b-1"})

    def test_the_stats_key_is_absent_when_no_session_is_supplied(self):
        result = self.run_acquisition(
            [board("lever", "a")],
            lambda b, f, **k: ([posting("a")], ""), session=None)
        self.assertNotIn("ats_board_accounting", result.stats or {})

    def test_the_stats_key_is_present_when_a_session_is_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp)
            result = self.run_acquisition(
                [board("lever", "a")],
                lambda b, f, **k: ([posting("a")], ""), session=session)
        accounting = (result.stats or {})["ats_board_accounting"]
        self.assertTrue(accounting["reconciled"])
        self.assertEqual(accounting["accounting"]["boards_completed"], 1)

    def test_a_session_without_a_checkpoint_dir_writes_no_file(self):
        session = AtsBoardSession()
        self.run_acquisition([board("lever", "a")],
                             lambda b, f, **k: ([posting("a")], ""), session=session)
        self.assertEqual(session.results[0].checkpoint_path, "")
        self.assertEqual(session.accounting()["boards_completed"], 1)

    def test_the_signature_addition_is_keyword_only(self):
        import inspect

        sig = inspect.signature(multi_source_acquisition.run_multi_source_acquisition)
        param = sig.parameters["ats_session"]
        self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIsNone(param.default)


if __name__ == "__main__":
    unittest.main()
