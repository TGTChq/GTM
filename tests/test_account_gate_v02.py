import unittest

from account_gate import AccountGate
from apollo_client import OrgEnrichment
from company_source_resolver import CompanySource
from decision_types import GateState


class _Resolver:
    def __init__(self, text="A commercial software platform for finance teams and modern businesses."):
        self.text = text
    def resolve(self, domain, fetch=None):
        return CompanySource("RESOLVED", domain, self.text)


def org(**overrides):
    values = dict(
        found=True,
        name="Example Corp",
        domain="example.com",
        employee_count=100,
        industry="Computer Software",
        raw={"short_description": "A software company building workflow products for business teams."},
    )
    values.update(overrides)
    return OrgEnrichment(**values)


class AccountGateV02Tests(unittest.TestCase):
    def test_known_in_range_commercial_account_passes(self):
        decision = AccountGate(_Resolver("We build finance workflow software for modern commercial businesses. Our product automates reporting and planning.")) .evaluate(
            org=org(), input_company_name="Example Corp", input_domain="example.com", jobs=[]
        )
        self.assertEqual(decision.state, GateState.PASS)

    def test_unknown_employee_count_abstains(self):
        decision = AccountGate(_Resolver()).evaluate(
            org=org(employee_count=None), input_company_name="Example Corp", input_domain="example.com", jobs=[]
        )
        self.assertEqual(decision.state, GateState.UNVERIFIED)
        self.assertEqual(str(decision.primary_reason.value), "UNVERIFIED_EMPLOYEE_COUNT")

    def test_too_large_rejects(self):
        decision = AccountGate(_Resolver()).evaluate(
            org=org(employee_count=1001), input_company_name="Example Corp", input_domain="example.com", jobs=[]
        )
        self.assertEqual(decision.state, GateState.REJECT)

    def test_staffing_business_model_rejects(self):
        decision = AccountGate(_Resolver(
            "We are a recruiting agency. Our staffing services place professionals with client companies."
        )).evaluate(org=org(), input_company_name="Example Corp", input_domain="example.com", jobs=[])
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_STAFFING")

    def test_vertical_healthcare_software_rejects(self):
        decision = AccountGate(_Resolver(
            "Practice management software designed for therapists and healthcare providers. We streamline patient management."
        )).evaluate(org=org(), input_company_name="Example Corp", input_domain="example.com", jobs=[])
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_HEALTHCARE")

    def test_horizontal_software_with_healthcare_customers_does_not_reject(self):
        decision = AccountGate(_Resolver(
            "We build a horizontal cloud security platform for companies across finance, retail, manufacturing, and healthcare."
        )).evaluate(org=org(), input_company_name="Example Corp", input_domain="example.com", jobs=[])
        self.assertEqual(decision.state, GateState.PASS)

    def test_generic_company_page_without_business_model_abstains(self):
        decision = AccountGate(_Resolver(
            "Welcome to Example Corp. Meet our team, read our news, view careers, and contact us today."
        )).evaluate(org=org(raw={}), input_company_name="Example Corp", input_domain="example.com", jobs=[])
        self.assertEqual(decision.state, GateState.UNVERIFIED)
        self.assertEqual(str(decision.primary_reason.value), "UNVERIFIED_BUSINESS_MODEL")

    def test_government_contractor_rejects(self):
        decision = AccountGate(_Resolver(
            "We are a government contractor delivering technology services to federal agencies under GSA contract vehicles."
        )).evaluate(org=org(), input_company_name="Example Corp", input_domain="example.com", jobs=[])
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_GOVERNMENT")

    def test_online_media_industry_rejects(self):
        decision = AccountGate(_Resolver(
            "We publish real-time financial news and market information for investors."
        )).evaluate(
            org=org(industry="Online Media"),
            input_company_name="Example Corp",
            input_domain="example.com",
            jobs=[],
        )
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_EXCLUDED_INDUSTRY")


if __name__ == "__main__":
    unittest.main()
