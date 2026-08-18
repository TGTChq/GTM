from __future__ import annotations

import unittest
from unittest import mock

import config
import fantastic_jobs_adapter as fja


class _Resp:
    def __init__(self, rows):
        self.status_code = 200
        self.headers = {"x-api-jobs-remaining": "19000", "x-api-requests-remaining": "9000"}
        self._rows = rows

    def json(self):
        return self._rows


def _run_capture(**overrides):
    captured = []

    def fake_http_get(url, headers, params, timeout):
        captured.append((url, dict(params)))
        return _Resp([])  # empty page -> segment stops after one request

    base = dict(
        FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="test-key",
        FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
        FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_WELLFOUND_LIMIT=0,
        FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0, FANTASTIC_JOBS_LINKEDIN_LIMIT=50,
        FANTASTIC_JOBS_MAX_JOBS_PER_RUN=50, FANTASTIC_JOBS_TIME_FRAME="24h",
        FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
        FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME", FANTASTIC_JOBS_EXCLUDE_AGENCY=True,
        FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=90, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=20,
        FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=True,
    )
    base.update(overrides)
    with mock.patch.multiple(config, **base):
        fja.run_fantastic_jobs_acquisition(http_get=fake_http_get)
    return captured


class FantasticRequestFilterTests(unittest.TestCase):
    def test_linkedin_request_pushes_down_confirmed_icp_filters(self):
        captured = _run_capture()
        self.assertEqual(len(captured), 1, "exactly one request (linkedin only)")
        url, params = captured[0]
        self.assertTrue(url.endswith("/v1/active-jb"))
        self.assertEqual(params.get("source"), "linkedin")
        self.assertEqual(params.get("time_frame"), "24h")
        self.assertEqual(params.get("location"), "United States")
        self.assertEqual(params.get("organization_headcount_gte"), 25)
        self.assertEqual(params.get("ai_employment_type"), "FULL_TIME")
        self.assertEqual(params.get("organization_agency"), "exclude")

    def test_never_calls_ats_wellfound_or_ycombinator(self):
        captured = _run_capture()
        urls = [u for u, _ in captured]
        self.assertFalse(any("/v1/active-ats" in u for u in urls))
        # only the linkedin source is requested
        self.assertTrue(all(p.get("source") == "linkedin" for _, p in captured))

    def test_filters_are_configurable_and_omitted_when_disabled(self):
        captured = _run_capture(FANTASTIC_JOBS_EXCLUDE_AGENCY=False,
                                FANTASTIC_JOBS_LOCATION="", FANTASTIC_JOBS_HEADCOUNT_MIN=0)
        _url, params = captured[0]
        self.assertNotIn("organization_agency", params)
        self.assertNotIn("location", params)
        self.assertNotIn("organization_headcount_gte", params)
        # employment-type still pushed
        self.assertEqual(params.get("ai_employment_type"), "FULL_TIME")

    def test_headcount_max_pushed_down_as_organization_headcount_lt(self):
        # Acquisition-time upper bound (parity fix): <1000 filtered UPSTREAM so we
        # stop paying for jobs the downstream MAX_EMPLOYEES gate would reject.
        captured = _run_capture(FANTASTIC_JOBS_HEADCOUNT_MAX=1000)
        _url, params = captured[0]
        self.assertEqual(params.get("organization_headcount_lt"), 1000)
        self.assertEqual(params.get("organization_headcount_gte"), 25)

    def test_headcount_max_omitted_when_zero(self):
        captured = _run_capture(FANTASTIC_JOBS_HEADCOUNT_MAX=0)
        _url, params = captured[0]
        self.assertNotIn("organization_headcount_lt", params)


if __name__ == "__main__":
    unittest.main()
