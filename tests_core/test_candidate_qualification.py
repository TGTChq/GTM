"""Regression tests for the isolated candidate qualification policy (``TGTC_CANDIDATE_QUALIFICATION``).

Every fixture case is a real record from the 2026-09-19 24h canary (run_20260919T061752Z), sanitized:
employer name / slug / website replaced (public government bodies keep their name: it is the evidence
under test); URLs, e-mail addresses, phone numbers and personal names removed. Each case stores the
outcome the CURRENT policy produces and the outcome the candidate produces, so a change to either
qualifier shows up here.
"""

import ast
import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from tgtc_core.domain import candidate_qualification as cq
from tgtc_core.domain.facts import extract_job_facts, sentences
from tgtc_core.services.daily_24h_canary import qualify_row

FIXTURE = Path(__file__).with_name("fixtures") / "candidate_qualification_canary_cases.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
NOW = datetime.fromisoformat(DATA["evaluated_at"])
CASES = DATA["cases"]
IDS = [c["case"] for c in CASES]


def by_requirement(requirement):
    found = [c for c in CASES if c["requirement"] == requirement]
    assert found, requirement
    return found


def candidate(row, steps=cq.ALL_STEPS, crm=frozenset()):
    return cq.qualify_candidate(row, now=NOW, steps=steps, crm=crm)


def test_flag_is_off_by_default():
    assert not cq.candidate_enabled({})
    assert not cq.candidate_enabled({cq.FLAG_ENV: "0"})
    assert not cq.candidate_enabled({cq.FLAG_ENV: "true"})
    assert cq.candidate_enabled({cq.FLAG_ENV: "1"})


def test_fixture_was_built_by_this_policy_version():
    assert DATA["policy_version"] == cq.POLICY_VERSION == "tgtc-candidate/2"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_no_steps_reproduces_the_current_policy(case):
    current = qualify_row(case["row"], now=NOW)
    assert current["outcome"] == case["current_outcome"]
    assert candidate(case["row"], steps=())["outcome"] == current["outcome"]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_candidate_outcomes_are_pinned(case):
    result = candidate(case["row"])
    assert result["outcome"] == case["candidate_outcome"]
    assert (result.get("group") or "") == case["candidate_group"]
    assert result["policy_version"] == cq.POLICY_VERSION


# ---------------------------------------------------------------------------- not automatic exclusions

@pytest.mark.parametrize("case", by_requirement("seniority_title_not_rejected"), ids=lambda c: c["case"])
def test_director_vp_head_title_is_not_an_automatic_rejection(case):
    assert case["current_outcome"] == "rejected:seniority:leadership_or_principal"
    alone = candidate(case["row"], steps=(cq.SENIORITY_MANAGEMENT,))
    assert not alone["outcome"].startswith("rejected:seniority")
    result = candidate(case["row"])
    assert result["outcome"] == "qualified_pre_contact"
    assert "seniority:leadership_or_principal" in result["flags"]   # carried as data, not a gate


@pytest.mark.parametrize("case", by_requirement("people_management_not_rejected"), ids=lambda c: c["case"])
def test_people_management_is_not_an_automatic_rejection(case):
    assert case["current_outcome"] == "rejected:people_management"
    result = candidate(case["row"])
    assert result["outcome"] == "qualified_pre_contact"
    assert "people_management" in result["flags"]


@pytest.mark.parametrize("case", by_requirement("onsite_label_not_rejected"), ids=lambda c: c["case"])
def test_onsite_or_hybrid_label_alone_is_not_a_rejection(case):
    result = candidate(case["row"])
    assert result["work_arrangement"] == "onsite"
    assert result["outcome"] == "qualified_pre_contact"


@pytest.mark.parametrize("case", by_requirement("physical_work_rejected"), ids=lambda c: c["case"])
def test_inherently_physical_work_remains_rejected(case):
    result = candidate(case["row"])
    assert result["outcome"] == "rejected:deliverability:location_bound_duty"
    assert result["evidence"] and result["evidence"][0]["excerpt"]


def test_lifting_boilerplate_does_not_reject_a_remote_capable_job():
    (case,) = by_requirement("incidental_lifting_not_rejected")
    assert case["current_outcome"] == "rejected:deliverability:physical_facility"
    result = candidate(case["row"])
    assert result["outcome"] == "qualified_pre_contact"
    assert "physical_demands_boilerplate" in result["flags"]


@pytest.mark.parametrize("requirement,production_reason", [
    ("incidental_negated_contract_not_rejected", "employment:contract"),     # "This is not a contract."
    ("incidental_agency_disclaimer_not_staffing", None),                     # "If contacted by a ... staffing agency"
    ("incidental_internship_experience_not_rejected", None),                 # "Relevant internship experience ..."
    ("incidental_client_site_travel_not_rejected", None),                    # "Travel to client sites as needed"
])
def test_incidental_words_do_not_cause_rejection(requirement, production_reason):
    (case,) = by_requirement(requirement)
    row = case["row"]
    if production_reason:
        facts = extract_job_facts(title=row["title"], description=row["description_text"],
                                  employment_type=row["employment_type"], ai_employment_type=row["ai_employment_type"])
        assert production_reason in [e.reason for e in facts.exclusions]   # the current rule does fire
    assert candidate(row)["outcome"] == "qualified_pre_contact"


def test_client_needs_wording_is_not_an_intermediary():
    text = ("B4Corp provides a low overhead, highly efficient, high salary environment that allows employees to excel "
            "at meeting the client's needs.")
    assert "agency:staffing_text" in [e.reason for e in extract_job_facts(title="", description=text).exclusions]
    assert cq._intermediary(sentences(text), True) == ("", "")


@pytest.mark.parametrize("sentence,reason", [
    ("Full-time positions are scheduled for approximately 40 hours per week, subject to business and operational needs.",
     "employment:part_time"),
    ("Handle change requests, contract negotiations, and project closeout activities.", None),
    ("Relevant internship experience may be considered Strong interest in learning paid media platforms.", None),
])
def test_incidental_employment_wording_from_canary_records(sentence, reason):
    if reason:
        assert reason in [e.reason for e in extract_job_facts(title="", description=sentence).exclusions]
    decided, _, _ = cq._employment_decision("", sentences(sentence), {"ai_employment_type": ["FULL_TIME"]})
    assert decided == ""


# ---------------------------------------------------------------------------- federal clearance

@pytest.mark.parametrize("case", by_requirement("federal_clearance_rejected"), ids=lambda c: c["case"])
def test_required_secret_ts_sci_or_federal_clearance_is_excluded(case):
    result = candidate(case["row"])
    assert result["outcome"] == "rejected:eligibility:federal_clearance_required"
    assert result["evidence"][0]["excerpt"]


@pytest.mark.parametrize("text,required", [
    ("Security Clearance Active Secret clearance; TS/SCI eligible preferred.", True),
    ("Must hold, or be able to obtain, a Public Trust clearance.", True),
    ("US citizenship with the ability to obtain and maintain Public Trust Desired Skills Experience", True),
    ("Federal DoD Secret clearance highly desirable Evidence of establishing client relationships", False),
    ("Preferred Qualifications: Active Secret clearance.", False),
    ("As a government contractor, you may be required to obtain a public trust or security clearance.", False),
    ("The PIO is the District's media liaison, building public trust and community engagement.", False),
    ("You will own security reviews and trust and safety tooling for our platform.", False),
])
def test_incidental_trust_or_security_language_is_not_a_clearance_requirement(text, required):
    assert bool(cq.federal_clearance("Engineer", sentences(text))[0]) is required


def test_clearance_named_in_the_title_is_required():
    assert cq.federal_clearance("App Developer - TS (Top Secret)", [])[0]


# ---------------------------------------------------------------------------- government

@pytest.mark.parametrize("case", by_requirement("government_employer_rejected"), ids=lambda c: c["case"])
def test_actual_government_employers_are_excluded(case):
    result = candidate(case["row"])
    assert result["outcome"] == "rejected:employer_is_government"
    alone = candidate(case["row"], steps=tuple(s for s in cq.ALL_STEPS if s != cq.GOVERNMENT_EMPLOYER))
    assert alone["outcome"] != "rejected:employer_is_government"


def test_a_product_company_selling_to_government_is_not_excluded():
    (case,) = by_requirement("product_company_selling_to_government_not_rejected")
    assert case["current_outcome"] == "rejected:government:public_sector_title"
    result = candidate(case["row"])
    assert result["outcome"] == "qualified_pre_contact"
    assert "government:public_sector_title" in result["flags"]


@pytest.mark.parametrize("name,government", [
    ("Hawai`i State Judiciary", True), ("City of Farmington Hills", True), ("Lubbock Independent School District", True),
    ("Cache County Sheriff's Office", True), ("Northern Illinois Transit Authority (NITA)", True),
    ("Versa Networks", False), ("State Farm", False), ("Public Sector Solutions Inc", False),
])
def test_government_employer_is_read_from_the_employer_name_only(name, government):
    assert bool(cq.government_employer(name)) is government


# ---------------------------------------------------------------------------- quota-carrying sales

@pytest.mark.parametrize("case", by_requirement("quota_carrying_sales_rejected"), ids=lambda c: c["case"])
def test_quota_carrying_sales_is_outside_the_offer_groups(case):
    result = candidate(case["row"])
    assert result["outcome"] == "rejected:outside_four_groups:quota_carrying_sales"
    assert "selling" in result["evidence"][0]["excerpt"]
    without = candidate(case["row"], steps=tuple(s for s in cq.ALL_STEPS if s != cq.QUOTA_SALES))
    assert without["outcome"] != result["outcome"]


@pytest.mark.parametrize("case", by_requirement("gtm_operations_qualified"), ids=lambda c: c["case"])
def test_revops_sales_ops_gtm_systems_remain_in_scope(case):
    result = candidate(case["row"])
    assert result["outcome"] == "qualified_pre_contact"
    assert result["group"] == "gtm_revops_salesops"


def test_sales_scope_separates_selling_from_gtm_operations():
    selling, ops, _, _ = cq.sales_scope(sentences(
        "Own the full sales cycle from initial outreach through close. Consistently exceed quota. Prospect new business."))
    assert selling >= 6 and ops == 0
    selling, ops, _, _ = cq.sales_scope(sentences(
        "Administer Salesforce workflows and automation. Build pipeline reporting and forecasting dashboards. "
        "Design lead routing and territory planning. Run sales enablement programs."))
    assert ops >= 9 and selling == 0


def test_a_generic_title_with_gtm_operations_responsibilities_is_qualified():
    (case,) = [c for c in CASES if c["row"]["title"] == "Global Vice President, CPQ Sales"]
    result = candidate(case["row"])
    assert result["group"] == "gtm_revops_salesops"
    assert result["role"]["basis"] == "gtm_operations_primary"
    assert result["role"]["gtm_ops_score"] >= 2 * result["role"]["selling_score"]


# ---------------------------------------------------------------------------- true hiring company

def test_a_third_party_repost_is_not_the_true_hiring_company():
    (case,) = by_requirement("third_party_repost_rejected")
    result = candidate(case["row"])
    assert result["outcome"] == "rejected:not_true_hiring_company:third_party_repost"
    without = candidate(case["row"], steps=tuple(s for s in cq.ALL_STEPS if s != cq.THIRD_PARTY_REPOST))
    assert without["outcome"] != result["outcome"]


@pytest.mark.parametrize("employer,description,repost", [
    ("Sundayy", "About The Company\n\nCapital One is a leading financial services company.", True),
    ("Sundayy", "About The Company\n\nJoin a dynamic and innovative organization committed to fintech.", True),
    ("Acme Robotics", "About The Company\n\nAcme Robotics builds warehouse robots.", False),   # names itself
    ("Blissy", "Company Description Blissy is a beauty and wellness brand.", False),          # no repost header
])
def test_repost_rule_needs_the_header_and_an_unnamed_employer(employer, description, repost):
    assert bool(cq.third_party_repost(employer, description)) is repost


def test_bare_quality_assurance_title_needs_software_evidence():
    assert cq.title_families("Sr. Quality Assurance Engineer")[0] == []            # manufacturing QA is common
    assert cq.title_families("QA Engineer")[0] == [("ai_engineering_automation", "software_data_ai")]
    assert cq.location_bound_evidence(sentences("This role is ideal for someone who thrives behind a parts counter."))


# ---------------------------------------------------------------------------- field travel

def test_essential_field_territory_travel_is_excluded():
    (case,) = by_requirement("essential_field_travel_rejected")
    assert candidate(case["row"])["outcome"] == "rejected:deliverability:essential_field_travel"


@pytest.mark.parametrize("text,essential", [
    ("Willingness to travel within the assigned territory (approximately 50-70%).", True),
    ("Collaborate with distributor sales teams through co-travel and regular joint customer visits.", True),
    ("It is FLEX remote where you will work from home while traveling to dealership locations in FL.", True),
    ("Travel to client sites as needed", False),
    ("Ability to travel up to 25% of the time to industry events and conferences.", False),
    ("Occasional travel to our Chicago office for team offsites.", False),
])
def test_incidental_travel_is_not_essential_field_travel(text, essential):
    assert bool(cq.essential_field_travel(sentences(text))) is essential


@pytest.mark.parametrize("sentence,hard,flag", [
    ("This position is expected to travel up to 95%.", True, None),
    ("Travel Approximately 40-50% travel throughout South Africa.", True, None),
    ("Willingness to travel approximately 40% of the time.", False, "substantial_travel:40%"),
    ("Travel Requirements 100% onsite.", False, None),
])
def test_travel_rejects_only_majority_travel(sentence, hard, flag):
    hit, flags = cq._travel(sentences(sentence))
    assert bool(hit) is hard
    assert (flag in flags) if flag else not flags


# ---------------------------------------------------------------------------- role fit

def test_a_target_title_without_supporting_responsibilities_is_preserved_not_qualified():
    (case,) = by_requirement("title_without_responsibilities_stays_review")
    assert candidate(case["row"])["outcome"] == "review:title_without_responsibility_evidence"


def test_industrial_automation_is_not_mapped_to_software_by_title():
    (case,) = by_requirement("industrial_automation_title_stays_review")
    assert candidate(case["row"])["outcome"] == "review:industrial_engineering_evidence"


def test_account_manager_group_is_decided_by_responsibilities():
    (case,) = by_requirement("responsibilities_decide_account_manager_group")
    result = candidate(case["row"])
    assert result["outcome"] == "qualified_pre_contact"
    assert result["group"] == "customer_success_support"


def test_out_of_group_function_is_not_qualified():
    (case,) = by_requirement("out_of_group_function_not_qualified")
    assert case["current_outcome"] == "qualified_pre_contact"   # production qualifies people_hr today
    assert candidate(case["row"])["outcome"] == "rejected:outside_four_groups"


# ---------------------------------------------------------------------------- legitimate exclusions stay

@pytest.mark.parametrize("requirement,outcome", [
    ("staffing_intermediary_blocked", "rejected:agency:staffing_text"),
    ("non_full_time_blocked", None),
    ("commission_only_blocked", "rejected:employment:unpaid"),
    ("excluded_industry_blocked", "rejected:employer_excluded_industry:chemicals"),
])
def test_legitimate_exclusions_remain_blocked(requirement, outcome):
    for case in by_requirement(requirement):
        result = candidate(case["row"])
        assert result["outcome"].startswith("rejected:")
        assert result["outcome"] == (outcome or case["current_outcome"])
        assert result["evidence"], "every rejection carries its evidence"


def _qualified_row():
    (case,) = by_requirement("people_management_not_rejected")
    return copy.deepcopy(case["row"])


def test_crm_company_is_blocked():
    row = _qualified_row()
    crm = cq.crm_keys([row["organization"], "Some Other Company"])
    assert candidate(row, crm=crm)["outcome"] == "rejected:crm_existing_company"
    assert candidate(row, steps=tuple(s for s in cq.ALL_STEPS if s != cq.CRM_EXCLUSION), crm=crm)["outcome"] \
        == "qualified_pre_contact"


def test_agency_employer_is_blocked():
    row = _qualified_row()
    row["org_linkedin_recruitment_agency_derived"] = True
    assert candidate(row)["outcome"] in {"rejected:employer_is_agency", "rejected:agency:provider_recruitment_agency_flag"}


def test_provider_contract_label_blocks_but_conflicting_labels_are_flagged():
    row = _qualified_row()
    row["ai_employment_type"], row["employment_type"] = ["CONTRACTOR"], None
    assert candidate(row)["outcome"] == "rejected:employment:contract"
    row["ai_employment_type"] = ["FULL_TIME", "CONTRACTOR"]
    result = candidate(row)
    assert result["outcome"] == "qualified_pre_contact"
    assert "employment:provider_tags_conflict" in result["flags"]


# ---------------------------------------------------------------------------- unknown data is preserved

def test_unknown_company_size_and_missing_profile_are_flagged_not_rejected():
    row = _qualified_row()
    for key in ("org_linkedin_headcount", "org_linkedin_industry", "org_linkedin_slug", "org_linkedin_website"):
        row[key] = None
    # without the role-mapping step the current behaviour holds the job as ambiguous
    assert candidate(row, steps=(cq.SENIORITY_MANAGEMENT,))["outcome"] == "ambiguous:company_size_unknown"
    result = candidate(row)
    assert result["outcome"] == "qualified_pre_contact"
    assert {"company_size_unknown", "company_profile_missing"} <= set(result["flags"])


def test_missing_employment_type_is_flagged_not_rejected():
    row = _qualified_row()
    row["ai_employment_type"], row["employment_type"] = None, None
    result = candidate(row)
    if result["outcome"] == "qualified_pre_contact":
        assert "employment_type_unknown" in result["flags"] or "full" in row["description_text"].lower()
    assert not result["outcome"].startswith("rejected:employment")


def test_wellfound_and_yc_rows_carry_a_freshness_flag():
    row = _qualified_row()
    row["source"] = "wellfound"
    assert "source_not_in_expired_feed:needs_freshness_rule" in candidate(row)["flags"]


def test_provider_ai_fields_never_approve_a_job():
    (case,) = [c for c in CASES if c["row"]["title"] == "Janitor"]
    row = copy.deepcopy(case["row"])
    row.update(ai_taxonomies_a=["Software", "Technology"], ai_work_arrangement="Remote Solely")
    assert candidate(row)["outcome"] == case["candidate_outcome"]


# ---------------------------------------------------------------------------- industries and hygiene

@pytest.mark.parametrize("label,expected", [
    ("Non-profit Organizations", ("nonprofit", "rename")),
    ("Medical Practices", ("medical_practices", "rename")),
    ("Chemical Manufacturing", ("chemicals", "rename")),
    ("Newspaper Publishing", ("newspapers", "rename")),
    ("Hospitals and Health Care", ("hospitals_and_healthcare", "exact")),
    ("Administration of Justice", ("government_administration", "child")),
    ("Internet News", ("", "")),          # not on the approved list
    ("Software Development", ("", "")),
])
def test_approved_industry_labels(label, expected):
    assert cq.approved_industry_exclusion(label) == expected


def test_query_labels_are_exact_case_and_cover_every_category():
    labels = cq.approved_industry_query_labels()
    assert "Non-profit Organizations" in labels and "Medical Practices" in labels and "Chemical Manufacturing" in labels
    assert all(label == label.strip() and label[0].isupper() for label in labels)
    assert {cq.approved_industry_exclusion(label)[0] for label in labels} == {
        c for _, c, _ in cq.APPROVED_INDUSTRIES}
    assert "Internet News" not in labels and "Online Media" not in labels


def test_unknown_step_is_refused():
    with pytest.raises(ValueError):
        candidate(CASES[0]["row"], steps=("no_such_step",))


def test_deterministic():
    for case in CASES:
        assert candidate(case["row"]) == candidate(case["row"])


def test_no_model_database_or_provider_imports():
    tree = ast.parse(Path(cq.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = ("anthropic", "inference", "psycopg", "db", "providers", "services", "requests", "apollo", "airtable",
              "instantly")
    assert not [m for m in imported if any(b in m.split(".") or m.endswith(b) for b in banned)], imported
