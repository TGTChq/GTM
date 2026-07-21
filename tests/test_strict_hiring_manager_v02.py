from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import hiring_manager
from apollo_client import OrgEnrichment, PersonMatch
from decision_types import GateDecision, GateState


class StrictHiringManagerV02Tests(unittest.TestCase):
    def test_territory_mismatch_reroutes_to_second_valid_contact(self):
        job_gate = GateDecision("job", GateState.PASS, "JOB_PASS").to_dict()
        role_gate = GateDecision("role", GateState.PASS, "ROLE_PASS").to_dict()
        job = {
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
            "_job_gate_decision": job_gate,
            "_role_gate_decision": role_gate,
        }
        org = OrgEnrichment(
            found=True, name="Acme", domain="acme.com", employee_count=100,
            industry="Software", raw={"description": "Acme builds accounting software for businesses."},
        )
        account = GateDecision(
            "account", GateState.PASS, "ACCOUNT_PASS",
            metadata={
                "canonical_domain": "acme.com",
                "canonical_company_name": "Acme",
                "business_model": "commercial_product_or_service",
            },
        )
        candidates = [
            {"id": "emea", "title": "Controller", "organization": {"name": "Acme", "domain": "acme.com"}},
            {"id": "us", "title": "Controller", "organization": {"name": "Acme", "domain": "acme.com"}},
        ]
        people = [
            PersonMatch(True, person_id="emea", first_name="E", last_name="Mea", title="Controller EMEA", organization_name="Acme", organization_domain="acme.com", email="e@acme.com", email_status="verified", country="United Kingdom"),
            PersonMatch(True, person_id="us", first_name="U", last_name="S", title="Controller", organization_name="Acme", organization_domain="acme.com", email="u@acme.com", email_status="verified", country="United States"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(config, "REROUTE_STATE_FILE", f"{temp}/reroute.json"),
                patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
                patch.object(config, "HUNTER_RATE_LIMIT_DELAY", 0),
                patch.object(config, "VERIFY_WITH_HUNTER", False),
                patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
                patch.object(hiring_manager.AccountGate, "evaluate", return_value=account),
                patch.object(hiring_manager.apollo, "search_people_at_company", return_value=candidates),
                patch.object(hiring_manager.apollo, "match_person", side_effect=people) as match_mock,
            ):
                leads, stats = hiring_manager.process_company([job])
        self.assertEqual(match_mock.call_count, 2)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["_final_state"], "FINAL_PASS")
        self.assertEqual(leads[0]["hiring_manager_email"], "u@acme.com")
        self.assertEqual(stats["person_match_attempts"], 2)

    def test_unknown_account_never_calls_people_search(self):
        job = {
            "job_id": "j1", "job_title": "Staff Accountant", "employer_name": "Acme",
            "employer_website": "https://acme.com", "_matched_role": "Staff Accountant",
            "_job_gate_state": "PASS", "_role_gate_state": "PASS",
            "_job_gate_decision": GateDecision("job", GateState.PASS, "JOB_PASS").to_dict(),
            "_role_gate_decision": GateDecision("role", GateState.PASS, "ROLE_PASS").to_dict(),
        }
        account = GateDecision("account", GateState.UNVERIFIED, "UNVERIFIED_EMPLOYEE_COUNT")
        with (
            patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
            patch.object(hiring_manager.apollo, "enrich_organization", return_value=OrgEnrichment(found=False)),
            patch.object(hiring_manager.AccountGate, "evaluate", return_value=account),
            patch.object(hiring_manager.apollo, "search_people_at_company") as search_mock,
        ):
            leads, _stats = hiring_manager.process_company([job])
        search_mock.assert_not_called()
        self.assertEqual(leads[0]["_final_state"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
