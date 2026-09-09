"""Suppressions: imported history, outcome events, and the checks at approval and
delivery time.

Keys mirror the legacy Airtable suppression derivation so existing rows suppress
correctly (INTEGRATION_MAP §4.3):

* ``company_function``: ``domain:<host>|bucket:<function>``, ``name:<key>|bucket:<function>``,
  ``linkedin:<slug>|bucket:<function>`` -- an ACTIVE row for the same company × function
  (Status not Error/Rejected);
* ``person_email``: the normalised email (any status, any campaign);
* ``account``: ``domain:<host>`` / ``name:<key>`` -- only consulted when the account-level
  policy is on (off in production).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import psycopg

from ..db.connection import jsonb, transaction
from ..domain.identity import is_intermediary_host, linkedin_slug, name_key, safe_employer_domain
from ..policy.requirements import rule

RETRYABLE_STATUSES = {"error", "rejected"}
OUTCOME_SUPPRESSING_EVENTS = {"unsubscribe", "unsubscribed", "reply", "replied", "bounce", "bounced",
                              "existing_customer", "do_not_contact", "spam_complaint"}


@dataclass
class ImportCounts:
    rows: int = 0
    inserted: int = 0
    already_present: int = 0
    skipped: int = 0
    by_kind: Dict[str, int] = field(default_factory=dict)


def company_function_keys(*, domain: str, name: str, slug: str, function_key: str) -> Set[str]:
    keys: Set[str] = set()
    d = safe_employer_domain(domain)
    if d and not is_intermediary_host(d):
        keys.add(f"domain:{d}|bucket:{function_key}")
    nk = name_key(name)
    if nk:
        keys.add(f"name:{nk}|bucket:{function_key}")
    s = linkedin_slug(slug)
    if s:
        keys.add(f"linkedin:{s}|bucket:{function_key}")
    return keys


def account_keys(*, domain: str, name: str) -> Set[str]:
    keys: Set[str] = set()
    d = safe_employer_domain(domain)
    if d:
        keys.add(f"domain:{d}")
    nk = name_key(name)
    if nk:
        keys.add(f"name:{nk}")
    return keys


def add(conn: psycopg.Connection, *, kind: str, key: str, source: str, reason: str,
        evidence: Optional[Dict[str, Any]] = None) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO suppressions (kind, key, source, reason, evidence) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (kind, key) DO NOTHING RETURNING id",
            (kind, key.strip().lower(), source, reason, jsonb(evidence or {})),
        )
        return cur.fetchone() is not None


def check(conn: psycopg.Connection, *, email: str = "", company_function: Iterable[str] = (),
          account: Iterable[str] = (), account_level: Optional[bool] = None) -> List[str]:
    """Return the suppression hits as ``kind:key`` strings (empty = clear)."""
    checks: List[tuple] = []
    if email:
        checks.append(("person_email", email.strip().lower()))
    for k in company_function:
        checks.append(("company_function", k.lower()))
    if account_level is None:
        account_level = bool(rule("account_level_suppression"))
    if account_level:
        for k in account:
            checks.append(("account", k.lower()))
    if not checks:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, key FROM suppressions WHERE (kind, key) IN (SELECT unnest(%s::text[]), unnest(%s::text[]))",
            ([c[0] for c in checks], [c[1] for c in checks]),
        )
        return [f"{r['kind']}:{r['key']}" for r in cur.fetchall()]


def import_airtable_rows(conn: psycopg.Connection, rows: Sequence[Dict[str, Any]], *, source: str = "airtable_import") -> ImportCounts:
    """Idempotent: importing the same rows twice inserts nothing new."""
    counts = ImportCounts()
    with transaction(conn):
        for rec in rows:
            counts.rows += 1
            fields = rec.get("fields") or rec
            status = str(fields.get("Status") or "").strip().lower()
            email = str(fields.get("Email") or "").strip().lower()
            bucket = str(fields.get("Role Bucket") or "").strip().lower()
            evidence = {"record_id": rec.get("id"), "status": status, "lead_key": fields.get("Lead Key")}
            inserted_any = False
            if email:
                if add(conn, kind="person_email", key=email, source=source, reason=f"airtable_row:{status or 'blank'}", evidence=evidence):
                    counts.by_kind["person_email"] = counts.by_kind.get("person_email", 0) + 1
                    inserted_any = True
            if status not in RETRYABLE_STATUSES and bucket:
                identity = str(fields.get("Outbound Company Identity") or "").strip().lower()
                confidence = str(fields.get("Outbound Company Confidence") or "").strip().lower()
                slug = ""
                if identity.startswith("linkedin:") and not fields.get("Outbound Hold") and confidence in {"high", "medium"}:
                    slug = identity.split(":", 1)[1]
                for key in company_function_keys(domain=str(fields.get("Website") or ""), name=str(fields.get("Company") or ""),
                                                 slug=slug, function_key=bucket):
                    if add(conn, kind="company_function", key=key, source=source, reason=f"active_airtable_row:{status or 'blank'}", evidence=evidence):
                        counts.by_kind["company_function"] = counts.by_kind.get("company_function", 0) + 1
                        inserted_any = True
                for key in account_keys(domain=str(fields.get("Website") or ""), name=str(fields.get("Company") or "")):
                    if add(conn, kind="account", key=key, source=source, reason=f"active_airtable_row:{status or 'blank'}", evidence=evidence):
                        counts.by_kind["account"] = counts.by_kind.get("account", 0) + 1
                        inserted_any = True
            if inserted_any:
                counts.inserted += 1
            elif email or bucket:
                counts.already_present += 1
            else:
                counts.skipped += 1
    return counts


def import_instantly_emails(conn: psycopg.Connection, emails: Iterable[str], *, campaign_id: str = "",
                            source: str = "instantly_import") -> ImportCounts:
    counts = ImportCounts()
    with transaction(conn):
        for email in emails:
            counts.rows += 1
            e = str(email or "").strip().lower()
            if not e or "@" not in e:
                counts.skipped += 1
                continue
            if add(conn, kind="person_email", key=e, source=source, reason="instantly_workspace_lead", evidence={"campaign_id": campaign_id}):
                counts.inserted += 1
            else:
                counts.already_present += 1
    return counts


def apply_outcome_event(conn: psycopg.Connection, *, provider: str, event_type: str, dedupe_key: str,
                        email: str = "", campaign_id: str = "", external_id: str = "",
                        occurred_at: Optional[datetime] = None, payload: Optional[Dict[str, Any]] = None) -> str:
    """Record one outcome event exactly once. Returns 'applied' | 'recorded' | 'duplicate'.

    Suppressing events (unsubscribe, reply, bounce, existing customer, complaint)
    create a person_email suppression the first time only.
    """
    kind = str(event_type or "").strip().lower()
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO outcome_events (provider, event_type, dedupe_key, email, campaign_id, external_id, occurred_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id
                """,
                (provider, kind, dedupe_key, email.strip().lower() or None, campaign_id or None, external_id or None,
                 occurred_at, jsonb(payload or {})),
            )
            row = cur.fetchone()
            if not row:
                return "duplicate"
            if kind in OUTCOME_SUPPRESSING_EVENTS and email:
                add(conn, kind="person_email", key=email, source=f"outcome:{provider}", reason=kind,
                    evidence={"campaign_id": campaign_id, "external_id": external_id, "dedupe_key": dedupe_key})
                cur.execute("UPDATE outcome_events SET applied = true, applied_at = now() WHERE id = %s", (row["id"],))
                return "applied"
            return "recorded"
