"""Outbound Wave 1 experiment-integrity checks.

Three properties this file exists to protect:

1. **A challenger copy failure never becomes control traffic.** Eligibility is
   decided before randomisation, so a record that cannot render is suppressed
   from the experiment entirely rather than relabelled A.
2. **A degrade moves friction, proof and offer together.** A cost-framed friction
   must never survive into a testing offer.
3. **The offer noun is the literal rendered text**, resolved once for E1 and
   repeated verbatim in E2/E3/E4.
"""

from __future__ import annotations

import collections
import json
import tempfile
from pathlib import Path

import pytest

import config
from outbound_wave1 import (
    ARM_A,
    ARM_B,
    ARM_NONE,
    campaign_for_bucket,
    resolve_batch,
    resolve_challenger,
    resolve_wave1,
)
from outbound_wave1.campaigns import (
    OFFER_ROLE_ECONOMICS,
    OFFER_TESTING_OVERVIEW,
    PROOF_ECONOMICS,
    PROOF_TESTING_MECHANICS,
    PROOF_EMPLOYMENT_ADMIN,
    OFFER_EMPLOYMENT_ADMIN_OVERVIEW,
    VALID_OFFER_NOUNS,
)
from outbound_wave1.claims import empty_registry, load_claim_registry
from outbound_wave1.qa import run_qa_gates
from outbound_wave1.render import FRICTION_COST_COMPARISON

EXPERIMENT = "test_wave1_integrity"


def _fields(**overrides):
    base = {
        "Lead Key": "acme.com|dana@acme.com|finance",
        "Company": "Acme Corporation",
        "Outbound Company": "Acme",
        "Outbound Company Identity": "domain:acme.com",
        "Outbound Company Confidence": "high",
        "Outbound Role": "Financial Analyst",
        "Outbound Roles": "Financial Analyst",
        "Outbound Role Confidence": "high",
        "Open Role": "Financial Analyst",
        "Open Roles": "Financial Analyst",
        "Matched Role": "Financial Analyst",
        "Role Bucket": "finance",
        "Role Focus": "financial reporting, budgeting, and variance analysis",
        "Focus Quality": "specific",
        "Focus Evidence": "financial reporting | budget cycle | variance",
        "Hiring Manager": "Dana Reeves",
        "HM Title": "Controller",
        "Website": "https://acme.com",
        "Posted At": "2026-08-01T00:00:00+00:00",
        "Job Freshness": "aging",
        "Job URL Status": "unverified_review",
        "Job URL": "https://acme.com/jobs/1",
        "Job Source": "linkedin",
        "Outbound Hold": False,
    }
    base.update(overrides)
    return base


#: A stored role display that cannot be rendered safely (pipe-delimited dump).
UNRENDERABLE_ROLE = "Financial Analyst | Remote | 90k"


def _unrenderable_fields(**overrides):
    fields = _fields(**overrides)
    fields["Outbound Role"] = UNRENDERABLE_ROLE
    fields["Outbound Roles"] = UNRENDERABLE_ROLE
    return fields


def _registry_with_economics(role="Financial Analyst", **page):
    payload = {
        "schema": "tgtc-outbound-wave1-claims/1",
        "claims": {
            "remote_readiness_1000_hires": {
                "text": "remote-readiness assessment built from 1,000+ hires",
                "verified": True,
                "claim_source": "wave1_frozen_spec:people_hr_remote_readiness",
            }
        },
        "role_pages": {
            role: {
                "canonical_role": role,
                "url": page.get("url", "https://example.invalid/roles/x"),
                "economics_available": page.get("economics_available", True),
                "monthly_cost_usd": page.get("monthly_cost_usd", 2400),
                "local_comparison_published": page.get("local_comparison_published", True),
                "claim_source": page.get("claim_source", "https://example.invalid/roles/x"),
            }
        },
    }
    target = Path(tempfile.mkdtemp()) / "claims.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return load_claim_registry(str(target))


# ---------------------------------------------------------------------------
# 1. Suppression, never reassignment
# ---------------------------------------------------------------------------

def test_a_challenger_copy_failure_is_suppressed_not_reassigned_to_control():
    resolution = resolve_wave1(
        _unrenderable_fields(), experiment_id=EXPERIMENT, b_split_pct=100,
        registry=empty_registry(),
    )
    assert resolution.experiment_arm == ARM_NONE
    assert resolution.experiment_arm != ARM_A
    assert resolution.wave1_eligible is False
    assert resolution.qa_pass is False
    assert "role_display_contains_unsafe_characters" in resolution.suppression_reason


def test_suppression_is_identical_at_every_split_so_it_is_arm_independent():
    outcomes = {
        split: resolve_wave1(
            _unrenderable_fields(), experiment_id=EXPERIMENT, b_split_pct=split,
            registry=empty_registry(),
        )
        for split in (0, 25, 50, 75, 100)
    }
    assert {r.experiment_arm for r in outcomes.values()} == {ARM_NONE}
    assert {r.wave1_eligible for r in outcomes.values()} == {False}
    assert len({r.suppression_reason for r in outcomes.values()}) == 1


def test_a_structurally_ineligible_record_is_suppressed_too():
    resolution = resolve_wave1(
        _fields(**{"Outbound Hold": True}), experiment_id=EXPERIMENT,
        b_split_pct=100, registry=empty_registry(),
    )
    assert resolution.experiment_arm == ARM_NONE
    assert not resolution.wave1_eligible
    assert "outbound_hold_set" in resolution.suppression_reason


def test_an_unassignable_company_is_suppressed_rather_than_treated_as_control():
    fields = _fields()
    for key in ("Outbound Company Identity", "Website", "Outbound Company", "Company"):
        fields[key] = ""
    resolution = resolve_wave1(
        fields, experiment_id=EXPERIMENT, b_split_pct=100, registry=empty_registry()
    )
    assert resolution.experiment_arm == ARM_NONE
    assert not resolution.wave1_eligible


def test_control_and_challenger_are_drawn_from_one_eligible_population():
    records = []
    for index in range(120):
        fields = _fields()
        fields["Outbound Company Identity"] = f"domain:c{index}.com"
        if index % 3 == 0:  # a third cannot render
            fields["Outbound Role"] = UNRENDERABLE_ROLE
            fields["Outbound Roles"] = UNRENDERABLE_ROLE
        records.append({"id": f"rec{index}", "fields": fields})

    resolutions, _previews, failures = resolve_batch(
        records, experiment_id=EXPERIMENT, b_split_pct=50, registry=empty_registry()
    )
    assert failures == []
    suppressed = [r for r in resolutions if r.experiment_arm == ARM_NONE]
    arm_a = [r for r in resolutions if r.experiment_arm == ARM_A]
    arm_b = [r for r in resolutions if r.experiment_arm == ARM_B]

    assert len(suppressed) == 40
    assert len(arm_a) + len(arm_b) + len(suppressed) == 120
    assert all(r.wave1_eligible and r.qa_pass for r in arm_a + arm_b)
    assert all(not r.wave1_eligible for r in suppressed)


def test_control_rows_keep_the_classification_but_none_of_the_copy():
    control = resolve_wave1(
        _fields(), experiment_id=EXPERIMENT, b_split_pct=0, registry=empty_registry()
    )
    assert control.experiment_arm == ARM_A
    assert control.wave1_eligible is True
    assert control.rendered_email_1 == ""
    assert control.rendered_subject == ""
    # Strata survive so control can be stratified exactly like treatment.
    assert control.campaign == "FINANCE"
    assert control.signal_tier
    assert control.proof_type
    assert control.outbound_offer_type
    assert control.friction_angle


def test_a_company_may_have_one_row_suppressed_and_another_in_an_arm():
    records = [
        {"id": "rec1", "fields": _fields()},
        {"id": "rec2", "fields": _unrenderable_fields()},
    ]
    resolutions, _previews, failures = resolve_batch(
        records, experiment_id=EXPERIMENT, b_split_pct=100, registry=empty_registry()
    )
    assert failures == []
    arms = {r.record_id: r.experiment_arm for r in resolutions}
    assert arms["rec1"] == ARM_B
    assert arms["rec2"] == ARM_NONE


# ---------------------------------------------------------------------------
# 2. Fallback coherence: friction -> proof -> offer degrade together
# ---------------------------------------------------------------------------

_ECONOMICS_LANGUAGE = (
    "monthly cost", "local cost", "the numbers for this role", "economics",
    "costs against a local hire",
)


def _campaign_fields(bucket, role):
    fields = _fields()
    fields.update({
        "Role Bucket": bucket,
        "Matched Role": role,
        "Outbound Role": role,
        "Outbound Roles": role,
    })
    return fields


def test_economics_fallback_changes_friction_proof_and_offer_together():
    """FINANCE is the only campaign left on the economics path.

    CUSTOMER EXPERIENCE and MARKETING & CREATIVE were moved off it: each has a
    verified angle that is stronger for its buyer than a price would be, so
    keeping an unreachable economics preference on them bought nothing.
    """
    bucket, role = "finance", "Financial Analyst"
    resolution = resolve_challenger(
        _campaign_fields(bucket, role), experiment_id=EXPERIMENT, registry=empty_registry()
    )
    assert resolution.role_page_match is False
    assert resolution.proof_type == PROOF_EMPLOYMENT_ADMIN
    assert resolution.outbound_offer_type == OFFER_EMPLOYMENT_ADMIN_OVERVIEW
    assert resolution.offer_fallback_type == OFFER_ROLE_ECONOMICS
    assert resolution.friction_angle != FRICTION_COST_COMPARISON
    assert resolution.offer_noun == campaign_for_bucket(bucket).offer_noun(
        OFFER_EMPLOYMENT_ADMIN_OVERVIEW
    )

    body = " ".join([
        resolution.rendered_email_1, resolution.rendered_email_2,
        resolution.rendered_email_3, resolution.rendered_email_4,
    ]).lower()
    for phrase in _ECONOMICS_LANGUAGE:
        assert phrase not in body, (bucket, phrase)
    assert resolution.qa_pass, resolution.qa_reasons


@pytest.mark.parametrize("bucket,role", [("finance", "Financial Analyst")])
def test_economics_path_uses_the_cost_friction_and_the_numbers_offer(bucket, role):
    registry = _registry_with_economics(role=role)
    resolution = resolve_challenger(
        _campaign_fields(bucket, role), experiment_id=EXPERIMENT, registry=registry
    )
    assert resolution.role_page_match is True
    assert resolution.proof_type == PROOF_ECONOMICS
    assert resolution.outbound_offer_type == OFFER_ROLE_ECONOMICS
    assert resolution.friction_angle == FRICTION_COST_COMPARISON
    assert resolution.offer_noun == "the numbers for this role"
    assert "costs against a local hire" in resolution.rendered_email_1
    assert f"monthly cost for {role}" in resolution.rendered_email_1
    assert resolution.qa_pass, resolution.qa_reasons


def test_cost_friction_paired_with_a_non_economics_offer_fails_qa():
    resolution = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=empty_registry()
    ).to_dict()
    assert resolution["proof_type"] == PROOF_EMPLOYMENT_ADMIN
    resolution["friction_angle"] = FRICTION_COST_COMPARISON
    passed, reasons = run_qa_gates(resolution, policy=campaign_for_bucket("finance"))
    assert not passed
    assert "friction_incoherent_with_proof_and_offer" in reasons


def test_economics_proof_paired_with_a_scope_friction_fails_qa():
    resolution = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=_registry_with_economics()
    ).to_dict()
    assert resolution["proof_type"] == PROOF_ECONOMICS
    resolution["friction_angle"] = "combined_scope_pool"
    passed, reasons = run_qa_gates(resolution, policy=campaign_for_bucket("finance"))
    assert not passed
    assert "friction_incoherent_with_proof_and_offer" in reasons


def test_wave1_is_launchable_with_no_economics_configured():
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    assert registry.economics_role_count == 0
    for bucket in config.CAMPAIGN_ENV_BY_BUCKET:
        fields = _fields()
        fields["Role Bucket"] = bucket
        resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=registry)
        assert resolution.qa_pass, (bucket, resolution.qa_reasons)
        assert resolution.proof_type != PROOF_ECONOMICS
        assert resolution.friction_angle != FRICTION_COST_COMPARISON


# ---------------------------------------------------------------------------
# 3. Offer wording: the exact rendered noun, inherited across the thread
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bucket,role,expected_noun",
    [
        ("product", "Product Manager", "how the headcount side works"),
        ("ecommerce", "Ecommerce Manager", "how our testing works"),
        ("operations", "Operations Analyst", "how we test for a scope like this"),
        ("gtm_revenue", "Revenue Operations Manager", "how we test the combination"),
        ("engineering", "AI Engineer", "how the assessment works"),
        ("people_hr", "Recruiter", "how we assess remote readiness"),
        ("finance", "Financial Analyst", "what we carry on the employment side"),
        ("marketing", "Growth Marketing Manager", "how an embedded hire works"),
        ("customer_success", "Customer Success Manager", "what the testing covers"),
    ],
)
def test_campaign_offer_noun_appears_verbatim_in_all_four_emails(bucket, role, expected_noun):
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    resolution = resolve_challenger(
        _campaign_fields(bucket, role), experiment_id=EXPERIMENT, registry=registry
    )
    assert resolution.offer_noun == expected_noun
    assert f"Want me to send {expected_noun}?" in resolution.rendered_email_1
    for index in (1, 2, 3, 4):
        body = getattr(resolution, f"rendered_email_{index}")
        capitalised = expected_noun[0].upper() + expected_noun[1:]
        assert expected_noun in body or capitalised in body, index
    assert resolution.qa_pass, resolution.qa_reasons


def test_economics_offer_noun_is_the_numbers_for_this_role_everywhere():
    resolution = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=_registry_with_economics()
    )
    noun = "the numbers for this role"
    assert resolution.offer_noun == noun
    assert f"Want me to send {noun}?" in resolution.rendered_email_1
    assert f"Happy to send {noun} if it" in resolution.rendered_email_2
    # The re-offer needs no plural agreement, so a plural noun reads naturally
    # without any word choice changing.
    assert f"Happy to send {noun} whenever." in resolution.rendered_email_3
    assert f"send {noun} over." in resolution.rendered_email_4


def test_the_re_offer_reads_the_same_for_any_noun():
    """The E3 re-offer is agreement-free, so it reads naturally whether the noun
    is singular or plural."""
    resolution = resolve_challenger(
        _campaign_fields("operations", "Operations Analyst"),
        experiment_id=EXPERIMENT, registry=empty_registry(),
    )
    assert (
        "Happy to send how we test for a scope like this whenever."
        in resolution.rendered_email_3
    )


def test_no_campaign_leaks_another_campaigns_offer_noun():
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    for bucket in config.CAMPAIGN_ENV_BY_BUCKET:
        fields = _fields()
        fields["Role Bucket"] = bucket
        resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=registry)
        thread = " ".join([
            resolution.rendered_email_1, resolution.rendered_email_2,
            resolution.rendered_email_3, resolution.rendered_email_4,
        ]).lower()
        for other in VALID_OFFER_NOUNS:
            if other != resolution.offer_noun:
                assert other not in thread, (bucket, other)


def test_a_swapped_noun_mid_thread_fails_qa():
    resolution = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=empty_registry()
    ).to_dict()
    resolution["rendered_email_3"] = resolution["rendered_email_3"].replace(
        "what we carry on the employment side", "the numbers for this role"
    )
    passed, reasons = run_qa_gates(resolution, policy=campaign_for_bucket("finance"))
    assert not passed
    assert "offer_changed_within_thread" in reasons
    assert "offer_noun_missing_from_email_3" in reasons


# ---------------------------------------------------------------------------
# 8. Strategic differentiation: nine arguments, not one argument nine times
# ---------------------------------------------------------------------------
#
# Wave 1's premise is that each campaign sells the reason ITS buyer has to care.
# An earlier revision drifted into one argument with nine wordings -- 8 of 9
# campaigns shipped the identical proof sentence and all 9 shared 3 CTA nouns.
# These gates make that regression fail the build instead of reaching a buyer.

#: One representative record per live campaign, on its T1 signal.
_DIFFERENTIATION_RECORDS = (
    ("product", "Product Manager", "Product Manager|Product Designer",
     "roadmap ownership | customer discovery"),
    ("operations", "Operations Manager", "Operations Manager",
     "vendor management | process documentation | SOP rollout"),
    ("finance", "Financial Analyst", "Financial Analyst",
     "month-end close | financial reporting | variance analysis"),
    ("people_hr", "Talent Acquisition Specialist", "Talent Acquisition Specialist",
     "full-cycle recruiting | onboarding"),
    ("ecommerce", "Ecommerce Manager", "Ecommerce Manager|Marketplace Specialist",
     "catalog operations | marketplace listings"),
    ("customer_success", "Customer Success Manager",
     "Customer Success Manager|Onboarding Specialist", "onboarding | renewals"),
    ("marketing", "Marketing Manager", "Marketing Manager",
     "paid social | lifecycle email | content production"),
    ("gtm_revenue", "Revenue Operations Manager", "Revenue Operations Manager",
     "hubspot administration | lead routing | pipeline reporting"),
    ("engineering", "AI Automation Engineer", "AI Automation Engineer",
     "workflow automation | LLM integration | API orchestration"),
)


def _differentiation_resolutions():
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    out = []
    for bucket, role, roles, evidence in _DIFFERENTIATION_RECORDS:
        fields = _campaign_fields(bucket, role)
        fields.update({"Outbound Roles": roles, "Open Roles": roles,
                       "Focus Evidence": evidence})
        out.append(resolve_challenger(
            fields, experiment_id=EXPERIMENT, registry=registry))
    return out


def test_every_campaign_asks_for_a_different_thing():
    """Nine campaigns, nine distinct CTAs. A shared CTA means a shared offer."""
    nouns = [r.offer_noun for r in _differentiation_resolutions()]
    assert len(set(nouns)) == len(nouns), sorted(nouns)


def test_the_friction_line_is_unique_to_each_campaign():
    """The friction states the reason THIS buyer cares. Two campaigns sharing it
    means one of them is borrowing the other's argument."""
    frictions = [r.e1_segments["friction"] for r in _differentiation_resolutions()]
    assert len(set(frictions)) == len(frictions), sorted(frictions)


def test_no_single_proof_sentence_dominates_the_nine_campaigns():
    """The shared testing claim is legitimate -- it is verified and several
    buckets genuinely have no better verified fact -- but it must not become the
    whole programme again. Cap it well below the 8-of-9 that triggered this gate.
    """
    proofs = [r.e1_segments["proof"] for r in _differentiation_resolutions()]
    most_common = max(collections.Counter(proofs).values())
    assert most_common <= 4, collections.Counter(proofs)
    assert len(set(proofs)) >= 6, sorted(set(proofs))


def test_the_campaigns_draw_on_more_than_one_verified_fact():
    """At least three distinct verified proof TYPES across the nine campaigns."""
    proof_types = {r.proof_type for r in _differentiation_resolutions()}
    assert len(proof_types) >= 3, proof_types


def test_every_campaign_still_passes_qa_after_differentiation():
    for resolution in _differentiation_resolutions():
        assert resolution.qa_pass, (resolution.campaign, resolution.qa_reasons)


def test_no_campaign_claims_a_capability_outside_the_verified_set():
    """Guards the four angles that were explicitly NOT approved.

    Coverage hours, a work sample sent before the first interview, a task run on
    the client's own system, and seasonal/flex capacity are all unverified. None
    may appear in any campaign's copy.
    """
    unverified = (
        "time zone", "timezone", "your hours", "business hours", "overlap",
        "work sample", "sample exercise", "take-home", "trial task",
        "your system", "your stack", "your instance", "your backlog",
        "seasonal", "peak season", "scale up", "scale down", "flex up",
    )
    for resolution in _differentiation_resolutions():
        thread = " ".join([
            resolution.rendered_email_1, resolution.rendered_email_2,
            resolution.rendered_email_3, resolution.rendered_email_4,
        ]).lower()
        for phrase in unverified:
            assert phrase not in thread, (resolution.campaign, phrase)


def test_a_headcount_proof_never_answers_an_evidence_friction():
    """"A CV cannot show you that" followed by "they are not on your headcount"
    answers a question the reader did not ask. The coherence gate must reject it."""
    resolution = resolve_challenger(
        _campaign_fields("product", "Product Manager"),
        experiment_id=EXPERIMENT, registry=empty_registry(),
    ).to_dict()
    resolution["friction_angle"] = "rare_intersection"
    passed, reasons = run_qa_gates(resolution, policy=campaign_for_bucket("product"))
    assert not passed
    assert "friction_incoherent_with_proof_and_offer" in reasons


def test_an_employment_admin_proof_never_answers_an_evidence_friction():
    resolution = resolve_challenger(
        _campaign_fields("finance", "Financial Analyst"),
        experiment_id=EXPERIMENT, registry=empty_registry(),
    ).to_dict()
    assert resolution["proof_type"] == PROOF_EMPLOYMENT_ADMIN
    resolution["friction_angle"] = "interview_weak_signal"
    passed, reasons = run_qa_gates(resolution, policy=campaign_for_bucket("finance"))
    assert not passed
    assert "friction_incoherent_with_proof_and_offer" in reasons


def test_a_signal_dependent_friction_is_not_rendered_behind_another_signal():
    """PRODUCT's approval line reads off a multi-opening count. With only one
    opening the signal degrades, so the line must not be asserted anyway."""
    fields = _campaign_fields("product", "Product Manager")
    fields.update({"Outbound Roles": "Product Manager", "Open Roles": "Product Manager"})
    resolution = resolve_challenger(
        fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.signal_type != "multi_opening"
    assert resolution.friction_angle != "approval_sequencing"
    assert "approved at the same time" not in resolution.rendered_email_1
    assert resolution.qa_pass, resolution.qa_reasons


#: Flat absolutes and asserted facts about the reader's own process. A friction
#: is a HYPOTHESIS the reader can accept or reject, never something the copy
#: claims to know about them, so these may not appear in one.
_UNSUPPORTED_ABSOLUTES = (
    r"\bnobody\b", r"\bno one\b", r"\balways\b", r"\bnever\b",
    r"\bcannot show\b", r"\bcan'?t show\b", r"\btells you (?:almost )?nothing\b",
    r"\bby definition\b", r"\bis not telling you\b", r"\bthe rare part\b",
    r"\bweakest\b", r"\bguarantee",
)

#: A friction that makes a claim about the reader's situation must hedge it.
_HEDGES = r"\b(?:can|could|may|might|often|usually|tends? to|if)\b"


def test_no_friction_states_an_unsupported_absolute():
    import re
    for resolution in _differentiation_resolutions():
        friction = resolution.e1_segments["friction"]
        for pattern in _UNSUPPORTED_ABSOLUTES:
            assert not re.search(pattern, friction, re.I), (
                resolution.campaign, pattern, friction)


def test_no_friction_asserts_the_readers_own_situation_as_fact():
    """Either the sentence is hedged, or it is true of any hire rather than of
    this reader. FINANCE's is the second kind: employing someone creates a
    payroll, tax and benefits obligation whoever administers it."""
    import re
    for resolution in _differentiation_resolutions():
        friction = resolution.e1_segments["friction"]
        hedged = bool(re.search(_HEDGES, friction, re.I))
        addresses_reader = " your " in f" {friction.lower()} "
        assert hedged or not addresses_reader, (resolution.campaign, friction)


def test_the_signal_line_stays_factual_and_unhedged():
    """Only the friction is a hypothesis. The signal is the real hiring event
    read off the record, and hedging it would throw away the one thing in the
    email that is verifiably true."""
    for resolution in _differentiation_resolutions():
        signal = resolution.e1_segments["signal"]
        assert signal
        for weasel in ("might be", "may be", "could be", "possibly", "perhaps"):
            assert weasel not in signal.lower(), (resolution.campaign, signal)


def test_no_campaign_uses_landing_page_vocabulary():
    """A short, hard list of words that are always wrong in this voice.

    This is NOT an attempt to encode "sounds human" as a phrase list -- that is
    an editorial judgement and stays one. It catches the handful of words that
    would make the copy read as marketing no matter how they were arranged.
    """
    from outbound_wave1.qa import _find_banned
    for resolution in _differentiation_resolutions():
        thread = " ".join([
            resolution.rendered_subject, resolution.rendered_email_1,
            resolution.rendered_email_2, resolution.rendered_email_3,
            resolution.rendered_email_4,
        ])
        assert _find_banned(thread) == [], (resolution.campaign, _find_banned(thread))


def test_email_1_stays_short_enough_to_read_on_a_phone():
    """Under ~100 words, which is where a cold first email stops being read."""
    import re
    for resolution in _differentiation_resolutions():
        count = len(re.findall(r"[A-Za-z0-9'/-]+", resolution.rendered_email_1))
        assert count <= 100, (resolution.campaign, count)


def test_every_email_asks_for_exactly_one_thing():
    """One CTA per email. A second question turns a low-friction offer into a
    decision the reader has to sort out."""
    for resolution in _differentiation_resolutions():
        for index in (1, 2, 3, 4):
            body = getattr(resolution, f"rendered_email_{index}")
            assert body.count("?") <= 1, (resolution.campaign, index, body)


def test_a_proof_never_points_back_at_a_signal_that_did_not_fire():
    """GTM's proof says "the combination" and AI's ends on a bare "instead".
    Behind a degraded signal there is nothing for those to refer to, so the
    shared wording is used instead."""
    for bucket, role, dangling in (
        ("gtm_revenue", "Revenue Operations Manager", "the combination, not just"),
        ("engineering", "AI Automation Engineer", "actual work instead."),
    ):
        fields = _campaign_fields(bucket, role)
        fields.update({"Role Focus": "", "Focus Evidence": "", "Outbound Roles": role})
        resolution = resolve_challenger(
            fields, experiment_id=EXPERIMENT, registry=empty_registry())
        assert resolution.signal_tier == "T3", (bucket, resolution.signal_tier)
        assert dangling not in resolution.rendered_email_1, (bucket, dangling)
        assert resolution.qa_pass, (bucket, resolution.qa_reasons)


def test_the_campaign_phrasing_is_still_used_when_its_signal_does_fire():
    fields = _campaign_fields("gtm_revenue", "Revenue Operations Manager")
    fields["Focus Evidence"] = "hubspot administration | lead routing | pipeline reporting"
    resolution = resolve_challenger(
        fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.signal_tier == "T1"
    assert "We test for the combination, not just the individual pieces." in (
        resolution.rendered_email_1)


def test_a_prospects_own_name_never_fails_our_copy_gate():
    """The banned-language gates judge what WE wrote.

    A real company called "CBX Solutions, LLC" tripped the buzzword list in a
    dry run, and a company called "Top Talent Inc" would trip the older one.
    Suppressing a good record over a name we did not choose is a defect, not a
    safeguard.
    """
    fields = _campaign_fields("people_hr", "Talent Acquisition Specialist")
    fields.update({
        "Outbound Company": "CBX Solutions, LLC",
        "Company": "CBX Solutions, LLC",
        "Outbound Roles": "Talent Acquisition Specialist",
    })
    resolution = resolve_challenger(
        fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert "buzzword_solution" not in resolution.qa_reasons, resolution.qa_reasons
    assert resolution.qa_pass, resolution.qa_reasons


def test_a_banned_word_we_actually_wrote_still_fails():
    """The redaction removes the record's values, not our sentences."""
    from outbound_wave1.qa import _find_banned, authored_text
    text = "Acme Solutions has two roles open. Our platform will transform hiring."
    assert _find_banned(authored_text(text, "Acme Solutions")) == [
        "buzzword_platform", "buzzword_transform"]
