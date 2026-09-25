"""Rotation removes finished, silent contacts -- and nothing else, ever.

Written against what the first manual rotation taught on 2026-09-24: deleting a lead
does return a slot, the backup is the only copy of what went, and the guards are the
whole product. A rule that is not tested here is a rule that will be broken by a tired
person at 03:00.
"""

from __future__ import annotations

import pytest

from tgtc_core.services import instantly_rotation as rot
from tgtc_core.services.instantly_rotation import RotationRefused
from tests_core.helpers import sql1, sqlall

LIVE = "269cd138-00b1-48c3-9093-16c36120a20e"      # a Challenger campaign
CONTROL = "1747c87e-12e9-4477-bc4d-048223d39513"   # a Control campaign
OLD = "aaaaaaaa-0000-0000-0000-000000000001"       # a finished legacy campaign


class Resp:
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


def lead(i, *, status=3, reply=None, replies=0, email=None):
    return {"id": f"L{i}", "email": email or f"p{i}@example.invalid", "status": status,
            "timestamp_last_reply": reply, "email_reply_count": replies,
            "first_name": f"P{i}", "company_name": "Example Ltd"}


def world(leads_by_campaign, *, fail_after=None):
    deleted = []

    def leads_of(cid):
        return list(leads_by_campaign.get(cid, []))

    def delete(lead_id):
        if fail_after is not None and len(deleted) >= fail_after:
            return Resp(503, "unavailable")
        deleted.append(lead_id)
        return Resp(200)

    return leads_of, delete, deleted


def backup_rows(conn):
    return sqlall(conn, "SELECT lead_id, campaign_id, reason, delete_status, "
                        "(deleted_at IS NOT NULL) AS deleted, payload_sha256 FROM instantly_rotation_backup "
                        "ORDER BY lead_id")


def test_nothing_is_deleted_when_no_room_is_needed(conn):
    leads_of, delete, deleted = world({OLD: [lead(1)]})
    out = rot.rotate(conn, None, needed=0, batch=100,
                     campaigns=[{"id": OLD, "status": 3, "name": "Old"}], leads_of=leads_of, delete=delete)
    assert out["deleted"] == 0 and deleted == []
    assert backup_rows(conn) == []


def test_it_stops_the_moment_it_has_the_room_it_asked_for(conn):
    leads_of, delete, deleted = world({OLD: [lead(i) for i in range(10)]})
    out = rot.rotate(conn, None, needed=3, batch=100,
                     campaigns=[{"id": OLD, "status": 3, "name": "Old"}], leads_of=leads_of, delete=delete)
    assert out["deleted"] == 3 and len(deleted) == 3, "it kept deleting after it had enough"
    assert len(backup_rows(conn)) == 3


def test_the_batch_ceiling_binds_even_when_more_room_is_wanted(conn):
    leads_of, delete, deleted = world({OLD: [lead(i) for i in range(50)]})
    out = rot.rotate(conn, None, needed=40, batch=5,
                     campaigns=[{"id": OLD, "status": 3, "name": "Old"}], leads_of=leads_of, delete=delete)
    assert out["deleted"] == 5 and out["ceiling"] == 5


def test_a_live_campaign_is_refused_by_id_whatever_its_status_says(conn):
    """Even if Instantly reports a Challenger or Control campaign as completed."""
    leads_of, delete, deleted = world({LIVE: [lead(1)], CONTROL: [lead(2)]})
    out = rot.rotate(conn, None, needed=10, batch=10,
                     campaigns=[{"id": LIVE, "status": 3, "name": "CHALLENGER"},
                                {"id": CONTROL, "status": 3, "name": "CONTROL"}],
                     leads_of=leads_of, delete=delete)
    assert out["deleted"] == 0 and deleted == []
    assert backup_rows(conn) == []


def test_only_a_finished_and_silent_contact_goes(conn):
    leads_of, delete, deleted = world({OLD: [
        lead(1, status=1),                       # still in sequence
        lead(2, status=-1),                      # bounced
        lead(3, reply="2026-09-01T10:00:00Z"),   # replied
        lead(4, replies=2),                      # replied
        lead(5),                                 # the only one that may go
    ]})
    out = rot.rotate(conn, None, needed=10, batch=10,
                     campaigns=[{"id": OLD, "status": 3, "name": "Old"}], leads_of=leads_of, delete=delete)
    assert deleted == ["L5"]
    assert out["refused"]["sequence_not_finished:1"] == 1
    assert out["refused"]["sequence_not_finished:-1"] == 1
    assert out["refused"]["has_reply"] == 2


def test_an_address_we_suppressed_or_still_owe_delivery_is_never_removed(conn, clock):
    from tests_core.seed import apollo_for, good_buyer, seed_opportunity
    from tests_core.helpers import opportunity_service

    # one of ours, approved and still awaiting delivery
    pid, eid, oid = seed_opportunity(conn, clock, domain="acme.com", org_name="Acme", job_id="j1",
                                     function_key="customer_success")
    out_ = opportunity_service(conn, apollo_for("acme.com", "Acme",
                                                people=[good_buyer("acme.com", "Acme", id="p1")]), clock).process(oid)
    ours = str(sql1(conn, "SELECT p.email FROM approvals a JOIN people p ON p.id = a.person_id WHERE a.id = %s",
                    (out_.approval_id,))).lower()
    conn.execute("INSERT INTO suppressions (kind, key, reason, source) VALUES ('person_email', %s, %s, %s)",
                 ("gone@example.invalid", "opt_out", "self:reply"))
    conn.commit()

    leads_of, delete, deleted = world({OLD: [lead(1, email=ours), lead(2, email="gone@example.invalid"), lead(3)]})
    out = rot.rotate(conn, None, needed=10, batch=10,
                     campaigns=[{"id": OLD, "status": 3, "name": "Old"}], leads_of=leads_of, delete=delete)

    assert deleted == ["L3"]
    assert out["refused"]["ours_suppressed_or_awaiting_delivery"] == 2


def test_the_backup_is_written_before_the_delete_and_carries_a_checksum(conn):
    leads_of, delete, deleted = world({OLD: [lead(1)]})
    rot.rotate(conn, None, needed=1, batch=1,
               campaigns=[{"id": OLD, "status": 3, "name": "Old campaign"}], leads_of=leads_of, delete=delete)
    rows = backup_rows(conn)
    assert len(rows) == 1
    assert rows[0]["deleted"] is True and rows[0]["delete_status"] == 200
    assert len(rows[0]["payload_sha256"]) == 64
    assert rows[0]["reason"] == "finished_and_never_replied"
    # the full record is there, so the contact can be put back
    stored = sql1(conn, "SELECT lead_json FROM instantly_rotation_backup WHERE lead_id = 'L1'")
    assert stored["email"] == "p1@example.invalid" and stored["first_name"] == "P1"


def test_a_delete_that_fails_leaves_the_row_as_not_deleted_and_stops_the_batch(conn):
    """An interrupted batch must still say exactly what it touched."""
    leads_of, delete, deleted = world({OLD: [lead(i) for i in range(10)]}, fail_after=2)
    out = rot.rotate(conn, None, needed=10, batch=10,
                     campaigns=[{"id": OLD, "status": 3, "name": "Old"}], leads_of=leads_of, delete=delete)

    assert out["deleted"] == 2 and out["failed"] == 3, "it stopped instead of hammering a broken API"
    rows = {r["lead_id"]: r for r in backup_rows(conn)}
    assert sum(1 for r in rows.values() if r["deleted"]) == 2
    assert rot.pending_backups(conn) == 3


def test_a_lead_already_backed_up_is_not_deleted_twice(conn):
    leads_of, delete, deleted = world({OLD: [lead(1)]})
    campaigns = [{"id": OLD, "status": 3, "name": "Old"}]
    rot.rotate(conn, None, needed=1, batch=1, campaigns=campaigns, leads_of=leads_of, delete=delete)
    again = rot.rotate(conn, None, needed=1, batch=1, campaigns=campaigns, leads_of=leads_of, delete=delete)
    assert again["deleted"] == 0 and again["refused"]["already_backed_up"] == 1
    assert len(deleted) == 1


def test_a_zero_batch_is_refused_rather_than_silently_doing_nothing(conn):
    leads_of, delete, _ = world({OLD: [lead(1)]})
    with pytest.raises(RotationRefused):
        rot.rotate(conn, None, needed=5, batch=0,
                   campaigns=[{"id": OLD, "status": 3, "name": "Old"}], leads_of=leads_of, delete=delete)


def test_the_settings_are_bounded_and_off_by_default():
    assert rot.enabled({}) is False
    assert rot.enabled({"TGTC_INSTANTLY_ROTATION_ENABLED": "1"}) is True
    s = rot.settings({})
    assert s["plan_contacts"] == 25000 and s["free_slot_floor"] == 1500 and s["batch"] == 500
    assert rot.settings({"TGTC_INSTANTLY_ROTATION_BATCH": "999999"})["batch"] == rot.HARD_BATCH_CEILING
    assert rot.settings({"TGTC_INSTANTLY_ROTATION_BATCH": "-5"})["batch"] == 0


def test_the_protected_set_is_all_eighteen_live_ids():
    assert len(rot.protected_ids()) == 18


# --- when rotation is allowed to happen at all -------------------------------------
# Measured 2026-09-25: 19,549 stored of 25,000, so 5,451 free. A 1,000-contact run plus
# a 1,500 reserve fits with room to spare, and nothing should be deleted.


class Analytics:
    """Just enough of the client for the capacity policy."""

    def __init__(self, counts, *, fails=False):
        self.counts, self.fails, self.calls = list(counts), fails, 0

    def campaign_analytics(self):
        self.calls += 1
        if self.fails:
            return type("R", (), {"ok": False, "status": 503, "message": "unavailable", "data": {}})()
        rows = self.counts[min(self.calls - 1, len(self.counts) - 1)]
        return type("R", (), {"ok": True, "status": 200,
                              "data": {"items": [{"campaign_id": f"c{i}", "leads_count": n}
                                                 for i, n in enumerate(rows)]}})()


def test_occupancy_is_the_sum_of_every_campaign_in_one_request():
    client = Analytics([[2601, 501, 14361, 2086]])
    out = rot.occupancy(client, plan_contacts=25000)
    assert out["known"] is True and out["stored"] == 19549 and out["free"] == 5451
    assert client.calls == 1, "occupancy must not page through the whole workspace"


def test_an_unreadable_occupancy_never_becomes_a_reason_to_delete(conn):
    client = Analytics([], fails=True)
    out = rot.make_room(conn, client, target=1000, campaigns=[], leads_of=lambda c: [],
                        delete=lambda i: None, env={"TGTC_INSTANTLY_ROTATION_ENABLED": "1"})
    assert out["rotated"] is False and out["reason"] == "occupancy_unknown"


def test_room_needed_counts_the_run_and_a_reserve():
    assert rot.room_needed(5451, target=1000, reserve=1500) == 0
    assert rot.room_needed(900, target=1000, reserve=1500) == 1600
    assert rot.room_needed(0, target=1000, reserve=0) == 1000
    assert rot.room_needed(None, target=1000, reserve=1500) == 0, "unknown is not a mandate to delete"


def test_todays_workspace_needs_no_rotation_at_all(conn):
    """The live figures on 2026-09-25. Nothing is deleted and nothing is even considered."""
    client = Analytics([[19549]])
    deleted = []
    out = rot.make_room(conn, client, target=1000, campaigns=[{"id": OLD, "status": 3, "name": "Old"}],
                        leads_of=lambda c: [lead(1)], delete=lambda i: deleted.append(i),
                        env={"TGTC_INSTANTLY_ROTATION_ENABLED": "1"})
    assert out["needed"] == 0 and out["reason"] == "enough_room"
    assert out["rotated"] is False and deleted == []
    assert backup_rows(conn) == []


def test_a_full_workspace_frees_exactly_what_the_run_needs_and_proves_it(conn):
    """24,600 stored leaves 400 free; a 500-contact run plus the 1,500 reserve needs 1,600."""
    leads_of, delete, deleted = world({OLD: [lead(i) for i in range(4000)]})
    client = Analytics([[24600], [24600 - 1600]])       # before, then after the deletes
    out = rot.make_room(conn, client, target=500, campaigns=[{"id": OLD, "status": 3, "name": "Old"}],
                        leads_of=leads_of, delete=delete,
                        env={"TGTC_INSTANTLY_ROTATION_ENABLED": "1", "TGTC_INSTANTLY_ROTATION_BATCH": "2000"})
    assert out["needed"] == 1600 and out["rotated"] is True
    assert out["rotation"]["deleted"] == 1600 and len(deleted) == 1600
    assert out["freed_measured"] == 1600, "the freed slots were assumed, not measured"
    assert out["deficit"] == 0 and out["reason"] == "room_made"
    assert len(backup_rows(conn)) == 1600, "every removal must be recoverable"


def test_too_few_safe_candidates_reports_the_exact_deficit(conn):
    """The number the caller needs is how many slots it is STILL short, not how many went."""
    leads_of, delete, deleted = world({OLD: [lead(i) for i in range(30)]})
    client = Analytics([[24600], [24600 - 30]])
    out = rot.make_room(conn, client, target=500, campaigns=[{"id": OLD, "status": 3, "name": "Old"}],
                        leads_of=leads_of, delete=delete,
                        env={"TGTC_INSTANTLY_ROTATION_ENABLED": "1", "TGTC_INSTANTLY_ROTATION_BATCH": "2000"})
    assert out["rotation"]["deleted"] == 30 and out["freed_measured"] == 30
    assert out["deficit"] == 1570 and out["reason"] == "not_enough_safe_candidates"


def test_while_rotation_is_switched_off_a_shortfall_is_reported_not_acted_on(conn):
    deleted = []
    client = Analytics([[24600]])
    out = rot.make_room(conn, client, target=1000, campaigns=[{"id": OLD, "status": 3, "name": "Old"}],
                        leads_of=lambda c: [lead(1)], delete=lambda i: deleted.append(i), env={})
    assert out["reason"] == "rotation_disabled" and out["deficit"] == 2100
    assert deleted == [] and out["rotated"] is False
