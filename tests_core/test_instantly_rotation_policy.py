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


def _ok(data):
    return type("R", (), {"ok": True, "status": 200, "data": data})()


def _fail(message="unavailable"):
    return type("R", (), {"ok": False, "status": 503, "message": message, "data": {}})()


class Analytics:
    """Just enough of the client for the capacity policy.

    ``lists`` maps a lead-list id to the number of contacts parked on it. Those
    belong to no campaign, so they appear in no ``leads_count`` -- and they still
    occupy a stored contact on the plan.
    """

    def __init__(self, counts, *, fails=False, lists=None, lists_fail=False):
        self.counts, self.fails, self.calls = list(counts), fails, 0
        self.lists = dict(lists or {})
        self.lists_fail = lists_fail
        self.list_calls = 0

    def campaign_analytics(self):
        self.calls += 1
        if self.fails:
            return _fail()
        rows = self.counts[min(self.calls - 1, len(self.counts) - 1)]
        return _ok({"items": [{"campaign_id": f"c{i}", "leads_count": n}
                              for i, n in enumerate(rows)]})

    def list_lead_lists(self, *, limit=100, starting_after=None):
        self.list_calls += 1
        if self.lists_fail:
            return _fail("lists unavailable")
        return _ok({"items": [{"id": k, "name": k} for k in self.lists]})

    def list_list_leads(self, list_id, *, limit=100, starting_after=None):
        self.list_calls += 1
        if self.lists_fail:
            return _fail("lists unavailable")
        remaining = int(self.lists.get(list_id, 0))
        start = int(starting_after or 0)
        page = [{"id": f"{list_id}-{i}"} for i in range(start, min(start + limit, remaining))]
        nxt = start + len(page)
        data = {"items": page}
        if nxt < remaining:
            data["next_starting_after"] = str(nxt)
        return _ok(data)


def test_occupancy_counts_every_campaign_in_one_analytics_request():
    client = Analytics([[2601, 501, 14361, 2086]])
    out = rot.occupancy(client, plan_contacts=25000)
    assert out["known"] is True and out["stored"] == 19549 and out["free"] == 5451
    assert out["stored_in_campaigns"] == 19549 and out["stored_on_lists"] == 0
    assert client.calls == 1, "occupancy must not page through every campaign's contacts"


def test_a_contact_parked_on_a_lead_list_still_occupies_the_plan():
    """The 2026-10-02 regression, in numbers. Moving 1,688 contacts to a hold list
    dropped the campaign sum from 22,757 to 21,069 and freed NOTHING. Counting only
    campaign membership would promise 3,931 free slots where there are 2,243."""
    client = Analytics([[21069]], lists={"hold": 1688})
    out = rot.occupancy(client, plan_contacts=25000)
    assert out["stored_in_campaigns"] == 21069
    assert out["stored_on_lists"] == 1688
    assert out["stored"] == 22757, "campaign membership is not storage"
    assert out["free"] == 2243
    assert rot.room_needed(out["free"], target=1000, reserve=1500) == 257


def test_unreadable_lead_lists_make_occupancy_unknown_never_zero():
    client = Analytics([[21069]], lists={"hold": 1688}, lists_fail=True)
    out = rot.occupancy(client, plan_contacts=25000)
    assert out["known"] is False and out["stored"] is None


def test_a_client_that_cannot_enumerate_lists_is_unknown_not_empty():
    class OldClient:
        def campaign_analytics(self):
            return _ok({"items": [{"campaign_id": "c0", "leads_count": 21069}]})

    out = rot.occupancy(OldClient(), plan_contacts=25000)
    assert out["known"] is False, "assuming zero list contacts is the very undercount this prevents"


def test_unknown_occupancy_is_reported_as_its_own_stop_condition(conn):
    client = Analytics([[21069]], lists={"hold": 1688}, lists_fail=True)
    out = rot.make_room(conn, client, target=1000, campaigns=[], leads_of=lambda c: [],
                        delete=lambda i: None, env={"TGTC_INSTANTLY_ROTATION_ENABLED": "1"})
    assert out["occupancy_unknown"] is True and out["rotated"] is False
    assert out["deficit"] == 0, "unknown is not a measured shortfall"


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


# ---------------------------------------------------------------------------
# The 2026-09-29..10-02 rotation failure: a successful delete scored as a failure.
# ---------------------------------------------------------------------------

def test_an_instantly_result_delete_is_read_as_the_success_it_is():
    """Production passes `InstantlyClient.delete_lead`, which answers an
    InstantlyResult: ok/status/message, with NO status_code and no text. Reading
    status_code scored every delete 0 -- a failure -- and the three-failure
    breaker then aborted rotation after three contacts."""
    from tgtc_core.providers.instantly import InstantlyResult

    assert rot._delete_outcome(InstantlyResult(True, 200)) == (200, "")
    assert rot._delete_outcome(InstantlyResult(True, 204)) == (204, "")
    code, err = rot._delete_outcome(InstantlyResult(False, 429, message="rate limited"))
    assert code == 429 and "rate limited" in err
    # a requests-like object still works
    assert rot._delete_outcome(type("R", (), {"status_code": 200})()) == (200, "")
    # ok without a status is still a success, not a zero
    assert rot._delete_outcome(type("R", (), {"ok": True, "status": None})()) == (200, "")


def test_rotation_counts_real_deletions_instead_of_aborting_on_three(conn):
    """With the real result shape, rotation must delete through the available
    candidates rather than stopping at the breaker after three."""
    from tgtc_core.providers.instantly import InstantlyResult

    leads = [{"id": f"l{i}", "email": f"p{i}@old.com", "status": 3, "email_reply_count": 0}
             for i in range(8)]
    campaigns = [{"id": "legacy-1", "status": 3, "name": "Legacy"}]
    deleted = []

    def delete(lead_id):
        deleted.append(lead_id)
        return InstantlyResult(True, 200)

    out = rot.rotate(conn, None, needed=5, batch=500, campaigns=campaigns,
                     leads_of=lambda c: list(leads), delete=delete)
    assert out["deleted"] == 5, out
    assert out["failed"] == 0, out
    assert len(deleted) == 5
    assert rot.pending_backups(conn) == 0, "every backup must be marked deleted"


def test_a_genuinely_failing_delete_still_trips_the_breaker(conn):
    from tgtc_core.providers.instantly import InstantlyResult

    leads = [{"id": f"l{i}", "email": f"p{i}@old.com", "status": 3, "email_reply_count": 0}
             for i in range(8)]
    campaigns = [{"id": "legacy-1", "status": 3, "name": "Legacy"}]
    out = rot.rotate(conn, None, needed=5, batch=500, campaigns=campaigns,
                     leads_of=lambda c: list(leads),
                     delete=lambda i: InstantlyResult(False, 500, message="boom"))
    assert out["deleted"] == 0 and out["failed"] == 3, out
    assert rot.pending_backups(conn) == 3, "failed deletes stay flagged for a human"


def test_occupancy_through_the_REAL_client_counts_a_contact_parked_on_a_list():
    """Not the stub: the real InstantlyClient against the simulated provider, so
    the endpoint shapes are exercised too. A contact moved to a list must still
    count, or the controller promises slots the plan does not have."""
    from tgtc_core.providers.instantly import InstantlyClient
    from tgtc_core.testing.fakes import FakeInstantly

    fake = FakeInstantly(campaign_status={"c-live": 1})
    fake.leads = {f"p{i}@acme.com": {"id": f"l{i}", "email": f"p{i}@acme.com",
                                     "campaign": "c-live"} for i in range(4)}
    fake.lead_lists = {"hold": ["h1", "h2", "h3"]}
    client = InstantlyClient(fake, base_url="https://api.instantly.ai/api/v2", api_key="sim")

    out = rot.occupancy(client, plan_contacts=10)
    assert out["known"] is True
    assert out["stored_in_campaigns"] == 4
    assert out["stored_on_lists"] == 3
    assert out["stored"] == 7 and out["free"] == 3


def test_moving_a_contact_to_a_list_frees_no_slot():
    """The 2026-10-02 mistake, as behaviour: taking contacts out of campaigns
    changes WHERE they are, not HOW MANY are stored."""
    from tgtc_core.providers.instantly import InstantlyClient
    from tgtc_core.testing.fakes import FakeInstantly

    fake = FakeInstantly(campaign_status={"c-live": 1})
    fake.leads = {f"p{i}@acme.com": {"id": f"l{i}", "email": f"p{i}@acme.com",
                                     "campaign": "c-live"} for i in range(4)}
    client = InstantlyClient(fake, base_url="https://api.instantly.ai/api/v2", api_key="sim")
    before = rot.occupancy(client, plan_contacts=10)

    # park two of them on a list, exactly as the hold move did
    for lead in list(fake.leads.values())[:2]:
        lead["campaign"] = ""
    fake.lead_lists = {"hold": ["l0", "l1"]}
    after = rot.occupancy(client, plan_contacts=10)

    assert after["stored_in_campaigns"] == before["stored_in_campaigns"] - 2
    assert after["stored"] == before["stored"], "storage is unchanged by a move"
    assert after["free"] == before["free"], "and so is the free-slot count"


def test_the_ooo_followup_campaign_is_protected_from_rotation():
    """It holds people who asked us to come back later. Deleting them discards a
    deferral we promised. Four of the twelve contacts rotation actually removed
    over 2026-09-29..10-02 came from it, because it was not protected."""
    env = {"TGTC_OOO_FOLLOWUP_CAMPAIGN_ID": "f0665173-d46b-49e6-8f17-4e024876a5a3"}
    protected = rot.protected_ids(env)
    assert "f0665173-d46b-49e6-8f17-4e024876a5a3" in protected
    # and judge refuses a finished, never-replied contact sitting in it
    lead = {"id": "l1", "email": "x@y.com", "status": 3, "email_reply_count": 0}
    assert rot.judge(lead, campaign_id="f0665173-d46b-49e6-8f17-4e024876a5a3",
                     campaign_status=3, held=set(), protected=protected) == "live_campaign"
    # an unconfigured follow-up must not add an empty id to the protected set
    assert "" not in rot.protected_ids({})


def test_rotation_skips_the_followup_campaign_entirely(conn, monkeypatch):
    from tgtc_core.providers.instantly import InstantlyResult

    monkeypatch.setenv("TGTC_OOO_FOLLOWUP_CAMPAIGN_ID", "camp-followup")
    leads = [{"id": "f1", "email": "a@old.com", "status": 3, "email_reply_count": 0}]
    campaigns = [{"id": "camp-followup", "status": 3, "name": "OOO follow-up"}]
    deleted = []
    out = rot.rotate(conn, None, needed=5, batch=500, campaigns=campaigns,
                     leads_of=lambda c: list(leads),
                     delete=lambda i: (deleted.append(i), InstantlyResult(True, 200))[1])
    assert deleted == [], "a protected campaign must not be paged or deleted from"
    assert out["deleted"] == 0 and out["considered"] == 0


def test_a_finished_contact_in_a_PAUSED_campaign_is_eligible(conn):
    """The selection gap, measured 2026-10-03. `judge` and the enumeration both asked
    whether the CAMPAIGN was completed, which hid 3,220 leads whose own sequence was
    finished, who never replied and who were not suppressed -- while the run was blocked
    for want of 22 slots. 10,282 leads sit in unprotected campaigns and only 269 of them
    were in a COMPLETED one.

    A lead with status 3 is done: resuming that paused campaign cannot send to it, so
    removing it costs no email. Protection, an unfinished sequence, a reply and our own
    suppressions all still refuse."""
    leads_of, delete, deleted = world({OLD: [lead(1), lead(2)]})
    for status, label in ((2, "PAUSED"), (-2, "BOUNCE_PROTECT"), (1, "ACTIVE"), (0, "DRAFT")):
        deleted.clear()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM instantly_rotation_backup")
        conn.commit()
        out = rot.rotate(conn, None, needed=10, batch=10,
                         campaigns=[{"id": OLD, "status": status, "name": "Legacy " + label}],
                         leads_of=leads_of, delete=delete)
        assert out["deleted"] == 2, (label, out)
        assert sorted(deleted) == ["L1", "L2"], label
        assert len(backup_rows(conn)) == 2, "the record must be backed up first, " + label


def test_protection_still_wins_whatever_the_campaign_status(conn):
    """Dropping the campaign-status rule must not open a protected campaign."""
    leads_of, delete, deleted = world({LIVE: [lead(1)], CONTROL: [lead(2)]})
    for status in (1, 2, 3, -2, 0):
        out = rot.rotate(conn, None, needed=10, batch=10,
                         campaigns=[{"id": LIVE, "status": status, "name": "CHALLENGER"},
                                    {"id": CONTROL, "status": status, "name": "CONTROL"}],
                         leads_of=leads_of, delete=delete)
        assert out["deleted"] == 0 and deleted == [], status
        assert backup_rows(conn) == []


def test_an_unfinished_contact_in_a_paused_campaign_is_still_refused(conn):
    """A paused campaign can be resumed, so a lead still in sequence must not go."""
    leads_of, delete, deleted = world({OLD: [lead(1, status=1), lead(2, status=-1),
                                             lead(3, reply="2026-09-01T10:00:00Z")]})
    out = rot.rotate(conn, None, needed=10, batch=10,
                     campaigns=[{"id": OLD, "status": 2, "name": "Legacy paused"}],
                     leads_of=leads_of, delete=delete)
    assert out["deleted"] == 0 and deleted == []
    assert out["refused"]["sequence_not_finished:1"] == 1
    assert out["refused"]["sequence_not_finished:-1"] == 1
    assert out["refused"]["has_reply"] == 1
