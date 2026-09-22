"""The daily controller end to end on the real schema with SIMULATED providers.

Proves that its SQL (measurement, gates, budget status) runs against the real
tables, that it buys through the real balanced path in small blocks, and that its
fresh count equals the database's own Instantly creation receipts for the run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests_core.helpers import runner as make_runner
from tgtc_core.daily import DailyController
from tgtc_core.services.budget_policy import budget_id_for, claim
from tgtc_core.services.spend_budget import BudgetLimits, create_budget
from tgtc_core.testing.scenario import build_nine_route_scenario


def test_daily_controller_runs_on_the_real_schema_and_counts_what_the_database_proves(conn):
    now = datetime.now(timezone.utc)
    sc = build_nine_route_scenario(now)
    bid = budget_id_for("manual", now)
    create_budget(conn, bid, BudgetLimits(fantastic_requests=60, fantastic_credits=4000, apollo_requests=6000,
                                          apollo_credits=1600), expires_at=now + timedelta(hours=24))
    r = make_runner(conn, sc, lambda: now, spend_budget_id=bid, acquisition_strategy="balanced_v1")
    claim(conn, budget_id=bid, kind="manual", run_id=r.run_id)
    rep = DailyController(r, budget_id=bid, target=1000, block_pages=2, max_rounds=40, env={}).run()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(DISTINCT lower(o.payload_json->>'email')) AS n
            FROM delivery_receipts r JOIN delivery_outbox o ON o.id = r.outbox_id JOIN approvals a ON a.id = o.approval_id
            WHERE r.channel = 'instantly' AND r.receipt_kind = 'created' AND a.run_id = %s""", (r.run_id,))
        db_fresh = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM spend_reservations WHERE budget_id <> %s", (bid,))
        other_budgets = cur.fetchone()["n"]
    conn.commit()
    assert rep.fresh_created == db_fresh > 0            # the KPI is the database's own receipts
    assert rep.fresh_created == len(sc.instantly.leads)
    assert other_budgets == 0                           # every paid call was charged to this run's budget
    assert rep.blocks and all(b["pages"] <= 2 for b in rep.blocks)
    # The simulated market is tiny: the run stops buying on its own and reports why.
    assert rep.stop_reason.startswith("target_not_reached:")
    assert rep.fantastic_records <= 4000
