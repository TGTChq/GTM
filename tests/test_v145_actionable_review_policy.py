from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from account_gate import AccountGate
from apollo_client import OrgEnrichment, PersonMatch
from company_source_resolver import CompanySource
from contact_gate import ContactGate
from decision_types import GateState
from email_gate import EmailGate
from hunter_client import HunterResult
from qualification_pipeline import run_precontact_qualification
from review_policy import is_airtable_reviewable


class _CompanyResolver:
    def resolve(self, domain, fetch=None):
        return CompanySource("SOURCE_UNRESOLVED", domain, "")


class _JobGate:
    def __init__(self, state: str):
        self.state = state

    def annotate(self, job, fetch=None):
        result = dict(job)
        result.update({
            "_job_gate_state": self.state,
            "_job_gate_reason": f"{self.state}_JOB",
            "_job_gate_decision": {
                "gate": "job",
                "state": self.state,
                "primary_reason": f"{self.state}_JOB",
                "metadata": {"source": {"state": "SOURCE_UNRESOLVED"}},
            },
        })
        return result


class _RoleGate:
    def __init__(self, state: str):
        self.state = state

    def annotate(self, job):
        result = dict(job)
        result.update({
            "_role_gate_state": self.state,
            "_role_gate_reason": f"{self.state}_ROLE",
            "_role_gate_decision": {
                "gate": "role",
                "state": self.state,
                "primary_reason": f"{self.state}_ROLE",
            },
        })
        return result


class V145ActionableReviewPolicyTests(unittest.TestCase):
    def _job_file(self, root: Path) -> str:
        path = root / "jobs.json"
        path.write_text(json.dumps({"jobs": [{
            "job_id": "j1",
            "job_title": "Staff Accountant",
            "employer_name": "Example Corp",
            "job_description": "Full-time accounting role for a US software company.",
        }]}), encoding="utf-8")
        return str(path)

    def test_unverified_precontact_evidence_continues_to_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_precontact_qualification(
                self._job_file(Path(tmp)),
                output_dir=tmp,
                job_gate=_JobGate("UNVERIFIED"),
                role_gate=_RoleGate("PASS"),
            )
        self.assertEqual(result.contact_eligible_jobs, 1)
        self.assertEqual(result.rejected_jobs, 0)

    def test_hard_precontact_reject_remains_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_precontact_qualification(
                self._job_file(Path(tmp)),
                output_dir=tmp,
                job_gate=_JobGate("REJECT"),
                role_gate=_RoleGate("PASS"),
            )
        self.assertEqual(result.contact_eligible_jobs, 0)
        self.assertEqual(result.rejected_jobs, 1)

    def test_unknown_firmographics_continue_with_safe_input_domain(self):
        org = OrgEnrichment(
            found=False,
            name=None,
            domain=None,
            employee_count=None,
            industry=None,
            raw={},
        )
        decision = AccountGate(_CompanyResolver()).evaluate(
            org=org,
            input_company_name="Example Corp",
            input_domain="example.com",
            jobs=[],
            fetch_company=False,
        )
        self.assertEqual(decision.state, GateState.NEEDS_CHECK)
        self.assertEqual(decision.metadata["canonical_domain"], "example.com")

    def test_explicit_out_of_range_company_still_rejects(self):
        org = OrgEnrichment(
            found=True,
            name="Example Corp",
            domain="example.com",
            employee_count=5001,
            industry="Computer Software",
            raw={},
        )
        decision = AccountGate(_CompanyResolver()).evaluate(
            org=org,
            input_company_name="Example Corp",
            input_domain="example.com",
            jobs=[],
            fetch_company=False,
        )
        self.assertEqual(decision.state, GateState.REJECT)

    def test_unverified_email_deliverability_is_reviewable(self):
        person = PersonMatch(
            person_found=True,
            first_name="Jane",
            last_name="Doe",
            title="VP Finance",
            organization_name="Example Corp",
            organization_domain="example.com",
            email="jane@example.com",
            email_status="guessed",
            linkedin_url="https://linkedin.com/in/jane",
            raw={},
        )
        decision = EmailGate().evaluate(
            person=person,
            hunter_result=HunterResult(found=False, status="quota_exhausted"),
            company_domains={"example.com"},
        )
        self.assertEqual(decision.state, GateState.NEEDS_CHECK)

    def test_explicit_invalid_email_remains_non_actionable(self):
        person = PersonMatch(
            person_found=True,
            first_name="Jane",
            last_name="Doe",
            title="VP Finance",
            organization_name="Example Corp",
            organization_domain="example.com",
            email="jane@example.com",
            email_status="guessed",
            linkedin_url="https://linkedin.com/in/jane",
            raw={},
        )
        decision = EmailGate().evaluate(
            person=person,
            hunter_result=HunterResult(found=True, email="jane@example.com", status="invalid"),
            company_domains={"example.com"},
        )
        self.assertEqual(decision.state, GateState.REROUTE)

    def test_actionable_unverified_lead_enters_review(self):
        lead = {
            "_final_state": "UNVERIFIED",
            "_gate_decisions": {
                "job": {"state": "UNVERIFIED"},
                "account": {"state": "NEEDS_CHECK"},
                "contact": {"state": "PASS"},
                "email": {"state": "NEEDS_CHECK"},
            },
            "hiring_manager_name": "Jane Doe",
            "hiring_manager_email": "jane@example.com",
            "lead_key": "example.com|jane@example.com|finance",
        }
        self.assertTrue(is_airtable_reviewable(lead))
        lead["_gate_decisions"]["account"]["state"] = "REJECT"
        self.assertFalse(is_airtable_reviewable(lead))

    def test_unverified_us_territory_routes_to_review_not_reroute(self):
        person = PersonMatch(
            person_found=True,
            first_name="Jane",
            last_name="Doe",
            title="VP Sales",
            organization_name="Example Corp",
            organization_domain="example.com",
            email="jane@example.com",
            email_status="verified",
            linkedin_url="https://linkedin.com/in/jane",
            country="Mexico",
            raw={"current_organization": {"name": "Example Corp", "domain": "example.com"}},
        )
        with patch("contact_gate.config.REQUIRE_US_CONTACT_TERRITORY", True):
            decision = ContactGate().evaluate(
                person=person,
                target_titles=["VP Sales"],
                company_domains={"example.com"},
                company_name="Example Corp",
            )
        self.assertEqual(decision.state, GateState.NEEDS_CHECK)

    def test_terminal_reroute_cannot_hide_inside_reviewable_final_state(self):
        lead = {
            "_final_state": "UNVERIFIED",
            "_gate_decisions": {
                "job": {"state": "UNVERIFIED"},
                "contact": {"state": "REROUTE"},
            },
            "hiring_manager_name": "Jane Doe",
            "hiring_manager_email": "jane@example.com",
            "lead_key": "example.com|jane@example.com|finance",
        }
        self.assertFalse(is_airtable_reviewable(lead))

    def test_large_mixed_volume_surfaces_only_actionable_nonterminal_leads(self):
        leads = []
        for index in range(1200):
            if index < 300:
                state, gate_state, email = "FINAL_PASS", "PASS", f"pass{index}@example.com"
            elif index < 700:
                state, gate_state, email = "UNVERIFIED", "UNVERIFIED", f"unknown{index}@example.com"
            elif index < 900:
                state, gate_state, email = "NEEDS_CHECK", "NEEDS_CHECK", f"review{index}@example.com"
            elif index < 1000:
                state, gate_state, email = "REJECT", "REJECT", f"reject{index}@example.com"
            elif index < 1100:
                state, gate_state, email = "UNVERIFIED", "UNVERIFIED", ""
            else:
                state, gate_state, email = "UNVERIFIED", "REROUTE", f"reroute{index}@example.com"
            leads.append({
                "_final_state": state,
                "_gate_decisions": {"account": {"state": gate_state}},
                "hiring_manager_name": "Jane Doe",
                "hiring_manager_email": email,
                "lead_key": f"example.com|{index}|finance",
            })
        reviewable = [lead for lead in leads if is_airtable_reviewable(lead)]
        self.assertEqual(len(reviewable), 900)



if __name__ == "__main__":
    unittest.main()
