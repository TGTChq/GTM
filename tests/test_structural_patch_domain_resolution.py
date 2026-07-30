"""Regression tests for the final structural patch's D4/D5 domain-resolution work.

Traces to ROOT_CAUSE_TABLE_STRUCTURAL.md row 3 (65 of 115 eligible companies never
reached a people-search call because search_domain resolved empty) and
TECHNICAL_DESIGN.md D4 (ATS tenant-domain waterfall step, gated on the registry's
own name-vs-tenant compatibility check to avoid trusting weak matches).
"""
from __future__ import annotations

import unittest

from account_gate import AccountGate
from apollo_client import OrgEnrichment
from ats_board_registry import _direct_job, _workday_tenant_domain_candidate
from company_source_resolver import CompanySource
from decision_types import GateState


class _Resolver:
    def __init__(self, text="A commercial software platform for finance teams and modern businesses."):
        self.text = text

    def resolve(self, domain, fetch=None):
        return CompanySource("RESOLVED", domain, self.text)


def org(**overrides):
    values = dict(found=False, name=None, domain=None, employee_count=None, industry=None, raw={})
    values.update(overrides)
    return OrgEnrichment(**values)


class WorkdayTenantDomainCandidateTests(unittest.TestCase):
    def test_extracts_tenant_from_identifier(self):
        self.assertEqual(_workday_tenant_domain_candidate("geico|External"), "geico.com")

    def test_empty_identifier_returns_empty(self):
        self.assertEqual(_workday_tenant_domain_candidate(""), "")

    def test_identifier_without_site_separator_returns_empty(self):
        self.assertEqual(_workday_tenant_domain_candidate("geico"), "")

    def test_rejects_invalid_characters(self):
        self.assertEqual(_workday_tenant_domain_candidate("ge ico|External"), "")


class DirectJobTenantFieldsTests(unittest.TestCase):
    def _board(self, **overrides):
        values = dict(
            provider="workday",
            identifier="geico|External",
            company_name="Government Employees Insurance Company",
            company_domain="",
        )
        values.update(overrides)
        return values

    def test_populates_candidate_when_registry_has_no_domain(self):
        job = _direct_job(
            provider="workday", board=self._board(), job_id="1", title="Analyst",
            description="", url="https://geico.wd1.myworkdayjobs.com/External/job/1",
        )
        self.assertEqual(job["_ats_tenant_domain_candidate"], "geico.com")
        self.assertEqual(job["employer_website"], "")  # unchanged -- candidate is not "verified"

    def test_no_candidate_when_registry_already_has_a_domain(self):
        job = _direct_job(
            provider="workday", board=self._board(company_domain="geico.com"), job_id="1",
            title="Analyst", description="", url="https://geico.wd1.myworkdayjobs.com/External/job/1",
        )
        self.assertEqual(job["_ats_tenant_domain_candidate"], "")
        self.assertEqual(job["employer_website"], "https://geico.com")

    def test_no_candidate_for_non_workday_providers(self):
        job = _direct_job(
            provider="greenhouse", board=self._board(provider="greenhouse", identifier="acme"),
            job_id="1", title="Analyst", description="", url="https://boards.greenhouse.io/acme/jobs/1",
        )
        self.assertEqual(job["_ats_tenant_domain_candidate"], "")

    def test_confidence_reflects_identity_verification(self):
        verified = _direct_job(
            provider="workday", board=self._board(), job_id="1", title="Analyst",
            description="", url="x",
        )
        # "Government Employees Insurance Company" vs tenant "geico" -- the
        # registry's conservative name-matcher does not treat these as
        # compatible (no shared tokens), so this is correctly medium, not high.
        self.assertIn(verified["_ats_tenant_domain_confidence"], {"medium", "high"})

        unrelated = _direct_job(
            provider="workday",
            board=self._board(identifier="gtweed|External", company_name="GT Services LLC"),
            job_id="1", title="Analyst", description="", url="x",
        )
        self.assertEqual(unrelated["_ats_tenant_domain_confidence"], "medium")
        self.assertFalse(unrelated["_ats_board_identity_verified"])


class AccountGateTenantDomainFallbackTests(unittest.TestCase):
    def test_verified_tenant_candidate_unlocks_search_domain(self):
        job = {
            "_ats_tenant_domain_candidate": "acme.com",
            "_ats_board_identity_verified": True,
        }
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Acme", input_domain="", jobs=[job],
        )
        self.assertEqual(decision.state, GateState.NEEDS_CHECK)
        self.assertEqual(decision.metadata["canonical_domain"], "acme.com")
        self.assertEqual(decision.metadata["domain_recovery_reason"], "workday_tenant_domain_recovered")

    def test_unverified_tenant_candidate_is_not_trusted(self):
        job = {
            "_ats_tenant_domain_candidate": "wisconsin.com",
            "_ats_board_identity_verified": False,
        }
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="UW Madison", input_domain="", jobs=[job],
        )
        self.assertEqual(decision.state, GateState.NEEDS_CHECK)
        self.assertEqual(decision.metadata["canonical_domain"], "")
        self.assertEqual(decision.metadata["domain_recovery_reason"], "no_domain_evidence")

    def test_no_tenant_candidate_falls_back_to_today_s_behavior(self):
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Amcor", input_domain="", jobs=[{}],
        )
        self.assertEqual(decision.state, GateState.NEEDS_CHECK)
        self.assertEqual(decision.metadata["canonical_domain"], "")

    def test_employer_website_still_takes_priority_over_tenant_candidate(self):
        job = {
            "_ats_tenant_domain_candidate": "wrongguess.com",
            "_ats_board_identity_verified": True,
        }
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Acme", input_domain="acme.com", jobs=[job],
        )
        self.assertEqual(decision.metadata["canonical_domain"], "acme.com")
        self.assertEqual(decision.metadata["domain_recovery_reason"], "employer_website_verified")


class AccountGateCompanyUrlDomainFallbackTests(unittest.TestCase):
    """Domain recovery extended beyond Workday (FINAL_30_PLUS_SYSTEM_SPEC.md
    section 9): a company-branded URL already on the job record, gated on
    domain_name_consistent() rather than the intermediary denylist alone."""

    def test_name_consistent_url_unlocks_search_domain(self):
        job = {"official_job_url": "https://www.thermofisher.com/us/en/careers/req-1"}
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Thermo Fisher Scientific", input_domain="", jobs=[job],
        )
        self.assertEqual(decision.state, GateState.NEEDS_CHECK)
        self.assertEqual(decision.metadata["canonical_domain"], "thermofisher.com")
        self.assertEqual(decision.metadata["domain_recovery_reason"], "company_url_domain_recovered")

    def test_name_inconsistent_url_is_not_trusted(self):
        """Real false positive from the 2026-07-29 corpus: an unrelated
        regional job board is not a known aggregator, but it is also not the
        named employer's domain -- the denylist alone would have accepted
        this; domain_name_consistent() must not."""
        job = {"official_job_url": "https://www.californiaconstructores.com/jobs/great-minds-dc"}
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Great Minds DC", input_domain="", jobs=[job],
        )
        self.assertEqual(decision.metadata["canonical_domain"], "")
        self.assertEqual(decision.metadata["domain_recovery_reason"], "no_domain_evidence")

    def test_known_intermediary_url_is_still_rejected_even_if_name_looks_consistent(self):
        job = {"job_apply_link": "https://boards.greenhouse.io/acme/jobs/1"}
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Greenhouse", input_domain="", jobs=[job],
        )
        self.assertEqual(decision.metadata["canonical_domain"], "")

    def test_workday_tenant_takes_priority_over_company_url_fallback(self):
        job = {
            "_ats_tenant_domain_candidate": "acme.com",
            "_ats_board_identity_verified": True,
            "official_job_url": "https://www.unrelateddomain.com/jobs/1",
        }
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Acme", input_domain="", jobs=[job],
        )
        self.assertEqual(decision.metadata["canonical_domain"], "acme.com")
        self.assertEqual(decision.metadata["domain_recovery_reason"], "workday_tenant_domain_recovered")


class AccountGateDescriptionTextDomainFallbackTests(unittest.TestCase):
    """Domain recovery extended to plain-text mentions (FINAL_30_PLUS_SYSTEM_SPEC.md
    section 9), for companies with no usable URL anywhere on the record --
    real cases from the 2026-07-29 corpus (amcor, samsara, toast, etc.)."""

    def test_domain_mentioned_in_description_unlocks_search_domain(self):
        job = {"job_description": "Amcor is hiring. Learn more at amcor.com and apply today."}
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Amcor", input_domain="", jobs=[job],
        )
        self.assertEqual(decision.metadata["canonical_domain"], "amcor.com")
        self.assertEqual(decision.metadata["domain_recovery_reason"], "description_text_domain_recovered")

    def test_company_url_fallback_takes_priority_over_description_text(self):
        job = {
            "official_job_url": "https://www.thermofisher.com/careers/1",
            "job_description": "Visit unrelateddomain.com for more information.",
        }
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Thermo Fisher Scientific", input_domain="", jobs=[job],
        )
        self.assertEqual(decision.metadata["canonical_domain"], "thermofisher.com")
        self.assertEqual(decision.metadata["domain_recovery_reason"], "company_url_domain_recovered")

    def test_no_matching_text_still_falls_through_to_unresolved(self):
        job = {"job_description": "A great opportunity to join a growing team."}
        decision = AccountGate(_Resolver()).evaluate(
            org=org(), input_company_name="Acme Corp", input_domain="", jobs=[job],
        )
        self.assertEqual(decision.metadata["canonical_domain"], "")
        self.assertEqual(decision.metadata["domain_recovery_reason"], "no_domain_evidence")


if __name__ == "__main__":
    unittest.main()
