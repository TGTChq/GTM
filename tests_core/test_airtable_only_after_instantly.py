"""Airtable holds only final, good, non-duplicate leads.

Measured 2026-09-22: the catch-up wrote 905 Airtable rows while Instantly genuinely
created 872 -- 33 rows for people Instantly already had (5 in the same campaign, 28
in another). Across all history 201 Airtable rows have no genuine Instantly creation.

The corrected invariant: an Airtable record is created ONLY after Instantly reports a
genuine creation for that approval. Every other outcome blocks the Airtable row with a
named reason, and one person never gets a second Airtable record.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tgtc_core.services.delivery import (OutboxItem, DUP_AIRTABLE, DUP_INSTANTLY_OTHER_CAMPAIGN, DUP_INSTANTLY_SAME_CAMPAIGN,
                                         FAILED_COMPLIANCE_GATE, SAME_PERSON_MULTIPLE_CAMPAIGNS,
                                         SAME_PERSON_MULTIPLE_JOBS, AWAITING_INSTANTLY)
from tgtc_core.db.connection import jsonb
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, seed_opportunity

CS_CAMPAIGN = CONTROL_ID_BY_CAMPAIGN_KEY["customer_experience"]


def approve(conn, clock, *, domain="acme.com", org="Acme", job="job-1", function_key="customer_success"):
    pid, eid, oid = seed_opportunity(conn, clock, domain=domain, org_name=org, job_id=job, function_key=function_key)
    out = opportunity_service(conn, apollo_for(domain, org), clock).process(oid)
    assert out.outcome == "approved"
    return out.approval_id


def airtable_state(conn, approval_id):
    return sqlall(conn, "SELECT state, coalesce(blocked_reason,'') reason, coalesce(last_error,'') note "
                        "FROM delivery_outbox WHERE approval_id = %s AND channel = 'airtable'", (approval_id,))[0]


def release(conn, clock):
    """Make deferred/backed-off outbox rows claimable again (the next cycle in production)."""
    conn.execute("UPDATE delivery_outbox SET available_at = %s WHERE state IN ('pending', 'failed')",
                 (clock() - timedelta(minutes=1),))
    conn.commit()


def clone_approval(conn, approval_id, *, campaign_id=None, new_person=False, email=None,
                   new_opportunity=False, function_key=None):
    """A second approval of the SAME person (another campaign or another job), or of a
    colleague at the same company, with its own outbox rows."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,))
        a = dict(cur.fetchone())
        person_id = a["person_id"]
        if new_person:
            cur.execute("SELECT * FROM people WHERE id = %s", (person_id,))
            person = dict(cur.fetchone())
            cur.execute(
                "INSERT INTO people (apollo_person_id, first_name, last_name, title, employer_id, email, email_status,"
                " email_verified_at, organization_domain, facts_json, contact_country) "
                "VALUES (%s, %s, 'Colleague', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (str(person["apollo_person_id"]) + "-2", person["first_name"], person["title"], person["employer_id"],
                 email, person["email_status"], person["email_verified_at"], person["organization_domain"],
                 jsonb(person["facts_json"] or {}), person["contact_country"]))
            person_id = int(cur.fetchone()["id"])
        opportunity_id = a["opportunity_id"]
        if new_opportunity:
            cur.execute("INSERT INTO opportunities (employer_id, function_key, campaign_key, lane, state) "
                        "SELECT employer_id, %s, %s, lane, state FROM opportunities WHERE id = %s RETURNING id",
                        (function_key or "finance", function_key or "finance", opportunity_id))
            opportunity_id = int(cur.fetchone()["id"])
        lead = dict(a["lead_json"] or {})
        if email:
            lead["email"] = email
        suffix = f"-{person_id}-{campaign_id or opportunity_id}"
        cur.execute(
            "INSERT INTO approvals (opportunity_id, person_id, employer_id, campaign_key, function_key, campaign_id,"
            " policy_version, lead_key, fingerprint, lead_json, state, run_id, outreach_eligible, contact_country) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (opportunity_id, person_id, a["employer_id"], a["campaign_key"], a["function_key"],
             campaign_id or a["campaign_id"], a["policy_version"], str(a["lead_key"]) + suffix, a["fingerprint"],
             jsonb(lead), a["state"], a["run_id"], a["outreach_eligible"], a["contact_country"]))
        new_id = int(cur.fetchone()["id"])
        assert new_id != approval_id
        for channel in ("airtable", "instantly"):
            cur.execute("SELECT payload_json, idempotency_key FROM delivery_outbox WHERE approval_id = %s AND channel = %s",
                        (approval_id, channel))
            row = dict(cur.fetchone())
            payload = dict(row["payload_json"])
            if email:
                payload["Email" if channel == "airtable" else "email"] = email
            cur.execute(
                "INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, available_at, state) "
                "VALUES (%s, %s, %s, %s, %s, 'pending')",
                (new_id, channel, str(row["idempotency_key"]) + suffix, jsonb(payload), a["approved_at"]))
    conn.commit()
    return new_id


def deliver(conn, clock, at, ins, *, order=("instantly", "airtable")):
    svc = delivery_service(conn, at, ins, clock)
    return {ch: [x.outcome for x in svc.drain(ch)] for ch in order}


# --- Instantly decides whether Airtable may be written ------------------------------------
def test_instantly_existing_in_the_same_campaign_creates_no_airtable_record(conn, clock):
    approval_id = approve(conn, clock)
    ins = FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    email = sql1(conn, "SELECT lead_json->>'email' FROM approvals WHERE id = %s", (approval_id,))
    ins.leads[email.lower()] = {"id": "lead-1", "email": email.lower(), "campaign": CS_CAMPAIGN,
                                "timestamp_created": "2026-09-01T00:00:00Z"}   # already in THIS campaign
    at = FakeAirtable()
    deliver(conn, clock, at, ins)
    assert at.records == {}                                   # the defect: this used to create a row
    st = airtable_state(conn, approval_id)
    assert (st["state"], st["reason"]) == ("blocked", DUP_INSTANTLY_SAME_CAMPAIGN)


def test_instantly_existing_in_another_campaign_creates_no_airtable_record(conn, clock):
    approval_id = approve(conn, clock)
    ins = FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    email = sql1(conn, "SELECT lead_json->>'email' FROM approvals WHERE id = %s", (approval_id,))
    ins.leads[email.lower()] = {"id": "lead-1", "email": email.lower(), "campaign": "another-campaign-id",
                                "timestamp_created": "2026-09-01T00:00:00Z"}   # already in ANOTHER campaign
    at = FakeAirtable()
    deliver(conn, clock, at, ins)
    assert at.records == {}
    st = airtable_state(conn, approval_id)
    assert (st["state"], st["reason"]) == ("blocked", DUP_INSTANTLY_OTHER_CAMPAIGN)


def test_a_genuine_instantly_creation_still_writes_exactly_one_airtable_record(conn, clock):
    approval_id = approve(conn, clock)
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    out = deliver(conn, clock, at, ins)
    assert out["instantly"] == ["delivered"] and out["airtable"] == ["delivered"]
    assert len(at.records) == 1 and len(ins.leads) == 1


def test_airtable_waits_for_instantly_and_is_never_written_first(conn, clock):
    approval_id = approve(conn, clock)
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    svc = delivery_service(conn, at, ins, clock)
    assert [x.outcome for x in svc.drain("airtable")] == ["deferred"]     # Instantly has not answered yet
    assert at.records == {} and airtable_state(conn, approval_id)["note"] == AWAITING_INSTANTLY
    svc.drain("instantly")
    release(conn, clock)                                   # the next cycle claims it
    assert [x.outcome for x in svc.drain("airtable")] == ["delivered"]
    assert len(at.records) == 1


def test_a_compliance_blocked_lead_never_reaches_airtable(conn, clock):
    approval_id = approve(conn, clock)
    conn.execute("UPDATE delivery_outbox SET state='blocked', blocked_reason='compliance:uk:no_lawful_basis_record' "
                 "WHERE approval_id=%s AND channel='instantly'", (approval_id,))
    conn.commit()
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    deliver(conn, clock, at, ins)
    assert at.records == {}
    st = airtable_state(conn, approval_id)
    assert (st["state"], st["reason"]) == ("blocked", FAILED_COMPLIANCE_GATE)


# --- one person, one Airtable record ------------------------------------------------------
def gate_for(conn, clock, approval_id, at, ins):
    """What the corrected rule decides for this approval's Airtable row."""
    svc = delivery_service(conn, at, ins, clock)
    row = sqlall(conn, "SELECT id, approval_id, idempotency_key, payload_json, state FROM delivery_outbox "
                       "WHERE approval_id = %s AND channel = 'airtable'", (approval_id,))[0]
    item = OutboxItem(row["id"], row["approval_id"], "airtable", row["idempotency_key"], row["payload_json"],
                      row["state"], 1, None, row["state"])
    return svc.airtable_gate(item)


def test_one_person_can_hold_only_one_active_approval_anywhere(conn, clock):
    """The schema already makes a person one canonical lead: approvals_person_active_uq
    is UNIQUE (person_id) WHERE state <> 'revoked' -- across every campaign and employer."""
    first = approve(conn, clock)
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    deliver(conn, clock, at, ins)
    assert len(at.records) == 1
    with pytest.raises(Exception) as err:
        clone_approval(conn, first, campaign_id="second-campaign-id", new_opportunity=True, function_key="finance")
    conn.rollback()
    assert "approvals_person_active_uq" in str(err.value)
    assert len(at.records) == 1


def test_a_revoked_approval_never_lets_the_same_person_into_airtable_twice(conn, clock):
    """The one way a second approval of one person can exist: the first was revoked.
    The gate still refuses a second Airtable record for that person."""
    first = approve(conn, clock)
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    deliver(conn, clock, at, ins)
    assert len(at.records) == 1
    conn.execute("UPDATE approvals SET state = 'revoked' WHERE id = %s", (first,))
    conn.commit()
    again = clone_approval(conn, first, campaign_id="second-campaign-id", new_opportunity=True, function_key="finance")
    assert gate_for(conn, clock, again, at, ins) == ("block", SAME_PERSON_MULTIPLE_CAMPAIGNS)
    same_campaign = clone_approval(conn, first, new_opportunity=True, function_key="marketing")
    assert gate_for(conn, clock, same_campaign, at, ins) == ("block", SAME_PERSON_MULTIPLE_JOBS)
    deliver(conn, clock, at, ins)
    assert len(at.records) == 1                       # still exactly one record for that person


def test_two_different_people_at_one_company_are_not_duplicates(conn, clock):
    first = approve(conn, clock)
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    deliver(conn, clock, at, ins)
    colleague = clone_approval(conn, first, new_person=True, email="second.buyer@acme.com")
    # A different person at the same company is never suppressed as a duplicate; once
    # Instantly creates them, the gate lets their record through.
    assert gate_for(conn, clock, colleague, at, ins) == ("defer", AWAITING_INSTANTLY)
    deliver(conn, clock, at, ins)
    release(conn, clock)
    deliver(conn, clock, at, ins)
    assert gate_for(conn, clock, colleague, at, ins) is None
    emails = {r["fields"]["Email"] for r in at.records.values()}
    assert len(at.records) == len(emails) == 2


def test_one_approval_can_never_have_two_airtable_rows(conn, clock):
    """The schema itself forbids it: UNIQUE (approval_id, channel)."""
    approval_id = approve(conn, clock)
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    deliver(conn, clock, at, ins)
    assert len(at.records) == 1
    with pytest.raises(Exception) as err:
        conn.execute("INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, state) "
                     "SELECT approval_id, 'airtable', idempotency_key || ':2', payload_json, 'pending' "
                     "FROM delivery_outbox WHERE approval_id = %s AND channel = 'airtable'", (approval_id,))
    conn.rollback()
    assert "delivery_outbox_approval_id_channel_key" in str(err.value)
    assert len(at.records) == 1


# --- idempotency ---------------------------------------------------------------------------
def test_a_failed_airtable_write_after_instantly_success_never_creates_a_second_lead(conn, clock, monkeypatch):
    approval_id = approve(conn, clock)
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    svc = delivery_service(conn, at, ins, clock)
    svc.drain("instantly")
    assert len(ins.leads) == 1
    from tgtc_core.providers.airtable import AirtableResult
    monkeypatch.setattr(svc.airtable, "create_records", lambda payloads: AirtableResult(False, 503, error_type="server"))
    svc.drain("airtable")
    assert at.records == {}
    monkeypatch.undo()
    svc2 = delivery_service(conn, at, ins, clock)
    release(conn, clock)
    svc2.drain("airtable")
    assert len(at.records) == 1 and len(ins.leads) == 1        # one lead, one record, no duplicates
