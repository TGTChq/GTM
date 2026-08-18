from __future__ import annotations

import json
from pathlib import Path

import pytest

import airtable_client
import config
import instantly_client
from audit_outbound_displays import audit_records
from apollo_client import OrgEnrichment
from company_display_resolver import CompanyDisplayCache, resolve_company_display
from decision_types import GateDecision, GateState
from hiring_manager import _outbound_display_gate, _strict_base_lead
from role_display_resolver import resolve_role_display
from validation_integrity import validation_fingerprint


def _cache(tmp_path: Path) -> CompanyDisplayCache:
    return CompanyDisplayCache(tmp_path / "company-display.json")


@pytest.mark.parametrize(
    ("kwargs", "expected", "confidence", "hold"),
    [
        (
            dict(organization="LEO Inc.", org_linkedin_slug="leoinc", employer_domain="leoinc.org"),
            "LEO", "medium", False,
        ),
        (
            dict(
                organization="Levelset, a Procore Company",
                org_linkedin_slug="levelset",
                employer_domain="levelset.com",
            ),
            "Levelset", "high", False,
        ),
        (
            dict(
                organization="SOCRadar",
                org_linkedin_name="SOCRadar® Extended Threat Intelligence",
                canonical_company_name="SOCRadar® Extended Threat Intelligence",
                org_linkedin_slug="socradar",
                employer_domain="socradar.io",
            ),
            "SOCRadar", "high", False,
        ),
        (
            dict(
                organization="Epionce / Episciences, Inc.",
                org_linkedin_slug="epionce",
                employer_domain="epionce.com",
            ),
            "Epionce", "high", False,
        ),
        (
            dict(
                organization="Unishippers - Hudson Group",
                canonical_company_name="Unishippers - Hudson Group",
                org_linkedin_slug="unishippersccshippingspree",
                employer_domain="unishippers.com",
            ),
            "Unishippers - Hudson Group", "low", True,
        ),
    ],
)
def test_audited_company_examples(tmp_path, kwargs, expected, confidence, hold):
    result = resolve_company_display(**kwargs, cache=_cache(tmp_path), persist=False)
    assert result.name == expected
    assert result.confidence == confidence
    assert result.hold is hold


def test_linkedin_name_is_a_candidate_not_an_override(tmp_path):
    result = resolve_company_display(
        organization="SOCRadar",
        org_linkedin_name="SOCRadar® Extended Threat Intelligence",
        org_linkedin_slug="socradar",
        employer_domain="socradar.io",
        cache=_cache(tmp_path),
        persist=False,
    )
    assert result.name == "SOCRadar"
    assert result.evidence["selected_source"] == "organization"


def test_linkedin_domain_identity_disagreement_holds(tmp_path):
    result = resolve_company_display(
        organization="Acme",
        canonical_company_name="Acme",
        org_linkedin_slug="globex",
        employer_domain="acme.com",
        cache=_cache(tmp_path),
        persist=False,
    )
    assert result.hold is True
    assert result.confidence == "low"
    assert "linkedin_slug_domain_disagreement" in result.evidence["reasons"]


def test_verified_canonical_name_domain_pair_is_medium_without_lexical_match(tmp_path):
    result = resolve_company_display(
        organization="The Motley Fool",
        canonical_company_name="The Motley Fool",
        employer_domain="fool.com",
        canonical_identity_verified=True,
        cache=_cache(tmp_path), persist=False,
    )
    assert result.name == "The Motley Fool"
    assert result.confidence == "medium"
    assert result.identity_safe is True
    assert result.hold is False


def test_verified_pair_does_not_release_malformed_or_coded_name(tmp_path):
    result = resolve_company_display(
        organization="1115 Target General Merchandise Inc",
        canonical_company_name="1115 Target General Merchandise Inc",
        employer_domain="target.com",
        canonical_identity_verified=True,
        cache=_cache(tmp_path), persist=False,
    )
    assert result.hold is True
    assert "malformed_or_coded_company_name" in result.evidence["reasons"]


def test_franchise_or_subsidiary_separator_is_not_blindly_split(tmp_path):
    result = resolve_company_display(
        organization="Acme - West Coast Franchise",
        org_linkedin_slug="acme-west-coast",
        employer_domain="acme.com",
        cache=_cache(tmp_path),
        persist=False,
    )
    assert result.name == "Acme - West Coast Franchise"
    assert result.hold is True


def test_legal_suffix_requires_identity_evidence(tmp_path):
    unsafe = resolve_company_display(
        organization="Acme Inc.", cache=_cache(tmp_path), persist=False
    )
    safe = resolve_company_display(
        organization="Acme Inc.", employer_domain="acme.com",
        cache=_cache(tmp_path / "safe"), persist=False,
    )
    assert unsafe.name == "Acme Inc."
    assert unsafe.hold is True
    assert safe.name == "Acme"
    assert safe.hold is False


def test_manual_cache_override_is_sticky(tmp_path):
    path = tmp_path / "company-display.json"
    path.write_text(json.dumps({
        "schema": "company-display-cache/1",
        "resolver_version": "company-display/1",
        "entries": {
            "linkedin:acme": {
                "display_name": "ACME",
                "confidence": "high",
                "identity_safe": True,
                "identity_keys": ["linkedin:acme", "domain:acme.com"],
                "manual_override": True,
            }
        },
        "aliases": {"domain:acme.com": "linkedin:acme"},
    }), encoding="utf-8")
    cache = CompanyDisplayCache(path)
    result = resolve_company_display(
        organization="Acme Incorporated", canonical_company_name="Acme Holdings",
        org_linkedin_slug="acme", employer_domain="acme.com", cache=cache,
    )
    assert result.name == "ACME"
    assert result.evidence["manual_override"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["entries"]["linkedin:acme"]["display_name"] == "ACME"


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (
            {"job_title": "People Development Team - TEMP Recruiter", "_matched_role": "Recruiter"},
            "Recruiter",
        ),
        (
            {
                "job_title": "Founding & Lead Recruiter Roles | AI Startups | $110K–$210K+",
                "_matched_role": "Recruiter",
            },
            "Lead Recruiter",
        ),
        (
            {"job_title": "HR Generalist - Learning and Development", "_matched_role": "HR Generalist"},
            "HR Generalist",
        ),
    ],
)
def test_audited_role_examples(job, expected):
    result = resolve_role_display(job)
    assert result.name == expected
    assert result.confidence == "high"


def test_role_focus_context_is_not_duplicated_into_outbound_role():
    raw = "HR Generalist, Learning & Development / Talent Acquisition"
    result = resolve_role_display({
        "job_title": raw,
        "_matched_role": "HR Generalist",
        "role_focus": "Learning & Development; Talent Acquisition",
    })
    assert result.name == "HR Generalist"
    assert result.hold is False


@pytest.mark.parametrize(("raw", "expected"), [
    ("Senior Condominium Community Manager | Flexible PTO", "Senior Community Manager"),
    ("GTM Engineer | B2B SaaS | p/y | Hybrid/Remote", "GTM Engineer"),
    ("Customer Success Manager | Fully Remote US", "Customer Success Manager"),
])
def test_pipe_posting_context_is_removed(raw, expected):
    matched = raw.split(" | ", 1)[0]
    if raw.startswith("Senior Condominium"):
        matched = "Community Manager"
    result = resolve_role_display({"job_title": raw, "_matched_role": matched})
    assert result.name == expected


def test_location_and_work_metadata_inside_parentheses_are_removed():
    raw = "Senior Affiliate Manager (Full Remote - Europe)"
    result = resolve_role_display({
        "job_title": raw,
        "_matched_role": "Affiliate Manager",
        "job_location": "Europe",
    })
    assert result.name == "Senior Affiliate Manager"


def test_promotional_suffix_does_not_survive_anchor_rendering():
    raw = "Java Backend Developer --- Locals Only --- Hybrid role"
    result = resolve_role_display({"job_title": raw, "_matched_role": "Backend Developer"})
    assert result.name == "Backend Developer"


def test_salary_and_confirmed_location_are_removed():
    result = resolve_role_display({
        "job_title": "Account Executive - Spokane | $100K–$150K+",
        "job_location": "Spokane, Washington, United States",
        "_matched_role": "Account Executive",
    })
    assert result.name == "Account Executive"
    assert "posting_context_excluded" in result.evidence["rules"]


def test_low_confidence_company_is_a_delivery_hold(tmp_path):
    result = resolve_company_display(
        organization="Unishippers - Hudson Group",
        org_linkedin_slug="unishippersccshippingspree",
        employer_domain="unishippers.com",
        cache=_cache(tmp_path), persist=False,
    )
    gate = _outbound_display_gate({
        "_outbound_company_hold": result.hold,
        "outbound_company_confidence": result.confidence,
        "outbound_company_identity_key": result.identity_key,
        "_outbound_company_identity_safe": result.identity_safe,
    })
    assert gate.state_value == "NEEDS_CHECK"
    with pytest.raises(ValueError, match="held"):
        instantly_client.airtable_record_to_lead({
            "id": "rec-held",
            "fields": {"Final Decision": "NEEDS_CHECK", "Validation Version": "v", "Outbound Hold": True},
        }, probe=False)


def test_ambiguous_role_is_a_delivery_hold():
    result = resolve_role_display({
        "job_title": "Sales Account Manager/ Broker",
        "_matched_role": "Account Manager",
    })
    gate = _outbound_display_gate({
        "_outbound_company_hold": False,
        "outbound_company_confidence": "high",
        "outbound_company_identity_key": "domain:example.com",
        "_outbound_company_identity_safe": True,
        "_outbound_role_hold": result.hold,
        "outbound_role_confidence": result.confidence,
        "_outbound_role_evidence": result.evidence,
    })
    assert gate.state_value == "NEEDS_CHECK"
    assert str(gate.primary_reason) == "ReasonCode.NEEDS_CHECK_OUTBOUND_ROLE_AMBIGUOUS"


def test_approved_sync_eligibility_rejects_held_company():
    fields = {
        "Status": "Approved",
        "Final Decision": "NEEDS_CHECK",
        "Validation Version": config.VALIDATION_VERSION,
        "Email": "hm@example.com",
        "Company": "Unishippers - Hudson Group",
        "Outbound Company": "Unishippers - Hudson Group",
        "Outbound Company Confidence": "low",
        "Outbound Company Identity": "domain:unishippers.com",
        "Outbound Hold": True,
        "Outbound Role": "Account Executive",
        "Role Bucket": "gtm_revenue",
        "Campaign ID": "campaign-test",
    }
    fields["Validation Fingerprint"] = validation_fingerprint(fields)
    category, reason = airtable_client.approved_row_eligibility(fields)
    assert category == "invalid"
    assert reason == "outbound_company_held_for_review"


def test_apollo_name_does_not_overwrite_display_or_canonical_identity(tmp_path, monkeypatch):
    # Prevent this focused integration test from touching the configured cache.
    monkeypatch.setattr(
        "hiring_manager.resolve_company_display",
        lambda **kwargs: resolve_company_display(
            **kwargs, cache=_cache(tmp_path), persist=False
        ),
    )
    primary = {
        "job_id": "j1",
        "job_title": "Sales Development Representative",
        "canonical_job_title": "Sales Development Representative",
        "employer_name": "SOCRadar",
        "organization": "SOCRadar",
        "org_linkedin_name": "SOCRadar® Extended Threat Intelligence",
        "org_linkedin_slug": "socradar",
        "org_linkedin_website": "https://socradar.io",
        "employer_website": "socradar.io",
        "_matched_role": "Sales Development Representative",
    }
    apollo_name = "SOCRadar® Extended Threat Intelligence"
    account = GateDecision(
        "account", GateState.PASS, "ACCOUNT_PASS",
        metadata={
            "canonical_company_name": apollo_name,
            "canonical_domain": "socradar.io",
            "business_model": "commercial_product_or_service",
        },
    )
    lead = _strict_base_lead(
        primary, [primary], "gtm_revenue",
        OrgEnrichment(True, name=apollo_name, domain="socradar.io", employee_count=100),
        account,
    )
    assert lead["canonical_company_name"] == apollo_name
    assert lead["employer_name"] == "SOCRadar"
    assert lead["job_title"] == "Sales Development Representative"
    assert lead["canonical_job_title"] == "Sales Development Representative"
    assert lead["_matched_role"] == "Sales Development Representative"
    assert lead["outbound_company_name"] == "SOCRadar"


def test_airtable_keeps_canonical_fields_separate_from_outbound_fields():
    job = {
        "lead_key": "k",
        "canonical_company_name": "Levelset, a Procore Company",
        "outbound_company_name": "Levelset",
        "outbound_company_confidence": "high",
        "outbound_company_identity_key": "linkedin:levelset",
        "_outbound_company_hold": False,
        "_outbound_role_hold": False,
        "canonical_job_title": "Account Executive - Spokane",
        "outbound_role_name": "Account Executive",
        "related_open_roles": ["Account Executive - Spokane"],
        "related_outbound_roles": ["Account Executive"],
        "outbound_role_confidence": "high",
    }
    fields = airtable_client._job_to_fields(job)
    assert fields["Company"] == "Levelset, a Procore Company"
    assert fields["Outbound Company"] == "Levelset"
    assert fields["Open Role"] == "Account Executive - Spokane"
    assert fields["Outbound Role"] == "Account Executive"


def test_queued_audit_is_read_only_and_reports_holds():
    result = audit_records([
        {"id": "safe", "fields": {
            "Status": "Pending", "Company": "Levelset, a Procore Company",
            "Website": "https://levelset.com", "Open Role": "Account Executive - Spokane",
            "Matched Role": "Account Executive", "Location": "Spokane, WA",
        }},
        {"id": "held", "fields": {
            "Status": "Approved", "Company": "Unishippers - Hudson Group",
            "Website": "https://unishippers.com", "Open Role": "Account Executive",
            "Matched Role": "Account Executive",
        }},
    ])
    assert result["mode"] == "read_only"
    assert result["writes"] == 0
    assert result["total_inspected"] == 2
    assert result["company_names_changed"] == 1
    assert result["open_roles_changed"] == 1
    assert [row["record_id"] for row in result["held_cases"]] == ["held"]
