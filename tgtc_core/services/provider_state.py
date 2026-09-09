"""Durable provider availability: refusing / serving / unauthorized.

Semantics from ``orchestrator/apollo_availability.py`` (production-proven):

* a refusal is recorded when the provider REFUSES a chargeable call (credit
  exhaustion, auth); a rate limit is recorded separately with a short wait;
* ``record_served`` is called ONLY from a response, never at reservation;
* ``may_attempt`` allows exactly one controlled attempt after the retry interval;
  an elapsed interval never lifts the refusal by itself -- only a served response does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import psycopg

from ..db.connection import jsonb, transaction

UNKNOWN, SERVING, REFUSING, UNAUTHORIZED = "unknown", "serving", "refusing", "unauthorized"


def load(conn: psycopg.Connection, provider: str) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM provider_state WHERE provider = %s", (provider,))
        row = cur.fetchone()
    if not row:
        return {"provider": provider, "state": UNKNOWN, "refusing_since": None, "last_attempt_at": None,
                "last_served_at": None, "last_error_code": None, "consecutive_refusals": 0, "details": {}}
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
                    updated_at = now()
                """,
                (provider, state, moment, moment, str(error_code or "")[:120], jsonb(details or {})),
            )


def record_served(conn: psycopg.Connection, provider: str, *, now: Optional[datetime] = None) -> None:
    """A chargeable call RETURNED. Recovery is automatic from here; idempotent."""
    moment = now or datetime.now(timezone.utc)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO provider_state (provider, state, last_served_at, last_attempt_at, consecutive_refusals, updated_at)
                VALUES (%s, 'serving', %s, %s, 0, now())
                ON CONFLICT (provider) DO UPDATE SET
                    state = 'serving', refusing_since = NULL, last_served_at = EXCLUDED.last_served_at,
                    last_attempt_at = EXCLUDED.last_attempt_at, consecutive_refusals = 0, last_error_code = NULL, updated_at = now()
                WHERE provider_state.state <> 'serving' OR provider_state.last_served_at IS NULL
                """,
                (provider, moment, moment),
            )


def may_attempt(conn: psycopg.Connection, provider: str, *, retry_hours: float,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """One controlled attempt is allowed once the interval has elapsed. It does not
    mean the provider is serving -- only a response can establish that."""
    moment = now or datetime.now(timezone.utc)
    state = load(conn, provider)
    if state["state"] not in (REFUSING, UNAUTHORIZED):
        return {"allowed": True, "state": state["state"], "next_attempt_after": None}
    last = state.get("last_attempt_at") or state.get("refusing_since")
    if last is None:
        return {"allowed": True, "state": state["state"], "next_attempt_after": None}
    next_after = last + timedelta(hours=retry_hours)
    return {"allowed": moment >= next_after, "state": state["state"], "next_attempt_after": next_after,
            "refusing_since": state.get("refusing_since")}


def mark_attempt(conn: psycopg.Connection, provider: str, *, now: Optional[datetime] = None) -> None:
    """A controlled attempt is being made against a refusing provider: move the clock
    so a second caller in the same interval does not also try."""
    moment = now or datetime.now(timezone.utc)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE provider_state SET last_attempt_at = %s, updated_at = now() WHERE provider = %s", (moment, provider))
