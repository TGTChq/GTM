"""Incident 2 regression tests: JSearch base pagination.

Production (config-C, 2026-07-30) repeatedly logged ``Fetched 10 raw postings``
for the initial JSearch request of each role -- one page per approved role -- so
base acquisition never retrieved up to ~30 postings per title. The intended
config-C behavior fetches up to ``NUM_PAGES`` (=3) base pages per role, one unit
at a time, stopping early on an empty / partial / duplicate page, and recording
every consumed page so adaptive deepening and FINAL_PASS top-up never re-request
a page already fetched.

``NUM_PAGES`` is the existing base-pages-per-role knob (committed default 1;
config-C value 3; see ``test_remote_volume_optimization.ThreePageProductionBudgetTests``).
No behavior changes unless it is raised above 1.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import jsearch_scraper
from jsearch_scraper import JSearchFetchResult, _next_topup_query_spec
from pipeline_state import SeenJobsRegistry
from role_catalog import DEFAULT_SEARCH_ROLES

ROLE = "Accountant"


def _jobs(page: int, count: int, *, id_prefix: str | None = None):
    prefix = id_prefix if id_prefix is not None else f"p{page}"
    return [
        {
            "job_id": f"{prefix}-{index}",
            "job_title": ROLE,
            "job_description": f"Remote {ROLE} role in the United States.",
            "job_country": "US",
            "job_is_remote": True,
            "employer_name": f"Acme {prefix}-{index}",
        }
        for index in range(count)
    ]


class _BasePaginationHarness(unittest.TestCase):
    def _run(self, page_map, *, num_pages=3, roles=(ROLE,), budget=450,
             stop_on_low_quota=False, min_remaining=0, quota_by_page=None):
        """page_map: dict page_number -> list[jobs] | Exception | (jobs, quota).

        Returns (result, calls) where calls is the ordered [(role, page), ...].
        """
        calls: list[tuple[str, int]] = []

        def fake_fetch(role, *, page=1, num_pages=None, **kwargs):
            calls.append((role, page))
            value = page_map.get(page, [])
            if isinstance(value, Exception):
                raise value
            quota = (quota_by_page or {}).get(page)
            if quota is not None:
                return JSearchFetchResult(jobs=list(value), duration_seconds=0.0, quota=quota)
            return list(value)

        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "OUTPUT_DIR", temp),
                patch.object(config, "NUM_PAGES", num_pages),
                patch.object(config, "SEARCH_DELAY_SECONDS", 0),
                patch.object(config, "PRODUCTION", False),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", budget),
                patch.object(config, "JSEARCH_STOP_ON_LOW_QUOTA", stop_on_low_quota),
                patch.object(config, "JSEARCH_MIN_REMAINING_REQUESTS", min_remaining),
                patch.object(jsearch_scraper, "fetch_jobs_for_role", side_effect=fake_fetch),
            ):
                result = jsearch_scraper.run_daily_scrape(
                    SeenJobsRegistry(path=str(Path(temp) / "seen.json")),
                    search_roles=list(roles),
                )
        return result, calls

    def _pages(self, result):
        return result.stats["query_metrics"][ROLE]["pages"]


class BasePaginationTests(_BasePaginationHarness):
    # 1 -----------------------------------------------------------------
    def test_thirty_distinct_postings_fetch_pages_1_2_3(self):
        result, calls = self._run({1: _jobs(1, 10), 2: _jobs(2, 10), 3: _jobs(3, 10)})
        self.assertEqual(calls, [(ROLE, 1), (ROLE, 2), (ROLE, 3)])
        self.assertEqual(result.stats["raw_role_counts"][ROLE], 30)
        self.assertEqual([p["page"] for p in self._pages(result)], [1, 2, 3])
        self.assertEqual([p["last_page"] for p in self._pages(result)], [1, 2, 3])

    # 2 -----------------------------------------------------------------
    def test_seventeen_postings_full_then_partial_then_stops(self):
        result, calls = self._run({1: _jobs(1, 10), 2: _jobs(2, 7), 3: _jobs(3, 10)})
        self.assertEqual(calls, [(ROLE, 1), (ROLE, 2)])  # page 3 never requested
        self.assertEqual(result.stats["raw_role_counts"][ROLE], 17)
        self.assertEqual(
            result.stats["query_metrics"][ROLE]["base_stop_reason"],
            "partial_page_provider_exhausted",
        )

    # 3 -----------------------------------------------------------------
    def test_empty_page_two_stops_base_pagination(self):
        result, calls = self._run({1: _jobs(1, 10), 2: [], 3: _jobs(3, 10)})
        self.assertEqual(calls, [(ROLE, 1), (ROLE, 2)])
        self.assertEqual(result.stats["query_metrics"][ROLE]["base_stop_reason"], "empty_page")

    # 4 -----------------------------------------------------------------
    def test_repeated_page_contents_do_not_inflate_and_stop(self):
        page1 = _jobs(1, 10, id_prefix="dup")
        page2 = _jobs(2, 10, id_prefix="dup")  # identical job_ids
        result, calls = self._run({1: page1, 2: page2, 3: _jobs(3, 10)})
        self.assertEqual(calls, [(ROLE, 1), (ROLE, 2)])  # stopped, page 3 untouched
        self.assertEqual(
            result.stats["query_metrics"][ROLE]["base_stop_reason"], "duplicate_page"
        )
        # In-run job-id dedupe means the repeat added no distinct selected jobs.
        self.assertEqual(result.total_jobs, 10)

    # 5 -----------------------------------------------------------------
    def test_global_unit_budget_enforced_across_roles(self):
        # 2 roles * 3 base pages = 6 estimated units > budget 5 -> pre-flight guard.
        with self.assertRaises(ValueError):
            self._run({1: _jobs(1, 10)}, roles=("Accountant", "Tax Accountant"), budget=5)

    # 6 -----------------------------------------------------------------
    def test_quota_reserve_guard_stops_base_pagination(self):
        result, calls = self._run(
            {1: _jobs(1, 10), 2: _jobs(2, 10), 3: _jobs(3, 10)},
            stop_on_low_quota=True, min_remaining=5,
            quota_by_page={1: {"remaining": 3, "limit": 100}},
        )
        self.assertEqual(calls, [(ROLE, 1)])
        self.assertEqual(result.stats["query_metrics"][ROLE]["base_stop_reason"], "quota_reserve")
        self.assertTrue(result.stats["query_plan_truncated"])

    # 7 -----------------------------------------------------------------
    def test_base_pagination_is_bounded_by_num_pages(self):
        # Every page full: the loop still terminates after exactly NUM_PAGES
        # requests -- it cannot run unbounded. (The run-level wall-clock guard
        # lives in final_pass_topup.FINAL_PASS_MAX_RUNTIME_SECONDS.)
        result, calls = self._run(
            {1: _jobs(1, 10), 2: _jobs(2, 10), 3: _jobs(3, 10), 4: _jobs(4, 10)},
            num_pages=3,
        )
        self.assertEqual(len([c for c in calls if c[0] == ROLE]), 3)

    # 8 -----------------------------------------------------------------
    def test_adaptive_deepening_disabled_at_three_pages(self):
        # With NUM_PAGES>1 adaptive deepening is redundant and disabled, so it
        # cannot re-request base pages 1-3.
        self.assertFalse(
            jsearch_scraper._adaptive_deepening_is_enabled(
                search_roles=None, max_queries=None, effective_max=0,
                planned_roles=list(DEFAULT_SEARCH_ROLES), num_pages=3,
            )
        )
        result, calls = self._run({1: _jobs(1, 10), 2: _jobs(2, 10), 3: _jobs(3, 10)})
        self.assertFalse(result.stats["adaptive_deepening_enabled"])
        # No (role, page) pair is requested twice.
        self.assertEqual(len(calls), len(set(calls)))

    # 9 & 10 ------------------------------------------------------------
    def test_topup_starts_from_page_four_when_base_consumed_1_to_3(self):
        metric = {
            "canonical_role": ROLE,
            "pages": [
                {"page": 1, "num_pages": 1, "last_page": 1, "query_variant": "base"},
                {"page": 2, "num_pages": 1, "last_page": 2, "query_variant": "base"},
                {"page": 3, "num_pages": 1, "last_page": 3, "query_variant": "base"},
            ],
        }
        with patch.object(config, "JSEARCH_TOPUP_MAX_PAGE", 6), patch.object(
            config, "JSEARCH_TOPUP_PAGES_PER_QUERY", 3
        ):
            spec = _next_topup_query_spec(metric, unit_budget_remaining=3)
        self.assertIsNotNone(spec)
        self.assertEqual(spec["page"], 4)  # never re-requests pages 1-3
        self.assertGreaterEqual(spec["page"], 4)

    # 11 ----------------------------------------------------------------
    def test_transient_failed_page_is_not_marked_consumed(self):
        result, calls = self._run({1: _jobs(1, 10), 2: ValueError("boom"), 3: _jobs(3, 10)})
        self.assertEqual(calls, [(ROLE, 1), (ROLE, 2)])
        pages = self._pages(result)
        # Only page 1 is recorded as consumed; the failed page 2 stays retryable.
        self.assertEqual([p["page"] for p in pages], [1])
        self.assertEqual(result.stats["query_metrics"][ROLE]["base_stop_reason"], "provider_error")

    # 12 ----------------------------------------------------------------
    def test_default_one_page_base_behavior_preserved(self):
        result, calls = self._run({1: _jobs(1, 10), 2: _jobs(2, 10)}, num_pages=1)
        self.assertEqual(calls, [(ROLE, 1)])  # exactly one base request per role
        self.assertEqual(result.stats["raw_role_counts"][ROLE], 10)
        self.assertEqual([p["page"] for p in self._pages(result)], [1])

    # 13 ----------------------------------------------------------------
    def test_pagination_and_stop_semantics_interaction(self):
        # After base consumed pages 1-3, remaining pages stay available to top-up
        # regardless of how many review rows exist -- pagination is not gated by
        # the review-row surface count (which never satisfies the FINAL_PASS
        # target; see test_final_pass_stop_semantics).
        metric = {
            "canonical_role": ROLE,
            "pages": [
                {"page": 1, "num_pages": 1, "last_page": 1, "query_variant": "base"},
                {"page": 2, "num_pages": 1, "last_page": 2, "query_variant": "base"},
                {"page": 3, "num_pages": 1, "last_page": 3, "query_variant": "base"},
            ],
        }
        with patch.object(config, "JSEARCH_TOPUP_MAX_PAGE", 6):
            spec = _next_topup_query_spec(metric, unit_budget_remaining=3)
        self.assertIsNotNone(spec)
        self.assertEqual(spec["page"], 4)


if __name__ == "__main__":
    unittest.main()
