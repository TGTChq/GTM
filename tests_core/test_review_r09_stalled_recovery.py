"""R09 -- stalled partitions are recovered by the normal scheduler; a stale quota reading
never blocks a partition after provider recovery or a billing renewal; cursors and
pending inventory survive process restarts."""

from __future__ import annotations

from datetime import timedelta

from tgtc_core.services import provider_state
from tgtc_core.services.acquisition import SOURCE_JOB_BOARDS
from tgtc_core.testing.fakes import FakeFantastic
from tgtc_core.testing.scenario import build_nine_route_scenario
from tests_core.helpers import acquisition, make_fresh_partition, rows_in_window, runner, sql1, sqlall


def test_quota_refusal_stalls_then_the_scheduler_recovers_after_the_interval(conn, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 7), quota_exhausted=True)
    svc = acquisition(conn, fake, clock, page_limit=3)
    pid = make_fresh_partition(conn, clock)
    assert svc.run_partition(pid).stop_reason == "quota_refused"
    assert sql1(conn, "SELECT state || ':' || stall_reason FROM source_partitions WHERE id = %s", (pid,)).startswith("stalled:quota")
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'fantastic'") == "refusing"
    # inside the interval the scheduler does not touch it (no storm)
    assert svc.recover_partitions(SOURCE_JOB_BOARDS)["reopened"] == 0 and len(fake.requests) == 1
    # provider renewed; after the interval ONE probe reopens it and the cursor resumes at 0
    fake.quota_exhausted = False
    clock.advance(hours=6, seconds=1)
    rec = svc.recover_partitions(SOURCE_JOB_BOARDS)
    assert rec["reopened"] == 1
    run = svc.run_partition(pid)
    assert run.stop_reason == "complete" and run.rows == 7
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'fantastic'") == "serving"


def test_stale_low_quota_receipt_does_not_block_after_billing_renewal(conn, clock):
    """The last receipt said 50 jobs left (below the 90 reserve) with a billing date that has
    since passed: the reading is stale and must not stop the next request."""
    fake = FakeFantastic(rows=rows_in_window(clock, 7), jobs_remaining=53, next_billing_date="2026-09-10")
    svc = acquisition(conn, fake, clock, page_limit=3, min_jobs_quota_remaining=90)
    pid = make_fresh_partition(conn, clock)
    run = svc.run_partition(pid)
    assert run.pages == 1 and run.stop_reason == "quota_reserve:jobs"     # 50 left < 90: correct stop today
    fake.jobs_remaining = 20000                                           # the cycle renewed
    clock.advance(days=3)                                                 # past next_billing_date 2026-09-10
    run2 = svc.run_partition(pid)
    assert run2.stop_reason == "complete" and sql1(conn, "SELECT next_offset FROM source_partitions WHERE id = %s", (pid,)) == 7


def test_stale_quota_reading_by_age_is_ignored(conn, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 7), jobs_remaining=53, next_billing_date="2026-12-31")
    svc = acquisition(conn, fake, clock, page_limit=3, min_jobs_quota_remaining=90, quota_max_age_hours=24)
    pid = make_fresh_partition(conn, clock)
    assert svc.run_partition(pid).stop_reason == "quota_reserve:jobs"
    fake.jobs_remaining = 20000
    clock.advance(hours=25)
    assert svc.run_partition(pid).stop_reason == "complete"


def test_restart_preserves_cursor_and_pending_inventory_and_the_runner_recovers_stalls(conn, clock):
    sc = build_nine_route_scenario(clock())
    sc.fantastic.rows = rows_in_window(clock, 5, prefix="job")
    sc.fantastic.quota_exhausted = True
    r1 = runner(conn, sc, clock)
    r1.acquire()
    stalled = sqlall(conn, "SELECT id, next_offset, state FROM source_partitions WHERE state = 'stalled'")
    assert len(stalled) >= 1
    # "restart": a new runner on the same database, provider renewed, interval elapsed
    sc.fantastic.quota_exhausted = False
    clock.advance(hours=6, seconds=1)
    r2 = runner(conn, sc, clock)
    reports = r2.acquire()
    assert sql1(conn, "SELECT count(*) FROM source_partitions WHERE state = 'stalled'") == 0
    assert sql1(conn, "SELECT count(*) FROM postings") == 5
    assert any(x["stop_reason"] == "complete" for x in reports)
    assert sql1(conn, "SELECT count(*) FROM work_items WHERE kind = 'resolve_identity' AND state = 'ready'") == 5
