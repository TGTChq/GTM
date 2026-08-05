"""Phase 1B-1: JSearch truncation attribution, request attribution, scheduling.

Fixtures use the measured values from run 20260805T021929Z-b25aaad1 (146
queries, 47 of 50 saturated at the 3x10 page ceiling, quota 1,983 of 10,000)
and from the real 145-board registry snapshot.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

import config
from free_job_sources import SourceResult
from retrieval_measurement import request_trace
from retrieval_measurement.ats_schedule import (
    DEFAULT_CYCLE_LENGTH,
    partitioned_schedule,
    select_boards,
    simulate,
    slot_for,
)
from retrieval_measurement.instrument import (
    RequestBudget,
    RequestCeilingReached,
    classify_jsearch_truncation,
    classify_truncation,
)


def jsearch_result(**stats):
    base = {
        "num_pages_per_query": 3,
        "estimated_unit_budget": 450,
        "estimated_request_units": 150,
        "raw_role_counts": {},
        "quota": {},
    }
    base.update(stats)
    return SourceResult(source="jsearch", metadata={"stats": base})


def kinds(records):
    return {(r.kind, r.reason.split(":")[0].split(" reached")[0]) for r in records}


# --------------------------------------------------------------------------
# H -- JSearch truncation attribution
# --------------------------------------------------------------------------


class JSearchTruncationTests(unittest.TestCase):
    def test_no_free_feed_cap_is_ever_attributed_to_jsearch(self):
        # The defect: applied_cap=1000 (FREE_SOURCE_MAX_RECORDS_PER_SOURCE)
        # reported against collected=1340.
        result = jsearch_result(raw_role_counts={f"q{i}": 30 for i in range(50)})
        result.jobs = [{"job_id": str(i)} for i in range(1340)]
        for record in classify_truncation("jsearch", result):
            self.assertNotIn("FREE_SOURCE_MAX_RECORDS_PER_SOURCE", record.reason)
            self.assertNotEqual(record.applied_cap, config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE)

    def test_three_page_saturation_reports_the_configured_page_limit(self):
        result = jsearch_result(raw_role_counts={f"q{i}": 30 for i in range(47)})
        records = classify_jsearch_truncation(result)
        cap = next(r for r in records if "page ceiling" in r.reason)
        self.assertEqual(cap.kind, "configured_cap")
        self.assertEqual(cap.applied_cap, 30)
        self.assertEqual(cap.evidence["num_pages_per_query"], 3)
        self.assertEqual(cap.evidence["queries_at_page_ceiling"], 47)

    def test_a_short_page_reports_query_exhaustion_not_a_cap(self):
        result = jsearch_result(raw_role_counts={"Shopify Developer": 20,
                                                 "Implementation Specialist": 10})
        records = classify_jsearch_truncation(result)
        exhausted = next(r for r in records if r.kind == "provider_exhaustion")
        self.assertEqual(exhausted.evidence["queries_exhausted"], 2)
        self.assertFalse([r for r in records if r.kind == "configured_cap"],
                         "a short page must not be blamed on a configured cap")

    def test_a_zero_result_query_is_an_empty_page(self):
        result = jsearch_result(raw_role_counts={"Digital Marketing Specialist": 0})
        empty = next(r for r in classify_jsearch_truncation(result) if r.kind == "empty_page")
        self.assertEqual(empty.evidence["queries_empty"], 1)

    def test_unit_budget_exhaustion_is_named(self):
        result = jsearch_result(estimated_request_units=450, estimated_unit_budget=450)
        record = next(r for r in classify_jsearch_truncation(result)
                      if "UNITS_PER_RUN" in r.reason)
        self.assertEqual(record.kind, "configured_cap")
        self.assertEqual(record.applied_cap, 450)

    def test_quota_threshold_is_named_and_is_not_a_configured_cap(self):
        result = jsearch_result(quota={"remaining": 400, "limit": 10000})
        with mock.patch.object(config, "JSEARCH_MIN_REMAINING_REQUESTS", 500), \
             mock.patch.object(config, "JSEARCH_STOP_ON_LOW_QUOTA", True):
            record = next(r for r in classify_jsearch_truncation(result)
                          if r.kind == "quota_guard")
        self.assertEqual(record.applied_cap, 500)
        self.assertEqual(record.evidence["remaining"], 400)

    def test_healthy_quota_is_not_reported_as_a_stop(self):
        # The real run: 1,983 remaining against a 500 threshold.
        result = jsearch_result(quota={"remaining": 1983, "limit": 10000})
        self.assertFalse([r for r in classify_jsearch_truncation(result)
                          if r.kind == "quota_guard"])

    def test_adaptive_limits_keep_their_own_reason(self):
        result = jsearch_result(adaptive_stop_reason="adaptive_query_or_unit_budget_exhausted",
                                adaptive_extra_queries=32, adaptive_query_cap=32)
        record = next(r for r in classify_jsearch_truncation(result)
                      if "adaptive deepening" in r.reason)
        self.assertEqual(record.applied_cap, 32)

    def test_adaptive_lookback_limit_keeps_its_own_reason(self):
        result = jsearch_result(adaptive_lookback_queries=16)
        with mock.patch.object(config, "JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES", 16):
            record = next(r for r in classify_jsearch_truncation(result)
                          if "LOOKBACK" in r.reason)
        self.assertEqual(record.applied_cap, 16)

    def test_errors_are_reported_as_error_stop(self):
        result = jsearch_result()
        result.errors = ["HTTP 500 from provider"]
        self.assertTrue([r for r in classify_jsearch_truncation(result)
                         if r.kind == "error_stop"])

    def test_a_clean_run_reports_not_truncated(self):
        record = classify_jsearch_truncation(jsearch_result())[0]
        self.assertEqual(record.kind, "not_truncated")

    def test_the_real_run_signature_is_classified_correctly(self):
        counts = {f"q{i}": 30 for i in range(47)}
        counts.update({"Shopify Developer": 20, "Implementation Specialist": 10,
                       "Digital Marketing Specialist": 0})
        result = jsearch_result(raw_role_counts=counts,
                                quota={"remaining": 1983, "limit": 10000})
        found = {r.kind for r in classify_jsearch_truncation(result)}
        self.assertIn("configured_cap", found)        # 47 saturated queries
        self.assertIn("provider_exhaustion", found)   # 2 genuinely short
        self.assertIn("empty_page", found)            # 1 empty
        self.assertNotIn("quota_guard", found)


# --------------------------------------------------------------------------
# E -- request attribution
# --------------------------------------------------------------------------


class Wire:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs.get("url") or (args[1] if len(args) > 1 else ""))
        return mock.Mock(status_code=200, text="{}", url="https://example.test/x")


class RequestAttributionTests(unittest.TestCase):
    def test_listing_and_detail_are_classified_separately(self):
        budget = RequestBudget(100)
        with mock.patch.object(requests, "request", Wire()), request_trace.install() as trace, \
             budget.installed():
            requests.request("GET", "https://boards-api.greenhouse.io/v1/boards/x/jobs")
            with request_trace.detail():
                requests.request("GET", "https://boards-api.greenhouse.io/v1/boards/x/jobs/1")
        self.assertEqual(trace.initial_listing, 1)
        self.assertEqual(trace.initial_detail, 1)

    def test_retries_and_redirects_are_counted_independently(self):
        budget = RequestBudget(100)
        with mock.patch.object(requests, "request", Wire()), request_trace.install() as trace, \
             budget.installed():
            requests.request("GET", "https://a.example")          # initial listing
            request_trace.mark_retry()
            requests.request("GET", "https://a.example")          # listing retry
            request_trace.mark_redirect()
            requests.request("GET", "https://b.example")          # listing redirect
            with request_trace.detail():
                requests.request("GET", "https://c.example")      # initial detail
                request_trace.mark_retry()
                requests.request("GET", "https://c.example")      # detail retry
                request_trace.mark_redirect()
                requests.request("GET", "https://d.example")      # detail redirect
        self.assertEqual(
            (trace.initial_listing, trace.listing_retries, trace.listing_redirects,
             trace.initial_detail, trace.detail_retries, trace.detail_redirects),
            (1, 1, 1, 1, 1, 1),
        )

    def test_attribution_sums_exactly_to_the_physical_request_count(self):
        budget = RequestBudget(100)
        with mock.patch.object(requests, "request", Wire()), request_trace.install() as trace, \
             budget.installed():
            for i in range(7):
                if i % 3 == 1:
                    request_trace.mark_retry()
                requests.request("GET", "https://a.example")
            with request_trace.detail():
                for _ in range(4):
                    requests.request("GET", "https://b.example")
        self.assertEqual(trace.total, budget.count)
        self.assertTrue(trace.reconciles(budget.count))
        self.assertEqual(trace.total, 11)

    def test_each_physical_attempt_consumes_exactly_one_budget_unit(self):
        budget = RequestBudget(3)
        wire = Wire()
        with mock.patch.object(requests, "request", wire), request_trace.install() as trace, \
             budget.installed():
            requests.request("GET", "https://a.example")
            request_trace.mark_retry()
            requests.request("GET", "https://a.example")
            request_trace.mark_redirect()
            requests.request("GET", "https://a.example")
            with self.assertRaises(RequestCeilingReached):
                requests.request("GET", "https://a.example")
        self.assertEqual(len(wire.calls), 3)
        self.assertEqual(trace.total, 3)
        self.assertTrue(trace.reconciles(budget.count))

    def test_attribution_is_recorded_per_board(self):
        budget = RequestBudget(100)
        with mock.patch.object(requests, "request", Wire()), request_trace.install() as trace, \
             budget.installed():
            with budget.context(lane="ats", source="ats_workday", board="workday:alpha"):
                requests.request("GET", "https://alpha.example")
                with request_trace.detail():
                    requests.request("GET", "https://alpha.example/job/1")
        self.assertEqual(trace.by_board["workday:alpha"],
                         {"initial_listing": 1, "initial_detail": 1})

    def test_disabled_tracing_installs_nothing_and_records_nothing(self):
        self.assertFalse(request_trace.enabled())
        budget = RequestBudget(10)
        wire = Wire()
        with mock.patch.object(requests, "request", wire), budget.installed():
            requests.request("GET", "https://a.example")
            request_trace.mark_retry()      # no-op
            request_trace.mark_redirect()   # no-op
            requests.request("GET", "https://a.example")
        self.assertEqual(budget.count, 2)
        self.assertIsNone(request_trace.classify())

    def test_http_semantics_are_untouched_by_the_annotations(self):
        """request_with_retry must return the same object, with the same retry
        count, timeout and headers, whether or not tracing is installed."""
        import http_utils

        seen = []

        def fake_request(method, url, **kwargs):
            seen.append({"method": method, "url": url, "timeout": kwargs.get("timeout"),
                         "headers": kwargs.get("headers")})
            return mock.Mock(status_code=200, text="ok", url=url,
                             raise_for_status=lambda: None, headers={})

        with mock.patch.object(requests, "request", fake_request):
            plain = http_utils.request_with_retry("GET", "https://a.example",
                                                  headers={"X": "1"}, timeout=9)
            baseline = list(seen)
            seen.clear()
            with request_trace.install():
                traced = http_utils.request_with_retry("GET", "https://a.example",
                                                       headers={"X": "1"}, timeout=9)
        self.assertEqual(seen, baseline, "the call to the transport differed")
        self.assertEqual(plain.status_code, traced.status_code)
        self.assertEqual(plain.text, traced.text)

    def test_a_failure_inside_a_traced_block_does_not_corrupt_state(self):
        budget = RequestBudget(100)
        with mock.patch.object(requests, "request", Wire()), request_trace.install() as trace, \
             budget.installed():
            try:
                with request_trace.detail():
                    requests.request("GET", "https://a.example/job/1")
                    raise RuntimeError("board exploded")
            except RuntimeError:
                pass
            requests.request("GET", "https://a.example")  # back to listing
        self.assertEqual(trace.initial_detail, 1)
        self.assertEqual(trace.initial_listing, 1)
        self.assertTrue(trace.reconciles(budget.count))

    def test_all_nine_ats_provider_branches_are_covered(self):
        """Six providers make listing calls only; three also make detail calls.
        Every branch is accounted for -- none is silently unclassified."""
        import ats_board_registry

        with open(ats_board_registry.__file__, encoding="utf-8") as handle:
            source = handle.read()
        listing_only = ("lever", "ashby", "recruitee", "workable", "personio",
                        "cornerstone_ondemand")
        with_detail = ("greenhouse", "smartrecruiters", "workday")
        for provider in listing_only + with_detail:
            self.assertIn(f'provider == "{provider}"', source, provider)
        # Exactly three detail-annotated call sites, one per detail provider.
        self.assertEqual(source.count("with _detail_request():"), len(with_detail))
        self.assertEqual(source.count("detail_calls += 1"), len(with_detail))


# --------------------------------------------------------------------------
# G -- deterministic scheduling
# --------------------------------------------------------------------------


def registry(n, *, checked_hours_ago=1.0, failures=0):
    now = datetime.now(timezone.utc)
    return [
        {"provider": ["workday", "greenhouse", "ashby", "lever"][i % 4],
         "identifier": f"board{i}",
         "company_name": f"Co{i}",
         "last_checked_at": (now - timedelta(hours=checked_hours_ago)).isoformat(),
         "consecutive_failures": failures}
        for i in range(n)
    ]


class SchedulingTests(unittest.TestCase):
    def test_slot_assignment_is_stable_and_bounded(self):
        a = slot_for("workday", "acme", 7)
        self.assertEqual(a, slot_for("workday", "acme", 7))
        self.assertEqual(a, slot_for("  WORKDAY ", "acme", 7))
        for i in range(200):
            self.assertIn(slot_for("greenhouse", f"b{i}", 7), range(7))

    def test_one_full_cycle_visits_every_board_exactly_once(self):
        boards = registry(145)
        result = simulate(boards, cycle_length=7, cycles=1, max_age_hours=0)
        self.assertEqual(result["boards"], 145)
        self.assertTrue(result["full_coverage"])
        self.assertEqual(result["starved_boards"], 0)
        self.assertEqual(result["visits_per_board"], [1])

    def test_multiple_cycles_visit_every_board_once_per_cycle(self):
        result = simulate(registry(145), cycle_length=7, cycles=3, max_age_hours=0)
        self.assertTrue(result["full_coverage"])
        self.assertTrue(result["visits_equal_cycles"])
        self.assertEqual(result["starved_boards"], 0)

    def test_no_run_takes_the_whole_registry_the_way_the_legacy_rule_does(self):
        result = simulate(registry(145), cycle_length=7, cycles=1, max_age_hours=0)
        self.assertLess(result["max_per_run"], 145,
                        "a partitioned run must never be the all-or-nothing herd")
        self.assertGreater(result["min_per_run"], 0, "no run may be empty")

    def test_the_per_run_cap_is_respected(self):
        decision = partitioned_schedule(registry(145), position=0, cycle_length=7,
                                        max_boards_per_run=5, max_age_hours=0)
        self.assertEqual(len(decision.selected), 5)
        self.assertGreater(decision.capped_out, 0)

    def test_schedules_are_deterministic_for_identical_inputs(self):
        boards = registry(60)
        first = partitioned_schedule(boards, position=3, cycle_length=7, max_age_hours=0)
        second = partitioned_schedule(boards, position=3, cycle_length=7, max_age_hours=0)
        self.assertEqual([b["identifier"] for b in first.selected],
                         [b["identifier"] for b in second.selected])

    def test_an_overdue_board_jumps_its_slot(self):
        boards = registry(20, checked_hours_ago=1.0)
        boards[7]["last_checked_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=500)
        ).isoformat()
        decision = partitioned_schedule(boards, position=0, cycle_length=7,
                                        max_age_hours=168)
        self.assertIn(f"{boards[7]['provider']}:board7", decision.overdue)
        self.assertIn("board7", [b["identifier"] for b in decision.selected])

    def test_a_never_checked_board_is_treated_as_overdue(self):
        boards = registry(10)
        boards[3]["last_checked_at"] = ""
        decision = partitioned_schedule(boards, position=0, cycle_length=7)
        self.assertIn("board3", [b["identifier"] for b in decision.selected])

    def test_failed_boards_get_bounded_retry_and_are_not_retried_forever(self):
        boards = registry(10, failures=1)
        decision = partitioned_schedule(boards, position=0, cycle_length=7,
                                        max_retry_attempts=2, max_age_hours=0)
        self.assertEqual(len(decision.retry), 10)
        exhausted = registry(10, failures=3)
        later = partitioned_schedule(exhausted, position=0, cycle_length=7,
                                     max_retry_attempts=2, max_age_hours=0)
        self.assertEqual(later.retry, [], "retries must be bounded, not endless")

    def test_boards_with_no_identity_are_skipped_and_counted(self):
        decision = partitioned_schedule([{"provider": "", "identifier": ""}],
                                        position=0, cycle_length=7)
        self.assertEqual(decision.selected, [])
        self.assertEqual(decision.reasons["missing_identity"], 1)

    def test_the_legacy_scheduler_is_unchanged_while_the_flag_is_off(self):
        boards = registry(145)
        legacy = boards[:5]
        decision = select_boards(boards, legacy_due=legacy, enabled=False)
        self.assertEqual([b["identifier"] for b in decision.selected],
                         [b["identifier"] for b in legacy])
        self.assertEqual(decision.reasons, {"legacy_interval_scheduler": 5})
        self.assertEqual(decision.cycle_length, 0)

    def test_enabling_the_flag_switches_to_partitioning(self):
        boards = registry(145)
        decision = select_boards(boards, legacy_due=boards[:5], enabled=True,
                                 position=2, cycle_length=7, max_age_hours=0)
        self.assertEqual(decision.cycle_length, 7)
        self.assertNotEqual(len(decision.selected), 5)

    def test_the_default_cycle_length_is_declared(self):
        self.assertEqual(DEFAULT_CYCLE_LENGTH, 7)


if __name__ == "__main__":
    unittest.main()
