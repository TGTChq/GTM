"""A departure leaves a vacancy. Filling it is a search, with every gate, or nothing.

Production on 2026-09-24 had 8 of these queued and nothing acted on them. What these
tests hold is the shape of acting on them safely: the unit goes back through the
ordinary contact path, the departed address cannot come back, a colleague named in the
reply is not promoted, and a unit whose posting is gone is not revived.
"""

from __future__ import annotations

import pytest

from tgtc_core.services import replacements
from tgtc_core.services.replacements import NO_CURRENT_VACANCY, REQUALIFYING, WORK_KIND
from tests_core.helpers import sql1, sqlall
from tests_core.seed import apollo_for, good_buyer, seed_opportunity
from tests_core.helpers import opportunity_service


def approve_one(conn, clock, *, i=0):
    domain = f"acme{i}.com"
    pid, eid, oid = seed_opportunity(conn, clock, domain=domain, org_name=f"Acme {i}", job_id=f"job-{i}",
                                     function_key="customer_success")
    fake = apollo_for(domain, f"Acme {i}", people=[good_buyer(domain, f"Acme {i}", id=f"p-good-{i}")])
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "approved", out.reason
    return oid, out.approval_id


def queue_departure(conn, opportunity_id, clock):
    conn.execute("INSERT INTO work_items (kind, subject_kind, subject_id, state, available_at, waiting_on) "
                 "VALUES (%s, 'opportunity', %s, 'ready', %s, 'contact_departed')",
                 (WORK_KIND, opportunity_id, clock()))
    conn.commit()


def items(conn, kind):
    return sqlall(conn, "SELECT state, coalesce(close_reason,'') reason FROM work_items WHERE kind = %s", (kind,))


def opportunity(conn, oid):
    return sqlall(conn, "SELECT state, coalesce(close_reason,'') reason, approved_person_id FROM opportunities "
                        "WHERE id = %s", (oid,))[0]


def test_a_departure_puts_the_unit_back_through_the_ordinary_contact_path(conn, clock):
    oid, _ = approve_one(conn, clock)
    queue_departure(conn, oid, clock)

    out = replacements.reopen_departed_units(conn, now=clock())

    assert out == {"considered": 1, "requalifying": 1, "no_current_vacancy": 0}
    opp = opportunity(conn, oid)
    assert opp["state"] == "open" and opp["approved_person_id"] is None, "the unit has no contact again"
    assert items(conn, WORK_KIND) == [{"state": "done", "reason": REQUALIFYING}]
    # ... and it is ordinary qualification work, not a special path
    qualify = sqlall(conn, "SELECT state FROM work_items WHERE kind = 'qualify_opportunity' AND subject_id = %s", (oid,))
    assert qualify and qualify[0]["state"] == "ready"


def test_a_unit_whose_vacancy_is_gone_is_not_revived(conn, clock):
    """Re-opening it would invent commercial evidence that no longer exists."""
    oid, _ = approve_one(conn, clock)
    conn.execute("UPDATE opportunities SET state = 'closed', close_reason = 'postings_expired' WHERE id = %s", (oid,))
    conn.commit()
    queue_departure(conn, oid, clock)

    out = replacements.reopen_departed_units(conn, now=clock())

    assert out["no_current_vacancy"] == 1 and out["requalifying"] == 0
    assert opportunity(conn, oid)["state"] == "closed"
    assert items(conn, WORK_KIND)[0]["state"] == "done"
    assert items(conn, WORK_KIND)[0]["reason"].startswith(NO_CURRENT_VACANCY)
    # nothing re-opened the unit: no contact search will run for a vacancy that is gone
    assert sql1(conn, "SELECT reopened_at FROM opportunities WHERE id = %s", (oid,)) is None


def test_the_departed_address_cannot_come_back_through_the_replacement(conn, clock):
    """The suppression written when the reply arrived is what the contact path reads."""
    oid, approval_id = approve_one(conn, clock)
    email = sql1(conn, "SELECT p.email FROM approvals a JOIN people p ON p.id = a.person_id WHERE a.id = %s",
                 (approval_id,))
    conn.execute("INSERT INTO suppressions (kind, key, reason, source) VALUES ('person_email', %s, %s, %s)",
                 (str(email).lower(), "no_longer_here", "self:reply"))
    conn.commit()
    queue_departure(conn, oid, clock)

    replacements.reopen_departed_units(conn, now=clock())

    # The unit is open again, and the person who left is on record as unusable.
    assert opportunity(conn, oid)["state"] == "open"
    assert int(sql1(conn, "SELECT count(*) FROM suppressions WHERE kind='person_email' AND key=%s",
                    (str(email).lower(),))) == 1


def test_it_is_bounded_and_resumable(conn, clock):
    for i in range(5):
        oid, _ = approve_one(conn, clock, i=i)
        queue_departure(conn, oid, clock)

    first = replacements.reopen_departed_units(conn, now=clock(), limit=2)
    assert first["considered"] == 2 and first["requalifying"] == 2
    assert len([r for r in items(conn, WORK_KIND) if r["state"] == "ready"]) == 3

    second = replacements.reopen_departed_units(conn, now=clock(), limit=10)
    assert second["requalifying"] == 3, "the ones already done are not done twice"
    assert all(r["state"] == "done" for r in items(conn, WORK_KIND))


def test_zero_per_run_does_nothing_at_all(conn, clock):
    oid, _ = approve_one(conn, clock)
    queue_departure(conn, oid, clock)
    assert replacements.reopen_departed_units(conn, now=clock(), limit=0) == {
        "considered": 0, "requalifying": 0, "no_current_vacancy": 0}
    assert items(conn, WORK_KIND)[0]["state"] == "ready"
    assert opportunity(conn, oid)["state"] == "approved"


def test_the_per_run_ceiling_is_configurable_and_never_negative():
    assert replacements.per_run({}) == replacements.DEFAULT_PER_RUN
    assert replacements.per_run({"TGTC_REPLACEMENTS_PER_RUN": "5"}) == 5
    assert replacements.per_run({"TGTC_REPLACEMENTS_PER_RUN": "0"}) == 0
    assert replacements.per_run({"TGTC_REPLACEMENTS_PER_RUN": "-3"}) == 0
    assert replacements.per_run({"TGTC_REPLACEMENTS_PER_RUN": "nonsense"}) == replacements.DEFAULT_PER_RUN
