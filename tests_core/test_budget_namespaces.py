"""Budget namespaces: a manual/canary run can never consume a scheduled budget, a
scheduled retry never gets a second allowance, an improperly consumed scheduled
budget is refused loudly, and usage survives process restarts.

Measured 2026-09-22: a manual run consumed `prod-core-20260922`, and that day's
scheduled run bought 0 Fantastic records and continued silently.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from tgtc_core import __main__ as cli
from tgtc_core.db.connection import connect, jsonb, transaction
from tgtc_core.services.budget_policy import BudgetPolicyError, budget_id_for, claim, kind_of, validate
from tgtc_core.services.spend_budget import BudgetLimits, SpendBudget, budget_status, create_budget

NOW = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)
LIMITS = BudgetLimits(fantastic_requests=60, fantastic_credits=4000, apollo_requests=6000, apollo_credits=1600)


def _make(conn, budget_id, limits=LIMITS):
    return create_budget(conn, budget_id, limits, expires_at=datetime.now(timezone.utc) + timedelta(hours=24))


def _reserve(conn, budget_id, key, *, provider="fantastic", credits=100):
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO request_attempts (provider, operation, idempotency_key, params_json) "
                        "VALUES (%s, 'page', %s, %s) RETURNING id", (provider, key, jsonb({})))
            attempt = int(cur.fetchone()["id"])
            SpendBudget(conn, budget_id).reserve_attempt(cur, attempt_id=attempt, provider=provider,
                                                         operation="page", estimated_credits=credits)


# --- namespaces ----------------------------------------------------------------------------
def test_each_kind_has_its_own_namespace():
    assert budget_id_for("scheduled", NOW) == "prod-scheduled-20260923"
    assert budget_id_for("manual", NOW) == "prod-manual-20260923T030000Z"
    assert budget_id_for("canary", NOW) == "canary-20260923T030000Z"
    assert budget_id_for("sidecar", NOW) == "call-sidecar-20260923T030000Z"
    assert [kind_of(budget_id_for(k, NOW)) for k in ("scheduled", "manual", "canary", "sidecar")] == \
        ["scheduled", "manual", "canary", "sidecar"]
    assert kind_of("prod-core-20260922") is None      # the old shared id belongs to no kind


@pytest.mark.parametrize("kind", ["manual", "canary", "sidecar"])
def test_no_other_kind_may_use_a_scheduled_budget_today_or_in_the_future(kind):
    for day in (NOW, NOW + timedelta(days=1)):
        with pytest.raises(BudgetPolicyError):
            validate(kind, budget_id_for("scheduled", day), NOW)


def test_a_scheduled_run_may_use_only_todays_scheduled_budget():
    validate("scheduled", "prod-scheduled-20260923", NOW)
    with pytest.raises(BudgetPolicyError):
        validate("scheduled", "prod-scheduled-20260924", NOW)   # tomorrow's
    with pytest.raises(BudgetPolicyError):
        validate("scheduled", "prod-manual-20260923T030000Z", NOW)


# --- claims ---------------------------------------------------------------------------------
def test_a_manual_run_cannot_consume_the_scheduled_budget(conn):
    sched = budget_id_for("scheduled")
    _make(conn, sched)
    claim(conn, budget_id=sched, kind="scheduled", run_id="cron-1")
    with pytest.raises(BudgetPolicyError):
        claim(conn, budget_id=sched, kind="manual", run_id="manual-1")
    manual = budget_id_for("manual")
    _make(conn, manual)
    claim(conn, budget_id=manual, kind="manual", run_id="manual-1")
    _reserve(conn, manual, "m1", credits=3000)
    # The manual spend is on the manual budget; the scheduled budget is untouched.
    assert budget_status(conn, sched)["used"] == {}
    assert budget_status(conn, manual)["used"]["fantastic"]["credits"] == 3000


def test_a_manual_budget_serves_exactly_one_run(conn):
    manual = budget_id_for("manual")
    _make(conn, manual)
    claim(conn, budget_id=manual, kind="manual", run_id="manual-1")
    with pytest.raises(BudgetPolicyError):
        claim(conn, budget_id=manual, kind="manual", run_id="manual-2")


def test_a_scheduled_retry_reuses_its_budget_and_never_gets_a_second_one(conn):
    sched = budget_id_for("scheduled")
    _make(conn, sched)
    first = claim(conn, budget_id=sched, kind="scheduled", run_id="cron-1")
    _reserve(conn, sched, "p1", credits=2500)
    # The retry creates "the day's budget" again: same row, same limits, usage kept.
    again = _make(conn, sched)
    retry = claim(conn, budget_id=sched, kind="scheduled", run_id="cron-2")
    assert first["reused"] is False and retry["reused"] is True and retry["runs"] == 2
    assert again["limits"]["fantastic_credits"] == 4000
    assert again["used"]["fantastic"]["credits"] == 2500
    with pytest.raises(ValueError):          # a bigger allowance needs a NEW id, which today's run cannot use
        _make(conn, sched, BudgetLimits(fantastic_requests=60, fantastic_credits=8000,
                                        apollo_requests=6000, apollo_credits=1600))


def test_an_improperly_consumed_scheduled_budget_is_refused_not_silently_used(conn):
    sched = budget_id_for("scheduled")
    _make(conn, sched)
    _reserve(conn, sched, "x1", credits=4000)       # consumed by something that never claimed it
    with pytest.raises(BudgetPolicyError, match="consumed without a claim"):
        claim(conn, budget_id=sched, kind="scheduled", run_id="cron-1")


def test_run_daily_exits_nonzero_and_logs_when_its_budget_was_consumed(conn, pg_url, monkeypatch):
    sched = budget_id_for("scheduled")
    _make(conn, sched)
    _reserve(conn, sched, "x1", credits=4000)
    for var in ("FANTASTIC_JOBS_API_KEY", "APOLLO_API_KEY", "AIRTABLE_TOKEN", "INSTANTLY_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    args = argparse.Namespace(i_understand_spend=True, database_url=pg_url, budget_id=sched, budget_kind="scheduled",
                              target=1000, block_pages=2, max_rounds=5, max_items=10)
    assert cli.cmd_run_daily(args) == cli.EXIT_BUDGET_REFUSED
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM run_log WHERE stage = 'daily' AND event = 'refused'")
        assert cur.fetchone()["n"] == 1
    conn.commit()


def test_run_daily_refuses_a_future_scheduled_budget_before_touching_anything(pg_url):
    tomorrow = budget_id_for("scheduled", datetime.now(timezone.utc) + timedelta(days=1))
    args = argparse.Namespace(i_understand_spend=True, database_url=pg_url, budget_id=tomorrow, budget_kind="manual",
                              target=1000, block_pages=2, max_rounds=5, max_items=10)
    assert cli.cmd_run_daily(args) == cli.EXIT_BUDGET_REFUSED


def test_budget_usage_is_correct_across_process_restarts(conn, pg_url):
    sched = budget_id_for("scheduled")
    _make(conn, sched)
    claim(conn, budget_id=sched, kind="scheduled", run_id="cron-1")
    _reserve(conn, sched, "a", credits=100)
    _reserve(conn, sched, "b", provider="apollo", credits=7)
    conn.close()
    fresh = connect(pg_url)                          # a new process
    try:
        status = budget_status(fresh, sched)
        assert status["used"]["fantastic"]["credits"] == 100
        assert status["used"]["apollo"]["credits"] == 7
        assert claim(fresh, budget_id=sched, kind="scheduled", run_id="cron-2")["runs"] == 2
    finally:
        fresh.close()
