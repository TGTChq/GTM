"""The ONE repair email: one per person, never blank, never to somebody who said stop.

These are the rules the 2026-09-21 incident was caused by not having. Each test here is a
way the repair could repeat the harm it exists to fix, so a rule without a test below is
a rule that will be broken by a replayed cron at 03:00.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from tgtc_core.policy.campaigns import EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID as CAMPAIGN
from tgtc_core.providers.instantly import DOCUMENTED_LEAD_FIELDS
from tgtc_core.services import empty_email_recovery as rec
from tests_core.helpers import sql1, sqlall

INCIDENT = rec.INCIDENT_SUPPRESSION_SOURCE
#: What a test needs to make `enrol` do any work: the switch on, and no pacing. The
#: switch is OFF in production by default -- that is the handover state -- and the real
#: pace is asserted by its own tests below; everywhere else it would only make the suite
#: slow, so both are stated here rather than inherited.
NO_PACE = {rec.PACE_SECONDS_ENV: "0", rec.ENROL_ENABLED_ENV: "1"}


class Result:
    def __init__(self, ok=True, status=200, data=None, message="", uncertain=False):
        self.ok, self.status, self.message, self.uncertain = ok, status, message, uncertain
        self.data = data if data is not None else {}


class FakeInstantly:
    """Enough of the client to exercise the paths, and it records what it was asked."""

    def __init__(self, *, stored=1000, allowance_error=False, emails_sent=0,
                 drop_variables=False, subject=None, on_lists=0, rate_limited=False,
                 move_fails=False, patch_fails=False, move_never_settles=False,
                 prefix="L", campaign_status=2, campaign_unreadable=False):
        # Paused by default, which is the state the whole queue is loaded into.
        self.campaign_status = campaign_status
        self.campaign_unreadable = campaign_unreadable
        self.rate_limited = rate_limited
        self.move_fails = move_fails
        self.patch_fails = patch_fails
        self.move_never_settles = move_never_settles
        self.moved: list = []
        self.patched: list = []
        # Its own id space, so a helper's client and a test's client cannot both mint
        # "L1" and collide on the unique index that binds one lead to one recipient.
        self.prefix = prefix
        self.stored = stored
        self.on_lists = on_lists
        self.deleted: list = []
        self.allowance_error = allowance_error
        self.emails_sent = emails_sent
        self.drop_variables = drop_variables
        self.subject = subject
        self.created: list = []
        self.leads: dict = {}
        self.paused: list = []
        self.messages: dict = {}

    # --- the campaign, which is what decides whether loading is safe
    def get_campaign(self, campaign_id):
        if self.campaign_unreadable:
            return Result(False, 500, message="upstream error")
        return Result(data={"id": campaign_id, "status": self.campaign_status})

    # --- capacity
    def list_lead_lists(self, *, limit=100, starting_after=None):
        if starting_after or not self.on_lists:
            return Result(data={"items": []})
        return Result(data={"items": [{"id": "hold-list"}]})

    def list_list_leads(self, list_id, *, limit=100, starting_after=None):
        if starting_after:
            return Result(data={"items": []})
        return Result(data={"items": [{"id": "x%d" % i} for i in range(self.on_lists)]})

    def delete_lead(self, lead_id):
        self.deleted.append(lead_id)
        return Result(data={})

    def list_campaigns(self, *, limit=100, starting_after=None):
        return Result(data={"items": []})

    def list_campaign_leads(self, campaign_id, *, limit=100, starting_after=None):
        return Result(data={"items": []})

    def campaign_analytics(self):
        return Result(data={"items": [
            {"campaign_id": "other", "leads_count": self.stored},
            {"campaign_id": CAMPAIGN, "leads_count": len(self.created),
             "emails_sent_count": self.emails_sent},
        ]})

    # --- the move route
    def move_lead_from(self, lead_id, *, source_kind, source_id, to_campaign):
        if lead_id not in self.leads:
            return Result(False, 404, message="not found")
        if self.move_fails:
            return Result(False, 500, message="upstream error")
        self.moved.append({"lead": lead_id, "from_kind": source_kind,
                           "from": source_id, "to": to_campaign})
        if not self.move_never_settles:
            self.leads[lead_id]["campaign"] = to_campaign
            # A move clears the sequence position. That is authorised here and is the
            # point: they receive this one email and nothing else.
            self.leads[lead_id]["status_summary"] = {}
        return Result(data={"id": "job-%d" % len(self.moved), "status": "pending"})

    def update_lead(self, lead_id, patch):
        if lead_id not in self.leads:
            return Result(False, 404, message="not found")
        if self.patch_fails:
            return Result(False, 500, message="upstream error")
        self.patched.append({"lead": lead_id, "patch": patch})
        variables = patch.get("custom_variables")
        if isinstance(variables, dict):
            # The real API REPLACES the set; the fake does the same so a caller that
            # forgets to merge loses data here too.
            kept = {k: v for k, v in self.leads[lead_id]["payload"].items()
                    if k in ("email", "campaign")}
            self.leads[lead_id]["payload"] = {**kept, **variables}
        return Result(data={"id": lead_id})

    # --- create and read back
    def create_lead(self, payload):
        from tgtc_core.domain.outbound_copy import copy_block_reason
        reason = copy_block_reason(payload)
        if reason:
            raise ValueError(reason)
        unknown = set(payload) - DOCUMENTED_LEAD_FIELDS
        if unknown:
            raise ValueError("undocumented: %s" % sorted(unknown))
        if self.rate_limited:
            return Result(False, 429, message="Too many requests")
        if self.allowance_error:
            return Result(False, 403, message='{"message":"Lead limit reached. Remaining uploads: 0"}')
        lead_id = "%s%d" % (self.prefix, len(self.created) + 1)
        self.created.append(dict(payload))
        variables = {} if self.drop_variables else dict(payload["custom_variables"])
        self.leads[lead_id] = {
            "id": lead_id, "email": payload["email"], "campaign": payload["campaign"],
            "status": 1, "status_summary": {},
            # Flattened, which is where Instantly really keeps them.
            "payload": {"email": payload["email"], "campaign": payload["campaign"],
                        "firstName": payload["first_name"], **variables}}
        return Result(data={"id": lead_id})

    def seed_existing(self, lead_id, *, email, campaign, first="Ana", status=1,
                      variables=None):
        """A lead that already exists in Instantly, as the affected contacts do."""
        self.leads[lead_id] = {
            "id": lead_id, "email": email, "campaign": campaign, "status": status,
            "status_summary": {"step": 1},
            "payload": {"email": email, "campaign": campaign, "firstName": first,
                        "signal_tier": "keep-me", **(variables or {})}}
        return lead_id

    def get_lead(self, lead_id):
        if lead_id not in self.leads:
            return Result(False, 404, message="not found")
        return Result(data=self.leads[lead_id])

    # --- receipts
    def emails_for(self, email, *, campaign_id="", limit=50):
        if email in self.messages:
            return Result(data={"items": [self.messages[email]]})
        return Result(data={"items": []})

    def delivered(self, email, *, first="Ana", role="Tax Accountant", **over):
        """The message a correct send produces, rendered the way Instantly renders it."""
        html = ("Hi {first},<br><br>An earlier email from us went out without its "
                "message&mdash;sorry about that.<br><br>I wanted to reach out about your "
                "{role} opening. The Global Talent Co. helps companies hire vetted "
                "international professionals matched to the role.<br><br>Would it be "
                "useful to see a few relevant profiles?<br><br><div>Devan Markus</div>"
                "<div>Business Development</div><div>The Global Talent Co.</div>"
                ).format(first=first, role=role)
        message = {"message_id": "M-%s" % email, "subject": "Your %s opening" % role,
                   "body": {"html": html}, "lead": email,
                   "to_address_email_list": email, "step": "0_0_0",
                   "from_address_email": "rep@gtcglobalteams.com"}
        message.update(over)
        self.messages[email] = message
        return message

    def pause_campaign(self, campaign_id):
        self.paused.append(campaign_id)
        return Result(data={"status": 2})


class CountingPace(rec._Pace):
    """Records every wait instead of taking it."""

    def __init__(self, seconds=3.0):
        self.waits = 0
        super().__init__(seconds, sleep=self._record, clock=lambda: 0.0)

    def _record(self, seconds):
        self.waits += 1


def queue_row(i, *, role="Tax Accountant", first="Ana"):
    return {"email": "p%d@employer.test" % i, "person_id": 1000 + i, "first_name": first,
            "last_name": "Ruiz", "employer": "Employer Ltd", "employer_domain": "employer.test",
            "function_key": "finance", "verified_role": role, "posting_id": "JOB%d" % i,
            "posting_title": "Senior %s" % role, "posting_is_the_original": True,
            "original_campaign_id": "269cd138-00b1-48c3-9093-16c36120a20e"}


def load(conn, n=3, *, gate_open=True, **kw):
    report = rec.load_queue(conn, [queue_row(i, **kw) for i in range(1, n + 1)])
    if gate_open:
        pass_the_internal_test(conn)
    return report


def pass_the_internal_test(conn, client=None):
    """Put the gate through the REAL path: enrol our own address, then receive the email.

    Deliberately not a hand-written evidence blob. The gate asks whether a whole received
    message checked out, so a helper that asserts that by fiat would let the tests pass
    while the thing they exist to protect was broken.
    """
    own = client or FakeInstantly(prefix="QA")
    rec.load_queue(conn, [dict(queue_row(0), email="qa@tgtc.test", is_internal_test=True)])
    rec.enrol(conn, own, limit=1, env=dict(NO_PACE))
    own.delivered("qa@tgtc.test", first="Ana", role="Tax Accountant")
    rec.record_receipts(conn, own, pace=CountingPace(0))
    gate = rec.internal_test_passed(conn)
    assert gate["passed"], gate
    return own


def suppress(conn, email, *, source, reason="opt_out", at=None):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO suppressions (kind, key, source, reason, created_at) "
                    "VALUES ('person_email', %s, %s, %s, coalesce(%s, now())) "
                    "ON CONFLICT (kind, key) DO NOTHING", (email, source, reason, at))
    conn.commit()


def event(conn, email, kind):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO outcome_events (provider, event_type, dedupe_key, email) "
                    "VALUES ('instantly', %s, %s, %s)", (kind, "%s:%s" % (email, kind), email))
    conn.commit()


def states(conn):
    """The affected recipients only: the internal test row is not one of them."""
    return {r["email"]: r["state"] for r in
            sqlall(conn, "SELECT email, state FROM empty_email_recovery "
                         "WHERE NOT is_internal_test ORDER BY email")}


def affected_rows(conn, columns):
    return sqlall(conn, "SELECT %s FROM empty_email_recovery WHERE NOT is_internal_test "
                        "ORDER BY email" % columns)


# ------------------------------------------------------------------ one per person


def test_a_sent_recipient_can_never_become_sendable_again(conn):
    """The database refuses it, so a replayed enroller cannot send a second email."""
    load(conn, 1, gate_open=False)
    with conn.cursor() as cur:
        cur.execute("UPDATE empty_email_recovery SET state = 'sent', sent_at = now(), "
                    "message_id = 'M1' WHERE email = 'p1@employer.test'")
    conn.commit()
    for target in ("authorised", "reserved", "enrolled"):
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute("UPDATE empty_email_recovery SET state = %s WHERE email = %s",
                            (target, "p1@employer.test"))
        conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("UPDATE empty_email_recovery SET sent_at = NULL "
                        "WHERE email = 'p1@employer.test'")
    conn.rollback()
    assert sql1(conn, "SELECT state FROM empty_email_recovery") == "sent"


def test_reloading_the_queue_does_not_re_authorise_anybody(conn):
    load(conn, 2, gate_open=False)
    with conn.cursor() as cur:
        cur.execute("UPDATE empty_email_recovery SET state = 'sent', sent_at = now() "
                    "WHERE email = 'p1@employer.test'")
        cur.execute("UPDATE empty_email_recovery SET state = 'revoked', "
                    "revoked_at = now(), state_reason = 'later_outcome:reply' "
                    "WHERE email = 'p2@employer.test'")
    conn.commit()
    again = load(conn, 2, gate_open=False)
    assert again["inserted"] == 0 and again["already_present"] == 2
    assert states(conn) == {"p1@employer.test": "sent", "p2@employer.test": "revoked"}


def test_one_row_per_recipient_whatever_the_queue_repeats(conn):
    report = rec.load_queue(conn, [queue_row(1), queue_row(1), queue_row(2)])
    assert report["inserted"] == 2
    assert sql1(conn, "SELECT count(*) AS n FROM empty_email_recovery") == 2


def test_a_row_without_a_role_or_a_name_is_never_queued(conn):
    report = rec.load_queue(conn, [queue_row(1, role="  "), queue_row(2, first=""),
                                   queue_row(3)])
    assert report == {"inserted": 1, "already_present": 0, "refused_incomplete": 2}


# ----------------------------------------------------------------- never blank copy


def test_the_payload_is_documented_and_satisfies_the_copy_contract(conn):
    from tgtc_core.domain.outbound_copy import copy_block_reason
    payload = rec._payload(queue_row(1))
    assert set(payload) <= DOCUMENTED_LEAD_FIELDS
    assert copy_block_reason(payload) == ""
    assert payload["custom_variables"] == {"verified_role": "Tax Accountant"}
    assert payload["campaign"] == CAMPAIGN


def test_a_lead_whose_role_vanished_on_the_way_out_is_withheld_not_sent(conn):
    """The contract inside the provider client is the last line, and it is reached."""
    load(conn, 1)
    with conn.cursor() as cur:                      # a role that the CHECK allows but copy cannot use
        cur.execute("UPDATE empty_email_recovery SET verified_role = '{{role}}' "
                    "WHERE email = 'p1@employer.test'")
    conn.commit()
    client = FakeInstantly()
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 0 and report["withheld"] == 1
    assert client.created == []
    row = affected_rows(conn, "state, state_reason")[0]
    assert row["state"] == "withheld"
    assert row["state_reason"] == "recovery_copy_unresolved:verified_role"


def test_a_created_lead_that_reads_back_without_its_role_is_not_enrolled(conn):
    """A 200 on the create is not evidence. The incident was 200s all the way down."""
    load(conn, 1)
    client = FakeInstantly(drop_variables=True)
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 0 and report["failed"] == 1
    row = affected_rows(conn, "state, last_error, instantly_lead_id")[0]
    assert row["state"] == "authorised"             # it stays sendable, once the data is fixed
    assert row["last_error"] == "verified_missing:verified_role"
    assert row["instantly_lead_id"] == "L1"         # recorded, so the lead is not orphaned


def test_an_enrolled_contact_is_only_sent_when_instantly_shows_a_message(conn):
    load(conn, 2)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert set(states(conn).values()) == {"enrolled"}

    # Nothing has been sent yet, so nothing may claim it was.
    assert rec.record_receipts(conn, client, pace=CountingPace(0))["sent"] == 0
    assert set(states(conn).values()) == {"enrolled"}

    client.delivered("p1@employer.test")
    report = rec.record_receipts(conn, client, pace=CountingPace(0))
    assert report["sent"] == 1 and report["not_as_approved_count"] == 0
    row = sqlall(conn, "SELECT state, message_id, evidence FROM empty_email_recovery "
                       "WHERE email = 'p1@employer.test'")[0]
    assert row["state"] == "sent" and row["message_id"] == "M-p1@employer.test"
    assert row["evidence"]["subject_as_approved"] is True
    assert states(conn)["p2@employer.test"] == "enrolled"


def test_a_blank_subject_that_got_out_is_recorded_and_pauses_the_campaign(conn):
    """The incident itself, as a test: our ledger must show it, not only an inbox."""
    load(conn, 1)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    client.delivered("p1@employer.test", subject="",
                     body={"html": "<div>Devan Markus</div><div>The Global Talent Co.</div>"})
    report = rec.record_receipts(conn, client, pace=CountingPace(0))
    assert report["not_as_approved_count"] == 1
    failed = set(report["not_as_approved"][0]["failed"])
    assert {"subject_is_the_approved_one", "body_arrived_at_all",
            "every_approved_sentence_is_there", "greeting_carries_their_name"} <= failed

    guard = rec.guard(conn, client)
    assert client.paused == [CAMPAIGN]
    assert "unapproved_subject_sent" in guard["campaign_paused_because"]
    assert rec.ledger(conn)["sent_with_an_unapproved_subject"] == 1


def test_more_messages_than_recipients_is_a_second_email_and_stops_the_campaign(conn):
    load(conn, 2)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    client.emails_sent = 4   # two affected plus the test row, and a fourth message
    guard = rec.guard(conn, client)
    assert "second_email_sent" in guard["campaign_paused_because"]
    assert client.paused == [CAMPAIGN]


def test_the_guard_stays_quiet_when_nothing_is_wrong(conn):
    load(conn, 2)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    client.emails_sent = 2
    guard = rec.guard(conn, client)
    assert guard["findings"] == [] and client.paused == []


# ----------------------------------------------------------- never against a refusal


def test_a_suppression_from_anywhere_else_revokes_the_authorisation(conn):
    load(conn, 3)
    suppress(conn, "p1@employer.test", source="reply_workflow", reason="unsubscribe")
    suppress(conn, "p2@employer.test", source=INCIDENT, reason="blank_copy_incident")
    report = rec.revalidate(conn)
    assert report["revoked"] == 1
    assert states(conn)["p1@employer.test"] == "revoked"
    # The incident's OWN suppression is what the recovery is an exception to.
    assert states(conn)["p2@employer.test"] == "authorised"


@pytest.mark.parametrize("kind", ["human_reply", "unsubscribe", "bounce", "no_longer_here"])
def test_anything_recorded_since_the_blank_email_revokes_it(conn, kind):
    load(conn, 1)
    event(conn, "p1@employer.test", kind)
    assert rec.revalidate(conn)["revoked"] == 1
    assert states(conn)["p1@employer.test"] == "revoked"


def test_an_opt_out_on_the_person_revokes_it(conn):
    load(conn, 1)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO people (email, opt_out_status) VALUES (%s, %s)",
                    ("p1@employer.test", "unsubscribed"))
    conn.commit()
    assert rec.revalidate(conn)["revoked"] == 1
    assert states(conn)["p1@employer.test"] == "revoked"


def test_a_revoked_recipient_is_never_enrolled(conn):
    load(conn, 2)
    event(conn, "p1@employer.test", "human_reply")
    rec.revalidate(conn)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert [c["email"] for c in client.created] == ["p2@employer.test"]


def test_somebody_who_was_already_suppressed_elsewhere_before_the_send_is_a_guard_finding(conn):
    load(conn, 1)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    suppress(conn, "p1@employer.test", source="reply_workflow", reason="unsubscribe")
    client.delivered("p1@employer.test")
    rec.record_receipts(conn, client, pace=CountingPace(0))
    guard = rec.guard(conn, client)
    assert "sent_to_an_excluded_recipient" in guard["campaign_paused_because"]


# ------------------------------------------------------------------------- storage


def test_the_daily_run_keeps_its_floor(conn):
    load(conn, 5)
    client = FakeInstantly(stored=24000)            # 1,000 free against a floor of 1,500
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["stopped"] == "storage_floor_reached"
    assert report["enrolled"] == 0 and client.created == []
    assert set(states(conn).values()) == {"authorised"}


def test_only_the_slots_above_the_floor_are_taken(conn):
    load(conn, 5)
    client = FakeInstantly(stored=23498)            # 1,502 free, floor 1,500 -> room for 2
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 2
    assert sorted(states(conn).values()) == ["authorised", "authorised", "authorised",
                                             "enrolled", "enrolled"]


def test_the_providers_own_refusal_parks_the_queue_without_losing_it(conn):
    load(conn, 4)
    client = FakeInstantly(allowance_error=True)
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["stopped"] == "provider_storage_full"
    assert report["enrolled"] == 0
    # Every row is still sendable, and the one that was tried says why it was not.
    assert set(states(conn).values()) == {"authorised"}
    assert sql1(conn, "SELECT count(*) AS n FROM empty_email_recovery "
                      "WHERE last_error = 'provider_storage_full'") == 1


def test_a_batch_is_bounded_so_the_shared_send_budget_is_not_raided(conn):
    load(conn, 10)
    client = FakeInstantly()
    report = rec.enrol(conn, client, limit=3, env=dict(NO_PACE))
    assert report["enrolled"] == 3
    assert rec.ledger(conn)["remaining_to_enrol"] == 7


def test_the_floor_and_the_batch_are_configurable_without_code(conn):
    load(conn, 10)
    client = FakeInstantly(stored=20000)
    report = rec.enrol(conn, client, env={rec.STORAGE_FLOOR_ENV: "4999",
                                          rec.BATCH_ENV: "4", **NO_PACE})
    assert report["capacity"]["available"] == 1      # 5,000 free minus a 4,999 floor
    assert report["enrolled"] == 1


# --------------------------------------------------------- not new work, not new rows


def test_contacts_held_outside_a_campaign_still_count_against_the_plan(conn):
    """Measured 2026-10-03: 22,522 stored against 20,509 the campaign view reports.

    Reading only the campaign view would have believed in 2,013 slots that do not exist
    and taken them from the floor the daily run needs.
    """
    load(conn, 5)
    on_campaigns_only = rec.free_slots(FakeInstantly(stored=21000), env={})
    with_a_hold_list = rec.free_slots(FakeInstantly(stored=21000, on_lists=2013), env={})
    assert on_campaigns_only["free"] == 4000
    assert with_a_hold_list["stored"] == 23013
    assert with_a_hold_list["free"] == 1987
    assert with_a_hold_list["on_lead_lists"] == 2013
    assert with_a_hold_list["available"] == 1987 - rec.DEFAULT_STORAGE_FLOOR


def test_a_workspace_whose_lead_lists_cannot_be_read_stops_the_tick(conn):
    """Unknown is not empty. Assuming zero is the undercount that wastes the allowance."""
    load(conn, 2)

    class Blind(FakeInstantly):
        def list_lead_lists(self, *, limit=100, starting_after=None):
            return Result(False, 500, message="upstream error")

    report = rec.enrol(conn, Blind(), limit=10, env=dict(NO_PACE))
    assert report["stopped"] == "capacity_unknown" and report["enrolled"] == 0


def test_the_queue_waits_at_the_floor_unless_rotation_is_explicitly_allowed(conn):
    load(conn, 3)
    client = FakeInstantly(stored=24000)
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["stopped"] == "storage_floor_reached"
    assert report["rotation"] is None and client.deleted == []


def test_when_rotation_is_allowed_it_is_the_authorised_one_with_its_own_guards(conn):
    """No second deletion path: the same make_room, so the same rules protect people."""
    load(conn, 3)
    client = FakeInstantly(stored=24000)
    report = rec.enrol(conn, client, limit=10,
                       env={rec.MAY_ROTATE_ENV: "1",
                            "TGTC_INSTANTLY_ROTATION_ENABLED": "0", **NO_PACE})
    # Rotation was asked and declined itself, because it is disabled in this environment.
    assert report["rotation"]["reason"] == "rotation_disabled"
    assert report["stopped"] == "storage_floor_reached"
    assert client.deleted == [], "nothing may be deleted while rotation is off"


def test_a_recovered_contact_is_not_net_new_and_opens_no_second_airtable_row(conn):
    """They were acquired, approved and emailed weeks ago. This is a repair."""
    load(conn, 3)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    client.delivered("p1@employer.test")
    rec.record_receipts(conn, client, pace=CountingPace(0))
    for table in ("approvals", "delivery_outbox", "delivery_receipts", "candidate_attempts",
                  "work_items", "credit_events"):
        assert sql1(conn, "SELECT count(*) AS n FROM %s" % table) == 0, table


def test_the_recovery_spends_nothing_at_a_metered_provider(conn):
    load(conn, 2)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert sql1(conn, "SELECT count(*) AS n FROM credit_events") == 0
    assert sql1(conn, "SELECT count(*) AS n FROM provider_state") == 0


# -------------------------------------------------------------------- protection


def test_rotation_may_never_remove_the_recovery_campaign_or_its_queue(conn):
    from tgtc_core.services import instantly_rotation as rot
    assert CAMPAIGN in rot.protected_ids({})
    load(conn, 1)
    held = rot._held_emails(conn)
    assert "p1@employer.test" in held
    assert rot.judge({"id": "L1", "status": 3}, campaign_id=CAMPAIGN, campaign_status=3,
                     held=held) == "live_campaign"


def test_a_queued_recipient_is_held_from_rotation_even_with_no_suppression(conn):
    """The protection is stated on its own terms, not borrowed from the suppression."""
    from tgtc_core.services import instantly_rotation as rot
    load(conn, 1)
    assert sql1(conn, "SELECT count(*) AS n FROM suppressions") == 0
    assert "p1@employer.test" in rot._held_emails(conn)


# ------------------------------------------------------------------------- ledger


def test_the_ledger_is_one_row_per_recipient_and_accounts_for_all_of_them(conn):
    load(conn, 6)
    event(conn, "p1@employer.test", "human_reply")
    rec.revalidate(conn)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=2, env=dict(NO_PACE))
    client.delivered("p2@employer.test")
    rec.record_receipts(conn, client, pace=CountingPace(0))
    led = rec.ledger(conn)
    assert led["recipients"] == 7          # six affected plus the internal test row
    assert sum(led["by_state"].values()) == 7
    assert led["by_state"]["revoked"] == 1
    assert led["by_state"]["sent"] == 2     # the test, and the one with a message
    assert led["remaining_to_enrol"] == 3
    assert led["internal_test"]["passed"] is True


def test_a_dry_run_creates_nothing(conn):
    load(conn, 3)
    client = FakeInstantly()
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE), dry_run=True)
    assert report["would_enrol"] == 3 and client.created == []
    assert set(states(conn).values()) == {"authorised"}


def test_an_unmeasurable_workspace_stops_rather_than_guesses(conn):
    load(conn, 2)

    class Blind(FakeInstantly):
        def campaign_analytics(self):
            return Result(False, 500, message="upstream error")

    report = rec.enrol(conn, Blind(), limit=10, env=dict(NO_PACE))
    assert report["stopped"] == "capacity_unknown" and report["enrolled"] == 0


def test_an_enrolment_that_is_interrupted_leaves_the_row_claimed(conn):
    """A crash between the create and the record must not look like untouched work."""
    load(conn, 1)

    class Crash(FakeInstantly):
        def create_lead(self, payload):
            raise KeyboardInterrupt("container replaced mid-call")

    with pytest.raises(KeyboardInterrupt):
        rec.enrol(conn, Crash(), limit=10, env=dict(NO_PACE))
    row = affected_rows(conn, "state, attempts")[0]
    assert row["state"] == "reserved" and row["attempts"] == 1


def test_an_alert_fires_on_harm_and_stays_quiet_on_a_healthy_queue(conn):
    from tgtc_core.reporting import health_alerts
    from tests_core.conftest import NOW

    load(conn, 2)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    client.delivered("p1@employer.test")
    rec.record_receipts(conn, client, pace=CountingPace(0))
    healthy = health_alerts.check_empty_email_recovery(conn, now=NOW)
    assert healthy is None or healthy["severity"] != "high"

    client.delivered("p2@employer.test", subject="",
                     body={"html": "<div>The Global Talent Co.</div>"})
    rec.record_receipts(conn, client, pace=CountingPace(0))
    alert = health_alerts.check_empty_email_recovery(conn, now=NOW)
    assert alert["severity"] == "high"
    assert alert["detail"]["unapproved_subject_sent"] == 1


def test_a_queue_that_has_stopped_moving_is_visible_without_being_an_emergency(conn):
    from tgtc_core.reporting import health_alerts
    from tests_core.conftest import NOW

    load(conn, 3)        # the gate is open, so a waiting queue is worth seeing
    alert = health_alerts.check_empty_email_recovery(conn, now=NOW)
    assert alert["severity"] == "medium"
    assert alert["detail"]["by_state"]["authorised"] == 3


def test_a_queue_waiting_behind_the_internal_test_is_not_an_alert(conn):
    """It would fire every hour from Friday night to Monday: about sixty identical
    alerts, which is how people learn to ignore the channel."""
    from tgtc_core.reporting import health_alerts
    from tests_core.conftest import NOW

    rec.load_queue(conn, [queue_row(i) for i in range(1, 4)])
    assert rec.internal_test_passed(conn)["passed"] is False
    assert health_alerts.check_empty_email_recovery(conn, now=NOW) is None

    # Harm still speaks, gate or no gate.
    client = FakeInstantly()
    rec.enrol(conn, client, limit=1, env=dict(NO_PACE))          # only the test row moves
    with conn.cursor() as cur:
        cur.execute("UPDATE empty_email_recovery SET state = 'enrolled', "
                    "enrolled_at = now() WHERE email = %s", ("p1@employer.test",))
    conn.commit()
    client.delivered("p1@employer.test", subject="",
                     body={"html": "<div>The Global Talent Co.</div>"})
    rec.record_receipts(conn, client, pace=CountingPace(0))
    alert = health_alerts.check_empty_email_recovery(conn, now=NOW)
    assert alert["severity"] == "high"
    assert alert["detail"]["unapproved_subject_sent"] == 1


def test_the_database_makes_a_roleless_enrolment_impossible(conn):
    """So the alert's roleless counter is a second line, not the protection.

    Worth asserting rather than assuming: the subject is "Your <role> opening", and the
    constraint is what guarantees nobody reaches a sendable state without one.
    """
    rec.load_queue(conn, [queue_row(1)])
    for state in ("authorised", "reserved", "enrolled"):
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute("UPDATE empty_email_recovery SET state = %s, "
                            "verified_role = '' WHERE email = %s",
                            (state, "p1@employer.test"))
        conn.rollback()
    # Only a row nobody will write to may lose its role.
    with conn.cursor() as cur:
        cur.execute("UPDATE empty_email_recovery SET state = 'withheld', "
                    "verified_role = '' WHERE email = %s", ("p1@employer.test",))
    conn.commit()
    assert states(conn)["p1@employer.test"] == "withheld"


def test_an_enrolment_nothing_has_sent_for_three_days_is_surfaced(conn):
    from tgtc_core.reporting import health_alerts
    from tests_core.conftest import NOW

    load(conn, 1)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE), now=NOW - timedelta(hours=96))
    alert = health_alerts.check_empty_email_recovery(conn, now=NOW)
    assert alert["detail"]["enrolled_over_72h_with_no_message"] == 1


# -------------------------------------------------------------- the internal test


def test_no_stranger_reaches_a_campaign_that_can_send_before_we_have_received_it(conn):
    """On 2026-09-21 the 7,777 recipients WERE the test. Not again.

    The rule moved rather than loosened. Loading into a PAUSED campaign is safe, so the
    queue may load; what stays forbidden is somebody unproven sitting in a campaign that
    can email them.
    """
    rec.load_queue(conn, [queue_row(i) for i in range(1, 4)])
    live = FakeInstantly(campaign_status=1)
    report = rec.enrol(conn, live, limit=10, env=dict(NO_PACE))
    assert report["stopped"] == "campaign_is_live_and_the_internal_test_has_not_passed"
    assert report["internal_test"]["passed"] is False
    assert live.created == [] and live.moved == []
    assert set(states(conn).values()) == {"authorised"}


def test_a_paused_campaign_takes_the_test_row_first(conn):
    """Ordering, so the thing that has to be checked is in place before the rest."""
    rec.load_queue(conn, [queue_row(1),
                          dict(queue_row(9), email="qa@tgtc.test", is_internal_test=True)])
    client = FakeInstantly(campaign_status=2)
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 2
    assert client.created[0]["email"] == "qa@tgtc.test", "the test row goes in first"


def test_an_enrolled_test_is_not_a_passed_test(conn):
    """Enrolling is not sending, and sending is not the approved subject arriving."""
    rec.load_queue(conn, [dict(queue_row(9), email="qa@tgtc.test", is_internal_test=True)])
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert rec.internal_test_passed(conn)["passed"] is False

    client.delivered("qa@tgtc.test", subject="wrong subject")
    rec.record_receipts(conn, client, pace=CountingPace(0))
    assert rec.internal_test_passed(conn)["passed"] is False,         "a message with the wrong subject must not open the gate"


def test_the_gate_opens_only_on_a_received_message_with_the_approved_subject(conn):
    """And once it is open, a LIVE campaign may take real recipients."""
    rec.load_queue(conn, [dict(queue_row(9), email="qa@tgtc.test", is_internal_test=True)])
    client = FakeInstantly(campaign_status=2)
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    client.delivered("qa@tgtc.test")
    rec.record_receipts(conn, client, pace=CountingPace(0))
    gate = rec.internal_test_passed(conn)
    assert gate["passed"] is True and gate["address"] == "qa@tgtc.test"

    rec.load_queue(conn, [queue_row(1)])
    client.campaign_status = 1                      # now it can send
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 1
    assert "p1@employer.test" in [c["email"] for c in client.created]


# --------------------------------------------------------------------------- pacing


def test_every_provider_call_in_a_batch_is_paced(conn):
    """Two calls per contact, each one waiting, because the client has no rate handling.

    Unlike the Airtable and Apollo clients, a 429 falls through the Instantly client as a
    plain failure, so an unpaced batch of 150 would spend its attempts on the rate limit.
    """
    load(conn, 3)
    client = FakeInstantly()
    pace = CountingPace()
    report = rec.enrol(conn, client, limit=10, env={rec.ENROL_ENABLED_ENV: "1"}, pace=pace)
    assert report["enrolled"] == 3
    # The first call of all needs no wait; after that, one per create and one per
    # read-back: 3 creates + 3 verifications = 6 calls, 5 waits.
    assert pace.waits == 5


def test_receipts_are_paced_too(conn):
    load(conn, 2)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    pace = CountingPace()
    rec.record_receipts(conn, client, pace=pace)
    assert pace.waits == 1, "one call per enrolment, so one wait after the first"


def test_a_rate_limit_stops_the_batch_and_blames_nobody(conn):
    """It is not a refusal and not this row's fault, so the row stays sendable."""
    load(conn, 4)
    client = FakeInstantly(rate_limited=True)
    report = rec.enrol(conn, client, limit=10, env={rec.ENROL_ENABLED_ENV: "1"}, pace=CountingPace(0))
    assert report["stopped"] == "rate_limited"
    assert report["enrolled"] == 0 and report["failed"] == 0
    assert set(states(conn).values()) == {"authorised"}
    assert sql1(conn, "SELECT count(*) AS n FROM empty_email_recovery "
                      "WHERE last_error = 'rate_limited'") == 1


def test_the_pace_can_be_turned_off_for_a_one_off_run(conn):
    load(conn, 2)
    client = FakeInstantly()
    pace = CountingPace(0)
    rec.enrol(conn, client, limit=10, env={rec.PACE_SECONDS_ENV: "0"}, pace=pace)
    assert pace.waits == 0


# ------------------------------------------------------- moving what already exists

ORIGINAL = "8bfa0769-4b9a-4346-8e93-17ac8b726dce"      # a paused Challenger campaign
HOLD_LIST = "c5eb1163-0061-4125-9e62-aa68a8793f57"      # the incident hold list


def movable(conn, client, i=1, *, kind="campaign", status=1, source=None, role="Tax Accountant"):
    """One authorised recipient who is ALREADY stored in Instantly."""
    email = "p%d@employer.test" % i
    lead_id = client.seed_existing("EXIST%d" % i, email=email,
                                  campaign=(source or ORIGINAL) if kind == "campaign" else "",
                                  status=status)
    rec.load_queue(conn, [dict(queue_row(i, role=role), source_kind=kind,
                               source_id=source or (ORIGINAL if kind == "campaign" else HOLD_LIST),
                               instantly_lead_id=lead_id, source_lead_status=status)])
    return email, lead_id


def test_an_existing_record_is_moved_and_not_created_again(conn):
    """5,892 of the 5,934 are already stored. Creating them again would be the bug."""
    pass_the_internal_test(conn)
    client = FakeInstantly()
    email, lead_id = movable(conn, client, 1)

    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))

    assert report["moved_existing_records"] == 1
    assert report["created_new_records"] == 0
    assert client.created == [], "nothing may be created for somebody already stored"
    assert client.moved[0]["lead"] == lead_id
    assert client.moved[0]["to"] == CAMPAIGN and client.moved[0]["from"] == ORIGINAL
    assert states(conn)[email] == "enrolled"
    # The same record, not a copy.
    assert sqlall(conn, "SELECT instantly_lead_id FROM empty_email_recovery "
                        "WHERE email = %s", (email,))[0]["instantly_lead_id"] == lead_id


def test_a_move_from_a_hold_list_names_the_list_not_a_campaign(conn):
    """/leads/move takes `campaign` or `list_id`, and they are not interchangeable."""
    pass_the_internal_test(conn)
    client = FakeInstantly()
    movable(conn, client, 1, kind="list")
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert client.moved[0]["from_kind"] == "list"
    assert client.moved[0]["from"] == HOLD_LIST


def test_the_role_is_written_before_the_move_never_after(conn):
    """The campaign is ACTIVE. A lead arriving without its role could be sent
    "Your  opening" inside the next window -- the incident, caused by the repair."""
    pass_the_internal_test(conn)
    client = FakeInstantly()
    movable(conn, client, 1)
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert client.patched, "the role must be written"
    assert client.patched[0]["patch"]["custom_variables"]["verified_role"] == "Tax Accountant"
    # Both happened, and the patch came first.
    assert client.patched and client.moved


def test_the_patch_merges_because_the_api_replaces(conn):
    """PATCH custom_variables REPLACES the set, so what the lead already had must be
    sent back with it or it is silently thrown away."""
    pass_the_internal_test(conn)
    client = FakeInstantly()
    email, lead_id = movable(conn, client, 1)
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    sent_variables = client.patched[0]["patch"]["custom_variables"]
    assert sent_variables["verified_role"] == "Tax Accountant"
    assert sent_variables.get("signal_tier") == "keep-me", "existing provenance kept"
    assert rec.lead_variables(client.leads[lead_id])["signal_tier"] == "keep-me"


def test_the_record_is_backed_up_to_the_database_before_it_is_touched(conn):
    """A move has to be reversible from production, not only from a file on a laptop."""
    pass_the_internal_test(conn)
    client = FakeInstantly()
    email, lead_id = movable(conn, client, 1)
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    row = sqlall(conn, "SELECT source_backup, source_lead_status FROM empty_email_recovery "
                       "WHERE email = %s", (email,))[0]
    assert row["source_backup"]["id"] == lead_id
    assert row["source_backup"]["campaign"] == ORIGINAL, "where it came from"
    assert row["source_backup"]["status_summary"] == {"step": 1}, "and where it had got to"
    assert row["source_lead_status"] == 1


def test_their_old_sequence_is_abandoned_and_they_are_never_moved_back(conn):
    """Authorised, and the point: this one email and nothing else."""
    pass_the_internal_test(conn)
    client = FakeInstantly()
    email, lead_id = movable(conn, client, 1)
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert client.leads[lead_id]["status_summary"] == {}, "the position is gone"
    assert client.leads[lead_id]["campaign"] == CAMPAIGN

    # A second tick must not move them anywhere, least of all back.
    again = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert again["enrolled"] == 0
    assert len(client.moved) == 1
    assert ORIGINAL not in [m["to"] for m in client.moved]


@pytest.mark.parametrize("status,reason", [(-1, "provider_bounced"),
                                           (-2, "provider_unsubscribed")])
def test_the_providers_own_verdict_on_a_lead_withholds_it(conn, status, reason):
    """246 of the authorised recipients turned out to be BOUNCED in Instantly.

    Our outcome_events did not know: the bounce was recorded by the provider, not by us.
    The first email never arrived at those addresses, and writing again spends our own
    deliverability for nothing.
    """
    pass_the_internal_test(conn)
    client = FakeInstantly()
    email, _ = movable(conn, client, 1, status=status)
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 0 and report["withheld"] == 1
    assert client.moved == [] and client.patched == []
    row = sqlall(conn, "SELECT state, state_reason FROM empty_email_recovery "
                       "WHERE email = %s", (email,))[0]
    assert row["state"] == "withheld" and row["state_reason"] == reason


def test_a_lead_id_that_belongs_to_somebody_else_is_withheld(conn):
    """The one way a move could email the wrong person. It is refused, not retried."""
    pass_the_internal_test(conn)
    client = FakeInstantly()
    client.seed_existing("EXIST1", email="someone@else.test", campaign=ORIGINAL)
    rec.load_queue(conn, [dict(queue_row(1), source_kind="campaign", source_id=ORIGINAL,
                               instantly_lead_id="EXIST1")])
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["withheld"] == 1 and client.moved == []
    assert sqlall(conn, "SELECT state_reason FROM empty_email_recovery WHERE email = %s",
                  ("p1@employer.test",))[0]["state_reason"] == "lead_id_belongs_to_another_address"


def test_a_record_rotation_removed_goes_back_to_the_queue_as_a_creation(conn):
    """A route can go stale between being recorded and being used."""
    pass_the_internal_test(conn)
    client = FakeInstantly()
    rec.load_queue(conn, [dict(queue_row(1), source_kind="campaign", source_id=ORIGINAL,
                               instantly_lead_id="GONE")])
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 0
    row = sqlall(conn, "SELECT state, last_error FROM empty_email_recovery "
                       "WHERE email = %s", ("p1@employer.test",))[0]
    assert row["state"] == "authorised"
    assert row["last_error"] == "record_gone_needs_creation"


def test_a_move_that_does_not_settle_is_not_an_enrolment(conn):
    """/leads/move answers 200 with a PENDING job. Accepted is not done."""
    pass_the_internal_test(conn)
    client = FakeInstantly(move_never_settles=True)
    email, _ = movable(conn, client, 1)
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 0
    row = sqlall(conn, "SELECT state, last_error FROM empty_email_recovery "
                       "WHERE email = %s", (email,))[0]
    assert row["state"] == "authorised" and row["last_error"] == "move_not_settled"


def test_a_failed_move_or_patch_leaves_them_sendable(conn):
    pass_the_internal_test(conn)
    failing_move = FakeInstantly(move_fails=True)
    email, _ = movable(conn, failing_move, 1)
    rec.enrol(conn, failing_move, limit=10, env=dict(NO_PACE))
    assert sqlall(conn, "SELECT state, last_error FROM empty_email_recovery "
                        "WHERE email = %s", (email,))[0]["state"] == "authorised"

    failing_patch = FakeInstantly(patch_fails=True)
    email2, _ = movable(conn, failing_patch, 2)
    rec.enrol(conn, failing_patch, limit=10, env=dict(NO_PACE))
    row = sqlall(conn, "SELECT state, last_error FROM empty_email_recovery "
                       "WHERE email = %s", (email2,))[0]
    assert row["state"] == "authorised" and row["last_error"].startswith("patch_failed")


# ----------------------------------------------------- capacity, and what it bounds


def test_a_full_workspace_still_moves_everybody_it_can(conn):
    """The correction this measurement forced: a move needs no slot, so a tight
    workspace must not stop 5,892 of the 5,934."""
    pass_the_internal_test(conn)
    client = FakeInstantly(stored=24999)        # nothing free at all
    email, _ = movable(conn, client, 1)
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["moved_existing_records"] == 1
    assert report["capacity"]["measured"] is None, "it was not even consulted"
    assert states(conn)[email] == "enrolled"


def test_only_a_creation_waits_for_a_slot(conn):
    pass_the_internal_test(conn)
    client = FakeInstantly(stored=24999)
    movable(conn, client, 1)                                        # needs no slot
    rec.load_queue(conn, [dict(queue_row(2), source_kind="absent")])  # needs one
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["moved_existing_records"] == 1
    assert report["created_new_records"] == 0
    assert report["held_for_storage"] == 1
    assert states(conn)["p2@employer.test"] == "authorised"


def test_the_two_routes_are_counted_separately_and_never_confused(conn):
    pass_the_internal_test(conn)
    client = FakeInstantly()
    movable(conn, client, 1)
    movable(conn, client, 2, kind="list")
    rec.load_queue(conn, [dict(queue_row(3), source_kind="absent")])
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["moved_existing_records"] == 2
    assert report["created_new_records"] == 1
    assert report["enrolled"] == 3
    assert len(client.created) == 1, "exactly one new record, for the one with none"
    assert len(client.moved) == 2


def test_a_route_is_only_set_on_somebody_still_waiting(conn):
    rec.load_queue(conn, [queue_row(1), queue_row(2)])
    with conn.cursor() as cur:
        cur.execute("UPDATE empty_email_recovery SET state = 'sent', sent_at = now() "
                    "WHERE email = 'p1@employer.test'")
    conn.commit()
    out = rec.set_route(conn, [
        {"email": "p1@employer.test", "source_kind": "campaign", "source_id": ORIGINAL,
         "instantly_lead_id": "X1"},
        {"email": "p2@employer.test", "source_kind": "campaign", "source_id": ORIGINAL,
         "instantly_lead_id": "X2"},
        {"email": "p3@employer.test", "source_kind": "campaign"},      # no lead id
    ])
    assert out == {"routed": 1, "not_waiting_any_more": 1, "refused": 1}
    assert sqlall(conn, "SELECT source_kind FROM empty_email_recovery "
                        "WHERE email = %s", ("p1@employer.test",))[0]["source_kind"] == "unknown"


def test_a_claimed_move_route_must_name_its_lead_and_source(conn):
    """The database refuses a route that cannot be executed."""
    rec.load_queue(conn, [queue_row(1)])
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("UPDATE empty_email_recovery SET source_kind = 'campaign' "
                        "WHERE email = 'p1@employer.test'")
    conn.rollback()


# ------------------------------------------------- the gate asks for the whole email


def test_a_send_record_alone_does_not_open_the_gate(conn):
    """Neither does a matching subject. The incident was a correct-looking send."""
    rec.load_queue(conn, [dict(queue_row(0), email="qa@tgtc.test", is_internal_test=True)])
    client = FakeInstantly()
    rec.enrol(conn, client, limit=1, env=dict(NO_PACE))

    # A message exists and its subject is exactly right, but the body is the incident.
    client.delivered("qa@tgtc.test",
                     body={"html": "<div>Devan Markus</div><div>The Global Talent Co.</div>"})
    rec.record_receipts(conn, client, pace=CountingPace(0))
    gate = rec.internal_test_passed(conn)
    assert gate["passed"] is False
    assert "body_arrived_at_all" in gate["failed"]
    assert "every_approved_sentence_is_there" in gate["failed"]


def test_a_pending_variable_in_the_internal_email_keeps_the_gate_shut(conn):
    rec.load_queue(conn, [dict(queue_row(0), email="qa@tgtc.test", is_internal_test=True)])
    client = FakeInstantly()
    rec.enrol(conn, client, limit=1, env=dict(NO_PACE))
    message = client.delivered("qa@tgtc.test")
    message["body"]["html"] = message["body"]["html"].replace("Hi Ana,", "Hi {{firstName}},")
    rec.record_receipts(conn, client, pace=CountingPace(0))
    gate = rec.internal_test_passed(conn)
    assert gate["passed"] is False
    assert "no_variable_is_still_pending" in gate["failed"]


def test_an_email_that_went_to_the_wrong_address_keeps_the_gate_shut(conn):
    rec.load_queue(conn, [dict(queue_row(0), email="qa@tgtc.test", is_internal_test=True)])
    client = FakeInstantly()
    rec.enrol(conn, client, limit=1, env=dict(NO_PACE))
    client.delivered("qa@tgtc.test", lead="someone@else.test",
                     to_address_email_list="someone@else.test")
    rec.record_receipts(conn, client, pace=CountingPace(0))
    assert "went_to_the_right_person" in rec.internal_test_passed(conn)["failed"]


def test_the_whole_received_email_is_kept_as_evidence(conn):
    """So a human can read what actually arrived, not a boolean about it."""
    client = pass_the_internal_test(conn)
    row = sqlall(conn, "SELECT evidence FROM empty_email_recovery WHERE is_internal_test")[0]
    checks = row["evidence"]["received_checks"]
    assert all(checks.values()), checks
    assert "An earlier email from us went out without its message" in row["evidence"]["received_body"]
    assert row["evidence"]["sent_subject"] == "Your Tax Accountant opening"


# ------------------------------------------------ loading into a campaign that is paused


def test_enrolment_is_off_unless_somebody_turns_it_on(conn):
    """The handover state: the queue is loaded, the campaign is paused, and whether to
    test and activate belongs to the team rather than to a cron."""
    rec.load_queue(conn, [queue_row(1)])
    client = FakeInstantly()
    report = rec.enrol(conn, client, limit=10, env={rec.PACE_SECONDS_ENV: "0"})
    assert report["stopped"] == "enrolment_disabled"
    assert report["enrolled"] == 0
    assert client.created == [] and client.moved == []


def test_a_paused_campaign_is_what_lets_the_whole_queue_load(conn):
    """Enrolling into a paused campaign cannot email anybody: the lead sits there with
    its copy ready. So the internal test is not required to LOAD, only to send."""
    client = FakeInstantly(campaign_status=2)
    rec.load_queue(conn, [queue_row(i) for i in range(1, 4)])
    assert rec.internal_test_passed(conn)["passed"] is False

    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))

    assert report["enrolled"] == 3
    assert report["campaign"] == {"known": True, "paused": True, "status": 2}
    assert report["internal_test"]["passed"] is False


def test_a_live_campaign_without_a_proven_test_enrols_nobody(conn):
    """The original protection, kept: the only way somebody unproven reaches a campaign
    that can send is if this refuses to happen."""
    client = FakeInstantly(campaign_status=1)
    rec.load_queue(conn, [queue_row(i) for i in range(1, 4)])
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["stopped"] == "campaign_is_live_and_the_internal_test_has_not_passed"
    assert report["enrolled"] == 0
    assert client.created == [] and client.moved == []
    assert set(states(conn).values()) == {"authorised"}


def test_a_live_campaign_with_a_proven_test_may_enrol(conn):
    client = FakeInstantly(campaign_status=1)
    pass_the_internal_test(conn)
    rec.load_queue(conn, [queue_row(1)])
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 1
    assert report["campaign"]["paused"] is False


def test_a_campaign_whose_status_cannot_be_read_enrols_nobody(conn):
    """Not knowing whether it can send is not the same as knowing it cannot."""
    client = FakeInstantly(campaign_unreadable=True)
    rec.load_queue(conn, [queue_row(1)])
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["stopped"] == "campaign_is_live_and_the_internal_test_has_not_passed"
    assert report["campaign"]["known"] is False
    assert client.created == [] and client.moved == []


def test_the_pace_is_the_measured_one_not_an_assumed_one(conn):
    """Measured 2026-10-04: 60 unpaced GETs in 21.1s, 171 a minute, zero 429s. The 3
    seconds this used to wait was a guess at "20 a minute" and cost a factor of eight."""
    assert rec.DEFAULT_PACE_SECONDS == 0.5
    assert 60 / rec.DEFAULT_PACE_SECONDS == 120, "a 30% margin under what was observed"
    # A batch still has to fit the tick that runs it: four calls per move.
    assert rec.DEFAULT_BATCH * 4 * rec.DEFAULT_PACE_SECONDS <= 15 * 60


def test_a_lead_already_at_its_destination_is_not_moved_again(conn):
    """What makes an interrupted load resumable instead of stuck.

    A container replaced mid-batch leaves a row `reserved` whose lead has already moved.
    Asking for the move again would be asking to move it out of a campaign it has already
    left: that fails, and the row would be parked forever.
    """
    pass_the_internal_test(conn)
    client = FakeInstantly()
    email, lead_id = movable(conn, client, 1)
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert len(client.moved) == 1 and states(conn)[email] == "enrolled"

    # Walk it back to `reserved`, as an interrupted batch would leave it, and re-run.
    with conn.cursor() as cur:
        cur.execute("UPDATE empty_email_recovery SET state = 'reserved' WHERE email = %s",
                    (email,))
    conn.commit()
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))

    assert report["moved_existing_records"] == 1, "it finishes rather than failing"
    assert len(client.moved) == 1, "and it does not ask for the move a second time"
    assert states(conn)[email] == "enrolled"


def test_the_wait_for_a_move_backs_off_instead_of_a_flat_three_seconds(conn):
    """Measured during the load: a flat 3s pinned it at 14 a minute while the rate limit
    allowed 120, because the job usually lands inside a second."""
    waits = []

    class Slow(FakeInstantly):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.reads = 0

        def get_lead(self, lead_id):
            self.reads += 1
            # Lands on the third read, as a pending job does.
            if lead_id in self.leads and self.reads < 3:
                return Result(data={**self.leads[lead_id], "campaign": "elsewhere"})
            return super().get_lead(lead_id)

    client = Slow()
    # Where the move will have put it; the first reads still report the old campaign,
    # which is what a pending background job looks like from outside.
    client.seed_existing("EX1", email="p1@employer.test", campaign=CAMPAIGN)
    settled = rec._settle(client, "EX1", want_campaign=CAMPAIGN, sleep=waits.append)
    assert str(settled.get("campaign")) == CAMPAIGN
    assert waits == [0.5, 1.0], "half a second, then one, then it was there"
    assert all(w <= 8.0 for w in waits)
