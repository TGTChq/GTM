"""R05 -- acceptance followed by timeout, connection reset or ambiguous 5xx must never cause
a blind second POST. Assert the actual external rows and identities."""

from __future__ import annotations

import pytest

from tgtc_core.providers.airtable import AirtableClient
from tgtc_core.providers.instantly import InstantlyClient
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, seed_opportunity

CS = CONTROL_ID_BY_CAMPAIGN_KEY["customer_experience"]


def _approve(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock)
    assert opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid).outcome == "approved"


@pytest.mark.parametrize("mode", ["reset", 500, 502])
def test_client_does_not_retry_an_ambiguous_create(mode):
    at = FakeAirtable(fail_after_create_once=mode)
    client = AirtableClient(at, base_url="https://api.airtable.com/v0", token="t", base_id="app", table="Leads", sleep=lambda s: None)
    result = client.create_records([{"Lead Key": "k1", "Status": "Approved"}])
    assert result.ok is False and result.uncertain is True
    assert len([r for r in at.requests if r["method"] == "POST"]) == 1      # exactly one POST
    assert len(at.records) == 1                                              # the server did create it


@pytest.mark.parametrize("mode", ["reset", 500, "timeout"])
def test_outbox_reconciles_an_accepted_create_after_reset_5xx_or_timeout(conn, clock, mode):
    _approve(conn, clock)
    at = FakeAirtable(lose_response_once=(mode == "timeout"), fail_after_create_once=None if mode == "timeout" else mode)
    svc = delivery_service(conn, at, None, clock)
    first = svc.drain("airtable")
    assert [x.outcome for x in first] == ["uncertain"], first
    assert len(at.records) == 1
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE channel = 'airtable'") == "in_flight"
    clock.advance(seconds=301)
    second = svc.drain("airtable")
    assert [x.outcome for x in second] == ["reconciled"]
    assert len(at.records) == 1                                              # still ONE external row
    assert len([r for r in at.requests if r["method"] == "POST"]) == 1
    rec = list(at.records.values())[0]["fields"]
    assert rec["Lead Key"] == "acme.com|good.buyer@acme.com|customer_success"
    assert sql1(conn, "SELECT external_id FROM delivery_receipts WHERE receipt_kind = 'reconciled'") == list(at.records)[0]
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE channel = 'airtable'") == "delivered"


def test_instantly_reset_after_create_is_reconciled_without_a_second_enrollment(conn, clock):
    _approve(conn, clock)
    ins = FakeInstantly(fail_after_create_once="reset", campaign_status={CS: 1}, clock=clock)
    svc = delivery_service(conn, None, ins, clock)
    assert [x.outcome for x in svc.drain("instantly")] == ["uncertain"]
    assert len(ins.leads) == 1
    clock.advance(seconds=301)
    assert [x.outcome for x in svc.drain("instantly")] == ["reconciled"]
    assert len(ins.leads) == 1
    assert len([r for r in ins.requests if r["path"].endswith("/leads") and r["method"] == "POST"]) == 1


def test_instantly_client_marks_post_connection_loss_uncertain():
    ins = FakeInstantly(fail_after_create_once="reset")
    client = InstantlyClient(ins, base_url="https://api.instantly.ai/api/v2", api_key="k")
    res = client.create_lead({"campaign": CS, "email": "x@y.com", "skip_if_in_campaign": True})
    assert res.ok is False and res.uncertain is True and len(ins.leads) == 1
