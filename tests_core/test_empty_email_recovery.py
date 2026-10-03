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
#: The real 3-second pace is asserted by its own tests below. Everywhere else it would
#: only make the suite slow, so it is off and said so rather than inherited.
NO_PACE = {rec.PACE_SECONDS_ENV: "0"}


class Result:
    def __init__(self, ok=True, status=200, data=None, message="", uncertain=False):
        self.ok, self.status, self.message, self.uncertain = ok, status, message, uncertain
        self.data = data if data is not None else {}


class FakeInstantly:
    """Enough of the client to exercise the paths, and it records what it was asked."""

    def __init__(self, *, stored=1000, allowance_error=False, emails_sent=0,
                 drop_variables=False, subject=None, on_lists=0, rate_limited=False):
        self.rate_limited = rate_limited
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
        lead_id = "L%d" % (len(self.created) + 1)
        self.created.append(dict(payload))
        variables = {} if self.drop_variables else dict(payload["custom_variables"])
        self.leads[lead_id] = {"id": lead_id, "email": payload["email"],
                               "campaign": payload["campaign"],
                               "payload": {"custom_variables": variables,
                                           "first_name": payload["first_name"]}}
        return Result(data={"id": lead_id})

    def get_lead(self, lead_id):
        if lead_id not in self.leads:
            return Result(False, 404, message="not found")
        return Result(data=self.leads[lead_id])

    # --- receipts
    def emails_for(self, email, *, campaign_id="", limit=50):
        if email in self.messages:
            return Result(data={"items": [self.messages[email]]})
        return Result(data={"items": []})

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


def pass_the_internal_test(conn):
    """A received test message with the approved subject: what the gate asks for."""
    rec.load_queue(conn, [dict(queue_row(0), email="qa@tgtc.test", is_internal_test=True)])
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE empty_email_recovery SET state = 'sent', sent_at = now(), "
            "message_id = 'TEST', evidence = evidence || "
            "'{\"sent_subject\": \"Your Tax Accountant opening\", "
            "  \"subject_as_approved\": true}'::jsonb "
            "WHERE email = 'qa@tgtc.test'")
    conn.commit()


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
    assert rec.record_receipts(conn, client)["sent"] == 0
    assert set(states(conn).values()) == {"enrolled"}

    client.messages["p1@employer.test"] = {
        "message_id": "M1", "subject": "Your Tax Accountant opening",
        "from_address_email": "rep@tgtc.test"}
    report = rec.record_receipts(conn, client)
    assert report["sent"] == 1 and report["subject_not_as_approved_count"] == 0
    row = sqlall(conn, "SELECT state, message_id, evidence FROM empty_email_recovery "
                       "WHERE email = 'p1@employer.test'")[0]
    assert row["state"] == "sent" and row["message_id"] == "M1"
    assert row["evidence"]["subject_as_approved"] is True
    assert states(conn)["p2@employer.test"] == "enrolled"


def test_a_blank_subject_that_got_out_is_recorded_and_pauses_the_campaign(conn):
    """The incident itself, as a test: our ledger must show it, not only an inbox."""
    load(conn, 1)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    client.messages["p1@employer.test"] = {"message_id": "M1", "subject": ""}
    report = rec.record_receipts(conn, client)
    assert report["subject_not_as_approved_count"] == 1

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
    client.messages["p1@employer.test"] = {"message_id": "M1",
                                           "subject": "Your Tax Accountant opening"}
    rec.record_receipts(conn, client)
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
    client.messages["p1@employer.test"] = {"message_id": "M1",
                                           "subject": "Your Tax Accountant opening"}
    rec.record_receipts(conn, client)
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
    client.messages["p2@employer.test"] = {"message_id": "M1",
                                           "subject": "Your Tax Accountant opening"}
    rec.record_receipts(conn, client)
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
    client.messages["p1@employer.test"] = {"message_id": "M1",
                                           "subject": "Your Tax Accountant opening"}
    rec.record_receipts(conn, client)
    healthy = health_alerts.check_empty_email_recovery(conn, now=NOW)
    assert healthy is None or healthy["severity"] != "high"

    client.messages["p2@employer.test"] = {"message_id": "M2", "subject": ""}
    rec.record_receipts(conn, client)
    alert = health_alerts.check_empty_email_recovery(conn, now=NOW)
    assert alert["severity"] == "high"
    assert alert["detail"]["unapproved_subject_sent"] == 1


def test_a_queue_that_has_stopped_moving_is_visible_without_being_an_emergency(conn):
    from tgtc_core.reporting import health_alerts
    from tests_core.conftest import NOW

    load(conn, 3)
    alert = health_alerts.check_empty_email_recovery(conn, now=NOW)
    assert alert["severity"] == "medium"
    assert alert["detail"]["by_state"]["authorised"] == 3


def test_an_enrolment_nothing_has_sent_for_three_days_is_surfaced(conn):
    from tgtc_core.reporting import health_alerts
    from tests_core.conftest import NOW

    load(conn, 1)
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE), now=NOW - timedelta(hours=96))
    alert = health_alerts.check_empty_email_recovery(conn, now=NOW)
    assert alert["detail"]["enrolled_over_72h_with_no_message"] == 1


# -------------------------------------------------------------- the internal test


def test_no_stranger_is_enrolled_before_one_of_our_own_has_received_it(conn):
    """On 2026-09-21 the 7,777 recipients WERE the test. Not again."""
    rec.load_queue(conn, [queue_row(i) for i in range(1, 4)])
    client = FakeInstantly()
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["stopped"] == "awaiting_internal_test"
    assert report["internal_test"]["passed"] is False
    assert client.created == []
    assert set(states(conn).values()) == {"authorised"}


def test_the_internal_test_itself_is_enrolled_while_the_gate_is_shut(conn):
    rec.load_queue(conn, [queue_row(1),
                          dict(queue_row(9), email="qa@tgtc.test", is_internal_test=True)])
    client = FakeInstantly()
    report = rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert report["enrolled"] == 1
    assert [c["email"] for c in client.created] == ["qa@tgtc.test"]
    assert states(conn)["p1@employer.test"] == "authorised"


def test_an_enrolled_test_is_not_a_passed_test(conn):
    """Enrolling is not sending, and sending is not the approved subject arriving."""
    rec.load_queue(conn, [dict(queue_row(9), email="qa@tgtc.test", is_internal_test=True)])
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    assert rec.internal_test_passed(conn)["passed"] is False

    client.messages["qa@tgtc.test"] = {"message_id": "T1", "subject": "wrong subject"}
    rec.record_receipts(conn, client)
    assert rec.internal_test_passed(conn)["passed"] is False,         "a message with the wrong subject must not open the gate"


def test_the_gate_opens_only_on_a_received_message_with_the_approved_subject(conn):
    rec.load_queue(conn, [queue_row(1),
                          dict(queue_row(9), email="qa@tgtc.test", is_internal_test=True)])
    client = FakeInstantly()
    rec.enrol(conn, client, limit=10, env=dict(NO_PACE))
    client.messages["qa@tgtc.test"] = {"message_id": "T1",
                                       "subject": "Your Tax Accountant opening"}
    rec.record_receipts(conn, client)
    gate = rec.internal_test_passed(conn)
    assert gate["passed"] is True and gate["address"] == "qa@tgtc.test"

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
    report = rec.enrol(conn, client, limit=10, env={}, pace=pace)
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
    report = rec.enrol(conn, client, limit=10, env={}, pace=CountingPace(0))
    assert report["stopped"] == "rate_limited"
    assert report["enrolled"] == 0 and report["failed"] == 0
    assert set(states(conn).values()) == {"authorised"}
    assert sql1(conn, "SELECT count(*) AS n FROM empty_email_recovery "
                      "WHERE last_error = 'rate_limited'") == 1


def test_the_batch_is_sized_for_the_hour_it_runs_in(conn):
    """150 contacts x 2 paced calls is about 15 minutes, inside an hourly tick."""
    assert rec.DEFAULT_BATCH * 2 * rec.DEFAULT_PACE_SECONDS <= 20 * 60
    assert 60 / rec.DEFAULT_PACE_SECONDS == 20, "the documented requests per minute"


def test_the_pace_can_be_turned_off_for_a_one_off_run(conn):
    load(conn, 2)
    client = FakeInstantly()
    pace = CountingPace(0)
    rec.enrol(conn, client, limit=10, env={rec.PACE_SECONDS_ENV: "0"}, pace=pace)
    assert pace.waits == 0
