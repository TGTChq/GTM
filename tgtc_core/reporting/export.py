"""The lead-level export: one row per delivered lead, traceable end to end.

Every row carries the whole chain -- job, employer, person, campaign and the delivery
receipt each provider gave back -- so any number in the summary can be drilled into
and any single lead can be explained.

This file contains personal data, so two rules are enforced in code, not by habit:

* it is **deduplicated by person**, the same rule the KPI uses, so the export can
  never show more leads than the report claims;
* it **refuses to be written inside the repository**, so personal data cannot be
  committed by accident. It belongs in the private directory beside the other
  private evidence.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

import psycopg

from .metrics import GENUINE_CREATION_PREDICATE
from .window import ReportWindow

#: The repository root (the parent of the ``tgtc_core`` package).
REPO_ROOT = Path(__file__).resolve().parents[2]

COLUMNS = (
    "created_at", "campaign", "campaign_id", "lead_key", "run_id",
    "first_name", "last_name", "title", "email", "email_status", "contact_country",
    "employer", "employer_domain", "employer_country", "employees",
    "job_title", "job_url", "job_first_seen_at", "job_source",
    "opportunity_id", "approval_id", "instantly_lead_id", "airtable_record_id",
)

_SQL = f"""
SELECT DISTINCT ON (lower(o.payload_json->>'email'))
       r.received_at AS created_at,
       a.campaign_key AS campaign, a.campaign_id, a.lead_key, a.run_id,
       p.first_name, p.last_name, p.title, lower(o.payload_json->>'email') AS email,
       p.email_status, p.contact_country,
       e.canonical_name AS employer, e.domain AS employer_domain, e.company_country AS employer_country,
       e.employee_count AS employees,
       job.title AS job_title, job.url AS job_url, job.first_seen_at AS job_first_seen_at, job.source AS job_source,
       a.opportunity_id, a.id AS approval_id,
       r.external_id AS instantly_lead_id,
       (SELECT r2.external_id FROM delivery_outbox o2 JOIN delivery_receipts r2 ON r2.outbox_id = o2.id
        WHERE o2.approval_id = a.id AND o2.channel = 'airtable'
          AND r2.receipt_kind IN ('created', 'reconciled') ORDER BY r2.received_at LIMIT 1) AS airtable_record_id
FROM delivery_receipts r
JOIN delivery_outbox o ON o.id = r.outbox_id
JOIN approvals a ON a.id = o.approval_id
JOIN people p ON p.id = a.person_id
JOIN employers e ON e.id = a.employer_id
LEFT JOIN LATERAL (
    SELECT pg.title, pg.url, pg.first_seen_at, pg.source
    FROM opportunity_postings op JOIN postings pg ON pg.id = op.posting_id
    WHERE op.opportunity_id = a.opportunity_id ORDER BY pg.first_seen_at DESC LIMIT 1) job ON true
WHERE {GENUINE_CREATION_PREDICATE} AND r.received_at >= %(t0)s AND r.received_at < %(t1)s
ORDER BY lower(o.payload_json->>'email'), r.received_at
"""


def lead_rows(conn: psycopg.Connection, window: ReportWindow) -> List[Dict[str, Any]]:
    """Every lead the report counts, one row each, deduplicated by person."""
    with conn.cursor() as cur:
        cur.execute(_SQL, {"t0": window.start_utc, "t1": window.end_utc})
        rows = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    for row in rows:
        for key in ("created_at", "job_first_seen_at"):
            if row.get(key) is not None:
                row[key] = row[key].isoformat()
    rows.sort(key=lambda r: (r["campaign"] or "", r["employer"] or "", r["email"] or ""))
    return rows


def write_csv(path: Path | str, rows: List[Dict[str, Any]]) -> Path:
    """Write the export, refusing any location inside the repository."""
    target = Path(path).resolve()
    try:
        target.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise ValueError(
            f"the lead export carries personal data and must not be written inside the repository ({target})")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return target
