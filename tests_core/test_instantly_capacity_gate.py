"""A full destination must stop the spending, not the contacts.

2026-09-24, production: Instantly answered
``403 {"statusCode":403,"error":"Forbidden","message":"Lead limit reached. Remaining uploads: 0"}``
to all 107 remaining deliveries while the run had 100 Fantastic credits and 61 Apollo
credits left and kept buying. Nothing upstream knew the destination was full.

What these tests hold:

* the money stops BEFORE it is spent -- no Fantastic purchase, no Apollo enrichment,
  while the destination is on record as full;
* finding out is free and happens once per interval, never once per pending row;
* a pending contact survives it: still pending, never failed into the attempt count,
  never blocked, never counted as created;
* a 403 that is NOT about capacity keeps its old behaviour, because silently stopping
  acquisition over a bad key would hide a different failure.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tgtc_core.services import instantly_capacity
from tgtc_core.services.instantly_capacity import DEFERRED_REASON, STOP_REASON
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tests_core.helpers import delivery_service, opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, good_buyer, seed_opportunity

FULL = (403, {"statusCode": 403, "error": "Forbidden", "message": "Lead limit reached. Remaining uploads: 0"})
BAD_KEY = (403, {"statusCode": 403, "error": "Forbidden", "message": "Invalid API key"})


def approve(conn, clock, *, n=1):
    ids = []
    for i in range(n):
        domain = f"acme{i}.com"
        pid, eid, oid = seed_opportunity(conn, clock, domain=domain, org_name=f"Acme {i}", job_id=f"job-{i}",
                                         function_key="customer_success")
        # A distinct buyer per company: the seed's default person id is shared, and a
        # person already judged elsewhere is not judged again.
        fake = apollo_for(domain, f"Acme {i}", people=[good_buyer(domain, f"Acme {i}", id=f"p-good-{i}")])
        out = opportunity_service(conn, fake, clock).process(oid)
        assert out.outcome == "approved", out.reason
        ids.append(out.approval_id)
    return ids


def outbox(conn, channel="instantly"):
    return sqlall(conn, "SELECT approval_id, state, attempts, coalesce(blocked_reason,'') reason, "
                        "coalesce(last_error,'') note FROM delivery_outbox WHERE channel = %s ORDER BY id", (channel,))


def creations(conn):
    return int(sql1(conn, "SELECT count(*) FROM delivery_receipts WHERE channel = 'instantly' "
                          "AND receipt_kind = 'created'"))


def posts(fake):
    return [r for r in fake.requests if r["method"] == "POST" and r["path"].endswith("/leads")]


def release(conn, clock):
    conn.execute("UPDATE delivery_outbox SET available_at = %s WHERE state IN ('pending', 'failed')",
                 (clock() - timedelta(minutes=1),))
    conn.commit()


# --- the refusal itself -----------------------------------------------------------------
def test_only_the_lead_limit_403_means_the_destination_is_full():
    assert instantly_capacity.is_capacity_refusal(403, '{"message":"Lead limit reached. Remaining uploads: 0"}')
    assert instantly_capacity.remaining_uploads("Lead limit reached. Remaining uploads: 0") == 0
    # ... and everything else is a different problem.
    assert not instantly_capacity.is_capacity_refusal(403, "Invalid API key")
    assert not instantly_capacity.is_capacity_refusal(403, "<html>Cloudflare</html>")
    assert not instantly_capacity.is_capacity_refusal(401, "Lead limit reached")
    assert not instantly_capacity.is_capacity_refusal(500, "server error")


def test_a_full_destination_defers_the_contact_and_never_damages_it(conn, clock):
    approve(conn, clock)
    fake = FakeInstantly(refuse_leads_with=FULL, clock=clock)
    out = delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=5)

    assert [o.outcome for o in out] == ["deferred"] and out[0].reason == DEFERRED_REASON
    row = outbox(conn)[0]
    assert row["state"] == "pending", "a full workspace is not the contact's fault"
    assert row["reason"] == "" and creations(conn) == 0
    assert instantly_capacity.state(conn)["blocked"] is True
    assert instantly_capacity.state(conn)["remaining_uploads"] == 0


def test_one_refusal_stops_the_sweep_instead_of_a_403_per_row(conn, clock):
    approve(conn, clock, n=6)
    fake = FakeInstantly(refuse_leads_with=FULL, clock=clock)
    delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)

    assert len(posts(fake)) == 1, "the wall answers the same to everybody: ask once"
    untouched = [r for r in outbox(conn) if r["attempts"] == 0]
    assert len(untouched) == 5, "the other rows were not even claimed"


def test_a_restarted_process_does_not_ask_again_before_the_interval(conn, clock):
    approve(conn, clock, n=3)
    fake = FakeInstantly(refuse_leads_with=FULL, clock=clock)
    delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)
    fake.requests.clear()
    release(conn, clock)

    # A new service instance, as after a restart: the block lives in the database.
    delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)
    assert posts(fake) == []
    assert all(r["state"] == "pending" for r in outbox(conn))


def test_the_probe_is_one_free_retry_per_interval(conn, clock):
    approve(conn, clock, n=3)
    fake = FakeInstantly(refuse_leads_with=FULL, clock=clock)
    delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)
    assert len(posts(fake)) == 1

    clock.advance(hours=1.5)
    release(conn, clock)
    delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)
    assert len(posts(fake)) == 2, "one probe, and only after the interval"


def test_a_persistent_wall_never_turns_a_pending_contact_into_created_or_blocked(conn, clock):
    approve(conn, clock, n=3)
    fake = FakeInstantly(refuse_leads_with=FULL, clock=clock)
    for _ in range(10):
        release(conn, clock)
        delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)
        clock.advance(hours=24)

    rows = outbox(conn)
    assert {r["state"] for r in rows} == {"pending"}
    assert all(r["reason"] == "" for r in rows), "never blocked as max_attempts"
    assert creations(conn) == 0
    assert len(posts(fake)) == 10, "one probe a day, not 3 x 10"


def test_room_again_drains_the_waiting_contacts_and_lifts_the_block(conn, clock):
    approve(conn, clock, n=3)
    fake = FakeInstantly(refuse_leads_with=FULL, clock=clock)
    delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)
    assert instantly_capacity.state(conn)["blocked"] is True

    fake.refuse_leads_with = None
    clock.advance(hours=2)
    release(conn, clock)
    out = delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)

    assert [o.outcome for o in out] == ["delivered"] * 3
    assert creations(conn) == 3
    assert instantly_capacity.state(conn)["blocked"] is False


def test_a_403_that_is_not_about_capacity_keeps_its_own_behaviour(conn, clock):
    approve(conn, clock, n=2)
    fake = FakeInstantly(refuse_leads_with=BAD_KEY, clock=clock)
    out = delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)

    assert [o.outcome for o in out] == ["failed", "failed"], "a bad key is not a full workspace"
    assert instantly_capacity.state(conn)["blocked"] is False, "and must not stop acquisition silently"


def test_the_block_is_alerted_exactly_once_per_episode(conn, clock):
    approve(conn, clock, n=2)
    fake = FakeInstantly(refuse_leads_with=FULL, clock=clock)
    delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)

    assert instantly_capacity.alert_once(conn) is True
    assert instantly_capacity.alert_once(conn) is False
    clock.advance(hours=2)
    release(conn, clock)
    delivery_service(conn, FakeAirtable(), fake, clock).drain("instantly", max_items=50)
    assert instantly_capacity.alert_once(conn) is False, "still the same block"

    # A new episode -- room, then full again -- alerts again.
    instantly_capacity.record_available(conn, now=clock.advance(hours=1))
    instantly_capacity.record_refusal(conn, message=FULL[1]["message"], now=clock.advance(hours=1))
    assert instantly_capacity.alert_once(conn) is True


# --- the money ---------------------------------------------------------------------------
def test_the_daily_run_buys_nothing_while_the_destination_is_full(monkeypatch):
    """The whole point: no Fantastic record, no Apollo credit, no new work."""
    from tests_core.test_daily_controller import World, controller, purchases

    w = World()
    w.instantly_full = True
    c = controller(w, monkeypatch)
    rep = c.run()

    assert rep.acquisition_stop == STOP_REASON
    assert purchases(w) == [], "nothing bought behind a full destination"
    assert w.apollo == 0 and w.records == 0
    assert rep.capacity_block.get("blocked") is True


def test_when_room_returns_the_waiting_contacts_go_first(monkeypatch):
    from tests_core.test_daily_controller import World, controller

    w = World(backlog=107)          # exactly the 107 that were waiting on 2026-09-24
    w.instantly_full = True
    w.room_returns_on_probe = True  # the free probe finds room again
    rep = controller(w, monkeypatch).run()

    assert rep.acquisition_stop != STOP_REASON
    assert w.calls[0] == ("deliver",), "the probe is a delivery, before anything is bought"
    assert rep.backlog_created == 107, "the ones already paid for were delivered first"
    first_buy = next((i for i, x in enumerate(w.calls) if x[0] == "acquire"), None)
    assert first_buy is None or w.backlog_pending == 0
