"""Re-decide an unknown contact jurisdiction from the person's own stored evidence.

Measured, production 2026-09-20: 100 approved contacts were blocked
``compliance:unknown_jurisdiction:absent``. 58 of them carried "United States"
in their OWN stored Apollo evidence (``people.facts_json.enriched``). The
column ``people.contact_country`` arrived with migration 011, nullable and with
no backfill, and the contact-reuse path ("never pay twice for a stored answer")
reuses a stored person without recomputing it. The approval read only the
column, so a known US person was treated as an unknown jurisdiction.

This pass fixes the stored rows. What it will and will not do:

* it reads ONLY the person's own stored fields, via
  ``jurisdiction.resolve_person_contact_country`` -- never the employer's
  location, which is not proof of a contact's residence;
* it never unblocks on the country alone: the FULL compliance evaluation is
  re-run with the resolved country, from the approval's stored snapshot;
* a resolved country that may not be emailed (DE, an unverified UK entity) is
  RECLASSIFIED under its correct named reason and stays blocked;
* a recovered row goes back to ``pending``, not to ``delivered``: delivery's own
  precheck then re-runs suppression, the retired-campaign guard, the canary
  ceiling and posting validity before anything is sent;
* rows blocked for any other reason are never touched;
* zero paid enrichment -- it re-reads records already bought.

Idempotent: a recovered row no longer carries the unknown-jurisdiction reason,
so a second pass finds nothing to do.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import psycopg

from ..db.connection import jsonb, transaction
from ..domain.jurisdiction import resolve_person_contact_country
from ..policy.compliance import OPT_OUT_NONE, ComplianceRecord, evaluate

UNKNOWN_JURISDICTION = "compliance:unknown_jurisdiction:absent"


def _text(lead: Mapping[str, Any], key: str) -> str:
    return str(lead.get(key) or "")


def _record(lead: Mapping[str, Any], person: Mapping[str, Any], contact_country: str) -> ComplianceRecord:
    """The approval's stored compliance snapshot, with the resolved country.

    `unsubscribe_available` and `suppression_available` are structural in this
    core, exactly as `OpportunityService.outreach_controls` states: every
    Instantly campaign carries the unsubscribe link and delivery runs the
    suppression check before every send.
    """
    facts = person.get("facts_json") or {}
    alignment = str((facts or {}).get("email_alignment") or "") if isinstance(facts, Mapping) else ""
    due_at = lead.get("privacy_notice_due_at")
    return ComplianceRecord(
        job_country=_text(lead, "job_country"),
        company_country=_text(lead, "company_country"),
        contact_country=contact_country,
        employer_legal_entity_type=_text(lead, "employer_legal_entity_type"),
        corporate_subscriber_status=_text(lead, "corporate_subscriber_status") or "unknown",
        legal_basis=_text(lead, "legal_basis"),
        legal_basis_evidence=_text(lead, "legal_basis_evidence"),
        privacy_notice_configured=due_at not in (None, ""),
        privacy_notice_due_at=due_at,
        opt_out_status=_text(lead, "opt_out_status") or OPT_OUT_NONE,
        email=_text(lead, "email"),
        email_alignment=alignment or _text(lead, "email_alignment"),
        email_status=_text(lead, "email_status"),
        unsubscribe_available=True,
        suppression_available=True,
    )


def recheck_unknown_jurisdiction(conn: psycopg.Connection, *, now: Optional[datetime] = None,
                                 limit: int = 1000) -> Dict[str, int]:
    moment = now or datetime.now(timezone.utc)
    counts = {"examined": 0, "recovered_eligible": 0, "reclassified_blocked": 0, "still_unknown": 0}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT a.id, a.person_id, a.lead_json FROM approvals a "
            "JOIN delivery_outbox o ON o.approval_id = a.id "
            "WHERE o.state = 'blocked' AND o.blocked_reason = %s ORDER BY a.id LIMIT %s",
            (UNKNOWN_JURISDICTION, limit),
        )
        approvals = [dict(r) for r in cur.fetchall()]
    conn.commit()

    for row in approvals:
        counts["examined"] += 1
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM people WHERE id = %s", (row["person_id"],))
            person = cur.fetchone()
        conn.commit()
        country, provenance = resolve_person_contact_country(dict(person or {}))
        if not country:
            counts["still_unknown"] += 1
            continue
        lead = dict(row["lead_json"] or {})
        decision = evaluate(_record(lead, dict(person), country))
        snapshot = {"contact_country": country, "contact_country_provenance": provenance,
                    "outreach_eligible": decision.outreach_eligible,
                    "outreach_block_reason": decision.outreach_block_reason,
                    "compliance_rechecked_at": moment.isoformat()}
        with transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE people SET contact_country = %s, "
                    "facts_json = COALESCE(facts_json, '{}'::jsonb) || %s, updated_at = now() "
                    "WHERE id = %s AND COALESCE(contact_country, '') = ''",
                    (country, jsonb({"contact_country_provenance": provenance}), row["person_id"]),
                )
                cur.execute(
                    "UPDATE approvals SET contact_country = %s, outreach_eligible = %s, outreach_block_reason = %s, "
                    "lead_json = lead_json || %s WHERE id = %s",
                    (country, decision.outreach_eligible, decision.outreach_block_reason or None,
                     jsonb(snapshot), row["id"]),
                )
                if decision.outreach_eligible:
                    cur.execute(
                        "UPDATE delivery_outbox SET state = 'pending', blocked_reason = NULL, available_at = %s, "
                        "updated_at = now() WHERE approval_id = %s AND state = 'blocked' AND blocked_reason = %s",
                        (moment, row["id"], UNKNOWN_JURISDICTION),
                    )
                else:
                    cur.execute(
                        "UPDATE delivery_outbox SET blocked_reason = %s, updated_at = now() "
                        "WHERE approval_id = %s AND state = 'blocked' AND blocked_reason = %s",
                        (decision.outreach_block_reason[:200], row["id"], UNKNOWN_JURISDICTION),
                    )
        counts["recovered_eligible" if decision.outreach_eligible else "reclassified_blocked"] += 1
    return counts


__all__ = ["UNKNOWN_JURISDICTION", "recheck_unknown_jurisdiction"]
