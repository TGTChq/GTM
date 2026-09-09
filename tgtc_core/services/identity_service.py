"""Employer identity resolution, cross-source posting dedupe, employer aliases.

Rules (PRODUCT_CONTRACT §5 'Employer identity'):

* anchors come from the organization's own fields: domain (never an ATS/aggregator/
  shortener host), LinkedIn slug, and a non-placeholder name;
* an existing employer is matched by domain alias, then slug alias, then -- only
  when the posting carries neither domain nor slug -- by name key;
* a second domain under one slug is added as an alias only when it is name-consistent
  with the employer's published name; otherwise the disagreement is recorded as
  evidence and the slug identity is kept. Similarity never merges two employers;
* a posting with no anchor at all closes as ``employer_identity_unresolved``
  (reopenable when a later observation carries one).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import psycopg

from ..db.connection import jsonb, transaction
from ..db import work_queue
from ..domain.identity import (
    domain_name_consistent, employer_anchors, employer_key, posting_canonical_key,
)


@dataclass
class IdentityOutcome:
    posting_id: int
    employer_id: Optional[int]
    outcome: str  # resolved | closed
    reason: str = ""
    duplicate_of: Optional[int] = None


def _find_alias(cur, kind: str, value: str) -> Optional[int]:
    if not value:
        return None
    cur.execute("SELECT employer_id FROM employer_aliases WHERE alias_kind = %s AND alias_value = %s", (kind, value))
    row = cur.fetchone()
    return int(row["employer_id"]) if row else None


def _add_alias(cur, employer_id: int, kind: str, value: str, evidence: Dict[str, Any]) -> None:
    if not value:
        return
    cur.execute(
        "INSERT INTO employer_aliases (employer_id, alias_kind, alias_value, evidence) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (alias_kind, alias_value) DO NOTHING",
        (employer_id, kind, value, jsonb(evidence)),
    )


def resolve_employer(conn: psycopg.Connection, *, org: Dict[str, Any], employer_name_fallback: str = "",
                     source: str = "") -> Tuple[Optional[int], str, str]:
    """Find or create the employer for a provider organization block.

    Returns ``(employer_id, employer_key, reason)``; ``employer_id`` is None when no
    anchor exists.
    """
    domain, slug, nk = employer_anchors({**org, "organization": org.get("organization") or employer_name_fallback})
    name = str(org.get("organization") or org.get("org_linkedin_name") or employer_name_fallback or "").strip()
    key = employer_key(domain, slug, nk)
    if not key:
        return None, "", "employer_identity_unresolved"
    with conn.cursor() as cur:
        eid = _find_alias(cur, "domain", domain) if domain else None
        via = "domain" if eid else ""
        if eid is None and slug:
            eid = _find_alias(cur, "linkedin_slug", slug)
            via = "linkedin_slug" if eid else ""
        if eid is None and not domain and not slug and nk:
            eid = _find_alias(cur, "name_key", nk)
            via = "name_key" if eid else ""
        if eid is None:
            # Two workers may resolve the same new employer at once. The partial unique
            # indexes on domain/slug make the second insert fail; a savepoint contains that
            # failure so the worker re-reads the alias the winner just wrote instead of
            # failing the whole posting.
            try:
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO employers (canonical_name, name_key, domain, linkedin_slug, employee_count, industry, agency_flag, facts_json)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                        """,
                        (name or nk or domain or slug, nk or name.lower(), domain or None, slug or None,
                         _int(org.get("org_linkedin_headcount")), org.get("org_linkedin_industry") or None,
                         _bool(org.get("org_linkedin_recruitment_agency_derived")), jsonb({"source": source, "org": org})),
                    )
                    eid = int(cur.fetchone()["id"])
                    _add_alias(cur, eid, "domain", domain, {"source": source, "basis": "organization_url_or_derived"})
                    _add_alias(cur, eid, "linkedin_slug", slug, {"source": source})
                    _add_alias(cur, eid, "name_key", nk, {"source": source})
                return eid, key, "employer_created"
            except psycopg.errors.UniqueViolation:
                eid = (_find_alias(cur, "domain", domain) if domain else None) or (_find_alias(cur, "linkedin_slug", slug) if slug else None)
                if eid is None:
                    cur.execute("SELECT id FROM employers WHERE domain = %s OR (linkedin_slug = %s AND %s <> '') LIMIT 1", (domain or None, slug or "", slug or ""))
                    row = cur.fetchone()
                    if row is None:
                        raise
                    eid = int(row["id"])
                via = "concurrent_creation"
        # Existing employer: corroborate additional anchors, never merge by similarity.
        cur.execute("SELECT canonical_name, domain, linkedin_slug FROM employers WHERE id = %s FOR UPDATE", (eid,))
        emp = cur.fetchone()
        if domain and not emp["domain"]:
            cur.execute("UPDATE employers SET domain = %s, updated_at = now() WHERE id = %s", (domain, eid))
            _add_alias(cur, eid, "domain", domain, {"source": source, "basis": f"matched_via_{via}"})
        elif domain and emp["domain"] and domain != emp["domain"]:
            if domain_name_consistent(emp["canonical_name"], domain):
                _add_alias(cur, eid, "domain", domain, {"source": source, "basis": "name_consistent_second_domain"})
            else:
                cur.execute(
                    "INSERT INTO evidence (subject_kind, subject_id, fact, value, status, source, excerpt) VALUES ('employer', %s, 'domain_disagreement', %s, 'recorded', %s, %s)",
                    (eid, jsonb({"existing": emp["domain"], "observed": domain, "slug": slug}), source, name),
                )
        if slug and not emp["linkedin_slug"]:
            cur.execute("UPDATE employers SET linkedin_slug = %s, updated_at = now() WHERE id = %s", (slug, eid))
            _add_alias(cur, eid, "linkedin_slug", slug, {"source": source, "basis": f"matched_via_{via}"})
        if nk:
            _add_alias(cur, eid, "name_key", nk, {"source": source, "basis": f"matched_via_{via}"})
        headcount = _int(org.get("org_linkedin_headcount"))
        if headcount is not None:
            cur.execute("UPDATE employers SET employee_count = COALESCE(employee_count, %s), industry = COALESCE(industry, %s), updated_at = now() WHERE id = %s",
                        (headcount, org.get("org_linkedin_industry") or None, eid))
        return eid, key, f"employer_{via}"


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}


def resolve_posting_identity(conn: psycopg.Connection, posting_id: int, *, now: Optional[datetime] = None, work_item: Optional[work_queue.WorkItem] = None) -> IdentityOutcome:
    with transaction(conn):
        work_queue.assert_owned(conn, work_item, now=now or datetime.now(timezone.utc))
        with conn.cursor() as cur:
            cur.execute("SELECT id, source, title, employer_name, description_text, org_json, lane, state FROM postings WHERE id = %s FOR UPDATE", (posting_id,))
            posting = cur.fetchone()
            if not posting:
                raise LookupError(f"posting {posting_id} not found")
            if posting["state"] == "expired":
                return IdentityOutcome(posting_id, None, "closed", "posting_expired")
            org = dict(posting["org_json"] or {})
            eid, ekey, reason = resolve_employer(conn, org=org, employer_name_fallback=posting["employer_name"] or "",
                                                source=posting["source"])
            if eid is None:
                cur.execute("UPDATE postings SET state = 'closed', close_reason = %s, updated_at = now() WHERE id = %s",
                            (reason, posting_id))
                return IdentityOutcome(posting_id, None, "closed", reason)
            ckey = posting_canonical_key(ekey, posting["title"], posting["description_text"])
            cur.execute("SELECT id FROM postings WHERE canonical_key = %s AND id <> %s AND source <> %s ORDER BY id LIMIT 1",
                        (ckey, posting_id, posting["source"]))
            dup = cur.fetchone()
            dup_id = int(dup["id"]) if dup else None
            cur.execute(
                "UPDATE postings SET employer_id = %s, canonical_key = %s, duplicate_of_posting_id = %s, state = 'identity_resolved', updated_at = now() WHERE id = %s",
                (eid, ckey, dup_id, posting_id),
            )
            work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=posting_id, lane=posting["lane"], reopen=True, available_at=now)
    return IdentityOutcome(posting_id, eid, "resolved", reason, dup_id)
