"""Budget namespaces: which run may spend which budget.

Measured 2026-09-22: a manual run used ``prod-core-20260922`` (the date-based id
the next cron would compute) and consumed it, so the scheduled run of that day got
zero Fantastic records and continued silently. Each kind of run now has its own
namespace, and the first run to use a budget claims it for its kind:

* ``prod-scheduled-YYYYMMDD`` -- the daily cron; its retries and restarts reuse it;
* ``prod-manual-<UTC timestamp>`` -- an explicitly authorised manual run;
* ``canary-<UTC timestamp>`` -- a bounded canary;
* ``call-sidecar-<UTC timestamp>`` -- the call-list sidecar.

A scheduled run may use only TODAY's scheduled budget; any other kind may never use
a scheduled budget at all, today's or a future day's.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg

from ..db.connection import transaction

PREFIX = {
    "scheduled": "prod-scheduled-",
    "manual": "prod-manual-",
    "canary": "canary-",
    "sidecar": "call-sidecar-",
}
KINDS = tuple(PREFIX)
_SCHEDULED = re.compile(r"^prod-scheduled-(\d{8})$")
_STAMPED = re.compile(r"^\d{8}T\d{6}Z$")


class BudgetPolicyError(RuntimeError):
    """The run may not use this budget. Never silently worked around."""


def budget_id_for(kind: str, now: Optional[datetime] = None) -> str:
    if kind not in PREFIX:
        raise BudgetPolicyError(f"unknown budget kind {kind!r}")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if kind == "scheduled":
        return PREFIX[kind] + moment.strftime("%Y%m%d")
    return PREFIX[kind] + moment.strftime("%Y%m%dT%H%M%SZ")


def kind_of(budget_id: str) -> Optional[str]:
    for kind, prefix in PREFIX.items():
        if str(budget_id or "").startswith(prefix):
            return kind
    return None


def validate(kind: str, budget_id: str, now: Optional[datetime] = None) -> None:
    """Raise unless ``budget_id`` is a budget this kind of run may use right now."""
    if kind not in PREFIX:
        raise BudgetPolicyError(f"unknown budget kind {kind!r}")
    actual = kind_of(budget_id)
    if actual != kind:
        raise BudgetPolicyError(f"a {kind} run may not use budget {budget_id!r} (namespace: {actual or 'none'})")
    if kind == "scheduled":
        expected = budget_id_for("scheduled", now)
        if budget_id != expected:
            raise BudgetPolicyError(f"a scheduled run may use only today's scheduled budget {expected!r}, "
                                    f"not {budget_id!r}")
    elif not _STAMPED.match(budget_id[len(PREFIX[kind]):]):
        raise BudgetPolicyError(f"{kind} budget ids are {PREFIX[kind]}<YYYYMMDDTHHMMSSZ>")


def claim(conn: psycopg.Connection, *, budget_id: str, kind: str, run_id: str,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """Claim ``budget_id`` for this run. Returns the claim, including ``reused`` for a
    scheduled retry. Refuses (BudgetPolicyError) when:

    * the budget belongs to another kind of run, or
    * the budget already has reservations but no claim -- it was consumed by a run
      that bypassed this policy, and the day's scheduled run must not silently go on
      with whatever is left.
    """
    validate(kind, budget_id, now)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT budget_id FROM spend_budgets WHERE budget_id = %s FOR UPDATE", (budget_id,))
            if cur.fetchone() is None:
                raise BudgetPolicyError(f"budget {budget_id!r} does not exist")
            cur.execute("SELECT * FROM budget_claims WHERE budget_id = %s FOR UPDATE", (budget_id,))
            existing = cur.fetchone()
            if existing is None:
                cur.execute("SELECT count(*) AS n FROM spend_reservations WHERE budget_id = %s", (budget_id,))
                if int(cur.fetchone()["n"]):
                    raise BudgetPolicyError(f"budget {budget_id!r} was consumed without a claim; refusing to "
                                            "continue on an improperly consumed budget")
                cur.execute("INSERT INTO budget_claims (budget_id, kind, first_run_id, last_run_id) VALUES (%s, %s, %s, %s)",
                            (budget_id, kind, run_id, run_id))
                return {"budget_id": budget_id, "kind": kind, "reused": False, "runs": 1}
            if existing["kind"] != kind:
                raise BudgetPolicyError(f"budget {budget_id!r} belongs to a {existing['kind']} run, not {kind}")
            if kind != "scheduled" and existing["first_run_id"] != run_id:
                # Manual, canary and sidecar budgets are single-run: a second run needs its own.
                raise BudgetPolicyError(f"{kind} budget {budget_id!r} was already used by another run")
            cur.execute("UPDATE budget_claims SET last_run_id = %s, runs = runs + 1, updated_at = now() "
                        "WHERE budget_id = %s RETURNING runs", (run_id, budget_id))
            return {"budget_id": budget_id, "kind": kind, "reused": True, "runs": int(cur.fetchone()["runs"])}


__all__ = ["BudgetPolicyError", "KINDS", "PREFIX", "budget_id_for", "claim", "kind_of", "validate"]
