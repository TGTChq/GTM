"""Transactional work queue on ``work_items``.

* ``claim`` uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never take the
  same row; the claim sets a fresh lease token, bumps ``version`` and ``attempts``.
* An expired lease is claimable by another worker. The old worker's token and
  version no longer match, so every later transition it attempts is rejected --
  a stale worker cannot confirm or overwrite the new worker's result.
* ``now`` is injectable so lease expiry is testable without sleeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import psycopg

CLAIMABLE_STATES = ("ready", "retry", "waiting")


class LeaseLost(RuntimeError):
    """A worker no longer owns the item whose business state it tried to write."""


class EvidenceChanged(RuntimeError):
    """An external response was computed from a superseded posting snapshot."""


@dataclass(frozen=True)
class WorkItem:
    id: int
    kind: str
    subject_kind: str
    subject_id: int
    lane: str
    priority: int
    state: str
    lease_token: UUID
    lease_expires_at: datetime
    attempts: int
    max_attempts: int
    version: int
    waiting_on: Optional[str]


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def assert_owned(conn: psycopg.Connection, item: Optional[WorkItem], *, now: datetime) -> None:
    """Call inside the SAME transaction as a stage's business writes.

    Holding this row lock until commit makes ownership validation and the writes
    indivisible with respect to another worker reclaiming the lease.
    Direct single-worker service tests may omit the queue item.
    """
    if item is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM work_items WHERE id = %s AND lease_token = %s AND version = %s "
            "AND state = 'running' AND lease_expires_at > %s FOR UPDATE",
            (item.id, item.lease_token, item.version, now),
        )
        if cur.fetchone() is None:
            raise LeaseLost(f"work item {item.id} no longer owned")


def enqueue(conn: psycopg.Connection, *, kind: str, subject_kind: str, subject_id: int,
            lane: str = "fresh", priority: int = 100, available_at: Optional[datetime] = None,
            max_attempts: int = 8, reopen: bool = False) -> bool:
    """Insert a work item once. Returns True when a row was inserted or reopened."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items (kind, subject_kind, subject_id, lane, priority, available_at, max_attempts)
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()), %s)
            ON CONFLICT (kind, subject_kind, subject_id) DO NOTHING
            RETURNING id
            """,
            (kind, subject_kind, subject_id, lane, priority, available_at, max_attempts),
        )
        if cur.fetchone():
            return True
        if not reopen:
            return False
        cur.execute(
            """
            UPDATE work_items SET state = 'ready', available_at = COALESCE(%s, now()), close_reason = NULL,
                   version = version + 1, updated_at = now()
            WHERE kind = %s AND subject_kind = %s AND subject_id = %s AND state IN ('closed', 'done')
            RETURNING id
            """,
            (available_at, kind, subject_kind, subject_id),
        )
        return cur.fetchone() is not None


def claim(conn: psycopg.Connection, *, kind: str, lane: Optional[str], lease_seconds: int,
          now: Optional[datetime] = None) -> Optional[WorkItem]:
    """Claim one item of ``kind`` (optionally restricted to ``lane``)."""
    moment = _now(now)
    lane_clause = "AND lane = %(lane)s" if lane else ""
    sql = f"""
        WITH candidate AS (
            SELECT id FROM work_items
            WHERE kind = %(kind)s {lane_clause}
              AND (
                    (state IN ('ready', 'retry', 'waiting') AND available_at <= %(now)s)
                 OR (state = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= %(now)s)
              )
            ORDER BY priority ASC, available_at ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE work_items w
           SET state = 'running',
               lease_token = gen_random_uuid(),
               lease_expires_at = %(now)s + make_interval(secs => %(lease)s),
               attempts = w.attempts + 1,
               version = w.version + 1,
               updated_at = now()
          FROM candidate
         WHERE w.id = candidate.id
        RETURNING w.id, w.kind, w.subject_kind, w.subject_id, w.lane, w.priority, w.state,
                  w.lease_token, w.lease_expires_at, w.attempts, w.max_attempts, w.version, w.waiting_on
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"kind": kind, "lane": lane, "now": moment, "lease": lease_seconds})
        row = cur.fetchone()
    if not row:
        return None
    return WorkItem(**row)


def _transition(conn: psycopg.Connection, item: WorkItem, set_sql: str, params: dict) -> bool:
    """Conditional transition: only the lease holder at the claimed version may move it."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE work_items SET {set_sql}, version = version + 1, updated_at = now()
            WHERE id = %(id)s AND lease_token = %(token)s AND version = %(version)s AND state = 'running'
            RETURNING id
            """,
            {**params, "id": item.id, "token": item.lease_token, "version": item.version},
        )
        return cur.fetchone() is not None


def complete(conn: psycopg.Connection, item: WorkItem) -> bool:
    return _transition(conn, item, "state = 'done', lease_token = NULL, lease_expires_at = NULL, last_error = NULL", {})


def close(conn: psycopg.Connection, item: WorkItem, reason: str) -> bool:
    return _transition(
        conn, item,
        "state = 'closed', close_reason = %(reason)s, lease_token = NULL, lease_expires_at = NULL",
        {"reason": reason[:500]},
    )


def retry(conn: psycopg.Connection, item: WorkItem, error: str, *, backoff_seconds: int,
          now: Optional[datetime] = None) -> bool:
    """Technical failure: retry with backoff, or close as max_attempts."""
    moment = _now(now)
    if item.attempts >= item.max_attempts:
        return _transition(
            conn, item,
            "state = 'closed', close_reason = 'max_attempts', last_error = %(error)s, lease_token = NULL, lease_expires_at = NULL",
            {"error": error[:500]},
        )
    delay = backoff_seconds * (2 ** max(0, item.attempts - 1))
    return _transition(
        conn, item,
        "state = 'retry', last_error = %(error)s, available_at = %(at)s, lease_token = NULL, lease_expires_at = NULL",
        {"error": error[:500], "at": moment + timedelta(seconds=min(delay, 6 * 3600))},
    )


def wait(conn: psycopg.Connection, item: WorkItem, waiting_on: str, until: datetime) -> bool:
    """Dependency wait (e.g. provider refusing). Not counted as a failure."""
    return _transition(
        conn, item,
        "state = 'waiting', waiting_on = %(on)s, available_at = %(until)s, attempts = attempts - 1, lease_token = NULL, lease_expires_at = NULL",
        {"on": waiting_on[:200], "until": until},
    )


def counts(conn: psycopg.Connection, kind: Optional[str] = None) -> dict:
    with conn.cursor() as cur:
        if kind:
            cur.execute("SELECT lane, state, count(*) AS n FROM work_items WHERE kind = %s GROUP BY lane, state", (kind,))
        else:
            cur.execute("SELECT kind AS lane, state, count(*) AS n FROM work_items GROUP BY kind, state")
        return {f"{r['lane']}:{r['state']}": int(r["n"]) for r in cur.fetchall()}
