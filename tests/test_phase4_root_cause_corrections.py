"""Regression tests for the Phase 4 audit corrections (tgtc_pipeline_audit only).

Each test traces to one row of audit_output/ROOT_CAUSE_TABLE.md:
- row 4: dead funnel log-line stat keys -> _reason_counts helper in run_daily.py
- row 5: age_recovery not logging its own pass-level company counts
- row 2: expanded bucket/company-level instrumentation for the 66-eligible-to-19-attempts
  reconciliation, including the apollo-search-error-vs-empty-response distinction and the
  per-bucket _row2_diagnostic record persisted on each lead.

The staffing internal-hire carve-out and health-tech vendor carve-out proposed earlier in
Phase 4 were removed after independent review: TGTC's stated policy excludes staffing/
recruiting/RPO companies outright (including their own internal hires), and one sampled
company is insufficient evidence to exempt employers by name-only signal. Health-tech
treatment is recorded as an unresolved business-policy/firmographic-classification
question, not implemented.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import hiring_manager
import run_daily
from apollo_client import OrgEnrichment
from decision_types import GateDecision, GateState
from reroute_state import RerouteRegistry


class FunnelReasonCountsHelperTests(unittest.TestCase):
    def test_aggregates_nonzero_prefixed_keys_only(self):
        stats = {
            "contact_reason__reasoncode_reroute_seniority_mismatch": 5,
            "contact_reason__reasoncode_other": 0,
            "email_reason__reasoncode_unverified_email": 3,
            "person_match_attempts": 19,
        }
        self.assertEqual(
            run_daily._reason_counts(stats, "contact_reason__"),
            {"reasoncode_reroute_seniority_mismatch": 5},
        )
        self.assertEqual(
            run_daily._reason_counts(stats, "email_reason__"),
            {"reasoncode_unverified_email": 3},
        )

    def test_empty_when_no_matching_keys(self):
        self.assertEqual(run_daily._reason_counts({"person_match_attempts": 1}, "contact_reason__"), {})


def _finance_job() -> dict:
    return {
        "job_id": "j1",
        "job_title": "Staff Accountant",
        "canonical_job_title": "Staff Accountant",
        "employer_name": "Acme",
        "canonical_employer_name": "Acme",
        "employer_website": "https://acme.com",
        "_employer_domain_input": "acme.com",
        "_matched_role": "Staff Accountant",
        "_search_role": "Staff Accountant",
        "_job_gate_state": "PASS",
        "_role_gate_state": "PASS",
        "_job_gate_decision": GateDecision("job", GateState.PASS, "JOB_PASS").to_dict(),
        "_role_gate_decision": GateDecision("role", GateState.PASS, "ROLE_PASS").to_dict(),
    }


def _account_pass() -> GateDecision:
    return GateDecision(
        "account", GateState.PASS, "ACCOUNT_PASS",
        metadata={
            "canonical_domain": "acme.com",
            "canonical_company_name": "Acme",
            "business_model": "commercial_product_or_service",
        },
    )


class Row2InstrumentationTests(unittest.TestCase):
    """Row 2: instrumentation only -- must never change person_match_attempts or leads."""

    def _run(self, *, people=None, search_side_effect=None, reroute_seed=None):
        org = OrgEnrichment(
            found=True, name="Acme", domain="acme.com", employee_count=100,
            industry="Software", raw={"description": "Acme builds accounting software."},
        )
        with tempfile.TemporaryDirectory() as temp:
            reroute_path = f"{temp}/reroute.json"
            if reroute_seed:
                registry = RerouteRegistry(reroute_path)
                registry.record("acme.com|" + reroute_seed["bucket"], reroute_seed["ids"], "reasoncode_test")
            search_kwargs = (
                {"side_effect": search_side_effect} if search_side_effect is not None
                else {"return_value": people}
            )
            with (
                patch.object(config, "REROUTE_STATE_FILE", reroute_path),
                patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
                patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
                patch.object(hiring_manager.AccountGate, "evaluate", return_value=_account_pass()),
                patch.object(hiring_manager.apollo, "search_people_at_company", **search_kwargs),
            ):
                leads, stats = hiring_manager.process_company([_finance_job()])
        return leads, stats

    def test_zero_apollo_people_counted(self):
        leads, stats = self._run(people=[])
        self.assertEqual(stats.get("bucket_zero_apollo_people", 0), 1)
        self.assertEqual(stats.get("bucket_no_title_match", 0), 0)
        self.assertEqual(stats.get("bucket_all_candidates_previously_attempted", 0), 0)
        self.assertEqual(stats.get("bucket_apollo_search_error", 0), 0)
        self.assertEqual(stats.get("person_match_attempts", 0), 0)
        self.assertEqual(stats.get("row2_people_search_calls_total", 0), 1)
        self.assertEqual(stats.get("row2_apollo_people_returned_total", 0), 0)
        self.assertEqual(stats.get("row2_buckets_with_apollo_person", 0), 0)
        self.assertEqual(stats.get("row2_companies_with_people_search_call", 0), 1)
        self.assertEqual(stats.get("row2_companies_with_person_returned", 0), 0)
        self.assertEqual(stats.get("row2_companies_with_person_match_attempt", 0), 0)
        diag = leads[0]["_row2_diagnostic"]
        self.assertEqual(diag["terminal_reason"], "zero_apollo_people")
        self.assertFalse(diag["apollo_search_error"])
        self.assertEqual(diag["people_returned"], 0)

    def test_no_title_match_counted(self):
        people = [{"id": "p1", "title": "Barista", "organization": {"name": "Acme", "domain": "acme.com"}}]
        leads, stats = self._run(people=people)
        self.assertEqual(stats.get("bucket_zero_apollo_people", 0), 0)
        self.assertEqual(stats.get("bucket_no_title_match", 0), 1)
        self.assertEqual(stats.get("bucket_all_candidates_previously_attempted", 0), 0)
        self.assertEqual(stats.get("person_match_attempts", 0), 0)
        self.assertEqual(stats.get("row2_apollo_people_returned_total", 0), 1)
        self.assertEqual(stats.get("row2_buckets_with_apollo_person", 0), 1)
        self.assertEqual(stats.get("row2_title_matched_candidates_total", 0), 0)
        self.assertEqual(stats.get("row2_buckets_with_title_match", 0), 0)
        self.assertEqual(stats.get("row2_companies_with_person_returned", 0), 1)
        self.assertEqual(stats.get("row2_companies_with_title_match", 0), 0)
        self.assertEqual(leads[0]["_row2_diagnostic"]["terminal_reason"], "no_title_match")

    def test_all_candidates_previously_attempted_counted(self):
        people = [{"id": "us", "title": "Controller", "organization": {"name": "Acme", "domain": "acme.com"}}]
        bucket = hiring_manager.get_bucket_name_for_job(_finance_job())
        leads, stats = self._run(people=people, reroute_seed={"bucket": bucket, "ids": ["us"]})
        self.assertEqual(stats.get("bucket_zero_apollo_people", 0), 0)
        self.assertEqual(stats.get("bucket_no_title_match", 0), 0)
        self.assertEqual(stats.get("bucket_all_candidates_previously_attempted", 0), 1)
        self.assertEqual(stats.get("person_match_attempts", 0), 0)
        self.assertEqual(stats.get("row2_title_matched_candidates_total", 0), 1)
        self.assertEqual(stats.get("row2_buckets_with_title_match", 0), 1)
        self.assertEqual(stats.get("row2_untried_candidates_total", 0), 0)
        self.assertEqual(stats.get("row2_buckets_with_untried_candidate", 0), 0)
        self.assertEqual(stats.get("row2_companies_with_title_match", 0), 1)
        self.assertEqual(stats.get("row2_companies_with_untried_candidate", 0), 0)
        self.assertEqual(leads[0]["_row2_diagnostic"]["terminal_reason"], "all_candidates_previously_attempted")

    def test_apollo_search_error_not_classified_as_zero_people(self):
        leads, stats = self._run(search_side_effect=RuntimeError("Apollo 500"))
        self.assertEqual(stats.get("bucket_apollo_search_error", 0), 1)
        self.assertEqual(stats.get("bucket_zero_apollo_people", 0), 0)
        self.assertEqual(stats.get("bucket_no_title_match", 0), 0)
        self.assertEqual(stats.get("bucket_all_candidates_previously_attempted", 0), 0)
        self.assertEqual(stats.get("person_match_attempts", 0), 0)
        self.assertEqual(stats.get("row2_people_search_calls_total", 0), 1)
        self.assertEqual(stats.get("row2_apollo_people_returned_total", 0), 0)
        diag = leads[0]["_row2_diagnostic"]
        self.assertTrue(diag["apollo_search_error"])
        self.assertEqual(diag["terminal_reason"], "apollo_search_error")
        self.assertIsNone(diag["people_returned"])
        self.assertIsNone(diag["title_matched_candidates"])

    def test_successful_attempt_records_diagnostic_and_company_counters(self):
        candidates = [
            {"id": "emea", "title": "Controller", "organization": {"name": "Acme", "domain": "acme.com"}},
            {"id": "us", "title": "Controller", "organization": {"name": "Acme", "domain": "acme.com"}},
        ]
        from apollo_client import PersonMatch
        people_matches = [
            PersonMatch(True, person_id="emea", first_name="E", last_name="Mea", title="Controller EMEA", organization_name="Acme", organization_domain="acme.com", email="e@acme.com", email_status="verified", country="United Kingdom", linkedin_url="https://linkedin.com/in/emea", raw={"current_organization": {"name": "Acme", "domain": "acme.com"}}),
            PersonMatch(True, person_id="us", first_name="U", last_name="S", title="Controller", organization_name="Acme", organization_domain="acme.com", email="u@acme.com", email_status="verified", country="United States", linkedin_url="https://linkedin.com/in/us", raw={"current_organization": {"name": "Acme", "domain": "acme.com"}}),
        ]
        org = OrgEnrichment(
            found=True, name="Acme", domain="acme.com", employee_count=100,
            industry="Software", raw={"description": "Acme builds accounting software."},
        )
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(config, "REROUTE_STATE_FILE", f"{temp}/reroute.json"),
                patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
                patch.object(config, "HUNTER_RATE_LIMIT_DELAY", 0),
                patch.object(config, "VERIFY_WITH_HUNTER", False),
                patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
                patch.object(hiring_manager.AccountGate, "evaluate", return_value=_account_pass()),
                patch.object(hiring_manager.apollo, "search_people_at_company", return_value=candidates),
                patch.object(hiring_manager.apollo, "match_person", side_effect=people_matches),
            ):
                leads, stats = hiring_manager.process_company([_finance_job()])
        self.assertEqual(stats.get("person_match_attempts", 0), 2)
        self.assertEqual(stats.get("row2_companies_with_person_match_attempt", 0), 1)
        self.assertEqual(stats.get("row2_buckets_with_untried_candidate", 0), 1)
        diag = leads[0]["_row2_diagnostic"]
        self.assertEqual(diag["terminal_reason"], "attempted")
        self.assertEqual(diag["person_match_attempts"], 2)
        self.assertFalse(diag["apollo_search_error"])


if __name__ == "__main__":
    unittest.main()
