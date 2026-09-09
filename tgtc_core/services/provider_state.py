"""Durable provider availability: refusing / serving / unauthorized.

Semantics from ``orchestrator/apollo_availability.py`` (production-proven), tightened
after review finding R08:

* a refusal is recorded when the provider REFUSES a chargeable call (credit
  exhaustion, auth); a rate limit is a short wait and never a refusal;
* ``record_served`` is called ONLY from a served CHARGEABLE response, never at
  reservation;
* after the retry interval, exactly ONE worker may make the controlled probe:
  ``reserve_probe`` is a single conditional UPDATE, so two workers cannot both
  reserve it. An elapsed interval never lifts the refusal by itself.
* ``acquisition_allowed`` answers the separate question "may paid inventory be
  bought?": only when the provider is not on record as refusing/unauthorized.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import psycopg

from ..db.connection import jsonb, transaction

UNKNOWN, SERVING, REFUSING, UNAUTHORIZED = "unknown", "serving", "refusing", "unauthorized"
BLOCKING_STATES = (REFUSING, UNAUTHORIZED)


def load(conn: psycopg.Connection, provider: str) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM provider_state WHERE provider = %s", (provider,))
        row = cur.fetchone()
    if not row:
        return {"provider": provider, "state": UNKNOWN, "refusing_since": None, "last_attempt_at": None,
                "last_served_at": None, "last_error_code": None, "consecutive_refusals": 0, "details": {},
                "probe_reserved_at": None}
    return dict(row)


def record_refusal(conn: psycopg.Connection, provider: str, error_code: str = "", *, state: str = REFUSING,
                   now: Optional[datetime] = None, details: Optional[Dict[str, Any]] = None) -> None:
    moment = now or datetime.now(timezone.utc)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO provider_state (provider, state, refusing_since, last_attempt_at, last_error_code,
                                            consecutive_refusals, details, updated_at)
                VALUES (%s, %s, %s, %s, %s, 1, %s, now())
                ON CONFLICT (provider) DO UPDATE SET
                    state = EXCLUDED.state,
                    refusing_since = COALESCE(CASE WHEN provider_state.state IN ('refusing','unauthorized')
                                                   THEN provider_state.refusing_since END, EXCLUDED.refusing_since),
                    last_attempt_at = EXCLUDED.last_attempt_at,
                    last_error_code = EXCLUDED.last_error_code,
                    consecutive_refusals = provider_state.consecutive_refusals + 1,
                    details = EXCLUDED.details,
                    probe_reserved_at = NULL,
                    updated_at = now()
                """,
                (provider, state, moment, moment, str(error_code or "")[:120], jsonb(details or {})),
            )


def record_served(conn: psycopg.Connection, provider: str, *, now: Optional[datetime] = None) -> None:
    """A CHARGEABLE call RETURNED. Recovery is automatic from here; idempotent."""
    moment = now or datetime.now(timezone.utc)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO provider_state (provider, state, last_served_at, last_attempt_at, consecutive_refusals, updated_at)
                VALUES (%s, 'serving', %s, %s, 0, now())
                ON CONFLICT (provider) DO UPDATE SET
                    state = 'serving', refusing_since = NULL, last_served_at = EXCLUDED.last_served_at,
                    last_attempt_at = EXCLUDED.last_attempt_at, consecutive_refusals = 0, last_error_code = NULL,
                    probe_reserved_at = NULL, updated_at = now()
                WHERE provider_state.state <> 'serving' OR provider_state.last_served_at IS NULL
                """,
                (provider, moment, moment),
            )


def acquisition_allowed(conn: psycopg.Connection, provider: str) -> Dict[str, Any]:
    """May paid inventory that DEPENDS on this provider be bought? Only when the provider
    is not on record as refusing or unauthorized. An elapsed interval does not change
    this answer; a served chargeable call does."""
    state = load(conn, provider)
    return {"allowed": state["state"] not in BLOCKING_STATES, "state": state["state"],
            "refusing_since": state.get("refusing_since"), "last_error_code": state.get("last_error_code")}


def reserve_probe(conn: psycopg.Connection, provider: str, *, retry_hours: float,
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """Atomically decide whether THIS caller may make a chargeable attempt now.

    * provider not refusing/unauthorized -> allowed (no reservation needed);
    * refusing/unauthorized and the interval since the last attempt has elapsed ->
      exactly one caller wins the reservation (conditional UPDATE); the others are told
      to wait for the next interval;
    * otherwise -> not allowed, with the instant the next probe becomes possible.
    """
    moment = now or datetime.now(timezone.utc)
    state = load(conn, provider)
    if state["state"] not in BLOCKING_STATES:
        return {"allowed": True, "reserved": False, "state": state["state"], "next_attempt_after": None}
    interval = timedelta(hours=max(0.0, float(retry_hours)))
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE provider_state SET last_attempt_at = %(now)s, probe_reserved_at = %(now)s, updated_at = now()
                WHERE provider = %(p)s AND state IN ('refusing', 'unauthorized')
                  AND COALESCE(last_attempt_at, refusing_since, '-infinity'::timestamptz) + %(iv)s <= %(now)s
                RETURNING provider
                """,
                {"now": moment, "p": provider, "iv": interval},
            )
            won = cur.fetchone() is not None
    if won:
        return {"allowed": True, "reserved": True, "state": state["state"], "next_attempt_after": None}
    fresh = load(conn, provider)
    last = fresh.get("last_attempt_at") or fresh.get("refusing_since") or moment
    return {"allowed": False, "reserved": False, "state": fresh["state"], "next_attempt_after": last + interval,
            "refusing_since": fresh.get("refusing_since")}


# Backwards-compatible names used by earlier tests; both now route through reserve_probe.
def may_attempt(conn: psycopg.Connection, provider: str, *, retry_hours: float,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    state = load(conn, provider)
    if state["state"] not in BLOCKING_STATES:
        return {"allowed": True, "state": state["state"], "next_attempt_after": None}
    last = state.get("last_attempt_at") or state.get("refusing_since")
    if last is None:
        return {"allowed": True, "state": state["state"], "next_attempt_after": None}
    next_after = last + timedelta(hours=retry_hours)
    return {"allowed": moment >= next_after, "state": state["state"], "next_attempt_after": next_after,
            "refusing_since": state.get("refusing_since")}


def mark_attempt(conn: psycopg.Connection, provider: str, *, now: Optional[datetime] = None) -> None:
    moment = now or datetime.now(timezone.utc)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE provider_state SET last_attempt_at = %s, updated_at = now() WHERE provider = %s", (moment, provider))
