"""Incident A regression tests: Adzuna canonicalization + posting_integrity.

The 2026-07-30 validation run acquired 1,566 selected Adzuna postings but only
11 were prefilter-viable -- ~1,541 were rejected by posting_integrity. Root
cause: Adzuna is a first-party structured provider feed (it supplies the
employer as a discrete ``company.display_name`` and sets
``_provider_record_structured=True``) but was omitted from the trusted
structured-provider set in ``job_quality._trusted_structured_employer_identity``;
and a secondary ``_url_host`` bug misread dotted legal-suffix company names
("Trimble Inc.") as deployment hostnames. Both are corrected here.

All fixtures are synthetic (fabricated for this test, not captured from any real
Adzuna response) and use obviously fake company/domain names.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import adzuna_client
import job_filter
import job_quality
from adzuna_client import AdzunaAdapter, AdzunaSettings, normalize_adzuna_job
from free_job_sources import FetchPayload
from job_filter import dedup_key


def _settings(**overrides) -> AdzunaSettings:
    base = dict(enabled=True, country="us", app_id="test-id", app_key="test-key",
                results_per_page=50, max_pages_per_query=1, max_requests_per_run=40,
                max_days_old=30, timeout_seconds=20)
    base.update(overrides)
    return AdzunaSettings(**base)


def _row(job_id="a1", title="Customer Success Manager", company="Northwind Analytics", **extra):
    row = {
        "id": str(job_id),
        "title": title,
        "company": {"display_name": company},
        "location": {"display_name": "Remote - United States"},
        "category": {"label": "IT Jobs"},
        "description": "A fully synthetic remote United States job description for testing.",
        # Real Adzuna redirects are adzuna.com tracking links -> a syndicated
        # (intermediary) source host, which is precisely why these records need
        # the structured-provider trust-list to clear posting_integrity.
        "redirect_url": f"https://www.adzuna.com/land/ad/{job_id}",
        "created": "2026-07-20T09:15:00Z",
        "contract_time": "full_time",
        "salary_min": 90000, "salary_max": 120000,
    }
    row.update(extra)
    return row


def _response(results, *, status_code=200):
    return FetchPayload(status_code=status_code,
                        url="https://api.adzuna.com/v1/api/jobs/us/search/1",
                        text=json.dumps({"results": results, "count": len(results)}))


class AdzunaCanonicalizationTests(unittest.TestCase):
    # 1 -----------------------------------------------------------------
    def test_valid_record_produces_complete_canonical_job(self):
        job = normalize_adzuna_job(_row())
        for key in ("job_id", "job_title", "employer_name", "job_description",
                    "job_apply_link", "job_location", "job_country",
                    "job_posted_at_datetime_utc", "job_publisher"):
            self.assertTrue(job.get(key), f"missing canonical field {key}")
        self.assertEqual(job["_acquisition_source"], "adzuna")
        self.assertTrue(job["_provider_record_structured"])
        self.assertFalse(job["job_apply_is_direct"])

    # 2 -----------------------------------------------------------------
    def test_provider_id_is_stable_and_deterministic(self):
        self.assertEqual(normalize_adzuna_job(_row("xyz"))["job_id"], "adzuna:xyz")
        self.assertEqual(normalize_adzuna_job(_row("xyz"))["job_id"],
                         normalize_adzuna_job(_row("xyz"))["job_id"])

    # 3 -----------------------------------------------------------------
    def test_company_name_mapped_from_display_name(self):
        self.assertEqual(normalize_adzuna_job(_row(company="Acme Robotics"))["employer_name"],
                         "Acme Robotics")

    # 4 -----------------------------------------------------------------
    def test_redirect_url_mapped_but_not_used_as_employer_website(self):
        job = normalize_adzuna_job(_row("u1"))
        self.assertEqual(job["job_apply_link"], "https://www.adzuna.com/land/ad/u1")
        self.assertEqual(job["canonical_source_url"], job["job_apply_link"])
        # A tracking redirect is never a company domain.
        self.assertEqual(job["employer_website"], "")

    # 5 -----------------------------------------------------------------
    def test_timestamp_normalized_to_utc_iso(self):
        self.assertEqual(normalize_adzuna_job(_row())["job_posted_at_datetime_utc"],
                         "2026-07-20T09:15:00Z")
        # An unparseable timestamp is dropped, never fabricated.
        self.assertEqual(normalize_adzuna_job(_row(created="not-a-date"))["job_posted_at_datetime_utc"], "")

    # 6 -----------------------------------------------------------------
    def test_location_normalized_without_inventing(self):
        self.assertEqual(normalize_adzuna_job(_row())["job_location"], "Remote - United States")
        # Missing location falls back to a country-level label, not a fabricated city.
        blank = dict(_row()); blank["location"] = {}
        self.assertEqual(normalize_adzuna_job(blank)["job_location"], "United States")

    # 7 -----------------------------------------------------------------
    def test_remote_status_not_fabricated(self):
        remote = normalize_adzuna_job(_row(title="Remote Customer Success Manager"))
        self.assertTrue(remote["job_is_remote"])
        onsite = dict(_row(title="Customer Success Manager"))
        onsite["description"] = "Onsite role in our Dallas office."
        self.assertFalse(normalize_adzuna_job(onsite)["job_is_remote"])

    # 8 -----------------------------------------------------------------
    def test_description_preserved(self):
        self.assertIn("synthetic", normalize_adzuna_job(_row())["job_description"].lower())

    # 9 -----------------------------------------------------------------
    def test_missing_optional_fields_do_not_fail(self):
        row = _row(); row.pop("salary_min"); row.pop("salary_max"); row.pop("contract_time")
        job = normalize_adzuna_job(row)
        self.assertIsNotNone(job)
        # Unknown employment type is honest emptiness, not a fabricated FULLTIME.
        self.assertEqual(job["job_employment_type"], "")

    # 10 ----------------------------------------------------------------
    def test_missing_required_id_fails_safely(self):
        row = _row(); row["id"] = ""
        self.assertIsNone(normalize_adzuna_job(row))

    # 11 ----------------------------------------------------------------
    def test_duplicate_adzuna_records_dedupe(self):
        adapter = AdzunaAdapter(settings=_settings(), queries=["Customer Success Manager"])
        result = adapter.fetch(fetcher=lambda *a, **k: _response([_row("d1"), _row("d1"), _row("d2")]))
        self.assertEqual(result.metadata["unique_jobs"], 2)

    # 12 ----------------------------------------------------------------
    def test_same_job_across_adzuna_and_jsearch_shares_dedup_key(self):
        adz = normalize_adzuna_job(_row(title="Staff Accountant", company="Acme Robotics"))
        jsearch_like = {
            "job_id": "jsearch-987", "job_title": "Staff Accountant",
            "employer_name": "Acme Robotics", "job_location": "Remote - United States",
            "_acquisition_source": "jsearch",
        }
        self.assertEqual(dedup_key(adz), dedup_key(jsearch_like))

    # 13 ----------------------------------------------------------------
    def test_malformed_records_cannot_inflate_counts(self):
        adapter = AdzunaAdapter(settings=_settings(), queries=["Customer Success Manager"])
        rows = [_row("ok1"), {"title": "no id"}, {"id": ""}, "not-a-dict", _row("ok2")]
        result = adapter.fetch(fetcher=lambda *a, **k: _response(rows))
        self.assertEqual(result.metadata["unique_jobs"], 2)

    # 14 ----------------------------------------------------------------
    def test_authentication_failure_stops_portfolio(self):
        adapter = AdzunaAdapter(settings=_settings(),
                                queries=["Customer Success Manager", "Staff Accountant"])
        calls = {"n": 0}
        def fetcher(*a, **k):
            calls["n"] += 1
            return _response([], status_code=401)
        result = adapter.fetch(fetcher=fetcher)
        self.assertTrue(result.metadata["auth_failed"])
        self.assertEqual(calls["n"], 1)  # stopped after the first 401

    # 15 ----------------------------------------------------------------
    def test_provider_failure_does_not_crash(self):
        adapter = AdzunaAdapter(settings=_settings(), queries=["Customer Success Manager"])
        result = adapter.fetch(fetcher=lambda *a, **k: _response([], status_code=500))
        self.assertIsNotNone(result)  # returned a SourceResult, did not raise


class AdzunaPostingIntegrityTests(unittest.TestCase):
    """The two job_quality corrections, isolated at the integrity gate."""

    def _integrity(self, job):
        return job_quality.assess_posting_integrity(job)

    def test_structured_adzuna_record_now_passes_integrity(self):
        # Clean company name (no dotted suffix): isolates the trust-list fix.
        job = normalize_adzuna_job(_row(company="Northwind Analytics"))
        self.assertTrue(self._integrity(job).eligible)

    def test_adzuna_not_in_trusted_set_would_fail(self):
        # Prove the dependency: strip the structured-provider signal and it fails.
        job = normalize_adzuna_job(_row(company="Northwind Analytics"))
        job["_acquisition_source"] = "unknown_scraper"
        self.assertFalse(self._integrity(job).eligible)

    def test_dotted_legal_suffix_company_no_longer_untrustworthy(self):
        job = normalize_adzuna_job(_row(company="Trimble Inc."))
        assessment = self._integrity(job)
        self.assertTrue(assessment.eligible, assessment.reason)

    def test_genuine_hostname_employer_still_flagged(self):
        # A real deployment hostname used as the employer name is still caught.
        self.assertEqual(job_quality._url_host("prod.deploy.io"), "prod.deploy.io")
        job = normalize_adzuna_job(_row(company="prod.deploy.io"))
        self.assertFalse(self._integrity(job).eligible)

    def test_url_host_rejects_space_containing_pseudo_hosts(self):
        self.assertEqual(job_quality._url_host("Trimble Inc."), "")
        self.assertEqual(job_quality._url_host("Acme Ltd."), "")
        self.assertEqual(job_quality._url_host("app.company.io"), "app.company.io")

    def test_generic_employer_still_rejected_even_when_structured(self):
        # Trust-list membership never rescues a generic/placeholder employer.
        job = normalize_adzuna_job(_row(company="Confidential"))
        self.assertFalse(self._integrity(job).eligible)


if __name__ == "__main__":
    unittest.main()
