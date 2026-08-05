"""Phase C: the JSearch live-transport fix.

Proves the CLI JSearch lane composes the REAL transport in live mode (not the
inert JSearchTransport(inner=None)), preserves the budget seam, and keeps offline
zero-network, without making any live request."""

from __future__ import annotations

import tempfile
import unittest

import requests

from retrieval_measurement.instrument import RequestBudget
from orchestrator.lanes import LaneManager
from orchestrator.adapters_real import (
    build_jsearch_transport,
    real_jsearch_runner,
    _live_jsearch_request,
)


def _raise_net():
    orig = requests.request
    requests.request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network in offline mode"))
    return lambda: setattr(requests, "request", orig)


class JSearchFixTests(unittest.TestCase):
    def test_offline_transport_is_replay_zero_network(self):
        t = build_jsearch_transport(live=False)
        self.assertIsNone(t.inner)                      # replay, never the network
        restore = _raise_net()
        try:
            runner = real_jsearch_runner(output_dir=tempfile.mkdtemp(), max_queries=0,
                                         registry=_NWR(), live=False)
            res = runner(LaneManager(budget=RequestBudget(limit=10)))
        finally:
            restore()
        self.assertEqual(res.lane, "jsearch")
        self.assertEqual(len(res.jobs), 0)              # max_queries=0 -> no queries

    def test_live_mode_composes_real_transport_and_budget_seam(self):
        import orchestrator.adapters_real as ar
        budget = RequestBudget(limit=10, lane_limits={"jsearch": 10})
        t = build_jsearch_transport(live=True, budget=budget)
        self.assertIsNotNone(t.inner)                   # real transport, not inert
        # Observe the real live-request function (which IS request_with_retry) and
        # prove the transport routes to it AND reserves against the budget first.
        orig = ar._live_jsearch_request
        seen = {"n": 0, "url": ""}

        class _R:
            status_code = 200
            text = '{"status":"OK","data":[]}'
            def json(self): return {"status": "OK", "data": []}

        ar._live_jsearch_request = lambda m, u, **k: (seen.update(n=seen["n"] + 1, url=u) or _R())
        try:
            t("GET", "https://jsearch.p.rapidapi.com/search", params={"query": "x", "page": 1})
        finally:
            ar._live_jsearch_request = orig
        self.assertEqual(seen["n"], 1)                  # routed through the live transport
        self.assertIn("rapidapi", seen["url"])
        self.assertEqual(budget.count, 1)               # reserved against jsearch lane
        self.assertEqual(budget.per_lane.get("jsearch"), 1)

    def test_injected_transport_returns_and_accounts_records(self):
        from retrieval_measurement.instrument import JSearchTransport
        recorded = {"engineer|page=1": {"status": "OK",
                    "data": [{"job_id": "A"}, {"job_id": "B"}, {"job_id": "C"}]}}
        t = JSearchTransport(recorded=recorded)
        resp = t("GET", "https://jsearch/search", params={"query": "engineer", "page": 1})
        data = resp.json().get("data")
        self.assertEqual(len(data), 3)                  # nonzero JSearch records returned
        self.assertEqual(len(t.requests), 1)            # request accounted
        # accounting reconciles: one call -> one recorded request
        self.assertEqual(len(t.requests), 1)

    def test_request_and_record_counts_reconcile(self):
        from retrieval_measurement.instrument import JSearchTransport
        rec = {f"q|page={p}": {"status": "OK", "data": [{"job_id": f"{p}"}]} for p in (1, 2, 3)}
        t = JSearchTransport(recorded=rec)
        total = 0
        for p in (1, 2, 3):
            total += len(t("GET", "u", params={"query": "q", "page": p}).json()["data"])
        self.assertEqual(len(t.requests), 3)            # 3 calls -> 3 requests
        self.assertEqual(total, 3)                      # 3 records

    def test_quota_or_preflight_failure_stops_cleanly(self):
        # No RAPIDAPI_KEY -> run_daily_scrape.validate_preflight fails; the lane
        # must return failed, never crash, and make no network call.
        restore = _raise_net()
        try:
            runner = real_jsearch_runner(output_dir=tempfile.mkdtemp(), max_queries=5,
                                         registry=_NWR(), live=True)
            res = runner(LaneManager(budget=RequestBudget(limit=500, lane_limits={"jsearch": 400})))
        finally:
            restore()
        self.assertIn(res.status, ("failed", "partial"))
        self.assertEqual(len(res.jobs), 0)
        self.assertTrue(res.errors)                     # captured, not raised


def _NWR():
    from retrieval_measurement.identity import NonWritingRegistry
    return NonWritingRegistry(None)


if __name__ == "__main__":
    unittest.main()
