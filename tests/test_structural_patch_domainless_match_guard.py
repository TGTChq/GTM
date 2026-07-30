"""D6: a domainless, fuzzy-matched Apollo organization must not drive an
automatic firmographic reject -- only an exact name match (or any
domain-corroborated match) may.

Traces to ROOT_CAUSE_TABLE_STRUCTURAL.md row 7: a confirmed real false
positive where an "Amazon" job posting matched Apollo's unrelated 22-person
"Amazon Group" and was auto-rejected as too-small.
"""
from __future__ import annotations

import unittest

from account_gate import AccountGate
from apollo_client import OrgEnrichment
from company_source_resolver import CompanySource
from decision_types import GateState


class _Resolver:
    def resolve(self, domain, fetch=None):
        return CompanySource("RESOLVED", domain, "A company.")


class DomainlessNameOnlyMatchGuardTests(unittest.TestCase):
    def test_fuzzy_domainless_match_routes_to_review_not_auto_reject(self):
        """Reproduces the confirmed Amazon -> "Amazon Group" false positive."""
        org = OrgEnrichment(
            found=True, name="Amazon Group", domain="aboutamazon.com",
            employee_count=22, industry="Retail", raw={},
        )
        decision = AccountGate(_Resolver()).evaluate(
            org=org, input_company_name="Amazon", input_domain="", jobs=[],
        )
        self.assertEqual(decision.state, GateState.NEEDS_CHECK)
        self.assertNotEqual(decision.state, GateState.REJECT)

    def test_exact_name_match_with_no_domain_still_auto_rejects(self):
        """NVIDIA case: employer_website blank, but Apollo's resolved name is
        an exact match -- strong enough evidence that the firmographic reject
        must still fire automatically, not be downgraded to review."""
        org = OrgEnrichment(
            found=True, name="NVIDIA", domain="nvidia.com",
            employee_count=30000, industry="Computer Software", raw={},
        )
        decision = AccountGate(_Resolver()).evaluate(
            org=org, input_company_name="NVIDIA", input_domain="", jobs=[],
        )
        self.assertEqual(decision.state, GateState.REJECT)

    def test_domain_corroborated_match_still_auto_rejects(self):
        """When there IS a real input domain to cross-check, the size reject
        must fire automatically even for a fuzzy name match -- this guard is
        scoped strictly to the domainless case."""
        org = OrgEnrichment(
            found=True, name="Acme Group", domain="acme.com",
            employee_count=30000, industry="Computer Software", raw={},
        )
        decision = AccountGate(_Resolver()).evaluate(
            org=org, input_company_name="Acme", input_domain="acme.com", jobs=[],
        )
        self.assertEqual(decision.state, GateState.REJECT)


if __name__ == "__main__":
    unittest.main()
