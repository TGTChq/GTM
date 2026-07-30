"""Two mandatory open items from FINAL_30_PLUS_SYSTEM_SPEC.md:

1. Cornerstone OnDemand (csod.com) tenant-based ATS support, discovered via
   the 65-company domain-recovery classification ("world bank group" hosted
   at worldbankgroup.csod.com -- a platform this codebase had no adapter
   for at all).
2. Closure/removal detection for direct ATS boards (confirmed gap: none of
   the 8 existing providers detected a job disappearing between fetches).

UNVERIFIED-OFFLINE note: Cornerstone OnDemand's real public career-site JSON
API response shape has not been confirmed against a live tenant in this
task -- fetch_board_jobs()'s cornerstone_ondemand branch targets the
commonly-documented convention and fails gracefully (a clear parse error,
not fabricated data) if a real tenant's shape differs. Correct once verified
via the spec's Section 27 controlled live validation.
"""
from __future__ import annotations

import json
import tempfile
import unittest

import config
from ats_board_registry import (
    AtsBoardRegistry,
    _cornerstone_ondemand_tenant_domain_candidate,
    _direct_job,
    _valid_cornerstone_identifier,
    detect_board_ref,
    fetch_board_jobs,
)
from company_identity import safe_company_domain


class _FakeResponse:
    def __init__(self, status_code=200, text="", error=""):
        self.status_code = status_code
        self.text = text
        self.error = error


class CornerstoneBoardDetectionTests(unittest.TestCase):
    def test_detects_tenant_and_site_from_careersite_url(self):
        ref = detect_board_ref(
            "https://worldbankgroup.csod.com/ux/ats/careersite/1/requisition/12345/job-title"
        )
        self.assertIsNotNone(ref)
        self.assertEqual(ref.provider, "cornerstone_ondemand")
        self.assertEqual(ref.identifier, "worldbankgroup|1")

    def test_no_site_id_returns_none(self):
        self.assertIsNone(detect_board_ref("https://worldbankgroup.csod.com/ux/ats/home"))

    def test_non_csod_host_is_not_matched(self):
        self.assertIsNone(detect_board_ref("https://worldbankgroup.example.com/careersite/1"))

    def test_valid_cornerstone_identifier(self):
        self.assertTrue(_valid_cornerstone_identifier("worldbankgroup|1"))
        self.assertFalse(_valid_cornerstone_identifier("worldbankgroup"))
        self.assertFalse(_valid_cornerstone_identifier(""))

    def test_csod_shared_domain_is_never_a_safe_company_domain(self):
        """csod.com itself (the shared platform domain) must never be
        treated as a recovered employer domain -- same guard already applied
        to myworkdayjobs.com."""
        candidate = safe_company_domain(
            "https://worldbankgroup.csod.com/ux/ats/careersite/1/home",
            config.INTERMEDIARY_JOB_DOMAINS,
        )
        self.assertEqual(candidate, "")


class CornerstoneTenantDomainCandidateTests(unittest.TestCase):
    def test_derives_domain_from_tenant(self):
        self.assertEqual(_cornerstone_ondemand_tenant_domain_candidate("worldbankgroup|1"), "worldbankgroup.com")

    def test_empty_identifier_returns_empty(self):
        self.assertEqual(_cornerstone_ondemand_tenant_domain_candidate(""), "")

    def test_direct_job_sets_tenant_candidate_when_domain_missing(self):
        board = {
            "provider": "cornerstone_ondemand",
            "identifier": "worldbankgroup|1",
            "company_name": "World Bank Group",
        }
        job = _direct_job(
            provider="cornerstone_ondemand", board=board, job_id="1",
            title="Analyst", description="", url="https://worldbankgroup.csod.com/ux/ats/careersite/1/requisition/1",
        )
        self.assertEqual(job["_ats_tenant_domain_candidate"], "worldbankgroup.com")

    def test_direct_job_skips_candidate_when_registry_already_has_domain(self):
        board = {
            "provider": "cornerstone_ondemand",
            "identifier": "worldbankgroup|1",
            "company_name": "World Bank Group",
            "company_domain": "worldbank.org",
        }
        job = _direct_job(
            provider="cornerstone_ondemand", board=board, job_id="1",
            title="Analyst", description="", url="x",
        )
        self.assertEqual(job["_ats_tenant_domain_candidate"], "")


class CornerstoneFetchBoardJobsTests(unittest.TestCase):
    def test_fetches_and_normalizes_paginated_jobs(self):
        board = {
            "provider": "cornerstone_ondemand",
            "identifier": "worldbankgroup|1",
            "company_name": "World Bank Group",
            "api_base": "https://worldbankgroup.csod.com",
        }
        pages = {
            1: {"requisitions": [{"reqId": "1", "title": "Analyst", "location": "Washington, DC"}] * 25},
            2: {"requisitions": [{"reqId": "2", "title": "Associate", "location": "Remote"}]},
        }

        def fetcher(url, method="GET", params=None, headers=None, **_kwargs):
            page = int((params or {}).get("page", 1))
            return _FakeResponse(text=json.dumps(pages.get(page, {"requisitions": []})))

        jobs, error = fetch_board_jobs(board, fetcher=fetcher)
        self.assertEqual(error, "")
        self.assertEqual(len(jobs), 26)
        self.assertTrue(all(job["_ats_provider"] == "cornerstone_ondemand" for job in jobs))

    def test_missing_identifier_separator_is_rejected(self):
        board = {"provider": "cornerstone_ondemand", "identifier": "nosite", "company_name": "Acme"}
        jobs, error = fetch_board_jobs(board, fetcher=lambda *a, **k: _FakeResponse())
        self.assertEqual(jobs, [])
        self.assertEqual(error, "invalid_cornerstone_identifier")

    def test_unexpected_response_shape_fails_gracefully_not_silently(self):
        board = {
            "provider": "cornerstone_ondemand", "identifier": "acme|1",
            "company_name": "Acme", "api_base": "https://acme.csod.com",
        }

        def fetcher(url, **_kwargs):
            return _FakeResponse(text="not json")

        jobs, error = fetch_board_jobs(board, fetcher=fetcher)
        self.assertEqual(jobs, [])
        self.assertIn("invalid_json", error)


class AtsBoardRegistryClosureDetectionTests(unittest.TestCase):
    def _registry(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        registry = AtsBoardRegistry(f"{temp.name}/ats_board_registry.json")
        registry.entries["greenhouse:acme"] = {
            "key": "greenhouse:acme", "provider": "greenhouse", "identifier": "acme",
            "company_name": "Acme", "company_domain": "acme.com",
        }
        return registry

    def test_no_job_ids_supplied_makes_no_closure_claim(self):
        registry = self._registry()
        closed = registry.record_result("greenhouse:acme", success=True, job_count=3, save=False)
        self.assertEqual(closed, 0)
        self.assertNotIn("closed_or_removed_job_ids", registry.entries["greenhouse:acme"])

    def test_detects_ids_absent_on_a_later_fetch(self):
        registry = self._registry()
        registry.record_result("greenhouse:acme", success=True, job_count=3, job_ids={"a", "b", "c"}, save=False)
        closed = registry.record_result("greenhouse:acme", success=True, job_count=2, job_ids={"a", "b"}, save=False)
        self.assertEqual(closed, 1)
        self.assertEqual(registry.entries["greenhouse:acme"]["closed_or_removed_job_ids"], 1)

    def test_first_ever_snapshot_claims_no_closures(self):
        """Nothing to compare against yet -- must not report every job as
        'closed' on the very first fetch."""
        registry = self._registry()
        closed = registry.record_result("greenhouse:acme", success=True, job_count=3, job_ids={"a", "b", "c"}, save=False)
        self.assertEqual(closed, 0)

    def test_failed_fetch_does_not_affect_closure_tracking(self):
        registry = self._registry()
        registry.record_result("greenhouse:acme", success=True, job_count=2, job_ids={"a", "b"}, save=False)
        registry.record_result("greenhouse:acme", success=False, error="timeout", save=False)
        closed = registry.record_result("greenhouse:acme", success=True, job_count=1, job_ids={"a"}, save=False)
        self.assertEqual(closed, 1)

    def test_unknown_key_returns_zero_without_error(self):
        registry = self._registry()
        self.assertEqual(registry.record_result("missing:key", success=True, job_ids={"a"}, save=False), 0)


if __name__ == "__main__":
    unittest.main()
