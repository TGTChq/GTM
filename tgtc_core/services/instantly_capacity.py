"""Instantly's lead limit is a wall, not an error to retry.

Measured 2026-09-24. With the workspace full, ``POST /leads`` answers

    403 {"statusCode":403,"error":"Forbidden","message":"Lead limit reached. Remaining uploads: 0"}

for every contact, whoever it is. That day the run bought 3,900 Fantastic records and
spent 1,539 Apollo credits producing contacts with nowhere to go, and 107 of them are
still waiting. Paid work upstream of a full destination is money spent on nothing.

So a capacity refusal is recorded against the provider exactly like an Apollo refusal,
and it closes the gate in FRONT of the paid stages: no Fantastic purchase, no Apollo
enrichment, while the destination has no room. Two properties matter:

* **it is free to find out** -- the probe is one retry of a contact we already own and
  already paid for, and a single reservation means one attempt per interval across the
  whole fleet rather than a 403 per pending row;
* **a full destination never damages a contact** -- the row stays ``pending`` with its
  identity, verified email and suppressions intact. It is never failed into the attempt
  count, never blocked, and never counted as created.

Only THIS 403 means capacity. A 403 from Cloudflare, a bad key or a revoked scope is a
different problem with a different answer, and must not stop acquisition silently.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import psycopg

from ..db.connection import jsonb
from . import provider_state

PROVIDER = "instantly"
CAPACITY_ERROR = "lead_limit_reached"
DEFERRED_REASON = "instantly_capacity_blocked"
STOP_REASON = "blocked_instantly_capacity"
RETRY_HOURS_ENV = "INSTANTLY_CAPACITY_RETRY_HOURS"
DEFAULT_RETRY_HOURS = 1.0

_LIMIT_TEXT = re.compile(r"lead limit reached|remaining uploads", re.I)
_REMAINING = re.compile(r"remaining uploads:\s*(\d+)", re.I)


def retry_hours(env: Optional[Mapping[str, str]] = None) -> float:
    raw = str((env or {}).get(RETRY_HOURS_ENV, "") or "").strip()
    try:
        hours = float(raw) if raw else DEFAULT_RETRY_HOURS
    except ValueError:
        hours = DEFAULT_RETRY_HOURS
    return max(0.0, hours)


def is_capacity_refusal(status: Optional[int], message: str = "") -> bool:
    """The workspace is full -- as opposed to any other 403."""
    return int(status or 0) == 403 and bool(_LIMIT_TEXT.search(str(message or "")))


def remaining_uploads(message: str = "") -> Optional[int]:
    found = _REMAINING.search(str(message or ""))
    return int(found.group(1)) if found else None


def state(conn: psycopg.Connection) -> Dict[str, Any]:
    """Is the destination on record as full? Only a CAPACITY refusal blocks here: a
    refusal recorded for another reason must not masquerade as a full workspace."""
    row = provider_state.load(conn, PROVIDER)
    details = row.get("details") or {}
    blocked = row["state"] in provider_state.BLOCKING_STATES and details.get("kind") == "capacity"
    return {"blocked": blocked, "state": row["state"], "since": row.get("refusing_since"),
            "remaining_uploads": details.get("remaining_uploads"), "message": details.get("message", ""),
            "alerted_at": details.get("alerted_at"), "consecutive_refusals": row.get("consecutive_refusals", 0)}


def may_attempt(conn: psycopg.Connection, *, env: Optional[Mapping[str, str]] = None,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read-only: may a creation be attempted at all right now? Used to skip the channel
    without claiming rows; the attempt itself must reserve the probe."""
    current = state(conn)
    if not current["blocked"]:
        return {"allowed": True, **current}
    decision = provider_state.may_attempt(conn, PROVIDER, retry_hours=retry_hours(env), now=now)
    return {"allowed": bool(decision["allowed"]), "next_attempt_after": decision.get("next_attempt_after"), **current}


def reserve_probe(conn: psycopg.Connection, *, env: Optional[Mapping[str, str]] = None,
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """Exactly one caller per interval may find out whether there is room again."""
    current = state(conn)
    if not current["blocked"]:
        return {"allowed": True, "reserved": False, **current}
    decision = provider_state.reserve_probe(conn, PROVIDER, retry_hours=retry_hours(env), now=now)
    return {"allowed": bool(decision["allowed"]), "reserved": bool(decision.get("reserved")),
            "next_attempt_after": decision.get("next_attempt_after"), **current}


def record_refusal(conn: psycopg.Connection, *, message: str = "", now: Optional[datetime] = None) -> bool:
    """Record that the destination is full. Returns True when this OPENS a block (the
    single moment worth alerting on); a block that is merely still true returns False."""
    moment = now or datetime.now(timezone.utc)
    before = state(conn)
    details: Dict[str, Any] = {"kind": "capacity", "message": str(message or "")[:300],
                               "remaining_uploads": remaining_uploads(message),
                               "observed_at": moment.isoformat()}
    if before["blocked"]:
        # Keep what identifies THIS episode, so the alert stays once per block.
        details["alerted_at"] = before.get("alerted_at")
        details["opened_at"] = (provider_state.load(conn, PROVIDER).get("details") or {}).get("opened_at")
    else:
        details["opened_at"] = moment.isoformat()
    provider_state.record_refusal(conn, PROVIDER, CAPACITY_ERROR, details=details, now=moment)
    return not before["blocked"]


def record_available(conn: psycopg.Connection, *, now: Optional[datetime] = None) -> None:
    """A creation was accepted: there is room. Idempotent, and cheap enough to call on
    every success because ``record_served`` only writes when something changes."""
    provider_state.record_served(conn, PROVIDER, now=now)


def alert_once(conn: psycopg.Connection, *, now: Optional[datetime] = None) -> bool:
    """True exactly once per block episode: the caller then emits the alert. Recorded in
    the provider row, so a process restart does not alert again."""
    moment = now or datetime.now(timezone.utc)
    row = provider_state.load(conn, PROVIDER)
    details = dict(row.get("details") or {})
    if details.get("kind") != "capacity" or row["state"] not in provider_state.BLOCKING_STATES:
        return False
    if details.get("alerted_at"):
        return False
    details["alerted_at"] = moment.isoformat()
    with conn.cursor() as cur:
        cur.execute("UPDATE provider_state SET details = %s, updated_at = now() "
                    "WHERE provider = %s AND COALESCE(details->>'alerted_at', '') = ''",
                    (jsonb(details), PROVIDER))
        won = cur.rowcount == 1
    conn.commit()
    return won
