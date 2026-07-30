"""Phase 4 of FINAL_30_PLUS_SYSTEM_SPEC.md: two silent pagination undershoots
found by the ATS/public-feed completeness audit.

1. Workday: page_limit was fixed at ATS_WORKDAY_MAX_PAGES_PER_BOARD (5) x 20
   jobs/page = 100, well below the pipeline's own ATS_MAX_JOBS_PER_BOARD
   (250) -- any tenant with more than 100 open reqs silently lost the rest,
   with no truncation signal.
2. Jobicy: a single hardcoded count=50 request with no pagination mechanism
   at all, while config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE implies the
   codebase expects materially higher per-source volume.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import config
from ats_board_registry import fetch_board_jobs
from free_job_sources import JobicyAdapter


class _FakeResponse:
    def __init__(self, status_code=200, text="", error=""):
        self.status_code = status_code
        self.text = text
        self.error = error


def _workday_page(rows, total):
    return _FakeResponse(text=json.dumps({"jobPostings": rows, "total": total}))


class WorkdayPaginationTests(unittest.TestCase):
    def test_fetches_beyond_the_old_100_job_page_limit_cap(self):
        total_jobs = 180  # 9 pages of 20; old page_limit=5 would cap at 100
        all_rows = [
            {"title": f"Revenue Systems Engineer {i}", "externalPath": f"/job/req-{i}"}
            for i in range(total_jobs)
        ]

        calls = {"n": 0}

        def fake_fetcher(url, method="GET", json_body=None, headers=None, **_kwargs):
            offset = int((json_body or {}).get("offset", 0))
            calls["n"] += 1
            page = all_rows[offset:offset + 20]
            return _workday_page(page, total_jobs)

        board = {
            "provider": "workday",
            "identifier": "acme|careers",
            "company_name": "Acme Corp",
            "api_base": "https://acme.wd1.myworkdayjobs.com",
        }
        with patch.object(config, "ATS_MAX_JOBS_PER_BOARD", 250):
            jobs, error = fetch_board_jobs(board, fetcher=fake_fetcher)
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), total_jobs)
        self.assertGreaterEqual(calls["n"], 9)

    def test_does_not_exceed_max_jobs_per_board(self):
        total_jobs = 400
        all_rows = [
            {"title": f"Role {i}", "externalPath": f"/job/req-{i}"}
            for i in range(total_jobs)
        ]

        def fake_fetcher(url, method="GET", json_body=None, headers=None, **_kwargs):
            offset = int((json_body or {}).get("offset", 0))
            page = all_rows[offset:offset + 20]
            return _workday_page(page, total_jobs)

        board = {
            "provider": "workday",
            "identifier": "bigco|careers",
            "company_name": "Big Co",
            "api_base": "https://bigco.wd1.myworkdayjobs.com",
        }
        with patch.object(config, "ATS_MAX_JOBS_PER_BOARD", 250):
            jobs, error = fetch_board_jobs(board, fetcher=fake_fetcher)
        self.assertEqual(error, "")
        self.assertLessEqual(len(jobs), 250)


class JobicyRequestSizeTests(unittest.TestCase):
    def test_requests_more_than_the_old_hardcoded_50(self):
        captured = {}

        def fake_fetcher(url, params=None, **_kwargs):
            captured["params"] = params
            return _FakeResponse(text=json.dumps({"jobs": []}))

        JobicyAdapter().fetch(fetcher=fake_fetcher)
        self.assertGreater(captured["params"]["count"], 50)
        self.assertEqual(captured["params"]["count"], config.JOBICY_REQUEST_COUNT)

    def test_marks_pagination_unsupported_and_flags_when_at_requested_count(self):
        request_count = config.JOBICY_REQUEST_COUNT
        rows = [{"id": i, "jobTitle": "Engineer", "companyName": "Acme"} for i in range(request_count)]

        def fake_fetcher(url, params=None, **_kwargs):
            return _FakeResponse(text=json.dumps({"jobs": rows}))

        result = JobicyAdapter().fetch(fetcher=fake_fetcher)
        self.assertFalse(result.metadata["pagination_supported"])
        self.assertTrue(result.metadata["response_at_requested_count"])

    def test_flags_false_when_response_is_short_of_requested_count(self):
        rows = [{"id": 1, "jobTitle": "Engineer", "companyName": "Acme"}]

        def fake_fetcher(url, params=None, **_kwargs):
            return _FakeResponse(text=json.dumps({"jobs": rows}))

        result = JobicyAdapter().fetch(fetcher=fake_fetcher)
        self.assertFalse(result.metadata["response_at_requested_count"])


if __name__ == "__main__":
    unittest.main()
