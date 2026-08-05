"""Parity: the harness must measure production, not a variant of it.

Three things have to hold, or every number the harness reports describes a
different pipeline than the one that runs in Railway:

1. The measuring fetcher is observationally transparent -- adapters produce
   identical ``SourceResult``s with and without it.
2. The ``transport`` and ``output_dir`` seams added to ``jsearch_scraper``
   change nothing when they are not supplied.
3. The harness's per-source discard counters equal the production loop's
   counters, over the same input, with the same removal semantics.

Test 3 transcribes ``multi_source_acquisition.py:987-1009`` verbatim rather
than importing it, because that loop is inline inside a 400-line function and
cannot be called in isolation. The transcription is the reference; if the
harness drifts from it, this test fails.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import jsearch_scraper
from adzuna_client import AdzunaAdapter, AdzunaSettings
from free_job_sources import build_adapters
from job_filter import assess_pre_enrichment_viability
from jsearch_scraper import JSearchFetchResult, build_search_query, fetch_jobs_for_role, is_excluded_title
from multi_source_acquisition import _classify, _dedupe

from retrieval_measurement.accounting import attribute_discards, production_equivalent_uniqueness
from retrieval_measurement.drivers import FixtureFetcher
from retrieval_measurement.instrument import JSearchTransport, MeasuringFetcher

FIXTURE = Path(__file__).parent / "fixtures" / "retrieval_measurement" / "sources.json"


def load_fixture() -> dict:
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)


class AdapterTransparencyTests(unittest.TestCase):
    """Every field of SourceResult, for every adapter, with and without the
    wrapper. Anything less would let a subtle divergence through."""

    def _compare(self, adapter, fixture):
        baseline = adapter.fetch(FixtureFetcher(fixture))
        wrapped = MeasuringFetcher(FixtureFetcher(fixture))
        with wrapped.context(source=adapter.name):
            measured = adapter.fetch(wrapped)
        for field in ("jobs", "raw_records", "requests_attempted", "requests_succeeded",
                      "pages", "success", "errors", "metadata"):
            self.assertEqual(
                getattr(baseline, field), getattr(measured, field),
                f"{adapter.name}.{field} diverged under the measuring fetcher",
            )
        return measured

    def test_every_free_adapter_is_unchanged(self):
        fixture = load_fixture()
        produced = 0
        for adapter in build_adapters(config.FREE_JOB_SOURCES):
            result = self._compare(adapter, fixture)
            produced += len(result.jobs)
        # A transparency test over empty results would prove nothing.
        self.assertGreater(produced, 0, "fixtures must produce records for this test to mean anything")

    def test_adzuna_adapter_is_unchanged(self):
        fixture = load_fixture()
        settings = AdzunaSettings(
            enabled=True, app_id="test-id", app_key="test-key", country="us",
            results_per_page=50, max_pages_per_query=1, max_requests_per_run=2,
            max_days_old=7, timeout_seconds=10,
        )
        adapter = AdzunaAdapter(settings=settings, queries=["senior software engineer"])
        result = self._compare(adapter, fixture)
        self.assertGreater(len(result.jobs), 0)

    def test_the_wrapper_returns_the_inner_payload_object_itself(self):
        inner_calls = []

        def inner(url, **kwargs):
            from free_job_sources import FetchPayload
            payload = FetchPayload(status_code=200, url=url, text="{}")
            inner_calls.append(payload)
            return payload

        fetcher = MeasuringFetcher(inner)
        with fetcher.context(source="himalayas"):
            returned = fetcher("https://himalayas.app/jobs/api")
        self.assertIs(returned, inner_calls[0])


class JSearchSeamTests(unittest.TestCase):
    def test_default_transport_is_request_with_retry(self):
        """Omitting transport must reach exactly the production call path."""
        recorded = {}

        class _Response:
            headers = {}
            url = "https://jsearch.example/search"

            def json(self):
                return {"status": "OK", "data": [{"job_id": "j1"}]}

        def fake(method, url, **kwargs):
            recorded["method"] = method
            recorded["url"] = url
            recorded["params"] = kwargs.get("params")
            return _Response()

        with patch.object(jsearch_scraper, "request_with_retry", side_effect=fake) as patched:
            result = fetch_jobs_for_role("Software Engineer", page=1, num_pages=1)
        self.assertTrue(patched.called)
        self.assertEqual(recorded["method"], "GET")
        self.assertEqual(recorded["url"], config.JSEARCH_ENDPOINT)
        self.assertEqual(result.jobs, [{"job_id": "j1"}])

    def test_injected_transport_produces_an_identical_result(self):
        query = build_search_query("Software Engineer")
        body = {"status": "OK", "data": [{"job_id": "j1"}, {"job_id": "j2"}]}

        class _Response:
            headers = {"x-ratelimit-requests-remaining": "100"}
            url = "https://jsearch.example/search"

            def json(self):
                return body

        with patch.object(jsearch_scraper, "request_with_retry", return_value=_Response()):
            baseline = fetch_jobs_for_role("Software Engineer", page=1, num_pages=1)

        transport = JSearchTransport(recorded={f"{query}|page=1": body})
        measured = fetch_jobs_for_role(
            "Software Engineer", page=1, num_pages=1, transport=transport
        )

        self.assertEqual(baseline.jobs, measured.jobs)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.misses, [])

    def test_query_construction_is_untouched_by_the_seam(self):
        """The harness never builds a JSearch query. Production does, and the
        transport only observes the result."""
        transport = JSearchTransport(recorded={})
        fetch_jobs_for_role("Software Engineer", page=2, num_pages=1, transport=transport)
        expected = build_search_query("Software Engineer")
        self.assertEqual(transport.requests[0].query_key, f"{expected}|page=2")

    def test_transport_signature_matches_request_with_retry(self):
        import inspect
        from http_utils import request_with_retry

        production = list(inspect.signature(request_with_retry).parameters)[:2]
        self.assertEqual(production, ["method", "url"])
        transport = JSearchTransport(recorded={})
        # Same positional contract; anything else would break on the real path.
        transport("GET", "https://example.com", params={"query": "x", "page": "1"})
        self.assertEqual(transport.requests[0].method, "GET")


class OutputDirSeamTests(unittest.TestCase):
    """run_daily_scrape wrote unconditionally to config.OUTPUT_DIR
    (jsearch_scraper.py:1187-1193). The seam must redirect it without changing
    the default."""

    def _run(self, output_dir=None):
        query = build_search_query("Software Engineer")
        body = {
            "status": "OK",
            "data": [{
                "job_id": "js-1",
                "job_title": "Software Engineer",
                "employer_name": "Northwind Systems",
                "job_apply_link": "https://example.com/js-1",
                "job_description": "We are hiring a software engineer to own delivery end to end.",
                "job_country": "US",
                "job_is_remote": True,
                "job_posted_at_datetime_utc": "2026-07-28T09:00:00Z",
            }],
        }
        transport = JSearchTransport(recorded={f"{query}|page={page}": body for page in range(1, 5)})
        return jsearch_scraper.run_daily_scrape(
            search_roles=["Software Engineer"],
            max_queries=1,
            base_num_pages=1,
            allow_adaptive=False,
            transport=transport,
            output_dir=output_dir,
        )

    def test_no_transport_means_the_call_signature_is_byte_identical(self):
        """The seam must be invisible when unused.

        Several existing tests replace fetch_jobs_for_role with a stub whose
        signature is ``(role, *, page, num_pages)``. Forwarding ``transport=None``
        would raise TypeError inside production's try/except and silently turn
        every query into a failure -- which is exactly what happened before this
        was made conditional.
        """
        seen_kwargs = []

        def strict_stub(role: str, *, page: int = 1, num_pages=None):
            seen_kwargs.append({"page": page, "num_pages": num_pages})
            return JSearchFetchResult(jobs=[], duration_seconds=0.0, quota={})

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=strict_stub), \
                 patch.object(config, "OUTPUT_DIR", tmp), \
                 patch.object(config, "RAPIDAPI_KEY", "test-key-not-a-credential"), \
                 patch.object(config, "SEEN_JOBS_FILE", str(Path(tmp) / "seen.json")), \
                 patch.object(config, "MIN_JOBS_PER_RUN", 0):
                jsearch_scraper.run_daily_scrape(
                    search_roles=["Software Engineer"], max_queries=1,
                    base_num_pages=1, allow_adaptive=False,
                )
        self.assertTrue(seen_kwargs, "the stub was never reached")

    def test_default_writes_to_config_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            with patch.object(config, "OUTPUT_DIR", str(raw)), \
                 patch.object(config, "RAPIDAPI_KEY", "test-key-not-a-credential"), \
                 patch.object(config, "SEEN_JOBS_FILE", str(Path(tmp) / "seen.json")), \
                 patch.object(config, "MIN_JOBS_PER_RUN", 0):
                result = self._run()
            self.assertTrue(Path(result.output_path).is_file())
            self.assertEqual(Path(result.output_path).parent, raw)

    def test_output_dir_redirects_both_the_daily_file_and_the_history_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            production = Path(tmp) / "raw"
            production.mkdir()
            harness = Path(tmp) / "harness"
            with patch.object(config, "OUTPUT_DIR", str(production)), \
                 patch.object(config, "RAPIDAPI_KEY", "test-key-not-a-credential"), \
                 patch.object(config, "SEEN_JOBS_FILE", str(Path(tmp) / "seen.json")), \
                 patch.object(config, "MIN_JOBS_PER_RUN", 0):
                result = self._run(output_dir=str(harness))
            self.assertEqual(Path(result.output_path).parent, harness)
            self.assertTrue((harness / "history").is_dir())
            # Production output directory must be untouched by a measured run.
            self.assertEqual(list(production.iterdir()), [])


class CounterSemanticsParityTests(unittest.TestCase):
    """The harness's counters must equal production's, exactly."""

    @staticmethod
    def production_loop(all_jobs, registry):
        """Verbatim transcription of multi_source_acquisition.py:987-1009."""
        stats = {
            "excluded_by_seniority": 0, "previously_seen_removed": 0,
            "missing_job_id_skipped": 0, "prefilter_viable": 0, "prefilter_rejected": 0,
            "role_accept": 0, "role_review": 0, "role_reject": 0,
        }
        normalized = []
        for original in all_jobs:
            job = dict(original)
            if not str(job.get("job_id") or "").strip():
                stats["missing_job_id_skipped"] += 1
                continue
            if registry.has_job_id(str(job.get("job_id"))):
                stats["previously_seen_removed"] += 1
                continue
            if is_excluded_title(str(job.get("job_title") or "")):
                stats["excluded_by_seniority"] += 1
                continue
            _classify(job)
            status = str(job.get("_role_relevance_status") or "reject")
            stats[f"role_{status}"] = int(stats.get(f"role_{status}", 0)) + 1
            assessment = assess_pre_enrichment_viability(job)
            job["_prefilter_viable"] = assessment.eligible
            job["_prefilter_stat"] = assessment.stat_name
            job["_prefilter_reason"] = assessment.reason
            if assessment.eligible and status in {"accept", "review"}:
                stats["prefilter_viable"] += 1
            elif status in {"accept", "review"}:
                stats["prefilter_rejected"] += 1
            normalized.append(job)
        return normalized, stats

    @staticmethod
    def sample_jobs():
        def make(job_id, title, company):
            return {
                "job_id": job_id, "job_title": title, "employer_name": company,
                "employer_website": "", "job_apply_link": f"https://example.com/{job_id or 'x'}",
                "canonical_source_url": f"https://example.com/{job_id or 'x'}",
                "job_description": (
                    "We are hiring for this role to support continued growth. You will own "
                    "delivery end to end and partner closely with product and design."
                ),
                "job_location": "Remote - United States", "job_country": "US",
                "_acquisition_source": "himalayas",
            }

        return [
            make("h:1", "Senior Software Engineer", "Northwind Systems"),
            make("h:2", "VP of Engineering", "Contoso Labs"),
            make("", "Data Engineer", "Fabrikam Studio"),
            make("h:4", "Underwater Basket Weaver", "Litware Inc"),
            make("h:5", "Account Executive", "Northwind Systems"),
            make("h:6", "Senior Software Engineer", "Northwind Systems"),
        ]

    def test_counters_match_the_production_loop_exactly(self):
        from retrieval_measurement.identity import ReadOnlySeenSnapshot

        jobs = self.sample_jobs()
        snapshot = ReadOnlySeenSnapshot.empty()
        expected_kept, expected = self.production_loop(jobs, snapshot)
        kept, _records, counters = attribute_discards(jobs, snapshot)

        bucket = counters["himalayas"]
        self.assertEqual(len(kept), len(expected_kept))
        self.assertEqual(bucket["missing_job_id"], expected["missing_job_id_skipped"])
        self.assertEqual(bucket["previously_seen"], expected["previously_seen_removed"])
        self.assertEqual(bucket["excluded_by_seniority"], expected["excluded_by_seniority"])
        for name in ("role_accept", "role_review", "role_reject",
                     "prefilter_viable", "prefilter_rejected"):
            self.assertEqual(bucket[name], expected[name], name)

    def test_kept_records_carry_the_same_annotations(self):
        from retrieval_measurement.identity import ReadOnlySeenSnapshot

        jobs = self.sample_jobs()
        expected_kept, _stats = self.production_loop(jobs, ReadOnlySeenSnapshot.empty())
        kept, _records, _counters = attribute_discards(jobs, ReadOnlySeenSnapshot.empty())
        self.assertEqual(
            [(item["job_id"], item["_role_relevance_status"], item["_prefilter_viable"])
             for item in kept],
            [(item["job_id"], item["_role_relevance_status"], item["_prefilter_viable"])
             for item in expected_kept],
        )

    def test_dedupe_ordering_and_winner_selection_are_unchanged(self):
        jobs = self.sample_jobs()
        direct_selected, direct_dupes = _dedupe([dict(item) for item in jobs])
        wrapped_selected, wrapped_dupes = production_equivalent_uniqueness(jobs)
        self.assertEqual(direct_dupes, wrapped_dupes)
        self.assertEqual(
            [item.get("job_id") for item in direct_selected],
            [item.get("job_id") for item in wrapped_selected],
        )


if __name__ == "__main__":
    unittest.main()
