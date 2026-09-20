"""The corrected exhaustive scope (Luis, 2026-09-20, third instruction).

Three-state logic, and nothing else:

1. explicit evidence that an approved hard exclusion applies -> REJECT;
2. missing or unknown information, with no such evidence           -> PASS;
3. passes the gates but matches no campaign allowlist              -> PASS,
   assigned automatically to the closest of the nine campaigns.

There is no NEEDS_CHECK bucket. The ONLY scope restriction removed is the
requirement for an exact functional/title fit; every approved hard exclusion is
preserved exactly as it was, and this file asserts that in both directions.

Work arrangement: onsite, hybrid, a false remote flag and a missing remote flag
are all eligible. Only inherently physical duties reject. A generic office
location statement is not evidence that the duties are physical.
"""
from __future__ import annotations

import json

import pytest

from tgtc_core.domain.classification import classify_posting
from tgtc_core.domain.exhaustive_routing import FLAG_ENV
from tgtc_core.policy.campaigns import (
    CAMPAIGN_BY_FUNCTION, CAMPAIGN_ENV_BY_FUNCTION, KNOWN_CHALLENGER_CAMPAIGN_IDS,
    KNOWN_CONTROL_CAMPAIGN_IDS, POLICY_VERSION, POLICY_VERSION_EXHAUSTIVE,
    effective_policy_version,
)

ON = {FLAG_ENV: "1"}

OFFICE = (
    "This role is based in our Chicago office and follows a hybrid schedule of "
    "three days on site each week. You will partner with teams across the company "
    "and report to the department lead. We offer competitive pay and full benefits."
)


def _c(title, description=OFFICE, **kw):
    return classify_posting(title=title, description=description, inference=None, env=ON, **kw)


# --- 2. work arrangement is no longer a gate ---------------------------------


def test_onsite_accountant_passes_to_finance():
    result = _c("Accountant", "You will own the monthly close from our downtown office. " + OFFICE)
    assert not result.excluded
    assert result.compatible_functions == ["finance"]


def test_hybrid_software_engineer_passes_to_ai_technical():
    result = _c("Software Engineer")
    assert not result.excluded
    assert CAMPAIGN_BY_FUNCTION[result.primary_function].key == "ai_technical"


def test_office_based_marketing_manager_passes():
    result = _c("Marketing Manager")
    assert not result.excluded
    assert CAMPAIGN_BY_FUNCTION[result.primary_function].key == "marketing_creative"


@pytest.mark.parametrize("location_type", ["onsite", "hybrid", "", None])
def test_every_work_arrangement_is_eligible(location_type):
    result = _c("Operations Analyst", location_type=location_type)
    assert not result.excluded, f"location_type={location_type!r} rejected"
    assert result.compatible_functions


def test_a_generic_office_statement_is_not_physical_evidence():
    result = _c("Product Manager", "You will work from our New York office five days a week. " + OFFICE)
    assert not result.excluded


# --- 1. the approved hard exclusions are preserved ---------------------------


def test_warehouse_physical_handling_still_rejects():
    result = _c(
        "Warehouse Associate",
        "You must work in the warehouse every shift and operate a forklift and pallet jack "
        "to move inventory across the fulfillment floor. Physical presence is required daily.",
    )
    assert result.excluded


def test_patient_care_still_rejects():
    result = _c(
        "Clinical Coordinator",
        "Direct patient care at the bedside is a core responsibility of this position and "
        "you must work in the hospital alongside the attending team for every scheduled shift.",
    )
    assert result.excluded


def test_field_installation_still_rejects():
    result = _c(
        "Field Service Technician",
        "This position requires frequent travel to customer sites to install and repair "
        "equipment on location. You must travel regularly across the assigned territory "
        "and must live near an airport to support the regional install schedule.",
    )
    assert result.excluded


def test_explicit_part_time_still_rejects():
    result = _c("Software Engineer", employment_type="PART_TIME")
    assert result.excluded


def test_internship_still_rejects():
    result = _c("Engineering Intern", "Join our summer internship program. " + OFFICE)
    assert result.excluded


def test_staffing_employer_still_rejects():
    result = _c("Software Engineer", employer_name="Apex Staffing Group", agency_flag=True)
    assert result.excluded


def test_staffing_agency_posting_a_recruiter_rejects_because_of_the_employer():
    """The employer's business model is what disqualifies it, not the job."""
    result = _c(
        "Recruiter",
        "We are a recruiting firm and our client is seeking talented professionals. "
        "We recruit candidates on behalf of the organizations we partner with. " + OFFICE,
    )
    assert result.excluded


def test_foreign_only_still_rejects():
    result = _c(
        "Software Engineer",
        "This position is an EMEA-only role and applicants must be based in Europe. " + OFFICE,
    )
    assert result.excluded


# --- the counterpart: an INTERNAL recruiter is People & HR -------------------


def test_internal_recruiter_at_a_legitimate_employer_passes_to_people_hr():
    result = _c(
        "Technical Recruiter",
        "You will own full-cycle hiring for our engineering organization, partner with "
        "hiring managers on scorecards and interview loops, and build our talent pipeline "
        "as we grow the team this year. " + OFFICE,
        employer_name="Northwind Software",
    )
    assert not result.excluded
    assert CAMPAIGN_BY_FUNCTION[result.primary_function].key == "people_hr"


# --- quota-carrying sales is in scope now ------------------------------------


@pytest.mark.parametrize("title", ["Account Executive", "Sales Development Representative"])
def test_quota_carrying_sales_passes_to_gtm(title):
    result = _c(
        title,
        "You will carry a quota, run the full sales cycle from qualification to close and "
        "prospect into new accounts with outbound cold calls to build pipeline. " + OFFICE,
    )
    assert not result.excluded
    assert CAMPAIGN_BY_FUNCTION[result.primary_function].key == "gtm_systems"


# --- 2. unknown facts pass. There is no NEEDS_CHECK bucket -------------------


def test_missing_employment_type_passes():
    result = _c("Financial Analyst", employment_type=None)
    assert not result.excluded
    assert result.compatible_functions == ["finance"]


def test_missing_company_size_passes_and_creates_no_needs_check():
    result = _c("Financial Analyst")
    assert not result.excluded
    facts = result.facts
    assert "company_size" not in facts or facts.get("company_size", {}).get("value") != "out_of_range"
    assert not [r for r in (facts.get("review_reasons") or []) if "needs_check" in str(r).lower()]


def test_no_needs_check_string_anywhere_in_a_routed_result():
    result = _c("Chief of Staff")
    assert "needs_check" not in json.dumps(result.to_dict()).lower()


def test_unknown_industry_passes():
    result = _c("Financial Analyst", org_industry=None)
    assert not result.excluded


# --- 3. no allowlist match is not a rejection --------------------------------


def test_title_absent_from_every_allowlist_is_assigned_not_rejected():
    result = _c("Chief of Staff")
    assert not result.excluded
    assert len(result.compatible_functions) == 1


def test_malformed_record_is_not_a_business_rejection():
    """No recoverable title and no usable description: it stays retryable and is
    never recorded as a business exclusion."""
    result = _c("", "x")
    assert not result.excluded
    assert result.exclusion_reason == ""
    assert any("insufficient_evidence" in n for n in result.notes)


# --- policy versioning -------------------------------------------------------


def test_new_policy_version_is_separate_from_the_frozen_one():
    assert POLICY_VERSION_EXHAUSTIVE != POLICY_VERSION
    assert effective_policy_version(ON) == POLICY_VERSION_EXHAUSTIVE
    assert effective_policy_version({}) == POLICY_VERSION


def test_routed_results_carry_the_new_policy_version():
    assert _c("Chief of Staff").policy_version == POLICY_VERSION_EXHAUSTIVE


# --- Challenger routing: ten keys collapse to exactly nine campaigns ---------


CHALLENGER_BY_FUNCTION = {
    "product": "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0",
    "operations": "69def27c-7799-41a2-9ba8-205e54ab071b",
    "finance": "7b319c7a-cc55-4e08-8a47-7058c345d8ae",
    "people_hr": "d2326028-e312-405b-9e16-526bd309d4dd",
    "ecommerce": "c3e81c21-db44-40f5-addc-d9945a78394b",
    "customer_success": "269cd138-00b1-48c3-9093-16c36120a20e",
    "customer_support": "269cd138-00b1-48c3-9093-16c36120a20e",
    "marketing": "1feb6344-6065-49d7-9764-d125985fb9c9",
    "gtm_revenue": "8f25abd5-568a-4e88-b310-9acf85161c6c",
    "engineering": "8bfa0769-4b9a-4346-8e93-17ac8b726dce",
}


def test_ten_internal_keys_collapse_to_exactly_nine_challenger_campaigns():
    assert len(CHALLENGER_BY_FUNCTION) == 10
    assert len(set(CHALLENGER_BY_FUNCTION.values())) == 9
    assert set(CHALLENGER_BY_FUNCTION.values()) == set(KNOWN_CHALLENGER_CAMPAIGN_IDS)


def test_the_only_shared_campaign_is_customer_experience():
    """Ten keys, nine campaigns: the collapse must come from CX alone, not from
    an accidental duplicate that would enrol one person twice."""
    shared = [fn for fn, cid in CHALLENGER_BY_FUNCTION.items()
              if list(CHALLENGER_BY_FUNCTION.values()).count(cid) > 1]
    assert sorted(shared) == ["customer_success", "customer_support"]
    assert CAMPAIGN_BY_FUNCTION["customer_success"] is CAMPAIGN_BY_FUNCTION["customer_support"]


def test_every_function_key_has_a_challenger_destination():
    assert set(CHALLENGER_BY_FUNCTION) == set(CAMPAIGN_ENV_BY_FUNCTION)


def test_challenger_and_control_campaigns_are_disjoint():
    """Eighteen campaigns exist. A new lead must never reach a retired Control one."""
    assert not (KNOWN_CHALLENGER_CAMPAIGN_IDS & KNOWN_CONTROL_CAMPAIGN_IDS)
    assert len(KNOWN_CHALLENGER_CAMPAIGN_IDS) == 9
    assert len(KNOWN_CONTROL_CAMPAIGN_IDS) == 9
