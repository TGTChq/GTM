"""One authorised scheduled execution per day, claimed atomically and durably.

The run lock stops two runs OVERLAPPING. It does nothing about a second run that
starts after the first has finished and released it — and a deployment starts a
cron service's command, so on 2026-10-02 two deploys produced two sequential runs
on the same day's budget. The UTC-hour window narrows that but cannot close it: a
redeploy between 03:00 and 05:59Z is inside the window.

This closes it. The day's execution is claimed by an INSERT that the primary key
makes atomic, and the claim OUTLIVES the process, so a later start sees it.

Three outcomes are distinguished before anything is spent:

``completed``    the day already ran to ``daily/end``. A second start is a
                 duplicate and is refused outright.
``interrupted``  a claim exists with no finish: the container died mid-run.
                 Refused too, UNLESS a recovery is explicitly authorised.
``rejected``     refused before spend, with the reason recorded.

A recovery is authorised by naming exactly what is being recovered —
``TGTC_RUN_RECOVER=<day>:<interrupted_run_id>`` — so it is auditable and cannot sit
in production as a blanket permission: once that execution finishes, the day reads
``completed`` and the same token authorises nothing. `TGTC_RUN_FORCE` deliberately
does NOT bypass this; it only opens the hour window.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

import psycopg

from ..db.connection import transaction

RECOVER_ENV = "TGTC_RUN_RECOVER"

CLAIMED = "claimed"
RECOVERING = "recovering"
ALREADY_COMPLETED = "already_completed"
INTERRUPTED_NEEDS_AUTHORISATION = "interrupted_needs_authorisation"
RECOVERY_TOKEN_MISMATCH = "recovery_token_mismatch"


@dataclass
class Decision:
    allowed: bool
    state: str
    reason: str
    day: str
    holder_run_id: str = ""
    attempt: int = 0

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "state": self.state, "reason": self.reason,
                "day": self.day, "holder_run_id": self.holder_run_id, "attempt": self.attempt}


def day_of(now: Optional[datetime] = None) -> str:
    """The execution day, on the SAME basis the scheduled budget id uses."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%d")


def _recovery_target(env: Optional[Mapping[str, str]] = None) -> tuple:
    raw = str((os.environ if env is None else env).get(RECOVER_ENV, "") or "").strip()
    if not raw or ":" not in raw:
        return "", ""
    day, run_id = raw.split(":", 1)
    return day.strip(), run_id.strip()


def claim(conn: psycopg.Connection, *, run_id: str, now: Optional[datetime] = None,
          env: Optional[Mapping[str, str]] = None) -> Decision:
    """Claim today's scheduled execution, or explain why this start is not allowed."""
    day = day_of(now)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO scheduled_executions (execution_day, run_id) VALUES (%s, %s) "
                "ON CONFLICT (execution_day) DO NOTHING RETURNING run_id",
                (day, run_id))
            if cur.fetchone() is not None:
                return Decision(True, CLAIMED, "claimed_day:" + day, day, run_id, 1)

            cur.execute(
                "SELECT run_id, finished_at, attempt FROM scheduled_executions "
                "WHERE execution_day = %s FOR UPDATE", (day,))
            row = cur.fetchone()
            holder = str(row["run_id"])
            attempt = int(row["attempt"] or 1)

            if row["finished_at"] is not None:
                return Decision(False, ALREADY_COMPLETED,
                                "day_already_completed_by:" + holder, day, holder, attempt)

            want_day, want_run = _recovery_target(env)
            if not want_day:
                return Decision(False, INTERRUPTED_NEEDS_AUTHORISATION,
                                "interrupted_run_needs_explicit_recovery:" + holder,
                                day, holder, attempt)
            if want_day != day or want_run != holder:
                return Decision(False, RECOVERY_TOKEN_MISMATCH,
                                "recovery_token_names_{}:{}_but_the_interrupted_run_is_{}:{}".format(
                                    want_day, want_run, day, holder),
                                day, holder, attempt)

            cur.execute(
                "UPDATE scheduled_executions SET run_id = %s, attempt = attempt + 1, "
                "recovery_of = %s, claimed_at = now(), updated_at = now() "
                "WHERE execution_day = %s RETURNING attempt",
                (run_id, holder, day))
            return Decision(True, RECOVERING, "recovering:" + holder, day, holder,
                            int(cur.fetchone()["attempt"]))


def mark_finished(conn: psycopg.Connection, *, run_id: str, outcome: str,
                  now: Optional[datetime] = None) -> bool:
    """Record that this run reached its end. Only the holder may close the day."""
    day = day_of(now)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE scheduled_executions SET finished_at = now(), outcome = %s, "
                "updated_at = now() WHERE execution_day = %s AND run_id = %s "
                "AND finished_at IS NULL RETURNING execution_day",
                (str(outcome)[:200], day, run_id))
            return cur.fetchone() is not None


def state_of(conn: psycopg.Connection, *, now: Optional[datetime] = None) -> dict:
    """What the day's execution record says, for a report or an operator."""
    day = day_of(now)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT execution_day, run_id, claimed_at, finished_at, outcome, attempt, recovery_of "
            "FROM scheduled_executions WHERE execution_day = %s", (day,))
        row = cur.fetchone()
    conn.commit()
    if row is None:
        return {"day": day, "state": "unclaimed"}
    return {"day": day, "state": "completed" if row["finished_at"] else "interrupted_or_running",
            "run_id": row["run_id"], "claimed_at": row["claimed_at"],
            "finished_at": row["finished_at"], "outcome": row["outcome"],
            "attempt": row["attempt"], "recovery_of": row["recovery_of"]}


__all__ = ["claim", "mark_finished", "state_of", "day_of", "Decision", "RECOVER_ENV",
           "CLAIMED", "RECOVERING", "ALREADY_COMPLETED",
           "INTERRUPTED_NEEDS_AUTHORISATION", "RECOVERY_TOKEN_MISMATCH"]
