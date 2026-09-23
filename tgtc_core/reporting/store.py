"""Report state and delivery, kept in the database.

The idempotency rule, which is the whole point of this module: **a week is delivered
at most once.** ``report_id`` is derived from the window's local start date, so a
retry -- a second cron firing, a manual re-run, a container restart halfway through --
addresses the same row, sees ``delivered_at`` and stops. Re-generating the numbers is
always safe; sending them twice is not.

A delivery row is written only after a provider accepted the message. If the process
dies between the POST and the write, the next attempt re-sends -- a duplicate report
is recoverable, a silently skipped one is not.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg

from ..db.connection import jsonb


class DeliveryRefused(Exception):
    """Raised when a send is asked for that this module will not make."""


def save(conn: psycopg.Connection, report: Dict[str, Any]) -> Dict[str, Any]:
    """Store (or refresh) a report. Never clears an existing delivery record: the
    numbers may be recomputed, but the fact that a week was sent is permanent."""
    w = report["window"]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO report_runs (report_id, kind, window_start, window_end, timezone, data_cutoff,
                                     generated_at, payload_json, flags_json, status)
            VALUES (%(id)s, %(kind)s, %(start)s, %(end)s, %(tz)s, %(cutoff)s, now(), %(payload)s, %(flags)s, %(status)s)
            ON CONFLICT (report_id) DO UPDATE SET
                payload_json = EXCLUDED.payload_json,
                flags_json   = EXCLUDED.flags_json,
                status       = EXCLUDED.status,
                data_cutoff  = EXCLUDED.data_cutoff,
                generated_at = now(),
                updated_at   = now()
            RETURNING report_id, delivered_at, delivery_target, attempts
            """,
            {"id": w["report_id"], "kind": w["kind"], "start": w["window_start_utc"], "end": w["window_end_utc"],
             "tz": w["timezone"], "cutoff": w["data_cutoff_utc"], "payload": jsonb(report),
             "flags": jsonb(report.get("flags", [])), "status": report.get("status", "ok")})
        row = dict(cur.fetchone())
    conn.commit()
    return row


def get(conn: psycopg.Connection, report_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM report_runs WHERE report_id = %s", (report_id,))
        row = cur.fetchone()
    conn.rollback()
    return dict(row) if row else None


def delivered(conn: psycopg.Connection, report_id: str) -> Optional[Dict[str, Any]]:
    """The delivery record for this week, or None if it has never been sent."""
    row = get(conn, report_id)
    if row and row.get("delivered_at"):
        return row
    return None


def _record_attempt(conn: psycopg.Connection, report_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE report_runs SET attempts = attempts + 1, updated_at = now() WHERE report_id = %s",
                    (report_id,))
    conn.commit()


def mark_delivered(conn: psycopg.Connection, report_id: str, *, target: str, receipt: Dict[str, Any],
                   when: Optional[datetime] = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE report_runs SET delivered_at = %s, delivery_target = %s, delivery_receipt = %s, "
            "updated_at = now() WHERE report_id = %s",
            (when or datetime.now(timezone.utc), target, jsonb(receipt), report_id))
    conn.commit()


def deliver(conn: psycopg.Connection, report: Dict[str, Any], *, sender, target: str,
            body: str, resend: bool = False) -> Dict[str, Any]:
    """Send this week's report once.

    ``sender`` is any callable ``(target, body) -> receipt dict``; the transport lives
    outside this module so the idempotency rule can be tested without a network.
    """
    report_id = report["window"]["report_id"]
    if report["window"]["kind"] != "weekly":
        raise DeliveryRefused("only a closed week is delivered; a partial week is for rehearsal and watching")
    stored = get(conn, report_id)
    if stored is None:
        raise DeliveryRefused(f"{report_id} was not stored; a report is saved before it is sent")
    if stored.get("delivered_at") and not resend:
        return {"sent": False, "reason": "already_delivered", "report_id": report_id,
                "delivered_at": stored["delivered_at"].isoformat(), "target": stored.get("delivery_target")}
    _record_attempt(conn, report_id)
    receipt = sender(target, body)
    mark_delivered(conn, report_id, target=target, receipt=receipt)
    return {"sent": True, "report_id": report_id, "target": target, "receipt": receipt}


def ensure_schema(conn: psycopg.Connection) -> None:
    """Create ``report_runs`` if it is not there yet.

    Narrow on purpose: a reporting command applies its OWN table and nothing else, so
    it can never migrate the production database as a side effect of drawing a report.
    ``migration 013`` records the same statements for a normal migration.
    """
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "db" / "migrations" / "013_report_runs.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
