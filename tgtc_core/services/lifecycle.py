"""Posting lifecycle (R11): known expiry and closure are enforced at observation, at
approval and before a delayed delivery. Re-observation never resets commercial age.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg

from ..db.connection import transaction

ACTIVE_STATES = ("new", "identity_resolved", "classified")


def posting_is_active(posting: Dict[str, Any], *, now: datetime) -> tuple[bool, str]:
    """(active, reason). A posting is active when its state is not closed/expired and
    its known ``date_valid_through`` has not passed."""
    state = str(posting.get("state") or "")
    if state in ("closed", "expired"):
        return False, f"posting_{state}"
    vt = posting.get("date_valid_through")
    if isinstance(vt, datetime) and vt < now:
        return False, "posting_expired"
    return True, "active"


def expire_postings(conn: psycopg.Connection, *, now: Optional[datetime] = None) -> Dict[str, int]:
    """Expire postings past ``date_valid_through``; close opportunities left with no
    active compatible posting. Approvals awaiting delivery are handled by the delivery
    precheck (which revokes them); nothing here touches delivered history."""
    moment = now or datetime.now(timezone.utc)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE postings SET state = 'expired', expired_at = %s, updated_at = now() "
                "WHERE date_valid_through IS NOT NULL AND date_valid_through < %s AND state IN ('new', 'identity_resolved', 'classified') "
                "RETURNING id",
                (moment, moment),
            )
            expired = cur.rowcount
            cur.execute(
                """
                UPDATE opportunities o SET state = 'closed', close_reason = 'postings_expired', updated_at = now()
                WHERE o.state = 'open' AND NOT EXISTS (
                    SELECT 1 FROM opportunity_postings op JOIN postings p ON p.id = op.posting_id
                    WHERE op.opportunity_id = o.id AND p.state IN ('new', 'identity_resolved', 'classified')
                      AND (p.date_valid_through IS NULL OR p.date_valid_through >= %s))
                RETURNING id
                """,
                (moment,),
            )
            closed = cur.rowcount
            # Work items for expired postings are closed, never left waiting.
            cur.execute(
                "UPDATE work_items w SET state = 'closed', close_reason = 'posting_expired', lease_token = NULL, lease_expires_at = NULL, updated_at = now() "
                "FROM postings p WHERE w.subject_kind = 'posting' AND w.subject_id = p.id AND p.state = 'expired' AND w.state IN ('ready', 'retry', 'waiting')"
            )
            items = cur.rowcount
    return {"postings_expired": expired, "opportunities_closed": closed, "work_items_closed": items}
