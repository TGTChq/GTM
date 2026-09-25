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
* **finished and silent only.** The campaign must be `completed`, the contact must have
  reached the end of the sequence, and it must never have replied;
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
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import psycopg

from ..db.connection import jsonb, transaction
from ..policy.campaigns import KNOWN_CHALLENGER_CAMPAIGN_IDS, KNOWN_CONTROL_CAMPAIGN_IDS

CAMPAIGN_COMPLETED = 3
LEAD_FINISHED = 3
#: Never more than this in one batch, whatever the caller asks for.
HARD_BATCH_CEILING = 2000
PLAN_CONTACTS_ENV = "TGTC_INSTANTLY_PLAN_CONTACTS"
DEFAULT_PLAN_CONTACTS = 25000
ENABLED_ENV = "TGTC_INSTANTLY_ROTATION_ENABLED"
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


def protected_ids() -> frozenset:
    return KNOWN_CHALLENGER_CAMPAIGN_IDS | KNOWN_CONTROL_CAMPAIGN_IDS


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
    conn.commit()
    return held


def judge(lead: Dict[str, Any], *, campaign_id: str, campaign_status: Any, held: set) -> str:
    """'' when this contact may be removed, otherwise why not. One place, one rule set."""
    if campaign_id in protected_ids():
        return "live_campaign"
    if campaign_status != CAMPAIGN_COMPLETED:
        return f"campaign_not_completed:{campaign_status}"
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
    out: Dict[str, Any] = {"needed": needed, "ceiling": ceiling, "considered": 0, "backed_up": 0,
                           "deleted": 0, "failed": 0, "refused": {}, "batch_id": bid}

    for campaign in campaigns:
        if out["deleted"] >= ceiling:
            break
        cid = str(campaign.get("id") or "")
        status = campaign.get("status")
        if cid in protected_ids() or status != CAMPAIGN_COMPLETED:
            continue
        for lead in leads_of(cid):
            if out["deleted"] >= ceiling:
                break
            out["considered"] += 1
            why = judge(lead, campaign_id=cid, campaign_status=status, held=held)
            if why:
                out["refused"][why] = out["refused"].get(why, 0) + 1
                continue
            if not _back_up(conn, lead, campaign_id=cid, campaign_name=str(campaign.get("name") or ""),
                            reason="finished_and_never_replied", batch_id=bid):
                out["refused"]["already_backed_up"] = out["refused"].get("already_backed_up", 0) + 1
                continue
            out["backed_up"] += 1
            response = delete(str(lead.get("id")))
            code = int(getattr(response, "status_code", 0) or 0)
            _record_delete(conn, str(lead.get("id")), status=code,
                           error="" if 200 <= code < 300 else str(getattr(response, "text", ""))[:200])
            if 200 <= code < 300:
                out["deleted"] += 1
            else:
                out["failed"] += 1
                if out["failed"] >= 3:
                    return out
    return out


def pending_backups(conn: psycopg.Connection) -> int:
    """Rows we backed up and cannot prove we deleted. Should be zero after a clean batch."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM instantly_rotation_backup WHERE deleted_at IS NULL")
        n = int(cur.fetchone()["n"])
    conn.commit()
    return n


__all__ = ["rotate", "judge", "settings", "enabled", "protected_ids", "pending_backups",
           "RotationRefused", "HARD_BATCH_CEILING"]
