"""Phase 13 section 4: Adzuna production-scale hardening.

Portfolio construction from the role catalog, plus the corrected semantics:
- unknown contract_time must NOT become FULLTIME;
- a 200 with zero results is a successful execution, not a failure;
- queries_attempted reports the actual attempted count, not the list length;
- bounded retry on transient failures;
- auth / quota / rate-limit / zero-result-success / transient stay distinct.
"""
from __future__ import annotations

import json
import unittest

from adzuna_client import (
    AdzunaAdapter, AdzunaSettings, AdzunaQuerySpec,
    build_query_portfolio, normalize_adzuna_job,
)
from free_job_sources import FetchPayload
from role_catalog import DEFAULT_ACQUISITION_ROLES, get_function_bucket


def _resp(status, body):
    return FetchPayload(status, "https://api.adzuna.com/x", text=body)


def _page(results):
    return _resp(200, json.dumps({"results": results}))


def _row(job_id, contract_time=None, contract_type=None, company="Acme Co"):
    row = {"id": job_id, "title": "Revenue Operations Manager",
           "company": {"display_name": company}, "location": {"display_name": "United States"},
           "description": "Own the revenue systems stack.", "redirect_url": f"https://x/{job_id}",
           "created": "2026-07-20T09:15:00Z", "category": {"label": "IT Jobs"}}
    if contract_time is not None:
        row["contract_time"] = contract_time
    if contract_type is not None:
        row["contract_type"] = contract_type
    return row


class ContractTimeTests(unittest.TestCase):
    def test_unknown_contract_time_is_not_labeled_fulltime(self):
        job = normalize_adzuna_job(_row("1"))
        self.assertEqual(job["job_employment_type"], "")

    def test_explicit_full_time_is_labeled_fulltime(self):
        job = normalize_adzuna_job(_row("1", contract_time="full_time"))
        self.assertEqual(job["job_employment_type"], "FULLTIME")

    def test_part_time_and_contract_preserved(self):
        self.assertEqual(normalize_adzuna_job(_row("1", contract_time="part_time"))["job_employment_type"], "PARTTIME")
        self.assertEqual(normalize_adzuna_job(_row("2", contract_type="contract"))["job_employment_type"], "CONTRACTOR")


class SuccessSemanticsTests(unittest.TestCase):
    def _adapter(self, **kw):
        return AdzunaAdapter(
            settings=AdzunaSettings(enabled=True, app_id="x", app_key="y", max_pages_per_query=1),
            queries=["revenue operations"], **kw,
        )

    def test_zero_result_200_is_success_not_failure(self):
        result = self._adapter().fetch(lambda *a, **k: _page([]))
        self.assertTrue(result.success)
        self.assertEqual(len(result.jobs), 0)
        self.assertFalse(result.metadata["auth_failed"])
        self.assertFalse(result.metadata["quota_exhausted"])

    def test_queries_attempted_reports_actual_not_list_length(self):
        # Portfolio of 3, but quota trips on the first -> attempted should be 1.
        adapter = AdzunaAdapter(
            settings=AdzunaSettings(enabled=True, app_id="x", app_key="y"),
            queries=["a", "b", "c"],
        )
        result = adapter.fetch(lambda *a, **k: _resp(429, "monthly quota exceeded"))
        self.assertEqual(result.metadata["queries_attempted"], 1)
        self.assertEqual(result.metadata["queries_planned"], 3)

    def test_auth_quota_rate_transient_are_distinct(self):
        self.assertTrue(self._adapter().fetch(lambda *a, **k: _resp(403, "forbidden")).metadata["auth_failed"])
        self.assertTrue(self._adapter().fetch(lambda *a, **k: _resp(429, "monthly quota exceeded")).metadata["quota_exhausted"])
        rate = self._adapter().fetch(lambda *a, **k: _resp(429, "slow down"))
        self.assertTrue(rate.metadata["rate_limited_queries"])
        self.assertFalse(rate.metadata["quota_exhausted"])
        transient = self._adapter().fetch(lambda *a, **k: _resp(503, "server error"))
        self.assertTrue(transient.metadata["transient_error_queries"])

    def test_bounded_retry_on_transient_then_recovers(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return _resp(503, "server error")
            return _page([_row("1")])

        result = self._adapter(max_transient_retries=2).fetch(flaky)
        self.assertGreaterEqual(calls["n"], 2)          # retried
        self.assertEqual(len(result.jobs), 1)           # recovered
        self.assertTrue(result.success)


class PortfolioTests(unittest.TestCase):
    def test_portfolio_derived_from_role_catalog(self):
        portfolio = build_query_portfolio(
            DEFAULT_ACQUISITION_ROLES, remote_variants=("", "remote"),
            freshness_windows=(30,), max_pages=2, max_queries=8,
            role_family_of=get_function_bucket,
        )
        self.assertEqual(len(portfolio), 8)
        self.assertTrue(all(isinstance(s, AdzunaQuerySpec) for s in portfolio))
        # Distinct roles covered before remote variants deepen.
        self.assertTrue(any(s.role_family for s in portfolio))
        self.assertTrue(all(s.max_pages == 2 for s in portfolio))

    def test_portfolio_respects_max_queries_cap(self):
        portfolio = build_query_portfolio(
            DEFAULT_ACQUISITION_ROLES, remote_variants=("", "remote"),
            freshness_windows=(15, 30), max_pages=1, max_queries=5,
        )
        self.assertEqual(len(portfolio), 5)

    def test_marginal_yield_stop_after_two_low_yield_queries(self):
        # Every query returns the SAME single company -> new_companies drops to
        # 0 after the first, so after two consecutive low-yield queries it stops.
        portfolio = [
            AdzunaQuerySpec("f", f"role{i}", f"role{i}", "", 30, 1) for i in range(6)
        ]
        adapter = AdzunaAdapter(
            settings=AdzunaSettings(enabled=True, app_id="x", app_key="y"),
            portfolio=portfolio, marginal_min_new_companies=1,
        )
        result = adapter.fetch(lambda *a, **k: _page([_row("same", company="OneCo")]))
        self.assertIn("marginal_yield_stop", result.errors)
        self.assertLess(result.metadata["queries_attempted"], 6)


if __name__ == "__main__":
    unittest.main()
