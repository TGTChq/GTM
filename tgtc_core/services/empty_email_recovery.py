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
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

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
#: Sized for the tick it runs in, not for the queue. A move costs about four provider
#: calls (read the lead, write its role, move it, read it back) and a create two, so at
#: the measured pace of 120 a minute a batch of 400 is roughly thirteen minutes -- inside
#: a fifteen-minute tick, with the run lock preventing an overlap if it is not.
#:
#: Enrolling is not sending. The campaign's own ``daily_max_leads`` of 500 paces the send
#: budget shared with the v2 campaigns, and while the campaign is paused nothing leaves at
#: all, so enrolling faster only queues people inside it.
BATCH_ENV = "TGTC_RECOVERY_BATCH"
DEFAULT_BATCH = 400

#: Seconds between provider calls.
#:
#: The Instantly client has no rate handling of its own -- unlike the Airtable and Apollo
#: clients, a 429 falls through it as a plain failure -- so the pacing lives here rather
#: than being assumed.
#:
#: MEASURED 2026-10-04, having previously been assumed: 60 unpaced GETs completed in 21.1
#: seconds, 171 a minute, with zero 429s and no rate-limit headers on any response. The
#: 3 seconds this used to wait came from a scratchpad client's guess at "20 a minute", and
#: it was the difference between a load taking three hours and nineteen. 0.5 s is 120 a
#: minute, which leaves a 30% margin under what was observed rather than riding it.
PACE_SECONDS_ENV = "TGTC_RECOVERY_PACE_SECONDS"
DEFAULT_PACE_SECONDS = 0.5

#: Measured 2026-09-24 on plan pid_hg_v1 (HyperGrowth): the allowance is a stock of
#: stored contacts, not a monthly grant.
PLAN_ALLOWANCE_ENV = "TGTC_INSTANTLY_LEAD_ALLOWANCE"
DEFAULT_PLAN_ALLOWANCE = 25000

#: Does the automation enrol at all?
#:
#: OFF by default, and that is the handover state: the queue is loaded, the campaign is
#: paused, and whether to test and activate is the team's decision rather than something
#: a cron reaches on its own. Setting it is how the bulk load was performed and how a
#: later batch would be.
ENROL_ENABLED_ENV = "TGTC_RECOVERY_ENROL_ENABLED"

#: May a short tick ask the already-authorised rotation to free slots?
#:
#: Off by default, deliberately. Rotation deletes contacts, and although every guard is
#: its own -- a protected campaign, an unfinished sequence, a reply, one of our own
#: suppressions, a pending delivery, and a durable backup of every record before it goes
#: -- the first batches of this repair should be watched before a deletion path is
#: attached to them. With it off, the queue simply waits at the floor, which the health
#: alert reports.
MAY_ROTATE_ENV = "TGTC_RECOVERY_MAY_ROTATE"

#: The approved copy, as sentences a RECEIVED message must actually contain.
#:
#: Not a template to render from -- the campaign holds the words and Instantly renders
#: them. This is the other direction: what has to be true of the message that came back,
#: so "we checked the copy" is a measurement rather than a belief. Kept as the approved
#: text, with the two per-recipient values substituted where the copy puts them.
APPROVED_GREETING = "Hi {first_name},"
APPROVED_SENTENCES = (
    u"An earlier email from us went out without its message\u2014sorry about that.",
    u"I wanted to reach out about your {verified_role} opening. The Global Talent Co. "
    u"helps companies hire vetted international professionals matched to the role.",
    u"Would it be useful to see a few relevant profiles?",
)
APPROVED_SUBJECT = "Your {verified_role} opening"

#: The signature renders to the sender's own block; this is the part of it that is the
#: same whoever sends, so its presence is checkable without pinning one mailbox's name.
SIGNATURE_MARKER = "The Global Talent Co."

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
            "SELECT email, sent_at, evidence->>'sent_subject' AS subject, "
            "       evidence->'received_checks' AS checks, "
            "       evidence->'received_failed' AS failed "
            "FROM empty_email_recovery WHERE is_internal_test AND state = 'sent' "
            "ORDER BY sent_at LIMIT 1")
        row = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM empty_email_recovery WHERE is_internal_test")
        tests = int(cur.fetchone()["n"])
    conn.commit()
    if not row:
        return {"passed": False, "test_rows": tests, "why": "no internal test has been sent"}
    checks = row["checks"] if isinstance(row["checks"], dict) else {}
    failed = sorted(k for k, ok in checks.items() if not ok)
    # Every check, not a majority and not the subject alone. A missing checks object is a
    # failure too: it means the message was recorded by code that did not read the body.
    if not checks or failed:
        return {"passed": False, "test_rows": tests, "address": row["email"],
                "failed": failed or ["the received email was never checked"],
                "why": "the internal test email did not pass every check"}
    return {"passed": True, "test_rows": tests, "address": row["email"],
            "sent_at": row["sent_at"], "subject": row["subject"], "checks": checks}


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
                         is_internal_test, source_kind, source_id, instantly_lead_id,
                         source_lead_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
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
                     bool(row.get("is_internal_test")),
                     str(row.get("source_kind") or "unknown"),
                     str(row.get("source_id") or "") or None,
                     str(row.get("instantly_lead_id") or "") or None,
                     row.get("source_lead_status")))
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
    return {"inserted": inserted, "already_present": skipped, "refused_incomplete": refused}


def set_route(conn: psycopg.Connection, routes: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Record how each recipient reaches the campaign, for rows that do not say yet.

    Separate from ``load_queue`` because the queue was loaded before the measurement that
    showed a move costs no storage, and because a route can go stale: rotation can remove
    a lead between the route being recorded and the move being attempted, which the move
    itself detects and sends back here as a create.

    Only ever sets a route on a row that is still waiting. A row already enrolled or sent
    has been through its route and must not have it rewritten underneath it.
    """
    updated = skipped = refused = 0
    with transaction(conn) as tx:
        with tx.cursor() as cur:
            for route in routes:
                email = str(route.get("email") or "").strip().lower()
                kind = str(route.get("source_kind") or "").strip()
                if kind not in ("campaign", "list", "absent"):
                    refused += 1
                    continue
                lead_id = str(route.get("instantly_lead_id") or "") or None
                source_id = str(route.get("source_id") or "") or None
                if kind in ("campaign", "list") and not (lead_id and source_id):
                    refused += 1
                    continue
                cur.execute(
                    "UPDATE empty_email_recovery SET source_kind = %s, source_id = %s, "
                    "instantly_lead_id = %s, source_lead_status = %s "
                    "WHERE email = %s AND state = ANY(%s)",
                    (kind, source_id, lead_id, route.get("source_lead_status"), email,
                     list(SENDABLE_STATES)))
                if cur.rowcount:
                    updated += 1
                else:
                    skipped += 1
    return {"routed": updated, "not_waiting_any_more": skipped, "refused": refused}


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


def verify_received(message: Mapping[str, Any], row: Mapping[str, Any]) -> Dict[str, Any]:
    """Judge the email that ARRIVED against the approved copy, check by check.

    This is the gate's real question, and it is deliberately not "did Instantly record a
    send" or "does the subject match". The incident was 16,875 messages whose enrolment,
    status and delivery were all fine and whose body was a signature; a send record would
    have reported every one of them as a success.

    So: the recipient, the whole subject, the whole body, the greeting carrying this
    person's name, the role carrying their vacancy, the signature rendered rather than
    left as a placeholder, and not one variable still unresolved. Returns every check by
    name, because "it failed" is not actionable and "which one failed" is.
    """
    from ..domain.outbound_copy import _visible

    first = str(row.get("first_name") or "").strip()
    role = str(row.get("verified_role") or "").strip()
    address = str(row.get("email") or "").strip().lower()

    subject = str(message.get("subject") or "")
    body_field = message.get("body")
    html = ""
    if isinstance(body_field, dict):
        html = str(body_field.get("html") or body_field.get("text") or "")
    elif isinstance(body_field, str):
        html = body_field
    text = _visible(html)
    recipients = {str(message.get("lead") or "").strip().lower()}
    recipients |= {a.strip().lower() for a in
                   str(message.get("to_address_email_list") or "").split(",") if a.strip()}

    greeting = APPROVED_GREETING.format(first_name=first)
    sentences = [t.format(verified_role=role) for t in APPROVED_SENTENCES]
    # The signature follows the last approved sentence, so anything after it is what
    # rendered in place of the placeholder.
    tail = ""
    if sentences[-1] in text:
        tail = text.split(sentences[-1], 1)[1].strip()

    checks = {
        "went_to_the_right_person": bool(address) and address in recipients,
        "subject_is_the_approved_one": subject.strip() == APPROVED_SUBJECT.format(
            verified_role=role),
        "body_arrived_at_all": len(text) >= 120,
        "greeting_carries_their_name": bool(first) and greeting in text,
        "role_is_in_the_subject": bool(role) and role in subject,
        "role_is_in_the_body": bool(role) and role in text,
        "every_approved_sentence_is_there": all(t in text for t in sentences),
        "nothing_was_added_after_the_signature": True,   # refined below
        "the_signature_rendered": bool(tail) and SIGNATURE_MARKER in tail,
        "exactly_one_signature": tail.count(SIGNATURE_MARKER) == 1,
        "no_variable_is_still_pending": ("{{" not in subject and "}}" not in subject
                                         and "{{" not in text and "}}" not in text),
        "no_placeholder_name_leaked": not any(
            token in (subject + " " + text)
            for token in ("accountSignature", "verified_role", "firstName", "first_name")),
        "it_came_from_one_of_our_mailboxes": "@" in str(message.get("from_address_email") or ""),
        "it_is_the_first_step": str(message.get("step") or "0_0_0").startswith("0_"),
    }
    # The signature is the last thing in the message: nothing may follow it.
    if checks["the_signature_rendered"]:
        after = tail.split(SIGNATURE_MARKER, 1)[1].strip()
        checks["nothing_was_added_after_the_signature"] = len(after) <= 80

    failed = sorted(k for k, ok in checks.items() if not ok)
    return {"passed": not failed, "failed": failed, "checks": checks,
            "subject": subject[:300], "body_text": text[:1200],
            "recipients": sorted(r for r in recipients if r)}


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


def lead_variables(lead: Mapping[str, Any]) -> Dict[str, Any]:
    """A lead's variables as Instantly really stores them: FLATTENED into ``payload``.

    Measured 2026-10-03. A lead created with ``custom_variables: {verified_role: ...}``
    reads back as::

        payload = {"firstName": "Devan", "lastName": "M", "companyName": "...",
                   "email": "...", "campaign": "...", "verified_role": "Operations Manager"}

    There is no ``payload.custom_variables``. Looking for one returns nothing, which an
    earlier version of this module treated as "the variable is missing" -- it would have
    refused every single enrolment and the recovery would have reported a clean,
    consistent, total failure. Hence a named function with the measurement attached.
    """
    payload = lead.get("payload")
    if isinstance(payload, dict):
        out = dict(payload)
    else:
        out = {}
    nested = out.get("custom_variables")
    if isinstance(nested, dict):                 # tolerated, never relied on
        out.update(nested)
    return out


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
    variables = lead_variables(data)
    for key in EMPTY_EMAIL_RECOVERY_REQUIRED_VARIABLES:
        value = str(variables.get(key) or "").strip()
        if not value:
            return "verified_missing:%s" % key
        if value != str(row["verified_role"] or "").strip():
            return "verified_changed:%s" % key
    if not str(variables.get("firstName") or data.get("first_name") or "").strip():
        return "verified_missing:first_name"
    return ""


#: Instantly campaign statuses. 2 is paused: it holds its leads and sends nothing.
CAMPAIGN_PAUSED = 2

#: Instantly's own verdict on a lead, and what it means for a repair email.
LEAD_BOUNCED = -1
LEAD_UNSUBSCRIBED = -2
#: Statuses that end the authorisation on sight. A bounced address never received the
#: blank email either, and writing to it again spends deliverability for nothing.
REFUSING_LEAD_STATUSES = {LEAD_BOUNCED: "provider_bounced",
                          LEAD_UNSUBSCRIBED: "provider_unsubscribed"}


def campaign_is_paused(client) -> Dict[str, Any]:
    """Is the recovery campaign paused right now, according to the provider?

    The question that replaces the internal-test gate for LOADING. Enrolling somebody
    into a paused campaign cannot email them: their lead sits there with its copy ready
    and nothing goes out. So the whole queue can be loaded and left for the team to
    decide on, which is not the same thing as making it sendable.

    Read from the provider on every batch, never from configuration. The campaign was
    activated once in this incident and paused again; a stale belief about which it is now
    is exactly the kind of thing that sends 16,875 emails.
    """
    result = client.get_campaign(EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID)
    if not result.ok:
        return {"known": False, "paused": False,
                "reason": str(result.message)[:200] or str(result.status)}
    status = (result.data or {}).get("status")
    try:
        status = int(status)
    except (TypeError, ValueError):
        return {"known": False, "paused": False, "reason": "unreadable status %r" % status}
    return {"known": True, "paused": status == CAMPAIGN_PAUSED, "status": status}


def _settle(client, lead_id: str, *, want_campaign: str, tries: int = 6,
            sleep=None) -> Dict[str, Any]:
    """Wait for a move to actually land. ``/leads/move`` answers 200 with a pending job.

    A 200 here means accepted, not done -- the response is a background job whose status
    is ``pending``. So the lead is read back until it reports the campaign we asked for,
    and an exhausted wait returns what it last saw rather than pretending.

    The waits back off from half a second rather than being a flat three. Measured during
    the 2026-10-04 load, a flat 3 s was costing about 3 s per contact and pinning the
    whole load at 14 a minute while the rate limit allowed 120: the job usually lands
    inside a second, so waiting three for it was most of the cost of the operation.
    Backing off 0.5, 1, 2, 4, 8 still gives a slow job 15 s to finish.
    """
    import time

    nap = sleep or time.sleep
    last: Dict[str, Any] = {}
    delay = 0.5
    for attempt in range(tries):
        result = client.get_lead(lead_id)
        if result.ok and isinstance(result.data, dict):
            last = result.data
            if str(last.get("campaign") or "") == want_campaign:
                return last
        if attempt < tries - 1:
            nap(delay)
            delay = min(delay * 2, 8.0)
    return last


def _bring_across(conn: psycopg.Connection, client, row: Mapping[str, Any], *,
                  moment: datetime, clock) -> Tuple[str, str]:
    """Move one existing record into the recovery campaign. Returns (state, reason).

    Order matters and is the whole point. The recovery campaign is ACTIVE, so a lead that
    arrived without its role could be sent "Your  opening" inside the next window -- the
    incident, reproduced by the repair. So the role is written and READ BACK first, and
    only a lead that already carries it is moved.

    The lead's own record is backed up to the database before anything changes it, so a
    move is reversible from production rather than only from a file on a workstation.

    Their old sequence is abandoned: a move clears ``status_summary``, which is authorised
    and is the point -- they are to receive this one email and nothing else. They are
    never moved back, and their four original steps are never restarted.

    Safe to re-run on a row an earlier attempt left ``reserved``: the lead is re-read, a
    role that is already right is not re-written, and a lead already in the destination
    is not moved again.
    """
    lead_id = str(row["instantly_lead_id"] or "")
    source_kind = str(row["source_kind"] or "")
    source_id = str(row["source_id"] or "")
    role = str(row["verified_role"] or "").strip()

    clock.wait()
    current = client.get_lead(lead_id)
    if not current.ok:
        if current.status == 404:
            # Rotation removed it since the route was recorded. It needs a create, and a
            # create needs a slot, so it goes back to the queue saying so.
            return "authorised", "record_gone_needs_creation"
        return "authorised", "unreadable:%s" % (current.status or "error")
    lead = current.data if isinstance(current.data, dict) else {}

    status = lead.get("status")
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = None
    if status in REFUSING_LEAD_STATUSES:
        return "withheld", REFUSING_LEAD_STATUSES[status]
    if str(lead.get("email") or "").strip().lower() != str(row["email"]).strip().lower():
        return "withheld", "lead_id_belongs_to_another_address"

    with transaction(conn) as tx:
        tx.execute("UPDATE empty_email_recovery SET source_backup = %s, "
                   "source_lead_status = %s WHERE email = %s AND source_backup IS NULL",
                   (jsonb(lead), status, row["email"]))

    variables = lead_variables(lead)
    if str(variables.get("verified_role") or "").strip() != role:
        # A PATCH with custom_variables REPLACES the whole set, so the existing payload is
        # merged rather than overwritten; dropping what is already there would strip the
        # provenance these leads carry.
        merged = {k: v for k, v in variables.items()
                  if k not in ("email", "campaign") and v is not None}
        merged["verified_role"] = role
        clock.wait()
        patched = client.update_lead(lead_id, {"custom_variables": merged})
        if not patched.ok:
            return "authorised", "patch_failed:%s" % (patched.status or "error")
        # Read-after-write here is eventually consistent, so this is checked after the
        # move rather than immediately, where a correct write reads back as a failure.

    # Already there? Then the move happened and something lost the answer -- a container
    # replaced mid-batch, a deploy, a crash between the move and the record. Asking for
    # the move again would be asking to move it out of a campaign it has already left,
    # which fails and would park the row forever. This is what makes an interrupted load
    # resumable rather than stuck.
    if str(lead.get("campaign") or "") == EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID:
        settled = lead
    else:
        clock.wait()
        moved = client.move_lead_from(lead_id, source_kind=source_kind,
                                      source_id=source_id,
                                      to_campaign=EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID)
        if not moved.ok:
            return "authorised", "move_failed:%s" % (moved.status or "error")
        settled = _settle(client, lead_id,
                          want_campaign=EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID)
    if str(settled.get("campaign") or "") != EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID:
        return "authorised", "move_not_settled"
    after = lead_variables(settled)
    if str(after.get("verified_role") or "").strip() != role:
        return "authorised", "moved_without_its_role"
    if not str(after.get("firstName") or settled.get("first_name") or "").strip():
        return "authorised", "moved_without_a_name"
    return "enrolled", ""


def enrol(conn: psycopg.Connection, client, *, limit: Optional[int] = None,
          env: Optional[Mapping[str, str]] = None, now: Optional[datetime] = None,
          dry_run: bool = False, pace: Optional[_Pace] = None) -> Dict[str, Any]:
    """Create one batch of recovery contacts, and verify each one by id.

    Two routes, and the difference is the whole capacity question. 5,892 of the 5,934
    recipients are already stored -- in the nine paused campaigns or on the incident hold
    list -- so they are MOVED, which was measured to cost no storage at all. Only the 42
    whose record rotation already removed are CREATED, and only a create needs a slot.

    Returns what happened, never a bare success, and reports the two routes separately so
    a move is never mistaken for a new creation. A contact is only ``enrolled`` once it
    has been read back from Instantly, in this campaign, carrying the role its subject
    line needs and a name for its greeting.
    """
    environ = env if env is not None else os.environ
    moment = _now(now)
    batch = limit if limit is not None else _int_env(environ, BATCH_ENV, DEFAULT_BATCH)

    if str(environ.get(ENROL_ENABLED_ENV, "")).strip() not in ("1", "true", "True"):
        return {"stage": "enrol", "enrolled": 0, "stopped": "enrolment_disabled",
                "note": "the queue is loaded and the campaign is paused; enrolling is a "
                        "deliberate act, not something a tick reaches on its own"}

    # Somebody may only be enrolled into this campaign when it cannot email them yet.
    # Either the campaign is PAUSED -- so a lead sits in it with its copy ready and
    # nothing goes out -- or one of our own addresses has already RECEIVED this email and
    # all of it checked out. Without one of those two, nobody is touched. On 2026-09-21
    # the 7,777 recipients were the test; that is what this refuses to repeat.
    gate = internal_test_passed(conn)
    paused = campaign_is_paused(client)
    may_enrol = bool(paused.get("paused")) or bool(gate["passed"])
    if not may_enrol:
        return {"stage": "enrol", "enrolled": 0, "internal_test": gate,
                "campaign": paused,
                "stopped": "campaign_is_live_and_the_internal_test_has_not_passed"}

    # While the campaign is paused the whole queue may load. Once it is live, only what
    # the proven test covers.
    load_everybody = bool(paused.get("paused")) or bool(gate["passed"])
    with conn.cursor() as cur:
        sql = ("SELECT * FROM empty_email_recovery WHERE state = ANY(%s) "
               + ("" if load_everybody else "AND is_internal_test ")
               # Movable recipients first: they cost no storage, so a tight workspace
               # never stops the bulk of the recovery.
               + "ORDER BY is_internal_test DESC, (source_kind = 'absent'), "
                 "authorised_at, email LIMIT %s")
        cur.execute(sql, (list(SENDABLE_STATES), batch))
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()

    movable = [r for r in rows if str(r["source_kind"]) in ("campaign", "list")]
    wanted_create = [r for r in rows if str(r["source_kind"]) not in ("campaign", "list")]

    # Capacity constrains the CREATES only. A move relocates a contact the plan already
    # counts, measured 2026-10-03, so it needs no slot and must not be made to wait for
    # one. Claiming the whole recovery needs 5,934 slots would be wrong by 5,892.
    rotation = None
    held_for_storage = 0
    if not wanted_create:
        capacity = {"measured": None, "available": 0,
                    "reason": "not consulted: nothing in this batch needs a slot"}
        needs_create = []
    else:
        capacity = free_slots(client, env=environ)
        if not capacity.get("measured"):
            needs_create = []
        else:
            room = int(capacity.get("available") or 0)
            # Rotation deletes contacts. It is authorised, but it does not run on behalf
            # of a repair that has not yet proved it can send a correct email: the
            # internal test comes first, then capacity is made for real recipients.
            may_rotate = (str(environ.get(MAY_ROTATE_ENV, "")).strip()
                          in ("1", "true", "True"))
            if room < len(wanted_create) and may_rotate:
                rotation = _make_room(conn, client, want=len(wanted_create), env=environ,
                                      now=moment)
                capacity = free_slots(client, env=environ)
                room = int(capacity.get("available") or 0)
            needs_create = wanted_create[:max(0, room)]
        held_for_storage = len(wanted_create) - len(needs_create)

    rows = movable + needs_create
    if not rows:
        if not load_everybody:
            stopped = "awaiting_internal_test"
        elif held_for_storage:
            stopped = ("capacity_unknown" if not capacity.get("measured")
                       else "storage_floor_reached")
        else:
            stopped = "queue_empty"
        return {"stage": "enrol", "enrolled": 0, "capacity": capacity,
                "internal_test": gate, "campaign": paused, "rotation": rotation,
                "held_for_storage": held_for_storage, "stopped": stopped}
    if dry_run:
        return {"stage": "plan", "enrolled": 0, "would_enrol": len(rows),
                "would_move": len(movable), "would_create": len(needs_create),
                "held_for_storage": held_for_storage, "capacity": capacity,
                "internal_test": gate, "campaign": paused,
                "sample": [r["email"] for r in rows[:5]]}

    created = refused = failed = moved = 0
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
        if str(row["source_kind"]) in ("campaign", "list"):
            state, reason = _bring_across(conn, client, row, moment=moment, clock=clock)
            if state == "enrolled":
                moved += 1
                with transaction(conn) as tx:
                    tx.execute(
                        "UPDATE empty_email_recovery SET state = 'enrolled', "
                        "enrolled_at = %s, last_error = '', state_reason = '' "
                        "WHERE email = %s", (moment, row["email"]))
            else:
                if state == "withheld":
                    refused += 1
                else:
                    failed += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                with transaction(conn) as tx:
                    tx.execute(
                        "UPDATE empty_email_recovery SET state = %s, state_reason = %s, "
                        "last_error = %s WHERE email = %s",
                        (state, reason if state == "withheld" else "",
                         "" if state == "withheld" else reason, row["email"]))
            continue

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
    return {"stage": "enrol", "attempted": len(rows), "enrolled": created + moved,
            "moved_existing_records": moved, "created_new_records": created,
            "withheld": refused, "failed": failed, "capacity": capacity,
            "rotation": rotation, "held_for_storage": held_for_storage,
            "internal_test": gate, "campaign": paused, "stopped": stopped,
            "reasons": reasons}


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
        cur.execute("SELECT email, first_name, verified_role, is_internal_test "
                    "FROM empty_email_recovery WHERE state = 'enrolled' "
                    "ORDER BY is_internal_test DESC, enrolled_at LIMIT %s", (limit,))
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
        verdict = verify_received(message, row)
        if not verdict["passed"]:
            unapproved.append({"email": row["email"], "failed": verdict["failed"]})
        sent += 1
        with transaction(conn) as tx:
            tx.execute(
                "UPDATE empty_email_recovery SET state = 'sent', sent_at = %s, "
                "message_id = %s, evidence = evidence || %s WHERE email = %s",
                (moment, str(message.get("message_id") or message.get("id") or "")[:200],
                 jsonb({"sent_subject": verdict["subject"],
                        # The name is historical: it is now the verdict on the WHOLE
                        # received email, not on its subject line.
                        "subject_as_approved": verdict["passed"],
                        "received_checks": verdict["checks"],
                        "received_failed": verdict["failed"],
                        "received_body": verdict["body_text"],
                        "from": str(message.get("from_address_email") or "")[:200]}),
                 row["email"]))
    return {"stage": "receipts", "checked": len(rows), "sent": sent,
            "not_as_approved": unapproved[:20],
            "not_as_approved_count": len(unapproved)}


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
            "WHERE state = 'sent' AND (evidence->>'subject_as_approved') IS DISTINCT FROM 'true'")
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


__all__ = ["load_queue", "set_route", "revalidate", "free_slots", "enrol", "record_receipts",
           "internal_test_passed",
           "guard", "ledger", "GUARD_PAUSE_REASONS", "verify_received",
           "APPROVED_SENTENCES", "APPROVED_SUBJECT", "APPROVED_GREETING",
           "SIGNATURE_MARKER",
           "EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID", "INCIDENT_SUPPRESSION_SOURCE",
           "REVOKING_EVENTS", "STORAGE_FLOOR_ENV", "BATCH_ENV", "PLAN_ALLOWANCE_ENV",
           "MAY_ROTATE_ENV", "ENROL_ENABLED_ENV", "campaign_is_paused",
           "CAMPAIGN_PAUSED", "PACE_SECONDS_ENV", "DEFAULT_PACE_SECONDS",
           "DEFAULT_STORAGE_FLOOR", "DEFAULT_BATCH", "SENDABLE_STATES", "OPEN_STATES"]
