"""A copy refusal readable from the vacancy must cost no Apollo, and produce nothing.

Apollo credits buy a CONTACT, and that happens before the copy gate runs at
approval — so a lead the gate refuses has already cost its credits. Every gate the
refusal depends on reads posting facts, so the refusal is available first.

What must hold: zero paid Apollo calls, no Instantly lead, no Airtable row, a
named reason on the opportunity — and the final guard still enforcing at approval.
"""
from __future__ import annotations

import pytest

from tests_core.helpers import delivery_service, opportunity_service, sqlall
from tests_core.seed import apollo_for, seed_opportunity
from tests_core.test_challenger_copy_blocks_not_crashes import challenger_env
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly

# Verified against the core's own role pipeline: these reach the renderer unsafe.
UNRENDERABLE = "Manager, Customer Success"
RENDERABLE = "Customer Success Manager"


def _run(conn, clock, title):
    _p, _e, oid = seed_opportunity(conn, clock, title=title)
    fake = apollo_for("acme.com", "Acme")
    out = opportunity_service(conn, fake, clock, campaign_env=challenger_env()).process(oid)
    return out, fake, oid


def test_an_unrenderable_posting_is_closed_before_any_apollo_is_spent(conn, clock):
    out, fake, oid = _run(conn, clock, UNRENDERABLE)

    assert out.outcome == "closed"
    assert "challenger_copy_qa_failed:role_display_contains_unsafe_characters" in out.reason

    # the whole point: nothing was bought
    assert fake.served_paid == 0, "a refusal readable from the vacancy must not buy a contact"
    # and nothing was produced
    assert sqlall(conn, "SELECT id FROM approvals") == []
    assert sqlall(conn, "SELECT id FROM delivery_outbox") == []


def test_nothing_reaches_instantly_or_airtable_for_such_a_posting(conn, clock):
    _run(conn, clock, UNRENDERABLE)
    airtable, instantly = FakeAirtable(), FakeInstantly()
    svc = delivery_service(conn, airtable, instantly, clock)
    assert svc.drain("instantly") == []
    assert svc.drain("airtable") == []
    assert instantly.leads == {} and airtable.records == {}


def test_a_renderable_posting_still_proceeds_and_pays(conn, clock):
    """The gate must decline to buy, not stop the pipeline."""
    out, fake, _oid = _run(conn, clock, RENDERABLE)
    assert out.outcome == "approved", out
    assert fake.served_paid > 0, "a usable posting must still be enriched"
    rows = sqlall(conn, "SELECT state FROM delivery_outbox WHERE channel = 'instantly'")
    assert [r["state"] for r in rows] == ["pending"]


def test_a_control_destination_is_never_pre_refused(conn, clock):
    """Control campaigns carry their own static copy; the gate must ignore them."""
    _p, _e, oid = seed_opportunity(conn, clock, title=UNRENDERABLE)
    fake = apollo_for("acme.com", "Acme")
    out = opportunity_service(conn, fake, clock).process(oid)      # default = Control env
    assert out.outcome == "approved", out
    assert fake.served_paid > 0


@pytest.mark.parametrize("role, expect_refusal", [
    ("Manager, Product", True),
    ("Product Manager - EMEA", True),
    # the core's own role pipeline reduces an over-long title to the campaign's
    # function noun ("product role"), which PASSES the display gate -- measured,
    # not assumed. It is listed here so that behaviour stays pinned.
    ("Product Manager for Strategic Enterprise Accounts and Partnerships", False),
    ("Product Manager", False),
    ("Senior Product Manager", False),
])
def test_the_pre_check_and_the_final_guard_give_the_same_verdict(role, expect_refusal):
    """Two gates, one rule. If they could disagree, one of them is wrong.

    Uses the canonical approval fixture, so posting/classification/employer carry
    exactly the shapes `build_approved_lead` is given in production.
    """
    from tests_core.test_approval_gate import _inputs
    from tgtc_core.domain import approval

    CHALLENGER = "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0"
    base = _inputs()
    base["posting"]["title"] = role

    pre = approval.challenger_copy_refusal_before_enrichment(
        posting=base["posting"], classification=base["classification"], employer=base["employer"],
        function_key="product", campaign_id=CHALLENGER, allowed_campaign_ids=[CHALLENGER],
        signing_key=base["signing_key"], now=base["now"])

    built = approval.build_approved_lead(**{**base, "campaign_id": CHALLENGER,
                                           "allowed_campaign_ids": [CHALLENGER]})
    final, _payload = approval.instantly_payload_with_copy_state(
        built.lead, skip_if_in_workspace=True, verify_on_import=False)

    assert bool(pre) == expect_refusal, (role, pre)
    assert pre == final, (role, pre, final)


def test_the_final_guard_is_still_the_last_word(conn, clock):
    """Even if a pre-check were bypassed, approval must still refuse."""
    from tgtc_core.domain import approval
    from tests_core.test_approval_gate import _inputs

    lead = approval.build_approved_lead(**_inputs()).lead
    lead.update(campaign_id="7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0", open_role="Manager, Product")
    with pytest.raises(ValueError, match="challenger_copy_qa_failed"):
        approval.instantly_payload(lead, skip_if_in_workspace=True, verify_on_import=False)
