"""Offline, fully-mocked tests for adzuna_client.py (FINAL_30_PLUS_SYSTEM_SPEC.md
section 11 / Phase 5). No live network access. All fixtures below are
synthetic -- fabricated for this test, not captured from any real Adzuna
response -- and use obviously fake company/domain names.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import adzuna_client
from adzuna_client import AdzunaAdapter, AdzunaSettings, normalize_adzuna_job
from free_job_sources import FetchPayload
from job_filter import dedup_key


def _settings(**overrides) -> AdzunaSettings:
    base = dict(
        enabled=True,
        country="us",
        app_id="test-app-id",
        app_key="test-app-key",
        results_per_page=50,
        max_pages_per_query=3,
        max_requests_per_run=40,
        max_days_old=30,
        timeout_seconds=20,
    )
    base.update(overrides)
    return AdzunaSettings(**base)


def _row(job_id, title="Revenue Systems Engineer", company="Synthetic Fixture Corp", **extra):
    row = {
        "id": str(job_id),
        "title": title,
        "company": {"display_name": company},
        "location": {"display_name": "Remote - United States"},
        "category": {"label": "IT Jobs"},
        "description": "A fully synthetic job description for testing only.",
        "redirect_url": f"https://example-adzuna-redirect.test/ad/{job_id}",
        "created": "2026-07-20T09:15:00Z",
        "contract_time": "full_time",
        "salary_min": 90000,
        "salary_max": 120000,
    }
    row.update(extra)
    return row


def _response(results, *, status_code=200):
    return FetchPayload(
        status_code=status_code,
        url="https://api.adzuna.com/v1/api/jobs/us/search/1",
        text=json.dumps({"results": results, "count": len(results)}),
    )


class NormalizationTests(unittest.TestCase):
    def test_field_mapping(self):
        job = normalize_adzuna_job(_row("123"))
        self.assertEqual(job["job_id"], "adzuna:123")
        self.assertEqual(job["job_title"], "Revenue Systems Engineer")
        self.assertEqual(job["employer_name"], "Synthetic Fixture Corp")
        self.assertEqual(job["employer_website"], "")  # redirect_url is never a company domain
        self.assertEqual(job["job_publisher"], "Adzuna")
        self.assertEqual(job["job_apply_link"], "https://example-adzuna-redirect.test/ad/123")
        self.assertFalse(job["job_apply_is_direct"])
        self.assertTrue(job["_provider_record_structured"])
        self.assertEqual(job["_acquisition_source"], "adzuna")
        self.assertEqual(job["job_posted_at_datetime_utc"], "2026-07-20T09:15:00+00:00".replace("+00:00", "Z"))
        self.assertEqual(job["job_country"], "US")
        self.assertEqual(job["job_employment_type"], "FULLTIME")

    def test_part_time_and_contract_mapping(self):
        part_time = normalize_adzuna_job(_row("1", contract_time="part_time"))
        self.assertEqual(part_time["job_employment_type"], "PARTTIME")
        contract = normalize_adzuna_job(_row("2", contract_time="", contract_type="contract"))
        self.assertEqual(contract["job_employment_type"], "CONTRACTOR")

    def test_remote_inference_from_title_or_description(self):
        remote = normalize_adzuna_job(_row("3", title="Remote Revenue Ops Manager"))
        self.assertTrue(remote["job_is_remote"])
        onsite = normalize_adzuna_job(_row("4", title="Revenue Ops Manager", **{"description": "Onsite role in our downtown office."}))
        self.assertFalse(onsite["job_is_remote"])

    def test_missing_job_id_is_rejected(self):
        row = _row("5")
        row["id"] = ""
        self.assertIsNone(normalize_adzuna_job(row))

    def test_output_is_dedup_key_compatible(self):
        job = normalize_adzuna_job(_row("6"))
        company, title = dedup_key(job)
        self.assertTrue(company)
        self.assertTrue(title)


class AdapterGatingTests(unittest.TestCase):
    def test_disabled_by_default_skips_without_error(self):
        adapter = AdzunaAdapter(settings=_settings(enabled=False))
        result = adapter.fetch(fetcher=lambda *a, **k: self.fail("must not call network when disabled"))
        self.assertTrue(result.success)
        self.assertEqual(result.jobs, [])
        self.assertEqual(result.requests_attempted, 0)
        self.assertFalse(result.metadata["enabled"])
        self.assertEqual(result.metadata["skipped_reason"], "disabled_by_config")

    def test_missing_credentials_fails_without_network_call(self):
        adapter = AdzunaAdapter(settings=_settings(enabled=True, app_id="", app_key=""))
        result = adapter.fetch(fetcher=lambda *a, **k: self.fail("must not call network without credentials"))
        self.assertFalse(result.success)
        self.assertIn("ADZUNA_APP_ID", result.metadata["missing_variables"])
        self.assertIn("ADZUNA_APP_KEY", result.metadata["missing_variables"])
        self.assertEqual(result.requests_attempted, 0)

    def test_env_defaults_are_disabled_when_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = AdzunaSettings()
            self.assertFalse(settings.enabled)
            self.assertFalse(settings.credentials_configured)


class PaginationTests(unittest.TestCase):
    def test_stops_when_a_page_returns_no_new_jobs(self):
        calls = []

        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            calls.append(params["what"])
            page = int(url.rsplit("/", 1)[-1])
            if page == 1:
                return _response([_row("p1-a"), _row("p1-b")])
            return _response([])  # page 2: nothing new -> pagination should stop

        adapter = AdzunaAdapter(settings=_settings(max_pages_per_query=5), queries=["revenue operations"])
        result = adapter.fetch(fetcher=fetcher)
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.requests_attempted, 2)  # page 1 (jobs) + page 2 (empty, stops)

    def test_multi_query_multi_page_accumulates_and_dedupes(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            query = params["what"]
            page = int(url.rsplit("/", 1)[-1])
            if page == 1:
                return _response([_row(f"{query}-{page}-1"), _row(f"{query}-{page}-2")])
            return _response([])

        adapter = AdzunaAdapter(
            settings=_settings(max_pages_per_query=3),
            queries=["revenue operations", "marketing operations"],
        )
        result = adapter.fetch(fetcher=fetcher)
        self.assertEqual(len(result.jobs), 4)
        self.assertTrue(result.success)

    def test_duplicate_job_ids_across_pages_are_deduplicated(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            page = int(url.rsplit("/", 1)[-1])
            if page == 1:
                return _response([_row("dup-1"), _row("dup-2")])
            if page == 2:
                return _response([_row("dup-1"), _row("dup-3")])  # dup-1 repeats
            return _response([])

        adapter = AdzunaAdapter(settings=_settings(max_pages_per_query=5), queries=["revenue operations"])
        result = adapter.fetch(fetcher=fetcher)
        job_ids = sorted(job["job_id"] for job in result.jobs)
        self.assertEqual(job_ids, ["adzuna:dup-1", "adzuna:dup-2", "adzuna:dup-3"])

    def test_request_budget_bounds_total_calls(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            page = int(url.rsplit("/", 1)[-1])
            return _response([_row(f"budget-{page}-1")])  # always returns a fresh job -> would page forever

        adapter = AdzunaAdapter(
            settings=_settings(max_pages_per_query=100, max_requests_per_run=3),
            queries=["revenue operations"],
        )
        result = adapter.fetch(fetcher=fetcher)
        self.assertEqual(result.requests_attempted, 3)


class ErrorClassificationTests(unittest.TestCase):
    def test_authentication_failure_stops_immediately(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            return FetchPayload(status_code=401, url=url, text="Unauthorized")

        adapter = AdzunaAdapter(settings=_settings(), queries=["revenue operations", "marketing operations"])
        result = adapter.fetch(fetcher=fetcher)
        self.assertFalse(result.success)
        self.assertTrue(result.metadata["auth_failed"])
        self.assertEqual(result.requests_attempted, 1)  # must not burn budget retrying auth failures

    def test_rate_limit_is_recorded_and_distinct_from_quota_exhaustion(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            return FetchPayload(status_code=429, url=url, text="Too Many Requests")

        adapter = AdzunaAdapter(settings=_settings(), queries=["revenue operations"])
        result = adapter.fetch(fetcher=fetcher)
        self.assertIn("revenue operations", result.metadata["rate_limited_queries"])
        self.assertFalse(result.metadata["quota_exhausted"])

    def test_quota_exhaustion_detected_from_response_body(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            return FetchPayload(status_code=403, url=url, text="Monthly quota exceeded for this application")

        adapter = AdzunaAdapter(settings=_settings(), queries=["revenue operations"])
        result = adapter.fetch(fetcher=fetcher)
        self.assertTrue(result.metadata["quota_exhausted"])

    def test_transient_server_error_is_recorded_not_raised(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            return FetchPayload(status_code=503, url=url, text="Service Unavailable")

        adapter = AdzunaAdapter(settings=_settings(), queries=["revenue operations"])
        result = adapter.fetch(fetcher=fetcher)
        self.assertTrue(any("transient_error" in error for error in result.errors))

    def test_malformed_json_is_handled_without_raising(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            return FetchPayload(status_code=200, url=url, text="not json at all {{{")

        adapter = AdzunaAdapter(settings=_settings(), queries=["revenue operations"])
        result = adapter.fetch(fetcher=fetcher)
        self.assertEqual(result.jobs, [])
        self.assertTrue(any("malformed_response" in error for error in result.errors))

    def test_missing_results_array_is_handled_without_raising(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            return FetchPayload(status_code=200, url=url, text=json.dumps({"unexpected": "shape"}))

        adapter = AdzunaAdapter(settings=_settings(), queries=["revenue operations"])
        result = adapter.fetch(fetcher=fetcher)
        self.assertEqual(result.jobs, [])
        self.assertTrue(any("malformed_response" in error for error in result.errors))

    def test_timeout_is_passed_through_to_fetcher(self):
        seen_timeouts = []

        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            seen_timeouts.append(timeout)
            return _response([])

        adapter = AdzunaAdapter(settings=_settings(timeout_seconds=7), queries=["revenue operations"])
        adapter.fetch(fetcher=fetcher)
        self.assertEqual(seen_timeouts, [7])


class RunAdzunaAcquisitionEntryPointTests(unittest.TestCase):
    def test_module_level_entry_point_matches_adapter(self):
        def fetcher(url, *, params=None, headers=None, timeout=None, method="GET", json_body=None):
            return _response([_row("entry-1")])

        result = adzuna_client.run_adzuna_acquisition(
            fetcher=fetcher, queries=["revenue operations"], settings=_settings(max_pages_per_query=1)
        )
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.source, "adzuna")


if __name__ == "__main__":
    unittest.main()
