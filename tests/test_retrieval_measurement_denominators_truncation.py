"""Provider denominators and truncation classification.

Four providers publish a total that the pipeline currently throws away:
Himalayas ``totalCount`` (kept in metadata but never used for coverage),
Adzuna ``count`` (never read at all -- adzuna_client.py:379 takes only
``results``), SmartRecruiters ``totalFound`` (computed and discarded at
ats_board_registry.py:965) and Workday ``total`` (same, at :1133). Reading them
at the transport boundary needs no adapter change.

The truncation tests exist because ``configured_cap`` and
``provider_exhaustion`` look identical in the output and mean opposite things.
Merging them is how a retrieval ceiling stays invisible for months.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import config
from free_job_sources import FetchPayload, SourceResult

from retrieval_measurement.instrument import (
    DENOMINATOR_FIELDS,
    MeasuringFetcher,
    classify_truncation,
)


def payload(body, status=200, url="https://example.com"):
    text = body if isinstance(body, str) else json.dumps(body)
    return FetchPayload(status_code=status, url=url, text=text)


def fetcher_returning(*bodies):
    queue = list(bodies)

    def inner(url, **kwargs):
        return queue.pop(0) if queue else payload({}, status=599, url=url)

    return MeasuringFetcher(inner)


class DenominatorExtractionTests(unittest.TestCase):
    def test_himalayas_total_count_is_whole_feed(self):
        fetcher = fetcher_returning(payload({"jobs": [], "totalCount": 480}))
        with fetcher.context(source="himalayas"):
            fetcher("https://himalayas.app/jobs/api")
        record = fetcher.denominator_for("himalayas")
        self.assertEqual(record.value, 480)
        self.assertEqual(record.field_name, "totalCount")
        self.assertEqual(record.scope, "whole_feed")
        self.assertIn("NOT a US-only", record.semantics)

    def test_adzuna_count_is_per_query(self):
        fetcher = fetcher_returning(payload({"results": [], "count": 1240}))
        with fetcher.context(source="adzuna", query_key="senior software engineer"):
            fetcher("https://api.adzuna.com/v1/api/jobs/us/search/1")
        record = fetcher.denominator_for("adzuna")
        self.assertEqual(record.value, 1240)
        self.assertEqual(record.field_name, "count")
        self.assertEqual(record.scope, "per_query")

    def test_smartrecruiters_accepts_either_total_field(self):
        for body, expected in (({"totalFound": 90}, 90), ({"total": 45}, 45)):
            fetcher = fetcher_returning(payload(body))
            with fetcher.context(source="ats_smartrecruiters", board_key="sr:acme"):
                fetcher("https://api.smartrecruiters.com/v1/companies/acme/postings")
            self.assertEqual(fetcher.denominator_for("ats_smartrecruiters").value, expected)

    def test_workday_total_is_per_board(self):
        fetcher = fetcher_returning(payload({"total": 312}))
        with fetcher.context(source="ats_workday", board_key="wd:contoso"):
            fetcher("https://contoso.wd1.myworkdayjobs.com/wday/cxs/contoso/careers/jobs")
        record = fetcher.denominator_for("ats_workday")
        self.assertEqual(record.value, 312)
        self.assertEqual(record.scope, "per_board")

    def test_sources_without_a_published_total_report_none(self):
        fetcher = fetcher_returning(payload({"jobs": [{"id": 1}]}))
        with fetcher.context(source="jobicy"):
            fetcher("https://jobicy.com/api/v2/remote-jobs")
        self.assertIsNone(fetcher.denominator_for("jobicy"))
        self.assertNotIn("jobicy", DENOMINATOR_FIELDS)

    def test_malformed_or_negative_totals_are_ignored_not_guessed(self):
        for body in ({"count": "many"}, {"count": -5}, "not json at all"):
            fetcher = fetcher_returning(payload(body))
            with fetcher.context(source="adzuna", query_key="q"):
                fetcher("https://api.adzuna.com/v1/api/jobs/us/search/1")
            self.assertIsNone(fetcher.denominator_for("adzuna"))

    def test_repeated_whole_feed_observations_take_the_maximum(self):
        fetcher = fetcher_returning(
            payload({"jobs": [], "totalCount": 400}),
            payload({"jobs": [], "totalCount": 480}),
        )
        with fetcher.context(source="himalayas"):
            fetcher("https://himalayas.app/jobs/api")
            fetcher("https://himalayas.app/jobs/api")
        self.assertEqual(fetcher.denominator_for("himalayas").value, 480)

    def test_per_query_totals_sum_across_distinct_scopes_only(self):
        fetcher = fetcher_returning(
            payload({"count": 100}), payload({"count": 100}), payload({"count": 50}),
        )
        with fetcher.context(source="adzuna", query_key="query-a"):
            fetcher("https://api.adzuna.com/v1/api/jobs/us/search/1")
            fetcher("https://api.adzuna.com/v1/api/jobs/us/search/2")  # same scope, not added twice
        with fetcher.context(source="adzuna", query_key="query-b"):
            fetcher("https://api.adzuna.com/v1/api/jobs/us/search/1")
        record = fetcher.denominator_for("adzuna")
        self.assertEqual(record.value, 150)
        self.assertIn("upper bound", record.semantics)

    def test_denominators_are_never_pooled_across_providers(self):
        fetcher = fetcher_returning(
            payload({"jobs": [], "totalCount": 480}), payload({"count": 1240}),
        )
        with fetcher.context(source="himalayas"):
            fetcher("https://himalayas.app/jobs/api")
        with fetcher.context(source="adzuna", query_key="q"):
            fetcher("https://api.adzuna.com/v1/api/jobs/us/search/1")
        self.assertEqual(fetcher.denominator_for("himalayas").value, 480)
        self.assertEqual(fetcher.denominator_for("adzuna").value, 1240)
        # There is deliberately no API that returns a combined total.
        self.assertFalse(hasattr(fetcher, "total_denominator"))


class RequestLedgerTests(unittest.TestCase):
    def test_parameter_values_are_never_recorded(self):
        """Adzuna params carry app_id and app_key. Only key names are kept."""
        fetcher = fetcher_returning(payload({"results": [], "count": 1}))
        with fetcher.context(source="adzuna", query_key="q"):
            fetcher(
                "https://api.adzuna.com/v1/api/jobs/us/search/1",
                params={"app_id": "REAL_ID", "app_key": "REAL_KEY", "what": "engineer"},
            )
        record = fetcher.requests[0]
        self.assertEqual(record.param_keys, ["app_id", "app_key", "what"])
        serialized = json.dumps(record.to_dict())
        self.assertNotIn("REAL_ID", serialized)
        self.assertNotIn("REAL_KEY", serialized)

    def test_requests_are_attributed_to_the_active_context(self):
        fetcher = fetcher_returning(payload({}), payload({}))
        with fetcher.context(source="himalayas"):
            fetcher("https://himalayas.app/jobs/api")
        with fetcher.context(source="jobicy"):
            fetcher("https://jobicy.com/api/v2/remote-jobs")
        self.assertEqual([r.source for r in fetcher.requests], ["himalayas", "jobicy"])
        self.assertEqual(len(fetcher.requests_for("himalayas")), 1)

    def test_context_restores_the_previous_scope(self):
        fetcher = fetcher_returning(payload({}), payload({}))
        with fetcher.context(source="outer"):
            with fetcher.context(source="inner"):
                fetcher("https://example.com/inner")
            fetcher("https://example.com/outer")
        self.assertEqual([r.source for r in fetcher.requests], ["inner", "outer"])


class TruncationTests(unittest.TestCase):
    def test_exact_cap_signature_is_a_configured_cap(self):
        cap = config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE
        result = SourceResult(source="jobicy", jobs=[{}] * cap, raw_records=cap, pages=1)
        records = classify_truncation("jobicy", result)
        kinds = {record.kind for record in records if record.detected}
        self.assertIn("configured_cap", kinds)
        capped = next(record for record in records if record.kind == "configured_cap")
        self.assertEqual(capped.applied_cap, cap)

    def test_himalayas_page_cap_is_reported_separately(self):
        with patch.object(config, "HIMALAYAS_MAX_PAGES", 3):
            result = SourceResult(source="himalayas", jobs=[{}] * 5, raw_records=5, pages=3)
            records = classify_truncation("himalayas", result)
        reasons = {record.reason for record in records if record.detected}
        self.assertIn("HIMALAYAS_MAX_PAGES reached", reasons)

    def test_an_unexplained_shortfall_is_never_called_provider_exhaustion(self):
        """Retrieving 4 of 480 with no cap and no error is not exhaustion. The
        provider says there is more and we did not ask for it."""
        from retrieval_measurement.schema import DenominatorRecord

        denominator = DenominatorRecord(
            provider="himalayas", value=480, field_name="totalCount",
            scope="whole_feed", scope_key="", semantics="", observed_at="",
        )
        result = SourceResult(source="himalayas", jobs=[{}] * 4, raw_records=4, pages=1)
        records = classify_truncation("himalayas", result, denominator=denominator)
        kinds = {record.kind for record in records if record.detected}
        self.assertEqual(kinds, {"unexplained_shortfall"})
        self.assertNotIn("provider_exhaustion", kinds)
        shortfall = next(record for record in records if record.detected)
        self.assertEqual(shortfall.known_unfetched, 476)

    def test_a_shortfall_behind_a_cap_is_attributed_to_the_cap(self):
        from retrieval_measurement.schema import DenominatorRecord

        cap = config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE
        denominator = DenominatorRecord(
            provider="himalayas", value=cap * 3, field_name="totalCount",
            scope="whole_feed", scope_key="", semantics="", observed_at="",
        )
        result = SourceResult(source="himalayas", jobs=[{}] * cap, raw_records=cap, pages=1)
        records = classify_truncation("himalayas", result, denominator=denominator)
        kinds = {record.kind for record in records if record.detected}
        self.assertEqual(kinds, {"configured_cap"})

    def test_shortfall_against_a_provider_total_reports_known_unfetched(self):
        from retrieval_measurement.schema import DenominatorRecord

        denominator = DenominatorRecord(
            provider="himalayas", value=480, field_name="totalCount",
            scope="whole_feed", scope_key="", semantics="", observed_at="",
        )
        result = SourceResult(source="himalayas", jobs=[{}] * 100, raw_records=100, pages=5)
        records = classify_truncation("himalayas", result, denominator=denominator)
        shortfall = next(record for record in records if record.known_unfetched)
        self.assertEqual(shortfall.known_unfetched, 380)
        self.assertEqual(shortfall.evidence["provider_total"], 480)
        self.assertEqual(shortfall.evidence["retrieved"], 100)

    def test_meeting_the_provider_total_is_exhaustion_not_a_cap(self):
        from retrieval_measurement.schema import DenominatorRecord

        denominator = DenominatorRecord(
            provider="himalayas", value=40, field_name="totalCount",
            scope="whole_feed", scope_key="", semantics="", observed_at="",
        )
        result = SourceResult(source="himalayas", jobs=[{}] * 40, raw_records=40, pages=2)
        records = classify_truncation("himalayas", result, denominator=denominator)
        exhaustion = next(record for record in records if record.kind == "provider_exhaustion")
        self.assertFalse(exhaustion.detected)
        self.assertEqual(exhaustion.known_unfetched, 0)
        self.assertNotIn("configured_cap", {record.kind for record in records if record.detected})

    def test_errors_produce_an_explicit_error_stop(self):
        result = SourceResult(
            source="adzuna", jobs=[{}] * 3, raw_records=3, pages=1,
            errors=["quota_exhausted:engineer:page2"],
        )
        records = classify_truncation("adzuna", result)
        error_stop = next(record for record in records if record.kind == "error_stop")
        self.assertTrue(error_stop.detected)
        self.assertIn("quota_exhausted", error_stop.reason)

    def test_a_clean_short_run_is_explicitly_not_truncated(self):
        result = SourceResult(source="remotive", jobs=[{}] * 4, raw_records=4, pages=1)
        records = classify_truncation("remotive", result)
        self.assertEqual([record.kind for record in records], ["not_truncated"])
        self.assertFalse(records[0].detected)


if __name__ == "__main__":
    unittest.main()
