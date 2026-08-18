from __future__ import annotations

import argparse
import unittest
from unittest import mock

import fantastic_jobs_adapter
import run_orchestrator
from free_job_sources import SourceResult
from orchestrator.adapters_real import real_fantastic_runner


class _Manager:
    budget = None


class FantasticLaneRunnerTests(unittest.TestCase):
    def _run(self, result=None, exc=None):
        def fake(*_a, **_k):
            if exc:
                raise exc
            return result
        with mock.patch.object(fantastic_jobs_adapter, "run_fantastic_jobs_acquisition", fake):
            runner = real_fantastic_runner()
            return runner(_Manager())

    def test_aggregates_jobs_and_reports_quota(self):
        sr = SourceResult(
            source="fantastic_jobs",
            jobs=[{"job_id": "fantastic_1"}, {"job_id": "fantastic_2"}],
            success=True, requests_attempted=3,
            metadata={"requests_attempted": 3, "stop_reason": "complete",
                      "jobs_quota_remaining": 500, "requests_quota_remaining": 40},
        )
        res = self._run(sr)
        self.assertEqual(res.lane, "fantastic")
        self.assertEqual(res.status, "complete")
        self.assertEqual(len(res.jobs), 2)
        self.assertEqual(res.physical_requests, 3)
        self.assertEqual(res.attribution["source"], "fantastic_jobs")
        self.assertEqual(res.attribution["jobs_quota_remaining"], 500)

    def test_partial_when_jobs_present_but_not_success(self):
        sr = SourceResult(source="fantastic_jobs", jobs=[{"job_id": "x"}], success=False,
                          errors=["fantastic_jobs_linkedin:http_response:http_500"],
                          metadata={"requests_attempted": 1})
        res = self._run(sr)
        self.assertEqual(res.status, "partial")
        self.assertEqual(res.errors, ["fantastic_jobs_linkedin:http_response:http_500"])

    def test_failed_when_no_jobs_and_not_success(self):
        sr = SourceResult(source="fantastic_jobs", jobs=[], success=False,
                          errors=["auth_failed_status_401"])
        res = self._run(sr)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.jobs, [])

    def test_exception_is_contained_as_failed(self):
        res = self._run(exc=RuntimeError("boom"))
        self.assertEqual(res.status, "failed")
        self.assertTrue(any("RuntimeError" in e for e in res.errors))
        self.assertEqual(res.jobs, [])

    def test_raw_jobs_are_role_classified_for_the_role_gate(self):
        # Fantastic postings arrive without a target role; the lane must classify
        # them so the RoleGate (a verifier) can accept target roles and reject
        # non-target titles instead of blanket UNVERIFIED_ROLE_CLASSIFICATION.
        sr = SourceResult(source="fantastic_jobs", success=True, metadata={"requests_attempted": 1}, jobs=[
            {"job_id": "fantastic_1", "job_title": "Account Executive", "employer_name": "Acme",
             "_acquisition_source": "fantastic_jobs_linkedin"},
            {"job_id": "fantastic_2", "job_title": "Kitchen Manager", "employer_name": "Diner",
             "_acquisition_source": "fantastic_jobs_linkedin"},
        ])
        res = self._run(sr)
        self.assertEqual(res.attribution["role_classified"], 2)
        by_title = {j["job_title"]: j for j in res.jobs}
        self.assertTrue(by_title["Account Executive"].get("_matched_role"))
        self.assertEqual(by_title["Account Executive"]["_role_relevance_status"], "accept")
        self.assertTrue(by_title["Kitchen Manager"].get("_matched_role"))     # least-bad target
        self.assertEqual(by_title["Kitchen Manager"]["_role_relevance_status"], "reject")


class _Policy:
    allow_enrichment = False
    allow_airtable_write = False


class FantasticPreflightGatingTests(unittest.TestCase):
    def _args(self, lanes):
        return argparse.Namespace(lanes=lanes, airtable_write=False, artifact_root=".")

    def _res(self, **overrides):
        base = {
            "integrity_ok": True, "boards_ok": False, "writable": True, "free_ok": True,
            "lock": {}, "lock_blocks": False,
            "RAPIDAPI_KEY": True, "APOLLO_API_KEY": True, "HUNTER_API_KEY": True,
            "AIRTABLE_TOKEN": True, "AIRTABLE_BASE_ID": True, "AIRTABLE_TABLE_NAME": True,
            "FANTASTIC_JOBS_API_KEY": True,
        }
        base.update(overrides)
        return base

    def test_fantastic_only_not_blocked_by_missing_boards(self):
        with mock.patch.object(run_orchestrator, "_preflight_checks",
                               return_value=(self._res(boards_ok=False), [])):
            rc = run_orchestrator._strict_preflight(self._args("fantastic"), _Policy())
        self.assertEqual(rc, 0)

    def test_fantastic_requires_api_key(self):
        with mock.patch.object(run_orchestrator, "_preflight_checks",
                               return_value=(self._res(FANTASTIC_JOBS_API_KEY=False), [])):
            rc = run_orchestrator._strict_preflight(self._args("fantastic"), _Policy())
        self.assertEqual(rc, 2)

    def test_ats_still_requires_boards(self):
        with mock.patch.object(run_orchestrator, "_preflight_checks",
                               return_value=(self._res(boards_ok=False), [])):
            rc = run_orchestrator._strict_preflight(self._args("ats"), _Policy())
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
