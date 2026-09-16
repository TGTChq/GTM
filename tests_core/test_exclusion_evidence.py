"""Synthetic regressions, not a measurement of live-cohort conversion."""
import math

import pytest

from tgtc_core.domain.classification import ClassificationResult, _apply_semantic
from tgtc_core.domain.facts import JobFacts
from tgtc_core.domain.inference import InferenceResponse, ResponsibilityItem


def decide(description, *, code="", excerpt="", confidence=0.95, **kwargs):
    response = InferenceResponse(available=True, confidence=confidence,
        exclusion_evidence=[{"code": code, "excerpt": excerpt}] if code else [], **kwargs)
    return _apply_semantic(ClassificationResult(), response, description, JobFacts())


@pytest.mark.parametrize("confidence", [0, 0.05, 0.79, -1, 1.1, math.nan, math.inf])
def test_rejecting_requires_bounded_confidence(confidence):
    text = "You will provide direct patient care in the hospital."
    r = decide(text, code="clinical_care", excerpt=text, confidence=confidence)
    assert not r.decided
    assert "insufficient_evidence:semantic_exclusion_low_confidence" in r.notes


@pytest.mark.parametrize("claim", [
    {"incompatible_reasons": ["The job is clinical"]},
    {"people_management": True}, {"seniority": "director_plus"},
])
def test_a_model_claim_without_a_quote_is_not_a_business_rejection(claim):
    r = decide("The employee prepares remote digital reports.", **claim)
    assert not r.decided
    assert "insufficient_evidence:semantic_exclusion_ungrounded_or_unsupported" in r.notes


@pytest.mark.parametrize("code,text", [
    ("clinical_care", "You will provide direct patient care in the hospital."),
    ("physical_work", "You will clean floors and restrooms at the facility."),
    ("people_management", "You will supervise a team of employees."),
    ("security_clearance", "An active security clearance is required for this role."),
    ("substantial_travel", "This role requires up to 25% travel."),
    ("employment", "This is a part-time position with a fixed schedule."),
    ("seniority", "This position is a director of operations."),
    ("field_work", "This is a field-based position at customer premises."),
])
def test_grounded_supported_exclusions_have_an_audit_trail(code, text):
    r = decide(text, code=code, excerpt=text)
    assert r.excluded and r.exclusion_reason == f"semantic_evidence:{code}"
    assert r.facts["semantic_exclusion"]["excerpt"] == text


@pytest.mark.parametrize("code,text", [
    ("substantial_travel", "Requires residence in Virginia and occasional in-person travel."),
    ("physical_work", "This position is office-based with hybrid flexibility."),
    ("clinical_care", "Our software serves nurses providing direct patient care."),
    ("people_management", "You manage projects and vendor relationships."),
    ("security_clearance", "No security clearance is required for this role."),
    ("clinical_care", "Experience providing direct patient care is preferred."),
    ("made_up_rule", "You will provide direct patient care in the hospital."),
])
def test_a_real_quote_alone_does_not_establish_an_exclusion(code, text):
    r = decide(text, code=code, excerpt=text)
    assert not r.decided


def test_fabricated_excerpt_cannot_reject_and_cannot_silently_approve():
    r = decide("This is remote digital work.", code="clinical_care",
        excerpt="You will provide direct patient care.", compatible_functions=["operations"])
    assert not r.decided


def test_quote_cannot_drop_its_negation_or_customer_context():
    for text, quote, code in [
        ("No security clearance is required for this role.", "security clearance is required", "security_clearance"),
        ("Our software serves nurses providing direct patient care.", "providing direct patient care", "clinical_care"),
    ]:
        assert not decide(text, code=code, excerpt=quote).decided


def test_supported_positive_responsibilities_still_work():
    text = "The employee coordinates internal activities across teams."
    r = decide(text, compatible_functions=["operations"], responsibilities=[
        ResponsibilityItem("internal coordination", "coordinates internal activities across teams")])
    assert r.compatible_functions == ["operations"] and not r.excluded
