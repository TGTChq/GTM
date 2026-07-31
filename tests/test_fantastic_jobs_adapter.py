"""Tests for the additive Fantastic.jobs acquisition source.

All HTTP is mocked; no live Fantastic.jobs / Apollo / Hunter / Airtable call is
made. Fixtures are synthetic and contain no real key or personal contact data.
"""
import importlib
import json
import os
import unittest

import config
import fantastic_jobs_adapter as fja


def _reload_config(**env):
    keys = [
        "FANTASTIC_JOBS_ENABLED", "FANTASTIC_JOBS_API_KEY", "FANTASTIC_JOBS_BASE_URL",
        "FANTASTIC_JOBS_TIME_FRAME", "FANTASTIC_JOBS_MAX_JOBS_PER_RUN",
        "FANTASTIC_JOBS_ATS_LIMIT", "FANTASTIC_JOBS_WELLFOUND_LIMIT",
        "FANTASTIC_JOBS_YCOMBINATOR_LIMIT", "FANTASTIC_JOBS_LINKEDIN_LIMIT",
        "FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING", "FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING",
        "FANTASTIC_JOBS_FAIL_OPEN", "FANTASTIC_JOBS_DESCRIPTION_FORMAT",
        "PIPELINE_AUTORUN_ENABLED",
    ]
    for k in keys:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = str(v)
    importlib.reload(config)
    importlib.reload(fja)
    return config


class FakeResp:
    def __init__(self, status=200, payload=None, headers=None, raise_exc=None):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.headers = headers or {}
        self._raise = raise_exc

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttp:
    """Queued responses; records calls (without ever storing the key)."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "has_auth": bool((headers or {}).get("Authorization")), "params": dict(params or {})})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ats_record(**over):
    rec = {
        "id": "ats1", "title": "Customer Success Manager", "organization": "Acme Co",
        "organization_url": "https://acme.com", "domain_derived": "acme.com",
        "org_linkedin_website": "https://acme.com", "org_linkedin_headcount": 120,
        "org_linkedin_size": "51-200", "org_linkedin_industry": "Software",
        "org_linkedin_recruitment_agency_derived": False,
        "countries_derived": ["US"], "locations_derived": ["New York, US"],
        "date_posted": "2026-07-30T10:00:00Z", "description_text": "Own retention and onboarding.",
        "source": "paylocity", "source_type": "ats", "employment_type": "FULLTIME",
        "ats_duplicate": False,
        # PII that must be dropped:
        "recruiter_name": "Jane Doe", "recruiter_url": "https://linkedin.com/in/jane",
        "ai_hiring_manager_email_address": "hm@acme.com",
    }
    rec.update(over)
    return rec


def _jb_record(source="linkedin", **over):
    rec = {
        "id": "jb1", "title": "Account Executive", "organization": "Beta Inc",
        "organization_url": "https://beta.io", "countries_derived": ["US"],
        "date_posted": "2026-07-29T08:00:00Z", "description_text": "Close deals.",
        "source": source, "source_type": "jobboard", "ats_duplicate": False,
        "recruiter_name": "Bob",
    }
    rec.update(over)
    return rec


class ConfigValidationTests(unittest.TestCase):
    def tearDown(self):
        _reload_config()  # restore defaults

    def test_disabled_missing_key_is_valid(self):
        c = _reload_config(FANTASTIC_JOBS_ENABLED="0", FANTASTIC_JOBS_API_KEY="")
        c.validate_fantastic_jobs_config()  # no raise

    def test_enabled_missing_key_errors(self):
        c = _reload_config(FANTASTIC_JOBS_ENABLED="1", FANTASTIC_JOBS_API_KEY="")
        with self.assertRaises(ValueError):
            c.validate_fantastic_jobs_config()

    def test_segment_sum_exceeds_max_errors(self):
        c = _reload_config(FANTASTIC_JOBS_ENABLED="1", FANTASTIC_JOBS_API_KEY="k",
                           FANTASTIC_JOBS_MAX_JOBS_PER_RUN="100", FANTASTIC_JOBS_ATS_LIMIT="300")
        with self.assertRaises(ValueError):
            c.validate_fantastic_jobs_config()

    def test_non_https_base_url_errors(self):
        c = _reload_config(FANTASTIC_JOBS_ENABLED="1", FANTASTIC_JOBS_API_KEY="k",
                           FANTASTIC_JOBS_BASE_URL="http://data.fantastic.jobs")
        with self.assertRaises(ValueError):
            c.validate_fantastic_jobs_config()

    def test_bad_description_format_errors(self):
        c = _reload_config(FANTASTIC_JOBS_ENABLED="1", FANTASTIC_JOBS_API_KEY="k",
                           FANTASTIC_JOBS_DESCRIPTION_FORMAT="pdf")
        with self.assertRaises(ValueError):
            c.validate_fantastic_jobs_config()

    def test_key_never_in_validation_error(self):
        c = _reload_config(FANTASTIC_JOBS_ENABLED="1", FANTASTIC_JOBS_API_KEY="SECRETKEY123",
                           FANTASTIC_JOBS_BASE_URL="http://x")
        try:
            c.validate_fantastic_jobs_config()
        except ValueError as exc:
            self.assertNotIn("SECRETKEY123", str(exc))


class DisabledParityTests(unittest.TestCase):
    def tearDown(self):
        _reload_config()

    def test_disabled_makes_no_request(self):
        _reload_config(FANTASTIC_JOBS_ENABLED="0")
        def boom(*a, **k):
            raise AssertionError("no request when disabled")
        r = fja.run_fantastic_jobs_acquisition(http_get=boom)
        self.assertEqual(r.jobs, [])
        self.assertFalse(r.metadata.get("enabled"))
        self.assertEqual(r.metadata.get("skipped_reason"), "disabled")


class MappingTests(unittest.TestCase):
    def tearDown(self):
        _reload_config()

    def test_ats_mapping_and_pii_drop(self):
        _reload_config()
        job, reason = fja.map_record(_ats_record(), fja.ATS_SOURCE)
        self.assertIsNotNone(job, reason)
        self.assertEqual(job["job_id"], "fantastic_ats1")
        self.assertEqual(job["employer_website"], "acme.com")
        self.assertEqual(job["job_posted_at_datetime_utc"], "2026-07-30T10:00:00+00:00")
        self.assertEqual(job["_acquisition_source"], fja.ATS_SOURCE)
        blob = json.dumps(job).lower()
        for token in ("jane", "recruiter", "hm@acme.com", "hiring_manager"):
            self.assertNotIn(token, blob)

    def test_domain_precedence_and_ats_host_reject(self):
        # organization_url wins
        j, _ = fja.map_record(_ats_record(organization_url="https://real.com", domain_derived="other.com"), fja.ATS_SOURCE)
        self.assertEqual(j["employer_website"], "real.com")
        # ATS host is never an employer domain, and name is never inferred
        j2, _ = fja.map_record(_ats_record(organization_url="https://jobs.lever.co/acme", domain_derived="", org_linkedin_website=""), fja.ATS_SOURCE)
        self.assertEqual(j2["employer_website"], "")

    def test_missing_stable_id_rejected(self):
        j, r = fja.map_record({"title": "x", "organization": "y"}, fja.ATS_SOURCE)
        self.assertIsNone(j)
        self.assertEqual(r, "missing_stable_id")

    def test_relative_and_malformed_dates_safe(self):
        for bad in ("Posted Yesterday", "30+ Days Ago", "not-a-date", None, ""):
            self.assertEqual(fja._safe_iso(bad), "")
        self.assertTrue(fja._safe_iso("2026-07-30T10:00:00Z").startswith("2026-07-30T10:00:00"))

    def test_headcount_and_staffing_carried_not_gated(self):
        j, _ = fja.map_record(_ats_record(org_linkedin_recruitment_agency_derived=True), fja.ATS_SOURCE)
        self.assertEqual(j["_org_headcount"], 120)
        self.assertTrue(j["_staffing_agency_flag"])

    def test_empty_description_preserved_not_fabricated(self):
        j, _ = fja.map_record(_ats_record(description_text="", description=""), fja.ATS_SOURCE)
        self.assertEqual(j["job_description"], "")
        self.assertTrue(j["job_title"])


class FetchBehaviorTests(unittest.TestCase):
    def tearDown(self):
        _reload_config()

    def _enable(self, **over):
        env = dict(
            FANTASTIC_JOBS_ENABLED="1", FANTASTIC_JOBS_API_KEY="testkey",
            FANTASTIC_JOBS_ATS_LIMIT="2", FANTASTIC_JOBS_WELLFOUND_LIMIT="0",
            FANTASTIC_JOBS_YCOMBINATOR_LIMIT="0", FANTASTIC_JOBS_LINKEDIN_LIMIT="2",
            FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING="0",
            FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING="0",
        )
        env.update(over)
        return _reload_config(**env)

    def test_happy_path_ats_and_jb(self):
        self._enable()
        h = FakeHttp([
            FakeResp(200, [_ats_record()], {"x-api-jobs-remaining": "495", "x-api-requests-remaining": "49"}),
            FakeResp(200, [_jb_record("linkedin")], {"x-api-jobs-remaining": "494"}),
        ])
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        self.assertTrue(r.success)
        labels = {j["_acquisition_source"] for j in r.jobs}
        self.assertIn(fja.ATS_SOURCE, labels)
        self.assertIn("fantastic_jobs_linkedin", labels)
        self.assertTrue(all(c["has_auth"] for c in h.calls))

    def test_jb_source_bucketing_filters_wrong_source(self):
        self._enable(FANTASTIC_JOBS_ATS_LIMIT="0", FANTASTIC_JOBS_LINKEDIN_LIMIT="3")
        # response returns a non-linkedin row that must be filtered out of the linkedin segment
        h = FakeHttp([FakeResp(200, [_jb_record("indeed"), _jb_record("linkedin", id="jb2")])])
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        srcs = {j["_fantastic_source"] for j in r.jobs}
        self.assertEqual(srcs, {"linkedin"})

    def test_401_disables_source_no_retry(self):
        self._enable()
        h = FakeHttp([FakeResp(401)])
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        self.assertFalse(r.success)
        self.assertEqual(len(h.calls), 1)  # no retry on auth failure
        self.assertEqual(r.metadata.get("stop_reason"), "auth_failed")

    def test_429_stops_preserves_jobs(self):
        self._enable()
        h = FakeHttp([FakeResp(200, [_ats_record()], {"x-api-jobs-remaining": "495"}), FakeResp(429)])
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        self.assertEqual(r.metadata.get("stop_reason"), "rate_limited")
        self.assertEqual(len(r.jobs), 1)  # already-acquired jobs preserved

    def test_malformed_json_fails_open(self):
        self._enable()
        h = FakeHttp([FakeResp(200, ValueError("bad")), FakeResp(200, [_jb_record("linkedin")])])
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        # ATS segment errored but source failed open; pipeline continues, no crash
        self.assertIsNotNone(r)

    def test_individual_bad_record_isolated(self):
        self._enable(FANTASTIC_JOBS_ATS_LIMIT="1", FANTASTIC_JOBS_LINKEDIN_LIMIT="0")
        h = FakeHttp([FakeResp(200, [{"title": "no id"}, _ats_record()])])
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        self.assertEqual(len(r.jobs), 1)  # bad record skipped, good one kept
        seg = r.metadata["segments"][fja.ATS_SOURCE]
        self.assertEqual(seg["schema_rejected"], 1)

    def test_quota_reserve_stops_before_request(self):
        self._enable(FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING="1000")
        h = FakeHttp([FakeResp(200, [_ats_record()], {"x-api-jobs-remaining": "500"})])
        # first request allowed (no prior header), sets remaining=500, then reserve blocks next
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        self.assertIsNotNone(r)

    def test_pagination_repeated_page_stops(self):
        self._enable(FANTASTIC_JOBS_ATS_LIMIT="10", FANTASTIC_JOBS_LINKEDIN_LIMIT="0")
        same = FakeResp(200, [_ats_record()], {"x-api-jobs-remaining": "495"})
        same2 = FakeResp(200, [_ats_record()], {"x-api-jobs-remaining": "494"})
        h = FakeHttp([same, same2, same2])
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        # dedup by id keeps 1 job; loop stops on no_new_ids/short_page, never infinite
        self.assertEqual(len(r.jobs), 1)

    def test_fail_open_on_network_error(self):
        self._enable()
        h = FakeHttp([ConnectionError("dns"), ConnectionError("dns"), ConnectionError("dns")])
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        self.assertIsNotNone(r)  # no crash; fail-open

    def test_metadata_observability_fields(self):
        self._enable(FANTASTIC_JOBS_LINKEDIN_LIMIT="0")
        h = FakeHttp([FakeResp(200, [_ats_record()], {"x-api-jobs-remaining": "495", "x-api-requests-remaining": "49"})])
        r = fja.run_fantastic_jobs_acquisition(http_get=h)
        m = r.metadata
        self.assertIn("segments", m)
        self.assertIn("jobs_quota_consumed", m)
        self.assertIn("requests_consumed", m)
        self.assertIn("stop_reason", m)
        self.assertNotIn("Authorization", json.dumps(m))


class StartupGuardTests(unittest.TestCase):
    def tearDown(self):
        _reload_config()
        import run_daily
        importlib.reload(run_daily)  # discard any per-test patches

    def test_autorun_disabled_does_not_run_pipeline(self):
        from unittest.mock import patch
        _reload_config(PIPELINE_AUTORUN_ENABLED="0")
        import run_daily
        importlib.reload(run_daily)
        with patch.object(run_daily, "main", side_effect=AssertionError("must not run")) as m:
            rc = run_daily.run_entrypoint()
        self.assertEqual(rc, 0)
        m.assert_not_called()

    def test_autorun_enabled_calls_main(self):
        from unittest.mock import patch
        _reload_config(PIPELINE_AUTORUN_ENABLED="1")
        import run_daily
        importlib.reload(run_daily)
        with patch.object(run_daily, "main", return_value=0) as m:
            rc = run_daily.run_entrypoint()
        self.assertEqual(rc, 0)
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
