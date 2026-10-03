"""The ONE repair email for the 2026-09-21 blank sends, drained from an authorised queue.

On 2026-09-21 the core began enrolling Challenger leads whose rendered copy was absent.
The nine campaigns hold no literal copy, so 16,875 messages reached 7,777 people with an
empty subject and nothing but a signature. Those nine campaigns are paused for good and
nobody in them receives another step.

What is authorised is narrow: ONE email, from a separate single-step campaign, to the
affected recipients that can still be revalidated. Everything here exists to keep that
sentence true under replay, under a crash, and under a provider that answers 200 before
it has done the work.

How the hazards are handled, each of which has already happened at least once here:

* **A second email to the same person.** The queue's key is the recipient. A row that has
  been sent cannot return to a sendable state -- the database refuses it, not this code.
* **An empty variable.** The copy is literal and the subject is "Your <role> opening", so
  an absent role would reproduce the incident. ``copy_block_reason`` refuses such a
  payload inside the provider client, and every created lead is read BACK by id.
* **A revoked authorisation.** A later reply, unsubscribe, bounce, departure or any
  suppression from another source ends the authorisation for that person immediately.
  Revalidation runs on every tick, not once when the queue was built.
* **Storage.** The plan caps stored contacts and the only honest signal is the provider's
  own refusal. The queue drains in batches, leaves a floor for the daily run, and a
  "Lead limit reached" answer parks the rest instead of losing it.
* **Counting them as new work.** Nothing here touches ``approvals``, ``delivery_outbox``
  or Airtable. These people were acquired, approved and emailed weeks ago; they are a
  repair, not a lead. So they cannot enter the daily success metric and cannot open a
  second Airtable row by changing campaign.

Enrolling is not sending. ``enrol`` creates contacts in a campaign whose status decides
whether anything leaves, and ``record_receipts`` is the only function that claims a send --
it claims it from a message id Instantly returns, never from a successful enrolment.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

import psycopg

from ..db.connection import jsonb, transaction
from ..policy.campaigns import (EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID,
                                EMPTY_EMAIL_RECOVERY_REQUIRED_VARIABLES)

#: The suppression source this recovery is a per-contact exception to. A suppression from
#: anything else is somebody's own decision and overrides the exception.
INCIDENT_SUPPRESSION_SOURCE = "copy_incident:20261002"

#: Anything recorded after the blank email that ends the authorisation.
REVOKING_EVENTS = frozenset({
    "unsubscribe", "unsubscribed", "opt_out", "do_not_contact", "spam_complaint",
    "no_longer_here", "bounce", "bounced", "human_reply", "reply", "replied",
})

#: Stored contacts kept free for the daily run, which needs room to acquire. The recovery
#: never deletes anybody to make space: it takes only what is already free above this
#: floor and waits for the run's own authorised rotation to replenish it.
STORAGE_FLOOR_ENV = "TGTC_RECOVERY_STORAGE_FLOOR"
DEFAULT_STORAGE_FLOOR = 1500

#: How many contacts one tick may enrol.
#:
#: Sized for the hour it runs in, not for the queue. Each contact costs two provider
#: calls -- the create, and the read-back that proves it -- and the key allows 20 requests
#: a minute, so 150 contacts is about 15 minutes and leaves the hourly tick room for the
#: reply poll and the alert. Enrolling is not sending: the campaign's own
#: ``daily_max_leads`` of 500 paces the send budget shared with the v2 campaigns, so
#: enrolling faster only queues people inside the campaign.
BATCH_ENV = "TGTC_RECOVERY_BATCH"
DEFAULT_BATCH = 150

#: Seconds between provider calls.
#:
#: The Instantly client has no rate handling of its own -- unlike the Airtable and Apollo
#: clients, a 429 falls through it as a plain failure -- so the pacing lives here rather
#: than being assumed. 3 seconds is the documented 20 requests a minute. Widening the
#: client's retry behaviour would change every caller, including the daily run, and that
#: is a different change from this one.
PACE_SECONDS_ENV = "TGTC_RECOVERY_PACE_SECONDS"
DEFAULT_PACE_SECONDS = 3.0

#: Measured 2026-09-24 on plan pid_hg_v1 (HyperGrowth): the allowance is a stock of
#: stored contacts, not a monthly grant.
PLAN_ALLOWANCE_ENV = "TGTC_INSTANTLY_LEAD_ALLOWANCE"
DEFAULT_PLAN_ALLOWANCE = 25000

#: May a short tick ask the already-authorised rotation to free slots?
#:
#: Off by default, deliberately. Rotation deletes contacts, and although every guard is
#: its own -- a protected campaign, an unfinished sequence, a reply, one of our own
#: suppressions, a pending delivery, and a durable backup of every record before it goes
#: -- the first batches of this repair should be watched before a deletion path is
#: attached to them. With it off, the queue simply waits at the floor, which the health
#: alert reports.
MAY_ROTATE_ENV = "TGTC_RECOVERY_MAY_ROTATE"

SENDABLE_STATES = ("authorised", "reserved")
OPEN_STATES = ("authorised", "reserved", "enrolled")


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(str(env.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


class _Pace:
    """A minimum gap between provider calls, so a batch does not trip the rate limit.

    Deliberately a sleep rather than a retry: a 429 that has already happened has
    consumed an attempt and told a row it failed, and the point is not to get there.
    """

    def __init__(self, seconds: float, sleep=time.sleep, clock=time.monotonic):
        self.seconds = max(0.0, float(seconds))
        self._sleep, self._clock = sleep, clock
        self._last = None

    def wait(self) -> None:
        if not self.seconds:
            return
        if self._last is not None:
            gap = self._clock() - self._last
            if gap < self.seconds:
                self._sleep(self.seconds - gap)
        self._last = self._clock()


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(str(env.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _now(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


# --------------------------------------------------------------------------- queue


def internal_test_passed(conn: psycopg.Connection) -> Dict[str, Any]:
    """Has one of our own addresses received this email, with the approved subject?

    The gate real recipients wait behind. It is deliberately a question about a RECEIVED
    message, not about an enrolment: the incident consisted entirely of enrolments that
    looked fine. Until the answer is yes, ``enrol`` will only enrol test rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT email, sent_at, evidence->>'sent_subject' AS subject "
            "FROM empty_email_recovery WHERE is_internal_test AND state = 'sent' "
            "AND (evidence->>'subject_as_approved') = 'true' ORDER BY sent_at LIMIT 1")
        row = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM empty_email_recovery WHERE is_internal_test")
        tests = int(cur.fetchone()["n"])
    conn.commit()
    if not row:
        return {"passed": False, "test_rows": tests}
    return {"passed": True, "test_rows": tests, "address": row["email"],
            "sent_at": row["sent_at"], "subject": row["subject"]}


def load_queue(conn: psycopg.Connection, rows: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    """Record the authorised queue. Idempotent, and it never walks a row backwards.

    A reload after a batch has been enrolled must not re-authorise those people, so an
    existing row is left exactly as it is. Only genuinely new recipients are inserted.
    """
    inserted = skipped = refused = 0
    with transaction(conn) as tx:
        with tx.cursor() as cur:
            for row in rows:
                email = str(row.get("email") or "").strip().lower()
                role = str(row.get("verified_role") or "").strip()
                first = str(row.get("first_name") or "").strip()
                if not email or not role or not first:
                    refused += 1
                    continue
                cur.execute(
                    """
                    INSERT INTO empty_email_recovery
                        (email, person_id, first_name, last_name, employer, employer_domain,
                         function_key, verified_role, posting_id, posting_title,
                         posting_is_original, original_campaign_id, evidence,
                         is_internal_test)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (email) DO NOTHING
                    """,
                    (email, row.get("person_id"), first, row.get("last_name"),
                     row.get("employer"), row.get("employer_domain"),
                     str(row.get("function_key") or ""), role,
                     str(row.get("posting_id") or "") or None,
                     row.get("posting_title"), row.get("posting_is_the_original"),
                     str(row.get("original_campaign_id") or "") or None,
                     jsonb({"employment_evidence": row.get("employment_evidence"),
                            "employment_verified_at": str(row.get("employment_verified_at") or ""),
                            "posting_last_active": str(row.get("posting_last_active") or "")}),
                     bool(row.get("is_internal_test"))))
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
    return {"inserted": inserted, "already_present": skipped, "refused_incomplete": refused}


def revalidate(conn: psycopg.Connection, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """End the authorisation for anybody it no longer covers.

    Runs before every batch, because the queue was built from a snapshot and people act
    after snapshots. A revocation is final: nothing here returns a revoked row to a
    sendable state.
    """
    moment = _now(now)
    revoked: Dict[str, int] = {}
    with transaction(conn) as tx:
        with tx.cursor() as cur:
            cur.execute(
                """
                SELECT r.email,
                       (SELECT string_agg(DISTINCT s.source || ':' || s.reason, ',')
                          FROM suppressions s
                         WHERE s.kind = 'person_email' AND lower(s.key) = r.email
                           AND s.source <> %s)                      AS other_suppression,
                       (SELECT string_agg(DISTINCT lower(o.event_type), ',')
                          FROM outcome_events o
                         WHERE lower(coalesce(o.email, '')) = r.email
                           AND lower(o.event_type) = ANY(%s))        AS events,
                       (SELECT coalesce(p.opt_out_status, '') FROM people p
                         WHERE lower(p.email) = r.email LIMIT 1)     AS opt_out
                  FROM empty_email_recovery r
                 WHERE r.state = ANY(%s)
                """,
                (INCIDENT_SUPPRESSION_SOURCE, list(REVOKING_EVENTS), list(OPEN_STATES)))
            for row in cur.fetchall():
                reason = ""
                if row["other_suppression"]:
                    reason = "suppressed_elsewhere:" + str(row["other_suppression"])[:120]
                elif row["events"]:
                    reason = "later_outcome:" + str(row["events"])[:100]
                elif str(row["opt_out"] or "").strip():
                    reason = "person_opt_out:" + str(row["opt_out"])[:60]
                if not reason:
                    continue
                cur.execute(
                    "UPDATE empty_email_recovery SET state = 'revoked', state_reason = %s, "
                    "revoked_at = %s WHERE email = %s AND state = ANY(%s)",
                    (reason, moment, row["email"], list(OPEN_STATES)))
                key = reason.split(":")[0]
                revoked[key] = revoked.get(key, 0) + 1
    return {"revoked": sum(revoked.values()), "by_reason": revoked}


# ------------------------------------------------------------------------- capacity


def free_slots(client, *, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """How many stored contacts the plan still allows, and how many this may take.

    Delegates to the rotation's ``occupancy``, which is the only measurement here that
    counts what the plan counts. Summing ``leads_count`` over campaigns is NOT that: a
    contact parked on a lead list belongs to no campaign and appears in no such count
    while still occupying a stored contact. Measured 2026-10-03, the campaign view said
    20,509 where the workspace held 22,522, so reading it alone would have believed in
    2,013 slots that do not exist and taken them from the floor the daily run needs.

    Unknown is never treated as empty: an unreadable workspace stops the tick.
    """
    from . import instantly_rotation as rot

    environ = env if env is not None else os.environ
    floor = _int_env(environ, STORAGE_FLOOR_ENV, DEFAULT_STORAGE_FLOOR)
    allowance = _int_env(environ, PLAN_ALLOWANCE_ENV, DEFAULT_PLAN_ALLOWANCE)
    occupancy = rot.occupancy(client, plan_contacts=allowance)
    if not occupancy.get("known"):
        return {"measured": False, "reason": str(occupancy.get("message") or "")[:200],
                "available": 0, "allowance": allowance, "floor": floor}
    free = int(occupancy.get("free") or 0)
    return {"measured": True, "stored": occupancy.get("stored"),
            "in_campaigns": occupancy.get("stored_in_campaigns"),
            "on_lead_lists": occupancy.get("stored_on_lists"),
            "allowance": allowance, "free": free, "floor": floor,
            "available": max(0, free - floor)}


# -------------------------------------------------------------------------- enrol


def _make_room(conn: psycopg.Connection, client, *, want: int,
               env: Mapping[str, str], now: datetime) -> Dict[str, Any]:
    """Ask the already-authorised rotation for room, with ITS guards, not new ones.

    Deliberately nothing new: the same ``make_room`` the daily run uses, so the rules
    about who may be removed, the durable backup taken before each delete and the
    measurement that a delete actually returned a slot are one implementation, not two.
    The target is the batch, so a tick frees what one batch needs and no more.
    """
    from . import instantly_rotation as rot

    return rot.make_room(
        conn, client, target=int(want),
        campaigns=rot.enumerate_campaigns(client),
        leads_of=rot.campaign_leads(client), delete=client.delete_lead,
        env=dict(env), now=now)


def _payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The lead exactly as the campaign's literal copy needs it.

    ``first_name`` fills ``{{firstName}}``; ``verified_role`` is a custom variable because
    the copy names it in both the subject and the body. Nothing else is sent: the campaign
    carries the words, so there is no second place for copy to go missing.
    """
    payload: Dict[str, Any] = {
        "campaign": EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID,
        "email": str(row["email"]),
        "first_name": str(row["first_name"] or "").strip(),
        "custom_variables": {"verified_role": str(row["verified_role"] or "").strip()},
        # Instantly must not create a duplicate of somebody already in this campaign.
        "skip_if_in_campaign": True,
    }
    last = str(row.get("last_name") or "").strip()
    if last:
        payload["last_name"] = last
    employer = str(row.get("employer") or "").strip()
    if employer:
        payload["company_name"] = employer
    return payload


def _verify_created(client, lead_id: str, row: Mapping[str, Any]) -> str:
    """Read the lead back and compare. '' when it is what we meant to create."""
    result = client.get_lead(lead_id)
    if not result.ok:
        return "unverified:%s" % (str(result.message)[:80] or result.status)
    data = result.data if isinstance(result.data, dict) else {}
    if str(data.get("campaign") or data.get("campaign_id") or "") != EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID:
        return "verified_wrong_campaign"
    if str(data.get("email") or "").strip().lower() != str(row["email"]).strip().lower():
        return "verified_wrong_address"
    # Variables are read from ``payload`` on a lead, which is where Instantly keeps them;
    # a 200 on the create says nothing about what was stored.
    stored = data.get("payload")
    if not isinstance(stored, dict):
        stored = data
    variables = stored.get("custom_variables")
    if not isinstance(variables, dict):
        return "verified_no_variables"
    for key in EMPTY_EMAIL_RECOVERY_REQUIRED_VARIABLES:
        value = str(variables.get(key) or "").strip()
        if not value:
            return "verified_missing:%s" % key
        if value != str(row["verified_role"] or "").strip():
            return "verified_changed:%s" % key
    if not str(stored.get("first_name") or data.get("first_name") or "").strip():
        return "verified_missing:first_name"
    return ""


def enrol(conn: psycopg.Connection, client, *, limit: Optional[int] = None,
          env: Optional[Mapping[str, str]] = None, now: Optional[datetime] = None,
          dry_run: bool = False, pace: Optional[_Pace] = None) -> Dict[str, Any]:
    """Create one batch of recovery contacts, and verify each one by id.

    Returns what happened, never a bare success. A contact is only ``enrolled`` once it
    has been read back from Instantly carrying the role its subject line needs.
    """
    environ = env if env is not None else os.environ
    moment = _now(now)
    batch = limit if limit is not None else _int_env(environ, BATCH_ENV, DEFAULT_BATCH)

    capacity = free_slots(client, env=environ)
    if not capacity.get("measured"):
        return {"stage": "capacity", "enrolled": 0, "capacity": capacity,
                "stopped": "capacity_unknown"}
    room = min(batch, int(capacity.get("available") or 0))
    rotation = None
    if room <= 0 and str(environ.get(MAY_ROTATE_ENV, "")).strip() in ("1", "true", "True"):
        rotation = _make_room(conn, client, want=batch, env=environ, now=moment)
        capacity = free_slots(client, env=environ)
        room = min(batch, int(capacity.get("available") or 0))
    if room <= 0:
        return {"stage": "capacity", "enrolled": 0, "capacity": capacity,
                "rotation": rotation, "stopped": "storage_floor_reached"}

    # The internal test comes first and alone. Until one of our own addresses has
    # RECEIVED this email with the approved subject, no stranger is enrolled -- which is
    # the check that was missing on 2026-09-21, when 7,777 people became the test.
    gate = internal_test_passed(conn)
    with conn.cursor() as cur:
        if gate["passed"]:
            cur.execute(
                "SELECT * FROM empty_email_recovery WHERE state = ANY(%s) "
                "ORDER BY is_internal_test DESC, authorised_at, email LIMIT %s",
                (list(SENDABLE_STATES), room))
        else:
            cur.execute(
                "SELECT * FROM empty_email_recovery WHERE state = ANY(%s) "
                "AND is_internal_test ORDER BY authorised_at, email LIMIT %s",
                (list(SENDABLE_STATES), room))
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    if not rows:
        return {"stage": "enrol", "enrolled": 0, "capacity": capacity,
                "internal_test": gate,
                "stopped": "queue_empty" if gate["passed"] else "awaiting_internal_test"}
    if dry_run:
        return {"stage": "plan", "enrolled": 0, "would_enrol": len(rows),
                "capacity": capacity, "internal_test": gate,
                "sample": [r["email"] for r in rows[:5]]}

    created = refused = failed = 0
    stopped = ""
    reasons: Dict[str, int] = {}
    clock = pace if pace is not None else _Pace(
        _float_env(environ, PACE_SECONDS_ENV, DEFAULT_PACE_SECONDS))
    for row in rows:
        # Reserved BEFORE the call, so a crash between the create and the record leaves a
        # row that says "this may already exist in Instantly" rather than one that looks
        # untouched and gets created twice.
        with transaction(conn) as tx:
            tx.execute("UPDATE empty_email_recovery SET state = 'reserved', reserved_at = %s, "
                       "attempts = attempts + 1 WHERE email = %s", (moment, row["email"]))
        payload = _payload(row)
        clock.wait()
        try:
            result = client.create_lead(payload)
        except ValueError as exc:                     # the copy contract refused it
            reason = str(exc)[:120]
            refused += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            with transaction(conn) as tx:
                tx.execute("UPDATE empty_email_recovery SET state = 'withheld', "
                           "state_reason = %s WHERE email = %s", (reason, row["email"]))
            continue
        if not result.ok:
            message = str(result.message or "")
            if result.status == 429:
                # Not this row's fault, and not a refusal. Stop the batch, leave everybody
                # sendable, and let the next tick carry on.
                with transaction(conn) as tx:
                    tx.execute("UPDATE empty_email_recovery SET state = 'authorised', "
                               "last_error = %s WHERE email = %s",
                               ("rate_limited", row["email"]))
                stopped = "rate_limited"
                break
            if "lead limit" in message.lower() or "remaining uploads" in message.lower():
                # The allowance is the real ceiling. Park the rest of the queue rather
                # than burning attempts against a wall, and leave this row sendable.
                with transaction(conn) as tx:
                    tx.execute("UPDATE empty_email_recovery SET state = 'authorised', "
                               "last_error = %s WHERE email = %s",
                               ("provider_storage_full", row["email"]))
                stopped = "provider_storage_full"
                break
            failed += 1
            key = "create_%s" % (result.status or "error")
            reasons[key] = reasons.get(key, 0) + 1
            with transaction(conn) as tx:
                tx.execute("UPDATE empty_email_recovery SET state = 'authorised', "
                           "last_error = %s WHERE email = %s", (message[:300], row["email"]))
            continue
        data = result.data if isinstance(result.data, dict) else {}
        lead_id = str(data.get("id") or "")
        clock.wait()
        problem = _verify_created(client, lead_id, row) if lead_id else "created_without_id"
        if problem:
            failed += 1
            reasons[problem] = reasons.get(problem, 0) + 1
            with transaction(conn) as tx:
                tx.execute("UPDATE empty_email_recovery SET state = 'authorised', "
                           "instantly_lead_id = %s, last_error = %s WHERE email = %s",
                           (lead_id or None, problem, row["email"]))
            continue
        created += 1
        with transaction(conn) as tx:
            tx.execute("UPDATE empty_email_recovery SET state = 'enrolled', enrolled_at = %s, "
                       "instantly_lead_id = %s, last_error = '', state_reason = '' "
                       "WHERE email = %s", (moment, lead_id, row["email"]))
    return {"stage": "enrol", "attempted": len(rows), "enrolled": created,
            "withheld": refused, "failed": failed, "capacity": capacity,
            "internal_test": gate, "stopped": stopped, "reasons": reasons}


# ----------------------------------------------------------------------- receipts


def record_receipts(conn: psycopg.Connection, client, *, limit: int = 200,
                    now: Optional[datetime] = None, env: Optional[Mapping[str, str]] = None,
                    pace: Optional[_Pace] = None) -> Dict[str, Any]:
    """Mark as sent only the contacts Instantly can show a message for.

    The distinction this module is built around: enrolling is not sending. A row becomes
    ``sent`` when ``/emails`` returns a message for that address in this campaign, and the
    subject it actually went out with is recorded beside it -- so a blank subject would be
    visible in our own ledger rather than only in somebody's inbox.

    That rests on ``/emails`` honouring ``campaign_id``, which is NOT a safe assumption in
    this API: ``/leads/list`` silently ignores a ``campaign_id`` and returns the whole
    workspace. Verified against production 2026-10-03 with an affected address that has
    one old message and none from this campaign: unfiltered returns that message (from
    `8bfa0769`, subject ``''`` -- the incident itself), and filtered to this campaign
    returns nothing. So a receipt here means a message from THIS campaign. If that ever
    changed, every row would be marked sent against somebody else's blank message and the
    guard would pause a campaign that had done nothing.
    """
    moment = _now(now)
    with conn.cursor() as cur:
        cur.execute("SELECT email, verified_role FROM empty_email_recovery "
                    "WHERE state = 'enrolled' ORDER BY enrolled_at LIMIT %s", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()

    sent = 0
    unapproved: List[str] = []
    clock = pace if pace is not None else _Pace(
        _float_env(env if env is not None else os.environ, PACE_SECONDS_ENV,
                   DEFAULT_PACE_SECONDS))
    for row in rows:
        clock.wait()
        result = client.emails_for(row["email"],
                                   campaign_id=EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID, limit=10)
        if not result.ok:
            continue
        items = result.data.get("items") if isinstance(result.data, dict) else result.data
        message = None
        for item in (items or []):
            if isinstance(item, dict) and str(item.get("message_id") or item.get("id") or ""):
                message = item
                break
        if not message:
            continue
        subject = str(message.get("subject") or "")
        expected = "Your %s opening" % str(row["verified_role"] or "").strip()
        as_approved = subject.strip() == expected
        if not as_approved:
            unapproved.append(row["email"])
        sent += 1
        with transaction(conn) as tx:
            tx.execute(
                "UPDATE empty_email_recovery SET state = 'sent', sent_at = %s, "
                "message_id = %s, evidence = evidence || %s WHERE email = %s",
                (moment, str(message.get("message_id") or message.get("id") or "")[:200],
                 jsonb({"sent_subject": subject[:300], "subject_as_approved": as_approved,
                        "from": str(message.get("from_address_email") or "")[:200]}),
                 row["email"]))
    return {"stage": "receipts", "checked": len(rows), "sent": sent,
            "subject_not_as_approved": unapproved[:20],
            "subject_not_as_approved_count": len(unapproved)}


# -------------------------------------------------------------------------- guard


#: Harm this campaign could do that is worth stopping it for, rather than noting.
GUARD_PAUSE_REASONS = ("unapproved_subject_sent", "second_email_sent",
                       "sent_to_an_excluded_recipient")


def guard(conn: psycopg.Connection, client, *, pause: bool = True) -> Dict[str, Any]:
    """Look for evidence that this campaign is doing harm, and stop it if it is.

    The incident was not caught by a test; it was caught by a person reading an inbox
    weeks later. So the four things that would mean the repair had gone wrong are checked
    against what Instantly actually did, and finding any of them pauses the campaign
    instead of filing a ticket:

    * a message went out whose subject is not the approved one, which includes the blank
      subject the whole incident consisted of;
    * more messages were sent than there are recipients, which is a second email;
    * somebody received it whose authorisation had already been revoked;
    * a lead is enrolled carrying no role, so its subject line would be "Your  opening".

    Pausing is reversible and keeps every lead, its history and its position. It is the
    right response to uncertainty here: the cost of a needless pause is a delay, and the
    cost of not pausing is another 16,875 messages.
    """
    findings: List[Dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM empty_email_recovery "
            "WHERE state = 'sent' AND (evidence->>'subject_as_approved') = 'false'")
        wrong = int(cur.fetchone()["n"])
        cur.execute("SELECT count(*) AS n FROM empty_email_recovery WHERE state = 'sent'")
        sent_rows = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT count(*) AS n FROM empty_email_recovery r "
            "WHERE r.state = 'sent' AND EXISTS (SELECT 1 FROM suppressions s "
            "  WHERE s.kind = 'person_email' AND lower(s.key) = r.email AND s.source <> %s "
            "    AND s.created_at < r.sent_at)", (INCIDENT_SUPPRESSION_SOURCE,))
        excluded = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT count(*) AS n FROM empty_email_recovery "
            "WHERE state IN ('reserved', 'enrolled') AND length(btrim(verified_role)) = 0")
        roleless = int(cur.fetchone()["n"])
    conn.commit()

    if wrong:
        findings.append({"reason": "unapproved_subject_sent", "count": wrong})
    if excluded:
        findings.append({"reason": "sent_to_an_excluded_recipient", "count": excluded})
    if roleless:
        findings.append({"reason": "enrolled_without_a_role", "count": roleless})

    # One message per recipient is the whole authorisation, so the provider's own count of
    # messages sent is the cheapest test of it -- and it does not depend on our ledger
    # being right about who was enrolled.
    analytics = client.campaign_analytics()
    emails_sent = None
    if analytics.ok:
        items = analytics.data.get("items") if isinstance(analytics.data, dict) else analytics.data
        for item in (items or []):
            if isinstance(item, dict) and str(item.get("campaign_id") or item.get("id") or "")                     == EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID:
                try:
                    emails_sent = int(item.get("emails_sent_count") or 0)
                except (TypeError, ValueError):
                    emails_sent = None
                break
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM empty_email_recovery "
                    "WHERE state IN ('enrolled', 'sent')")
        ever_enrolled = int(cur.fetchone()["n"])
    conn.commit()
    if emails_sent is not None and emails_sent > ever_enrolled:
        findings.append({"reason": "second_email_sent", "emails_sent": emails_sent,
                         "recipients_ever_enrolled": ever_enrolled})

    paused = None
    must_stop = [f for f in findings if f["reason"] in GUARD_PAUSE_REASONS]
    if must_stop and pause:
        result = client.pause_campaign(EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID)
        paused = {"ok": bool(result.ok), "status": result.status,
                  "message": str(result.message)[:200]}
    return {"stage": "guard", "findings": findings, "sent_rows": sent_rows,
            "emails_sent_at_provider": emails_sent, "paused": paused,
            "campaign_paused_because": [f["reason"] for f in must_stop]}


# ------------------------------------------------------------------------- ledger


def ledger(conn: psycopg.Connection) -> Dict[str, Any]:
    """One row per recipient, counted by state. The answer to "how far along"."""
    with conn.cursor() as cur:
        cur.execute("SELECT state, count(*) AS n FROM empty_email_recovery GROUP BY 1")
        by_state = {str(r["state"]): int(r["n"]) for r in cur.fetchall()}
        cur.execute("SELECT count(*) AS n FROM empty_email_recovery "
                    "WHERE state = 'sent' AND (evidence->>'subject_as_approved') = 'false'")
        wrong_subject = int(cur.fetchone()["n"])
        cur.execute("SELECT state_reason, count(*) AS n FROM empty_email_recovery "
                    "WHERE state IN ('withheld', 'revoked') AND state_reason <> '' "
                    "GROUP BY 1 ORDER BY 2 DESC LIMIT 12")
        why = {str(r["state_reason"])[:80]: int(r["n"]) for r in cur.fetchall()}
    conn.commit()
    return {"recipients": sum(by_state.values()), "by_state": by_state,
            "internal_test": internal_test_passed(conn),
            "remaining_to_enrol": by_state.get("authorised", 0) + by_state.get("reserved", 0),
            "sent_with_an_unapproved_subject": wrong_subject,
            "withheld_or_revoked_reasons": why}


__all__ = ["load_queue", "revalidate", "free_slots", "enrol", "record_receipts",
           "internal_test_passed",
           "guard", "ledger", "GUARD_PAUSE_REASONS",
           "EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID", "INCIDENT_SUPPRESSION_SOURCE",
           "REVOKING_EVENTS", "STORAGE_FLOOR_ENV", "BATCH_ENV", "PLAN_ALLOWANCE_ENV",
           "MAY_ROTATE_ENV", "PACE_SECONDS_ENV", "DEFAULT_PACE_SECONDS",
           "DEFAULT_STORAGE_FLOOR", "DEFAULT_BATCH", "SENDABLE_STATES", "OPEN_STATES"]
