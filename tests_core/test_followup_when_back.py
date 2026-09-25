"""The return date arrives. Who gets followed up, and who must never be.

Production on 2026-09-25 had 41 of these queued and nothing had ever looked at one. The
rule this holds: an out-of-office is not an opt-out, an opt-out is not a follow-up, and
a vacancy that has gone is not a reason to write to anybody.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tgtc_core.services import followups
from tgtc_core.services.followups import NEEDS_MECHANISM, NO_CURRENT_VACANCY, WORK_KIND
from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, good_buyer, seed_opportunity


def approve(conn, clock, *, i=0):
    domain = f"acme{i}.com"
    pid, eid, oid = seed_opportunity(conn, clock, domain=domain, org_name=f"Acme {i}", job_id=f"job-{i}",
                                     function_key="customer_success")
    fake = apollo_for(domain, f"Acme {i}", people=[good_buyer(domain, f"Acme {i}", id=f"p-good-{i}")])
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "approved", out.reason
    return out.approval_id


def queue_followup(conn, approval_id, when):
    conn.execute("INSERT INTO work_items (kind, subject_kind, subject_id, state, available_at, waiting_on) "
                 "VALUES (%s, 'approval', %s, 'ready', %s, 'out_of_office')", (WORK_KIND, approval_id, when))
    conn.commit()


def item(conn, approval_id):
    return sqlall(conn, "SELECT state, coalesce(close_reason,'') reason, coalesce(waiting_on,'') waiting_on, "
                        "available_at FROM work_items WHERE kind = %s AND subject_id = %s",
                  (WORK_KIND, approval_id))[0]


def email_of(conn, approval_id):
    return str(sql1(conn, "SELECT p.email FROM approvals a JOIN people p ON p.id = a.person_id WHERE a.id = %s",
                    (approval_id,))).lower()


def test_a_follow_up_that_is_not_due_yet_is_left_alone(conn, clock):
    approval_id = approve(conn, clock)
    queue_followup(conn, approval_id, clock() + timedelta(days=5))

    assert followups.due_followups(conn, now=clock())["considered"] == 0
    assert item(conn, approval_id)["state"] == "ready"


def test_a_due_follow_up_with_everything_still_true_is_verified_and_held(conn, clock):
    """It is held, not sent: Instantly has no way to resume a finished sequence, and
    saying otherwise would be a follow-up that never happens."""
    approval_id = approve(conn, clock)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))

    out = followups.due_followups(conn, now=clock())

    assert (out["considered"], out["verified_due"], out["moved_to_followup"]) == (1, 1, 0)
    row = item(conn, approval_id)
    assert row["state"] == "waiting" and row["waiting_on"] == NEEDS_MECHANISM
    assert row["available_at"] > clock(), "it is re-examined later, not churned every run"


def test_an_opt_out_closes_the_follow_up_and_is_never_written_to(conn, clock):
    approval_id = approve(conn, clock)
    conn.execute("INSERT INTO suppressions (kind, key, reason, source) VALUES ('person_email', %s, %s, %s)",
                 (email_of(conn, approval_id), "opt_out", "self:reply"))
    conn.commit()
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))

    out = followups.due_followups(conn, now=clock())

    assert out["suppressed"] == 1 and out["verified_due"] == 0
    row = item(conn, approval_id)
    assert row["state"] == "closed" and row["reason"].startswith("suppressed:")


def test_a_departure_recorded_later_also_closes_the_follow_up(conn, clock):
    """The same person can send an out-of-office in September and be gone in October."""
    approval_id = approve(conn, clock)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    conn.execute("INSERT INTO suppressions (kind, key, reason, source) VALUES ('person_email', %s, %s, %s)",
                 (email_of(conn, approval_id), "no_longer_here", "self:reply"))
    conn.commit()

    followups.due_followups(conn, now=clock())

    assert item(conn, approval_id)["state"] == "closed"


def test_a_vacancy_that_has_gone_closes_the_follow_up(conn, clock):
    approval_id = approve(conn, clock)
    conn.execute("UPDATE postings SET state = 'expired' WHERE id = "
                 "(SELECT (lead_json->>'posting_id')::bigint FROM approvals WHERE id = %s)", (approval_id,))
    conn.commit()
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))

    out = followups.due_followups(conn, now=clock())

    assert out["no_current_vacancy"] == 1
    assert item(conn, approval_id)["reason"].startswith(NO_CURRENT_VACANCY)


def test_a_revoked_approval_closes_the_follow_up(conn, clock):
    approval_id = approve(conn, clock)
    conn.execute("UPDATE approvals SET state = 'revoked' WHERE id = %s", (approval_id,))
    conn.commit()
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))

    followups.due_followups(conn, now=clock())

    assert item(conn, approval_id)["state"] == "closed"


def test_it_is_bounded_and_does_not_reprocess(conn, clock):
    ids = []
    for i in range(4):
        approval_id = approve(conn, clock, i=i)
        queue_followup(conn, approval_id, clock() - timedelta(hours=1))
        ids.append(approval_id)

    first = followups.due_followups(conn, now=clock(), limit=2)
    assert first["considered"] == 2
    second = followups.due_followups(conn, now=clock(), limit=10)
    assert second["considered"] == 2, "the two already decided were not looked at again"
    assert all(item(conn, a)["state"] == "waiting" for a in ids)


def test_zero_per_run_does_nothing(conn, clock):
    approval_id = approve(conn, clock)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    assert followups.due_followups(conn, now=clock(), limit=0)["considered"] == 0
    assert item(conn, approval_id)["state"] == "ready"


def test_the_ceiling_is_configurable_and_never_negative():
    assert followups.per_run({}) == followups.DEFAULT_PER_RUN
    assert followups.per_run({"TGTC_FOLLOWUPS_PER_RUN": "5"}) == 5
    assert followups.per_run({"TGTC_FOLLOWUPS_PER_RUN": "-2"}) == 0
    assert followups.per_run({"TGTC_FOLLOWUPS_PER_RUN": "rubbish"}) == followups.DEFAULT_PER_RUN


# --- the follow-up that actually happens ------------------------------------------
FOLLOWUP_CAMPAIGN = "f0110000-0000-0000-0000-00000000f01d"


def instantly_client(fake):
    from tgtc_core.providers.instantly import InstantlyClient

    return InstantlyClient(fake, base_url="https://api.instantly.ai/api/v2", api_key="sim")


def record_creation(conn, approval_id, lead_id, campaign):
    """What delivery writes when Instantly genuinely creates the lead. The approval flow
    already made the outbox row, so this only adds the receipt to it."""
    outbox_id = sql1(conn, "SELECT id FROM delivery_outbox WHERE approval_id = %s AND channel = 'instantly'",
                     (approval_id,))
    conn.execute("UPDATE delivery_outbox SET state = 'delivered' WHERE id = %s", (outbox_id,))
    conn.execute("INSERT INTO delivery_receipts (outbox_id, channel, receipt_kind, external_id, external_campaign) "
                 "VALUES (%s, 'instantly', 'created', %s, %s)", (outbox_id, lead_id, campaign))
    conn.commit()


def test_a_due_follow_up_moves_the_contact_into_the_one_step_campaign(conn, clock):
    """One further message, not the four they already received."""
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    lead_id, source = "L-1", "269cd138-00b1-48c3-9093-16c36120a20e"
    record_creation(conn, approval_id, lead_id, source)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock)
    fake.leads["x@acme0.com"] = {"id": lead_id, "email": "x@acme0.com", "campaign": source, "status": 3}

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN})

    assert out["moved_to_followup"] == 1 and out["verified_due"] == 0
    assert fake.moved == [{"ids": [lead_id], "from": source, "to": FOLLOWUP_CAMPAIGN}]
    assert fake.leads["x@acme0.com"]["campaign"] == FOLLOWUP_CAMPAIGN
    # Moved is not sent. The email is the one-step campaign's to send, in its own
    # window, so this waits for a receipt instead of calling itself finished.
    assert out["awaiting_send"] == 1 and out["followup_sent"] == 0
    row = item(conn, approval_id)
    assert row["state"] == "waiting" and row["waiting_on"] == followups.AWAITING_SEND
    moved = sqlall(conn, "SELECT lead_id, to_campaign, sent_at FROM followup_deliveries")
    assert moved == [{"lead_id": lead_id, "to_campaign": FOLLOWUP_CAMPAIGN, "sent_at": None}]


def test_the_same_follow_up_never_moves_twice(conn, clock):
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    source = "269cd138-00b1-48c3-9093-16c36120a20e"
    record_creation(conn, approval_id, "L-2", source)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock)
    fake.leads["x@acme0.com"] = {"id": "L-2", "email": "x@acme0.com", "campaign": source, "status": 3}
    env = {followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN}

    followups.due_followups(conn, now=clock(), instantly=instantly_client(fake), env=env)
    again = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake), env=env)

    assert again["considered"] == 0, "the item was done, so it was not looked at again"
    assert len(fake.moved) == 1


def test_a_suppressed_contact_is_never_moved(conn, clock):
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    record_creation(conn, approval_id, "L-3", "269cd138-00b1-48c3-9093-16c36120a20e")
    conn.execute("INSERT INTO suppressions (kind, key, reason, source) VALUES ('person_email', %s, %s, %s)",
                 (email_of(conn, approval_id), "opt_out", "self:reply"))
    conn.commit()
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock)

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN})

    assert out["suppressed"] == 1 and out["moved_to_followup"] == 0
    assert fake.moved == [], "an opt-out was moved into a campaign"


def test_the_destination_can_never_be_a_challenger_or_control_campaign():
    """Moving somebody there would restart a four-step sequence, which is the one thing
    this must not do."""
    for bad in ("269cd138-00b1-48c3-9093-16c36120a20e", "1747c87e-12e9-4477-bc4d-048223d39513"):
        with pytest.raises(ValueError, match="dedicated one-step campaign"):
            followups.followup_campaign({followups.FOLLOWUP_CAMPAIGN_ENV: bad})
    assert followups.followup_campaign({followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN}) == FOLLOWUP_CAMPAIGN
    assert followups.followup_campaign({}) == ""


def test_without_a_recorded_creation_there_is_nobody_to_move(conn, clock):
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock)

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN})

    assert out["no_recorded_creation"] == 1 and fake.moved == []
    assert item(conn, approval_id)["state"] == "closed"


def test_a_refused_move_waits_instead_of_losing_the_follow_up(conn, clock):
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    source = "269cd138-00b1-48c3-9093-16c36120a20e"
    record_creation(conn, approval_id, "L-5", source)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))

    class Refusing(FakeInstantly):
        def request(self, method, url, **kw):
            if url.endswith("/leads/move"):
                from tgtc_core.providers.http import Response
                return Response(status=503, headers={}, text='{"error":"unavailable"}')
            return super().request(method, url, **kw)

    refusing = Refusing(clock=clock)
    refusing.leads["x@acme0.com"] = {"id": "L-5", "email": "x@acme0.com", "campaign": source, "status": 3}
    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(refusing),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN})

    assert out["move_failed"] == 1 and out["moved_to_followup"] == 0
    row = item(conn, approval_id)
    assert row["state"] == "waiting" and row["waiting_on"] == followups.MOVE_FAILED


# --- what the live probe on 2026-09-25 taught -------------------------------------
# POST /leads/move answers 200 with a background job ("status": "pending"), so the
# response says accepted, not done. One real contact was moved that way and the move
# had not landed when the call returned.


def test_a_move_that_lands_a_moment_later_is_confirmed_in_the_same_run(conn, clock):
    """Measured 2026-09-25: the job takes about twenty seconds, and checking immediately
    left four of five follow-ups unconfirmed for a day when they had in fact moved."""
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    source = "269cd138-00b1-48c3-9093-16c36120a20e"
    record_creation(conn, approval_id, "L-13", source)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    who = email_of(conn, approval_id)
    fake = FakeInstantly(clock=clock, move_is_async=True)
    fake.leads[who] = {"id": "L-13", "email": who, "campaign": source, "status": 3}

    rounds = []

    def settle(_seconds):
        rounds.append(1)
        if len(rounds) == 2:                       # it lands during the second wait
            fake.leads[who]["campaign"] = FOLLOWUP_CAMPAIGN

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN},
                                  sleep=settle)

    assert out["moved_to_followup"] == 1 and out["move_unconfirmed"] == 0
    assert len(fake.moved) == 1, "waiting for a job to settle must not re-issue it"
    assert item(conn, approval_id)["waiting_on"] == followups.AWAITING_SEND
    assert len(rounds) <= followups.CONFIRM_ROUNDS


def test_a_move_that_never_lands_is_bounded_and_left_for_the_next_run(conn, clock):
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    source = "269cd138-00b1-48c3-9093-16c36120a20e"
    record_creation(conn, approval_id, "L-6", source)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock, move_is_async=True)
    fake.leads["x@acme0.com"] = {"id": "L-6", "email": "x@acme0.com", "campaign": source, "status": 3}
    waits = []

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN},
                                  sleep=waits.append)
    assert len(waits) == followups.CONFIRM_ROUNDS - 1, "it must stop trying, not spin"

    assert out["move_unconfirmed"] == 1 and out["moved_to_followup"] == 0
    row = item(conn, approval_id)
    assert row["state"] == "waiting", "a 200 was treated as a delivered follow-up"
    assert row["waiting_on"] == followups.MOVE_UNCONFIRMED, "the wait does not say what it is waiting for"
    assert fake.leads["x@acme0.com"]["campaign"] == source


def test_the_next_run_confirms_a_move_that_settled_and_does_not_repeat_it(conn, clock):
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    source = "269cd138-00b1-48c3-9093-16c36120a20e"
    record_creation(conn, approval_id, "L-7", source)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock, move_is_async=True)
    fake.leads["x@acme0.com"] = {"id": "L-7", "email": "x@acme0.com", "campaign": source, "status": 3}
    env = {followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN}

    followups.due_followups(conn, now=clock(), instantly=instantly_client(fake), env=env,
                            sleep=lambda _s: None)
    fake.leads["x@acme0.com"]["campaign"] = FOLLOWUP_CAMPAIGN          # the job finished
    out = followups.due_followups(conn, now=clock() + timedelta(days=2),
                                  instantly=instantly_client(fake), env=env)

    assert len(fake.moved) == 1, "it moved somebody who was already there"
    assert out["awaiting_send"] == 1, "the settled move was noticed without repeating it"
    row = item(conn, approval_id)
    assert row["state"] == "waiting" and row["waiting_on"] == followups.AWAITING_SEND


def test_a_contact_instantly_no_longer_has_is_closed_rather_than_moved(conn, clock):
    """Rotation removes finished contacts. There is then nobody to follow up."""
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    record_creation(conn, approval_id, "L-8", "269cd138-00b1-48c3-9093-16c36120a20e")
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock)                                   # holds no leads at all

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN})

    assert out["lead_gone"] == 1 and fake.moved == []
    row = item(conn, approval_id)
    assert row["state"] == "closed" and row["reason"] == "lead_no_longer_in_instantly"


def test_the_move_starts_from_where_instantly_says_the_contact_is_now(conn, clock):
    """Our receipt records where we created them, which is not always where they are."""
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    record_creation(conn, approval_id, "L-9", "269cd138-00b1-48c3-9093-16c36120a20e")
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    elsewhere = "aaaaaaaa-0000-0000-0000-00000000000e"
    fake = FakeInstantly(clock=clock)
    fake.leads["x@acme0.com"] = {"id": "L-9", "email": "x@acme0.com", "campaign": elsewhere, "status": 3}

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN})

    assert out["moved_to_followup"] == 1
    assert fake.moved == [{"ids": ["L-9"], "from": elsewhere, "to": FOLLOWUP_CAMPAIGN}]


def test_a_held_follow_up_is_looked_at_again_once_its_wait_has_passed(conn, clock):
    """Held means later, not never."""
    approval_id = approve(conn, clock)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))

    first = followups.due_followups(conn, now=clock())
    assert first["verified_due"] == 1 and item(conn, approval_id)["state"] == "waiting"

    later = followups.due_followups(conn, now=clock() + timedelta(days=followups.RECHECK_DAYS + 1))
    assert later["considered"] == 1, "a held follow-up was never revisited"


def test_a_contact_whose_sequence_is_still_running_is_never_pulled_out_of_it(conn, clock):
    """Moving them would stop emails that are already going. The follow-up waits."""
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    source = "269cd138-00b1-48c3-9093-16c36120a20e"
    record_creation(conn, approval_id, "L-10", source)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock)
    fake.leads["x@acme0.com"] = {"id": "L-10", "email": "x@acme0.com", "campaign": source,
                                 "status": followups.IN_SEQUENCE}

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN})

    assert out["still_in_sequence"] == 1 and out["moved_to_followup"] == 0
    assert fake.moved == [], "a running sequence was interrupted"
    row = item(conn, approval_id)
    assert row["state"] == "waiting" and row["waiting_on"] == followups.STILL_IN_SEQUENCE


def test_the_follow_up_is_finished_only_by_the_email_itself(conn, clock):
    """A moved contact is not a contacted contact. Instantly's own message id is the receipt."""
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    source = "269cd138-00b1-48c3-9093-16c36120a20e"
    record_creation(conn, approval_id, "L-11", source)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock)
    who = email_of(conn, approval_id)
    fake.leads[who] = {"id": "L-11", "email": who, "campaign": source, "status": 3}
    env = {followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN}

    first = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake), env=env)
    assert first["awaiting_send"] == 1 and first["followup_sent"] == 0
    assert item(conn, approval_id)["state"] == "waiting"

    # the send window opens and Instantly sends the one step
    fake.emails[who] = [{"id": "msg-77", "ue_type": 1, "subject": "Following up",
                         "from_address_email": "devan@example.invalid",
                         "timestamp_created": clock().isoformat()}]
    later = followups.due_followups(conn, now=clock() + timedelta(days=2),
                                    instantly=instantly_client(fake), env=env)

    assert later["followup_sent"] == 1
    assert len(fake.moved) == 1, "waiting for the email must never move anybody again"
    row = item(conn, approval_id)
    assert row["state"] == "done" and row["reason"] == "followup_sent:msg-77"
    receipt = sqlall(conn, "SELECT sent_message_id, sent_subject, (sent_at IS NOT NULL) AS sent "
                           "FROM followup_deliveries")[0]
    assert receipt == {"sent_message_id": "msg-77", "sent_subject": "Following up", "sent": True}


def test_their_own_out_of_office_is_never_mistaken_for_our_follow_up(conn, clock):
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    source = "269cd138-00b1-48c3-9093-16c36120a20e"
    record_creation(conn, approval_id, "L-12", source)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock)
    who = email_of(conn, approval_id)
    fake.leads[who] = {"id": "L-12", "email": who, "campaign": source, "status": 3}
    fake.emails[who] = [{"id": "their-reply", "ue_type": 2, "subject": "OOO"}]
    env = {followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN}

    followups.due_followups(conn, now=clock(), instantly=instantly_client(fake), env=env)
    again = followups.due_followups(conn, now=clock() + timedelta(days=2),
                                    instantly=instantly_client(fake), env=env)

    assert again["followup_sent"] == 0 and again["awaiting_send"] == 1
    assert item(conn, approval_id)["state"] == "waiting"


def test_somebody_who_answered_for_themselves_is_not_followed_up(conn, clock):
    """A real answer supersedes the auto-reply that queued this."""
    approval_id = approve(conn, clock)
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    queued_at = sql1(conn, "SELECT created_at FROM work_items WHERE kind = %s AND subject_id = %s",
                     (WORK_KIND, approval_id))
    conn.execute("INSERT INTO outcome_events (provider, event_type, dedupe_key, email, occurred_at) "
                 "VALUES ('instantly', 'reply', %s, %s, %s)",
                 ("dk-1", email_of(conn, approval_id), queued_at + timedelta(minutes=5)))
    conn.commit()

    out = followups.due_followups(conn, now=clock() + timedelta(hours=1))

    assert out["replied_since"] == 1 and out["verified_due"] == 0
    row = item(conn, approval_id)
    assert row["state"] == "closed" and row["reason"] == "replied_since_the_out_of_office"
