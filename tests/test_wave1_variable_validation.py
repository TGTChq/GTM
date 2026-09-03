"""The Wave 1 variable-validation tool: scope guards and payload shape.

Offline. The single HTTP entry point is patched or the guard fires before it.
"""

from __future__ import annotations

import json

import pytest

import validate_wave1_variables as tool


# ---------------------------------------------------------------------------
# Scope: two writes, one campaign, never a Control
# ---------------------------------------------------------------------------

def test_only_two_writes_are_permitted():
    assert tool.ALLOWED_WRITES == frozenset({
        ("POST", "leads"),
        ("PATCH", f"campaigns/{tool.PRODUCT_CHALLENGER_ID}"),
    })


@pytest.mark.parametrize("method,path", [
    ("DELETE", f"campaigns/{tool.PRODUCT_CHALLENGER_ID}"),
    ("POST", f"campaigns/{tool.PRODUCT_CHALLENGER_ID}/activate"),
    ("POST", f"campaigns/{tool.PRODUCT_CHALLENGER_ID}/pause"),
    ("POST", "campaigns"),
    ("PATCH", "leads/some-lead"),
    ("POST", f"campaigns/{tool.PRODUCT_CHALLENGER_ID}/variables"),
])
def test_every_other_write_is_refused(method, path):
    with pytest.raises(tool.UnauthorisedWrite):
        tool._request(method, path, {}, execute=True)


def test_a_patch_aimed_at_another_challenger_is_refused():
    """The eight other challengers are as out of bounds as the Controls."""
    with pytest.raises(tool.UnauthorisedWrite):
        tool._request(
            "PATCH", "campaigns/69def27c-7799-41a2-9ba8-205e54ab071b",
            {"sequences": []}, execute=True)


@pytest.mark.parametrize("control_id", sorted(tool.CONTROL_CAMPAIGN_IDS))
def test_no_control_campaign_can_be_patched(control_id):
    with pytest.raises(tool.UnauthorisedWrite):
        tool._request("PATCH", f"campaigns/{control_id}", {}, execute=True)


def test_a_lead_aimed_at_another_campaign_is_refused():
    with pytest.raises(tool.UnauthorisedWrite, match="not the PRODUCT"):
        tool._request(
            "POST", "leads",
            {"campaign": "1db88bbe-b2cf-4574-a5b7-1cb948151a86", "email": "x@y.invalid"},
            execute=True)


def test_a_lead_with_no_campaign_is_refused():
    with pytest.raises(tool.UnauthorisedWrite):
        tool._request("POST", "leads", {"email": "x@y.invalid"}, execute=True)


def test_a_permitted_write_still_needs_execute():
    with pytest.raises(tool.UnauthorisedWrite, match="without --execute"):
        tool._request(
            "POST", "leads",
            {"campaign": tool.PRODUCT_CHALLENGER_ID, "email": "x@y.invalid"})


def test_the_target_campaign_is_a_constant_not_an_argument():
    """No invocation can point this tool at a different campaign."""
    import argparse
    parser = argparse.ArgumentParser()
    source = open(tool.__file__, encoding="utf-8").read()
    assert "--campaign" not in source
    assert tool.PRODUCT_CHALLENGER_ID == "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0"


# ---------------------------------------------------------------------------
# Synthetic lead payload
# ---------------------------------------------------------------------------

def test_the_lead_carries_every_wave1_variable_name():
    variables = tool.synthetic_variables()
    for name in tool.WAVE1_VARIABLE_NAMES:
        assert name in variables, name
    assert len(tool.WAVE1_VARIABLE_NAMES) == 24


def test_every_custom_variable_value_is_a_string():
    """The docs restrict values to string, number, boolean or null -- no objects
    or arrays."""
    for name, value in tool.synthetic_variables().items():
        assert isinstance(value, str), (name, type(value))


def test_the_html_sentinels_distinguish_all_four_outcomes():
    """Rendered markup, escaped literal, removed, or blanked."""
    first = tool.HTML_SENTINELS[1]
    assert "WAVE1_HTML_ALPHA" in first and "WAVE1_HTML_BETA" in first
    # Two divs separated by a break: as markup this is two lines, escaped it is
    # visible tags, removed it is absent, blanked it is an empty gap.
    assert first.count("<div>") == 3
    assert "&amp;" in first


def test_the_lead_payload_uses_only_documented_fields():
    documented = {
        "campaign", "email", "personalization", "website", "last_name",
        "first_name", "company_name", "job_title", "phone", "lt_interest_status",
        "pl_value_lead", "list_id", "assigned_to", "skip_if_in_workspace",
        "skip_if_in_campaign", "skip_if_in_list", "blocklist_id",
        "verify_leads_for_lead_finder", "verify_leads_on_import", "custom_variables",
    }
    payload = tool.synthetic_lead_payload("wave1@example.invalid")
    assert set(payload) <= documented, set(payload) - documented


def test_the_lead_is_bound_to_the_product_challenger():
    payload = tool.synthetic_lead_payload("wave1@example.invalid")
    assert payload["campaign"] == tool.PRODUCT_CHALLENGER_ID
    assert payload["verify_leads_on_import"] is False
    assert payload["skip_if_in_campaign"] is True


# ---------------------------------------------------------------------------
# Campaign patch payload
# ---------------------------------------------------------------------------

def _live_campaign():
    return {
        "id": tool.PRODUCT_CHALLENGER_ID,
        "name": tool.PRODUCT_CHALLENGER_NAME,
        "status": 0,
        "email_list": ["a@x.invalid"],
        "daily_limit": 550,
        "sequences": [{"steps": [
            {"type": "email", "delay": d, "delay_unit": "days",
             "variants": [{"subject": s, "body": "<div><br /></div><div>"
                           "{{accountSignature}}</div>", "v_disabled": False}]}
            for d, s in ((3, "{{rendered_subject}}"), (4, ""), (5, ""), (1, ""))]}],
    }


def test_the_patch_sends_only_sequences():
    """Name, senders, schedule, limits, tracking, stops and status are untouched
    by construction -- they are not in the payload at all."""
    payload = tool.campaign_patch_payload(_live_campaign())
    assert set(payload) == {"sequences"}


def test_the_patch_restores_all_four_intended_bodies():
    payload = tool.campaign_patch_payload(_live_campaign())
    steps = payload["sequences"][0]["steps"]
    for index, step in enumerate(steps, start=1):
        assert step["variants"][0]["body"] == (
            f"{{{{rendered_email_{index}_html}}}}{tool.SIGNATURE_BLOCK}")


def test_the_patch_preserves_delays_and_subjects_from_what_is_live():
    payload = tool.campaign_patch_payload(_live_campaign())
    steps = payload["sequences"][0]["steps"]
    assert [s["delay"] for s in steps] == [3, 4, 5, 1]
    assert [s["variants"][0]["subject"] for s in steps] == [
        "{{rendered_subject}}", "", "", ""]


def test_the_patch_never_carries_a_status():
    payload = tool.campaign_patch_payload(_live_campaign())
    assert "status" not in json.dumps(payload) or '"status"' not in json.dumps(payload)


def test_a_campaign_with_the_wrong_step_count_is_refused():
    campaign = _live_campaign()
    campaign["sequences"][0]["steps"] = campaign["sequences"][0]["steps"][:2]
    with pytest.raises(RuntimeError, match="expected 4 steps"):
        tool.campaign_patch_payload(campaign)


def test_a_step_with_two_variants_is_refused():
    campaign = _live_campaign()
    campaign["sequences"][0]["steps"][0]["variants"].append({"subject": "", "body": ""})
    with pytest.raises(RuntimeError, match="2 variants"):
        tool.campaign_patch_payload(campaign)


def test_the_patch_does_not_mutate_the_campaign_it_was_given():
    campaign = _live_campaign()
    before = json.dumps(campaign)
    tool.campaign_patch_payload(campaign)
    assert json.dumps(campaign) == before


# ---------------------------------------------------------------------------
# Stage ordering
# ---------------------------------------------------------------------------

def test_the_patch_stage_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(tool, "get_campaign", _live_campaign)
    result = tool.stage_patch({}, execute=False)
    assert "would_patch" in result


def test_the_lead_stage_dry_run_writes_nothing():
    result = tool.stage_lead("wave1@example.invalid", {}, execute=False)
    assert "would_post" in result
    assert result["would_post"]["campaign"] == tool.PRODUCT_CHALLENGER_ID


# ---------------------------------------------------------------------------
# Discriminating probe
# ---------------------------------------------------------------------------

def test_the_probe_touches_step_one_only():
    campaign = _live_campaign()
    stored = [s["variants"][0]["body"] for s in campaign["sequences"][0]["steps"]]
    payload = tool.probe_patch_payload(campaign)
    steps = payload["sequences"][0]["steps"]
    assert steps[0]["variants"][0]["body"] != stored[0]
    for index in (1, 2, 3):
        assert steps[index]["variants"][0]["body"] == stored[index], index


def test_the_probe_body_carries_both_marker_and_variable():
    payload = tool.probe_patch_payload(_live_campaign())
    body = payload["sequences"][0]["steps"][0]["variants"][0]["body"]
    assert tool.PATCH_PROBE_MARKER in body
    assert "{{rendered_email_1_html}}" in body
    assert body.endswith(tool.SIGNATURE_BLOCK)


def test_the_marker_contains_no_variable_syntax():
    """It must be removable only by the body not being written, never by a
    merge-variable sanitiser."""
    assert "{{" not in tool.PATCH_PROBE_MARKER and "}}" not in tool.PATCH_PROBE_MARKER


def test_the_probe_sends_only_sequences():
    assert set(tool.probe_patch_payload(_live_campaign())) == {"sequences"}


def test_the_probe_preserves_delays_and_subjects():
    steps = tool.probe_patch_payload(_live_campaign())["sequences"][0]["steps"]
    assert [s["delay"] for s in steps] == [3, 4, 5, 1]
    assert [s["variants"][0]["subject"] for s in steps] == [
        "{{rendered_subject}}", "", "", ""]


@pytest.mark.parametrize("body,expected", [
    ("<div>WAVE1_PATCH_PROBE</div>{{rendered_email_1_html}}<div>sig</div>", "A"),
    ("<div>WAVE1_PATCH_PROBE</div><div>sig</div>", "B"),
    ("<div><br /></div><div>{{accountSignature}}</div>", "C"),
    ("", "C"),
])
def test_classification_is_read_off_the_stored_body(body, expected):
    campaign = {"sequences": [{"steps": [{"variants": [{"body": body}]}]}]}
    assert tool.classify_probe(campaign)["verdict"] == expected


def test_the_probe_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(tool, "get_campaign", _live_campaign)
    assert "would_patch" in tool.stage_probe({}, execute=False)
