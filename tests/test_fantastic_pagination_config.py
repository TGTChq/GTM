from __future__ import annotations

import unittest
from unittest import mock

import config
import fantastic_jobs_adapter as fja


class _PagingResp:
    def __init__(self, offset, limit):
        self.status_code = 200
        self.headers = {"x-api-jobs-remaining": "1000000", "x-api-requests-remaining": "1000000"}
        # a full page of `limit` unique in-scope records keyed by absolute offset
        self._rows = [{
            "id": str(offset + i),
            "title": "Software Engineer",
            "organization": f"Company {offset + i}",
            "source": "linkedin",
            "countries_derived": ["United States"],
            "employment_type": ["FULL_TIME"],
            "org_linkedin_headcount": 100,
        } for i in range(limit)]

    def json(self):
        return self._rows


def _run_pages(max_pages):
    captured_pages = {"n": 0}

    def fake_http_get(url, headers, params, timeout):
        captured_pages["n"] += 1
        return _PagingResp(int(params.get("offset", 0)), int(params.get("limit", 100)))

    base = dict(
        FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
        FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
        FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_WELLFOUND_LIMIT=0,
        FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
        FANTASTIC_JOBS_LINKEDIN_LIMIT=1_000_000, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=1_000_000,
        FANTASTIC_JOBS_TIME_FRAME="24h", FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=max_pages,
        FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=90, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=20,
        FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=True,
        FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
        FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME", FANTASTIC_JOBS_EXCLUDE_AGENCY=True,
    )
    with mock.patch.multiple(config, **base):
        res = fja.run_fantastic_jobs_acquisition(http_get=fake_http_get)
    return res, captured_pages["n"]


class FantasticPaginationConfigTests(unittest.TestCase):
    def test_page_ceiling_is_configurable_low(self):
        res, requests_made = _run_pages(max_pages=3)
        seg = res.metadata["segments"]["fantastic_jobs_linkedin"]
        self.assertEqual(seg["stop_reason"], "page_cap")
        self.assertEqual(requests_made, 3)          # exactly 3 pages fetched
        self.assertEqual(len(res.jobs), 300)         # 3 x 100

    def test_page_ceiling_is_configurable_higher(self):
        res, requests_made = _run_pages(max_pages=5)
        seg = res.metadata["segments"]["fantastic_jobs_linkedin"]
        self.assertEqual(seg["stop_reason"], "page_cap")
        self.assertEqual(requests_made, 5)
        self.assertEqual(len(res.jobs), 500)

    def test_supports_3500_per_run_within_default_50_pages(self):
        # 3,500 jobs at 100/page = 35 pages, well within the default ceiling of 50.
        self.assertGreaterEqual(config.FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT * 100, 3500)


if __name__ == "__main__":
    unittest.main()
