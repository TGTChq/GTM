"""The HTML transport of the rendered Challenger emails.

Instantly campaign bodies are HTML. Interpolating a plain-text value with blank
lines into one collapses every paragraph break, so each rendered email is also
published in the `<div>` shape the live campaigns already use. The conversion is
transport only: it must round-trip back to the exact plain text, which is what
makes it impossible for anything to enter the email through the HTML.
"""

from __future__ import annotations

import pytest

import config
from outbound_wave1 import resolve_challenger, resolve_wave1, campaign_for_bucket
from outbound_wave1.claims import empty_registry, load_claim_registry
from outbound_wave1.qa import run_qa_gates
from outbound_wave1.render import html_to_text, to_html

EXPERIMENT = "test_wave1_html"


def _fields(**overrides):
    base = {
        "Outbound Company": "Acme",
        "Outbound Company Identity": "domain:acme.com",
        "Outbound Company Confidence": "high",
        "Outbound Role": "Financial Analyst",
        "Outbound Roles": "Financial Analyst",
        "Outbound Role Confidence": "high",
        "Matched Role": "Financial Analyst",
        "Role Bucket": "finance",
        "Role Focus": "financial reporting, budgeting, and variance analysis",
        "Focus Quality": "specific",
        "Focus Evidence": "financial reporting | budget cycle",
        "Hiring Manager": "Dana Reeves",
        "Website": "https://acme.com",
        "Posted At": "2026-08-01T00:00:00+00:00",
        "Job Freshness": "aging",
        "Job URL Status": "unverified_review",
        "Outbound Hold": False,
    }
    base.update(overrides)
    return base


def test_paragraphs_survive_the_conversion():
    text = "Hi Dana,\n\nFirst paragraph.\n\nSecond paragraph."
    markup = to_html(text)
    assert markup == (
        "<div>Hi Dana,</div><div><br /></div>"
        "<div>First paragraph.</div><div><br /></div>"
        "<div>Second paragraph.</div>"
    )
    assert markup.count("<div><br /></div>") == 2


def test_html_special_characters_are_escaped():
    markup = to_html("Saw Ben & Jerry's is hiring for <Analyst>.")
    assert "&amp;" in markup
    assert "&lt;Analyst&gt;" in markup
    assert "<Analyst>" not in markup


def test_conversion_round_trips_exactly():
    text = "Hi Dana,\n\nSaw Ben & Jerry's is hiring for <Analyst>.\n\nWant me to send?"
    assert html_to_text(to_html(text)) == text


def test_empty_text_yields_empty_html():
    assert to_html("") == ""
    assert to_html("   \n\n  ") == ""


def test_conversion_introduces_no_links_or_images():
    markup = to_html("Hi Dana,\n\nNo links here.\n\nNone at all.")
    assert "http" not in markup
    assert "<img" not in markup
    assert "<a " not in markup


@pytest.mark.parametrize("bucket", sorted(config.CAMPAIGN_ENV_BY_BUCKET))
def test_every_campaign_publishes_four_faithful_html_bodies(bucket):
    registry = load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH)
    fields = _fields()
    fields["Role Bucket"] = bucket
    resolution = resolve_challenger(fields, experiment_id=EXPERIMENT, registry=registry)
    assert resolution.qa_pass, resolution.qa_reasons
    for index in (1, 2, 3, 4):
        text = getattr(resolution, f"rendered_email_{index}")
        markup = getattr(resolution, f"rendered_email_{index}_html")
        assert markup, index
        assert markup.startswith("<div>")
        assert html_to_text(markup) == text


def test_html_bodies_are_published_as_custom_variables():
    variables = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=empty_registry()
    ).to_custom_variables()
    for index in (1, 2, 3, 4):
        assert variables[f"rendered_email_{index}_html"].startswith("<div>")
        assert variables[f"rendered_email_{index}"]


def test_control_rows_carry_no_html_body_either():
    control = resolve_wave1(
        _fields(), experiment_id=EXPERIMENT, b_split_pct=0, registry=empty_registry()
    )
    assert control.experiment_arm == "A"
    for index in (1, 2, 3, 4):
        assert getattr(control, f"rendered_email_{index}_html") == ""


def test_a_tampered_html_body_fails_qa():
    resolution = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=empty_registry()
    ).to_dict()
    resolution["rendered_email_1_html"] = resolution["rendered_email_1_html"].replace(
        "</div>", ' <a href="https://example.invalid">click</a></div>', 1
    )
    passed, reasons = run_qa_gates(resolution, policy=campaign_for_bucket("finance"))
    assert not passed
    assert "html_body_does_not_match_email_1" in reasons


def test_a_missing_html_body_fails_qa():
    resolution = resolve_challenger(
        _fields(), experiment_id=EXPERIMENT, registry=empty_registry()
    ).to_dict()
    resolution["rendered_email_3_html"] = ""
    passed, reasons = run_qa_gates(resolution, policy=campaign_for_bucket("finance"))
    assert not passed
    assert "missing_html_body_for_email_3" in reasons
