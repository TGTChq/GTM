import unittest

from apollo_client import PersonMatch
from contact_gate import ContactGate
from decision_types import GateState
from email_gate import EmailGate
from hunter_client import HunterResult


class ContactEmailGateV02Tests(unittest.TestCase):
    def person(self, **overrides):
        values = dict(
            person_found=True,
            first_name="A",
            last_name="Buyer",
            title="VP Sales",
            organization_name="Example Corp",
            organization_domain="example.com",
            email="a@example.com",
            email_status="verified",
            linkedin_url="https://linkedin.com/in/a-buyer",
            country="United States",
            raw={"current_organization": {"name": "Example Corp", "domain": "example.com"}},
        )
        values.update(overrides)
        return PersonMatch(**values)

    def test_us_role_rejects_emea_contact_for_reroute(self):
        decision = ContactGate().evaluate(
            person=self.person(title="VP Sales, EMEA"),
            target_titles=["VP Sales"],
            company_domains={"example.com"},
            company_name="Example Corp",
        )
        self.assertEqual(decision.state, GateState.REROUTE)
        self.assertEqual(str(decision.primary_reason.value), "REROUTE_TERRITORY_MISMATCH")

    def test_global_or_unmarked_functional_contact_passes(self):
        decision = ContactGate().evaluate(
            person=self.person(title="VP Sales"),
            target_titles=["VP Sales"],
            company_domains={"example.com"},
            company_name="Example Corp",
        )
        self.assertEqual(decision.state, GateState.PASS)

    def test_missing_current_org_identity_does_not_pass(self):
        decision = ContactGate().evaluate(
            person=self.person(organization_name=None, organization_domain=None),
            target_titles=["VP Sales"],
            company_domains={"example.com"},
            company_name="Example Corp",
        )
        self.assertEqual(decision.state, GateState.REROUTE)

    def test_wrong_function_reroutes(self):
        decision = ContactGate().evaluate(
            person=self.person(title="Marketing Operations Manager"),
            target_titles=["CIO", "VP Information Technology", "IT Director"],
            company_domains={"example.com"},
            company_name="Example Corp",
        )
        self.assertEqual(decision.state, GateState.REROUTE)

    def test_apollo_verified_email_passes_without_hunter(self):
        decision = EmailGate().evaluate(
            person=self.person(email_status="verified"),
            hunter_result=None,
            company_domains={"example.com"},
        )
        self.assertEqual(decision.state, GateState.PASS)

    def test_hunter_valid_email_passes(self):
        hunter = HunterResult(found=True, email="a@example.com", status="valid")
        decision = EmailGate().evaluate(
            person=self.person(email_status="guessed"),
            hunter_result=hunter,
            company_domains={"example.com"},
        )
        self.assertEqual(decision.state, GateState.PASS)

    def test_accept_all_or_risky_routes_to_review(self):
        hunter = HunterResult(found=True, email="a@example.com", status="accept_all")
        decision = EmailGate().evaluate(
            person=self.person(email_status="guessed"),
            hunter_result=hunter,
            company_domains={"example.com"},
        )
        self.assertEqual(decision.state, GateState.NEEDS_CHECK)

    def test_email_domain_mismatch_reroutes(self):
        decision = EmailGate().evaluate(
            person=self.person(email="a@other.com"),
            hunter_result=None,
            company_domains={"example.com"},
        )
        self.assertEqual(decision.state, GateState.REROUTE)


if __name__ == "__main__":
    unittest.main()
