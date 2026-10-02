"""Synthetic regressions, not a measurement of live-cohort conversion."""
import math

import pytest

from tgtc_core.domain.classification import ClassificationResult, _apply_semantic, score_functions
from tgtc_core.domain.facts import JobFacts
from tgtc_core.domain.inference import InferenceResponse, ResponsibilityItem


def decide(description, *, code="", excerpt="", confidence=0.95, **kwargs):
    response = InferenceResponse(available=True, confidence=confidence,
        exclusion_evidence=[{"code": code, "excerpt": excerpt}] if code else [], **kwargs)
    # C1 (final whole-branch review): _apply_semantic now requires this
    # description's own deterministic evidence, so the GTM Systems scope
    # predicate governs the semantic assignment too. Built with the SAME
    # score_functions call classify_posting makes, not a stub.
    return _apply_semantic(ClassificationResult(), response, description, JobFacts(), score_functions(description)[1])


@pytest.mark.parametrize("confidence", [0, 0.05, 0.79, -1, 1.1, math.nan, math.inf])
def test_rejecting_requires_bounded_confidence(confidence):
    text = "You will provide direct patient care in the hospital."
    r = decide(text, code="clinical_care", excerpt=text, confidence=confidence)
    assert not r.decided
    assert "ignored_semantic_exclusion:low_confidence" in r.notes


@pytest.mark.parametrize("claim", [
    {"incompatible_reasons": ["The job is clinical"]},
    {"people_management": True}, {"seniority": "director_plus"},
])
def test_a_model_claim_without_a_quote_is_not_a_business_rejection(claim):
    r = decide("The employee prepares remote digital reports.", **claim)
    assert not r.decided
    assert "ignored_semantic_exclusion:ungrounded_or_unsupported" in r.notes


@pytest.mark.parametrize("code,text", [
    ("clinical_care", "You will provide direct patient care in the hospital."),
    ("physical_work", "You will clean floors and restrooms at the facility."),
    ("security_clearance", "An active security clearance is required for this role."),
    ("substantial_travel", "This role requires up to 25% travel."),
    ("employment", "This is a part-time position with a fixed schedule."),
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
    # Phase 2 audit task 1 (2026-09-19, Luis): a title word (Director/VP/Head/Chief/
    # Lead/Senior) is never on its own grounds for rejection -- corroborates() no
    # longer finds a "seniority:" exclusion in facts.py to back this model claim, so
    # it is correctly ignored rather than honoured. See tgtc_core/domain/facts.py
    # (role_level fact) and .superpowers/sdd/phase2/task-1-3-brief.md.
    ("seniority", "This position is a director of operations."),
    # Phase 2 audit fix round 1 (2026-09-19, CRITICAL finding): people management is
    # never on its own grounds for rejection either, on THIS (semantic/LLM) path any
    # more than on the deterministic one -- corroborates() no longer finds a
    # "people_management" exclusion in facts.py to back this model claim. Before this
    # fix, corroborates() called has_people_authority(excerpt) directly, bypassing
    # facts.exclusions entirely, so task 2 alone did not close this path.
    ("people_management", "You will supervise a team of employees."),
])
def test_a_real_quote_alone_does_not_establish_an_exclusion(code, text):
    r = decide(text, code=code, excerpt=text)
    assert not r.decided


def test_semantic_incidental_lifting_claim_is_not_corroborated():
    """CRITICAL finding, fix round 1 (2026-09-19): PATTERNS["physical_work"][0] is
    byte-identical to the pre-task-3 facts.FACILITY lift/lifting pattern, but was
    never updated with facts.FACILITY_LIFT_INCIDENTAL. Reuses that same regex
    object (not a copy) so the two rules cannot drift apart again."""
    text = "Physical demands: must be able to lift up to 25 pounds occasionally."
    r = decide(text, code="physical_work", excerpt=text)
    assert not r.decided


def test_semantic_lift_with_other_physical_evidence_still_corroborates():
    """The narrowing only drops a lift/lifting-alone-with-an-ADA-qualifier clause;
    a genuinely physical sentence (here, lift + forklift together) still excludes
    on the semantic path, exactly as on the deterministic one."""
    text = "Occasionally lift up to 25 pounds while operating a forklift on the floor."
    r = decide(text, code="physical_work", excerpt=text)
    assert r.excluded and r.exclusion_reason == "semantic_evidence:physical_work"


def test_semantic_genuine_physical_work_claim_still_corroborates():
    text = "Operate a forklift for the duration of the shift and palletize orders on the floor."
    r = decide(text, code="physical_work", excerpt=text)
    assert r.excluded and r.exclusion_reason == "semantic_evidence:physical_work"


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


@pytest.mark.parametrize("confidence,note", [
    (0.79, "ignored_semantic_exclusion:low_confidence"),
    (0.95, "ignored_semantic_exclusion:ungrounded_or_unsupported"),
])
def test_unproven_exclusion_does_not_erase_grounded_positive_routing(confidence, note):
    text = "The employee coordinates internal activities across teams."
    r = decide(
        text, code="people_management", excerpt="You will supervise a team",
        confidence=confidence, compatible_functions=["operations"], responsibilities=[
            ResponsibilityItem("internal coordination", "coordinates internal activities across teams")
        ], people_management=True,
    )
    assert r.compatible_functions == ["operations"] and not r.excluded
    assert note in r.notes
