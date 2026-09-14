"""Persistent, atomic ceilings for every physical paid-provider attempt.

The command-line acknowledgement says a human intended to run the production path;
it is not a budget.  This module is the budget.  A reservation is committed in the
same transaction as ``request_attempts`` and before the network call.  Reservations
are never returned to the budget automatically: a crash or ambiguous response may
have been billed and therefore continues to count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

import psycopg

from ..db.connection import transaction


class BudgetExceeded(RuntimeError):
    """No physical request was made because its persistent ceiling was reached."""

    def __init__(self, budget_id: str, provider: str, metric: str, retry_after: datetime):
        self.budget_id = budget_id
        self.provider = provider
        self.metric = metric
        self.retry_after = retry_after
        super().__init__(f"spend_budget_exhausted:{provider}:{metric}")


@dataclass(frozen=True)
class BudgetLimits:
    fantastic_requests: int = 0
    fantastic_credits: int = 0
    apollo_requests: int = 0
    apollo_credits: int = 0
    anthropic_requests: int = 0
    anthropic_input_tokens: int = 0
    anthropic_output_tokens: int = 0

    def as_dict(self) -> Dict[str, int]:
        return dict(self.__dict__)


_LIMITS = {
    "fantastic": ("fantastic_requests_limit", "fantastic_credits_limit", None, None),
    "apollo": ("apollo_requests_limit", "apollo_credits_limit", None, None),
    "anthropic": (
        "anthropic_requests_limit", None,
        "anthropic_input_tokens_limit", "anthropic_output_tokens_limit",
    ),
}


def create_budget(
    conn: psycopg.Connection,
    budget_id: str,
    limits: BudgetLimits,
    *,
    expires_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Create an immutable budget, or verify an identical existing definition.

    Re-running a deployment with the same id never resets usage.  Changing a ceiling
    requires a new id, which makes the authorization boundary explicit and auditable.
    """
    key = str(budget_id or "").strip()
    if not key or len(key) > 120:
        raise ValueError("budget_id must contain 1..120 characters")
    values = limits.as_dict()
    if any(not isinstance(v, int) or v < 0 for v in values.values()):
        raise ValueError("budget limits must be non-negative integers")
    expiry = expires_at or (datetime.now(timezone.utc) + timedelta(hours=24))
    if expiry.tzinfo is None:
        raise ValueError("budget expiry must include a timezone")
    columns = [f"{name}_limit" for name in values]
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO spend_budgets (budget_id, {', '.join(columns)}, expires_at) "
                f"VALUES (%s, {', '.join(['%s'] * len(columns))}, %s) ON CONFLICT (budget_id) DO NOTHING",
                (key, *values.values(), expiry),
            )
            cur.execute("SELECT * FROM spend_budgets WHERE budget_id = %s FOR UPDATE", (key,))
            row = dict(cur.fetchone())
            expected = {f"{name}_limit": value for name, value in values.items()}
            actual = {name: int(row[name]) for name in expected}
            if actual != expected:
                raise ValueError("existing budget has different immutable limits; use a new budget_id")
    return budget_status(conn, key)


def budget_status(conn: psycopg.Connection, budget_id: str) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM spend_budgets WHERE budget_id = %s", (budget_id,))
        budget = cur.fetchone()
        if not budget:
            conn.commit()
            raise LookupError(f"spend budget {budget_id!r} does not exist")
        cur.execute(
            """
            SELECT provider, count(*) AS requests, COALESCE(sum(estimated_credits), 0) AS credits,
                   COALESCE(sum(input_tokens_reserved), 0) AS input_tokens,
                   COALESCE(sum(output_tokens_reserved), 0) AS output_tokens
            FROM spend_reservations WHERE budget_id = %s GROUP BY provider ORDER BY provider
            """,
            (budget_id,),
        )
        used = {r["provider"]: {
            "requests": int(r["requests"]), "credits": float(r["credits"]),
            "input_tokens": int(r["input_tokens"]), "output_tokens": int(r["output_tokens"]),
        } for r in cur.fetchall()}
    conn.commit()
    return {
        "budget_id": budget["budget_id"], "state": budget["state"],
        "expires_at": budget["expires_at"].isoformat(),
        "limits": {name[:-6]: int(value) for name, value in dict(budget).items() if name.endswith("_limit")},
        "used": used,
    }


class SpendBudget:
    def __init__(self, conn: psycopg.Connection, budget_id: str,
                 *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.conn = conn
        self.budget_id = str(budget_id or "").strip()
        self.now = now
        if not self.budget_id:
            raise ValueError("a persistent spend budget id is required")

    def reserve_attempt(
        self,
        cur,
        *,
        attempt_id: int,
        provider: str,
        operation: str,
        estimated_credits: float = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> int:
        """Reserve under the caller's intent transaction, immediately before dispatch."""
        if provider not in _LIMITS:
            raise ValueError(f"provider {provider!r} has no spend-budget definition")
        cur.execute("SELECT * FROM spend_budgets WHERE budget_id = %s FOR UPDATE", (self.budget_id,))
        budget = cur.fetchone()
        if not budget:
            raise LookupError(f"spend budget {self.budget_id!r} does not exist")
        now = self.now()
        retry_after = budget["expires_at"] + timedelta(seconds=1)
        if budget["state"] != "active" or budget["expires_at"] <= now:
            raise BudgetExceeded(self.budget_id, provider, "inactive", retry_after)
        cur.execute(
            """
            SELECT count(*) AS requests, COALESCE(sum(estimated_credits), 0) AS credits,
                   COALESCE(sum(input_tokens_reserved), 0) AS input_tokens,
                   COALESCE(sum(output_tokens_reserved), 0) AS output_tokens
            FROM spend_reservations WHERE budget_id = %s AND provider = %s
            """,
            (self.budget_id, provider),
        )
        used = cur.fetchone()
        req_col, credit_col, input_col, output_col = _LIMITS[provider]
        checks = [
            ("requests", Decimal(used["requests"]) + 1, Decimal(budget[req_col])),
        ]
        if credit_col:
            checks.append(("credits", Decimal(used["credits"]) + Decimal(str(estimated_credits)), Decimal(budget[credit_col])))
        if input_col:
            checks.append(("input_tokens", Decimal(used["input_tokens"]) + input_tokens, Decimal(budget[input_col])))
        if output_col:
            checks.append(("output_tokens", Decimal(used["output_tokens"]) + output_tokens, Decimal(budget[output_col])))
        for metric, proposed, ceiling in checks:
            if proposed > ceiling:
                raise BudgetExceeded(self.budget_id, provider, metric, retry_after)
        cur.execute(
            """
            INSERT INTO spend_reservations
                (budget_id, provider, operation, attempt_id, estimated_credits,
                 input_tokens_reserved, output_tokens_reserved)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (self.budget_id, provider, operation, attempt_id, estimated_credits, input_tokens, output_tokens),
        )
        return int(cur.fetchone()["id"])

    def finish_attempt(
        self,
        attempt_id: int,
        status: str,
        *,
        input_tokens_used: Optional[int] = None,
        output_tokens_used: Optional[int] = None,
    ) -> None:
        # Status is evidence only.  The reservation remains counted even on refusal or
        # failure because the physical request crossed the process boundary.
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE spend_reservations SET status = %s,
                        input_tokens_used = COALESCE(%s, input_tokens_used),
                        output_tokens_used = COALESCE(%s, output_tokens_used), finished_at = now()
                    WHERE attempt_id = %s
                    """,
                    (status, input_tokens_used, output_tokens_used, attempt_id),
                )
