"""A hard ceiling on Apollo credits across a rolling 30 days, in code.

The daily budget id rotates every day, so a per-budget limit alone bounds one
day and nothing more. TGTC_APOLLO_ROLLING_30D_CREDITS bounds the sum of Apollo
credits reserved across EVERY budget in the last 30 days (authorised maximum:
50,000). A refused reservation costs nothing and is not counted.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tgtc_core.db.connection import jsonb, transaction
from tgtc_core.services.spend_budget import (
    APOLLO_ROLLING_CAP_ENV, BudgetExceeded, BudgetLimits, SpendBudget, create_budget,
)


def _budget(conn, clock, budget_id, credits=100):
    create_budget(conn, budget_id, BudgetLimits(apollo_requests=1000, apollo_credits=credits),
                  expires_at=clock() + timedelta(hours=24))


def _reserve(conn, clock, budget_id, key, credits=1):
    budget = SpendBudget(conn, budget_id, now=clock)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO request_attempts (provider, operation, idempotency_key, params_json) "
                        "VALUES ('apollo', 'person_match', %s, %s) RETURNING id", (key, jsonb({})))
            attempt_id = int(cur.fetchone()["id"])
            budget.reserve_attempt(cur, attempt_id=attempt_id, provider="apollo", operation="person_match",
                                   estimated_credits=credits)


def test_the_rolling_cap_spans_daily_budget_ids(conn, clock, monkeypatch):
    monkeypatch.setenv(APOLLO_ROLLING_CAP_ENV, "3")
    _budget(conn, clock, "day-1")
    _budget(conn, clock, "day-2")
    _reserve(conn, clock, "day-1", "a")
    _reserve(conn, clock, "day-1", "b")
    _reserve(conn, clock, "day-2", "c")
    with pytest.raises(BudgetExceeded) as err:
        _reserve(conn, clock, "day-2", "d")
    assert "rolling_30d" in str(err.value.metric)


def test_reservations_older_than_30_days_do_not_count(conn, clock, monkeypatch):
    monkeypatch.setenv(APOLLO_ROLLING_CAP_ENV, "2")
    _budget(conn, clock, "old")
    _reserve(conn, clock, "old", "a")
    _reserve(conn, clock, "old", "b")
    with conn.cursor() as cur:
        cur.execute("UPDATE spend_reservations SET created_at = %s", (clock() - timedelta(days=31),))
    conn.commit()
    _budget(conn, clock, "new")
    _reserve(conn, clock, "new", "c")  # does not raise


def test_refused_reservations_are_not_counted(conn, clock, monkeypatch):
    monkeypatch.setenv(APOLLO_ROLLING_CAP_ENV, "1")
    _budget(conn, clock, "d")
    _reserve(conn, clock, "d", "a")
    with conn.cursor() as cur:
        cur.execute("UPDATE spend_reservations SET status = 'refused'")
    conn.commit()
    _reserve(conn, clock, "d", "b")  # the refused one cost nothing


def test_free_searches_are_never_blocked_by_the_credit_cap(conn, clock, monkeypatch):
    monkeypatch.setenv(APOLLO_ROLLING_CAP_ENV, "1")
    _budget(conn, clock, "d")
    _reserve(conn, clock, "d", "a")
    _reserve(conn, clock, "d", "search", credits=0)  # zero-credit search still allowed


def test_no_cap_configured_means_no_rolling_limit(conn, clock, monkeypatch):
    monkeypatch.delenv(APOLLO_ROLLING_CAP_ENV, raising=False)
    _budget(conn, clock, "d")
    for i in range(5):
        _reserve(conn, clock, "d", f"k{i}")
