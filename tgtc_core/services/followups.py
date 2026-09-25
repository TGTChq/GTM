"""What happens when an out-of-office reply says "back on the 6th" and the 6th arrives.

Measured against the live workspace on 2026-09-25, because the answer decides what this
step can honestly do:

* the nine campaigns run `stop_on_reply: true`, so an out-of-office auto-answer **ends**
  that contact's sequence. All 75 contacts who had replied across the nine campaigns sat
  at lead status 3; not one was still in sequence;
* Instantly exposes **no way to resume a finished lead** -- `/leads/{id}/resume` and
  `/leads/{id}/restart` are both "route not found", and `PATCH /leads/{id}` answers 200
  while silently ignoring `status`;
* `POST /leads/subsequence/move` **does** accept a finished lead, and it keeps them in
  their own campaign. It was tried on one real contact: 200, `subsequence_id` set, and
  the lead flipped from status 3 to status 1. That flip is the problem. Instantly then
  counts the contact in the parent campaign's ACTIVE pool, and
  `POST /leads/subsequence/remove` does **not** undo it -- it clears the subsequence and
  leaves the contact active in a live four-step campaign. There is no route back to
  status 3. Until a subsequence is proven to send its one step and return the contact to
  finished, that door stays shut;
* moving the contact to a **campaign whose sequence is one step** is therefore the
  mechanism, and campaign membership -- not a status field -- is what bounds what they
  can receive.

So a queued `reply_followup_when_back` item decides each case on its merits when the
return date arrives:

* somebody who opted out, or whose address is suppressed, is **closed and never written
  to** -- an out-of-office must never become an opt-out, and an opt-out must never
  become a follow-up;
* a unit whose vacancy has gone is closed as `no_current_vacancy`: there is no longer a
  reason to write;
* with a one-step campaign configured, the contact is moved there and the move is
  **confirmed against Instantly's own record of where they are**. `POST /leads/move`
  answers 200 with a background job whose status is `pending`, so the response means
  accepted, not done; an unconfirmed move waits and is tried again. What this proves is
  that the contact is now somewhere that can only send one message -- the message itself
  is then the campaign's to send, which is why the count is called
  `moved_to_followup` and not `sent`;
* a contact whose sequence is still running is **held, never moved** -- moving them
  would stop emails that are already going out, and this step is for somebody whose
  sequence has already ended;
* without a one-step campaign, the case is marked verified and due and waits for a
  mechanism rather than pretending to be one.

Nothing here suppresses, and nothing here re-enrols anybody in the four emails they
already received.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import psycopg

from ..db.connection import transaction
from ..policy.campaigns import KNOWN_CHALLENGER_CAMPAIGN_IDS, KNOWN_CONTROL_CAMPAIGN_IDS

WORK_KIND = "reply_followup_when_back"
#: A campaign whose sequence is ONE step, used for nothing else. Moving the contact
#: there is the follow-up: one appropriate message, not the four they already had.
FOLLOWUP_CAMPAIGN_ENV = "TGTC_OOO_FOLLOWUP_CAMPAIGN_ID"
NEEDS_MECHANISM = "needs_followup_mechanism"
#: Instantly refused the move, or we could not reach it at all.
MOVE_FAILED = "followup_move_failed"
#: Instantly accepted the move as a background job and has not carried it out yet.
MOVE_UNCONFIRMED = "followup_move_unconfirmed"
#: The contact is still being emailed where they are. Moving them would stop that.
STILL_IN_SEQUENCE = "followup_contact_still_in_sequence"
#: Instantly's lead status for "in sequence".
IN_SEQUENCE = 1
NO_CURRENT_VACANCY = "no_current_vacancy"
PER_RUN_ENV = "TGTC_FOLLOWUPS_PER_RUN"
DEFAULT_PER_RUN = 50
#: How long a verified case waits before it is looked at again. Long enough not to churn,
#: short enough that a suppression arriving later still closes it.
RECHECK_DAYS = 7


def per_run(env: Optional[Dict[str, str]] = None) -> int:
    raw = str((env or {}).get(PER_RUN_ENV, "") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_PER_RUN
    except ValueError:
        value = DEFAULT_PER_RUN
    return max(0, value)


def followup_campaign(env: Optional[Dict[str, str]] = None) -> str:
    """The configured one-step follow-up campaign, or "" when there is none.

    It may never be one of the nine Challenger campaigns or one of the nine Control
    campaigns: moving somebody into those would restart a four-step sequence, which is
    the one thing this must not do.
    """
    import os as _os

    cid = str((env if env is not None else _os.environ).get(FOLLOWUP_CAMPAIGN_ENV, "") or "").strip()
    if not cid:
        return ""
    if cid in KNOWN_CHALLENGER_CAMPAIGN_IDS or cid in KNOWN_CONTROL_CAMPAIGN_IDS:
        raise ValueError(f"{FOLLOWUP_CAMPAIGN_ENV} must be a dedicated one-step campaign, "
                         "never a Challenger or Control campaign")
    return cid


def _created_lead(conn: psycopg.Connection, approval_id: int) -> Optional[Dict[str, str]]:
    """The Instantly lead this approval genuinely created, from our own receipt."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.external_id AS lead_id, r.external_campaign AS campaign
            FROM delivery_receipts r JOIN delivery_outbox o ON o.id = r.outbox_id
            WHERE o.approval_id = %s AND r.channel = 'instantly' AND r.receipt_kind = 'created'
              AND r.external_id IS NOT NULL AND r.external_campaign IS NOT NULL
            ORDER BY r.received_at DESC LIMIT 1
            """, (approval_id,))
        row = cur.fetchone()
    conn.commit()
    return {"lead_id": str(row["lead_id"]), "campaign": str(row["campaign"])} if row else None


#: Instantly has no record of this contact any more.
GONE = object()


def _lead_now(instantly: Any, lead_id: str) -> Any:
    """What Instantly says about this contact RIGHT NOW: their campaign and their status.

    ``GONE`` when the contact no longer exists, ``None`` when we could not find out. Used
    to pick the source of a move, to refuse to interrupt a sequence that is still
    running, and to confirm afterwards that the move happened -- the move itself only
    answers with an accepted background job.
    """
    result = instantly.get_lead(lead_id)
    if getattr(result, "ok", False):
        return getattr(result, "data", None) or {}
    return GONE if getattr(result, "status", None) == 404 else None


def _done(conn: psycopg.Connection, work_id: int, reason: str) -> None:
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET state = 'done', close_reason = %s, updated_at = now() "
                        "WHERE id = %s AND state IN ('ready', 'waiting')", (reason[:200], work_id))


def _close(conn: psycopg.Connection, work_id: int, reason: str) -> None:
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET state = 'closed', close_reason = %s, updated_at = now() "
                        "WHERE id = %s AND state IN ('ready', 'waiting')", (reason[:200], work_id))


def _hold(conn: psycopg.Connection, work_id: int, until: datetime,
          waiting_on: str = NEEDS_MECHANISM) -> None:
    """Wait, and say what for. Three different things hold a follow-up and they are not
    the same problem: no mechanism configured, a transport that refused, and a move
    Instantly accepted but has not carried out yet."""
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET state = 'waiting', waiting_on = %s, available_at = %s, "
                        "updated_at = now() WHERE id = %s AND state IN ('ready', 'waiting')",
                        (waiting_on[:200], until, work_id))


def due_followups(conn: psycopg.Connection, *, now: Optional[datetime] = None,
                  limit: int = DEFAULT_PER_RUN, instantly: Any = None,
                  env: Optional[Dict[str, str]] = None) -> Dict[str, int]:
    """Decide every follow-up whose return date has arrived.

    With a one-step follow-up campaign configured it MOVES the contact there -- exactly
    one further message. Without one it decides and holds, because the alternative
    (re-adding the contact to their own campaign) would replay four emails at somebody
    who only said they were away.
    """
    moment = now or datetime.now(timezone.utc)
    out = {"considered": 0, "suppressed": 0, "no_current_vacancy": 0, "verified_due": 0,
           "moved_to_followup": 0, "no_recorded_creation": 0, "move_failed": 0,
           "move_unconfirmed": 0, "lead_gone": 0, "still_in_sequence": 0}
    destination = followup_campaign(env)
    if limit <= 0:
        return out
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT w.id AS work_id, a.id AS approval_id, a.state AS approval_state,
                   lower(coalesce(p.email, '')) AS email,
                   coalesce(p.opt_out_status, '') AS opt_out,
                   po.state AS posting_state,
                   (SELECT count(*) FROM suppressions s
                     WHERE s.kind = 'person_email' AND s.key = lower(coalesce(p.email, ''))) AS suppressed
            FROM work_items w
            JOIN approvals a ON a.id = w.subject_id
            JOIN people p ON p.id = a.person_id
            LEFT JOIN postings po ON po.id = (a.lead_json->>'posting_id')::bigint
            WHERE w.kind = %s AND w.subject_kind = 'approval' AND w.state IN ('ready', 'waiting')
              AND w.available_at <= %s
            ORDER BY w.available_at, w.id LIMIT %s
            """,
            (WORK_KIND, moment, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()

    for row in rows:
        out["considered"] += 1
        work_id = int(row["work_id"])
        if int(row["suppressed"] or 0) or str(row["opt_out"]).strip():
            # Whatever put it there -- an opt-out, a departure, a bounce -- this address
            # is not written to again, and the follow-up ends here.
            _close(conn, work_id, f"suppressed:{row['opt_out'] or 'person_email'}")
            out["suppressed"] += 1
            continue
        if row["approval_state"] == "revoked" or (row["posting_state"] or "closed") != "classified":
            _close(conn, work_id, f"{NO_CURRENT_VACANCY}:{row['posting_state'] or 'no_posting'}")
            out["no_current_vacancy"] += 1
            continue
        if not destination or instantly is None:
            _hold(conn, work_id, moment + timedelta(days=RECHECK_DAYS))
            out["verified_due"] += 1
            continue
        lead = _created_lead(conn, int(row["approval_id"]))
        if lead is None:
            # Nothing was ever created for this approval, so there is nobody to move.
            _close(conn, work_id, "no_recorded_creation")
            out["no_recorded_creation"] += 1
            continue
        current = _lead_now(instantly, lead["lead_id"])
        if current is GONE:
            # Rotation or a person's own request removed them. Nobody to write to.
            _close(conn, work_id, "lead_no_longer_in_instantly")
            out["lead_gone"] += 1
            continue
        where = str((current or {}).get("campaign") or "")
        if current is None or not where:
            # We could not find out where they are, so we do not guess. Try tomorrow.
            _hold(conn, work_id, moment + timedelta(days=1), MOVE_FAILED)
            out["move_failed"] += 1
            continue
        if where == destination:
            _done(conn, work_id, f"already_in_followup:{lead['lead_id']}")
            out["moved_to_followup"] += 1
            continue
        if int(current.get("status") or 0) == IN_SEQUENCE:
            # They are still being emailed where they are. Moving them would stop a
            # sequence that is running, and nobody asked for that; the follow-up is for
            # somebody whose sequence has already ended.
            _hold(conn, work_id, moment + timedelta(days=1), STILL_IN_SEQUENCE)
            out["still_in_sequence"] += 1
            continue
        result = instantly.move_lead(lead["lead_id"], from_campaign=where, to_campaign=destination)
        if not getattr(result, "ok", False):
            # A transport problem is a wait, never a lost follow-up.
            _hold(conn, work_id, moment + timedelta(days=1), MOVE_FAILED)
            out["move_failed"] += 1
            continue
        if str((_lead_now(instantly, lead["lead_id"]) or {}).get("campaign") or "") == destination:
            _done(conn, work_id, f"moved_to_followup:{lead['lead_id']}")
            out["moved_to_followup"] += 1
        else:
            # 200 meant the job was accepted. Only Instantly's own record of the
            # contact settles whether it happened, and it does not say so yet.
            _hold(conn, work_id, moment + timedelta(days=1), MOVE_UNCONFIRMED)
            out["move_unconfirmed"] += 1
    return out


__all__ = ["due_followups", "per_run", "followup_campaign", "WORK_KIND", "NEEDS_MECHANISM",
           "NO_CURRENT_VACANCY", "RECHECK_DAYS", "FOLLOWUP_CAMPAIGN_ENV", "GONE",
           "MOVE_FAILED", "MOVE_UNCONFIRMED", "STILL_IN_SEQUENCE"]
