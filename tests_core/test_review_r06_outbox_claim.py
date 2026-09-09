"""R06 -- claiming an outbox item is exclusive: with two PostgreSQL connections the second
worker gets nothing under an active lease; a stale worker's state changes and receipts
are fenced after lease loss."""

from __future__ import annotations

from tgtc_core.testing.fakes import FakeAirtable
from tests_core.helpers import delivery_service, opportunity_service, sql1
from tests_core.seed import apollo_for, seed_opportunity


def _approve(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock)
    assert opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid).outcome == "approved"


def test_second_connection_cannot_claim_an_item_under_an_active_lease(conn, conn2, clock):
    _approve(conn, clock)
    a = delivery_service(conn, FakeAirtable(), None, clock)
    b = delivery_service(conn2, FakeAirtable(), None, clock)
    first = a.claim("airtable")
    assert len(first) == 1 and first[0].state == "claimed"
    assert b.claim("airtable") == []                                   # nothing while the lease is active
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE channel = 'airtable'") == "claimed"
    clock.advance(seconds=301)
    second = b.claim("airtable")
    assert len(second) == 1 and second[0].id == first[0].id and second[0].lease_token != first[0].lease_token


def test_stale_worker_cannot_record_a_delivery_after_lease_loss(conn, conn2, clock):
    _approve(conn, clock)
    at_a, at_b = FakeAirtable(), FakeAirtable()
    a = delivery_service(conn, at_a, None, clock)
    b = delivery_service(conn2, at_b, None, clock)
    item_a = a.claim("airtable")[0]
    clock.advance(seconds=301)                                          # A's lease expires while it is slow
    item_b = b.claim("airtable")[0]
    assert item_b.id == item_a.id
    # A wakes up and tries to finish: every fenced write is rejected
    assert a._receipt(item_a, "created", external_id="recSTALE") is False
    assert a._set(item_a, "delivered") is False
    assert sql1(conn, "SELECT count(*) FROM delivery_receipts WHERE external_id = 'recSTALE'") == 0
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE id = %s", (item_a.id,)) == "claimed"
    # B delivers normally
    out = b.process(item_b)
    assert out.outcome == "delivered" and len(at_b.records) == 1 and len(at_a.records) == 0
    assert sql1(conn, "SELECT count(*) FROM delivery_receipts WHERE receipt_kind = 'created'") == 1


def test_stale_worker_processing_after_lease_loss_reports_lease_lost_and_sends_nothing_twice(conn, conn2, clock):
    """A processes past its lease; it must not double-create and must report the loss."""
    _approve(conn, clock)
    at = FakeAirtable()
    a = delivery_service(conn, at, None, clock)
    b = delivery_service(conn2, at, None, clock)
    item_a = a.claim("airtable")[0]
    clock.advance(seconds=301)
    item_b = b.claim("airtable")[0]
    assert b.process(item_b).outcome == "delivered"
    out_a = a.process(item_a)
    assert out_a.outcome == "lease_lost"
    assert len(at.records) == 1 and len([r for r in at.requests if r["method"] == "POST"]) == 1
