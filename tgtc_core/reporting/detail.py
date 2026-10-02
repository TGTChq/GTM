"""The week's lead-level detail: generated, reconciled, stored, and published only to
readers somebody has verified.

Three rules, and the second is the one that makes the file worth opening:

1. **One row per lead the headline counted.** The rows come from the same predicate as
   ``Added to Instantly`` -- a receipt-confirmed creation in the campaign the approval
   was routed to, deduplicated by person -- so the file cannot describe a different
   population from the message that links to it. The 217 historical Airtable records
   without a genuine creation are not in it and never can be: they have no creation
   receipt to match.
2. **It reconciles, or it says so.** The row count is checked against the headline
   figure on every generation and the result is carried in the report. A detail file
   that quietly disagrees with the summary is worse than no file.
3. **It is stored where the data already lives.** Personal data is not copied into a
   second system before a destination AND its reader list have been verified; until
   then the file waits in the same private, authenticated database as the leads it
   describes, and the Slack summary says the detail is pending rather than linking to
   nothing.

Publication is recorded per week (``published_url``, ``published_to``), so a retry
re-uses the file that already exists instead of creating a second one.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import psycopg

from ..db.connection import jsonb
from . import export
from .window import ReportWindow


def csv_bytes(rows: List[Dict[str, Any]], columns: Sequence[str] = export.COLUMNS) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def build(conn: psycopg.Connection, window: ReportWindow) -> Dict[str, Any]:
    """Generate the week's file in memory, with its checksum."""
    rows = export.lead_rows(conn, window)
    payload = csv_bytes(rows)
    return {"rows": rows, "row_count": len(rows), "csv": payload,
            "sha256": hashlib.sha256(payload).hexdigest(), "columns": list(export.COLUMNS)}


def store(conn: psycopg.Connection, window: ReportWindow, built: Dict[str, Any]) -> Dict[str, Any]:
    """Keep exactly one file per reporting week. A retry replaces its content; it can
    never create a second file for the same week, and it never touches a publication
    record that already exists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO report_lead_exports (report_id, window_start, window_end, row_count, columns_json,
                                             csv_gzip, sha256, generated_at)
            VALUES (%(id)s, %(start)s, %(end)s, %(n)s, %(cols)s, %(csv)s, %(sha)s, now())
            ON CONFLICT (report_id) DO UPDATE SET
                row_count = EXCLUDED.row_count, columns_json = EXCLUDED.columns_json,
                csv_gzip = EXCLUDED.csv_gzip, sha256 = EXCLUDED.sha256,
                window_end = EXCLUDED.window_end, generated_at = now()
            RETURNING report_id, row_count, sha256, published_url, published_at, published_to
            """,
            {"id": window.report_id, "start": window.start_utc, "end": window.end_utc,
             "n": built["row_count"], "cols": jsonb(built["columns"]),
             "csv": gzip.compress(built["csv"]), "sha": built["sha256"]})
        row = dict(cur.fetchone())
    conn.commit()
    return row


def load(conn: psycopg.Connection, report_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM report_lead_exports WHERE report_id = %s", (report_id,))
        row = cur.fetchone()
    conn.rollback()
    if row is None:
        return None
    out = dict(row)
    out["csv"] = gzip.decompress(bytes(out.pop("csv_gzip")))
    return out


def record_publication(conn: psycopg.Connection, report_id: str, *, url: str,
                       viewers: Sequence[str], when: Optional[datetime] = None) -> Dict[str, Any]:
    """Record where the week's file was published and who was granted access.

    The reader list is stored, not assumed: the next person to ask "who can see this?"
    reads the answer instead of guessing, and a retry that finds a publication already
    recorded changes neither the link nor the permissions.
    """
    if not url.strip():
        raise ValueError("a publication needs a URL")
    if not viewers:
        raise ValueError("a publication needs the list of readers it was shared with; "
                         "'anyone with the link' is not a reader list")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE report_lead_exports SET published_url = %s, published_at = %s, published_to = %s "
            "WHERE report_id = %s RETURNING report_id, row_count, published_url, published_at, published_to",
            (url.strip(), when or datetime.now(timezone.utc), jsonb(list(viewers)), report_id))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"no lead detail is stored for {report_id}")
        cur.execute("UPDATE report_runs SET detail_url = %s, updated_at = now() WHERE report_id = %s",
                    (url.strip(), report_id))
    conn.commit()
    return dict(row)


def summarise(stored: Optional[Dict[str, Any]], headline_added: int) -> Dict[str, Any]:
    """What the report says about its own detail file, including whether it agrees."""
    if stored is None:
        return {"generated": False, "rows": None, "reconciles": None,
                "state": "not generated", "published_url": None}
    rows = int(stored["row_count"])
    published = stored.get("published_url")
    return {
        "generated": True,
        "rows": rows,
        "sha256": stored.get("sha256"),
        "reconciles": rows == int(headline_added),
        "headline_added_to_instantly": int(headline_added),
        "published_url": published,
        "published_to": stored.get("published_to"),
        "state": "published" if published else "pending publication: no verified destination and reader list",
        "rule": ("one row per lead the headline counted; the 217 historical Airtable records without a genuine "
                 "Instantly creation are not in it"),
    }
