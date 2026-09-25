"""What happens when an out-of-office reply says "back on the 6th" and the 6th arrives.

Measured against the live workspace on 2026-09-25, because the answer decides what this
step can honestly do:

* the nine campaigns run `stop_on_reply: true`, so an out-of-office auto-answer **ends**
  that contact's sequence. All 38 out-of-office repliers sit at lead status 3;
* Instantly exposes **no way to resume a finished lead** -- `/leads/{id}/resume` and
  `/leads/{id}/restart` are both "route not found";
* the nine campaigns have **no subsequences** configured, so there is no follow-up
  vehicle attached to them either.

So a queued `reply_followup_when_back` item cannot, by itself, cause an email. Saying
otherwise would be the lie this module exists to avoid. What it CAN do, and what nothing
did before, is decide each case on its merits when the return date arrives:

* somebody who opted out, or whose address is suppressed, is **closed and never written
  to** -- an out-of-office must never become an opt-out, and an opt-out must never
  become a follow-up;
* a unit whose vacancy has gone is closed as `no_current_vacancy`: there is no longer a
  reason to write;
* everything that survives both is marked **verified and due**, and waits for a sending
  mechanism rather than pretending to be one. Re-adding the contact would replay the
  same four emails, and a subsequence needs copy nobody has written.

Nothing here sends, and nothing here suppresses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import psycopg

from ..db.connection import transaction

WORK_KIND = "reply_followup_when_back"
NEEDS_MECHANISM = "needs_followup_mechanism"
NO_CURRENT_VACANCY = "no_current_vacancy"
PER_RUN_ENV = "TGTC_FOLLOWUPS_PER_RUN"
DEFAULT_PER_RUN = 50
#: How long a verified case waits before it is looked at again. Long enough not to churn,
#: short enough that a suppression arriving later still closes it.
RECHECK_DAYS = 7


def per_run(env: Optional[Dict[str, str]] = None) -> int:
    raw = str((env or {}).get(PER_RUN_ENV, "") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_PER_RUN
    except ValueError:
        value = DEFAULT_PER_RUN
    return max(0, value)


def _close(conn: psycopg.Connection, work_id: int, reason: str) -> None:
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET state = 'closed', close_reason = %s, updated_at = now() "
                        "WHERE id = %s AND state = 'ready'", (reason[:200], work_id))


def _hold(conn: psycopg.Connection, work_id: int, until: datetime) -> None:
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET state = 'waiting', waiting_on = %s, available_at = %s, "
                        "updated_at = now() WHERE id = %s AND state = 'ready'",
                        (NEEDS_MECHANISM, until, work_id))


def due_followups(conn: psycopg.Connection, *, now: Optional[datetime] = None,
                  limit: int = DEFAULT_PER_RUN) -> Dict[str, int]:
    """Decide every follow-up whose return date has arrived. Sends nothing."""
    moment = now or datetime.now(timezone.utc)
    out = {"considered": 0, "suppressed": 0, "no_current_vacancy": 0, "verified_due": 0}
    if limit <= 0:
        return out
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT w.id AS work_id, a.id AS approval_id, a.state AS approval_state,
                   lower(coalesce(p.email, '')) AS email,
                   coalesce(p.opt_out_status, '') AS opt_out,
                   po.state AS posting_state,
                   (SELECT count(*) FROM suppressions s
                     WHERE s.kind = 'person_email' AND s.key = lower(coalesce(p.email, ''))) AS suppressed
            FROM work_items w
            JOIN approvals a ON a.id = w.subject_id
            JOIN people p ON p.id = a.person_id
            LEFT JOIN postings po ON po.id = (a.lead_json->>'posting_id')::bigint
            WHERE w.kind = %s AND w.subject_kind = 'approval' AND w.state = 'ready'
              AND w.available_at <= %s
            ORDER BY w.available_at, w.id LIMIT %s
            """,
            (WORK_KIND, moment, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()

    for row in rows:
        out["considered"] += 1
        work_id = int(row["work_id"])
        if int(row["suppressed"] or 0) or str(row["opt_out"]).strip():
            # Whatever put it there -- an opt-out, a departure, a bounce -- this address
            # is not written to again, and the follow-up ends here.
            _close(conn, work_id, f"suppressed:{row['opt_out'] or 'person_email'}")
            out["suppressed"] += 1
            continue
        if row["approval_state"] == "revoked" or (row["posting_state"] or "closed") != "classified":
            _close(conn, work_id, f"{NO_CURRENT_VACANCY}:{row['posting_state'] or 'no_posting'}")
            out["no_current_vacancy"] += 1
            continue
        _hold(conn, work_id, moment + timedelta(days=RECHECK_DAYS))
        out["verified_due"] += 1
    return out


__all__ = ["due_followups", "per_run", "WORK_KIND", "NEEDS_MECHANISM", "NO_CURRENT_VACANCY", "RECHECK_DAYS"]
