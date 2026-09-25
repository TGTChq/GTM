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

    assert (out["considered"], out["verified_due"], out["followed_up"]) == (1, 1, 0)
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
    fake.leads["x@acme0.com"] = {"id": lead_id, "email": "x@acme0.com", "campaign": source}

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(fake),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN})

    assert out["followed_up"] == 1 and out["verified_due"] == 0
    assert fake.moved == [{"ids": [lead_id], "from": source, "to": FOLLOWUP_CAMPAIGN}]
    assert fake.leads["x@acme0.com"]["campaign"] == FOLLOWUP_CAMPAIGN
    row = item(conn, approval_id)
    assert row["state"] == "done" and row["reason"] == f"followed_up:{lead_id}"


def test_the_same_follow_up_never_moves_twice(conn, clock):
    from tgtc_core.testing.fakes import FakeInstantly

    approval_id = approve(conn, clock)
    record_creation(conn, approval_id, "L-2", "269cd138-00b1-48c3-9093-16c36120a20e")
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))
    fake = FakeInstantly(clock=clock)
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

    assert out["suppressed"] == 1 and out["followed_up"] == 0
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
    record_creation(conn, approval_id, "L-5", "269cd138-00b1-48c3-9093-16c36120a20e")
    queue_followup(conn, approval_id, clock() - timedelta(hours=1))

    class Refusing(FakeInstantly):
        def request(self, method, url, **kw):
            if url.endswith("/leads/move"):
                from tgtc_core.providers.http import Response
                return Response(status=503, headers={}, text='{"error":"unavailable"}')
            return super().request(method, url, **kw)

    out = followups.due_followups(conn, now=clock(), instantly=instantly_client(Refusing(clock=clock)),
                                  env={followups.FOLLOWUP_CAMPAIGN_ENV: FOLLOWUP_CAMPAIGN})

    assert out["move_failed"] == 1 and out["followed_up"] == 0
    assert item(conn, approval_id)["state"] == "waiting"
