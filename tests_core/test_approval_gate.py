"""Automatic quality: nothing incomplete is approved, and no review state exists."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tgtc_core.domain import approval as ap
from tgtc_core.domain.gates import evaluate_email
from tgtc_core.policy.campaigns import KNOWN_CONTROL_CAMPAIGN_IDS

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
CID = "45ac1e03-67e7-4bdd-b372-808042104e4c"


def _inputs(**over):
    base = dict(
        posting={"id": 1, "state": "classified", "source": "fantastic:active-jb", "provider_job_id": "x", "title": "Product Manager",
                 "url": "https://jobs.example/x", "date_posted": NOW - timedelta(days=2), "first_seen_at": NOW - timedelta(days=1),
                 "commercial_age_anchor": NOW - timedelta(days=2)},
        classification={"excluded": False, "compatible_functions": ["product"], "method": "deterministic",
                        "responsibilities": [{"phrase": "product roadmap and requirements", "excerpt": "Own the product roadmap"}]},
        employer={"id": 1, "canonical_name": "Acme", "domain": "acme.com", "employee_count": 120, "industry": "Software", "excluded_industry": ""},
        person={"id": 1, "apollo_person_id": "p1", "first_name": "Jane", "last_name": "Doe", "title": "VP Product",
                "linkedin_url": "https://linkedin.com/in/jane", "email": "jane@acme.com", "email_status": "verified",
                "contact_gate_passed": True, "email_gate_passed": True, "email_alignment": "EXACT_EMPLOYER_DOMAIN"},
        function_key="product", campaign_id=CID, allowed_campaign_ids=[CID], signing_key="k", now=NOW,
    )
    base.update(over)
    return base


def test_complete_facts_are_approved_with_a_legacy_compatible_lead_key_and_fingerprint():
    out = ap.build_approved_lead(**_inputs())
    assert isinstance(out, ap.ApprovedLead)
    assert out.lead_key == "acme.com|jane@acme.com|product"
    assert out.lead["status"] == "Approved" and out.lead["campaign_key"] == "product"
    assert out.fingerprint == ap.fingerprint(out.lead, "k") and out.fingerprint != ap.fingerprint(out.lead, "other")


@pytest.mark.parametrize("mutation, reason", [
    (lambda i: i["person"].update(email_status="extrapolated"), "email_not_verified"),
    (lambda i: i["person"].update(email_gate_passed=False), "email_not_verified"),
    (lambda i: i["person"].update(contact_gate_passed=False), "contact_gate_not_passed"),
    (lambda i: i["person"].update(last_name=""), "person_name_incomplete"),
    (lambda i: i["person"].update(linkedin_url=""), "person_linkedin_missing"),
    (lambda i: i["employer"].update(domain=""), "employer_identity_incomplete"),
    (lambda i: i["employer"].update(employee_count=10), "employer_too_small"),
    (lambda i: i["employer"].update(employee_count=5000), "employer_too_large"),
    (lambda i: i["employer"].update(excluded_industry="staffing"), "employer_excluded_industry"),
    (lambda i: i["classification"].update(responsibilities=[]), "no_responsibility_evidence"),
    (lambda i: i["classification"].update(excluded=True, exclusion_reason="x"), "posting_excluded"),
    (lambda i: i["classification"].update(compatible_functions=["finance"]), "function_not_compatible"),
    (lambda i: i["posting"].update(commercial_age_anchor=NOW - timedelta(days=120)), "posting_too_old"),
    (lambda i: i["posting"].update(state="closed"), "posting_not_active"),
    (lambda i: i.update(campaign_id=""), "no_campaign_configured"),
    (lambda i: i.update(campaign_id="not-a-known-campaign", allowed_campaign_ids=[]), "campaign_id_not_allowed"),
    (lambda i: i.update(signing_key=""), "signing_key_missing"),
    (lambda i: i.update(suppression_hits=["person_email:jane@acme.com"]), "suppressed"),
])
def test_every_missing_requirement_refuses_with_a_named_reason(mutation, reason):
    inputs = _inputs()
    mutation(inputs)
    out = ap.build_approved_lead(**inputs)
    assert isinstance(out, ap.ApprovalRefusal) and out.reason == reason


def test_email_gate_never_promotes_non_verified_statuses():
    for status in ("extrapolated", "likely_to_engage", "unavailable", "unverified", "accept_all", "unknown", "", None):
        assert not evaluate_email(email="jane@acme.com", email_status=status, employer_domains={"acme.com"}).passed
    assert evaluate_email(email="jane@acme.com", email_status="Verified", employer_domains={"acme.com"}).passed
    assert not evaluate_email(email="careers@acme.com", email_status="verified", employer_domains={"acme.com"}).passed
    assert not evaluate_email(email="jane@other.com", email_status="verified", employer_domains={"acme.com"}).passed


def test_airtable_payload_is_approved_only_and_carries_the_core_validation_version():
    out = ap.build_approved_lead(**_inputs())
    fields = ap.airtable_fields(out.lead, out.fingerprint)
    assert fields["Status"] == "Approved"
    assert fields["Validation Version"] == "tgtc-core/1"
    assert fields["Validation Fingerprint"] == out.fingerprint
    assert fields["Lead Key"] == out.lead_key and fields["Campaign ID"] == CID and fields["Role Bucket"] == "product"
    for internal in ("Pending", "NEEDS_CHECK", "UNVERIFIED", "REROUTE", "ready", "waiting", "retry"):
        assert internal not in {str(v) for v in fields.values()}
    assert {"Outbound Company", "Outbound Role", "Role Focus", "Email", "Hiring Manager", "HM Title"} <= set(fields)


def test_instantly_payload_uses_documented_fields_and_the_control_variable_names():
    out = ap.build_approved_lead(**_inputs())
    payload = ap.instantly_payload(out.lead, skip_if_in_workspace=True, verify_on_import=False)
    assert set(payload) == {"campaign", "email", "first_name", "last_name", "company_name", "job_title", "website",
                            "skip_if_in_workspace", "skip_if_in_campaign", "verify_leads_on_import", "custom_variables"}
    assert payload["campaign"] == CID and payload["skip_if_in_campaign"] is True
    variables = payload["custom_variables"]
    assert {"open_role", "open_roles", "role_focus", "role_bucket", "company_size_band", "job_url", "relevance"} <= set(variables)
    assert all(isinstance(v, (str, int, float, bool)) for v in variables.values())
    assert variables["role_focus"] == "product roadmap and requirements"


def test_display_role_and_role_focus_are_never_empty_for_a_known_function():
    assert ap.display_role("Customer Success Manager (Remote - US)", "customer_success") == "Customer Success Manager"
    assert ap.display_role("", "customer_success") == "customer success role"
    assert ap.display_role("URGENT!!! hiring $$$ 12345678 x/y|z", "finance") == "finance role"
    assert ap.role_focus_text(["a", "b", "c", "d"]) == "a, b, and c"
    assert ap.role_focus_text(["a", "b"]) == "a and b"
