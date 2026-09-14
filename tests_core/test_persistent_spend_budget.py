"""Persistent provider ceilings: concurrency, crashes, retries and each adapter.

All providers are simulated.  PostgreSQL is real so row locking is exercised.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from tgtc_core.db.connection import jsonb, transaction
from tgtc_core.domain.inference import BudgetedInference, InferenceRequest, ReplayAdapter
from tgtc_core.services.spend_budget import (
    BudgetExceeded, BudgetLimits, SpendBudget, budget_status, create_budget,
)
from tgtc_core.testing.fakes import FakeFantastic
from tests_core.helpers import acquisition, make_fresh_partition, rows_in_window, sql1


def _create(conn, clock, budget_id="acceptance", **over):
    values = dict(
        fantastic_requests=0, fantastic_credits=0,
        apollo_requests=0, apollo_credits=0,
        anthropic_requests=0, anthropic_input_tokens=0, anthropic_output_tokens=0,
    )
    values.update(over)
    return create_budget(conn, budget_id, BudgetLimits(**values), expires_at=clock() + timedelta(hours=1))


def _reserve_one(conn, clock, budget_id, key):
    budget = SpendBudget(conn, budget_id, now=clock)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO request_attempts (provider, operation, idempotency_key, params_json) "
                "VALUES ('apollo', 'person_match', %s, %s) RETURNING id",
                (key, jsonb({})),
            )
            attempt_id = int(cur.fetchone()["id"])
            budget.reserve_attempt(
                cur, attempt_id=attempt_id, provider="apollo", operation="person_match",
                estimated_credits=1,
            )
    return attempt_id


def test_atomic_concurrent_reservation_allows_only_one_physical_attempt(conn, conn2, clock):
    _create(conn, clock, apollo_requests=1, apollo_credits=1)

    def try_one(connection, key):
        try:
            return ("reserved", _reserve_one(connection, clock, "acceptance", key))
        except BudgetExceeded as exc:
            return (exc.metric, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: try_one(*pair), [(conn, "a"), (conn2, "b")]))
    assert [r[0] for r in results].count("reserved") == 1
    assert sum(r[0] in ("requests", "credits") for r in results) == 1
    assert sql1(conn, "SELECT count(*) FROM spend_reservations") == 1
    assert sql1(conn, "SELECT count(*) FROM request_attempts") == 1


def test_reservation_survives_restart_and_uncertain_result(conn, clock):
    _create(conn, clock, apollo_requests=1, apollo_credits=1)
    attempt_id = _reserve_one(conn, clock, "acceptance", "first")
    SpendBudget(conn, "acceptance", now=clock).finish_attempt(attempt_id, "uncertain")
    with pytest.raises(BudgetExceeded):
        _reserve_one(conn, clock, "acceptance", "second")
    status = budget_status(conn, "acceptance")
    assert status["used"]["apollo"] == {"requests": 1, "credits": 1.0, "input_tokens": 0, "output_tokens": 0}


def test_fantastic_stops_before_second_page_and_does_not_retry_inside_client(conn, clock):
    _create(conn, clock, fantastic_requests=1, fantastic_credits=3)
    fake = FakeFantastic(rows=rows_in_window(clock, 7))
    svc = acquisition(
        conn, fake, clock, page_limit=3,
        spend_budget=SpendBudget(conn, "acceptance", now=clock),
    )
    pid = make_fresh_partition(conn, clock)
    run = svc.run_partition(pid)
    assert run.pages == 1 and run.rows == 3
    assert run.stop_reason == "spend_budget_exhausted:fantastic:requests"
    assert len(fake.requests) == 1
    again = svc.run_partition(pid)
    assert again.pages == 0 and len(fake.requests) == 1


def test_inference_reserves_tokens_and_fails_closed_after_cap(conn, clock):
    _create(
        conn, clock, anthropic_requests=1,
        anthropic_input_tokens=10000, anthropic_output_tokens=1024,
    )
    inner = ReplayAdapter({
        "one": {"compatible_functions": [], "responsibilities": [], "seniority": "ic",
                "people_management": False, "incompatible_reasons": [], "confidence": 0.9},
        "two": {"compatible_functions": [], "responsibilities": [], "seniority": "ic",
                "people_management": False, "incompatible_reasons": [], "confidence": 0.9},
    }, model_version="simulated-physical/1")
    wrapped = BudgetedInference(conn, inner, SpendBudget(conn, "acceptance", now=clock))
    first = wrapped.classify(InferenceRequest(content_hash="one", description="x" * 100))
    second = wrapped.classify(InferenceRequest(content_hash="two", description="y" * 100))
    assert first.available
    assert not second.available and second.unavailable_reason == "spend_budget_exhausted"
    assert inner.calls == 1
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE provider = 'anthropic'") == 1


def test_budget_definition_is_immutable_and_rerun_never_resets_usage(conn, clock):
    first = _create(conn, clock, apollo_requests=2, apollo_credits=2)
    _reserve_one(conn, clock, "acceptance", "first")
    second = _create(conn, clock, apollo_requests=2, apollo_credits=2)
    assert first["budget_id"] == second["budget_id"]
    assert second["used"]["apollo"]["requests"] == 1
    with pytest.raises(ValueError, match="different immutable limits"):
        _create(conn, clock, apollo_requests=3, apollo_credits=2)
