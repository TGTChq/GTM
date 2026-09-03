"""Outbound Wave 1: assignment, signal, proof, offer, render, QA and timing.

Every test is offline. Nothing here touches Airtable, Instantly or any provider.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

import config
import instantly_client
from outbound_wave1 import (
    ARM_A,
    ARM_B,
    ARM_NONE,
    CAMPAIGNS,
    WAVE1_CAMPAIGN_NAMES,
    account_assignment,
    campaign_for_bucket,
    company_assignment_key,
    resolve_batch,
    resolve_challenger,
    resolve_wave1,
    sequence_schedule,
)
from outbound_wave1.campaigns import (
    OFFER_ROLE_ECONOMICS,
    OFFER_TESTING_OVERVIEW,
    PROOF_ECONOMICS,
    PROOF_REMOTE_READINESS,
    PROOF_TESTING_MECHANICS,
    PROOF_EMPLOYMENT_ADMIN,
    OFFER_EMPLOYMENT_ADMIN_OVERVIEW,
    VALID_OFFER_NOUNS,
)
from outbound_wave1.claims import ClaimRegistry, load_claim_registry, empty_registry
from outbound_wave1.evidence import read_focus_evidence, render_evidence_list
from outbound_wave1.qa import audit_arm_consistency, role_display_send_safe
from outbound_wave1.signals import T2_MAX_AGE_DAYS, T2_MIN_AGE_DAYS
from outbound_wave1.timing import SEQUENCE_DAY_LABELS

EXPERIMENT = "test_wave1"


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
                "url": page.get("url", "https://example.invalid/roles/financial-analyst"),
                "economics_available": page.get("economics_available", True),
                "monthly_cost_usd": page.get("monthly_cost_usd", 2400),
                "local_comparison_published": page.get("local_comparison_published", True),
                "claim_source": page.get(
                    "claim_source", "https://example.invalid/roles/financial-analyst"
                ),
            }
        },
    }
    return _registry_from_payload(payload)


def _registry_from_payload(payload, tmp_path=None):
    import tempfile
    from pathlib import Path

    directory = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    target = directory / "claims.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return load_claim_registry(str(target))


# ---------------------------------------------------------------------------
# Campaign table
# ---------------------------------------------------------------------------

def test_wave1_covers_exactly_the_nine_live_campaigns():
    assert len(CAMPAIGNS) == 9
    assert set(WAVE1_CAMPAIGN_NAMES) == {
        "PRODUCT", "OPERATIONS", "FINANCE", "PEOPLE & HR", "ECOMMERCE",
        "CUSTOMER EXPERIENCE", "MARKETING & CREATIVE",
        "GTM SYSTEMS & REVENUE AUTOMATION", "AI & TECHNICAL AUTOMATION",
    }


def test_every_production_role_bucket_maps_to_a_campaign():
    for bucket in config.CAMPAIGN_ENV_BY_BUCKET:
        assert campaign_for_bucket(bucket) is not None, bucket


def test_customer_experience_carries_both_cx_buckets():
    policy = campaign_for_bucket("customer_success")
    assert policy is campaign_for_bucket("customer_support")
    assert policy.audience_by_bucket == {
        "customer_success": "customer success",
        "customer_support": "customer support",
    }


def test_operations_never_bifurcates_to_economics():
    policy = campaign_for_bucket("operations")
    assert policy.preferred_proof == policy.fallback_proof == PROOF_TESTING_MECHANICS
    assert policy.preferred_offer == policy.fallback_offer == OFFER_TESTING_OVERVIEW


# ---------------------------------------------------------------------------
# A/B assignment
# ---------------------------------------------------------------------------

def test_assignment_is_stable_across_reruns():
    fields = _fields()
    first = account_assignment(fields, experiment_id=EXPERIMENT, b_split_pct=50)
    for _ in range(20):
        again = account_assignment(fields, experiment_id=EXPERIMENT, b_split_pct=50)
        assert again.arm == first.arm and again.bucket == first.bucket


def test_assignment_ignores_campaign_so_one_company_is_never_split():
    arms = set()
    for bucket in ("finance", "marketing", "engineering", "people_hr", "product"):
        fields = _fields()
        fields["Role Bucket"] = bucket
        fields["Outbound Role"] = "Financial Analyst"
        arms.add(
            account_assignment(fields, experiment_id=EXPERIMENT, b_split_pct=50).arm
        )
    assert len(arms) == 1


def test_assignment_key_prefers_canonical_identity_then_domain_then_name():
    assert company_assignment_key({"Outbound Company Identity": "domain:acme.com"}) == "domain:acme.com"
    assert company_assignment_key({"Website": "https://www.acme.com/jobs"}) == "domain:acme.com"
    assert company_assignment_key({"Outbound Company": "Acme Corp."}) == "name:acmecorp"
    assert company_assignment_key({}) == ""


def test_unassignable_company_is_suppressed_not_put_in_control():
    assignment = account_assignment({}, experiment_id=EXPERIMENT, b_split_pct=100)
    assert assignment.arm == ARM_NONE
    assert not assignment.assignable


def test_zero_split_keeps_everyone_on_control():
    for index in range(50):
        fields = _fields(**{"Outbound Company Identity": f"domain:c{index}.com"})
        assert account_assignment(
            fields, experiment_id=EXPERIMENT, b_split_pct=0
        ).arm == ARM_A


def test_split_percentage_is_approximately_honoured():
    b_count = 0
    total = 2000
    for index in range(total):
        fields = {"Outbound Company Identity": f"domain:company{index}.com"}
        if account_assignment(fields, experiment_id=EXPERIMENT, b_split_pct=50).arm == ARM_B:
            b_count += 1
    assert 0.45 * total <= b_count <= 0.55 * total


def test_batch_never_splits_a_company_across_arms():
    records = []
    for bucket in ("finance", "marketing", "people_hr"):
        fields = _fields()
        fields["Role Bucket"] = bucket
        records.append({"id": f"rec{bucket}", "fields": fields})
    _resolutions, _previews, failures = resolve_batch(
        records, experiment_id=EXPERIMENT, b_split_pct=50, registry=empty_registry()
    )
    assert failures == []


def test_arm_consistency_audit_detects_a_split_company():
    assert audit_arm_consistency([
        {"company_assignment_key": "domain:acme.com", "experiment_arm": "A"},
        {"company_assignment_key": "domain:acme.com", "experiment_arm": "B"},
    ]) == ["company_received_both_arms:domain:acme.com"]


# ---------------------------------------------------------------------------
# Control A is untouched
# ---------------------------------------------------------------------------

def test_control_arm_renders_no_copy():
    fields = _fields()
    resolution = resolve_wave1(
        fields, experiment_id=EXPERIMENT, b_split_pct=0, registry=empty_registry()
    )
    assert resolution.experiment_arm == ARM_A
    assert resolution.rendered_email_1 == ""
    assert resolution.rendered_subject == ""
    assert resolution.copy_version == "control-a-live-instantly-copy"
    assert resolution.qa_pass is True
    assert resolution.to_custom_variables().get("rendered_email_1") is None


# ---------------------------------------------------------------------------
# Signal policy
# ---------------------------------------------------------------------------

def test_t1_multi_opening_uses_the_records_actual_count():
    fields = _fields()
    fields.update({
        "Role Bucket": "product",
        "Matched Role": "Product Manager",
        "Outbound Role": "Product Manager",
        "Outbound Roles": "Product Manager | Product Analyst | Product Designer",
    })
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.signal_tier == "T1"
    assert resolution.opening_count == 3
    # The count renders as a word: a sentence opening with a digit is a
    # mail-merge tell. It is still the record's own count, not a hard-coded one.
    assert "Acme has three product roles open right now." in resolution.rendered_email_1
    assert "two product roles" not in resolution.rendered_email_1.lower()


def test_product_single_opening_degrades_rather_than_claiming_multi():
    fields = _fields()
    fields.update({
        "Role Bucket": "product",
        "Matched Role": "Product Manager",
        "Outbound Role": "Product Manager",
        "Outbound Roles": "Product Manager",
    })
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.signal_tier != "T1"
    assert any("multi_opening_needs_2" in reason for reason in resolution.degrade_reasons)


def test_role_focus_match_requires_two_usable_evidence_items():
    fields = _fields()
    fields.update({
        "Role Bucket": "operations",
        "Matched Role": "Operations Analyst",
        "Outbound Role": "Operations Analyst",
        "Focus Evidence": "process documentation",
    })
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.usable_evidence_count == 1
    assert resolution.signal_tier != "T1"

    fields["Focus Evidence"] = "process documentation | vendor coordination"
    upgraded = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert upgraded.signal_tier == "T1"
    assert upgraded.signal_type == "role_focus_match"


def test_fallback_focus_never_counts_as_evidence():
    fields = _fields()
    fields.update({
        "Role Bucket": "operations",
        "Matched Role": "Operations Analyst",
        "Outbound Role": "Operations Analyst",
        "Focus Quality": "manual_required",
        "Focus Evidence": "fallback_from_role:Operations Analyst",
    })
    focus = read_focus_evidence(fields)
    assert focus.usable_count == 0
    assert focus.fallback_only
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.signal_tier != "T1"


def test_scope_combination_needs_two_distinct_facets():
    fields = _fields()
    fields.update({
        "Role Bucket": "marketing",
        "Matched Role": "Growth Marketing Manager",
        "Outbound Role": "Growth Marketing Manager",
        "Role Focus": "paid media and paid acquisition",
        "Focus Evidence": "paid media | paid acquisition",
    })
    single_facet = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert single_facet.signal_tier != "T1"

    fields["Role Focus"] = "paid media and lifecycle"
    fields["Focus Evidence"] = "paid media | lifecycle"
    combined = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert combined.signal_tier == "T1"
    assert combined.scope_combination == "paid media + lifecycle"
    assert "paid media and lifecycle" in combined.rendered_email_1


def test_marketing_evidence_list_grows_with_real_item_count():
    fields = _fields()
    fields.update({
        "Role Bucket": "marketing",
        "Matched Role": "Growth Marketing Manager",
        "Outbound Role": "Growth Marketing Manager",
        "Role Focus": "paid media, lifecycle, and content",
        "Focus Evidence": "paid media | lifecycle | content",
    })
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.scope_list == "paid media, lifecycle, and content"
    assert resolution.scope_item_count == 3


def test_evidence_list_never_exceeds_three_items():
    assert render_evidence_list(("a", "b")) == "a and b"
    assert render_evidence_list(("a", "b", "c")) == "a, b, and c"
    fields = _fields()
    fields.update({
        "Role Bucket": "marketing",
        "Matched Role": "Growth Marketing Manager",
        "Outbound Role": "Growth Marketing Manager",
        "Role Focus": "paid media, lifecycle, content, creative, and analytics",
        "Focus Evidence": "paid media | lifecycle | content | creative | analytics",
    })
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.scope_item_count <= 3
    assert resolution.evidence_item_count <= 3
    assert resolution.qa_pass


def test_t2_fires_only_inside_the_job_age_window():
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    for age, expected in (
        (T2_MIN_AGE_DAYS - 1, "T3"),
        (T2_MIN_AGE_DAYS, "T2"),
        (T2_MAX_AGE_DAYS, "T2"),
        (T2_MAX_AGE_DAYS + 1, "T3"),
    ):
        fields = _fields(**{
            "Focus Quality": "manual_required",
            "Focus Evidence": "fallback_from_role:Financial Analyst",
            "Posted At": (now - timedelta(days=age)).isoformat(),
        })
        resolution = resolve_challenger(
            fields, experiment_id=EXPERIMENT, registry=empty_registry(), as_of=now
        )
        assert resolution.signal_tier == expected, (age, resolution.signal_tier)


def test_t2_needs_a_parseable_posted_date():
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    fields = _fields(**{
        "Focus Quality": "manual_required",
        "Focus Evidence": "fallback_from_role:Financial Analyst",
        "Posted At": "not a date",
        "Job Freshness": "unknown_review",
    })
    resolution = resolve_challenger(
        fields, experiment_id=EXPERIMENT, registry=empty_registry(), as_of=now
    )
    assert resolution.signal_tier == "T3"
    assert any("posted_at" in reason for reason in resolution.degrade_reasons)


def test_held_or_dead_posting_is_not_wave1_eligible():
    for override in ({"Outbound Hold": True}, {"Job URL Status": "expired"}):
        fields = _fields(**override)
        resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
        assert not resolution.eligible
        assert not resolution.qa_pass
        assert resolution.rendered_email_1 == ""


def test_cross_bucket_signal_needs_a_company_index():
    fields = _fields(**{
        "Role Bucket": "people_hr",
        "Matched Role": "Recruiter",
        "Outbound Role": "Recruiter",
        "Outbound Roles": "Recruiter",
    })
    alone = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert alone.signal_tier != "T1"

    records = [
        {"id": "rec1", "fields": fields},
        {"id": "rec2", "fields": _fields(**{
            "Role Bucket": "finance",
            "Outbound Role": "Accountant",
            "Outbound Roles": "Accountant",
        })},
    ]
    resolutions, previews, _failures = resolve_batch(
        records, experiment_id=EXPERIMENT, b_split_pct=100,
        registry=empty_registry(), challenger_preview=True,
    )
    hr = next(r for r in resolutions + previews if r.role_bucket == "people_hr")
    assert hr.signal_tier == "T1"
    assert hr.signal_type == "multi_opening_cross_bucket"
    assert "across two teams" in hr.rendered_email_1


def test_reposted_and_first_hire_signals_are_not_implemented():
    signal_types = {policy.t1_signal for policy in CAMPAIGNS}
    assert "reposted" not in signal_types
    assert "first_hire" not in signal_types


# ---------------------------------------------------------------------------
# Proof / offer / claims
# ---------------------------------------------------------------------------

def test_economics_never_renders_without_a_role_page():
    """FINANCE degrades to the EMPLOYMENT ADMINISTRATION proof, not to testing.

    A controller's objection to a hire is who carries the payroll, tax, benefits
    and compliance load, so that is the verified fact the campaign falls back to.
    """
    fields = _fields()
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.role_page_match is False
    assert resolution.proof_type == PROOF_EMPLOYMENT_ADMIN
    assert resolution.outbound_offer_type == OFFER_EMPLOYMENT_ADMIN_OVERVIEW
    assert resolution.offer_fallback_type == OFFER_ROLE_ECONOMICS
    assert "payroll, taxes, benefits" in resolution.rendered_email_1
    assert resolution.qa_pass


def test_economics_renders_for_an_exact_role_page_match():
    registry = _registry_with_economics()
    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=registry)
    assert resolution.role_page_match is True
    assert resolution.proof_type == PROOF_ECONOMICS
    assert resolution.outbound_offer_type == OFFER_ROLE_ECONOMICS
    assert resolution.claim_source
    assert resolution.economics_role == "Financial Analyst"
    assert "monthly cost for Financial Analyst" in resolution.rendered_email_1
    assert resolution.offer_noun == "the numbers for this role"
    assert resolution.qa_pass


def test_a_modified_title_does_not_inherit_the_plain_roles_economics():
    registry = _registry_with_economics()
    fields = _fields(**{"Outbound Role": "Senior Financial Analyst"})
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=registry)
    assert resolution.role_page_match is False
    assert resolution.role_page_reason == "display_role_is_not_the_exact_mapped_role"
    assert resolution.proof_type == PROOF_EMPLOYMENT_ADMIN
    assert "monthly cost" not in resolution.rendered_email_1


def test_one_roles_economics_is_never_used_for_another_role():
    registry = _registry_with_economics(role="Financial Analyst")
    fields = _fields(**{
        "Matched Role": "Accountant",
        "Outbound Role": "Accountant",
    })
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=registry)
    assert resolution.role_page_match is False
    assert "Financial Analyst" not in resolution.rendered_email_1
    assert resolution.proof_type == PROOF_EMPLOYMENT_ADMIN


def test_economics_without_a_claim_source_degrades():
    registry = _registry_with_economics(claim_source="")
    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=registry)
    assert resolution.proof_type == PROOF_EMPLOYMENT_ADMIN
    assert resolution.qa_pass


def test_economics_without_a_local_comparison_drops_that_clause():
    registry = _registry_with_economics(local_comparison_published=False)
    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=registry)
    assert resolution.proof_type == PROOF_ECONOMICS
    assert "typical local cost" not in resolution.rendered_email_1
    assert "monthly cost for Financial Analyst" in resolution.rendered_email_1


def test_shipped_registry_licenses_no_economics_at_all():
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    assert registry.role_pages, "registry should enumerate the canonical roles"
    assert registry.economics_role_count == 0


def test_people_hr_uses_the_verified_remote_readiness_wording():
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    fields = _fields(**{
        "Role Bucket": "people_hr",
        "Matched Role": "Recruiter",
        "Outbound Role": "Recruiter",
    })
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=registry)
    assert resolution.proof_type == PROOF_REMOTE_READINESS
    assert "remote-readiness assessment built from 1,000+ hires" in resolution.rendered_email_1
    assert resolution.claim_source
    assert resolution.offer_noun == "how we assess remote readiness"
    assert resolution.qa_pass


def test_people_hr_degrades_when_the_claim_is_not_verified():
    fields = _fields(**{
        "Role Bucket": "people_hr",
        "Matched Role": "Recruiter",
        "Outbound Role": "Recruiter",
    })
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.proof_type == PROOF_TESTING_MECHANICS
    assert "remote-readiness" not in resolution.rendered_email_1
    assert resolution.qa_pass


def test_gtm_uses_safe_wording_without_a_verified_claim():
    fields = _fields(**{
        "Role Bucket": "gtm_revenue",
        "Matched Role": "Revenue Operations Manager",
        "Outbound Role": "Revenue Operations Manager",
    })
    # GTM's own phrasing points at "the combination", so it needs its T1 scope
    # signal to have fired.
    fields["Focus Evidence"] = "hubspot administration | lead routing | pipeline reporting"
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=registry)
    # The campaign phrases the SAME verified testing claim in its own words. It
    # must never reach for the unverified "we test that exact combination
    # directly" wording the registry deliberately leaves unusable.
    assert (
        "We test for the combination, not just the individual pieces."
        in resolution.rendered_email_1
    )
    assert resolution.proof_type == PROOF_TESTING_MECHANICS
    assert "directly" not in resolution.rendered_email_1.lower()


def test_ai_campaign_makes_no_live_llm_claim_without_a_source():
    fields = _fields(**{
        "Role Bucket": "engineering",
        "Matched Role": "AI Engineer",
        "Outbound Role": "AI Engineer",
    })
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=registry)
    assert "llm" not in resolution.rendered_email_1.lower()


# ---------------------------------------------------------------------------
# Customer Experience audience
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bucket,expected,forbidden",
    [
        ("customer_success", "customer success", "customer support"),
        ("customer_support", "customer support", "customer success"),
    ],
)
def test_customer_experience_never_conflates_support_and_success(bucket, expected, forbidden):
    fields = _fields(**{
        "Role Bucket": bucket,
        "Matched Role": "Customer Success Manager",
        "Outbound Role": "Customer Success Manager",
        "Outbound Roles": "Customer Success Manager | Customer Onboarding Manager",
    })
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    signal = resolution.e1_segments["signal"]
    assert expected in signal
    assert forbidden not in signal
    assert resolution.qa_pass


# ---------------------------------------------------------------------------
# Offer stability across the thread
# ---------------------------------------------------------------------------

def test_offer_noun_is_inherited_by_every_follow_up():
    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=empty_registry())
    noun = resolution.offer_noun
    assert noun in VALID_OFFER_NOUNS
    for body in (
        resolution.rendered_email_2,
        resolution.rendered_email_3,
        resolution.rendered_email_4,
    ):
        assert noun.lower() in body.lower()
    others = [n for n in VALID_OFFER_NOUNS if n != noun]
    thread = " ".join([
        resolution.rendered_email_1, resolution.rendered_email_2,
        resolution.rendered_email_3, resolution.rendered_email_4,
    ]).lower()
    assert not any(other.lower() in thread for other in others)


def test_frozen_follow_up_copy_is_rendered_verbatim():
    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.rendered_email_2.startswith("Just bumping this one.")
    assert (
        "Happy to send what we carry on the employment side if it's useful."
        in resolution.rendered_email_2
    )
    assert resolution.rendered_email_3.startswith("One thing I left out.")
    # E3 carries the two verified commercial facts held back from E1.
    assert (
        "You don't pay anything until you've picked someone. If nobody on the "
        "shortlist works, we keep looking."
        in resolution.rendered_email_3
    )
    assert resolution.rendered_email_4.startswith("Last one from me.")
    assert (
        "If the Financial Analyst search is already handled, tell me and I'll stop."
        in resolution.rendered_email_4
    )


# ---------------------------------------------------------------------------
# QA hard gates
# ---------------------------------------------------------------------------

def test_no_banned_language_in_any_rendered_challenger_email():
    banned = (
        "two-week", "14 day", "14-day", "30 day", "30-day", "ai-fluent",
        "top talent", "pre-vetted", "prevetted", "world-class", "curated",
        "$3.5", "$4k",
    )
    for bucket in config.CAMPAIGN_ENV_BY_BUCKET:
        fields = _fields()
        fields["Role Bucket"] = bucket
        resolution = resolve_challenger(
            fields, experiment_id=EXPERIMENT, registry=empty_registry()
        )
        text = " ".join([
            resolution.rendered_subject, resolution.rendered_email_1,
            resolution.rendered_email_2, resolution.rendered_email_3,
            resolution.rendered_email_4,
        ]).lower()
        for phrase in banned:
            assert phrase not in text, (bucket, phrase)


def test_no_links_images_or_attachments_in_the_challenger():
    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=empty_registry())
    for body in (
        resolution.rendered_email_1, resolution.rendered_email_2,
        resolution.rendered_email_3, resolution.rendered_email_4,
    ):
        assert "http://" not in body and "https://" not in body
        assert "<img" not in body
        assert "attach" not in body.lower()


def test_no_unresolved_merge_variable_survives_rendering():
    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=empty_registry())
    for body in (
        resolution.rendered_subject, resolution.rendered_email_1,
        resolution.rendered_email_2, resolution.rendered_email_3,
        resolution.rendered_email_4,
    ):
        assert "{{" not in body and "}}" not in body


def test_a_claim_source_url_is_metadata_and_never_rendered():
    registry = _registry_with_economics()
    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=registry)
    assert resolution.claim_source
    assert resolution.claim_source not in resolution.rendered_email_1


def test_missing_contact_or_company_is_not_enrollable():
    for override in ({"Hiring Manager": ""}, {"Outbound Company": "", "Company": ""}):
        fields = _fields(**override)
        resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
        assert not resolution.qa_pass
        assert "missing_required_render_field" in resolution.ineligible_reason


def test_unknown_role_bucket_is_out_of_wave1():
    fields = _fields(**{"Role Bucket": "logistics"})
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert not resolution.eligible
    assert resolution.ineligible_reason.startswith("role_bucket_not_in_wave1")


@pytest.mark.parametrize(
    "role,safe",
    [
        ("Financial Analyst", True),
        ("UX/UI Designer", True),
        ("GTM Engineer | B2B SaaS | p/y | Hybrid/Remote", False),
        ("AI Engineer - W2 ONLY", False),
        ("Tuckernuck is hiring: UX/UI Designer in Washington", False),
        ("Lead, Global Paid Media", False),
        ("", False),
    ],
)
def test_role_display_send_safety(role, safe):
    assert role_display_send_safe(role)[0] is safe


def test_unsafe_role_display_fails_qa_rather_than_being_rewritten():
    fields = _fields(**{"Outbound Role": "Financial Analyst | Remote | $90k"})
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert not resolution.qa_pass
    assert "role_display_contains_unsafe_characters" in resolution.qa_reasons
    # The stored display is signed evidence: the gate reads it, it never edits it.
    assert fields["Outbound Role"] == "Financial Analyst | Remote | $90k"


def test_low_confidence_displays_fail_qa():
    fields = _fields(**{"Outbound Role Confidence": "low"})
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert not resolution.qa_pass
    assert "role_display_confidence_low" in resolution.qa_reasons


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

def test_sequence_uses_the_frozen_day_labels():
    steps = sequence_schedule(date(2026, 8, 31))  # Monday
    assert [step.day_label for step in steps] == list(SEQUENCE_DAY_LABELS)
    assert [step.send_date for step in steps] == [
        "2026-08-31", "2026-09-03", "2026-09-07", "2026-09-14"
    ]


def test_weekend_sends_shift_to_monday_without_compressing():
    # Wednesday start: Day 4 lands Saturday and must move to Monday, and the next
    # gap is measured from the Monday, not from the nominal Saturday.
    steps = sequence_schedule(date(2026, 9, 2))  # Wednesday
    dates = [date.fromisoformat(step.send_date) for step in steps]
    assert dates[0] == date(2026, 9, 2)
    assert dates[1] == date(2026, 9, 7)   # 09-05 is Saturday -> Monday
    assert steps[1].weekend_shifted
    assert (dates[2] - dates[1]).days >= 4
    assert (dates[3] - dates[2]).days >= 5
    for value in dates:
        assert value.weekday() < 5


def test_a_weekend_start_opens_on_monday():
    steps = sequence_schedule(date(2026, 9, 5))  # Saturday
    assert steps[0].send_date == "2026-09-07"
    assert steps[0].weekend_shifted


def test_first_email_opens_the_thread_and_the_rest_continue_it():
    steps = sequence_schedule(date(2026, 8, 31))
    assert steps[0].same_thread is False
    assert all(step.same_thread for step in steps[1:])


# ---------------------------------------------------------------------------
# Experiment metadata
# ---------------------------------------------------------------------------

def test_resolution_carries_every_required_output_field():
    required = {
        "experiment_id", "experiment_arm", "company_assignment_key", "campaign",
        "signal_tier", "signal_type", "signal_evidence", "friction_angle",
        "proof_type", "claim_source", "outbound_offer_type", "offer_noun",
        "offer_class", "offer_fallback_type", "copy_version", "role_page_match",
        "rendered_subject", "rendered_email_1", "rendered_email_2",
        "rendered_email_3", "rendered_email_4", "qa_pass", "qa_reasons",
    }
    payload = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=empty_registry()
    ).to_dict()
    assert required <= set(payload)


def test_custom_variables_carry_the_analysis_metadata():
    variables = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=empty_registry()
    ).to_custom_variables()
    for key in (
        "experiment_id", "experiment_arm", "company_assignment_key", "wave1_campaign",
        "signal_tier", "signal_type", "proof_type", "outbound_offer_type",
        "offer_noun", "offer_class", "copy_version", "rendered_email_1",
        "rendered_email_4",
    ):
        assert variables.get(key), key


# ---------------------------------------------------------------------------
# Enrollment wiring
# ---------------------------------------------------------------------------

#: Experiment start watermark used by the overlay tests, and a record created
#: after it. Wave 1 refuses to run without a watermark, so every enabled-path
#: test states one explicitly rather than inheriting a default.
WAVE1_START = "2026-09-01T00:00:00Z"
RECORD_CREATED_AFTER_START = "2026-09-05T12:00:00.000Z"
RECORD_CREATED_BEFORE_START = "2026-06-01T12:00:00.000Z"


def _approved_record(created_time=RECORD_CREATED_AFTER_START):
    fields = _fields()
    fields.update({
        "Final Decision": "FINAL_PASS",
        "Validation Version": config.VALIDATION_VERSION,
        "Email": "dana@acme.com",
        "Role Focus": "financial reporting, budgeting, and variance analysis",
        "Campaign ID": "control-campaign-id",
        "Job URL Status": "verified",
    })
    return {"id": "recTest", "createdTime": created_time, "fields": fields}


def _enable_wave1(monkeypatch, *, b_split_pct=100, campaigns=None, start=WAVE1_START):
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_B_SPLIT_PCT", b_split_pct)
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT", start)
    monkeypatch.setattr(
        config,
        "OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS",
        {"finance": "challenger-campaign-id"} if campaigns is None else campaigns,
    )


def test_enrollment_payload_is_unchanged_while_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_ENABLED", False)
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]
    assert "rendered_email_1" not in lead["custom_variables"]


def test_challenger_overlay_switches_campaign_and_adds_variables(monkeypatch):
    _enable_wave1(monkeypatch)
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "challenger-campaign-id"
    assert lead["custom_variables"]["experiment_arm"] == "B"
    assert lead["custom_variables"]["rendered_email_1"]
    # The control variables are still present: nothing about the existing payload
    # is removed by the overlay.
    assert lead["custom_variables"]["open_role"] == "Financial Analyst"


def test_control_arm_records_keep_the_control_campaign(monkeypatch):
    _enable_wave1(monkeypatch, b_split_pct=0)
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]


def test_no_challenger_campaign_configured_keeps_the_record_on_control(monkeypatch):
    _enable_wave1(monkeypatch, campaigns={})
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]


def test_a_suppressed_record_gets_the_unchanged_production_payload(monkeypatch):
    _enable_wave1(monkeypatch)
    record = _approved_record()
    unsafe = "Financial Analyst | Remote | 90k"
    record["fields"]["Outbound Role"] = unsafe
    record["fields"]["Outbound Roles"] = unsafe

    # The record never entered the experiment, so it is not arm B -- and it is not
    # arm A either. It simply keeps the payload the pipeline built before Wave 1
    # existed, and the measurement frame excludes it from BOTH denominators.
    from outbound_wave1 import ARM_NONE, resolve_wave1

    resolution = resolve_wave1(
        record["fields"], experiment_id=config.OUTBOUND_WAVE1_EXPERIMENT_ID,
        b_split_pct=100,
    )
    assert resolution.experiment_arm == ARM_NONE
    assert not resolution.wave1_eligible

    lead = instantly_client.airtable_record_to_lead(record, probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "rendered_email_1" not in lead["custom_variables"]
    assert "experiment_arm" not in lead["custom_variables"]


def test_an_overlay_failure_never_breaks_a_control_enrollment(monkeypatch):
    _enable_wave1(monkeypatch)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr("outbound_wave1.claims.load_claim_registry", _boom)
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]


def test_email_1_asks_for_the_resolved_offer_noun_verbatim():
    from outbound_wave1.render import offer_question

    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.e1_segments["offer"] == offer_question(resolution.offer_noun)
    assert resolution.offer_noun in resolution.rendered_email_1
    assert resolution.qa_pass


def test_a_mismatched_offer_question_fails_qa():
    from outbound_wave1.qa import run_qa_gates

    resolution = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=empty_registry()
    ).to_dict()
    resolution["e1_segments"] = dict(resolution["e1_segments"])
    resolution["e1_segments"]["offer"] = "Want me to send the numbers for this role?"
    passed, reasons = run_qa_gates(resolution, policy=campaign_for_bucket("finance"))
    assert not passed
    assert "email_1_does_not_ask_for_the_resolved_offer_noun" in reasons


def test_an_offer_noun_swapped_mid_thread_fails_qa():
    from outbound_wave1.qa import run_qa_gates

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
# Segmentation: only NEW leads, and only buckets with a Challenger campaign
# ---------------------------------------------------------------------------

def test_wave1_refuses_to_run_without_an_experiment_start_watermark(monkeypatch):
    """No watermark is a misconfiguration, not "no restriction".

    Without one there is nothing separating a genuinely new lead from an Approved
    row created months ago for a person the live Control campaigns may already
    have emailed, so the overlay fails closed.
    """
    _enable_wave1(monkeypatch, start="")
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]

    # An unparseable watermark is treated exactly like a missing one.
    _enable_wave1(monkeypatch, start="not-a-date")
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]


def test_a_record_created_before_the_watermark_is_suppressed(monkeypatch):
    _enable_wave1(monkeypatch)
    record = _approved_record(created_time=RECORD_CREATED_BEFORE_START)
    lead = instantly_client.airtable_record_to_lead(record, probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]
    assert "rendered_email_1" not in lead["custom_variables"]


def test_a_record_with_no_created_time_is_suppressed(monkeypatch):
    """An unknown creation instant cannot be proven to be new, so it fails closed."""
    _enable_wave1(monkeypatch)
    record = _approved_record()
    record.pop("createdTime")
    lead = instantly_client.airtable_record_to_lead(record, probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]


def test_the_watermark_is_inclusive_of_its_own_instant():
    from outbound_wave1 import ARM_B, resolve_wave1
    from outbound_wave1.resolver import parse_instant

    start = parse_instant(WAVE1_START)
    resolution = resolve_wave1(
        _fields(), experiment_id=EXPERIMENT, b_split_pct=100,
        registry=empty_registry(),
        record_created_at=WAVE1_START, min_created_at=start,
        configured_buckets={"finance"},
    )
    assert resolution.experiment_arm == ARM_B
    assert resolution.wave1_eligible


def test_a_predating_record_is_suppressed_not_relabelled_control():
    from outbound_wave1 import ARM_NONE, resolve_wave1
    from outbound_wave1.resolver import SUPPRESS_PREDATES_START, parse_instant

    resolution = resolve_wave1(
        _fields(), experiment_id=EXPERIMENT, b_split_pct=100,
        registry=empty_registry(),
        record_created_at=RECORD_CREATED_BEFORE_START,
        min_created_at=parse_instant(WAVE1_START),
        configured_buckets={"finance"},
    )
    assert resolution.experiment_arm == ARM_NONE
    assert not resolution.wave1_eligible
    assert SUPPRESS_PREDATES_START in resolution.suppression_reason


def test_an_unconfigured_bucket_is_suppressed_not_counted_as_control():
    """A bucket with no Challenger campaign is delivered on Control A regardless.

    Labelling such a record ``B`` would put a control-delivered row in the
    treatment arm, so it is suppressed and excluded from both denominators.
    """
    from outbound_wave1 import ARM_NONE, resolve_wave1
    from outbound_wave1.resolver import SUPPRESS_CAMPAIGN_NOT_CONFIGURED, parse_instant

    resolution = resolve_wave1(
        _fields(), experiment_id=EXPERIMENT, b_split_pct=100,
        registry=empty_registry(),
        record_created_at=RECORD_CREATED_AFTER_START,
        min_created_at=parse_instant(WAVE1_START),
        configured_buckets={"marketing"},   # finance is not in the rollout
    )
    assert resolution.experiment_arm == ARM_NONE
    assert not resolution.wave1_eligible
    assert SUPPRESS_CAMPAIGN_NOT_CONFIGURED in resolution.suppression_reason


def test_the_segmentation_gates_are_off_when_not_configured():
    """Both gates are opt-in at the resolver level, so the dry run is unchanged."""
    from outbound_wave1 import ARM_B, resolve_wave1

    resolution = resolve_wave1(
        _fields(), experiment_id=EXPERIMENT, b_split_pct=100, registry=empty_registry()
    )
    assert resolution.experiment_arm == ARM_B
    assert resolution.wave1_eligible


def test_resolve_batch_reads_created_time_off_the_record():
    from outbound_wave1 import ARM_NONE, resolve_batch
    from outbound_wave1.resolver import SUPPRESS_PREDATES_START, parse_instant

    records = [
        {"id": "recNew", "createdTime": RECORD_CREATED_AFTER_START, "fields": _fields()},
        {"id": "recOld", "createdTime": RECORD_CREATED_BEFORE_START,
         "fields": _fields(**{
             "Lead Key": "oldco.com|sam@oldco.com|finance",
             "Outbound Company Identity": "domain:oldco.com",
             "Website": "https://oldco.com",
         })},
    ]
    resolutions, _previews, failures = resolve_batch(
        records, experiment_id=EXPERIMENT, b_split_pct=100,
        registry=empty_registry(),
        min_created_at=parse_instant(WAVE1_START),
        configured_buckets={"finance"},
    )
    by_id = {item.record_id: item for item in resolutions}
    assert by_id["recNew"].record_created_at == RECORD_CREATED_AFTER_START
    assert by_id["recNew"].wave1_eligible
    assert by_id["recOld"].experiment_arm == ARM_NONE
    assert SUPPRESS_PREDATES_START in by_id["recOld"].suppression_reason
    assert failures == []


def test_configured_buckets_helper_ignores_blank_campaign_ids(monkeypatch):
    monkeypatch.setattr(
        config,
        "OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS",
        {"finance": "id-1", "Marketing": " ", "product": "id-2", "": "id-3"},
    )
    assert config.wave1_configured_challenger_buckets() == frozenset({"finance", "product"})
