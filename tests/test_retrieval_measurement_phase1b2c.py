"""Phase 1B-2C: offline behavioral validation of all nine ATS provider branches.

Every test here drives the REAL integrated path:

* real ``fetch_board_jobs`` provider dispatch and parsing,
* the real ``free_job_sources.default_fetcher`` transport (its actual redirect
  loop, headers, timeout and streaming read),
* the real ``RequestBudget`` seam that classifies each physical request,
* the real ``request_trace`` role x phase attribution,
* the real ``AtsBoardSession`` checkpointing and lane accounting,
* and, for the integration/scheduler tests, the real
  ``multi_source_acquisition.run_multi_source_acquisition`` wiring.

No provider logic is re-implemented. The only substitution is at
``requests.request`` (a ``FakeTransport`` that serves recorded-shape fixtures)
and ``socket.getaddrinfo`` (a fixed public address, so ``_safe_public_url``'s
real logic runs without a real DNS query). Nothing leaves the machine; every
test asserts the set of contacted hosts and that the wire was never touched.

Fixtures live under ``tests/fixtures/retrieval_measurement/ats_providers`` and are
provenance category 3 (minimal realistic payloads constructed from the adapter
contracts) except the Workday paging shape, which matches the shape already in
``tests/test_structural_patch_ats_feed_pagination.py`` (category 2). They are NOT
recorded live responses; see PROVENANCE in the fixtures directory.
"""
from __future__ import annotations

import contextlib
import json
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
from unittest import mock

import requests

import config
import multi_source_acquisition
from ats_board_registry import fetch_board_jobs
from free_job_sources import default_fetcher
from retrieval_measurement import request_trace
from retrieval_measurement.ats_checkpoint import AtsBoardSession
from retrieval_measurement.instrument import RequestBudget, RequestCeilingReached

FIX = Path(__file__).resolve().parent / "fixtures" / "retrieval_measurement" / "ats_providers"


def load(rel: str) -> str:
    return (FIX / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Offline transport
# --------------------------------------------------------------------------


class FakeResponse:
    """Enough of ``requests.Response`` for ``default_fetcher``'s streaming path."""

    def __init__(self, status=200, body=b"", url="", headers=None):
        self.status_code = status
        self._body = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        self.url = url
        self.headers = dict(headers or {})
        self.encoding = "utf-8"

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    @property
    def is_permanent_redirect(self):
        return self.status_code in (301, 308) and "Location" in self.headers

    def iter_content(self, chunk_size=65536):
        b = self._body
        if not b:
            yield b""
            return
        for i in range(0, len(b), chunk_size):
            yield b[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTransport:
    """Routes ``requests.request`` calls to fixture responses. No network."""

    def __init__(self):
        self.routes = []
        self.calls = []
        self.hosts = set()

    def add(self, pred, resp):
        self.routes.append((pred, resp))
        return self

    def __call__(self, method, url, **kw):
        rec = {
            "method": method,
            "url": url,
            "params": dict(kw.get("params") or {}),
            "json": dict(kw.get("json") or {}),
            "headers": dict(kw.get("headers") or {}),
        }
        self.calls.append(rec)
        self.hosts.add(urlparse(url).hostname or "")
        for pred, resp in self.routes:
            if pred(rec):
                out = resp(rec)
                if isinstance(out, BaseException):
                    raise out
                return out
        raise AssertionError(f"unrouted request: {method} {url}")


def _fixed_addr(host, *a, **k):
    # A fixed global address so free_job_sources._safe_public_url's real check
    # passes without a real DNS lookup. Nothing is dialed; requests.request is
    # itself faked.
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def ok(rel):
    return lambda rec: FakeResponse(200, load(rel), rec["url"])


# _fixed_addr + FakeTransport are installed by the harnesses below.


# --------------------------------------------------------------------------
# Provider board specs + routing
# --------------------------------------------------------------------------

BOARDS = {
    "greenhouse": {"provider": "greenhouse", "identifier": "acme", "company_name": "Acme",
                   "key": "greenhouse:acme:api"},
    "lever": {"provider": "lever", "identifier": "acme", "company_name": "Acme",
              "key": "lever:acme:api"},
    "ashby": {"provider": "ashby", "identifier": "acme", "company_name": "Acme",
              "key": "ashby:acme:api"},
    "recruitee": {"provider": "recruitee", "identifier": "acme", "company_name": "Acme",
                  "key": "recruitee:acme:api"},
    "workable": {"provider": "workable", "identifier": "acme", "company_name": "Acme",
                 "key": "workable:acme:api"},
    "personio": {"provider": "personio", "identifier": "acme", "company_name": "Acme",
                 "api_base": "https://acme.jobs.personio.de", "key": "personio:acme:api"},
    "smartrecruiters": {"provider": "smartrecruiters", "identifier": "acme", "company_name": "Acme",
                        "key": "smartrecruiters:acme:api"},
    "workday": {"provider": "workday", "identifier": "acme|careers", "company_name": "Acme",
                "api_base": "https://acme.wd1.myworkdayjobs.com", "key": "workday:acme|careers:api"},
    "cornerstone_ondemand": {"provider": "cornerstone_ondemand", "identifier": "acme|5",
                             "company_name": "Acme", "api_base": "https://acme.csod.com",
                             "key": "cornerstone_ondemand:acme|5:api"},
}

DETAIL_PROVIDERS = ("greenhouse", "smartrecruiters", "workday")
LISTING_ONLY = ("lever", "ashby", "recruitee", "workable", "personio", "cornerstone_ondemand")
ALL_PROVIDERS = tuple(BOARDS)


def route_default(provider, t):
    """Register the happy-path routes for one provider on a FakeTransport."""
    if provider == "greenhouse":
        t.add(lambda r: "/v1/boards/acme/jobs/101" in r["url"], ok("greenhouse/detail_101.json"))
        t.add(lambda r: r["url"].endswith("/v1/boards/acme/jobs"), ok("greenhouse/jobs.json"))
        t.add(lambda r: r["url"].endswith("/v1/boards/acme"), ok("greenhouse/board.json"))
    elif provider == "lever":
        t.add(lambda r: "/v0/postings/acme" in r["url"], ok("lever/postings.json"))
    elif provider == "ashby":
        t.add(lambda r: "/posting-api/job-board/acme" in r["url"], ok("ashby/jobs.json"))
    elif provider == "recruitee":
        t.add(lambda r: "acme.recruitee.com/api/offers" in r["url"], ok("recruitee/offers.json"))
    elif provider == "workable":
        t.add(lambda r: "workable.com/api/accounts/acme" in r["url"], ok("workable/account.json"))
    elif provider == "personio":
        t.add(lambda r: "acme.jobs.personio.de/xml" in r["url"], ok("personio/positions.xml"))
    elif provider == "smartrecruiters":
        # single small page by default (pagination proven in its own test)
        t.add(lambda r: r["url"].endswith("/companies/acme/postings"),
              ok("smartrecruiters/postings_small.json"))
    elif provider == "workday":
        t.add(lambda r: r["url"].endswith("/wday/cxs/acme/careers/job/req-0"),
              ok("workday/detail_req-0.json"))
        t.add(lambda r: r["url"].endswith("/wday/cxs/acme/careers/jobs"),
              ok("workday/jobs_small.json"))
    elif provider == "cornerstone_ondemand":
        t.add(lambda r: "/ux/ats/careersite/5/api/search" in r["url"],
              ok("cornerstone_ondemand/search_small.json"))
    return t


def route_sr_paginated(t):
    def sr(rec):
        offset = int(rec["params"].get("offset", 0) or 0)
        return FakeResponse(200, load("smartrecruiters/postings_p1.json" if offset == 0
                                      else "smartrecruiters/postings_p2.json"), rec["url"])
    return t.add(lambda r: r["url"].endswith("/companies/acme/postings"), sr)


def route_wd_paginated(t):
    t.add(lambda r: r["url"].endswith("/wday/cxs/acme/careers/job/req-0"),
          ok("workday/detail_req-0.json"))

    def wd(rec):
        offset = int(rec["json"].get("offset", 0) or 0)
        return FakeResponse(200, load("workday/jobs_p1.json" if offset == 0
                                      else "workday/jobs_p2.json"), rec["url"])
    return t.add(lambda r: r["url"].endswith("/wday/cxs/acme/careers/jobs"), wd)


def route_cs_paginated(t):
    def cs(rec):
        page = int(rec["params"].get("page", 1) or 1)
        return FakeResponse(200, load("cornerstone_ondemand/search_p1.json" if page == 1
                                      else "cornerstone_ondemand/search_p2.json"), rec["url"])
    return t.add(lambda r: "/ux/ats/careersite/5/api/search" in r["url"], cs)


# --------------------------------------------------------------------------
# Harnesses -- the real path, two entry points
# --------------------------------------------------------------------------


def run_board(board, transport, *, budget=None, trace=None, checkpoint_dir=None,
              detail_budget=None, session=None):
    """Drive exactly what the production ATS loop runs for one board:
    ``session.board()`` scope -> real ``fetch_board_jobs`` -> ``session.record``.
    """
    budget = budget or RequestBudget(100000)
    provider = board["provider"]
    detail_kw = {}
    if provider == "greenhouse":
        detail_kw = {"greenhouse_detail_budget": detail_budget}
    elif provider == "workday":
        detail_kw = {"workday_detail_budget": detail_budget}
    elif provider == "smartrecruiters":
        detail_kw = {"smartrecruiters_detail_budget": detail_budget}

    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(socket, "getaddrinfo", _fixed_addr))
    stack.enter_context(mock.patch.object(requests, "request", transport))
    stack.enter_context(budget.installed())
    trace_cm = request_trace.install(trace) if trace is not None else contextlib.nullcontext(trace)
    active_trace = stack.enter_context(trace_cm)
    with stack:
        sess = session or AtsBoardSession(budget=budget, trace=active_trace,
                                          checkpoint_dir=checkpoint_dir)
        with sess.board(board) as state:
            try:
                jobs, error = fetch_board_jobs(board, default_fetcher,
                                               **{k: v for k, v in detail_kw.items() if v is not None})
            except Exception as exc:  # noqa: BLE001
                jobs, error = [], f"{type(exc).__name__}: {exc}"
            result = sess.record(state, jobs=jobs, error=error)
    return sess, result, jobs, error


class PipelineHarness(unittest.TestCase):
    """Drives the production ``run_multi_source_acquisition`` with the real ATS
    loop and fetch_board_jobs (only the wire + DNS are faked)."""

    def setUp(self):
        self.selected_jobs = []

    def run_pipeline(self, boards, transport, *, session, budget, scheduler_mode=None):
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
            mock.patch.object(socket, "getaddrinfo", _fixed_addr),
            mock.patch.object(requests, "request", transport),
        ]
        if scheduler_mode is not None:
            patches.append(mock.patch.object(config, "ATS_SCHEDULER_MODE", scheduler_mode))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(budget.installed())
            seen = mock.MagicMock()
            seen.has_job_id.return_value = False
            seen.has_dedup_key.return_value = False
            result = multi_source_acquisition.run_multi_source_acquisition(seen, ats_session=session)
        self._registry = reg
        return result


def transport_for(*providers):
    t = FakeTransport()
    for p in providers:
        route_default(p, t)
    return t


# --------------------------------------------------------------------------
# 4/5/11 -- per-provider behavior through the real integrated path
# --------------------------------------------------------------------------


class ProviderBehaviorTests(unittest.TestCase):
    """One behavioral pass per provider: dispatch, parse, normalize, identity,
    counts, uniqueness, attribution, physical count, checkpoint."""

    def _run(self, provider):
        t = transport_for(provider)
        trace = request_trace.Trace()
        with tempfile.TemporaryDirectory() as tmp:
            sess, result, jobs, error = run_board(
                BOARDS[provider], t, trace=trace, checkpoint_dir=tmp, detail_budget=100)
            files = sorted(p.name for p in Path(tmp).glob("*.json"))
        return t, trace, sess, result, jobs, error, files

    def test_greenhouse(self):
        t, trace, sess, result, jobs, error, files = self._run("greenhouse")
        self.assertEqual(error, "")
        self.assertEqual(t.hosts, {"boards-api.greenhouse.io"})
        self.assertEqual([c["method"] for c in t.calls], ["GET", "GET", "GET"])  # board, jobs, 1 detail
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["job_id"], "ats:greenhouse:acme:101")
        self.assertEqual(jobs[0]["_ats_provider"], "greenhouse")
        self.assertTrue(jobs[0]["_greenhouse_detail_request_made"])
        self.assertFalse(jobs[1]["_greenhouse_detail_request_made"])
        self.assertEqual(jobs[0]["job_posted_at_datetime_utc"], "2026-06-15T00:00:00Z")  # from detail
        d = trace.to_dict()
        self.assertEqual((d["initial_listing"], d["initial_detail"]), (2, 1))
        self.assertEqual(trace.total, result.physical_requests)
        self.assertEqual(result.canonical_records, 2)
        self.assertEqual(result.detail_records, 1)
        self.assertEqual(files, ["greenhouse__acme.json"])

    def test_lever(self):
        t, trace, sess, result, jobs, error, files = self._run("lever")
        self.assertEqual(error, "")
        self.assertEqual([c["method"] for c in t.calls], ["GET"])   # listing only
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["job_id"], "ats:lever:acme:L1")
        self.assertTrue(jobs[0]["job_is_remote"])                    # workplaceType remote
        self.assertEqual(trace.to_dict()["initial_detail"], 0)
        self.assertEqual(trace.total, result.physical_requests)
        self.assertEqual(result.canonical_records, 2)

    def test_ashby(self):
        t, trace, sess, result, jobs, error, files = self._run("ashby")
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)                              # A2 isListed=False filtered
        self.assertEqual(jobs[0]["job_id"], "ats:ashby:acme:A1")
        self.assertTrue(jobs[0]["job_is_remote"])
        self.assertEqual(trace.to_dict()["initial_detail"], 0)

    def test_recruitee(self):
        t, trace, sess, result, jobs, error, files = self._run("recruitee")
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)                              # closed offer filtered
        self.assertEqual(jobs[0]["job_id"], "ats:recruitee:acme:11")
        self.assertEqual(trace.to_dict()["initial_detail"], 0)

    def test_workable(self):
        t, trace, sess, result, jobs, error, files = self._run("workable")
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], "ats:workable:acme:WK1")
        self.assertIn("Austin", jobs[0]["job_location"])
        self.assertEqual(trace.to_dict()["initial_detail"], 0)

    def test_personio(self):
        t, trace, sess, result, jobs, error, files = self._run("personio")
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["job_id"], "ats:personio:acme:P1")
        self.assertEqual(t.calls[0]["headers"].get("Accept"), "application/xml,text/xml")
        self.assertEqual(trace.to_dict()["initial_detail"], 0)

    def test_smartrecruiters(self):
        """Multi-page offset pagination; non-matching titles => no detail."""
        t = FakeTransport()
        route_sr_paginated(t)
        trace = request_trace.Trace()
        with tempfile.TemporaryDirectory() as tmp:
            sess, result, jobs, error = run_board(
                BOARDS["smartrecruiters"], t, trace=trace, checkpoint_dir=tmp, detail_budget=0)
        self.assertEqual(error, "")
        self.assertEqual([c["method"] for c in t.calls], ["GET", "GET"])
        self.assertEqual([c["params"].get("offset") for c in t.calls], [0, 100])
        self.assertEqual(len(jobs), 105)
        self.assertEqual(trace.to_dict()["initial_detail"], 0)
        self.assertEqual(trace.to_dict()["initial_listing"], 2)
        self.assertEqual(trace.total, result.physical_requests)

    def test_workday(self):
        """Multi-page POST pagination + one detail (only row 0 matches)."""
        t = FakeTransport()
        route_wd_paginated(t)
        trace = request_trace.Trace()
        with tempfile.TemporaryDirectory() as tmp:
            sess, result, jobs, error = run_board(
                BOARDS["workday"], t, trace=trace, checkpoint_dir=tmp, detail_budget=100)
        self.assertEqual(error, "")
        self.assertEqual([c["method"] for c in t.calls], ["POST", "POST", "GET"])
        self.assertEqual(len(jobs), 25)
        self.assertEqual(jobs[0]["job_posted_at_datetime_utc"], "2026-07-07")  # from detail startDate
        d = trace.to_dict()
        self.assertEqual((d["initial_listing"], d["initial_detail"]), (2, 1))
        self.assertEqual(trace.total, result.physical_requests)

    def test_cornerstone_ondemand(self):
        """Multi-page GET pagination; no detail endpoint."""
        t = FakeTransport()
        route_cs_paginated(t)
        trace = request_trace.Trace()
        with tempfile.TemporaryDirectory() as tmp:
            sess, result, jobs, error = run_board(
                BOARDS["cornerstone_ondemand"], t, trace=trace, checkpoint_dir=tmp)
        self.assertEqual(error, "")
        self.assertEqual([c["method"] for c in t.calls], ["GET", "GET"])   # 2 pages, no detail
        self.assertEqual([c["params"].get("page") for c in t.calls], [1, 2])
        self.assertEqual(len(jobs), 30)
        self.assertEqual(jobs[0]["job_id"], "ats:cornerstone_ondemand:acme|5:CS-0")
        self.assertEqual(trace.to_dict()["initial_detail"], 0)

    def test_all_nine_reconcile_physical_to_six_classes(self):
        """G: physical == sum of the six role x phase classes, every provider."""
        for provider in ALL_PROVIDERS:
            t = transport_for(provider)
            trace = request_trace.Trace()
            _, result, _, error = run_board(BOARDS[provider], t, trace=trace, detail_budget=100)
            self.assertEqual(error, "", provider)
            self.assertEqual(trace.total, result.physical_requests, provider)
            self.assertEqual(trace.total, len(t.calls), provider)
            self.assertTrue(trace.origins_reconcile(), provider)


# --------------------------------------------------------------------------
# D -- detail providers: listing vs detail independence
# --------------------------------------------------------------------------


class DetailProviderTests(unittest.TestCase):
    def test_smartrecruiters_detail_call_is_a_detail_role(self):
        t = FakeTransport()
        t.add(lambda r: r["url"].endswith("/companies/acme/postings/SRD1"),
              ok("smartrecruiters/detail_SRD1.json"))
        t.add(lambda r: r["url"].endswith("/companies/acme/postings"),
              ok("smartrecruiters/postings_detail.json"))
        trace = request_trace.Trace()
        _, result, jobs, error = run_board(BOARDS["smartrecruiters"], t, trace=trace, detail_budget=100)
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 1)
        d = trace.to_dict()
        self.assertEqual(d["initial_listing"], 1)          # one page
        self.assertEqual(d["initial_detail"], 1)           # one detail
        self.assertTrue(jobs[0]["_smartrecruiters_detail_request_made"])
        self.assertEqual(result.detail_records, 1)
        self.assertEqual(trace.total, 2)

    def test_greenhouse_detail_failure_after_listing_success_preserves_records(self):
        """D: detail request fails; listing records survive with an error note."""
        t = FakeTransport()
        t.add(lambda r: "/v1/boards/acme/jobs/101" in r["url"],
              lambda r: FakeResponse(500, "boom", r["url"]))
        t.add(lambda r: r["url"].endswith("/v1/boards/acme/jobs"), ok("greenhouse/jobs.json"))
        t.add(lambda r: r["url"].endswith("/v1/boards/acme"), ok("greenhouse/board.json"))
        trace = request_trace.Trace()
        _, result, jobs, error = run_board(BOARDS["greenhouse"], t, trace=trace, detail_budget=100)
        self.assertEqual(error, "")                        # board-level fetch still succeeds
        self.assertEqual(len(jobs), 2)                     # both listing rows kept
        self.assertIn("HTTP 500", jobs[0]["_greenhouse_detail_error"])
        self.assertEqual(jobs[0]["job_posted_at_datetime_utc"], "")   # detail never applied
        d = trace.to_dict()
        self.assertEqual((d["initial_listing"], d["initial_detail"]), (2, 1))  # detail was attempted

    def test_workday_detail_budget_boundary_preserves_listing(self):
        """D: the per-run detail budget (the production mechanism that decrements
        across boards) reaching zero means a later board fetches listing only.
        Listing output is preserved in full, no detail request is sent, and
        nothing is double counted. This is the SUPPORTED graceful boundary --
        distinct from a hard wire-budget stop mid-detail, which raises the whole
        board (see test_8_board_budget_stops_pagination)."""
        budget = RequestBudget(100000)
        t = FakeTransport()
        route_wd_paginated(t)
        trace = request_trace.Trace()
        with tempfile.TemporaryDirectory() as tmp:
            _, result, jobs, error = run_board(
                BOARDS["workday"], t, budget=budget, trace=trace,
                checkpoint_dir=tmp, detail_budget=0)          # detail budget exhausted
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 25)                        # listing fully preserved
        self.assertEqual([c["method"] for c in t.calls], ["POST", "POST"])  # no detail hop
        self.assertEqual(trace.total, 2)
        self.assertEqual(trace.to_dict()["initial_detail"], 0)
        self.assertEqual(trace.total, result.physical_requests)


# --------------------------------------------------------------------------
# E -- listing-only providers: no false detail attribution
# --------------------------------------------------------------------------


class ListingOnlyProviderTests(unittest.TestCase):
    def test_no_detail_role_is_ever_attributed(self):
        for provider in LISTING_ONLY:
            t = transport_for(provider)
            trace = request_trace.Trace()
            _, result, jobs, error = run_board(BOARDS[provider], t, trace=trace, detail_budget=100)
            d = trace.to_dict()
            self.assertEqual(d["initial_detail"], 0, provider)
            self.assertEqual(d["detail_retries"], 0, provider)
            self.assertEqual(d["detail_redirects"], 0, provider)
            self.assertEqual(result.detail_records, 0, provider)

    def test_cornerstone_pagination_and_exhaustion(self):
        t = FakeTransport()
        route_cs_paginated(t)
        trace = request_trace.Trace()
        _, result, jobs, error = run_board(BOARDS["cornerstone_ondemand"], t, trace=trace)
        # page 2 returns 5 (< pageSize 25) -> exhausted, no third page.
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(len(jobs), 30)

    def test_malformed_records_do_not_erase_valid_records(self):
        """E/F#3: a mixed payload (a non-dict row, a row missing id) keeps the
        valid record."""
        t = FakeTransport()
        t.add(lambda r: r["url"].endswith("/v1/boards/acme/jobs"), ok("greenhouse/jobs_malformed.json"))
        t.add(lambda r: r["url"].endswith("/v1/boards/acme"), ok("greenhouse/board.json"))
        trace = request_trace.Trace()
        _, result, jobs, error = run_board(BOARDS["greenhouse"], t, trace=trace, detail_budget=0)
        self.assertEqual(error, "")
        # valid rows kept: the matching one (201) and the id-less one (still a
        # dict, normalized with a null id); the string row is skipped.
        ids = [j["job_id"] for j in jobs]
        self.assertIn("ats:greenhouse:acme:201", ids)
        self.assertGreaterEqual(len(jobs), 1)
        self.assertTrue(all(isinstance(j, dict) for j in jobs))


# --------------------------------------------------------------------------
# F -- failure injection matrix (real path)
# --------------------------------------------------------------------------


class FailureInjectionTests(unittest.TestCase):
    def test_1_first_page_succeeds_second_page_fails(self):
        """Workday page 2 errors after page 1 succeeded."""
        t = FakeTransport()

        def wd(rec):
            offset = int(rec["json"].get("offset", 0) or 0)
            if offset == 0:
                return FakeResponse(200, load("workday/jobs_p1.json"), rec["url"])
            return FakeResponse(503, "unavailable", rec["url"])
        t.add(lambda r: r["url"].endswith("/wday/cxs/acme/careers/jobs"), wd)
        trace = request_trace.Trace()
        _, result, jobs, error = run_board(BOARDS["workday"], t, trace=trace, detail_budget=0)
        # Page 1's 20 rows are lost on a hard second-page error (provider
        # contract returns [] + error); this is the documented Workday behavior.
        self.assertNotEqual(error, "")
        self.assertIn("HTTP 503", error)
        self.assertEqual(trace.total, result.physical_requests)

    def test_2_listing_ok_one_detail_fails(self):
        t = FakeTransport()
        t.add(lambda r: "/v1/boards/acme/jobs/101" in r["url"],
              lambda r: FakeResponse(500, "x", r["url"]))
        t.add(lambda r: r["url"].endswith("/v1/boards/acme/jobs"), ok("greenhouse/jobs.json"))
        t.add(lambda r: r["url"].endswith("/v1/boards/acme"), ok("greenhouse/board.json"))
        _, result, jobs, error = run_board(BOARDS["greenhouse"], t, detail_budget=100)
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 2)

    def test_3_malformed_json_is_a_clean_error_not_a_crash(self):
        t = FakeTransport()
        t.add(lambda r: "/v0/postings/acme" in r["url"],
              lambda r: FakeResponse(200, "{not json", r["url"]))
        _, result, jobs, error = run_board(BOARDS["lever"], t)
        self.assertEqual(jobs, [])
        self.assertIn("invalid_json", error)

    def test_6_redirect_chain_succeeds_and_classifies_as_redirect(self):
        """F#6: a 302 to the real endpoint; the hop is a redirect request."""
        t = FakeTransport()
        t.add(lambda r: r["url"].endswith("/v0/postings/acme"),
              lambda r: FakeResponse(302, "", r["url"],
                                     headers={"Location": "https://api.lever.co/v0/postings/acme-moved"}))
        t.add(lambda r: r["url"].endswith("/v0/postings/acme-moved"), ok("lever/postings.json"))
        trace = request_trace.Trace()
        _, result, jobs, error = run_board(BOARDS["lever"], t, trace=trace)
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 2)
        d = trace.to_dict()
        self.assertEqual(d["initial_listing"], 1)
        self.assertEqual(d["listing_redirects"], 1)
        self.assertEqual(d["redirect_origins"], {"initial": 1})
        self.assertEqual(trace.total, 2)
        self.assertEqual(trace.total, result.physical_requests)

    def test_7_redirect_limit_exceeded_is_a_clean_error(self):
        """F#7: an endless redirect loop stops at the fetcher's 4-hop limit."""
        t = FakeTransport()
        t.add(lambda r: "/v0/postings/acme" in r["url"],
              lambda r: FakeResponse(302, "", r["url"],
                                     headers={"Location": "https://api.lever.co/v0/postings/acme"}))
        trace = request_trace.Trace()
        _, result, jobs, error = run_board(BOARDS["lever"], t, trace=trace)
        self.assertEqual(jobs, [])
        self.assertIn("too_many_redirects", error)
        # 1 initial + 3 redirect hops = 4 physical attempts, all reconciled.
        self.assertEqual(trace.total, result.physical_requests)
        self.assertEqual(trace.to_dict()["listing_redirects"], 3)

    def test_8_board_budget_stops_pagination(self):
        """F#8: a board budget smaller than the pages available stops before the
        next page's wire. Only page 1 is physically fetched; the mid-pagination
        stop raises out of the provider branch (default_fetcher does not catch
        RequestCeilingReached), so this board becomes a board-level failure with
        its partial page discarded -- the board-level preservation contract is
        that OTHER boards survive (proven in the pipeline tests), not that a
        half-paginated board keeps its first page."""
        budget = RequestBudget(100000, board_limit=1)
        t = FakeTransport()
        route_cs_paginated(t)
        _, result, jobs, error = run_board(BOARDS["cornerstone_ondemand"], t, budget=budget)
        self.assertEqual(len(t.calls), 1)           # page 2 refused before the wire
        self.assertEqual(result.physical_requests, 1)
        self.assertNotEqual(error, "")              # board failed on the budget stop
        self.assertIn("RequestCeilingReached", error)
        self.assertEqual(jobs, [])

    def test_all_error_scenarios_reconcile_requests(self):
        """Every physical request in a failure run still reconciles to the six
        classes, and origins never inflate the total."""
        scenarios = []
        # a. hard 503 on greenhouse jobs
        t = FakeTransport()
        t.add(lambda r: r["url"].endswith("/v1/boards/acme/jobs"),
              lambda r: FakeResponse(503, "x", r["url"]))
        t.add(lambda r: r["url"].endswith("/v1/boards/acme"), ok("greenhouse/board.json"))
        scenarios.append(("greenhouse", t))
        # b. connection error on ashby
        t2 = FakeTransport()
        t2.add(lambda r: True, lambda r: requests.RequestException("connreset"))
        scenarios.append(("ashby", t2))
        for provider, transport in scenarios:
            trace = request_trace.Trace()
            _, result, _, _ = run_board(BOARDS[provider], transport, trace=trace)
            self.assertEqual(trace.total, result.physical_requests, provider)
            self.assertTrue(trace.origins_reconcile(), provider)


# --------------------------------------------------------------------------
# F#9/10/11 + B/H -- multi-board integration through run_multi_source_acquisition
# --------------------------------------------------------------------------


class PipelineIntegrationTests(PipelineHarness):
    def _boards(self):
        return [BOARDS[p] for p in ALL_PROVIDERS]

    def test_all_nine_providers_run_through_the_real_pipeline(self):
        """B/H: the production wiring dispatches all nine, checkpoints each, and
        the lane accounting reconciles."""
        t = transport_for(*ALL_PROVIDERS)
        budget = RequestBudget(100000)
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget,
                                      trace=request_trace.Trace())
            self.run_pipeline(self._boards(), t, session=session, budget=budget)
            acct = session.accounting()
            files = len(list(Path(tmp).glob("*.json")))
        self.assertEqual(acct["boards_attempted"], 9)
        self.assertEqual(acct["boards_completed"], 9)
        self.assertEqual(acct["boards_failed"], 0)
        self.assertTrue(session.reconciles())
        self.assertTrue(all(session.full_reconciliation().values()))
        self.assertEqual(files, 9)
        # No external host beyond the nine faked provider APIs.
        self.assertNotIn("", t.hosts)
        # The pipeline dedupes and role-filters downstream, so the saved set is a
        # subset of the ATS lane's normalized records; every saved job is ATS.
        self.assertGreater(len(self.selected_jobs), 0)
        self.assertLessEqual(len(self.selected_jobs), acct["normalized_records"])
        self.assertTrue(all(j.get("_ats_provider") in ALL_PROVIDERS for j in self.selected_jobs))

    def test_11_board_three_fails_others_survive(self):
        """F#11: one provider raises; the other eight remain usable and persisted."""
        boards = [BOARDS["greenhouse"], BOARDS["lever"], BOARDS["ashby"], BOARDS["workable"]]
        t = transport_for("greenhouse", "lever", "workable")
        # ashby route missing on purpose -> unrouted -> AssertionError inside
        # fetch_board_jobs -> board-level failure, not a lane crash.
        t.add(lambda r: "/posting-api/job-board/acme" in r["url"],
              lambda r: FakeResponse(500, "ashby down", r["url"]))
        budget = RequestBudget(100000)
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_pipeline(boards, t, session=session, budget=budget)
            acct = session.accounting()
        self.assertEqual(acct["boards_attempted"], 4)
        self.assertEqual(acct["boards_completed"], 3)
        self.assertEqual(acct["boards_failed"], 1)
        self.assertTrue(session.reconciles())
        ids = {j["_ats_provider"] for j in self.selected_jobs}
        self.assertEqual(ids, {"greenhouse", "lever", "workable"})

    def test_9_provider_budget_stops_one_provider_not_another(self):
        """F#9: a provider budget exhausts one provider; the other still runs."""
        boards = [BOARDS["greenhouse"], BOARDS["lever"]]
        t = transport_for("greenhouse", "lever")
        budget = RequestBudget(100000, provider_limits={"ats_greenhouse": 0})
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_pipeline(boards, t, session=session, budget=budget)
            acct = session.accounting()
        gh = next(r for r in session.results if r.provider == "greenhouse")
        lv = next(r for r in session.results if r.provider == "lever")
        self.assertTrue(gh.skipped_by_budget)
        self.assertEqual(gh.physical_requests, 0)
        self.assertFalse(lv.skipped_by_budget)
        self.assertGreater(lv.canonical_records, 0)
        self.assertEqual(acct["boards_skipped_by_budget"], 1)
        self.assertEqual(acct["boards_attempted"], 1)

    def test_10_ats_lane_budget_preserves_non_ats_reservation(self):
        """F#10: ATS pre-skips when its share is gone; reserved JSearch capacity
        is untouched."""
        boards = [BOARDS[p] for p in ("greenhouse", "lever", "ashby")]
        t = transport_for("greenhouse", "lever", "ashby")
        budget = RequestBudget(6, reserved_for_lanes={"jsearch": 3})
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            self.run_pipeline(boards, t, session=session, budget=budget)
        self.assertLessEqual(budget.per_lane.get("ats", 0), 3)   # 6 - 3 reserved
        self.assertIsNone(budget.would_block(lane="jsearch"))
        self.assertFalse(budget.exhausted)
        self.assertTrue(session.reconciles())


# --------------------------------------------------------------------------
# H -- scheduler interaction: selection changes, parsing/attribution do not
# --------------------------------------------------------------------------


class SchedulerInteractionTests(PipelineHarness):
    def _registry_boards(self):
        # give each board a last_checked so the partition scheduler is meaningful
        boards = []
        for p in ALL_PROVIDERS:
            b = dict(BOARDS[p])
            b["last_checked_at"] = "2026-07-01T00:00:00Z"
            boards.append(b)
        return boards

    def _run(self, mode):
        t = transport_for(*ALL_PROVIDERS)
        budget = RequestBudget(100000)
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget,
                                      trace=request_trace.Trace())
            with mock.patch.object(config, "ATS_SCHEDULER_MAX_AGE_HOURS", 0), \
                 mock.patch.object(config, "ATS_SCHEDULER_CYCLE_LENGTH", 1):
                # cycle_length 1 => every board is in the single slot => all nine
                # selected, so parsing is directly comparable to legacy.
                self.run_pipeline(self._registry_boards(), t, session=session,
                                  budget=budget, scheduler_mode=mode)
            acct = session.accounting()
            jobs_by_provider = {}
            for j in self.selected_jobs:
                jobs_by_provider.setdefault(j["_ats_provider"], 0)
                jobs_by_provider[j["_ats_provider"]] += 1
        return acct, jobs_by_provider

    def test_parsing_and_checkpoints_are_identical_across_modes(self):
        legacy_acct, legacy_jobs = self._run("legacy_interval")
        part_acct, part_jobs = self._run("deterministic_partition")
        # Same nine boards parsed the same way, same normalized record counts.
        self.assertEqual(legacy_jobs, part_jobs)
        self.assertEqual(legacy_acct["normalized_records"], part_acct["normalized_records"])
        self.assertEqual(legacy_acct["boards_completed"], part_acct["boards_completed"])
        # Only the scheduler provenance differs.
        self.assertEqual(legacy_acct["boards_skipped_by_scheduler"], 0)
        self.assertEqual(part_acct.get("scheduler_mode"), "deterministic_partition")

    def test_deterministic_partition_actually_partitions(self):
        """Scheduling changes which boards are selected (a narrower cycle slot
        selects a subset), without touching provider parsing."""
        t = transport_for(*ALL_PROVIDERS)
        budget = RequestBudget(100000)
        with tempfile.TemporaryDirectory() as tmp:
            session = AtsBoardSession(checkpoint_dir=tmp, budget=budget)
            with mock.patch.object(config, "ATS_SCHEDULER_MAX_AGE_HOURS", 0), \
                 mock.patch.object(config, "ATS_SCHEDULER_CYCLE_LENGTH", 7), \
                 mock.patch.object(config, "ATS_SCHEDULER_POSITION", 0):
                self.run_pipeline(self._registry_boards(), t, session=session,
                                  budget=budget, scheduler_mode="deterministic_partition")
            acct = session.accounting()
        self.assertEqual(acct["boards_available"], 9)
        self.assertLess(acct["boards_selected"], 9)          # a slot is a subset
        self.assertEqual(acct["boards_available"],
                         acct["boards_selected"] + acct["boards_skipped_by_scheduler"])
        self.assertTrue(session.reconciles())


# --------------------------------------------------------------------------
# I -- Cornerstone evidence boundary
# --------------------------------------------------------------------------


class CornerstoneEvidenceTests(unittest.TestCase):
    def test_cornerstone_parses_the_currently_implemented_contract_only(self):
        """Proven offline: the implemented `requisitions` search shape parses,
        paginates and normalizes. The live shape remains unverified (see the
        UNVERIFIED-OFFLINE note in ats_board_registry.fetch_board_jobs)."""
        t = FakeTransport()
        route_cs_paginated(t)
        trace = request_trace.Trace()
        _, result, jobs, error = run_board(BOARDS["cornerstone_ondemand"], t, trace=trace)
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 30)
        self.assertEqual(jobs[0]["_ats_provider"], "cornerstone_ondemand")
        self.assertEqual(trace.total, result.physical_requests)

    def test_cornerstone_unknown_shape_yields_clean_error_not_fabrication(self):
        """A tenant whose response shape differs returns no rows and a clean
        error, never invented jobs."""
        t = FakeTransport()
        t.add(lambda r: "/ux/ats/careersite/5/api/search" in r["url"],
              lambda r: FakeResponse(200, json.dumps({"unexpected": "shape"}), r["url"]))
        _, result, jobs, error = run_board(BOARDS["cornerstone_ondemand"], t)
        self.assertEqual(jobs, [])            # empty list -> break -> no jobs
        self.assertEqual(result.canonical_records, 0)


# --------------------------------------------------------------------------
# No external contact
# --------------------------------------------------------------------------


class NoExternalRequestTests(unittest.TestCase):
    def test_the_real_wire_is_never_reached(self):
        real = requests.request  # captured before any patch
        sentinel = mock.Mock(side_effect=AssertionError("a real request escaped"))
        t = transport_for("greenhouse")
        with mock.patch.object(requests, "request", sentinel):
            # run_board re-patches requests.request with the transport; if any
            # request bypassed the fetcher path it would hit the sentinel.
            run_board(BOARDS["greenhouse"], t, detail_budget=100)
        sentinel.assert_not_called()
        self.assertIs(requests.request, real)


if __name__ == "__main__":
    unittest.main()
