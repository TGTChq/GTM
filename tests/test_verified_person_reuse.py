"""Paid data survives a process restart; cached data still passes current gates."""
import copy
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

import config
import hiring_manager as hm
from apollo_client import OrgEnrichment, PersonMatch
from decision_types import GateDecision, GateState
from orchestrator.apollo_cache import ApolloCache


def test_company_domain_containing_a_social_brand_remains_a_valid_identity():
    from orchestrator.apollo_cache import normalize_domain
    assert normalize_domain("https://www.fedex.com/jobs") == "fedex.com"
    assert normalize_domain("https://www.linkedin.com/company/example") == ""


def test_verified_person_is_reused_by_the_strict_path_after_restart():
    job = {"job_id": "job-one", "job_title": "Staff Accountant",
           "employer_name": "Acme", "canonical_employer_name": "Acme",
           "employer_website": "https://acme.com", "_employer_domain_input": "acme.com",
           "_matched_role": "Staff Accountant", "_search_role": "Staff Accountant",
           "_job_gate_state": "PASS", "_role_gate_state": "PASS",
           "_job_gate_decision": GateDecision("job", GateState.PASS, "JOB_PASS").to_dict(),
           "_role_gate_decision": GateDecision("role", GateState.PASS, "ROLE_PASS").to_dict()}
    candidate = {"id": "person-one", "title": "Controller",
                 "organization": {"name": "Acme", "domain": "acme.com"}}
    person = PersonMatch(True, person_id="person-one", first_name="Jane", last_name="Doe",
        title="Controller", organization_name="Acme", organization_domain="acme.com",
        email="jane@acme.com", email_status="verified", country="United States",
        linkedin_url="https://linkedin.com/in/example",
        raw={"current_organization": {"name": "Acme", "domain": "acme.com"}})
    account = GateDecision("account", GateState.PASS, "ACCOUNT_PASS", metadata={
        "canonical_domain": "acme.com", "canonical_company_name": "Acme",
        "business_model": "commercial_product_or_service"})
    with tempfile.TemporaryDirectory() as root, mock.patch.multiple(config,
            APOLLO_CACHE_ENABLED=True, APOLLO_CACHE_PATH=str(Path(root) / "cache.json"),
            REROUTE_STATE_FILE=str(Path(root) / "reroute.json"),
            APOLLO_RATE_LIMIT_DELAY=0, VERIFY_WITH_HUNTER=False), mock.patch.object(
            hm.apollo, "enrich_organization", return_value=OrgEnrichment(
                True, name="Acme", domain="acme.com", employee_count=100)), mock.patch.object(
            hm.AccountGate, "evaluate", return_value=account), mock.patch.object(
            hm.apollo, "search_people_at_company", return_value=[candidate]), mock.patch.object(
            hm.apollo, "match_person", return_value=person) as paid, mock.patch.object(
            hm.ContactGate, "evaluate", wraps=hm.ContactGate().evaluate) as gate:
        hm.reset_apollo_cache()
        hm.reset_paid_match_budget()
        try:
            first, _ = hm.process_company([copy.deepcopy(job)])
            hm.reset_apollo_cache()  # force durable reload, not an in-memory hit
            job["job_id"] = "job-two"
            second, stats = hm.process_company([copy.deepcopy(job)])
            assert paid.call_count == 1
            assert gate.call_count == 2
            assert first[0]["hiring_manager_email"] == second[0]["hiring_manager_email"]
            assert stats["person_match_cache_hits"] == 1
            assert stats.get("person_match_attempts", 0) == 0
            assert hm._cached_verified_person(candidate, "another.com") is None
            cache = hm._apollo_cache()
            cache._now = cache.now() + timedelta(days=46)
            assert hm._cached_verified_person(candidate, "acme.com") is None
        finally:
            hm.reset_apollo_cache()
            hm.reset_paid_match_budget()


def test_unverified_or_wrong_employer_data_is_not_reused():
    candidate = {"id": "person-one"}
    with tempfile.TemporaryDirectory() as root:
        cache = ApolloCache(str(Path(root) / "cache.json"), enabled=True, ttl_days={})
        with mock.patch.object(hm, "_apollo_cache", return_value=cache):
            for domain, status in [("other.com", "verified"), ("acme.com", "extrapolated")]:
                hm._remember_verified_person(candidate, "acme.com", PersonMatch(
                    True, person_id="person-one", email="jane@acme.com",
                    organization_domain=domain, email_status=status))
                assert hm._cached_verified_person(candidate, "acme.com") is None
