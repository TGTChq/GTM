"""Wave 1 Challenger campaign builder: payload shape and read-only guarantees.

Offline. Nothing here reaches Instantly; the one HTTP entry point is patched.
"""

from __future__ import annotations

import json

import pytest

import create_wave1_challenger_campaigns as builder
from outbound_wave1.campaigns import (
    CAMPAIGNS,
    CUSTOMER_EXPERIENCE,
    FINANCE,
    CampaignPolicy,
)
from outbound_wave1.timing import SEQUENCE_DAY_LABELS, SEQUENCE_OFFSET_DAYS


def _control(name="FINANCE", **overrides):
    control = {
        "id": "control-finance-id",
        "name": name,
        "status": 1,
        "daily_limit": 550,
        "daily_max_leads": 100,
        "text_only": True,
        "first_email_text_only": False,
        "link_tracking": False,
        "open_tracking": False,
        "stop_on_reply": True,
        "stop_on_auto_reply": False,
        "stop_for_company": True,
        "insert_unsubscribe_header": True,
        "match_lead_esp": True,
        "allow_risky_contacts": False,
        "prioritize_new_leads": True,
        "cc_list": [],
        "bcc_list": [],
        "provider_routing_rules": [],
        "email_list": ["a@example.invalid", "b@example.invalid"],
        "email_tag_list": ["tag-1"],
        "campaign_schedule": {
            "schedules": [{
                "name": "Default Schedule",
                "timing": {"from": "08:00", "to": "18:00"},
                "days": {"0": False, "1": True, "2": True, "3": True,
                         "4": True, "5": True, "6": False},
                "timezone": "America/Chicago",
            }]
        },
        "custom_variables": {"open_role": True, "role_focus": True},
        "core_variables": {"firstName": True, "companyName": True},
        "sequences": [{"steps": [{"type": "email", "delay": 3, "variants": [
            {"subject": "{{open_role}}", "body": "<div>control</div>"}]}]}],
        "organization": "org",
        "owned_by": "owner",
        "timestamp_created": "2026-01-01T00:00:00.000Z",
    }
    control.update(overrides)
    return control


# ---------------------------------------------------------------------------
# Sequence shape
# ---------------------------------------------------------------------------

def test_the_sequence_is_four_steps_on_the_frozen_day_1_4_8_13_cadence():
    sequences = builder.build_sequence()
    assert len(sequences) == 1
    steps = sequences[0]["steps"]
    assert len(steps) == len(SEQUENCE_DAY_LABELS) == 4
    # Instantly reads a step's delay as "wait this long before the NEXT step", so
    # the first three carry the Day 1->4->8->13 gaps and the last is unused.
    assert [step["delay"] for step in steps[:-1]] == list(SEQUENCE_OFFSET_DAYS) == [3, 4, 5]
    assert steps[-1]["delay"] == builder.TRAILING_DELAY_DAYS
    assert all(step["delay_unit"] == "days" for step in steps)


def test_only_email_1_carries_a_subject_so_2_to_4_reply_on_the_thread():
    steps = builder.build_sequence()[0]["steps"]
    assert steps[0]["variants"][0]["subject"] == "{{rendered_subject}}"
    assert [step["variants"][0]["subject"] for step in steps[1:]] == ["", "", ""]


def test_each_step_body_is_its_own_rendered_html_variable_plus_the_signature():
    steps = builder.build_sequence()[0]["steps"]
    for index, step in enumerate(steps, start=1):
        body = step["variants"][0]["body"]
        assert body == f"{{{{rendered_email_{index}_html}}}}{builder.SIGNATURE_BLOCK}"
        # The plain-text variants must never be the campaign body: an Instantly
        # body is HTML and would collapse their paragraph breaks.
        assert f"{{{{rendered_email_{index}}}}}" not in body


def test_every_step_has_exactly_one_variant():
    """The experiment is Control vs Challenger; a second variant inside the
    Challenger would split the treatment arm again."""
    for step in builder.build_sequence()[0]["steps"]:
        assert len(step["variants"]) == 1


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def test_the_payload_mirrors_every_operational_control_setting():
    control = _control()
    payload = builder.build_payload(FINANCE, control)
    for key in builder.MIRRORED_SETTINGS:
        assert payload[key] == control[key], key


def test_the_challenger_shares_the_controls_sender_pool():
    """A dedicated pool would confound the arm with mailbox reputation, and a
    mailbox can belong to several campaigns without touching the Control."""
    control = _control()
    payload = builder.build_payload(FINANCE, control)
    assert payload["email_list"] == control["email_list"]


def test_the_payload_declares_the_rendered_variables_and_keeps_the_controls():
    payload = builder.build_payload(FINANCE, _control())
    for name in builder.CHALLENGER_CUSTOM_VARIABLES:
        assert payload["custom_variables"][name] is True
    assert payload["custom_variables"]["open_role"] is True
    assert payload["custom_variables"]["role_focus"] is True


def test_the_name_is_prefixed_so_a_challenger_is_never_mistaken_for_a_control():
    payload = builder.build_payload(FINANCE, _control(name="FINANCE"))
    assert payload["name"] == "WAVE1 CHALLENGER - FINANCE"
    assert payload["name"] != "FINANCE"


def test_the_payload_carries_no_control_identity():
    """Nothing that would address, or attempt to overwrite, the live campaign."""
    payload = builder.build_payload(FINANCE, _control())
    for key in ("id", "status", "organization", "owned_by",
                "timestamp_created", "timestamp_updated"):
        assert key not in payload


def test_the_control_sequence_is_never_copied():
    payload = builder.build_payload(FINANCE, _control())
    assert "control" not in json.dumps(payload["sequences"])


# ---------------------------------------------------------------------------
# Control resolution
# ---------------------------------------------------------------------------

def test_customer_experience_resolves_one_control_from_its_two_buckets(monkeypatch):
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS", "cx-id")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT", "cx-id")
    assert builder.control_campaign_id(CUSTOMER_EXPERIENCE) == "cx-id"


def test_disagreeing_control_ids_are_an_error_not_a_guess(monkeypatch):
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS", "cx-success")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT", "cx-support")
    with pytest.raises(RuntimeError, match="disagree"):
        builder.control_campaign_id(CUSTOMER_EXPERIENCE)


def test_a_missing_control_id_is_an_error(monkeypatch):
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FINANCE", "")
    monkeypatch.setattr(__import__("config"), "INSTANTLY_CAMPAIGN_FINANCE", "", raising=False)
    with pytest.raises(RuntimeError, match="no control campaign id"):
        builder.control_campaign_id(FINANCE)


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------

def test_planning_issues_get_requests_only(monkeypatch):
    calls = []

    def _fake(method, path, body=None):
        calls.append((method, path, body))
        return _control(name="FINANCE")

    monkeypatch.setattr(builder, "_request", _fake)
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FINANCE", "control-finance-id")
    artifact = builder.plan([FINANCE])

    assert [method for method, _path, _body in calls] == ["GET"]
    assert artifact["entries"][0]["control_campaign_id"] == "control-finance-id"
    assert artifact["entries"][0]["challenger_step_count"] == 4
    assert artifact["entries"][0]["challenger_day_labels"] == [1, 4, 8, 13]


def test_execute_only_ever_posts_new_campaigns(monkeypatch):
    calls = []

    def _fake(method, path, body=None):
        calls.append((method, path))
        return {"id": "new-id", "name": body["name"], "status": 0}

    monkeypatch.setattr(builder, "_request", _fake)
    created = builder.execute([{
        "campaign_key": "finance",
        "role_buckets": ["finance"],
        "post_body": builder.build_payload(FINANCE, _control()),
    }])
    assert calls == [("POST", "campaigns")]
    assert created[0]["challenger_campaign_id"] == "new-id"


def test_the_env_value_maps_every_bucket_the_campaign_serves():
    created = [{
        "campaign_key": "customer_experience",
        "role_buckets": list(CUSTOMER_EXPERIENCE.buckets),
        "challenger_campaign_id": "cx-challenger",
    }]
    value = builder.challenger_campaign_env(created)
    assert json.loads(value) == {
        "customer_success": "cx-challenger",
        "customer_support": "cx-challenger",
    }


def test_a_creation_that_returned_no_id_is_not_mapped():
    value = builder.challenger_campaign_env(
        [{"role_buckets": ["finance"], "challenger_campaign_id": None}]
    )
    assert json.loads(value) == {}


def test_every_wave1_campaign_can_be_built():
    for policy in CAMPAIGNS:
        assert isinstance(policy, CampaignPolicy)
        payload = builder.build_payload(policy, _control(name=policy.name))
        assert payload["name"].startswith(builder.CHALLENGER_NAME_PREFIX)
        assert len(payload["sequences"][0]["steps"]) == 4
