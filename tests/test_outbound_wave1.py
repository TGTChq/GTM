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


def test_unassignable_company_stays_on_control():
    assignment = account_assignment({}, experiment_id=EXPERIMENT, b_split_pct=100)
    assert assignment.arm == ARM_A
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
    assert "3 product roles open at Acme at once." in resolution.rendered_email_1
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
    assert "2 functions" in hr.rendered_email_1


def test_reposted_and_first_hire_signals_are_not_implemented():
    signal_types = {policy.t1_signal for policy in CAMPAIGNS}
    assert "reposted" not in signal_types
    assert "first_hire" not in signal_types


# ---------------------------------------------------------------------------
# Proof / offer / claims
# ---------------------------------------------------------------------------

def test_economics_never_renders_without_a_role_page():
    fields = _fields()
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.role_page_match is False
    assert resolution.proof_type == PROOF_TESTING_MECHANICS
    assert resolution.outbound_offer_type == OFFER_TESTING_OVERVIEW
    assert resolution.offer_fallback_type == OFFER_ROLE_ECONOMICS
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
    assert resolution.proof_type == PROOF_TESTING_MECHANICS
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
    assert resolution.proof_type == PROOF_TESTING_MECHANICS


def test_economics_without_a_claim_source_degrades():
    registry = _registry_with_economics(claim_source="")
    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=registry)
    assert resolution.proof_type == PROOF_TESTING_MECHANICS
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
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=registry)
    assert (
        "We test candidates on role-specific work rather than relying on the title "
        "or tool list alone." in resolution.rendered_email_1
    )
    assert "directly test" not in resolution.rendered_email_1.lower()


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
    assert resolution.rendered_email_2.startswith("Bumping this in case it slipped past.")
    assert "Still happy to send how we test for this role if it's useful." in resolution.rendered_email_2
    assert resolution.rendered_email_3.startswith("One thing I left out.")
    assert (
        "You don't pay anything until you've picked someone. If nobody on the "
        "shortlist clears your bar, you owe nothing and we keep looking."
        in resolution.rendered_email_3
    )
    assert resolution.rendered_email_4.startswith("Closing this out.")
    assert "If the Financial Analyst search is handled, tell me and I'll stop." in resolution.rendered_email_4


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

def _approved_record():
    fields = _fields()
    fields.update({
        "Final Decision": "FINAL_PASS",
        "Validation Version": config.VALIDATION_VERSION,
        "Email": "dana@acme.com",
        "Role Focus": "financial reporting, budgeting, and variance analysis",
        "Campaign ID": "control-campaign-id",
        "Job URL Status": "verified",
    })
    return {"id": "recTest", "fields": fields}


def test_enrollment_payload_is_unchanged_while_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_ENABLED", False)
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]
    assert "rendered_email_1" not in lead["custom_variables"]


def test_challenger_overlay_switches_campaign_and_adds_variables(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_B_SPLIT_PCT", 100)
    monkeypatch.setattr(
        config, "OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS", {"finance": "challenger-campaign-id"}
    )
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "challenger-campaign-id"
    assert lead["custom_variables"]["experiment_arm"] == "B"
    assert lead["custom_variables"]["rendered_email_1"]
    # The control variables are still present: nothing about the existing payload
    # is removed by the overlay.
    assert lead["custom_variables"]["open_role"] == "Financial Analyst"


def test_control_arm_records_keep_the_control_campaign(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_B_SPLIT_PCT", 0)
    monkeypatch.setattr(
        config, "OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS", {"finance": "challenger-campaign-id"}
    )
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]


def test_no_challenger_campaign_configured_keeps_the_record_on_control(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_B_SPLIT_PCT", 100)
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS", {})
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]


def test_a_qa_failure_keeps_the_record_on_control(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_B_SPLIT_PCT", 100)
    monkeypatch.setattr(
        config, "OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS", {"finance": "challenger-campaign-id"}
    )
    record = _approved_record()
    record["fields"]["Outbound Role"] = "Financial Analyst | Remote | $90k"
    record["fields"]["Outbound Roles"] = "Financial Analyst | Remote | $90k"
    lead = instantly_client.airtable_record_to_lead(record, probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "rendered_email_1" not in lead["custom_variables"]


def test_an_overlay_failure_never_breaks_a_control_enrollment(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOUND_WAVE1_B_SPLIT_PCT", 100)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr("outbound_wave1.claims.load_claim_registry", _boom)
    lead = instantly_client.airtable_record_to_lead(_approved_record(), probe=False)
    assert lead["campaign"] == "control-campaign-id"
    assert "experiment_arm" not in lead["custom_variables"]


def test_email_1_offer_question_matches_the_offer_type():
    from outbound_wave1.render import offer_question

    resolution = resolve_challenger(_fields(), experiment_id=EXPERIMENT, registry=empty_registry())
    assert resolution.e1_segments["offer"] == offer_question(resolution.outbound_offer_type)
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
    assert "email_1_offer_is_not_the_registered_offer_for_its_type" in reasons


def test_an_offer_noun_swapped_mid_thread_fails_qa():
    from outbound_wave1.qa import run_qa_gates

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
