"""A Challenger lead whose copy cannot be rendered must BLOCK, never raise.

Why this file exists: every other integrated test routes to a CONTROL campaign
(`scenario.CONTROL_ID_BY_CAMPAIGN_KEY`), so the Challenger copy path was never
exercised on the real call graph. Production routes to the NINE CHALLENGER ids,
and `instantly_payload` is called inside the approval transaction as an INSERT
argument. A raise there propagates out of `_commit_approval` -- whose call sites
catch only `UniqueViolation` -- and would end the daily run on the first lead
whose role display fails QA. Measured on real approvals that is 23.3% of them.

The required behaviour is the one this codebase already uses for a
compliance-blocked lead: approve it, store it, count it, and make neither outbox
item claimable, with the named reason on the row.
"""
from __future__ import annotations

import pytest

from tests_core.helpers import opportunity_service, sqlall
from tests_core.seed import apollo_for, seed_opportunity
from tgtc_core.policy.campaigns import (CAMPAIGN_BY_FUNCTION, CAMPAIGN_ENV_BY_FUNCTION,
                                        FUNCTION_KEYS, KNOWN_CHALLENGER_CAMPAIGN_IDS)

#: The live Challenger ids, keyed the same way `scenario.CONTROL_ID_BY_CAMPAIGN_KEY` is.
CHALLENGER_ID_BY_CAMPAIGN_KEY = {
    "product": "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0",
    "operations": "69def27c-7799-41a2-9ba8-205e54ab071b",
    "finance": "7b319c7a-cc55-4e08-8a47-7058c345d8ae",
    "people_hr": "d2326028-e312-405b-9e16-526bd309d4dd",
    "ecommerce": "c3e81c21-db44-40f5-addc-d9945a78394b",
    "customer_experience": "269cd138-00b1-48c3-9093-16c36120a20e",
    "marketing_creative": "1feb6344-6065-49d7-9764-d125985fb9c9",
    "gtm_systems": "8f25abd5-568a-4e88-b310-9acf85161c6c",
    "ai_technical": "8bfa0769-4b9a-4346-8e93-17ac8b726dce",
}
assert set(CHALLENGER_ID_BY_CAMPAIGN_KEY.values()) == set(KNOWN_CHALLENGER_CAMPAIGN_IDS)


def challenger_env():
    return {CAMPAIGN_ENV_BY_FUNCTION[f]: CHALLENGER_ID_BY_CAMPAIGN_KEY[CAMPAIGN_BY_FUNCTION[f].key]
            for f in FUNCTION_KEYS}


def _approve(conn, clock, title):
    _pid, _eid, oid = seed_opportunity(conn, clock, title=title)
    fake = apollo_for("acme.com", "Acme")
    out = opportunity_service(conn, fake, clock, campaign_env=challenger_env()).process(oid)
    return out, sqlall(conn, "SELECT * FROM approvals ORDER BY id"), \
        sqlall(conn, "SELECT channel, state, blocked_reason FROM delivery_outbox ORDER BY channel")


# Titles verified to survive the core's own role pipeline still unusable. The
# core already normalises some shapes ("… - Remote (Evergreen)" becomes
# "Customer Success Manager") and falls back to a function noun for others, so
# only these actually reach the renderer unsafe -- which is the real production
# population: 896 unsafe-character + 581 appended-qualifier refusals of 1,829.
@pytest.mark.parametrize("title, expected_reason", [
    ("Manager, Customer Success", "role_display_contains_unsafe_characters"),
    ("Customer Success Manager, Enterprise", "role_display_contains_unsafe_characters"),
    ("Customer Success Manager - EMEA", "role_display_carries_an_appended_qualifier"),
])
def test_unrenderable_challenger_copy_blocks_the_outbox_and_does_not_raise(conn, clock, title, expected_reason):
    out, approvals, outbox = _approve(conn, clock, title)

    # the lead is approved, stored and counted -- never dropped
    assert out.outcome == "approved", out
    assert len(approvals) == 1

    # ...and NEITHER outbox item is claimable, each carrying the named reason
    assert [r["state"] for r in outbox] == ["blocked", "blocked"]
    for row in outbox:
        assert row["blocked_reason"] == "challenger_copy_qa_failed:" + expected_reason, row


def test_a_renderable_challenger_lead_is_pending_and_carries_the_copy(conn, clock):
    out, approvals, outbox = _approve(conn, clock, "Customer Success Manager")
    assert out.outcome == "approved" and len(approvals) == 1
    assert [r["state"] for r in outbox] == ["pending", "pending"]
    assert all(not r["blocked_reason"] for r in outbox)

    stored = sqlall(conn, "SELECT payload_json FROM delivery_outbox WHERE channel = 'instantly'")
    variables = stored[0]["payload_json"]["custom_variables"]
    for key in ("rendered_subject", "rendered_email_1_html", "rendered_email_2_html",
                "rendered_email_3_html", "rendered_email_4_html"):
        assert variables.get(key), key
        assert "{{" not in variables[key]


def test_a_blocked_copy_lead_can_never_be_drained(conn, clock):
    """The row is terminal for delivery: draining the channel must not send it."""
    from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
    from tests_core.helpers import delivery_service

    _out, _approvals, outbox = _approve(conn, clock, "Manager, Customer Success")
    assert [r["state"] for r in outbox] == ["blocked", "blocked"]

    instantly = FakeInstantly(campaign_status={CHALLENGER_ID_BY_CAMPAIGN_KEY["customer_experience"]: 1})
    svc = delivery_service(conn, FakeAirtable(), instantly, clock)
    outcomes = svc.drain("instantly")
    assert all(o.outcome != "delivered" for o in outcomes), outcomes
    created = sqlall(conn, "SELECT count(*) AS n FROM delivery_receipts WHERE receipt_kind = 'created'")
    assert created[0]["n"] == 0
