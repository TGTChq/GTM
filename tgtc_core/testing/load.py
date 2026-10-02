"""Load envelope: 50,000 posting events (repeats, modifications, closures) with a 10,000 burst,
several worker threads, against embedded PostgreSQL and SIMULATED providers.

What it measures: software throughput of ingestion + identity + classification + queue
claiming, memory, and DB latency, with ZERO provider latency. What it does not
measure: inventory, buyer coverage, conversion, credits, or anything about the live
services. Run: ``python -m tgtc_core.testing.load [--events 50000] [--workers 4]``.
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from ..config import Settings
from ..db import apply_schema, connect, work_queue
from ..db.connection import transaction
from ..providers.fantastic import FantasticClient
from ..services.acquisition import AcquisitionService
from ..services.classification_service import classify_one
from ..services.identity_service import resolve_posting_identity
from ..testing.corpus import CORPUS
from ..testing.embedded_pg import database_url
from ..testing.fakes import FakeFantastic, make_posting_row

POSITIVES = [e for e in CORPUS if e.expected_function and not e.expected_excluded]


def _rows(n: int, now: datetime, *, unique_jobs: int, employers: int) -> List[Dict[str, Any]]:
    rng = random.Random(42)
    rows = []
    for i in range(n):
        job = rng.randrange(unique_jobs)                  # repeats: several events per job
        ex = POSITIVES[job % len(POSITIVES)]
        emp = job % employers
        desc = ex.description + (" Updated responsibilities." if rng.random() < 0.15 else "")   # modifications
        rows.append(make_posting_row(id=f"job-{job}", title=ex.title, organization=f"Employer {emp}", domain=f"emp{emp}-example.com",
                                     description=desc, date_created=now - timedelta(hours=4, seconds=i % 3500)))
    return rows


def run(*, events: int, burst: int, workers: int, database_url_override: str = "") -> Dict[str, Any]:
    url, server = (database_url_override, None) if database_url_override else database_url()
    conn = connect(url)
    apply_schema(conn)
    now = datetime.now(timezone.utc)
    report: Dict[str, Any] = {"events": events, "burst": burst, "workers": workers, "evidence_class": "SIMULATED_LOAD"}
    tracemalloc.start()
    t0 = time.perf_counter()
    try:
        # --- ingestion: the fake serves `events` rows across pages; repeats and modifications included
        fake = FakeFantastic(rows=_rows(events, now, unique_jobs=max(1, events // 3), employers=max(1, events // 12)),
                             jobs_remaining=10**9, requests_remaining=10**9)
        client = FantasticClient(fake, base_url="https://data.fantastic.jobs", api_key="sim", sleep=lambda s: None)
        svc = AcquisitionService(conn, client, page_limit=500, time_frame="7d", fresh_window_minutes=60, fresh_lag_minutes=180,
                                 backfill_window_hours=24, max_pages_per_partition=10_000, min_jobs_quota_remaining=0,
                                 min_requests_quota_remaining=0, now=lambda: now)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO source_partitions (source, lane, window_start, window_end) VALUES ('fantastic:active-jb','fresh',%s,%s) RETURNING id",
                        (now - timedelta(hours=5), now - timedelta(hours=3)))
            pid = int(cur.fetchone()["id"])
        conn.commit()
        t1 = time.perf_counter()
        run1 = svc.run_partition(pid)
        # the burst: a second window observation replays `burst` events at once (repeats + closures)
        burst_rows = fake.rows[:burst]
        for r in burst_rows[: burst // 10]:
            r["description_text"] = r["description_text"] + " CLOSED."          # modifications
        fake.rows = burst_rows
        with conn.cursor() as cur:
            cur.execute("INSERT INTO source_partitions (source, lane, window_start, window_end) VALUES ('fantastic:active-jb','backfill',%s,%s) RETURNING id",
                        (now - timedelta(hours=5), now - timedelta(hours=3)))
            pid2 = int(cur.fetchone()["id"])
        conn.commit()
        run2 = svc.run_partition(pid2)
        t2 = time.perf_counter()
        report["ingestion"] = {"seconds": round(t2 - t1, 2), "pages": run1.pages + run2.pages, "rows": run1.rows + run2.rows,
                               "stop_reasons": [run1.stop_reason, run2.stop_reason],
                               "new_postings": run1.new_postings, "modified": run1.modified_postings + run2.modified_postings,
                               "rows_per_second": round((run1.rows + run2.rows) / max(1e-6, t2 - t1))}

        # --- identity + classification with N worker threads competing on the queue
        counts = {"resolve_identity": 0, "classify": 0, "errors": 0}
        lock = threading.Lock()

        def worker():
            c = connect(url)
            try:
                for kind, fn in (("resolve_identity", lambda pid_: resolve_posting_identity(c, pid_, now=now)),
                                 ("classify", lambda pid_: classify_one(c, pid_, inference=None, now=now))):
                    while True:
                        item = work_queue.claim(c, kind=kind, lane=None, lease_seconds=300, now=now)
                        c.commit()
                        if item is None:
                            break
                        try:
                            fn(item.subject_id)
                            with transaction(c):
                                work_queue.complete(c, item)
                            with lock:
                                counts[kind] += 1
                        except Exception:  # noqa: BLE001
                            c.rollback()
                            with transaction(c):
                                work_queue.retry(c, item, "error", backoff_seconds=1, now=now)
                            with lock:
                                counts["errors"] += 1
            finally:
                c.close()

        t3 = time.perf_counter()
        # identity must precede classification for the same posting; workers process identity first
        # then classification, and a classify item only exists after identity ran.
        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # second pass picks up classify items enqueued by identity resolution after workers moved on
        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        t4 = time.perf_counter()
        report["stages"] = {"seconds": round(t4 - t3, 2), **counts,
                            "items_per_second": round((counts["resolve_identity"] + counts["classify"]) / max(1e-6, t4 - t3))}
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM postings")
            postings = cur.fetchone()["count"]
            cur.execute("SELECT count(*) FROM opportunities")
            opps = cur.fetchone()["count"]
            cur.execute("SELECT kind, state, count(*) AS n FROM work_items GROUP BY kind, state")
            wi = {f"{r['kind']}:{r['state']}": int(r["n"]) for r in cur.fetchall()}
            cur.execute("SELECT count(*) FROM posting_versions WHERE version > 1")
            versions = cur.fetchone()["count"]
        conn.commit()
        report["result"] = {"postings": postings, "opportunities": opps, "work_items": wi, "modified_versions": versions,
                            # only the two stages this script runs; qualify_opportunity items are the NEXT
                            # stage's queue (Apollo is not simulated here), not lost work
                            "queue_lost_or_duplicated": sum(v for k, v in wi.items()
                                                            if k.split(":")[0] in ("resolve_identity", "classify")
                                                            and k.split(":")[1] in ("running", "ready", "retry"))}
        current, peak = tracemalloc.get_traced_memory()
        report["memory_mb"] = {"current": round(current / 1e6, 1), "peak": round(peak / 1e6, 1)}
        report["total_seconds"] = round(time.perf_counter() - t0, 2)
        return report
    finally:
        tracemalloc.stop()
        conn.close()
        if server is not None:
            server.cleanup()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--events", type=int, default=50_000)
    p.add_argument("--burst", type=int, default=10_000)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--database-url", default="")
    a = p.parse_args(argv)
    print(json.dumps(run(events=a.events, burst=a.burst, workers=a.workers, database_url_override=a.database_url), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
