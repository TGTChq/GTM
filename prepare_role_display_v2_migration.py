"""Prepare exact read-only ``role-display/2`` migration manifests.

Provider operations are limited to Airtable GET, Instantly campaign GET, and
Instantly's documented read-only POST ``/leads/list``. The generated manifests
contain exact proposed patches but this module has no provider write methods.
Contact names and email addresses are intentionally omitted.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import config
from audit_instantly_campaign_outbound import (
    LEAD_STATUS,
    _airtable_records,
    _campaigns,
    _email,
    _list_campaign_leads,
    _match_record,
    _record_campaign_id,
    _text,
)
from extract_outbound_role_audit_population import _record_context
from role_display_resolver import RESOLVER_VERSION, resolve_role_display
from validation_integrity import fingerprint_payload


QUEUED_STATUSES = {"Pending", "Error"}
ROLE_PATCH_CANDIDATES = (
    "Outbound Role",
    "Outbound Roles",
    "Outbound Role Confidence",
    "Outbound Role Evidence",
    "Outbound Hold",
    "Validation Version",
    "Validated At",
    "Validation Fingerprint",
)
PROTECTED_CANONICAL_FIELDS = (
    "Company",
    "Website",
    "Open Role",
    "Open Roles",
    "Matched Role",
    "Role Bucket",
    "Role Focus",
    "Domain",
    "Hiring Manager",
    "HM Title",
    "LinkedIn",
    "Apollo Person ID",
    "Email",
    "Lead Key",
    "Campaign ID",
    "CRM Exclusion",
    "Final Decision",
    "Status",
    "Error",
)


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(fields: Dict[str, Any], signing_key: str) -> str:
    serialized = _compact_json(fingerprint_payload(fields))
    return hmac.new(signing_key.encode(), serialized.encode(), hashlib.sha256).hexdigest()


def _canonical_guard_hash(fields: Dict[str, Any]) -> str:
    payload = {key: fields.get(key) for key in PROTECTED_CANONICAL_FIELDS}
    return hashlib.sha256(_compact_json(payload).encode()).hexdigest()


def _role_input(record: Dict[str, Any]) -> Dict[str, Any]:
    context = _record_context(record)
    return {
        "job_title": context.get("raw_title"),
        "canonical_job_title": context.get("canonical_title"),
        "_matched_role": context.get("matched_role"),
        "role_focus": context.get("role_focus"),
        "job_location": context.get("location"),
        "canonical_location": context.get("location"),
        "canonical_employment_type": context.get("employment_type"),
        "job_employment_type": context.get("employment_type"),
        "work_arrangement": context.get("work_arrangement"),
        "job_is_remote": context.get("job_is_remote"),
        "seniority": context.get("seniority"),
        "salary": context.get("salary"),
        "salary_min": context.get("salary_min"),
        "salary_max": context.get("salary_max"),
    }


def _same_field(field: str, current: Any, proposed: Any) -> bool:
    if field == "Outbound Hold":
        return (current is True) == (proposed is True)
    return current == proposed


def build_airtable_patch(
    record: Dict[str, Any],
    *,
    generated_at: str,
    signing_key: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return an exact role-only/fingerprint patch and non-PII audit metadata."""
    if not signing_key:
        raise ValueError("VALIDATION_SIGNING_KEY is required")
    fields = dict(record.get("fields") or {})
    role_result = resolve_role_display(_role_input(record))
    existing_hold = fields.get("Outbound Hold") is True
    combined_hold = bool(existing_hold or role_result.hold)
    proposed_values = {
        "Outbound Role": role_result.name,
        # Existing Open Roles cannot be safely split because its delimiter may
        # occur inside a title. The reviewed primary display is deterministic.
        "Outbound Roles": role_result.name,
        "Outbound Role Confidence": role_result.confidence,
        "Outbound Role Evidence": _compact_json(role_result.evidence),
        "Outbound Hold": combined_hold,
        "Validation Version": config.VALIDATION_VERSION,
        "Validated At": generated_at,
    }
    signed = dict(fields)
    signed.update(proposed_values)
    proposed_values["Validation Fingerprint"] = _fingerprint(signed, signing_key)
    patch = {
        field: proposed_values[field]
        for field in ROLE_PATCH_CANDIDATES
        if not _same_field(field, fields.get(field), proposed_values[field])
    }
    if set(patch) - set(ROLE_PATCH_CANDIDATES):
        raise AssertionError("Role migration produced an out-of-contract Airtable field")
    return patch, {
        "airtable_record_id": record.get("id"),
        "lifecycle_status": _text(fields.get("Status")),
        "campaign_id": _record_campaign_id(fields),
        "current_open_role": fields.get("Open Role"),
        "current_outbound_role": fields.get("Outbound Role"),
        "current_outbound_roles": fields.get("Outbound Roles"),
        "proposed_outbound_role": role_result.name,
        "proposed_outbound_roles": role_result.name,
        "role_confidence": role_result.confidence,
        "role_status": role_result.evidence.get("status"),
        "role_hold": role_result.hold,
        "current_hold": existing_hold,
        "proposed_hold": combined_hold,
        "role_value_changed": (
            _text(fields.get("Outbound Role")) != role_result.name
            or _text(fields.get("Outbound Roles")) != role_result.name
        ),
        "canonical_guard_hash": _canonical_guard_hash(fields),
        "resolver_version": RESOLVER_VERSION,
        "patch_fields": list(patch),
        "patch": patch,
    }


def _lead_payload(lead: Dict[str, Any]) -> Dict[str, Any]:
    payload = lead.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _lead_status(lead: Dict[str, Any]) -> str:
    value = int(lead.get("status") or 0)
    return LEAD_STATUS.get(value, f"unknown:{value}")


def _instantly_row(
    lead: Dict[str, Any],
    record: Dict[str, Any],
    campaign: Dict[str, Any],
    *,
    match_method: str,
) -> tuple[str, Dict[str, Any]]:
    role_result = resolve_role_display(_role_input(record))
    payload = _lead_payload(lead)
    current_role = _text(payload.get("open_role"))
    current_roles = _text(payload.get("open_roles"))
    common = {
        "instantly_lead_id": _text(lead.get("id")),
        "airtable_record_id": record.get("id"),
        "airtable_lifecycle_status": _text((record.get("fields") or {}).get("Status")),
        "campaign_id": _text(lead.get("campaign")),
        "campaign_name": campaign.get("name"),
        "lead_status": _lead_status(lead),
        "identity_match": match_method,
        "timestamp_last_contact_precondition": "null_or_absent",
        "current_open_role": current_role,
        "current_open_roles": current_roles,
        "proposed_open_role": role_result.name,
        "proposed_open_roles": role_result.name,
        "role_confidence": role_result.confidence,
        "role_status": role_result.evidence.get("status"),
        "resolver_version": RESOLVER_VERSION,
    }
    if role_result.hold:
        return "hold", {
            **common,
            "required_action": "move_to_existing_dedicated_hold_list_before_any_campaign_resumes",
            "display_patch": {},
            "hold_reasons": role_result.evidence.get("rules") or [],
        }
    fields = []
    if current_role != role_result.name:
        fields.append("custom_variables.open_role")
    if current_roles != role_result.name:
        fields.append("custom_variables.open_roles")
    return "safe", {
        **common,
        "changed_fields": fields,
        "merge_existing_custom_variables": True,
        "preserve_unrelated_custom_variables": True,
        "patch_required": bool(fields),
    }


def build_manifests(
    airtable_records: Iterable[Dict[str, Any]],
    campaigns: Iterable[Dict[str, Any]],
    campaign_leads: Dict[str, list[Dict[str, Any]]],
    *,
    generated_at: str,
    signing_key: str,
) -> Dict[str, Any]:
    records = list(airtable_records)
    campaign_rows = list(campaigns)
    campaigns_by_id = {_text(row.get("id")): row for row in campaign_rows}
    by_email: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        contact = _email((record.get("fields") or {}).get("Email"))
        if contact:
            by_email[contact].append(record)

    contacted_record_ids: set[str] = set()
    unsent_matches: list[tuple[Dict[str, Any], Dict[str, Any], str]] = []
    unmatched: list[Dict[str, Any]] = []
    total_instantly = contacted_count = unsent_count = 0
    seen_lead_ids: set[str] = set()
    for campaign_id, leads in campaign_leads.items():
        campaign = campaigns_by_id[campaign_id]
        for lead in leads:
            total_instantly += 1
            lead_id = _text(lead.get("id"))
            if not lead_id or lead_id in seen_lead_ids:
                raise RuntimeError(f"Missing or duplicate Instantly lead ID: {lead_id!r}")
            seen_lead_ids.add(lead_id)
            record, match_method, warning = _match_record(lead, by_email)
            contacted = bool(lead.get("timestamp_last_contact"))
            contacted_count += int(contacted)
            unsent_count += int(not contacted)
            if not record or match_method != "normalized_email+campaign" or warning:
                possible_records = by_email.get(_email(lead.get("email"))) or []
                possible_record_ids = sorted({
                    _text(candidate.get("id")) for candidate in possible_records
                    if _text(candidate.get("id"))
                })
                if contacted:
                    # Fail closed: if a contacted identity is ambiguous, protect
                    # every Airtable candidate rather than risk patching one.
                    contacted_record_ids.update(possible_record_ids)
                unmatched.append({
                    "instantly_lead_id": lead_id,
                    "campaign_id": campaign_id,
                    "campaign_name": campaign.get("name"),
                    "contact_state": "contacted" if contacted else "unsent",
                    "reason": warning or f"unsafe_identity_match:{match_method}",
                    "possible_airtable_record_ids": possible_record_ids,
                })
                continue
            record_id = _text(record.get("id"))
            if contacted:
                contacted_record_ids.add(record_id)
            else:
                unsent_matches.append((lead, record, match_method))

    airtable_safe: list[Dict[str, Any]] = []
    airtable_holds: list[Dict[str, Any]] = []
    protected_contacted: list[Dict[str, Any]] = []
    queued_count = 0
    for record in records:
        fields = record.get("fields") or {}
        status = _text(fields.get("Status"))
        if status not in QUEUED_STATUSES:
            continue
        queued_count += 1
        record_id = _text(record.get("id"))
        if record_id in contacted_record_ids:
            protected_contacted.append({
                "airtable_record_id": record_id,
                "lifecycle_status": status,
                "reason": "linked_to_contacted_instantly_lead",
            })
            continue
        patch, metadata = build_airtable_patch(
            record, generated_at=generated_at, signing_key=signing_key
        )
        target = airtable_holds if metadata["role_hold"] else airtable_safe
        target.append(metadata)

    instantly_safe: list[Dict[str, Any]] = []
    instantly_holds: list[Dict[str, Any]] = []
    for lead, record, match_method in unsent_matches:
        campaign_id = _text(lead.get("campaign"))
        kind, row = _instantly_row(
            lead,
            record,
            campaigns_by_id[campaign_id],
            match_method=match_method,
        )
        (instantly_holds if kind == "hold" else instantly_safe).append(row)

    safe_updates = [row for row in instantly_safe if row["patch_required"]]
    safe_unchanged = [row for row in instantly_safe if not row["patch_required"]]
    instantly_by_campaign = Counter(row["campaign_name"] for row in safe_updates)
    summary = {
        "schema": "tgtc-role-display-v2-migration-summary/v1",
        "generated_at": generated_at,
        "mode": "dry_run_read_only",
        "resolver_version": RESOLVER_VERSION,
        "validation_version": config.VALIDATION_VERSION,
        "provider_operations": [
            "GET Airtable records",
            "GET Instantly campaigns",
            "POST Instantly /leads/list (read-only)",
        ],
        "external_writes": 0,
        "campaign_count": len(campaign_rows),
        "airtable_total": len(records),
        "airtable_queued_inspected": queued_count,
        "airtable_safe_role_updates": len(airtable_safe),
        "airtable_safe_role_value_changes": sum(row["role_value_changed"] for row in airtable_safe),
        "airtable_safe_role_evidence_or_version_only": sum(
            not row["role_value_changed"] for row in airtable_safe
        ),
        "airtable_send_safe_after_patch": sum(not row["proposed_hold"] for row in airtable_safe),
        "airtable_role_safe_but_existing_company_hold": sum(
            row["proposed_hold"] for row in airtable_safe
        ),
        "airtable_role_holds": len(airtable_holds),
        "airtable_new_role_holds": sum(not row["current_hold"] for row in airtable_holds),
        "airtable_role_holds_already_held": sum(row["current_hold"] for row in airtable_holds),
        "airtable_contacted_protected": len(protected_contacted),
        "instantly_total_inspected": total_instantly,
        "instantly_contacted": contacted_count,
        "instantly_unsent": unsent_count,
        "instantly_unsent_safe_updates": len(safe_updates),
        "instantly_open_role_corrections": sum(
            "custom_variables.open_role" in row["changed_fields"] for row in safe_updates
        ),
        "instantly_open_roles_corrections": sum(
            "custom_variables.open_roles" in row["changed_fields"] for row in safe_updates
        ),
        "instantly_safe_updates_by_campaign": dict(sorted(instantly_by_campaign.items())),
        "instantly_unsent_safe_unchanged": len(safe_unchanged),
        "instantly_unsent_ambiguous": len(instantly_holds),
        "unmatched_or_identity_risk_count": len(unmatched),
    }
    return {
        "summary": summary,
        "campaigns": campaign_rows,
        "airtable_safe": airtable_safe,
        "airtable_holds": airtable_holds,
        "instantly_safe_updates": safe_updates,
        "instantly_safe_unchanged": safe_unchanged,
        "instantly_holds": instantly_holds,
        "protected_contacted": protected_contacted,
        "unmatched_or_identity_risk": unmatched,
    }


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(prefix: Path) -> Dict[str, Any]:
    signing_key = _text(os.getenv("VALIDATION_SIGNING_KEY") or config.VALIDATION_SIGNING_KEY)
    if not signing_key:
        raise ValueError("VALIDATION_SIGNING_KEY is required")
    generated_at = datetime.now(timezone.utc).isoformat()
    records = _airtable_records()
    campaigns = _campaigns()
    campaign_leads = {
        _text(campaign.get("id")): _list_campaign_leads(_text(campaign.get("id")))
        for campaign in campaigns
    }
    result = build_manifests(
        records,
        campaigns,
        campaign_leads,
        generated_at=generated_at,
        signing_key=signing_key,
    )
    paths = {
        "summary": prefix.with_name(prefix.name + "_summary.json"),
        "airtable_safe": prefix.with_name(prefix.name + "_airtable_safe_updates.json"),
        "airtable_holds": prefix.with_name(prefix.name + "_airtable_role_holds.json"),
        "instantly_safe": prefix.with_name(prefix.name + "_instantly_safe_updates.json"),
        "instantly_holds": prefix.with_name(prefix.name + "_instantly_role_holds.json"),
    }
    summary_payload = {
        **result["summary"],
        "manifest_paths": {key: str(path.resolve()) for key, path in paths.items() if key != "summary"},
        "protected_contacted": result["protected_contacted"],
        "unmatched_or_identity_risk_rows": result["unmatched_or_identity_risk"],
        "campaigns": result["campaigns"],
    }
    _write(paths["summary"], summary_payload)
    _write(paths["airtable_safe"], {
        "summary": result["summary"],
        "allowed_patch_fields": list(ROLE_PATCH_CANDIDATES),
        "protected_canonical_fields": list(PROTECTED_CANONICAL_FIELDS),
        "rows": result["airtable_safe"],
    })
    _write(paths["airtable_holds"], {
        "summary": result["summary"],
        "allowed_patch_fields": list(ROLE_PATCH_CANDIDATES),
        "protected_canonical_fields": list(PROTECTED_CANONICAL_FIELDS),
        "rows": result["airtable_holds"],
    })
    _write(paths["instantly_safe"], {
        "summary": result["summary"],
        "allowed_patch_fields": ["custom_variables.open_role", "custom_variables.open_roles"],
        "rows": result["instantly_safe_updates"],
    })
    _write(paths["instantly_holds"], {
        "summary": result["summary"],
        "display_values_must_not_be_patched": True,
        "rows": result["instantly_holds"],
    })
    return summary_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        default="reports/role_display_v2_dry_run_20260817",
        help="Output filename prefix; five JSON files are generated",
    )
    args = parser.parse_args(argv)
    result = run(Path(args.prefix))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
