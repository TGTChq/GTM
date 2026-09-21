"""Re-judge already-paid people that now pass the mail-domain rule.

A unit whose only candidates were rejected as ``email:domain_not_employer`` waits
24 hours in ``buyer_search_pending:*``. When `TGTC_CORROBORATED_MAIL_DOMAIN`
is on, a stored person at that employer may now pass
``gates.corroborated_mail_domain``; this pass releases exactly those units so the
normal qualify stage re-judges the STORED record (the reuse path never pays for
the same person twice).

Only ``buyer_search_pending:*`` waits are touched, only when a stored person at
the unit's employer passes the rule now, and never twice within six hours
(by this pass's own release stamp).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import psycopg

from ..db.connection import transaction
from ..domain.gates import MAIL_DOMAIN_FLAG_ENV, corroborated_mail_domain

RELEASE_GUARD = timedelta(hours=6)
#: This pass's OWN release stamp, kept in last_error (which the normal re-wait
#: does not clear). The guard throttles re-releases by this pass; it must not
#: key on updated_at, which ordinary processing also sets -- that blocked the
#: first release for six hours after any normal re-wait.
RELEASE_MARKER = "mail_domain_released@"


def _enabled(env: Optional[Mapping[str, str]]) -> bool:
    env = os.environ if env is None else env
    return str(env.get(MAIL_DOMAIN_FLAG_ENV, "") or "").strip() == "1"


def release_mail_domain_recoverable(conn: psycopg.Connection, *, now: Optional[datetime] = None,
                                    env: Optional[Mapping[str, str]] = None, limit: int = 2000) -> int:
    if not _enabled(env):
        return 0
    moment = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT w.id AS work_id, o.employer_id, e.domain AS employer_domain, e.canonical_name "
            "FROM work_items w JOIN opportunities o ON o.id = w.subject_id "
            "JOIN employers e ON e.id = o.employer_id "
            "WHERE w.kind = 'qualify_opportunity' AND w.state = 'waiting' "
            "AND w.waiting_on LIKE 'buyer_search_pending:%%' AND w.available_at > %s "
            # CASE, not AND: Postgres does not short-circuit, and a normal error
            # text in last_error must never reach the timestamptz cast.
            "AND COALESCE(CASE WHEN w.last_error LIKE %s THEN substring(w.last_error from %s)::timestamptz END, "
            "'-infinity'::timestamptz) <= %s "
            "ORDER BY w.id LIMIT %s",
            (moment, RELEASE_MARKER + "%", len(RELEASE_MARKER) + 1, moment - RELEASE_GUARD, limit),
        )
        units = [dict(r) for r in cur.fetchall()]
    conn.commit()
    released = 0
    for unit in units:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT apollo_person_id, lower(split_part(email, '@', 2)) AS email_domain, email, "
                "COALESCE(facts_json->>'email_alignment', '') AS alignment, "
                "lower(COALESCE(organization_domain, '')) AS org_domain, facts_json "
                "FROM people WHERE employer_id = %s AND email_status = 'verified' AND email IS NOT NULL",
                (unit["employer_id"],),
            )
            people = [dict(r) for r in cur.fetchall()]
        conn.commit()
        employer_domains = {str(unit["employer_domain"] or "").lower()} - {""}
        recoverable = False
        for p in people:
            if p["alignment"] != "" or not isinstance(p["facts_json"], dict):
                continue
            enriched = p["facts_json"].get("enriched") or {}
            siblings = [{"email_domain": q["email_domain"], "alignment": q["alignment"], "org_domain": q["org_domain"]}
                        for q in people if q["apollo_person_id"] != p["apollo_person_id"]]
            ok, _why = corroborated_mail_domain(email=p["email"], person=enriched, employer_domains=employer_domains,
                                                employer_name=str(unit["canonical_name"] or ""), siblings=siblings)
            if ok:
                recoverable = True
                break
        if not recoverable:
            continue
        with transaction(conn):
            with conn.cursor() as cur:
                cur.execute("UPDATE work_items SET available_at = %s, updated_at = %s, last_error = %s "
                            "WHERE id = %s AND state = 'waiting'",
                            (moment, moment, RELEASE_MARKER + moment.isoformat(), unit["work_id"]))
                released += cur.rowcount
    return released


__all__ = ["release_mail_domain_recoverable", "RELEASE_GUARD"]
