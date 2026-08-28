"""Outbound Wave 1 experiment measurement.

The point of these tests is the denominator: the comparable population must be
decided the same way for both arms, or the primary metric is not a treatment
effect at all.
"""

from __future__ import annotations

import pytest

from outbound_wave1 import ARM_A, ARM_B, resolve_batch
from outbound_wave1.claims import empty_registry
from outbound_wave1.measurement import (
    STRATA,
    analyze,
    build_frame,
    randomization_row,
)

EXPERIMENT = "test_wave1_measurement"


def _fields(company_index: int, bucket: str = "finance", **overrides):
    base = {
        "Lead Key": f"c{company_index}.com|hm@c{company_index}.com|{bucket}",
        "Outbound Company": f"Company {company_index}",
        "Outbound Company Identity": f"domain:c{company_index}.com",
        "Outbound Company Confidence": "high",
        "Outbound Role": "Financial Analyst",
        "Outbound Roles": "Financial Analyst",
        "Outbound Role Confidence": "high",
        "Matched Role": "Financial Analyst",
        "Role Bucket": bucket,
        "Role Focus": "financial reporting, budgeting, and variance analysis",
        "Focus Quality": "specific",
        "Focus Evidence": "financial reporting | budget cycle",
        "Hiring Manager": "Dana Reeves",
        "Website": f"https://c{company_index}.com",
        "Posted At": "2026-08-01T00:00:00+00:00",
        "Job Freshness": "aging",
        "Job URL Status": "unverified_review",
        "Outbound Hold": False,
    }
    base.update(overrides)
    return base


def _batch(count=40, bucket="finance", **overrides):
    return [
        {"id": f"rec{i}", "fields": _fields(i, bucket, **overrides)}
        for i in range(count)
    ]


def _frame(records, b_split_pct=50):
    resolutions, previews, _failures = resolve_batch(
        records,
        experiment_id=EXPERIMENT,
        b_split_pct=b_split_pct,
        registry=empty_registry(),
        challenger_preview=True,
    )
    return build_frame(resolutions, previews)


def test_frame_covers_both_arms_and_every_record():
    records = _batch(60)
    frame = _frame(records)
    assert len(frame) == 60
    assert {row.experiment_arm for row in frame} == {ARM_A, ARM_B}


def test_control_rows_are_classified_from_the_challenger_render():
    """A control row must land in the same strata a treatment row would."""
    frame = _frame(_batch(60))
    control = [row for row in frame if row.experiment_arm == ARM_A]
    assert control, "expected control rows"
    for row in control:
        assert row.campaign == "FINANCE"
        assert row.signal_tier
        assert row.proof_type
        assert row.randomized_eligible


def test_eligibility_is_arm_independent():
    frame = _frame(_batch(60))
    eligible_by_arm = {
        arm: {row.randomized_eligible for row in frame if row.experiment_arm == arm}
        for arm in (ARM_A, ARM_B)
    }
    assert eligible_by_arm[ARM_A] == eligible_by_arm[ARM_B] == {True}


def test_a_qa_failure_removes_a_contact_from_both_arms_denominator():
    records = _batch(40, **{
        "Outbound Role": "Financial Analyst | Remote | $90k",
        "Outbound Roles": "Financial Analyst | Remote | $90k",
    })
    frame = _frame(records)
    assert all(not row.randomized_eligible for row in frame)
    report = analyze(frame)
    assert report["randomized_eligible"] == 0
    assert any(
        "role_display" in reason for reason in report["withheld_from_frame"]
    )


def test_primary_metric_is_positive_replies_over_randomized_eligible():
    frame = _frame(_batch(40))
    outcomes = {}
    for index, row in enumerate(frame):
        outcomes[row.contact_key] = {
            "delivered": True,
            "replied": index % 4 == 0,
            "positive_reply": index % 4 == 0 and row.experiment_arm == ARM_B,
            "reply_step": 1 if index % 4 == 0 else None,
        }
    report = analyze(frame, outcomes)
    b = report["overall"][ARM_B]
    a = report["overall"][ARM_A]
    assert b["positive_replies"] > 0
    assert a["positive_replies"] == 0
    expected = b["positive_replies"] / b["randomized_eligible"]
    assert b["primary_metric_positive_replies_per_randomized_eligible"] == pytest.approx(expected)
    assert report["lift"]["absolute"] == pytest.approx(expected)


def test_positive_replies_per_1000_enrolled_is_reported():
    frame = _frame(_batch(40))
    outcomes = {
        row.contact_key: {"delivered": True, "positive_reply": True}
        for row in frame
    }
    report = analyze(frame, outcomes)
    for arm in (ARM_A, ARM_B):
        assert report["overall"][arm]["positive_replies_per_1000_enrolled"] == pytest.approx(1000.0)


def test_a_contact_with_no_outcome_still_counts_in_the_denominator():
    frame = _frame(_batch(40))
    report = analyze(frame, {})
    total = sum(report["overall"][arm]["randomized_eligible"] for arm in (ARM_A, ARM_B))
    assert total == len(frame)
    for arm in (ARM_A, ARM_B):
        assert report["overall"][arm]["positive_replies"] == 0
        assert report["overall"][arm]["primary_metric_positive_replies_per_randomized_eligible"] == 0.0


def test_a_rate_with_no_denominator_is_none_not_zero():
    report = analyze([], {})
    for arm in (ARM_A, ARM_B):
        assert report["overall"][arm]["primary_metric_positive_replies_per_randomized_eligible"] is None
        assert report["overall"][arm]["guardrails"]["delivered_rate"] is None


def test_every_required_stratum_is_reported():
    frame = _frame(_batch(40))
    report = analyze(frame, {})
    assert set(report["by_stratum"]) == set(STRATA)
    for name in ("campaign", "signal_tier", "proof_type", "outbound_offer_type"):
        assert report["by_stratum"][name]


def test_treatment_effect_can_be_read_per_campaign():
    records = _batch(30, "finance") + _batch(30, "marketing")
    for index, record in enumerate(records[30:], start=100):
        record["id"] = f"recm{index}"
        record["fields"]["Outbound Company Identity"] = f"domain:m{index}.com"
        record["fields"]["Outbound Role"] = "Growth Marketing Manager"
        record["fields"]["Outbound Roles"] = "Growth Marketing Manager"
        record["fields"]["Matched Role"] = "Growth Marketing Manager"
        record["fields"]["Role Focus"] = "paid media and lifecycle"
        record["fields"]["Focus Evidence"] = "paid media | lifecycle"
    frame = _frame(records)
    report = analyze(frame, {})
    campaigns = report["by_stratum"]["campaign"]
    assert "FINANCE" in campaigns
    assert "MARKETING & CREATIVE" in campaigns
    for cell in campaigns.values():
        assert cell["A"]["randomized_eligible"] + cell["B"]["randomized_eligible"] > 0


def test_reply_step_is_preserved_for_positive_replies():
    frame = _frame(_batch(40))
    outcomes = {
        row.contact_key: {"delivered": True, "positive_reply": True, "reply_step": 3}
        for row in frame
    }
    report = analyze(frame, outcomes)
    for arm in (ARM_A, ARM_B):
        assert report["overall"][arm]["reply_steps"] == {
            "step_3": report["overall"][arm]["positive_replies"]
        }


def test_downstream_outcomes_are_carried_through():
    frame = _frame(_batch(20))
    outcomes = {
        row.contact_key: {
            "delivered": True, "positive_reply": True,
            "meeting_booked": True, "opportunity_created": True, "fulfilled": True,
        }
        for row in frame
    }
    report = analyze(frame, outcomes)
    totals = sum(
        report["overall"][arm][key]
        for arm in (ARM_A, ARM_B)
        for key in ("meetings", "opportunities", "fulfilled")
    )
    assert totals == len(frame) * 3


def test_deliverability_is_a_guardrail_not_the_metric():
    frame = _frame(_batch(40))
    outcomes = {
        row.contact_key: {"delivered": row.experiment_arm == ARM_A, "bounced": row.experiment_arm == ARM_B}
        for row in frame
    }
    report = analyze(frame, outcomes)
    guardrails = report["overall"][ARM_B]["guardrails"]
    assert guardrails["bounce_rate"] == pytest.approx(1.0)
    assert guardrails["delivered_rate"] == pytest.approx(0.0)
    # Guardrails moved; the primary metric is untouched by them.
    assert report["overall"][ARM_B]["primary_metric_positive_replies_per_randomized_eligible"] == 0.0


def test_outcomes_may_be_supplied_as_an_iterable():
    frame = _frame(_batch(20))
    rows = [{"contact_key": row.contact_key, "positive_reply": True} for row in frame]
    report = analyze(frame, rows)
    total = sum(report["overall"][arm]["positive_replies"] for arm in (ARM_A, ARM_B))
    assert total == len(frame)


def test_a_row_without_an_account_key_is_never_in_the_frame_denominator():
    class _Stub:
        def to_dict(self):
            return {
                "record_id": "rec1", "company_assignment_key": "",
                "eligible": True, "qa_pass": True, "qa_reasons": [],
                "experiment_arm": ARM_B, "campaign": "FINANCE",
            }

    row = randomization_row(_Stub())
    assert not row.randomized_eligible
    assert "no_resolvable_company_assignment_key" in row.eligibility_reasons
