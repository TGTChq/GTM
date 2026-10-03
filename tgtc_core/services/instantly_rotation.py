"""Make room in Instantly, only when room is needed, and never at anyone's expense.

The workspace holds a fixed number of contacts. On 2026-09-24 it held 25,000 of 25,000
and a day's production had nowhere to go. Rotation is what keeps that from recurring:
contacts whose sequence finished and who never replied are removed, and each removal
returns a slot (measured: the counter falls by exactly the number deleted).

Every rule here exists because getting it wrong costs somebody something:

* **only when needed.** Nothing is deleted while there is room. The caller says how many
  free slots it wants and the batch stops the moment it has them;
* **backed up first, durably.** The verbatim Instantly record and its checksum go into
  our own database BEFORE the delete is attempted, so an interrupted batch still leaves
  a complete account of what it touched -- and every removed contact can be re-uploaded;
* **finished and silent only.** The CONTACT must have reached the end of its sequence
  (status 3) and must never have replied. The campaign's own status is deliberately
  NOT asked: requiring `completed` hid 3,220 safe candidates in paused and
  bounce-protected legacy campaigns while a run was blocked for 22 slots, and a
  finished contact receives nothing even if its campaign is resumed;
* **live outreach is untouchable.** The nine Challenger and nine Control ids are refused
  by id, whatever their status says;
* **our own people are untouchable.** An address we suppressed, or one still waiting for
  delivery, is never a candidate;
* **bounded and audited.** A hard per-batch ceiling, and one row per lead id with the
  outcome of its delete.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg

from ..db.connection import jsonb, transaction
from ..policy.campaigns import (EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID,
                                KNOWN_CHALLENGER_CAMPAIGN_IDS, KNOWN_CONTROL_CAMPAIGN_IDS)

CAMPAIGN_COMPLETED = 3
LEAD_FINISHED = 3
#: Never more than this in one batch, whatever the caller asks for.
HARD_BATCH_CEILING = 2000
PLAN_CONTACTS_ENV = "TGTC_INSTANTLY_PLAN_CONTACTS"
DEFAULT_PLAN_CONTACTS = 25000
ENABLED_ENV = "TGTC_INSTANTLY_ROTATION_ENABLED"
#: The OOO follow-up destination. Protected: it holds deferrals we promised.
FOLLOWUP_CAMPAIGN_ENV = "TGTC_OOO_FOLLOWUP_CAMPAIGN_ID"
FLOOR_ENV = "TGTC_INSTANTLY_FREE_SLOT_FLOOR"
BATCH_ENV = "TGTC_INSTANTLY_ROTATION_BATCH"


class RotationRefused(RuntimeError):
    """The batch was not safe to run. Nothing was deleted."""


def _int_env(env: Dict[str, str], name: str, default: int) -> int:
    raw = str((env or {}).get(name, "") or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def enabled(env: Optional[Dict[str, str]] = None) -> bool:
    return str((env or {}).get(ENABLED_ENV, "") or "").strip() in ("1", "true", "yes")


def settings(env: Optional[Dict[str, str]] = None) -> Dict[str, int]:
    env = env or {}
    return {"plan_contacts": _int_env(env, PLAN_CONTACTS_ENV, DEFAULT_PLAN_CONTACTS),
            "free_slot_floor": _int_env(env, FLOOR_ENV, 1500),
            "batch": min(HARD_BATCH_CEILING, max(0, _int_env(env, BATCH_ENV, 500)))}


def protected_ids(env: Optional[Dict[str, str]] = None) -> frozenset:
    """Campaigns rotation must never remove a contact from.

    The nine Challenger campaigns, the nine v2 campaigns that replaced them, the nine
    Control campaigns, the empty-copy recovery campaign and the OOO follow-up
    campaign. That last one was NOT protected before, and it is a real omission:
    it holds people who asked us to come back later, so deleting them discards a
    deferral we promised to honour. Measured 2026-10-02: of the twelve contacts
    rotation actually removed over four days, four came from it.
    """
    extra = str((env if env is not None else os.environ).get(FOLLOWUP_CAMPAIGN_ENV, "") or "").strip()
    base = (KNOWN_CHALLENGER_CAMPAIGN_IDS | KNOWN_CONTROL_CAMPAIGN_IDS
            | {EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID})
    return (base | {extra}) if extra else base


def _held_emails(conn: psycopg.Connection) -> set:
    """Addresses we must not remove: suppressed, or still awaiting delivery for us."""
    with conn.cursor() as cur:
        cur.execute("SELECT lower(key) AS e FROM suppressions WHERE kind = 'person_email'")
        held = {str(r["e"]) for r in cur.fetchall()}
        cur.execute(
            """
            SELECT DISTINCT lower(p.email) AS e
            FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id
            JOIN people p ON p.id = a.person_id
            WHERE o.state IN ('pending', 'failed', 'claimed', 'in_flight') AND p.email IS NOT NULL
            """)
        held |= {str(r["e"]) for r in cur.fetchall()}
        # Somebody owed the one repair email for the 2026-09-21 blank sends. They are
        # also suppressed, which already holds them, but the suppression is what the
        # recovery grants a per-contact exception to -- so the protection is stated here
        # on its own terms and does not depend on that row surviving.
        cur.execute(
            "SELECT lower(email) AS e FROM empty_email_recovery "
            "WHERE state IN ('authorised', 'reserved', 'enrolled')")
        held |= {str(r["e"]) for r in cur.fetchall()}
    conn.commit()
    return held


def judge(lead: Dict[str, Any], *, campaign_id: str, campaign_status: Any, held: set,
          protected: Optional[frozenset] = None) -> str:
    """'' when this contact may be removed, otherwise why not. One place, one rule set."""
    if campaign_id in (protected if protected is not None else protected_ids()):
        return "live_campaign"
    # The CAMPAIGN's status is the wrong question about an individual contact, and asking
    # it hid almost all of the safe inventory. Measured 2026-10-03: 10,282 leads sit in
    # unprotected campaigns and only 269 of them are in a COMPLETED one, so this rule
    # refused 3,220 leads whose OWN sequence is finished, who never replied and who are
    # not suppressed -- while the run was blocked for want of 22 slots.
    #
    # What matters is the lead: status 3 means Instantly is done with it. Even if someone
    # resumed that paused campaign tomorrow, a finished lead receives nothing more, so
    # removing it cannot cost an email. The checks that do protect a relationship -- a
    # protected campaign, an unfinished sequence, a reply, one of our own suppressions or
    # a pending delivery -- are all still here, and the caller still backs the record up
    # durably before anything is deleted.
    if lead.get("status") != LEAD_FINISHED:
        return f"sequence_not_finished:{lead.get('status')}"
    if lead.get("timestamp_last_reply") or int(lead.get("email_reply_count") or 0):
        return "has_reply"
    email = str(lead.get("email") or "").strip().lower()
    if email and email in held:
        return "ours_suppressed_or_awaiting_delivery"
    return ""


def _back_up(conn: psycopg.Connection, lead: Dict[str, Any], *, campaign_id: str, campaign_name: str,
             reason: str, batch_id: str) -> bool:
    """Write the full record before touching anything. False when it is already there."""
    body = json.dumps(lead, sort_keys=True, default=str)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO instantly_rotation_backup
                    (lead_id, campaign_id, campaign_name, email, lead_json, payload_sha256, reason, batch_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (lead_id) DO NOTHING RETURNING lead_id
                """,
                (str(lead.get("id")), campaign_id, campaign_name[:200],
                 str(lead.get("email") or "").lower(), jsonb(lead), digest, reason[:200], batch_id),
            )
            return cur.fetchone() is not None


def _delete_outcome(response: Any) -> Tuple[int, str]:
    """``(http_status, error_text)`` from whatever the delete callable returned.

    Production passes ``InstantlyClient.delete_lead``, which answers an
    ``InstantlyResult`` carrying ``ok``/``status``/``message`` -- it has NO
    ``status_code`` and no ``text``. Reading ``status_code`` therefore scored every
    delete as 0, i.e. a failure, and the three-failure circuit breaker below then
    aborted the whole rotation after the first three contacts.

    Measured consequence (2026-09-29 .. 2026-10-02): four consecutive runs
    reported ``deleted: 0`` and stopped on ``not_enough_safe_candidates`` while the
    slots they had actually freed showed up as the deficit falling 265, 263, 260,
    257 -- about three real deletions a day, each recorded as a failure and each
    leaving its backup row marked not-deleted.

    Both shapes are accepted so either client can be passed.
    """
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "status_code", None)
    code = int(status or 0)
    if code == 0 and getattr(response, "ok", False):
        code = 200          # a client that reports success without a status
    if 200 <= code < 300:
        return code, ""
    message = getattr(response, "message", None)
    if message is None:
        message = getattr(response, "text", "")
    return code, str(message or "")[:200]


def _record_delete(conn: psycopg.Connection, lead_id: str, *, status: int, error: str = "") -> None:
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE instantly_rotation_backup SET deleted_at = CASE WHEN %s BETWEEN 200 AND 299 "
                "THEN now() ELSE deleted_at END, delete_status = %s, delete_error = %s WHERE lead_id = %s",
                (status, status, error[:300], lead_id))


def rotate(conn: psycopg.Connection, transport: Any, *, needed: int, batch: int,
           campaigns: Sequence[Dict[str, Any]], leads_of: Callable[[str], Iterable[Dict[str, Any]]],
           delete: Callable[[str], Any], now: Optional[datetime] = None,
           batch_id: str = "") -> Dict[str, Any]:
    """Free up to ``needed`` slots, never deleting more than ``batch`` contacts.

    The three callables are the only contact with Instantly, so the rules above can be
    tested without a network: ``campaigns`` is the campaign list, ``leads_of`` yields a
    campaign's leads, and ``delete`` removes one lead id and returns something with a
    ``status_code``.
    """
    if needed <= 0:
        return {"needed": needed, "deleted": 0, "backed_up": 0, "considered": 0, "refused": {}, "batch_id": ""}
    ceiling = min(int(batch), HARD_BATCH_CEILING, int(needed))
    if ceiling <= 0:
        raise RotationRefused("a rotation batch of zero was requested")
    bid = batch_id or f"rot-{(now or datetime.now(timezone.utc)).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    held = _held_emails(conn)
    protected = protected_ids()
    out: Dict[str, Any] = {"needed": needed, "ceiling": ceiling, "considered": 0, "backed_up": 0,
                           "deleted": 0, "failed": 0, "refused": {}, "batch_id": bid}

    for campaign in campaigns:
        if out["deleted"] >= ceiling:
            break
        cid = str(campaign.get("id") or "")
        status = campaign.get("status")
        # Only PROTECTION decides whether a campaign is off limits. The campaign's own
        # status is left to `judge`, which asks about the lead instead -- see the note
        # there. Skipping non-COMPLETED campaigns here is what kept 3,220 safe
        # candidates out of reach while the run was blocked for 22 slots.
        if cid in protected:
            continue
        for lead in leads_of(cid):
            if out["deleted"] >= ceiling:
                break
            out["considered"] += 1
            why = judge(lead, campaign_id=cid, campaign_status=status, held=held,
                        protected=protected)
            if why:
                out["refused"][why] = out["refused"].get(why, 0) + 1
                continue
            if not _back_up(conn, lead, campaign_id=cid, campaign_name=str(campaign.get("name") or ""),
                            reason="finished_and_never_replied", batch_id=bid):
                out["refused"]["already_backed_up"] = out["refused"].get("already_backed_up", 0) + 1
                continue
            out["backed_up"] += 1
            response = delete(str(lead.get("id")))
            code, error = _delete_outcome(response)
            _record_delete(conn, str(lead.get("id")), status=code, error=error)
            if 200 <= code < 300:
                out["deleted"] += 1
            else:
                out["failed"] += 1
                if out["failed"] >= 3:
                    return out
    return out


def occupancy(instantly: Any, *, plan_contacts: int = DEFAULT_PLAN_CONTACTS) -> Dict[str, Any]:
    """How full the workspace is, BEFORE anything is spent.

    The plan caps STORED CONTACTS, which is not the same thing as campaign
    membership. `/campaigns/analytics` reports `leads_count` per campaign, but a
    contact parked on a lead list belongs to no campaign and so appears in none of
    those counts -- while still occupying a stored contact.

    Measured 2026-10-02: moving 1,688 contacts to a hold list dropped the campaign
    sum from 22,757 to 21,069 and freed NOTHING; both populations still summed to
    22,757. Counting only campaigns would have told this run it had 3,931 free
    slots when it had 2,243, so it would have paid for contacts the provider then
    refuses with "Lead limit reached".

    Both populations are reported separately and the total is their sum. If the
    lists cannot be read, occupancy is UNKNOWN -- never silently zero.
    """
    result = instantly.campaign_analytics()
    if not getattr(result, "ok", False):
        return {"known": False, "stored": None, "free": None, "plan": plan_contacts,
                "message": str(getattr(result, "message", ""))[:200]}
    data = getattr(result, "data", None) or {}
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = data if isinstance(data, list) else []
    in_campaigns = sum(int((row or {}).get("leads_count") or 0) for row in rows if isinstance(row, dict))

    on_lists, lists, ok = _lead_list_occupancy(instantly)
    if not ok:
        return {"known": False, "stored": None, "free": None, "plan": plan_contacts,
                "stored_in_campaigns": in_campaigns, "campaigns": len(rows),
                "message": "lead_list_occupancy_unknown"}

    stored = in_campaigns + on_lists
    return {"known": True, "stored": stored, "free": max(0, plan_contacts - stored),
            "plan": plan_contacts, "campaigns": len(rows),
            "stored_in_campaigns": in_campaigns, "stored_on_lists": on_lists,
            "lead_lists": lists}


def _lead_list_occupancy(instantly: Any) -> Tuple[int, int, bool]:
    """``(contacts_on_lists, list_count, known)``. Fails closed, never guesses zero.

    A client too old to enumerate lists is treated as unknown rather than empty:
    assuming zero is exactly the undercount this function exists to prevent.
    """
    if not hasattr(instantly, "list_lead_lists") or not hasattr(instantly, "list_list_leads"):
        return 0, 0, False
    lists: List[str] = []
    after = None
    while True:
        result = instantly.list_lead_lists(limit=100, starting_after=after)
        if not getattr(result, "ok", False):
            return 0, 0, False
        data = getattr(result, "data", None) or {}
        items = data.get("items") or []
        lists.extend(str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id"))
        after = data.get("next_starting_after")
        if not after or not items:
            break

    total = 0
    for list_id in lists:
        after = None
        while True:
            result = instantly.list_list_leads(list_id, limit=100, starting_after=after)
            if not getattr(result, "ok", False):
                return 0, 0, False
            data = getattr(result, "data", None) or {}
            items = data.get("items") or []
            total += sum(1 for i in items if isinstance(i, dict))
            after = data.get("next_starting_after")
            if not after or not items:
                break
    return total, len(lists), True


def enumerate_campaigns(instantly: Any) -> List[Dict[str, Any]]:
    """Every campaign in the workspace, paged. The inventory rotation chooses from."""
    out: List[Dict[str, Any]] = []
    after = None
    while True:
        result = instantly.list_campaigns(limit=100, starting_after=after)
        if not getattr(result, "ok", False):
            break
        data = getattr(result, "data", None) or {}
        items = data.get("items") or []
        out.extend(i for i in items if isinstance(i, dict))
        after = data.get("next_starting_after")
        if not after or not items:
            break
    return out


def campaign_leads(instantly: Any) -> Callable[[str], Iterable[Dict[str, Any]]]:
    """A ``leads_of`` for ``make_room``, so every caller pages identically.

    Stops on the first failed page rather than treating a truncated read as a complete
    one: a short list would make rotation believe a campaign holds fewer contacts than it
    does, and the judgement that protects people runs per contact.
    """
    def leads_of(campaign_id: str):
        after = None
        while True:
            result = instantly.list_campaign_leads(campaign_id, limit=100, starting_after=after)
            if not getattr(result, "ok", False):
                return
            data = getattr(result, "data", None) or {}
            items = data.get("items") or []
            for item in items:
                if isinstance(item, dict):
                    yield item
            after = data.get("next_starting_after")
            if not after or not items:
                return
    return leads_of


def room_needed(free: Optional[int], *, target: int, reserve: int) -> int:
    """Slots this run must free before it starts. Zero means leave the workspace alone.

    A run needs one slot per contact it intends to create, and a reserve on top so the
    next one is not starting from empty. Nothing is deleted while that holds.
    """
    if free is None:
        return 0
    return max(0, int(target) + max(0, int(reserve)) - int(free))


def make_room(conn: psycopg.Connection, instantly: Any, *, target: int,
              campaigns: Sequence[Dict[str, Any]], leads_of: Callable[[str], Iterable[Dict[str, Any]]],
              delete: Callable[[str], Any], env: Optional[Dict[str, str]] = None,
              now: Optional[datetime] = None) -> Dict[str, Any]:
    """Free slots only if this run would otherwise not fit, and prove that it worked.

    Returns what it decided and what actually changed. ``deficit`` is non-zero only when
    there were not enough contacts safe to remove -- in that case the caller must stop
    buying, because everything it bought would land nowhere.
    """
    conf = settings(env)
    out: Dict[str, Any] = {"enabled": enabled(env), "target": int(target),
                           "reserve": conf["free_slot_floor"], "rotated": False, "deficit": 0}
    before = occupancy(instantly, plan_contacts=conf["plan_contacts"])
    out["free_before"] = before.get("free")
    out["stored_before"] = before.get("stored")
    if not before.get("known"):
        # We do not know how full it is, so we do not delete on a guess.
        # Not knowing how full the workspace is is not the same as there being
        # room. Acquisition must stop rather than pay for contacts the provider
        # may refuse, so this is reported as its own stop condition.
        out["reason"] = "occupancy_unknown"
        out["occupancy_unknown"] = True
        out["message"] = str(before.get("message") or "")[:200]
        return out
    need = room_needed(before.get("free"), target=target, reserve=conf["free_slot_floor"])
    out["needed"] = need
    if need <= 0:
        out["reason"] = "enough_room"
        return out
    if not out["enabled"]:
        out["reason"] = "rotation_disabled"
        out["deficit"] = need
        return out
    result = rotate(conn, None, needed=need, batch=conf["batch"], campaigns=campaigns,
                    leads_of=leads_of, delete=delete, now=now)
    out["rotated"] = True
    out["rotation"] = result
    after = occupancy(instantly, plan_contacts=conf["plan_contacts"])
    out["free_after"] = after.get("free")
    out["stored_after"] = after.get("stored")
    # A delete that answered 200 is not a slot until the workspace says so.
    out["freed_measured"] = (None if not after.get("known") or not before.get("known")
                             else int(after["free"]) - int(before["free"]))
    still = room_needed(after.get("free"), target=target, reserve=conf["free_slot_floor"])
    out["deficit"] = still
    out["reason"] = "room_made" if still <= 0 else "not_enough_safe_candidates"
    return out


def pending_backups(conn: psycopg.Connection) -> int:
    """Rows we backed up and cannot prove we deleted. Should be zero after a clean batch."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM instantly_rotation_backup WHERE deleted_at IS NULL")
        n = int(cur.fetchone()["n"])
    conn.commit()
    return n


__all__ = ["rotate", "judge", "settings", "enabled", "protected_ids", "pending_backups",
           "occupancy", "room_needed", "make_room", "enumerate_campaigns",
           "campaign_leads", "RotationRefused", "HARD_BATCH_CEILING"]
