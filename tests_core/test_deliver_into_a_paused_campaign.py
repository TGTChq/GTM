"""Delivering an approved lead into a PAUSED campaign, when explicitly allowed.

Run 20261002T233322.605777Z-b7478fc9 left 25 approved Instantly rows undelivered
with `last_error = campaign_status_2`, and 25 Airtable rows behind them with
`awaiting_instantly`. The cause was OUR deferral rule, not a provider limitation:
`delivery.py` deferred on any `status != 1`.

Measured on the internal TEST B campaign on 2026-10-03 before writing this: creating
a genuinely new lead in a PAUSED campaign left the campaign at status 2, left
`emails_sent` unchanged, and the lead was never contacted (`timestamp_last_contact`
null, no sequence step). So a paused campaign accepts leads and sends nothing.

It stays OFF by default, because those leads do send the moment someone resumes the
campaign, and that is an operational decision. COMPLETED (3) is deliberately NOT
covered: adding leads to a drained campaign flips it to ACTIVE, which would send.
"""
from __future__ import annotations

import pytest

from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, sql1
from tests_core.seed import apollo_for, seed_opportunity
from tests_core.helpers import opportunity_service

CAMPAIGN = CONTROL_ID_BY_CAMPAIGN_KEY["customer_experience"]
ALLOW = {"TGTC_DELIVER_INTO_PAUSED_CAMPAIGNS": "1"}


def _approve(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock)
    opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    assert sql1(conn, "SELECT count(*) FROM approvals") == 1


def test_a_paused_campaign_defers_by_default(conn, clock):
    """Nothing changes unless it is asked for."""
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CAMPAIGN: 2}, clock=clock)
    svc = delivery_service(conn, None, ins, clock)
    assert [x.outcome for x in svc.drain("instantly")] == ["deferred"]
    assert ins.leads == {}
    assert sql1(conn, "SELECT last_error FROM delivery_outbox WHERE channel='instantly'") == "campaign_status_2"


def test_when_allowed_the_lead_is_created_into_the_paused_campaign(conn, clock):
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CAMPAIGN: 2}, clock=clock)
    svc = delivery_service(conn, None, ins, clock, env=ALLOW)
    assert [x.outcome for x in svc.drain("instantly")] == ["delivered"]
    assert len(ins.leads) == 1
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE channel='instantly'") == "delivered"
    # A genuine creation, with a receipt, which is what an Airtable row waits on.
    assert sql1(conn, "SELECT count(*) FROM delivery_receipts WHERE receipt_kind='created'") == 1


def test_it_is_exactly_ONE_create_call_and_no_duplicate_on_a_second_drain(conn, clock):
    """The recovery must not enrol anybody twice."""
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CAMPAIGN: 2}, clock=clock)
    svc = delivery_service(conn, None, ins, clock, env=ALLOW)
    assert [x.outcome for x in svc.drain("instantly")] == ["delivered"]
    assert svc.drain("instantly") == []
    assert len([r for r in ins.requests if r["method"] == "POST"]) == 1
    assert len(ins.leads) == 1


def test_a_lead_already_in_the_paused_campaign_is_reconciled_not_recreated(conn, clock):
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CAMPAIGN: 2}, clock=clock)
    ins.leads["good.buyer@acme.com"] = {"id": "pre", "email": "good.buyer@acme.com",
                                        "campaign": CAMPAIGN,
                                        "timestamp_created": "2026-01-01T00:00:00+00:00"}
    svc = delivery_service(conn, None, ins, clock, env=ALLOW)
    assert [x.outcome for x in svc.drain("instantly")] == ["delivered"]
    assert len(ins.leads) == 1, "the existing lead was not duplicated"


def test_the_airtable_row_follows_the_instantly_creation(conn, clock):
    """The invariant: a CRM row only after a genuine Instantly creation. The 25
    Airtable rows were waiting on exactly this."""
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CAMPAIGN: 2}, clock=clock)
    at = FakeAirtable()
    svc = delivery_service(conn, at, ins, clock, env=ALLOW)
    assert [x.outcome for x in svc.drain("airtable")] == ["deferred"]
    assert at.records == {}, "no CRM row before the lead exists"
    assert [x.outcome for x in svc.drain("instantly")] == ["delivered"]
    # The Airtable row carries its own deferral, so it does NOT drain in the same
    # pass as the Instantly creation -- it waits for its backoff. That is why the
    # real recovery needs a second drain later, not one sweep.
    assert svc.drain("airtable") == []
    assert at.records == {}
    clock.advance(hours=1)
    assert [x.outcome for x in svc.drain("airtable")] == ["delivered"]
    assert len(at.records) == 1


@pytest.mark.parametrize("status", [0, 3, 4, -1, 99])
def test_only_PAUSED_is_opened_up_and_never_completed(conn, clock, status):
    """COMPLETED is the dangerous one: adding leads to a drained campaign flips it
    ACTIVE and it starts sending. The allowance must not touch it."""
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CAMPAIGN: status}, clock=clock)
    svc = delivery_service(conn, None, ins, clock, env=ALLOW)
    assert [x.outcome for x in svc.drain("instantly")] == ["deferred"]
    assert ins.leads == {}


def test_the_flag_only_counts_as_set_when_it_is_exactly_one(conn, clock):
    for raw in ("", "0", "true", "yes", "2"):
        assert [x.outcome for x in delivery_service(
            conn, None, FakeInstantly(campaign_status={CAMPAIGN: 2}, clock=clock), clock,
            env={"TGTC_DELIVER_INTO_PAUSED_CAMPAIGNS": raw}).drain("instantly")] in ([], ["deferred"])
