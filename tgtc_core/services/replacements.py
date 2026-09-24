"""The vacancy a departure leaves, filled by the ordinary path or not at all.

When a reply says the person has left, `services.replies` suppresses that address and
queues `replace_departed_contact` against the company x campaign unit. Nothing acted on
that queue: the vacancy stayed visible and unfilled.

This turns such an item into ordinary qualification work, and that is the whole of it.
The unit goes back through the SAME contact path with every gate it always had --
current employer, verified work email, suppression (which now holds the departed
address), compliance, campaign routing -- so a replacement is discovered, never
promoted. In particular:

* a colleague named in the reply is evidence for whoever works the case, never an
  approval: we do not tell somebody that a departed colleague referred us;
* a unit whose posting is gone is NOT revived. Re-opening it would be inventing
  commercial evidence that no longer exists, so it is closed as `no_current_vacancy`;
* nothing here sends anything. It enqueues work; delivery has its own gates, including
  the destination's capacity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg

from ..db import work_queue
from ..db.connection import transaction

WORK_KIND = "replace_departed_contact"
QUALIFY_KIND = "qualify_opportunity"
REQUALIFYING = "requalifying"
NO_CURRENT_VACANCY = "no_current_vacancy"
PER_RUN_ENV = "TGTC_REPLACEMENTS_PER_RUN"
DEFAULT_PER_RUN = 25

#: Only a unit that still stands can be re-opened. 'closed' means the evidence went.
REOPENABLE = ("approved", "open")


def per_run(env: Optional[Dict[str, str]] = None) -> int:
    raw = str((env or {}).get(PER_RUN_ENV, "") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_PER_RUN
    except ValueError:
        value = DEFAULT_PER_RUN
    return max(0, value)


def _finish(conn: psycopg.Connection, work_id: int, reason: str) -> None:
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET state = 'done', close_reason = %s, updated_at = now() "
                        "WHERE id = %s AND state = 'ready'", (reason, work_id))


def reopen_departed_units(conn: psycopg.Connection, *, now: Optional[datetime] = None,
                          limit: int = DEFAULT_PER_RUN) -> Dict[str, int]:
    """Hand every queued departure back to the ordinary contact path, bounded."""
    moment = now or datetime.now(timezone.utc)
    out = {"considered": 0, "requalifying": 0, "no_current_vacancy": 0}
    if limit <= 0:
        return out
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT w.id AS work_id, w.subject_id AS opportunity_id, o.state, o.lane,
                   COALESCE(o.close_reason, '') AS close_reason
            FROM work_items w JOIN opportunities o ON o.id = w.subject_id
            WHERE w.kind = %s AND w.subject_kind = 'opportunity' AND w.state = 'ready'
              AND w.available_at <= %s
            ORDER BY w.id LIMIT %s
            """,
            (WORK_KIND, moment, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()

    for row in rows:
        out["considered"] += 1
        if row["state"] not in REOPENABLE:
            # The vacancy that justified contacting this company is gone. Filling it
            # would be outreach with no current reason behind it.
            _finish(conn, int(row["work_id"]), f"{NO_CURRENT_VACANCY}:{row['close_reason'] or row['state']}"[:200])
            out["no_current_vacancy"] += 1
            continue
        with transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE opportunities SET state = 'open', close_reason = NULL, approved_person_id = NULL, "
                    "reopened_at = %s, updated_at = now() WHERE id = %s AND state IN ('approved', 'open')",
                    (moment, int(row["opportunity_id"])),
                )
        work_queue.enqueue(conn, kind=QUALIFY_KIND, subject_kind="opportunity",
                           subject_id=int(row["opportunity_id"]), lane=str(row["lane"] or "fresh"),
                           reopen=True, available_at=moment)
        conn.commit()
        _finish(conn, int(row["work_id"]), REQUALIFYING)
        out["requalifying"] += 1
    return out


__all__ = ["reopen_departed_units", "per_run", "WORK_KIND", "REQUALIFYING", "NO_CURRENT_VACANCY"]
