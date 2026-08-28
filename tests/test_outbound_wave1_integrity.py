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


@pytest.mark.parametrize(
    "bucket,role",
    [
        ("finance", "Financial Analyst"),
        ("customer_success", "Customer Success Manager"),
        ("customer_support", "Customer Support Specialist"),
        ("marketing", "Growth Marketing Manager"),
    ],
)
def test_economics_fallback_changes_friction_proof_and_offer_together(bucket, role):
    resolution = resolve_challenger(
        _campaign_fields(bucket, role), experiment_id=EXPERIMENT, registry=empty_registry()
    )
    assert resolution.role_page_match is False
    assert resolution.proof_type == PROOF_TESTING_MECHANICS
    assert resolution.outbound_offer_type == OFFER_TESTING_OVERVIEW
    assert resolution.offer_fallback_type == OFFER_ROLE_ECONOMICS
    assert resolution.friction_angle != FRICTION_COST_COMPARISON
    assert resolution.offer_noun == campaign_for_bucket(bucket).offer_noun(
        OFFER_TESTING_OVERVIEW
    )

    body = " ".join([
        resolution.rendered_email_1, resolution.rendered_email_2,
        resolution.rendered_email_3, resolution.rendered_email_4,
    ]).lower()
    for phrase in _ECONOMICS_LANGUAGE:
        assert phrase not in body, (bucket, phrase)
    assert resolution.qa_pass, resolution.qa_reasons


@pytest.mark.parametrize(
    "bucket,role",
    [
        ("finance", "Financial Analyst"),
        ("customer_success", "Customer Success Manager"),
        ("marketing", "Growth Marketing Manager"),
    ],
)
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


def test_cost_friction_paired_with_a_testing_offer_fails_qa():
    resolution = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=empty_registry()
    ).to_dict()
    assert resolution["proof_type"] == PROOF_TESTING_MECHANICS
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
        ("product", "Product Manager", "how our testing works"),
        ("ecommerce", "Ecommerce Manager", "how our testing works"),
        ("operations", "Operations Analyst", "how we test for this role"),
        ("gtm_revenue", "Revenue Operations Manager", "how we test for this role"),
        ("engineering", "AI Engineer", "how the assessment works"),
        ("people_hr", "Recruiter", "how we assess remote readiness"),
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
    assert f"Still happy to send {noun} if it" in resolution.rendered_email_2
    # Plural agreement is selected mechanically; no word choice changes.
    assert (
        "The numbers for this role are still there if you want them first."
        in resolution.rendered_email_3
    )
    assert f"send {noun} today." in resolution.rendered_email_4


def test_singular_offer_noun_keeps_the_frozen_singular_sentence():
    resolution = resolve_challenger(
        _campaign_fields("operations", "Operations Analyst"),
        experiment_id=EXPERIMENT, registry=empty_registry(),
    )
    assert (
        "How we test for this role is still there if you want it first."
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
        "How we test for this role", "The numbers for this role"
    )
    passed, reasons = run_qa_gates(resolution, policy=campaign_for_bucket("finance"))
    assert not passed
    assert "offer_changed_within_thread" in reasons
    assert "offer_noun_missing_from_email_3" in reasons
