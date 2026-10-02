"""Two workers resolve postings of the SAME new employer at the same instant: exactly one
employer row, both postings linked, no failed work item. Real PostgreSQL constraints."""

from __future__ import annotations

import threading
from datetime import timedelta

from tgtc_core.db import connect
from tgtc_core.services.identity_service import resolve_posting_identity
from tgtc_core.testing.fakes import make_posting_row
from tests_core.helpers import sql1
from tests_core.seed import example_for, seed_posting


def test_concurrent_employer_creation_converges_on_one_employer(pg_url, conn, clock):
    ex = example_for("finance")
    ids = []
    for i in range(12):
        row = make_posting_row(id=f"job-{i}", title=ex.title, organization="Acme Robotics", domain="acmerobotics.com",
                               description=ex.description, date_created=clock() - timedelta(hours=4, minutes=i))
        ids.append(seed_posting(conn, clock, row))
    barrier = threading.Barrier(6)
    errors = []

    def worker(chunk):
        c = connect(pg_url)
        try:
            barrier.wait()
            for pid in chunk:
                try:
                    resolve_posting_identity(c, pid, now=clock())
                except Exception as exc:  # noqa: BLE001
                    c.rollback()
                    errors.append(repr(exc))
        finally:
            c.close()

    chunks = [ids[i::6] for i in range(6)]
    threads = [threading.Thread(target=worker, args=(ch,)) for ch in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sql1(conn, "SELECT count(*) FROM employers") == 1
    assert sql1(conn, "SELECT count(DISTINCT employer_id) FROM postings WHERE employer_id IS NOT NULL") == 1
    assert sql1(conn, "SELECT count(*) FROM postings WHERE state = 'identity_resolved'") == 12
    assert sql1(conn, "SELECT count(*) FROM employer_aliases WHERE alias_kind = 'domain'") == 1
