"""Read-only extraction of live role-display audit populations.

External operations are limited to Airtable GET, Instantly campaign GET, and
Instantly's documented read-only POST /leads/list operation.  The output omits
email addresses and personal names.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from audit_instantly_campaign_outbound import (
    _airtable_records,
    _campaigns,
    _list_campaign_leads,
    _match_record,
    _record_campaign_id,
    _text,
)


QUEUED_STATUSES = {"Pending", "Error"}
EVIDENCE_KEYS = {
    "job_title",
    "canonical_job_title",
    "matched_role",
    "role_bucket",
    "role_focus",
    "seniority",
    "job_seniority",
    "work_arrangement",
    "job_is_remote",
    "salary",
    "salary_min",
    "salary_max",
    "job_location",
    "canonical_location",
    "employment_type",
    "canonical_employment_type",
}


def _parsed_json(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _short_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value)[:500]
    return None


def _evidence_signals(value: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                normalized_key = str(key).strip().lower()
                if normalized_key in EVIDENCE_KEYS and normalized_key not in found:
                    scalar = _short_scalar(child)
                    if scalar not in (None, ""):
                        found[normalized_key] = scalar
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def _record_context(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    evidence = _evidence_signals(_parsed_json(fields.get("Evidence Bundle")))
    return {
        "airtable_record_id": record.get("id"),
        "status": fields.get("Status"),
        "raw_title": evidence.get("job_title") or fields.get("Open Role"),
        "canonical_title": evidence.get("canonical_job_title") or fields.get("Open Role"),
        "airtable_open_role": fields.get("Open Role"),
        "matched_role": fields.get("Matched Role") or evidence.get("matched_role"),
        "role_bucket": fields.get("Role Bucket") or evidence.get("role_bucket"),
        "role_focus": fields.get("Role Focus") or evidence.get("role_focus"),
        "outbound_role": fields.get("Outbound Role"),
        "outbound_roles": fields.get("Outbound Roles"),
        "location": fields.get("Location") or evidence.get("canonical_location") or evidence.get("job_location"),
        "employment_type": fields.get("Employment Type") or evidence.get("canonical_employment_type") or evidence.get("employment_type"),
        "work_arrangement": evidence.get("work_arrangement"),
        "job_is_remote": evidence.get("job_is_remote"),
        "seniority": evidence.get("seniority") or evidence.get("job_seniority"),
        "salary": evidence.get("salary"),
        "salary_min": evidence.get("salary_min"),
        "salary_max": evidence.get("salary_max"),
        "campaign_id": _record_campaign_id(fields),
        "outbound_role_confidence": fields.get("Outbound Role Confidence"),
        "outbound_role_evidence": _parsed_json(fields.get("Outbound Role Evidence")),
    }


def extract() -> dict[str, Any]:
    airtable = _airtable_records()
    records_by_email: dict[str, list[dict[str, Any]]] = {}
    from audit_instantly_campaign_outbound import _email

    for record in airtable:
        email = _email((record.get("fields") or {}).get("Email"))
        if email:
            records_by_email.setdefault(email, []).append(record)

    queued = [
        {"population": "airtable_queued", **_record_context(record)}
        for record in airtable
        if _text((record.get("fields") or {}).get("Status")) in QUEUED_STATUSES
    ]

    campaigns = _campaigns()
    campaign_by_id = {row["id"]: row for row in campaigns}
    instantly_unsent: list[dict[str, Any]] = []
    unmatched_unsent: list[dict[str, Any]] = []
    for campaign in campaigns:
        for lead in _list_campaign_leads(campaign["id"]):
            if lead.get("timestamp_last_contact"):
                continue
            record, match_method, warning = _match_record(lead, records_by_email)
            if not record:
                unmatched_unsent.append({
                    "instantly_lead_id": lead.get("id"),
                    "campaign_id": lead.get("campaign"),
                    "campaign_name": campaign.get("name"),
                    "reason": warning,
                })
                continue
            payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
            context = _record_context(record)
            context.update({
                "population": "instantly_unsent",
                "instantly_lead_id": lead.get("id"),
                "campaign_id": lead.get("campaign"),
                "campaign_name": campaign_by_id[lead.get("campaign")].get("name"),
                "match_method": match_method,
                "match_warning": warning,
                "outbound_role": payload.get("open_role"),
                "outbound_roles": payload.get("open_roles"),
                "instantly_company_name": lead.get("company_name"),
                "instantly_lead_status": lead.get("status"),
            })
            instantly_unsent.append(context)

    return {
        "schema": "tgtc-outbound-role-audit-population/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "provider_operations": [
            "GET Airtable records",
            "GET Instantly campaigns",
            "POST Instantly /leads/list",
        ],
        "external_writes": 0,
        "scope": {
            "airtable_statuses": sorted(QUEUED_STATUSES),
            "instantly_contact_filter": "timestamp_last_contact null/absent",
            "campaign_count": len(campaigns),
        },
        "summary": {
            "airtable_total": len(airtable),
            "airtable_queued": len(queued),
            "instantly_unsent_matched": len(instantly_unsent),
            "instantly_unsent_unmatched": len(unmatched_unsent),
            "combined_records": len(queued) + len(instantly_unsent),
        },
        "campaigns": campaigns,
        "records": queued + instantly_unsent,
        "unmatched_unsent": unmatched_unsent,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/outbound_role_population_20260817.json")
    args = parser.parse_args(argv)
    result = extract()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output.resolve()),
        "summary": result["summary"],
        "external_writes": 0,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
