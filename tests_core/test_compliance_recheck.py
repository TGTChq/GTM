"""Re-deciding an unknown jurisdiction from the person's own stored evidence.

Measured, production 2026-09-20: 100 approved contacts blocked
`compliance:unknown_jurisdiction:absent`; 58 of them carried "United States" in
their OWN stored Apollo evidence, because `people.contact_country` arrived with
migration 011 unbackfilled and the reuse path never recomputed it.

The recheck re-runs the FULL compliance evaluation with the resolved country.
It never unblocks on the country alone, never reads the employer's location,
and hands a recovered row back to `pending` -- where delivery's own precheck
re-runs suppression, the retired-campaign guard, the canary cap and posting
validity before anything is sent.
"""
from __future__ import annotations

import json

from tests_core.helpers import opportunity_service, sql1
from tests_core.seed import apollo_for, seed_opportunity
from tgtc_core.services.compliance_recheck import UNKNOWN_JURISDICTION, recheck_unknown_jurisdiction


def _blocked_unknown(conn, clock, *, stored_country=None, stored_state=None):
    """An approved lead whose contact country was never stored."""
    pid, eid, oid = seed_opportunity(conn, clock)
    out = opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    assert out.outcome == "approved"
    aid = out.approval_id
    enriched = {}
    if stored_country is not None:
        enriched["country"] = stored_country
    if stored_state is not None:
        enriched["state"] = stored_state
    with conn.cursor() as cur:
        cur.execute("SELECT person_id FROM approvals WHERE id = %s", (aid,))
        person_id = cur.fetchone()["person_id"]
        cur.execute("UPDATE people SET contact_country = NULL, facts_json = %s WHERE id = %s",
                    (json.dumps({"enriched": enriched}), person_id))
        cur.execute("UPDATE approvals SET contact_country = NULL, outreach_eligible = false, "
                    "outreach_block_reason = %s WHERE id = %s", (UNKNOWN_JURISDICTION, aid))
        cur.execute("UPDATE delivery_outbox SET state = 'blocked', blocked_reason = %s WHERE approval_id = %s",
                    (UNKNOWN_JURISDICTION, aid))
    conn.commit()
    return aid, person_id


def test_a_us_person_is_recovered_and_handed_back_to_delivery(conn, clock):
    aid, person_id = _blocked_unknown(conn, clock, stored_country="United States")
    counts = recheck_unknown_jurisdiction(conn, now=clock())
    assert counts["recovered_eligible"] == 1
    assert sql1(conn, "SELECT outreach_eligible FROM approvals WHERE id = %s", (aid,)) is True
    assert sql1(conn, "SELECT contact_country FROM approvals WHERE id = %s", (aid,)) == "US"
    states = {r for r in [sql1(conn, "SELECT count(*) FROM delivery_outbox WHERE approval_id = %s AND state = 'pending'", (aid,))]}
    assert states == {2}
    assert sql1(conn, "SELECT contact_country FROM people WHERE id = %s", (person_id,)) == "US"
    assert sql1(conn, "SELECT facts_json->>'contact_country_provenance' FROM people WHERE id = %s",
                (person_id,)) == "stored:person.country"


def test_a_us_state_alone_on_the_persons_record_is_recovered(conn, clock):
    aid, _ = _blocked_unknown(conn, clock, stored_state="Texas")
    assert recheck_unknown_jurisdiction(conn, now=clock())["recovered_eligible"] == 1
    assert sql1(conn, "SELECT contact_country FROM approvals WHERE id = %s", (aid,)) == "US"


def test_a_non_matrix_country_stays_blocked(conn, clock):
    aid, _ = _blocked_unknown(conn, clock, stored_country="India")
    counts = recheck_unknown_jurisdiction(conn, now=clock())
    assert counts["still_unknown"] == 1
    assert sql1(conn, "SELECT count(*) FROM delivery_outbox WHERE approval_id = %s AND state = 'blocked'", (aid,)) == 2
    assert sql1(conn, "SELECT outreach_eligible FROM approvals WHERE id = %s", (aid,)) is False


def test_a_resolved_but_non_sendable_country_is_reclassified_not_unblocked(conn, clock):
    """Germany resolves, and Germany may not be cold-emailed: the row stays
    blocked, now under the correct named reason instead of 'unknown'."""
    aid, _ = _blocked_unknown(conn, clock, stored_country="Germany")
    counts = recheck_unknown_jurisdiction(conn, now=clock())
    assert counts["reclassified_blocked"] == 1
    reason = sql1(conn, "SELECT blocked_reason FROM delivery_outbox WHERE approval_id = %s LIMIT 1", (aid,))
    assert reason != UNKNOWN_JURISDICTION and "DE" in reason
    assert sql1(conn, "SELECT count(*) FROM delivery_outbox WHERE approval_id = %s AND state = 'pending'", (aid,)) == 0


def test_no_stored_evidence_stays_blocked(conn, clock):
    aid, _ = _blocked_unknown(conn, clock)
    assert recheck_unknown_jurisdiction(conn, now=clock())["still_unknown"] == 1
    assert sql1(conn, "SELECT outreach_eligible FROM approvals WHERE id = %s", (aid,)) is False


def test_rows_blocked_for_any_other_reason_are_never_touched(conn, clock):
    aid, _ = _blocked_unknown(conn, clock, stored_country="United States")
    with conn.cursor() as cur:
        cur.execute("UPDATE delivery_outbox SET blocked_reason = 'retired_control_campaign' WHERE approval_id = %s", (aid,))
    conn.commit()
    counts = recheck_unknown_jurisdiction(conn, now=clock())
    assert counts["recovered_eligible"] == 0
    assert sql1(conn, "SELECT count(*) FROM delivery_outbox WHERE approval_id = %s AND state = 'blocked'", (aid,)) == 2


def test_the_recheck_is_idempotent(conn, clock):
    _blocked_unknown(conn, clock, stored_country="United States")
    assert recheck_unknown_jurisdiction(conn, now=clock())["recovered_eligible"] == 1
    assert recheck_unknown_jurisdiction(conn, now=clock())["recovered_eligible"] == 0
