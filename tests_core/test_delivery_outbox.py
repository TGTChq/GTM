"""Outbox: atomic with approval, idempotent consumers, reconciliation after a lost response,
truthful Instantly membership. SIMULATED Airtable/Instantly."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgtc_core.providers.instantly import InstantlyResult
from tgtc_core.policy.campaigns import POLICY_VERSION
from tgtc_core.services import opportunity as opp_mod
from tgtc_core.services.delivery import DeliveryService, OutboxItem
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, seed_opportunity

CS_CAMPAIGN = CONTROL_ID_BY_CAMPAIGN_KEY["customer_experience"]


@pytest.mark.parametrize("result, outcome, reason", [
    *[(InstantlyResult(False, status), "failed", "campaign_lookup_failed")
      for status in (None, 401, 403, 404, 429, 500, 503)],
    *[(InstantlyResult(True, 200, data), "failed", "campaign_response_invalid")
      for data in ({}, {"id": CS_CAMPAIGN}, {"id": CS_CAMPAIGN, "status": None},
                   {"id": CS_CAMPAIGN, "status": True}, {"id": CS_CAMPAIGN, "status": "1"},
                   {"id": "wrong-campaign", "status": 1}, {"status": 1})],
    *[(InstantlyResult(True, 200, {"id": CS_CAMPAIGN, "status": status}), "deferred", f"campaign_not_active:{status}")
      for status in (0, 2, 3, 4, -1, 99)],
])
def test_campaign_preflight_fails_closed_without_database_or_send(monkeypatch, clock, result, outcome, reason):
    """Portable safety regression: no membership/write call after an unverified campaign."""
    def forbidden(*args, **kwargs):
        pytest.fail("unverified campaign reached outbound delivery")

    client = SimpleNamespace(get_campaign=lambda target: result, create_lead=forbidden, resolve_membership=forbidden)
    svc = DeliveryService(None, airtable=None, instantly=client, now=clock)
    changes = []
    monkeypatch.setattr(svc, "_precheck", lambda item: None)
    monkeypatch.setattr(svc, "_set", lambda item, state, **kw: changes.append((state, kw)))
    monkeypatch.setattr(svc, "_receipt", forbidden)
    item = OutboxItem(1, 1, "instantly", "key", {"campaign": CS_CAMPAIGN, "email": "buyer@example.com"},
                      "claimed", 1, "token", "pending")
    out = svc.process_instantly(item)
    assert (out.outcome, out.reason) == (outcome, reason)
    assert len(changes) == 1 and changes[0][0] == ("pending" if outcome == "deferred" else "failed")
    assert changes[0][1]["available_at"] > clock()


def test_campaign_lookup_failure_recovers_once_without_duplicate_enrollment(conn, clock, monkeypatch):
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CS_CAMPAIGN: 1}, clock=clock)
    svc = delivery_service(conn, None, ins, clock)
    original = svc.instantly.get_campaign
    monkeypatch.setattr(svc.instantly, "get_campaign", lambda target: InstantlyResult(False, 503))
    assert [x.outcome for x in svc.drain("instantly")] == ["failed"]
    assert ins.leads == {}
    assert sql1(conn, "SELECT count(*) FROM delivery_receipts") == 0
    assert sql1(conn, "SELECT state FROM approvals") == "approved"
    monkeypatch.setattr(svc.instantly, "get_campaign", original)
    clock.advance(hours=1)
    assert [x.outcome for x in svc.drain("instantly")] == ["delivered"]
    assert svc.drain("instantly") == []
    assert len(ins.leads) == 1
    assert len([r for r in ins.requests if r["method"] == "POST"]) == 1


def test_preexisting_target_lead_is_reported_existing_without_duplicate(conn, clock):
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CS_CAMPAIGN: 1}, clock=clock)
    ins.leads["good.buyer@acme.com"] = {"id": "pre", "email": "good.buyer@acme.com", "campaign": CS_CAMPAIGN,
                                        "timestamp_created": "2026-01-01T00:00:00+00:00"}
    svc = delivery_service(conn, None, ins, clock)
    assert [x.outcome for x in svc.drain("instantly")] == ["delivered"]
    assert svc.drain("instantly") == []
    assert len(ins.leads) == 1 and ins.leads["good.buyer@acme.com"]["id"] == "pre"
    assert sql1(conn, "SELECT receipt_kind FROM delivery_receipts WHERE channel = 'instantly' ORDER BY id DESC LIMIT 1") == "existing"
    assert sql1(conn, "SELECT count(*) FROM delivery_receipts WHERE receipt_kind = 'created'") == 0


def _approve(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock)
    out = opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    assert out.outcome == "approved"
    return out.approval_id


def test_approval_and_both_outbox_items_are_one_transaction(conn, clock, monkeypatch):
    pid, eid, oid = seed_opportunity(conn, clock)
    monkeypatch.setattr(opp_mod, "instantly_payload", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom after approvals insert")))
    with pytest.raises(RuntimeError):
        opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    conn.rollback()
    assert sql1(conn, "SELECT count(*) FROM approvals") == 0
    assert sql1(conn, "SELECT count(*) FROM delivery_outbox") == 0
    assert sql1(conn, "SELECT state FROM opportunities WHERE id = %s", (oid,)) == "open"


def test_happy_path_writes_created_receipts_and_marks_the_approval_delivered(conn, clock):
    approval_id = _approve(conn, clock)
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    svc = delivery_service(conn, at, ins, clock)
    a = svc.drain("airtable")
    i = svc.drain("instantly")
    assert [x.outcome for x in a] == ["delivered"] and [x.outcome for x in i] == ["delivered"]
    assert len(at.records) == 1 and len(ins.leads) == 1
    rec = list(at.records.values())[0]["fields"]
    assert rec["Status"] == "Approved" and rec["Validation Version"] == POLICY_VERSION
    lead = list(ins.leads.values())[0]
    assert lead["campaign"] == CS_CAMPAIGN and lead["custom_variables"]["role_bucket"] == "customer_success"
    kinds = [r["receipt_kind"] for r in sqlall(conn, "SELECT receipt_kind FROM delivery_receipts ORDER BY id")]
    assert kinds == ["attempted", "created", "attempted", "created"]
    assert sql1(conn, "SELECT state FROM approvals WHERE id = %s", (approval_id,)) == "delivered"
    # a second drain sends nothing
    assert svc.drain("airtable") == [] and svc.drain("instantly") == []
    assert len(at.requests) == 1 and len([r for r in ins.requests if r["path"].endswith("/leads")]) == 1


def test_lost_airtable_response_is_reconciled_by_lead_key_without_a_second_row(conn, clock):
    _approve(conn, clock)
    at = FakeAirtable(lose_response_once=True)
    svc = delivery_service(conn, at, None, clock)
    first = svc.drain("airtable")
    assert [x.outcome for x in first] == ["uncertain"]
    assert len(at.records) == 1                                   # the provider DID create the row
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE channel = 'airtable'") == "in_flight"
    # restart: lease expires, next claim reconciles by Lead Key and sends nothing
    clock.advance(seconds=301)
    second = svc.drain("airtable")
    assert [x.outcome for x in second] == ["reconciled"]
    assert len(at.records) == 1
    kinds = [r["receipt_kind"] for r in sqlall(conn, "SELECT receipt_kind FROM delivery_receipts WHERE channel = 'airtable' ORDER BY id")]
    assert kinds == ["attempted", "reconciled"]
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE channel = 'airtable'") == "delivered"
    posts = [r for r in at.requests if r["method"] == "POST"]
    assert len(posts) == 1


def test_lost_instantly_response_is_reconciled_by_membership_without_a_second_enrollment(conn, clock):
    _approve(conn, clock)
    ins = FakeInstantly(lose_response_once=True, campaign_status={CS_CAMPAIGN: 1}, clock=clock)
    svc = delivery_service(conn, None, ins, clock)
    assert [x.outcome for x in svc.drain("instantly")] == ["uncertain"]
    assert len(ins.leads) == 1
    clock.advance(seconds=301)
    assert [x.outcome for x in svc.drain("instantly")] == ["reconciled"]
    assert len(ins.leads) == 1
    assert len([r for r in ins.requests if r["path"].endswith("/leads") and r["method"] == "POST"]) == 1


def test_email_already_in_another_campaign_is_not_reported_as_delivered(conn, clock):
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CS_CAMPAIGN: 1}, clock=clock)
    other = "1db88bbe-b2cf-4574-a5b7-1cb948151a86"
    ins.leads["good.buyer@acme.com"] = {"id": "pre", "email": "good.buyer@acme.com", "campaign": other,
                                        "timestamp_created": "2026-01-01T00:00:00+00:00"}
    out = delivery_service(conn, None, ins, clock).drain("instantly")
    assert [x.outcome for x in out] == ["blocked"] and out[0].reason == "instantly_existing_other_campaign"
    assert sql1(conn, "SELECT blocked_reason FROM delivery_outbox WHERE channel = 'instantly'") == "not_delivered:instantly_existing_other_campaign"
    assert sql1(conn, "SELECT receipt_kind FROM delivery_receipts WHERE channel = 'instantly' ORDER BY id DESC LIMIT 1") == "rejected"
    assert sql1(conn, "SELECT state FROM approvals") == "approved"    # never marked delivered


def test_paused_campaign_defers_the_lead_and_does_not_invalidate_it(conn, clock):
    _approve(conn, clock)
    ins = FakeInstantly(campaign_status={CS_CAMPAIGN: 2}, clock=clock)
    svc = delivery_service(conn, None, ins, clock)
    out = svc.drain("instantly")
    assert [x.outcome for x in out] == ["deferred"]
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE channel = 'instantly'") == "pending"
    ins.campaign_status[CS_CAMPAIGN] = 1
    clock.advance(hours=1, seconds=1)
    assert [x.outcome for x in svc.drain("instantly")] == ["delivered"]


def test_airtable_422_is_blocked_with_the_field_error_not_retried_forever(conn, clock):
    _approve(conn, clock)
    at = FakeAirtable(fail_next_status=422)
    out = delivery_service(conn, at, None, clock).drain("airtable")
    assert [x.outcome for x in out] == ["blocked"] and out[0].reason.startswith("airtable_422")
    assert len([r for r in at.requests if r["method"] == "POST"]) == 1


def test_ambiguous_5xx_on_create_is_uncertain_and_reconciled_without_a_second_row(conn, clock):
    """R05: a 503 after the request was sent may have created the row. The client must not
    retry blindly; the outbox reconciles by Lead Key and sends nothing a second time."""
    _approve(conn, clock)
    at = FakeAirtable(fail_next_status=503)          # rejected BEFORE creating anything
    svc = delivery_service(conn, at, None, clock)
    assert [x.outcome for x in svc.drain("airtable")] == ["uncertain"]
    assert len(at.records) == 0 and len([r for r in at.requests if r["method"] == "POST"]) == 1
    clock.advance(seconds=301)
    # reconciliation finds nothing -> ONE more create, which succeeds
    assert [x.outcome for x in svc.drain("airtable")] == ["delivered"]
    assert len(at.records) == 1 and len([r for r in at.requests if r["method"] == "POST"]) == 2
