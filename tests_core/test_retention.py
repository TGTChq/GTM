"""Pruning drops old compressed payloads only; receipts, ids and counts survive."""

from __future__ import annotations

from tgtc_core.services.retention import prune_payloads
from tgtc_core.testing.fakes import FakeFantastic
from tests_core.helpers import acquisition, make_fresh_partition, rows_in_window, sql1


def test_prune_keeps_receipts_and_reconciliation_keys(conn, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 5))
    svc = acquisition(conn, fake, clock, page_limit=3)
    pid = make_fresh_partition(conn, clock)
    svc.run_partition(pid)
    assert sql1(conn, "SELECT count(rows_compressed) FROM page_receipts") == 2
    # nothing is old enough yet
    out = prune_payloads(conn, retention_days=30, now=clock())
    assert out["pruned"] == 0 and out["receipts_with_payload"] == 2
    clock.advance(days=31)
    out = prune_payloads(conn, retention_days=30, now=clock())
    assert out["pruned"] == 2 and out["receipts_with_payload"] == 0 and out["receipts_total"] == 2
    assert sql1(conn, "SELECT count(*) FROM page_receipts WHERE row_ids IS NOT NULL AND fingerprint IS NOT NULL") == 2
    assert sql1(conn, "SELECT count(*) FROM postings WHERE description_text IS NOT NULL") == 5
