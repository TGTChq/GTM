"""A huge backfill queue never starves fresh work; expired items do not return as new."""

from __future__ import annotations

from tgtc_core.db import work_queue
from tgtc_core.services.scheduler import FairShare
from tests_core.helpers import sql1


def test_fresh_gets_eighty_percent_under_contention_and_borrows_when_empty(conn, now):
    for i in range(1000):
        work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=10_000 + i, lane="backfill", available_at=now)
    for i in range(20):
        work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=i + 1, lane="fresh", available_at=now)
    conn.commit()
    fs = FairShare(fresh_share_pct=80)
    lanes = []
    for _ in range(25):
        item = fs.claim(conn, kind="classify", lease_seconds=300, now=now)
        lanes.append(item.lane)
        work_queue.complete(conn, item)
        conn.commit()
    # first 20 claims: 16 fresh + 4 backfill; the 20 fresh items are done after 25 claims (16 + 4 borrowed)
    assert lanes[:10].count("fresh") == 8 and lanes[:10].count("backfill") == 2
    assert lanes[10:20].count("fresh") == 8
    assert fs.fresh_claims == 20 and fs.backfill_claims == 5
    # once fresh is empty, backfill borrows every slot
    for _ in range(10):
        item = fs.claim(conn, kind="classify", lease_seconds=300, now=now)
        assert item.lane == "backfill"
        work_queue.complete(conn, item)
        conn.commit()
    assert fs.borrowed_by_backfill >= 8
    assert sql1(conn, "SELECT count(*) FROM work_items WHERE lane = 'fresh' AND state <> 'done'") == 0


def test_closed_and_expired_items_are_not_reclaimed_as_new(conn, now):
    work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=1, lane="backfill", available_at=now)
    conn.commit()
    item = work_queue.claim(conn, kind="classify", lane="backfill", lease_seconds=300, now=now)
    work_queue.close(conn, item, "insufficient_evidence")
    conn.commit()
    assert work_queue.claim(conn, kind="classify", lane="backfill", lease_seconds=300, now=now) is None
    conn.commit()
    # re-enqueue without reopen is a no-op; with reopen (new evidence) it returns once, keeping identity
    assert work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=1, lane="backfill") is False
    assert work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=1, lane="backfill", reopen=True, available_at=now) is True
    conn.commit()
    assert sql1(conn, "SELECT count(*) FROM work_items") == 1
