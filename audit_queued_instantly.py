"""Read-only Instantly overlap audit for queued Airtable backfill records.

Instantly's documented list operation is a POST because it accepts complex
filters; this script calls only POST /leads/list and performs no mutation.
Output excludes email addresses and contact names.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, Iterable
from urllib.request import Request, urlopen

from audit_outbound_displays import _fetch_status_records
from prepare_outbound_backfill import ALLOWED_STATUSES, prepare_records


STATUS_LABELS = {1: "active", 2: "paused", 3: "completed", -1: "bounced", -2: "unsubscribed", -3: "skipped"}


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _list_leads(contacts: list[str]) -> list[Dict[str, Any]]:
    key = str(os.getenv("INSTANTLY_API_KEY") or "").strip()
    base = str(os.getenv("INSTANTLY_BASE_URL") or "https://api.instantly.ai/api/v2").rstrip("/")
    if not key:
        raise ValueError("INSTANTLY_API_KEY is required")
    items: list[Dict[str, Any]] = []
    for contact_batch in _chunks(contacts, 100):
        starting_after = ""
        while True:
            body: Dict[str, Any] = {"contacts": contact_batch, "limit": 100, "distinct_contacts": False}
            if starting_after:
                body["starting_after"] = starting_after
            request = Request(
                f"{base}/leads/list",
                data=json.dumps(body, separators=(",", ":")).encode(),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "TGTC-readonly-audit/1.0",
                },
                method="POST",
            )
            with urlopen(request, timeout=45) as response:  # noqa: S310 - configured Instantly host
                payload = json.loads(response.read().decode("utf-8"))
            page = list(payload.get("items") or [])
            items.extend(page)
            starting_after = str(payload.get("next_starting_after") or "")
            if not starting_after or len(page) < 100:
                break
    return items


def _sent_or_processed(lead: Dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if lead.get("timestamp_last_contact"):
        reasons.append("timestamp_last_contact")
    if lead.get("last_contacted_from"):
        reasons.append("last_contacted_from")
    for field in ("email_open_count", "email_reply_count", "email_click_count"):
        if int(lead.get(field) or 0) > 0:
            reasons.append(field)
    status = int(lead.get("status") or 0)
    if status in {3, -1, -2, -3}:
        reasons.append(f"terminal_status:{STATUS_LABELS.get(status, status)}")
    return bool(reasons), reasons


def audit_records(airtable_records: list[Dict[str, Any]]) -> Dict[str, Any]:
    signing_key = str(os.getenv("VALIDATION_SIGNING_KEY") or "readonly-audit-key")
    manifest = prepare_records(airtable_records, signing_key=signing_key)
    columns = manifest["columns"]
    proposed_by_id = {row[0]: dict(zip(columns, row)) for row in manifest["rows"]}

    records_by_contact: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for record in airtable_records:
        contact = str((record.get("fields") or {}).get("Email") or "").strip().lower()
        if contact:
            records_by_contact[contact].append(record)
    leads = _list_leads(sorted(records_by_contact))
    overlaps = []
    for lead in leads:
        contact = str(lead.get("email") or "").strip().lower()
        for record in records_by_contact.get(contact, []):
            record_id = str(record.get("id") or "")
            proposed = proposed_by_id[record_id]
            payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
            differing_fields = []
            comparisons = {
                "company_name": (lead.get("company_name"), proposed["proposed_outbound_company"]),
                "payload.open_role": (payload.get("open_role"), proposed["proposed_outbound_role"]),
                "payload.open_roles": (payload.get("open_roles"), proposed["proposed_outbound_role"]),
            }
            for field, (current, target) in comparisons.items():
                if str(current or "").strip() != str(target or "").strip():
                    differing_fields.append(field)
            protected, protection_reasons = _sent_or_processed(lead)
            status_value = int(lead.get("status") or 0)
            unsent_candidate = bool(not protected and status_value in {1, 2})
            update_candidate = bool(
                unsent_candidate
                and proposed["safe_backfill"]
                and not proposed["outbound_hold"]
                and differing_fields
            )
            overlaps.append({
                "airtable_record_id": record_id,
                "airtable_status": proposed["lifecycle_status"],
                "instantly_lead_id": lead.get("id"),
                "instantly_status": STATUS_LABELS.get(status_value, str(status_value)),
                "campaign_id": lead.get("campaign"),
                "sent_or_processed": protected,
                "protection_reasons": protection_reasons,
                "outbound_hold": proposed["outbound_hold"],
                "differing_fields": differing_fields,
                "unsent_update_candidate": update_candidate,
                "current_company": lead.get("company_name"),
                "proposed_company": proposed["proposed_outbound_company"],
                "current_open_role": payload.get("open_role"),
                "proposed_open_role": proposed["proposed_outbound_role"],
            })

    return {
        "schema": "tgtc-instantly-queued-overlap-audit/v1",
        "mode": "read_only_list_operation",
        "airtable_scope": list(ALLOWED_STATUSES),
        "airtable_queued_records": len(airtable_records),
        "airtable_contacts_queried": len(records_by_contact),
        "instantly_matching_leads": len(leads),
        "airtable_instantly_associations": len(overlaps),
        "sent_or_processed_protected": sum(bool(row["sent_or_processed"]) for row in overlaps),
        "unsent_update_candidates": sum(bool(row["unsent_update_candidate"]) for row in overlaps),
        "held_overlap": sum(bool(row["outbound_hold"]) for row in overlaps),
        "external_writes": 0,
        "overlaps": overlaps,
    }


def audit() -> Dict[str, Any]:
    airtable_records: list[Dict[str, Any]] = []
    for status in ALLOWED_STATUSES:
        airtable_records.extend(_fetch_status_records(status))
    return audit_records(airtable_records)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(audit(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
