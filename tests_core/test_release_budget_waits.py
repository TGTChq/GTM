"""Work deferred on an exhausted budget resumes when budget is available again.

Measured, production 2026-09-21: 977 already-bought opportunities sat in
`waiting` on `spend_budget_exhausted:apollo:requests|credits`, scheduled for
the EXPIRY of the canary budget that deferred them (2026-09-21 22:40:41), while
the day's own budget still had 515 Apollo credits unused. `reserve_attempt`
defers to `budget.expires_at + 1s`, which is right for the budget that refused
and wrong for the next one: a new daily budget is exactly the event the work
was waiting for.

Release is per provider and only when the ACTIVE budget has headroom for it.
A wait on some other dependency is never touched.
"""
from __future__ import annotations

from datetime import timedelta

from tgtc_core.db import work_queue
from tgtc_core.services.spend_budget import BudgetLimits, create_budget, release_budget_waits
from tests_core.helpers import sql1


def _waiting(conn, clock, subject_id, waiting_on, *, hours=20):
    work_queue.enqueue(conn, kind="qualify_opportunity", subject_kind="opportunity", subject_id=subject_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE work_items SET state = 'waiting', waiting_on = %s, available_at = %s "
                    "WHERE subject_id = %s AND kind = 'qualify_opportunity'",
                    (waiting_on, clock() + timedelta(hours=hours), subject_id))
    conn.commit()


def _available(conn, clock, subject_id):
    return sql1(conn, "SELECT available_at <= %s FROM work_items WHERE subject_id = %s AND kind = 'qualify_opportunity'",
                (clock(), subject_id))


def test_apollo_waits_are_released_when_the_active_budget_has_apollo_headroom(conn, clock):
    create_budget(conn, "day-2", BudgetLimits(apollo_requests=100, apollo_credits=100), expires_at=clock() + timedelta(hours=24))
    _waiting(conn, clock, 1, "spend_budget_exhausted:apollo:requests")
    _waiting(conn, clock, 2, "spend_budget_exhausted:apollo:credits")
    released = release_budget_waits(conn, "day-2", now=clock())
    assert released == {"apollo": 2}
    assert _available(conn, clock, 1) and _available(conn, clock, 2)


def test_nothing_is_released_without_headroom_for_that_provider(conn, clock):
    create_budget(conn, "no-apollo", BudgetLimits(fantastic_requests=10, fantastic_credits=10),
                  expires_at=clock() + timedelta(hours=24))
    _waiting(conn, clock, 3, "spend_budget_exhausted:apollo:credits")
    assert release_budget_waits(conn, "no-apollo", now=clock()) == {}
    assert not _available(conn, clock, 3)


def test_other_dependency_waits_are_never_touched(conn, clock):
    create_budget(conn, "day-3", BudgetLimits(apollo_requests=100, apollo_credits=100), expires_at=clock() + timedelta(hours=24))
    _waiting(conn, clock, 4, "buyer_search_pending:no_candidates_found")
    release_budget_waits(conn, "day-3", now=clock())
    assert not _available(conn, clock, 4)


def test_an_expired_budget_releases_nothing(conn, clock):
    create_budget(conn, "stale", BudgetLimits(apollo_requests=100, apollo_credits=100), expires_at=clock() - timedelta(hours=1))
    _waiting(conn, clock, 5, "spend_budget_exhausted:apollo:credits")
    assert release_budget_waits(conn, "stale", now=clock()) == {}
    assert not _available(conn, clock, 5)


def test_release_is_idempotent(conn, clock):
    create_budget(conn, "day-4", BudgetLimits(apollo_requests=100, apollo_credits=100), expires_at=clock() + timedelta(hours=24))
    _waiting(conn, clock, 6, "spend_budget_exhausted:apollo:requests")
    assert release_budget_waits(conn, "day-4", now=clock()) == {"apollo": 1}
    assert release_budget_waits(conn, "day-4", now=clock()) == {}
