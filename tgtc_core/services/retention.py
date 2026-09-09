"""Payload retention: drop the compressed raw rows of old page receipts, never the receipt.

Ids, counts, fingerprints, quota headers and every reconciliation key stay forever;
only ``rows_compressed`` (the bulky provider payload) is nulled after the retention
window. Postings keep their description text (it is evidence), so pruning receipts
never changes a decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import psycopg

from ..db.connection import transaction


def prune_payloads(conn: psycopg.Connection, *, retention_days: int, now: Optional[datetime] = None,
                   batch: int = 5000) -> Dict[str, int]:
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=max(0, int(retention_days)))
    pruned = 0
    while True:
        with transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE page_receipts SET rows_compressed = NULL
                    WHERE id IN (SELECT id FROM page_receipts WHERE rows_compressed IS NOT NULL AND received_at < %s
                                 ORDER BY id LIMIT %s)
                    """,
                    (cutoff, batch),
                )
                n = cur.rowcount
        pruned += n
        if n < batch:
            break
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total, count(rows_compressed) AS with_payload FROM page_receipts")
        row = cur.fetchone()
    conn.commit()
    return {"pruned": pruned, "receipts_total": int(row["total"]), "receipts_with_payload": int(row["with_payload"]),
            "cutoff": cutoff.isoformat()}
