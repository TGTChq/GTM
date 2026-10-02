"""The copy refusal, decided from the posting BEFORE a contact is paid for.

Apollo credits are spent revealing and verifying a contact, which happens before
the copy gate runs at approval. So a lead the gate refuses has already cost its
credits. `challenger_copy_refusal_from_posting` answers the same question from
posting facts alone.

It must give the SAME answer and the SAME reason as the final guard, must not
depend on who the contact turns out to be, and must not replace the final guard,
which still runs on the real lead.
"""
from __future__ import annotations

import pytest

from tests_core.test_approval_gate import _inputs
from tgtc_core.domain import approval

CHALLENGER = "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0"
CONTROL = "45ac1e03-67e7-4bdd-b372-808042104e4c"


def _lead(role=None, campaign=CHALLENGER, **over):
    lead = approval.build_approved_lead(**_inputs()).lead
    lead["campaign_id"] = campaign
    if role is not None:
        lead["open_role"] = role
    lead.update(over)
    return lead


@pytest.mark.parametrize("role, expected", [
    ("Manager, Product", "challenger_copy_qa_failed:role_display_contains_unsafe_characters"),
    ("Product Manager - EMEA", "challenger_copy_qa_failed:role_display_carries_an_appended_qualifier"),
    ("Product Manager for Strategic Enterprise Accounts and Partnerships",
     "challenger_copy_qa_failed:role_display_longer_than_48_chars"),
])
def test_the_posting_probe_gives_the_final_guard_s_exact_reason(role, expected):
    lead = _lead(role)
    assert approval.challenger_copy_refusal_from_posting(lead) == expected
    # and the final guard, on the REAL lead, says the same thing
    final, _ = approval.instantly_payload_with_copy_state(
        lead, skip_if_in_workspace=True, verify_on_import=False)
    assert final == expected


def test_a_renderable_posting_is_not_pre_refused():
    lead = _lead("Product Manager")
    assert approval.challenger_copy_refusal_from_posting(lead) == ""
    final, payload = approval.instantly_payload_with_copy_state(
        lead, skip_if_in_workspace=True, verify_on_import=False)
    assert final == ""
    assert payload["custom_variables"]["rendered_subject"]


def test_a_control_destination_is_never_pre_refused():
    assert approval.challenger_copy_refusal_from_posting(_lead("Manager, Product", campaign=CONTROL)) == ""


@pytest.mark.parametrize("first, last, title", [
    ("Jane", "Doe", "VP Product"),
    ("Zoë", "Ngô", "Chief Product Officer"),
    ("A", "B", "Head of Product"),
])
def test_the_verdict_does_not_depend_on_who_the_contact_is(first, last, title):
    """The whole point: the answer is available before anybody is revealed."""
    unrenderable = approval.challenger_copy_refusal_from_posting(
        _lead("Manager, Product", first_name=first, last_name=last, buyer_title=title))
    renderable = approval.challenger_copy_refusal_from_posting(
        _lead("Product Manager", first_name=first, last_name=last, buyer_title=title))
    assert unrenderable.startswith("challenger_copy_qa_failed:")
    assert renderable == ""


def test_it_agrees_with_the_final_guard_across_a_sweep_of_role_shapes():
    roles = ["Product Manager", "Senior Product Manager", "Manager, Product",
             "Product Manager - EMEA", "Product Manager (Remote)", "product role",
             "Product Manager, Enterprise", "Head of Product", "Product Lead",
             "Product Manager is hiring", "VP Product", "Director of Product",
             "Product Manager for Strategic Enterprise Accounts and Partnerships"]
    for role in roles:
        lead = _lead(role)
        pre = approval.challenger_copy_refusal_from_posting(lead)
        final, _ = approval.instantly_payload_with_copy_state(
            lead, skip_if_in_workspace=True, verify_on_import=False)
        assert pre == final, (role, pre, final)


def test_the_final_guard_is_still_enforced_and_is_not_replaced():
    """A pre-check that silently became the only check would be a regression."""
    lead = _lead("Manager, Product")
    with pytest.raises(ValueError, match="challenger_copy_qa_failed"):
        approval.instantly_payload(lead, skip_if_in_workspace=True, verify_on_import=False)
    from tgtc_core.domain.outbound_copy import copy_block_reason
    assert copy_block_reason({"campaign": CHALLENGER, "custom_variables": {}})


def test_the_probe_contact_never_reaches_a_payload():
    """The placeholder exists only to answer the question; it must not be stored."""
    lead = _lead("Product Manager")
    _refusal, payload = approval.instantly_payload_with_copy_state(
        lead, skip_if_in_workspace=True, verify_on_import=False)
    blob = str(payload)
    assert "probe@example.invalid" not in blob
    assert "Probe" not in payload["email"]
    assert payload["email"] == lead["email"]
