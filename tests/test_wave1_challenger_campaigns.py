"""Wave 1 Challenger campaign builder: payload shape and read-only guarantees.

Offline. Nothing here reaches Instantly; the one HTTP entry point is patched.
"""

from __future__ import annotations

import json
import os

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


def test_execute_only_ever_posts_new_campaigns(monkeypatch, tmp_path):
    calls = []

    def _fake(method, path, body=None, *, allow_write=False):
        calls.append((method, path, allow_write))
        return {"id": "new-id", "name": body["name"], "status": 0}

    monkeypatch.setattr(builder, "_request", _fake)
    created = builder.execute([{
        "campaign_key": "finance",
        "role_buckets": ["finance"],
        "challenger_name": "WAVE1 CHALLENGER - FINANCE",
        "post_body": builder.build_payload(FINANCE, _control()),
    }], state_path=str(tmp_path / "cp.json"))
    assert calls == [("POST", "campaigns", True)]
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


# ---------------------------------------------------------------------------
# Creator safety: idempotency, partial failure, and the write allowlist
# ---------------------------------------------------------------------------

def _entry(key="finance", name="WAVE1 CHALLENGER - FINANCE"):
    return {
        "campaign_key": key,
        "role_buckets": [key],
        "challenger_name": name,
        "control_campaign_id": "control-id",
        "control_campaign_name": "FINANCE",
        "post_body": {"name": name},
    }


def test_a_non_get_is_refused_unless_it_is_the_one_allowed_write():
    for method, path in (("PATCH", "campaigns/x"), ("DELETE", "campaigns/x"),
                         ("POST", "leads"), ("POST", "campaigns/x/activate")):
        with pytest.raises(builder.UnauthorisedWrite):
            builder._request(method, path, {}, allow_write=True)


def test_even_the_allowed_write_is_refused_without_an_explicit_opt_in():
    with pytest.raises(builder.UnauthorisedWrite):
        builder._request("POST", "campaigns", {})


def test_the_allowlist_holds_exactly_one_write():
    assert builder.WRITE_ALLOWLIST == frozenset({("POST", "campaigns")})


def test_a_created_campaign_is_checkpointed_before_the_next_one_starts(tmp_path):
    """A failure on campaign three must not lose one and two."""
    state_path = str(tmp_path / "plan.json.checkpoint.json")
    calls = []

    def _fake(method, path, body=None, *, allow_write=False):
        calls.append(body["name"])
        if len(calls) == 3:
            raise RuntimeError("Instantly 500")
        return {"id": f"id-{len(calls)}", "name": body["name"], "status": 0}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(builder, "_request", _fake)
    entries = [_entry(f"k{i}", f"WAVE1 CHALLENGER - {i}") for i in range(1, 5)]
    with pytest.raises(RuntimeError, match="Instantly 500"):
        builder.execute(entries, state_path=state_path)
    monkey.undo()

    saved = builder.load_checkpoint(state_path)
    assert sorted(saved) == ["k1", "k2"], saved
    assert saved["k1"]["challenger_campaign_id"] == "id-1"
    assert saved["k2"]["challenger_campaign_id"] == "id-2"


def test_a_rerun_skips_what_the_checkpoint_already_records(tmp_path):
    state_path = str(tmp_path / "plan.json.checkpoint.json")
    builder.save_checkpoint(state_path, {
        "k1": {"campaign_key": "k1", "role_buckets": ["finance"],
               "challenger_campaign_id": "id-1", "challenger_name": "x", "status": 0},
    })
    posted = []

    def _fake(method, path, body=None, *, allow_write=False):
        posted.append(body["name"])
        return {"id": "id-2", "name": body["name"], "status": 0}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(builder, "_request", _fake)
    created = builder.execute(
        [_entry("k1", "WAVE1 CHALLENGER - 1"), _entry("k2", "WAVE1 CHALLENGER - 2")],
        state_path=state_path)
    monkey.undo()

    assert posted == ["WAVE1 CHALLENGER - 2"], posted
    assert [c["challenger_campaign_id"] for c in created] == ["id-1", "id-2"]


def test_a_full_rerun_creates_nothing(tmp_path):
    state_path = str(tmp_path / "plan.json.checkpoint.json")
    builder.save_checkpoint(state_path, {
        "k1": {"campaign_key": "k1", "role_buckets": ["a"],
               "challenger_campaign_id": "id-1", "challenger_name": "x", "status": 0},
    })

    def _boom(*_a, **_k):
        raise AssertionError("must not POST")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(builder, "_request", _boom)
    created = builder.execute([_entry("k1", "WAVE1 CHALLENGER - 1")], state_path=state_path)
    monkey.undo()
    assert len(created) == 1


def test_the_checkpoint_is_written_atomically(tmp_path):
    path = str(tmp_path / "cp.json")
    builder.save_checkpoint(path, {"a": {"challenger_campaign_id": "1"}})
    assert not os.path.exists(f"{path}.tmp")
    assert builder.load_checkpoint(path)["a"]["challenger_campaign_id"] == "1"


def test_an_unreadable_checkpoint_fails_loudly_rather_than_licensing_duplicates(tmp_path):
    path = str(tmp_path / "cp.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ truncated")
    with pytest.raises(RuntimeError, match="could not be read"):
        builder.load_checkpoint(path)


# --- structural identity -----------------------------------------------------

def test_a_challenger_is_identified_by_its_body_shape_not_only_its_name():
    """A name can be edited in the UI; only this script writes that body."""
    renamed = {"name": "Renamed By Someone", "sequences": [{"steps": [
        {"variants": [{"body": "{{rendered_email_1_html}}<div>x</div>"}]}]}]}
    assert builder.is_challenger_campaign(renamed)


def test_a_challenger_is_identified_by_its_name_prefix():
    assert builder.is_challenger_campaign({"name": "WAVE1 CHALLENGER - FINANCE"})


def test_a_control_campaign_is_not_mistaken_for_a_challenger():
    control = {"name": "FINANCE", "sequences": [{"steps": [
        {"variants": [{"body": "<div>Hi {{firstName}},</div>"}]}]}]}
    assert not builder.is_challenger_campaign(control)


# --- preflight ---------------------------------------------------------------

def test_preflight_blocks_when_a_challenger_exists_outside_the_checkpoint(monkeypatch):
    monkeypatch.setattr(builder, "list_all_campaigns", lambda: [
        {"id": "orphan", "name": "WAVE1 CHALLENGER - FINANCE"}])
    report = builder.preflight([_entry()], state={})
    assert report["safe_to_create"] is False
    assert report["blocking"]


def test_preflight_allows_a_resume_of_a_campaign_this_checkpoint_created(monkeypatch):
    monkeypatch.setattr(builder, "list_all_campaigns", lambda: [
        {"id": "known", "name": "WAVE1 CHALLENGER - FINANCE"}])
    state = {"finance": {"challenger_campaign_id": "known"}}
    report = builder.preflight([_entry()], state=state)
    assert report["safe_to_create"] is True
    assert report["resumable"] == [{"campaign_key": "finance", "id": "known"}]


def test_preflight_is_clean_on_an_empty_workspace(monkeypatch):
    monkeypatch.setattr(builder, "list_all_campaigns", lambda: [
        {"id": "c1", "name": "FINANCE"}])
    report = builder.preflight([_entry()], state={})
    assert report["safe_to_create"] is True
    assert report["existing_challengers"] == []
