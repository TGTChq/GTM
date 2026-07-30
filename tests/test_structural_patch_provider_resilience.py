"""D10: apollo.match_person / hunter provider errors must not abort the run.

Traces to ROOT_CAUSE_TABLE_STRUCTURAL.md row 9.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import hiring_manager
from apollo_client import OrgEnrichment
from decision_types import GateDecision, GateState


def _finance_job() -> dict:
    return {
        "job_id": "j1", "job_title": "Staff Accountant", "canonical_job_title": "Staff Accountant",
        "employer_name": "Acme", "canonical_employer_name": "Acme", "employer_website": "https://acme.com",
        "_employer_domain_input": "acme.com", "_matched_role": "Staff Accountant", "_search_role": "Staff Accountant",
        "_job_gate_state": "PASS", "_role_gate_state": "PASS",
        "_job_gate_decision": GateDecision("job", GateState.PASS, "JOB_PASS").to_dict(),
        "_role_gate_decision": GateDecision("role", GateState.PASS, "ROLE_PASS").to_dict(),
    }


def _account_pass() -> GateDecision:
    return GateDecision(
        "account", GateState.PASS, "ACCOUNT_PASS",
        metadata={"canonical_domain": "acme.com", "canonical_company_name": "Acme", "business_model": "commercial_product_or_service"},
    )


class MatchPersonErrorTests(unittest.TestCase):
    def test_match_person_exception_does_not_abort_and_is_counted(self):
        candidates = [{"id": "p1", "title": "Controller", "organization": {"name": "Acme", "domain": "acme.com"}}]
        org = OrgEnrichment(found=True, name="Acme", domain="acme.com", employee_count=100, industry="Software", raw={})
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(config, "REROUTE_STATE_FILE", f"{temp}/reroute.json"),
                patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
                patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
                patch.object(hiring_manager.AccountGate, "evaluate", return_value=_account_pass()),
                patch.object(hiring_manager.apollo, "search_people_at_company", return_value=candidates),
                patch.object(hiring_manager.apollo, "match_person", side_effect=RuntimeError("Apollo 500")),
            ):
                # Must not raise.
                leads, stats = hiring_manager.process_company([_finance_job()])
        self.assertEqual(stats.get("candidate_match_error", 0), 1)
        self.assertEqual(leads[0]["_final_state"], "UNVERIFIED")


class HunterErrorTests(unittest.TestCase):
    def test_find_email_exception_does_not_abort_and_is_counted(self):
        candidates = [{"id": "p1", "title": "Controller", "organization": {"name": "Acme", "domain": "acme.com"}}]
        org = OrgEnrichment(found=True, name="Acme", domain="acme.com", employee_count=100, industry="Software", raw={})
        from apollo_client import PersonMatch
        person = PersonMatch(
            True, person_id="p1", first_name="A", last_name="B", title="Controller",
            organization_name="Acme", organization_domain="acme.com",
            email=None, email_status=None, country="United States",
            linkedin_url="https://linkedin.com/in/ab", raw={"current_organization": {"name": "Acme", "domain": "acme.com"}},
        )
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(config, "REROUTE_STATE_FILE", f"{temp}/reroute.json"),
                patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
                patch.object(config, "HUNTER_RATE_LIMIT_DELAY", 0),
                patch.object(config, "HUNTER_API_KEY", "test-key"),
                patch.object(config, "VERIFY_WITH_HUNTER", False),
                patch.object(config, "HUNTER_MAX_FALLBACK_ATTEMPTS_PER_BUCKET", 1),
                patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
                patch.object(hiring_manager.AccountGate, "evaluate", return_value=_account_pass()),
                patch.object(hiring_manager.apollo, "search_people_at_company", return_value=candidates),
                patch.object(hiring_manager.apollo, "match_person", return_value=person),
                patch.object(hiring_manager.hunter, "find_email", side_effect=RuntimeError("Hunter down")),
            ):
                leads, stats = hiring_manager.process_company([_finance_job()])
        self.assertEqual(stats.get("email_verify_error", 0), 1)


if __name__ == "__main__":
    unittest.main()
