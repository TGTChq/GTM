"""R07 -- two acquirers cannot commit the same partition position independently. Two
requests both reading offset 0 before either commits: the cursor never skips a page and
ends where the received pages put it. Response ids, receipts and the cursor are asserted."""

from __future__ import annotations

from tgtc_core.db import connect
from tgtc_core.testing.fakes import FakeFantastic
from tests_core.helpers import acquisition, make_fresh_partition, rows_in_window, sql1, sqlall


def test_second_acquirer_cannot_claim_a_leased_partition(conn, conn2, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 5))
    a = acquisition(conn, fake, clock, page_limit=100)
    b = acquisition(conn2, fake, clock, page_limit=100)
    pid = make_fresh_partition(conn, clock)
    assert a.claim_partition(pid) is not None
    assert b.claim_partition(pid) is None                          # active lease
    assert b.open_partitions("fresh") == []                        # nor is it offered to the scheduler
    assert b.run_partition(pid).stop_reason == "partition_not_owned"
    assert fake.requests == []


def test_two_requests_at_offset_zero_before_either_commits_never_skip_a_page(pg_url, conn, conn2, clock):
    """Worker A requests offset 0 and stalls; its lease expires; worker B claims, requests
    offset 0, commits (cursor 100) and offset 100 (cursor 200, short page -> complete).
    A then tries to commit its offset-0 page: fenced. The cursor must not read 300 and
    every received page id must be accounted for."""
    rows = rows_in_window(clock, 150)
    fake = FakeFantastic(rows=rows)
    a = acquisition(conn, fake, clock, page_limit=100, lease_seconds=60)
    b = acquisition(conn2, fake, clock, page_limit=100, lease_seconds=60)
    pid = make_fresh_partition(conn, clock)
    state = {"b_done": False}

    def on_request(record):
        # first request is A's offset 0; before it commits, time passes and B runs the partition
        if not state["b_done"] and record["params"]["offset"] == "0":
            state["b_done"] = True
            clock.advance(seconds=61)                  # A's lease expires
            fake.on_request = None                     # B's own requests are ordinary
            run_b = b.run_partition(pid)
            assert run_b.stop_reason == "complete" and run_b.pages == 2 and run_b.rows == 150
    fake.on_request = on_request

    run_a = a.run_partition(pid)
    assert run_a.stop_reason == "lease_lost"
    part = sqlall(conn, "SELECT next_offset, state, pages_received FROM source_partitions WHERE id = %s", (pid,))[0]
    assert part["next_offset"] == 150 and part["state"] == "complete" and part["pages_received"] == 2
    receipts = sqlall(conn, "SELECT page_offset, row_count, fenced FROM page_receipts WHERE partition_id = %s ORDER BY id", (pid,))
    assert [(r["page_offset"], r["row_count"], r["fenced"]) for r in receipts] == [(0, 100, False), (100, 50, False), (0, 100, True)]
    requested = [r["params"]["offset"] for r in fake.requests]
    assert requested == ["0", "0", "100"]
    ids = sorted(int(x.split("-")[1]) for x in [r["provider_job_id"] for r in sqlall(conn, "SELECT provider_job_id FROM postings")])
    assert ids == list(range(150))                                   # no row skipped, none duplicated
    assert sql1(conn, "SELECT count(*) FROM postings") == 150


def test_crash_after_call_before_commit_then_another_worker_resumes_at_the_same_offset(pg_url, conn, conn2, clock, monkeypatch):
    from tgtc_core.services import acquisition as acq

    fake = FakeFantastic(rows=rows_in_window(clock, 7))
    a = acquisition(conn, fake, clock, page_limit=3, lease_seconds=60)
    pid = make_fresh_partition(conn, clock)
    real = acq.zlib.compress
    calls = {"n": 0}

    def boom(data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("crash after the second call, before its commit")
        return real(data)

    monkeypatch.setattr(acq.zlib, "compress", boom)
    try:
        a.run_partition(pid)
    except RuntimeError:
        conn.rollback()
    monkeypatch.setattr(acq.zlib, "compress", real)
    assert sql1(conn, "SELECT next_offset FROM source_partitions WHERE id = %s", (pid,)) == 3
    # the lease was released in ``finally``; another worker resumes from offset 3
    b = acquisition(conn2, fake, clock, page_limit=3, lease_seconds=60)
    run = b.run_partition(pid)
    assert run.stop_reason == "complete"
    assert [r["params"]["offset"] for r in fake.requests] == ["0", "3", "3", "6"]
    assert sql1(conn, "SELECT count(*) FROM postings") == 7
