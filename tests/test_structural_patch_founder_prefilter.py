"""Regression tests for the final structural patch's D2 (founder/CEO pre-filter).

Traces to ROOT_CAUSE_TABLE_STRUCTURAL.md row 2 (47% of person-match attempts
wasted on Founder/CEO candidates at companies already known too large) and
TECHNICAL_DESIGN.md D2.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import hiring_manager
import role_mapping
from apollo_client import OrgEnrichment
from decision_types import GateDecision, GateState


class FounderAllowedHelperTests(unittest.TestCase):
    def test_none_employee_count_is_not_allowed(self):
        self.assertFalse(role_mapping.founder_allowed_for_employee_count(None))

    def test_small_company_is_allowed(self):
        with patch.object(config, "FOUNDER_FALLBACK_MAX_EMPLOYEES", 25):
            self.assertTrue(role_mapping.founder_allowed_for_employee_count(10))

    def test_large_company_is_not_allowed(self):
        with patch.object(config, "FOUNDER_FALLBACK_MAX_EMPLOYEES", 25):
            self.assertFalse(role_mapping.founder_allowed_for_employee_count(200))


class FoundersLastFilteringTests(unittest.TestCase):
    def test_drops_founder_titles_when_not_allowed(self):
        titles = role_mapping._founders_last(
            ["VP Finance", "Controller", "Founder", "CEO"], founder_allowed=False,
        )
        self.assertEqual(titles, ["VP Finance", "Controller"])

    def test_keeps_founder_titles_last_when_allowed(self):
        titles = role_mapping._founders_last(
            ["VP Finance", "Founder", "Controller", "CEO"], founder_allowed=True,
        )
        self.assertEqual(titles, ["VP Finance", "Controller", "Founder", "CEO"])

    def test_get_target_titles_for_jobs_excludes_founders_for_large_company(self):
        job = {"_matched_role": "Staff Accountant", "job_title": "Staff Accountant", "job_description": ""}
        with patch.object(config, "FOUNDER_FALLBACK_MAX_EMPLOYEES", 25):
            titles = role_mapping.get_target_titles_for_jobs([job], employee_count=500)
        self.assertFalse(any(role_mapping.is_founder_tier_title(t) for t in titles))

    def test_get_target_titles_for_jobs_includes_founders_for_small_company(self):
        job = {"_matched_role": "Staff Accountant", "job_title": "Staff Accountant", "job_description": ""}
        with patch.object(config, "FOUNDER_FALLBACK_MAX_EMPLOYEES", 25):
            titles = role_mapping.get_target_titles_for_jobs([job], employee_count=10)
        self.assertTrue(any(role_mapping.is_founder_tier_title(t) for t in titles))


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


class StrictPathFounderPrefilterTests(unittest.TestCase):
    """End-to-end: a too-large company must never spend an Apollo match_person
    call on a founder/CEO-titled candidate, and the search itself should not
    even request founder-tier titles."""

    def test_founder_candidate_excluded_at_search_and_ranking_for_large_company(self):
        # Apollo (mocked) ignores target_titles and returns a founder anyway.
        # Because target_titles no longer contains founder-tier terms at all,
        # rank_candidates' own title-matching already excludes this candidate
        # before the defensive post-rank filter would even run -- confirming
        # the fix works at the earliest possible point, not just as a
        # last-resort safety net.
        candidates = [{"id": "f1", "title": "Co-Founder & CEO", "organization": {"name": "Acme", "domain": "acme.com"}}]
        org = OrgEnrichment(found=True, name="Acme", domain="acme.com", employee_count=5000, industry="Software", raw={})
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(config, "REROUTE_STATE_FILE", f"{temp}/reroute.json"),
                patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
                patch.object(config, "FOUNDER_FALLBACK_MAX_EMPLOYEES", 25),
                patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
                patch.object(hiring_manager.AccountGate, "evaluate", return_value=_account_pass()),
                patch.object(hiring_manager.apollo, "search_people_at_company", return_value=candidates) as search_mock,
                patch.object(hiring_manager.apollo, "match_person") as match_person,
            ):
                leads, stats = hiring_manager.process_company([_finance_job()])
        match_person.assert_not_called()
        self.assertEqual(stats.get("person_match_attempts", 0), 0)
        self.assertEqual(stats.get("bucket_no_title_match", 0), 1)
        # The search query itself should not have requested founder-tier titles.
        called_titles = search_mock.call_args[0][1]
        self.assertFalse(any(role_mapping.is_founder_tier_title(t) for t in called_titles))

    def test_defensive_post_rank_filter_catches_a_founder_that_still_ranks(self):
        # Simulates the rare case rank_candidates() still matches a founder-tier
        # candidate on some other criterion despite target_titles exclusion
        # (e.g. Apollo's own similar-title expansion). The defensive filter in
        # _process_company_strict must still catch it before an attempt.
        org = OrgEnrichment(found=True, name="Acme", domain="acme.com", employee_count=5000, industry="Software", raw={})
        founder_candidate = {"id": "f1", "title": "Founder & CEO", "organization": {"name": "Acme", "domain": "acme.com"}}
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(config, "REROUTE_STATE_FILE", f"{temp}/reroute.json"),
                patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
                patch.object(config, "FOUNDER_FALLBACK_MAX_EMPLOYEES", 25),
                patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
                patch.object(hiring_manager.AccountGate, "evaluate", return_value=_account_pass()),
                patch.object(hiring_manager.apollo, "search_people_at_company", return_value=[founder_candidate]),
                patch.object(hiring_manager, "rank_candidates", return_value=[founder_candidate]),
                patch.object(hiring_manager.apollo, "match_person") as match_person,
            ):
                leads, stats = hiring_manager.process_company([_finance_job()])
        match_person.assert_not_called()
        self.assertEqual(stats.get("person_match_attempts", 0), 0)
        self.assertEqual(stats.get("bucket_founder_tier_prefiltered", 0), 1)

    def test_founder_candidate_attempted_at_small_company(self):
        candidates = [{"id": "f1", "title": "Co-Founder & CEO", "organization": {"name": "Acme", "domain": "acme.com"}}]
        org = OrgEnrichment(found=True, name="Acme", domain="acme.com", employee_count=5, industry="Software", raw={})
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(config, "REROUTE_STATE_FILE", f"{temp}/reroute.json"),
                patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
                patch.object(config, "FOUNDER_FALLBACK_MAX_EMPLOYEES", 25),
                patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
                patch.object(hiring_manager.AccountGate, "evaluate", return_value=_account_pass()),
                patch.object(hiring_manager.apollo, "search_people_at_company", return_value=candidates) as search_mock,
                patch.object(hiring_manager.apollo, "match_person") as match_person,
            ):
                hiring_manager.process_company([_finance_job()])
        match_person.assert_called()
        called_titles = search_mock.call_args[0][1]
        self.assertTrue(any(role_mapping.is_founder_tier_title(t) for t in called_titles))


if __name__ == "__main__":
    unittest.main()
