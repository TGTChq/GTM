"""Read-only campaign-wide Instantly/Airtable outbound-display reconciliation.

The Instantly list endpoint is a POST because it accepts complex filters, but
this utility calls only the documented read operation ``POST /leads/list`` plus
``GET /campaigns/{id}``. Airtable is accessed with GET requests only. Output
contains record/lead IDs and display values, never email addresses or names.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import config
from company_display_resolver import resolve_company_display
from prepare_outbound_backfill import _resolver_cache
from role_display_resolver import resolve_role_display


CAMPAIGN_ENV_KEYS = (
    "INSTANTLY_CAMPAIGN_ENGINEERING",
    "INSTANTLY_CAMPAIGN_GTM",
    "INSTANTLY_CAMPAIGN_MARKETING",
    "INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS",
    "INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT",
    "INSTANTLY_CAMPAIGN_ECOMMERCE",
    "INSTANTLY_CAMPAIGN_PEOPLE_HR",
    "INSTANTLY_CAMPAIGN_FINANCE",
    "INSTANTLY_CAMPAIGN_OPERATIONS",
    "INSTANTLY_CAMPAIGN_PRODUCT",
)
CAMPAIGN_STATUS = {
    -99: "account_suspended",
    -2: "bounce_protect",
    -1: "accounts_unhealthy",
    0: "draft",
    1: "active",
    2: "paused",
    3: "completed",
    4: "running_subsequences",
}
LEAD_STATUS = {1: "active", 2: "paused", 3: "completed", -1: "bounced", -2: "unsubscribed", -3: "skipped"}
MUTABLE_UNSENT_LEAD_STATUSES = {1, 2}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _email(value: Any) -> str:
    return str(value or "").strip().lower()


def _contact_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _json_request(
    url: str,
    *,
    method: str = "GET",
    body: Dict[str, Any] | None = None,
    headers: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    request_headers = {"Accept": "application/json", "User-Agent": "TGTC-readonly-campaign-audit/1.0"}
    request_headers.update(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            request = Request(url, data=data, headers=request_headers, method=method)
            with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed/configured provider hosts
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            last_error = RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}")
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                break
            time.sleep(attempt + 1)
        except Exception as exc:  # noqa: BLE001 - bounded retry around provider reads
            last_error = exc
            if attempt == 4:
                break
            time.sleep(attempt + 1)
    raise RuntimeError(f"Read-only provider request failed after retries: {last_error}")


def _campaign_ids() -> Dict[str, list[str]]:
    by_id: Dict[str, list[str]] = defaultdict(list)
    for key in CAMPAIGN_ENV_KEYS:
        campaign_id = _text(os.getenv(key))
        if campaign_id:
            by_id[campaign_id].append(key)
    if len(by_id) != 9:
        raise RuntimeError(f"Expected exactly nine distinct configured campaign IDs; found {len(by_id)}")
    return dict(by_id)


def _campaigns() -> list[Dict[str, Any]]:
    api_key = _text(os.getenv("INSTANTLY_API_KEY"))
    base = _text(os.getenv("INSTANTLY_BASE_URL")) or "https://api.instantly.ai/api/v2"
    if not api_key:
        raise ValueError("INSTANTLY_API_KEY is required")
    result = []
    for campaign_id, env_keys in _campaign_ids().items():
        payload = _json_request(
            f"{base.rstrip('/')}/campaigns/{quote(campaign_id, safe='')}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        status = int(payload.get("status") or 0)
        schedules = []
        for schedule in (payload.get("campaign_schedule") or {}).get("schedules") or []:
            schedules.append({
                "name": schedule.get("name"),
                "timezone": schedule.get("timezone"),
                "timing": schedule.get("timing"),
                "days": schedule.get("days"),
            })
        result.append({
            "id": campaign_id,
            "name": payload.get("name"),
            "status": status,
            "status_label": CAMPAIGN_STATUS.get(status, f"unknown:{status}"),
            "env_keys": env_keys,
            "schedule": {
                "start_date": (payload.get("campaign_schedule") or {}).get("start_date"),
                "end_date": (payload.get("campaign_schedule") or {}).get("end_date"),
                "schedules": schedules,
            },
        })
    return sorted(result, key=lambda row: str(row.get("name") or ""))


def _list_campaign_leads(campaign_id: str) -> list[Dict[str, Any]]:
    api_key = _text(os.getenv("INSTANTLY_API_KEY"))
    base = _text(os.getenv("INSTANTLY_BASE_URL")) or "https://api.instantly.ai/api/v2"
    items: list[Dict[str, Any]] = []
    cursor = ""
    while True:
        body: Dict[str, Any] = {"campaign": campaign_id, "limit": 100, "distinct_contacts": False}
        if cursor:
            body["starting_after"] = cursor
        payload = _json_request(
            f"{base.rstrip('/')}/leads/list",
            method="POST",
            body=body,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        page = list(payload.get("items") or [])
        items.extend(page)
        next_cursor = _text(payload.get("next_starting_after"))
        if not next_cursor:
            break
        if next_cursor == cursor:
            raise RuntimeError(f"Instantly pagination cursor did not advance for campaign {campaign_id}")
        cursor = next_cursor
    return items


def _airtable_records() -> list[Dict[str, Any]]:
    if not config.AIRTABLE_TOKEN or not config.AIRTABLE_BASE_ID or not config.AIRTABLE_TABLE_NAME:
        raise ValueError("Airtable credentials/base/table are required")
    base = f"https://api.airtable.com/v0/{config.AIRTABLE_BASE_ID}/{quote(config.AIRTABLE_TABLE_NAME, safe='')}"
    headers = {"Authorization": f"Bearer {config.AIRTABLE_TOKEN}"}
    items: list[Dict[str, Any]] = []
    offset = ""
    while True:
        params: list[tuple[str, str | int]] = [("pageSize", 100)]
        if offset:
            params.append(("offset", offset))
        payload = _json_request(f"{base}?{urlencode(params)}", headers=headers)
        items.extend(payload.get("records") or [])
        offset = _text(payload.get("offset"))
        if not offset:
            break
    return items


def _record_campaign_id(fields: Dict[str, Any]) -> str:
    explicit = _text(fields.get("Campaign ID"))
    if explicit:
        return explicit
    return _text(config.resolve_campaign_id(_text(fields.get("Role Bucket")), fields.get("Employees")))


def _resolve_target(fields: Dict[str, Any], cache) -> Dict[str, Any]:
    raw_company = _text(fields.get("Company"))
    raw_role = _text(fields.get("Open Role"))
    prior_identity = _text(fields.get("Outbound Company Identity"))
    linkedin_slug = prior_identity.split(":", 1)[1] if prior_identity.startswith("linkedin:") else ""
    company_result = resolve_company_display(
        organization=raw_company,
        canonical_company_name=raw_company,
        org_linkedin_slug=linkedin_slug,
        employer_domain=_text(fields.get("Website")),
        canonical_identity_verified=_text(fields.get("Firmographics Status")).upper() == "PASS",
        cache=cache,
        persist=False,
    )
    role_result = resolve_role_display({
        "job_title": raw_role,
        "canonical_job_title": raw_role,
        "_matched_role": fields.get("Matched Role"),
        "role_focus": fields.get("Role Focus"),
        "job_location": fields.get("Location"),
        "canonical_employment_type": fields.get("Employment Type"),
        "job_employment_type": fields.get("Employment Type"),
    })
    stored_company = _text(fields.get("Outbound Company"))
    stored_role = _text(fields.get("Outbound Role"))
    stored_roles = _text(fields.get("Outbound Roles"))
    company = stored_company or company_result.name
    role = stored_role or role_result.name
    roles = stored_roles or role
    company_confidence = _text(fields.get("Outbound Company Confidence")).lower() or company_result.confidence
    role_confidence = _text(fields.get("Outbound Role Confidence")).lower() or role_result.confidence
    source = "airtable_outbound" if stored_company and stored_role else "computed_read_only_from_airtable_canonical"
    reasons = []
    if bool(fields.get("Outbound Hold")):
        reasons.append("airtable_outbound_hold")
    if company_confidence not in {"high", "medium"}:
        reasons.append("company_confidence_not_send_safe")
    if not company_result.identity_safe and not stored_company:
        reasons.append("company_identity_ambiguous")
    if role_confidence == "low":
        reasons.append("role_confidence_low")
    if role_result.hold:
        reasons.append("outbound_role_ambiguous")
    if not company:
        reasons.append("missing_outbound_company")
    if not role or not roles:
        reasons.append("missing_outbound_role")
    if _text(fields.get("Status")) == config.AIRTABLE_STATUS_REJECTED:
        reasons.append("airtable_rejected")
    return {
        "company_name": company,
        "open_role": role,
        "open_roles": roles,
        "company_confidence": company_confidence,
        "role_confidence": role_confidence,
        "company_identity": _text(fields.get("Outbound Company Identity")) or company_result.identity_key,
        "source": source,
        "hold": bool(reasons),
        "hold_reasons": reasons,
    }


def _match_record(
    lead: Dict[str, Any],
    records_by_email: Dict[str, list[Dict[str, Any]]],
) -> tuple[Dict[str, Any] | None, str, str]:
    contact = _email(lead.get("email"))
    if not contact:
        return None, "", "missing_instantly_email"
    candidates = records_by_email.get(contact) or []
    if not candidates:
        return None, "", "no_airtable_email_match"
    campaign_id = _text(lead.get("campaign"))
    exact_campaign = [
        record for record in candidates
        if _record_campaign_id(record.get("fields") or {}) == campaign_id
    ]
    if len(exact_campaign) == 1:
        return exact_campaign[0], "normalized_email+campaign", ""
    if len(exact_campaign) > 1:
        return None, "", "ambiguous_airtable_email_campaign_matches"
    if len(candidates) == 1:
        return candidates[0], "unique_normalized_email", "campaign_identity_not_equal"
    return None, "", "ambiguous_airtable_email_matches"


def audit() -> Dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    campaigns = _campaigns()
    campaign_by_id = {row["id"]: row for row in campaigns}
    airtable = _airtable_records()
    records_by_email: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for record in airtable:
        contact = _email((record.get("fields") or {}).get("Email"))
        if contact:
            records_by_email[contact].append(record)

    all_leads: list[Dict[str, Any]] = []
    for campaign in campaigns:
        all_leads.extend(_list_campaign_leads(campaign["id"]))
    lead_ids = [_text(lead.get("id")) for lead in all_leads]
    if len(set(lead_ids)) != len(lead_ids):
        raise RuntimeError("Duplicate Instantly lead IDs appeared across campaign enumeration")

    cache = _resolver_cache()
    safe_updates = []
    held_unsent = []
    unmatched = []
    contacted = 0
    unsent = 0
    company_corrections = 0
    role_corrections = 0
    matched = 0
    campaign_counts: Dict[str, Counter] = {campaign["id"]: Counter() for campaign in campaigns}
    lead_status_counts: Counter = Counter()

    for lead in all_leads:
        lead_id = _text(lead.get("id"))
        campaign_id = _text(lead.get("campaign"))
        campaign = campaign_by_id[campaign_id]
        campaign_count = campaign_counts[campaign_id]
        lead_status_value = int(lead.get("status") or 0)
        lead_status_label = LEAD_STATUS.get(lead_status_value, f"unknown:{lead_status_value}")
        lead_status_counts[lead_status_label] += 1
        campaign_count["total"] += 1
        was_contacted = bool(lead.get("timestamp_last_contact"))
        if was_contacted:
            contacted += 1
            campaign_count["contacted"] += 1
        else:
            unsent += 1
            campaign_count["unsent"] += 1

        record, match_method, match_warning = _match_record(lead, records_by_email)
        if not record:
            campaign_count["unmatched"] += 1
            unmatched.append({
                "instantly_lead_id": lead_id,
                "campaign_id": campaign_id,
                "campaign_name": campaign.get("name"),
                "contact_state": "contacted" if was_contacted else "unsent",
                "lead_status": lead_status_label,
                "contact_hash": _contact_hash(_email(lead.get("email"))),
                "reason": match_warning,
            })
            continue
        matched += 1
        campaign_count["matched"] += 1
        if was_contacted:
            continue

        fields = record.get("fields") or {}
        target = _resolve_target(fields, cache)
        payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
        current = {
            "company_name": _text(lead.get("company_name")),
            "open_role": _text(payload.get("open_role")),
            "open_roles": _text(payload.get("open_roles")),
        }
        proposed = {
            "company_name": target["company_name"],
            "open_role": target["open_role"],
            "open_roles": target["open_roles"],
        }
        company_change = current["company_name"] != proposed["company_name"]
        role_change = (
            current["open_role"] != proposed["open_role"]
            or current["open_roles"] != proposed["open_roles"]
        )
        company_corrections += int(company_change)
        role_corrections += int(role_change)
        if company_change:
            campaign_count["company_correction"] += 1
        if role_change:
            campaign_count["role_correction"] += 1
        base_row = {
            "instantly_lead_id": lead_id,
            "campaign_id": campaign_id,
            "campaign_name": campaign.get("name"),
            "instantly_lead_status": lead_status_label,
            "airtable_record_id": record.get("id"),
            "airtable_status": fields.get("Status"),
            "match_method": match_method,
            "match_warning": match_warning,
            "target_source": target["source"],
            "company_confidence": target["company_confidence"],
            "role_confidence": target["role_confidence"],
            "company_identity": target["company_identity"],
            "current": current,
            "proposed": proposed,
            "company_change": company_change,
            "role_change": role_change,
        }
        if target["hold"] or lead_status_value not in MUTABLE_UNSENT_LEAD_STATUSES:
            reasons = list(target["hold_reasons"])
            if lead_status_value not in MUTABLE_UNSENT_LEAD_STATUSES:
                reasons.append(f"terminal_instantly_lead_status:{lead_status_label}")
            held_unsent.append({**base_row, "hold_reasons": reasons})
            campaign_count["held_unsent"] += 1
            continue
        fields_to_patch = []
        if company_change:
            fields_to_patch.append("company_name")
        if current["open_role"] != proposed["open_role"]:
            fields_to_patch.append("payload.open_role")
        if current["open_roles"] != proposed["open_roles"]:
            fields_to_patch.append("payload.open_roles")
        if fields_to_patch:
            safe_updates.append({**base_row, "fields_to_patch": fields_to_patch})
            campaign_count["safe_update"] += 1

    unsent_unmatched = [row for row in unmatched if row["contact_state"] == "unsent"]
    representatives = []
    seen_campaigns = set()
    for row in safe_updates:
        if row["campaign_id"] in seen_campaigns and len(representatives) >= 9:
            continue
        representatives.append({
            "campaign_name": row["campaign_name"],
            "company": [row["current"]["company_name"], row["proposed"]["company_name"]],
            "open_role": [row["current"]["open_role"], row["proposed"]["open_role"]],
            "open_roles": [row["current"]["open_roles"], row["proposed"]["open_roles"]],
            "fields_to_patch": row["fields_to_patch"],
        })
        seen_campaigns.add(row["campaign_id"])
        if len(representatives) >= 12:
            break

    for campaign in campaigns:
        counts = campaign_counts[campaign["id"]]
        campaign["counts"] = {key: counts.get(key, 0) for key in (
            "total", "contacted", "unsent", "matched", "unmatched",
            "company_correction", "role_correction", "safe_update", "held_unsent",
        )}

    active_campaigns = [campaign for campaign in campaigns if campaign["status"] in {1, 4}]
    return {
        "schema": "tgtc-instantly-campaign-outbound-audit/v1",
        "generated_at": generated_at,
        "mode": "read_only",
        "provider_operations": ["GET Airtable records", "GET Instantly campaigns", "POST Instantly /leads/list"],
        "external_writes": 0,
        "identity_policy": {
            "primary": "normalized_email+campaign_id",
            "fallback": "unique_normalized_email",
            "ambiguous_matches": "unmatched_fail_closed",
        },
        "summary": {
            "total_instantly_leads_inspected": len(all_leads),
            "contacted": contacted,
            "unsent": unsent,
            "matched": matched,
            "unmatched_total": len(unmatched),
            "unmatched_unsent": len(unsent_unmatched),
            "unsent_company_corrections": company_corrections,
            "unsent_role_corrections": role_corrections,
            "safe_update_set": len(safe_updates),
            "held_or_ambiguous_unsent": len(held_unsent),
            "active_or_running_campaigns": len(active_campaigns),
        },
        "lead_status_counts": dict(sorted(lead_status_counts.items())),
        "campaigns": campaigns,
        "race_assessment": {
            "active_campaigns": [{"id": row["id"], "name": row["name"], "status": row["status_label"]} for row in active_campaigns],
            "temporary_pause_required": bool(active_campaigns and safe_updates),
            "reason": "Any active/running campaign can contact a currently-unsent lead between audit and patch.",
        },
        "representative_examples": representatives,
        "safe_updates": safe_updates,
        "held_unsent": held_unsent,
        "unmatched": unmatched,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/instantly_campaign_outbound_audit_20260817.json")
    args = parser.parse_args(argv)
    result = audit()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output.resolve()),
        "summary": result["summary"],
        "campaigns": [
            {"name": row["name"], "status": row["status_label"], "counts": row["counts"]}
            for row in result["campaigns"]
        ],
        "race_assessment": result["race_assessment"],
        "representative_examples": result["representative_examples"],
        "external_writes": 0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
