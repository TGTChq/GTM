"""Phase 3 audit (2026-09-20): qualification recovery — valid jobs the rules still reject,
and campaign assignments the classifier still makes without evidence.

Every change here comes from a ruling Luis has already made (OPEN_DECISIONS.md, resolution pass
2026-09-20) or from the frozen rubric's own "not sufficient" list, measured on the CALIBRATION
stratum and the 411-item purchased-corpus set. The holdout is not read.

One hypothesis per commit:
  A  industry mapping      — online/digital media is not an approved excluded industry (Q6, D3)
  B  substring exclusion   — a Public Trust determination is not a security clearance (D5)
  C  location/travel       — the numeric travel line is withdrawn; "essential" carries the test (D4)
  D  classifier evidence   — a campaign assignment needs deterministic support, per campaign
"""
from __future__ import annotations

from tgtc_core.domain.classification import (
    SEMANTIC_MIN_DETERMINISTIC_HITS, classify_posting, score_functions,
)
from tgtc_core.domain.exclusion_evidence import corroborates
from tgtc_core.domain.inference import InferenceResponse, ResponsibilityItem
from tgtc_core.domain.facts import RULE_VERSION, extract_job_facts
from tgtc_core.policy.requirements import EXCLUDED_INDUSTRIES, excluded_industry

#: Enough ordinary prose that the postings under test are judged on the clause being tested,
#: not on being too short for the classifier to read.
_FILLER = ("You will build reports and dashboards for the analytics team, work with stakeholders "
           "across the business, and document your work. This role is remote.")


# --- A: industry mapping ----------------------------------------------------
# Luis, OPEN_DECISIONS.md: "Online news, digital media and media production stay allowed —
# not among the fixed exclusions" (Q6, restated in the 2026-09-20 resolution pass), and D3
# keeps the APPROVED list as-is. The rubric's own §4.K agrees: "online news, digital media,
# internet publishing and media production are NOT on the approved list ... do not exclude".
# Broadcast media, newspapers and book publishing ARE on the approved list and stay.

MEDIA_LABELS_LUIS_ALLOWS = (
    "online media", "internet news", "news media", "media production",
    "digital news", "financial news",
)
APPROVED_MEDIA_EXCLUSIONS = ("broadcast media", "newspapers", "book publishing")


def test_online_and_digital_media_industries_are_not_excluded():
    for label in MEDIA_LABELS_LUIS_ALLOWS:
        assert excluded_industry(label) == "", label
        assert label not in EXCLUDED_INDUSTRIES, label


def test_the_approved_media_exclusions_are_untouched():
    for label in APPROVED_MEDIA_EXCLUSIONS:
        assert excluded_industry(label) == label, label


def test_the_rest_of_the_approved_industry_list_is_untouched():
    for label in ("staffing and recruiting", "government administration", "mental health care",
                  "hospitals and health care", "human resources services", "outsourcing/offshoring",
                  "events services", "chemicals", "medical practice"):
        assert excluded_industry(label) == label, label


def test_an_apollo_qualifier_suffix_still_matches_an_approved_label():
    assert excluded_industry("Broadcast Media / Television") == "broadcast media"


# --- B: a Public Trust determination is not a security clearance ------------
# Luis, OPEN_DECISIONS.md D5 (2026-09-20 resolution pass): "Only an actual security
# clearance (Secret/TS/SCI) or an explicitly stated federal clearance requirement excludes.
# A Public Trust determination, a Tier 1 investigation, an HSPD-12 PIV card and ordinary
# background checks do NOT." The approved exclusion is "required federal clearance", and a
# Public Trust determination is not a clearance.
#
# The frozen rubric §4.H agrees on the substring half: "the phrase 'public trust' used in a
# non-clearance sense ('building public trust')" is explicitly NOT sufficient. Measured on
# the two frozen corpora (7,349 rows): 46 rows are excluded on the bare "public trust"
# substring with no other clearance evidence in the posting; one of them is a Public
# Information Officer whose text reads "building public trust and community engagement".

_PUBLIC_TRUST_ONLY = (
    "Ability to hold a position of public trust with the US government.",
    "Clearance Requirement: NACI (Public Trust) to be obtained after Government acceptance.",
    "Must meet eligibility for a Public Trust background investigation for IT access.",
    "The PIO serves as the media liaison, building public trust and community engagement.",
    "Candidates must pass a standard background check and an HSPD-12 PIV credentialing process.",
)

_REAL_CLEARANCE = (
    "Clearance Requirement: Active TS/SCI clearance required. Active CI Polygraph required.",
    "Must be able to obtain and maintain a Top Secret security clearance based on a T5 investigation.",
    "Candidate must have the ability to hold and maintain a Secret level security clearance.",
    "Requires Top Secret/SCI with Full Scope Poly.",
)


def test_public_trust_alone_is_not_a_clearance_exclusion():
    for text in _PUBLIC_TRUST_ONLY:
        facts = extract_job_facts(title="Data Analyst", description=text + " " + _FILLER)
        assert not any(e.reason == "deliverability:security_clearance" for e in facts.exclusions), text
        assert facts.facts["security_clearance"].value is None, text


def test_a_real_clearance_requirement_still_excludes():
    for text in _REAL_CLEARANCE:
        facts = extract_job_facts(title="Software Engineer", description=text + " " + _FILLER)
        assert any(e.reason == "deliverability:security_clearance" for e in facts.exclusions), text


def test_public_trust_beside_a_real_clearance_still_excludes():
    facts = extract_job_facts(
        title="Principal Systems Analyst",
        description="Clearance Level Must Currently Possess: Top Secret/SCI. Public Trust/Other Required: None. " + _FILLER)
    assert any(e.reason == "deliverability:security_clearance" for e in facts.exclusions)


def test_the_clearance_decision_path_carries_the_audit_rule_version():
    facts = extract_job_facts(title="Software Engineer", description=_REAL_CLEARANCE[0] + " " + _FILLER)
    exclusion = next(e for e in facts.exclusions if e.reason == "deliverability:security_clearance")
    assert exclusion.rule_version == RULE_VERSION
    assert facts.facts["security_clearance"].rule_version == RULE_VERSION


# --- C: a physical-DEMANDS clause is a fact, not an exclusion ---------------
# The frozen rubric §4.F names this verbatim under "Not sufficient": "lifting or
# physical-demands boilerplate ('may lift up to 25 lbs', 'sitting for long periods')",
# and asks for it to be recorded as `physical_duties = incidental` instead. Phase 2
# (task 3) narrowed the lift pattern only when the sentence ALSO carried ADA
# boilerplate ("occasionally", "as needed", "with reasonable accommodation").
#
# Measured on the two frozen corpora (7,349 net-new rows): **320 rows (4.35%)** are still
# excluded on a lift/lifting-pounds span that is the posting's ONLY facility evidence,
# and **none of those 320** carries the ADA qualifier phase 2 looks for -- the carve-out
# does not reach them. A labelled example: "Sr. Construction Project Engineer", labelled
# `qualified` / `operations` with `physical_duties = incidental`, excluded on the single
# span "Ability to lift up to 25 lbs."
#
# So the pattern is deleted rather than carved out a third time: a stated weight is a
# measure of physical DEMANDS, and the approved exclusion is about physical DUTIES. The
# clause is kept as a FACT (`physical_demands_statement`) so the evidence is not lost.
# Every other FACILITY span -- forklift/pallet jack, must work in a lab/warehouse/plant,
# physical presence required, front-desk greeting, patient care -- is untouched and still
# excludes on its own.

_LIFT_ONLY = (
    "Ability to lift up to 25 lbs.",
    "Physical demands: must be able to lift 50 pounds.",
    "The role requires lifting up to 30 lbs and sitting for long periods.",
)

_REAL_PHYSICAL_DUTY = (
    "Operate a forklift for the duration of the shift and palletize orders on the floor.",
    "Must work in the warehouse alongside the shift team.",
    "Physical presence is required for this position.",
)


def test_a_lifting_clause_alone_no_longer_excludes():
    for text in _LIFT_ONLY:
        facts = extract_job_facts(title="Project Engineer", description=text + " " + _FILLER)
        assert not any("physical" in e.reason for e in facts.exclusions), text


def test_the_lifting_clause_is_still_recorded_as_a_fact():
    facts = extract_job_facts(title="Project Engineer", description=_LIFT_ONLY[0] + " " + _FILLER)
    statement = facts.facts["physical_demands_statement"]
    assert statement.value == "stated"
    assert "lift up to 25 lbs" in statement.excerpt
    assert statement.rule_version == RULE_VERSION


def test_no_physical_demands_statement_when_the_posting_has_none():
    facts = extract_job_facts(title="Analyst", description=_FILLER)
    assert facts.facts["physical_demands_statement"].value is None


def test_a_real_physical_duty_still_excludes():
    for text in _REAL_PHYSICAL_DUTY:
        facts = extract_job_facts(title="Warehouse Associate", description=text + " " + _FILLER)
        assert any("physical" in e.reason for e in facts.exclusions), text


def test_a_lifting_clause_beside_a_real_physical_duty_still_excludes():
    facts = extract_job_facts(
        title="Warehouse Associate",
        description="Lift up to 50 pounds while operating a forklift on the floor. " + _FILLER)
    assert any("physical" in e.reason for e in facts.exclusions)


def test_the_model_cannot_exclude_on_a_lifting_clause_either():
    """`exclusion_evidence.corroborates` is the semantic twin of the FACILITY gate: it
    decides whether a model-claimed hard exclusion is supported. It carried its OWN copy
    of the lift regex, so dropping the pattern from FACILITY alone would have left the
    classifier able to reject on exactly the evidence the deterministic path no longer
    accepts."""
    for excerpt in ("Ability to lift up to 25 lbs.",
                    "Physical demands: must be able to lift 50 pounds.",
                    "The role requires lifting up to 30 lbs."):
        assert not corroborates("physical_work", excerpt, excerpt), excerpt


def test_the_model_can_still_exclude_on_a_real_physical_duty():
    for excerpt in ("Operate a forklift and assemble equipment on the floor.",
                    "Cleaning restrooms and sanitizing floors each shift.",
                    "Patrolling the premises overnight."):
        assert corroborates("physical_work", excerpt, excerpt), excerpt


def test_a_lifting_clause_beside_a_real_duty_still_corroborates():
    excerpt = "Lift up to 50 pounds while operating machinery on the floor."
    assert corroborates("physical_work", excerpt, excerpt)


# --- C: travel volume alone is not essential field travel -------------------
# Luis, OPEN_DECISIONS.md D4 (2026-09-20 resolution pass): "Exclude when field or
# territory duties are a CORE RESPONSIBILITY. Drop the proposed >= 50% numeric: it was
# my invention, and 'essential' already carries the test. Incidental conference travel
# never excludes."
#
# The deployed rule excluded at >= 20% stated travel -- stricter than both the number
# Luis withdrew and the frozen rubric's own 50% parameter (§4.G, which additionally
# names "conferences, quarterly off-sites, trips to HQ" as NOT sufficient). The numeric
# patterns are removed. The qualitative essentiality markers (frequent travel, travel
# regularly, must live near an airport) and the whole FIELD list are untouched, so a
# genuine field or territory role still excludes -- through evidence of the DUTY, which
# is what the approved exclusion asks for.

_TRAVEL_PERCENT_ONLY = (
    "Ability to travel up to 25% of the time to industry events and conferences.",
    "Travel up to 20%.",
    "Able to work overtime and travel up to 25% to various office locations.",
    "Both Domestic and International Travel up to 20%.",
    "This role requires travel approximately 50% of the time.",
)

_TRAVEL_STILL_EXCLUDES = (
    "This position is a support role with frequent travel, typically three to five-day trips.",
    "You will travel regularly to keep the accounts moving.",
    "Must live near an airport.",
)


def test_a_stated_travel_percentage_alone_no_longer_excludes():
    for text in _TRAVEL_PERCENT_ONLY:
        facts = extract_job_facts(title="Social Media Lead", description=text + " " + _FILLER)
        assert not any(e.reason == "deliverability:travel" for e in facts.exclusions), text


def test_essential_travel_language_still_excludes():
    for text in _TRAVEL_STILL_EXCLUDES:
        facts = extract_job_facts(title="Account Manager", description=text + " " + _FILLER)
        assert any(e.reason == "deliverability:travel" for e in facts.exclusions), text


def test_field_duties_still_exclude_independently_of_travel_volume():
    for text in ("This is a field-based role.", "You will regularly visit customer sites.",
                 "The work is performed on customer sites."):
        facts = extract_job_facts(title="Engineer", description=text + " " + _FILLER)
        assert any(e.reason == "deliverability:field_work" for e in facts.exclusions), text


def test_the_travel_decision_path_carries_the_audit_rule_version():
    facts = extract_job_facts(title="Account Manager",
                              description=_TRAVEL_STILL_EXCLUDES[0] + " " + _FILLER)
    exclusion = next(e for e in facts.exclusions if e.reason == "deliverability:travel")
    assert exclusion.rule_version == RULE_VERSION
    assert facts.facts["work_arrangement"].rule_version == RULE_VERSION


# --- D: a semantic campaign assignment needs deterministic support, per campaign ----
# Brief (phase 3): the classifier "must not approve on confidence or a copied quote
# alone". The branch already refuses a `gtm_revenue` assignment backed by ZERO
# deterministic evidence (scoped re-review, IMPORTANT 2). This generalises that ONE
# predicate into a per-campaign table instead of copying it per function.
#
# The table is measured, not assumed. On the CALIBRATION stratum only (holdout not
# read), replaying the 129 already-bought answers through score_functions --
# `qualification_recovery/route_evidence.py`, 109 assignments over 70 items -- requiring
# >= 1 distinct deterministic hit for the assigned function withholds:
#
#   function          withheld right / wrong     precision of that function
#   customer_success        0 / 3                 0.200 -> 0.500
#   engineering             0 / 2                 0.444 -> 0.571
#   people_hr               0 / 2                 0.286 -> 0.400
#   ecommerce               0 / 2                 0.200 -> 0.333
#   finance                 0 / 1                 0.429 -> 0.500
#   ---- not enrolled: ----
#   operations              4 / 8                 0.364 -> 0.400   (costs 4 right)
#   product                 1 / 2                 0.467 -> 0.500   (costs 1 right)
#   customer_support        1 / 1                 0.300 -> 0.250   (costs 1, gains none)
#   marketing               0 / 0                 no measured effect either way
#
# Only the five that withhold ZERO labelled-right assignments are enrolled, plus
# gtm_revenue which was already enrolled. `marketing` is left out deliberately: a
# change with no measured effect is not evidence, and the brief says not to generalise
# this shape blind.
#
# Withholding is never a rejection: the function is dropped, the posting keeps any
# other function, and with none left it closes as `insufficient_evidence:*`, which
# classification_service's reopen query re-enters when a new model version appears.

_CS_NO_EVIDENCE = (
    "You will be the main point of contact for a set of enterprise logos after they sign, "
    "keeping them happy, running their business cadence with us, and making sure they see value "
    "from what they bought. You will coordinate internally and escalate where needed.")
_CS_WITH_EVIDENCE = (
    "You will run customer onboarding for new accounts, drive product adoption across your "
    "portfolio of customers, and own renewals and retention for the book. Quarterly business "
    "reviews and health scores are part of the cadence.")
_OPS_NO_EVIDENCE = (
    "You will keep the wheels turning day to day, chase down whatever is blocking the team, and "
    "bring order to how work moves through the business. A generalist role with a wide remit.")


class _FixedAnswerD:
    """A port that returns exactly the functions asked for, grounded and confident."""

    def __init__(self, functions, excerpt):
        self._functions, self._excerpt = list(functions), excerpt

    def classify(self, request):
        return InferenceResponse(
            available=True, compatible_functions=list(self._functions),
            responsibilities=[ResponsibilityItem("the work described", self._excerpt)],
            seniority="unknown", people_management=None, incompatible_reasons=[],
            exclusion_evidence=[], confidence=0.95, model_version="test:fixed/1")


def test_the_enrolled_fixtures_really_carry_the_evidence_they_claim():
    """Precondition, so the tests below cannot silently stop testing what they say."""
    assert score_functions(_CS_NO_EVIDENCE)[1].get("customer_success") in (None, [])
    assert score_functions(_CS_WITH_EVIDENCE)[1].get("customer_success")
    assert score_functions(_OPS_NO_EVIDENCE)[1].get("operations") in (None, [])


def test_an_enrolled_campaign_with_no_deterministic_evidence_is_withheld():
    for function in ("customer_success", "finance", "engineering", "people_hr", "ecommerce"):
        result = classify_posting(title="Specialist", description=_CS_NO_EVIDENCE,
                                  inference=_FixedAnswerD([function], _CS_NO_EVIDENCE[:80]))
        assert result.method == "semantic", function
        assert function not in result.compatible_functions, function
        assert result.campaign_keys == [], function
        assert not result.excluded, "an unqualified guess is review, never a reject"
        assert any(n == f"insufficient_evidence:{function}_unsupported_by_deterministic_evidence"
                   for n in result.notes), (function, result.notes)
        assert result.rule_version == RULE_VERSION, function


def test_an_enrolled_campaign_with_deterministic_evidence_still_routes():
    result = classify_posting(title="Customer Success Manager", description=_CS_WITH_EVIDENCE,
                              inference=_FixedAnswerD(["customer_success"], _CS_WITH_EVIDENCE[:80]))
    assert result.compatible_functions == ["customer_success"]
    assert result.campaign_keys == ["customer_experience"]
    assert not result.excluded


def test_a_campaign_that_is_not_enrolled_is_not_affected():
    """operations, product, marketing and customer_support are deliberately NOT enrolled:
    on calibration the requirement costs them labelled-right assignments."""
    for function in ("operations", "product", "marketing", "customer_support"):
        result = classify_posting(title="Associate", description=_OPS_NO_EVIDENCE,
                                  inference=_FixedAnswerD([function], _OPS_NO_EVIDENCE[:80]))
        assert result.compatible_functions == [function], function
        assert not result.excluded, function


def test_a_second_function_survives_when_only_the_enrolled_one_is_unsupported():
    result = classify_posting(title="Associate", description=_OPS_NO_EVIDENCE,
                              inference=_FixedAnswerD(["finance", "operations"], _OPS_NO_EVIDENCE[:80]))
    assert result.compatible_functions == ["operations"]
    assert result.campaign_keys == ["operations"]
    assert not result.excluded
    assert [w["function"] for w in result.facts["withheld_functions"]] == ["finance"]


def test_every_withheld_function_is_recorded_with_its_rule_version():
    result = classify_posting(title="Associate", description=_OPS_NO_EVIDENCE,
                              inference=_FixedAnswerD(["finance", "ecommerce"], _OPS_NO_EVIDENCE[:80]))
    withheld = result.facts["withheld_functions"]
    assert [w["function"] for w in withheld] == ["finance", "ecommerce"]
    assert all(w["code"] == "no_deterministic_function_evidence" for w in withheld)
    assert all(w["rule_version"] == RULE_VERSION for w in withheld)
    assert not result.excluded and result.compatible_functions == []


def test_the_enrolment_table_matches_what_was_measured():
    assert set(SEMANTIC_MIN_DETERMINISTIC_HITS) == {
        "gtm_revenue", "customer_success", "engineering", "people_hr", "ecommerce", "finance"}
    assert all(v == 1 for v in SEMANTIC_MIN_DETERMINISTIC_HITS.values())
