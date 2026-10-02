"""Regression for the rebuilt core enrolling Challenger leads without their copy.

The nine live Challenger campaigns carry NO literal copy: every step body is
exactly ``{{rendered_email_N_html}}`` plus the account signature, and step 1's
subject is ``{{rendered_subject}}``. A lead enrolled without those variables
therefore receives an empty subject and a signature-only body, which is the
2026-10-02 incident. These tests pin the contract that prevents it.
"""
from copy import deepcopy
from datetime import datetime

import pytest

from tests_core.test_approval_gate import _inputs
from tgtc_core.domain import approval
from tgtc_core.policy.campaigns import FUNCTION_KEYS

CHALLENGER = "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0"  # WAVE1 CHALLENGER - PRODUCT
REQUIRED = ("rendered_subject", *(f"rendered_email_{i}_html" for i in range(1, 5)))


def _challenger_lead(function="product"):
    lead = approval.build_approved_lead(**_inputs()).lead
    lead.update(function_key=function, campaign_id=CHALLENGER)
    return lead


@pytest.mark.parametrize("function", FUNCTION_KEYS)
def test_every_challenger_function_has_complete_frozen_copy(function):
    payload = approval.instantly_payload(_challenger_lead(function),
                                         skip_if_in_workspace=True, verify_on_import=False)
    variables = payload["custom_variables"]
    for key in REQUIRED:
        assert variables.get(key), f"{function} missing {key}"
        assert "{{" not in variables[key] and "}}" not in variables[key]
    assert "Jane" in variables["rendered_email_1_html"]
    assert "Product Manager" in variables["rendered_email_1_html"]


def test_control_still_uses_live_static_copy_without_rendered_variables():
    lead = approval.build_approved_lead(**_inputs()).lead
    payload = approval.instantly_payload(lead, skip_if_in_workspace=True, verify_on_import=False)
    assert not any(key.startswith("rendered_") for key in payload["custom_variables"])


def test_copy_matches_the_frozen_renderer_and_does_not_assign_a_legacy_experiment():
    from outbound_wave1.resolver import resolve_challenger

    lead = _challenger_lead()
    expected = resolve_challenger(approval.airtable_fields(lead, ""),
                                  as_of=datetime.fromisoformat(str(lead["approved_at"]).replace("Z", "+00:00")))
    variables = approval.instantly_payload(lead, skip_if_in_workspace=True,
                                           verify_on_import=False)["custom_variables"]
    assert all(variables[key] == getattr(expected, key) for key in REQUIRED)
    # The rebuilt core routes by policy; it must not manufacture legacy A/B
    # assignment metadata that would misrepresent an experiment arm.
    assert not {"experiment_id", "experiment_arm", "company_assignment_key"} & variables.keys()


def test_provider_itself_refuses_a_challenger_without_copy():
    from types import SimpleNamespace

    from tgtc_core.providers.instantly import InstantlyClient

    calls = []
    transport = SimpleNamespace(request=lambda *a, **k: calls.append((a, k)))
    client = InstantlyClient(transport, api_key="test-key",
                             base_url="https://api.instantly.ai/api/v2")
    with pytest.raises(ValueError, match="challenger_copy_missing_variables"):
        client.create_lead({"campaign": CHALLENGER, "email": "test@example.com"})
    assert calls == [], "transport must never see an incomplete Challenger create"


@pytest.mark.parametrize("key", REQUIRED)
def test_stored_challenger_payload_missing_any_copy_field_is_rejected(key):
    from tgtc_core.domain.outbound_copy import copy_block_reason

    variables = dict.fromkeys(REQUIRED, "Meaningful approved email copy")
    variables.pop(key)
    assert copy_block_reason({"campaign": CHALLENGER, "custom_variables": variables})


@pytest.mark.parametrize("body", ["", "   ", "<div><br></div>", "<p>&nbsp;</p>",
                                 "{{rendered_email_1_html}}", "<script>hidden</script>"])
def test_html_markup_or_unresolved_tokens_cannot_pass_as_body(body):
    from tgtc_core.domain.outbound_copy import copy_block_reason

    variables = dict.fromkeys(REQUIRED, "Meaningful approved email copy")
    variables["rendered_email_1_html"] = body
    assert copy_block_reason({"campaign": CHALLENGER, "custom_variables": variables})


def test_a_complete_challenger_payload_is_not_blocked():
    from tgtc_core.domain.outbound_copy import copy_block_reason

    variables = dict.fromkeys(REQUIRED, "Meaningful approved email copy")
    assert copy_block_reason({"campaign": CHALLENGER, "custom_variables": variables}) == ""


def test_control_payload_is_never_judged_against_the_challenger_contract():
    from tgtc_core.domain.outbound_copy import copy_block_reason

    assert copy_block_reason({"campaign": "45ac1e03-67e7-4bdd-b372-808042104e4c",
                              "custom_variables": {}}) == ""


def test_render_failure_is_named_and_never_returns_an_incomplete_payload(monkeypatch):
    from outbound_wave1 import resolver

    lead = _challenger_lead()
    original = resolver.resolve_challenger

    def fail(*a, **k):
        result = deepcopy(original(*a, **k))
        result.qa_pass = False
        result.qa_reasons = ["forced_qa_failure"]
        return result

    monkeypatch.setattr(resolver, "resolve_challenger", fail)
    with pytest.raises(ValueError, match="challenger_copy_qa_failed"):
        approval.instantly_payload(lead, skip_if_in_workspace=True, verify_on_import=False)


def test_pending_outbox_with_missing_copy_never_reaches_provider(monkeypatch, clock):
    from types import SimpleNamespace

    from tgtc_core.services.delivery import DeliveryService, OutboxItem

    def forbidden(*a, **k):
        pytest.fail("incomplete Challenger copy reached the provider")

    svc = DeliveryService(None, airtable=None,
                          instantly=SimpleNamespace(create_lead=forbidden), now=clock)
    monkeypatch.setattr(svc, "_precheck", lambda item: None)
    changes = []
    monkeypatch.setattr(svc, "_set", lambda item, state, **kw: changes.append((state, kw)))
    item = OutboxItem(1, 1, "instantly", "key",
                      {"campaign": CHALLENGER, "email": "test@example.com"},
                      "claimed", 1, "token", "pending")
    outcome = svc.process_instantly(item)
    assert outcome.outcome == "blocked"
    assert outcome.reason.startswith("challenger_copy_")
    assert changes and changes[0][0] == "blocked"
