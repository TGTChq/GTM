"""A run that was interrupted, told apart from a run that is still working.

The weekly report waits for the window's production to close. "Started and never ended"
normally means a run is still going, and waiting is the right answer. It can also mean
the container was replaced underneath it, and then waiting is the wrong answer for ever:
that run cannot close itself, so the report never goes out.

The difference cannot be guessed from the run log alone, so it is recorded -- and then
**re-checked**, every time, against the four things that make the claim true:

1. the container is gone. The run lock is exclusive, so a LATER run that took it and
   completed proves the earlier one is no longer running. The interrupted run must also
   have been silent since the incident was filed;
2. the run lock is free right now;
3. the run named as the successor actually completed;
4. the week's receipts reconcile.

A recorded incident is a claim, never a dismissal. If any check stops holding the gate
goes back to blocking, which is the behaviour a stale or wrong record must have.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import psycopg

from ..db.connection import jsonb, transaction

KIND_CONTAINER_REPLACED = "container_replaced"

#: Runs that started inside the window, never ended, and carry an incident whose per-run
#: proofs hold. The global proofs (lock free, receipts reconcile) are applied by the
#: caller, because a report that does not reconcile must block whatever any record says.
_SETTLED_SQL = """
    SELECT l.run_id,
           i.kind,
           i.superseded_by,
           i.note,
           i.recorded_at,
           min(l.created_at) AS started_at
    FROM run_log l
    JOIN run_incidents i ON i.run_id = l.run_id
    WHERE l.stage = 'daily' AND l.event = 'start'
      AND l.created_at >= %(t0)s AND l.created_at < %(t1)s
      -- it never closed itself
      AND NOT EXISTS (SELECT 1 FROM run_log e WHERE e.run_id = l.run_id
                      AND e.stage = 'daily' AND e.event IN ('end', 'refused'))
      -- the successor completed, and started after it
      AND EXISTS (SELECT 1 FROM run_log s WHERE s.run_id = i.superseded_by
                  AND s.stage = 'daily' AND s.event = 'end'
                  AND s.created_at > l.created_at)
      -- and it has been silent since the incident was filed
      AND NOT EXISTS (SELECT 1 FROM run_log q WHERE q.run_id = l.run_id
                      AND q.created_at >= i.recorded_at)
    GROUP BY l.run_id, i.kind, i.superseded_by, i.note, i.recorded_at
    ORDER BY 6
"""


def settled(cur, *, window_start, window_end) -> List[Dict[str, Any]]:
    """Interrupted runs in the window whose per-run proofs hold, right now."""
    cur.execute(_SETTLED_SQL, {"t0": window_start, "t1": window_end})
    return [{"run_id": r["run_id"], "kind": r["kind"], "superseded_by": r["superseded_by"],
             "note": r["note"], "recorded_at": r["recorded_at"].isoformat(),
             "started_at": r["started_at"].isoformat()} for r in cur.fetchall()]


def record(conn: psycopg.Connection, *, run_id: str, superseded_by: str, kind: str,
           evidence: Optional[Dict[str, Any]] = None, note: str = "",
           recorded_by: str = "") -> Dict[str, Any]:
    """File the incident. Refuses anything it can see is untrue.

    It will not accept a run that closed itself, a successor that did not complete, a
    successor that did not start after it, or a run still holding the run lock. What it
    cannot check here -- that the week reconciles -- is checked by the gate at report
    time, every time.
    """
    run_id, superseded_by = str(run_id).strip(), str(superseded_by).strip()
    if not run_id or not superseded_by:
        raise ValueError("an incident needs both the interrupted run and the run that superseded it")
    if run_id == superseded_by:
        raise ValueError("a run cannot supersede itself")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM run_log WHERE run_id = %s AND stage = 'daily' "
                    "AND event IN ('end', 'refused')", (run_id,))
        if int(cur.fetchone()["n"]):
            raise ValueError(f"{run_id} closed itself; it is not an interrupted run")
        cur.execute("SELECT min(created_at) AS started_at FROM run_log WHERE run_id = %s "
                    "AND stage = 'daily' AND event = 'start'", (run_id,))
        started = (cur.fetchone() or {}).get("started_at")
        if started is None:
            raise ValueError(f"{run_id} never logged a start; there is nothing to settle")
        cur.execute("SELECT min(created_at) AS ended_at FROM run_log WHERE run_id = %s "
                    "AND stage = 'daily' AND event = 'end'", (superseded_by,))
        ended = (cur.fetchone() or {}).get("ended_at")
        if ended is None:
            raise ValueError(f"{superseded_by} has not completed; it cannot stand in for {run_id}")
        if ended <= started:
            raise ValueError(f"{superseded_by} did not run after {run_id}; it proves nothing about it")
        cur.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' "
                    "AND objid = %s AND granted", (0x74677463,))
        if int(cur.fetchone()["n"]):
            raise ValueError("a production run holds the run lock right now; nothing is settled while it does")
    conn.commit()

    payload = dict(evidence or {})
    payload.setdefault("interrupted_started_at", started.isoformat())
    payload.setdefault("superseding_ended_at", ended.isoformat())
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_incidents (run_id, kind, superseded_by, evidence, note, recorded_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    kind = EXCLUDED.kind, superseded_by = EXCLUDED.superseded_by,
                    evidence = EXCLUDED.evidence, note = EXCLUDED.note,
                    recorded_by = EXCLUDED.recorded_by, recorded_at = now()
                RETURNING run_id, kind, superseded_by, recorded_at
                """,
                (run_id, kind, superseded_by, jsonb(payload), note[:500], recorded_by[:200]))
            row = dict(cur.fetchone())
    row["recorded_at"] = row["recorded_at"].isoformat()
    return row


__all__ = ["settled", "record", "KIND_CONTAINER_REPLACED"]
