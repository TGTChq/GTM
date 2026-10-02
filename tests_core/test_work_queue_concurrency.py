"""Two workers, one row; expired leases; a stale worker cannot confirm the new one's result.
Real PostgreSQL unique constraints."""

from __future__ import annotations

import threading
from datetime import timedelta

import psycopg
import pytest

from tgtc_core.db import connect, work_queue
from tests_core.helpers import sql1


def _enqueue(conn, n, now=None):
    for i in range(n):
        work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=i + 1, available_at=now)
    conn.commit()


def test_two_connections_never_claim_the_same_item(conn, conn2, now):
    _enqueue(conn, 20, now)
    a = [work_queue.claim(conn, kind="classify", lane="fresh", lease_seconds=300, now=now) for _ in range(10)]
    conn.commit()
    b = [work_queue.claim(conn2, kind="classify", lane="fresh", lease_seconds=300, now=now) for _ in range(10)]
    conn2.commit()
    ids_a = {x.id for x in a if x}
    ids_b = {x.id for x in b if x}
    assert len(ids_a) == 10 and len(ids_b) == 10 and not (ids_a & ids_b)
    assert work_queue.claim(conn, kind="classify", lane="fresh", lease_seconds=300, now=now) is None


def test_parallel_threads_each_get_distinct_items(pg_url, conn, now):
    _enqueue(conn, 50, now)
    got = []
    lock = threading.Lock()

    def worker():
        c = connect(pg_url)
        try:
            while True:
                item = work_queue.claim(c, kind="classify", lane="fresh", lease_seconds=300, now=now)
                c.commit()
                if item is None:
                    return
                with lock:
                    got.append(item.id)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == list(range(1, 51))


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_complete(conn, conn2, now):
    _enqueue(conn, 1, now)
    old = work_queue.claim(conn, kind="classify", lane="fresh", lease_seconds=60, now=now)
    conn.commit()
    assert old is not None
    later = now + timedelta(seconds=61)
    fresh = work_queue.claim(conn2, kind="classify", lane="fresh", lease_seconds=60, now=later)
    conn2.commit()
    assert fresh is not None and fresh.id == old.id and fresh.lease_token != old.lease_token and fresh.version == old.version + 1
    # the stale worker's confirmation is rejected
    assert work_queue.complete(conn, old) is False
    conn.commit()
    assert work_queue.close(conn, old, "stale") is False
    conn.commit()
    # the new holder completes
    assert work_queue.complete(conn2, fresh) is True
    conn2.commit()
    assert sql1(conn, "SELECT state FROM work_items WHERE id = %s", (old.id,)) == "done"


def test_retry_backoff_then_close_at_max_attempts(conn, now):
    work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=7, max_attempts=2, available_at=now)
    conn.commit()
    item = work_queue.claim(conn, kind="classify", lane="fresh", lease_seconds=60, now=now)
    assert work_queue.retry(conn, item, "boom", backoff_seconds=120, now=now)
    conn.commit()
    assert sql1(conn, "SELECT state FROM work_items WHERE id = %s", (item.id,)) == "retry"
    assert work_queue.claim(conn, kind="classify", lane="fresh", lease_seconds=60, now=now) is None  # backoff honoured
    conn.commit()
    item2 = work_queue.claim(conn, kind="classify", lane="fresh", lease_seconds=60, now=now + timedelta(seconds=121))
    assert item2 is not None and item2.attempts == 2
    assert work_queue.retry(conn, item2, "boom again", backoff_seconds=120, now=now)
    conn.commit()
    row = sql1(conn, "SELECT state || ':' || close_reason FROM work_items WHERE id = %s", (item.id,))
    assert row == "closed:max_attempts"


def test_unique_constraints_are_real(conn):
    assert work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=1) is True
    assert work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=1) is False
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO suppressions (kind, key, source, reason) VALUES ('person_email', 'a@b.com', 't', 'r')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO suppressions (kind, key, source, reason) VALUES ('person_email', 'a@b.com', 't', 'r')")
    conn.rollback()


def test_wait_does_not_count_as_an_attempt_and_becomes_claimable_after(conn, now):
    work_queue.enqueue(conn, kind="qualify_opportunity", subject_kind="opportunity", subject_id=1, available_at=now)
    conn.commit()
    item = work_queue.claim(conn, kind="qualify_opportunity", lane="fresh", lease_seconds=60, now=now)
    assert work_queue.wait(conn, item, "apollo_refusing", now + timedelta(hours=6))
    conn.commit()
    assert work_queue.claim(conn, kind="qualify_opportunity", lane="fresh", lease_seconds=60, now=now + timedelta(hours=1)) is None
    conn.commit()
    again = work_queue.claim(conn, kind="qualify_opportunity", lane="fresh", lease_seconds=60, now=now + timedelta(hours=6, seconds=1))
    assert again is not None and again.attempts == 1 and again.waiting_on == "apollo_refusing"
