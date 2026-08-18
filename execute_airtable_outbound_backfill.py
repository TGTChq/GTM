"""Controlled Airtable schema creation and outbound-display backfill.

This utility never calls an Instantly mutation endpoint and never changes an
Airtable lifecycle or canonical field.  Execution is fail-closed against the
reviewed manifest and a fresh contact-level Instantly reconciliation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from audit_outbound_displays import _fetch_status_records
from audit_queued_instantly import audit_records as audit_instantly_records
from prepare_outbound_backfill import (
    ALLOWED_STATUSES,
    PATCH_FIELDS,
    SIGNED_FIELDS,
    VALIDATION_VERSION,
    _fingerprint,
    _resolver_cache,
    apply_instantly_overlap_guards,
    build_record_patch,
    prepare_records,
)


SCHEMA_FIELDS: tuple[Dict[str, Any], ...] = (
    {"name": "Outbound Company", "type": "singleLineText"},
    {"name": "Outbound Company Confidence", "type": "singleSelect", "options": {"choices": [{"name": "high"}, {"name": "medium"}, {"name": "low"}]}},
    {"name": "Outbound Company Identity", "type": "singleLineText"},
    {"name": "Outbound Company Evidence", "type": "multilineText"},
    {"name": "Outbound Hold", "type": "checkbox", "options": {"icon": "check", "color": "greenBright"}},
    {"name": "Outbound Role", "type": "singleLineText"},
    {"name": "Outbound Roles", "type": "multilineText"},
    {"name": "Outbound Role Confidence", "type": "singleSelect", "options": {"choices": [{"name": "high"}, {"name": "medium"}, {"name": "low"}]}},
    {"name": "Outbound Role Evidence", "type": "multilineText"},
)
SCHEMA_NAMES = tuple(field["name"] for field in SCHEMA_FIELDS)
EXPECTED_SEND_SAFE = 519
EXPECTED_HELD_PATCH = 43
EXPECTED_SENT_PROTECTED = 9
MATERIAL_MANIFEST_FIELDS = (
    "lifecycle_status",
    "current_company",
    "current_open_role",
    "proposed_outbound_company",
    "proposed_outbound_role",
    "company_confidence",
    "role_confidence",
    "outbound_hold",
    "safe_backfill",
    "company_identity_key",
    "proposed_validation_fingerprint",
    "instantly_overlap_state",
    "backfill_eligibility",
)
CANONICAL_GUARD_FIELDS = (
    "Lead Key", "Company", "Website", "Open Role", "Open Roles", "Matched Role", "Role Bucket",
    "Job URL", "Job URL Source", "Job ID", "Location", "Employment Type",
    "Hiring Manager", "HM Title", "LinkedIn", "Apollo Person ID", "Email",
    "Campaign ID", "Employees", "Size Band", "Final Decision", "Status",
    "Firmographics Status", "Contact Alignment", "Email Validation",
)


def _env() -> tuple[str, str, str]:
    token = str(os.getenv("AIRTABLE_TOKEN") or "").strip()
    base_id = str(os.getenv("AIRTABLE_BASE_ID") or "").strip()
    table = str(os.getenv("AIRTABLE_TABLE_NAME") or "Leads").strip()
    if not token or not base_id or not table:
        raise ValueError("AIRTABLE_TOKEN, AIRTABLE_BASE_ID, and AIRTABLE_TABLE_NAME are required")
    return token, base_id, table


def _json_request(url: str, *, method: str = "GET", body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    token, _, _ = _env()
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    request = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "TGTC-controlled-airtable-backfill/1.0",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=45) as response:  # noqa: S310 - fixed provider hosts
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc


def _base_schema() -> Dict[str, Any]:
    _, base_id, _ = _env()
    return _json_request(f"https://api.airtable.com/v0/meta/bases/{base_id}/tables")


def _table_schema(payload: Dict[str, Any]) -> Dict[str, Any]:
    _, _, table = _env()
    matches = [item for item in payload.get("tables") or [] if item.get("name") == table]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one Airtable table named {table!r}; found {len(matches)}")
    return matches[0]


def _verify_schema(table: Dict[str, Any]) -> list[str]:
    by_name = {str(field.get("name")): field for field in table.get("fields") or []}
    errors: list[str] = []
    for expected in SCHEMA_FIELDS:
        actual = by_name.get(expected["name"])
        if not actual:
            errors.append(f"missing:{expected['name']}")
            continue
        if actual.get("type") != expected["type"]:
            errors.append(f"type:{expected['name']}:{actual.get('type')}!={expected['type']}")
        if expected["type"] == "singleSelect":
            choices = [str(choice.get("name")) for choice in (actual.get("options") or {}).get("choices") or []]
            if choices != ["high", "medium", "low"]:
                errors.append(f"choices:{expected['name']}:{choices}")
    return errors


def create_schema() -> Dict[str, Any]:
    table = _table_schema(_base_schema())
    existing = {str(field.get("name")): field for field in table.get("fields") or []}
    created: list[Dict[str, Any]] = []
    already_present: list[str] = []
    _, base_id, _ = _env()
    table_id = str(table.get("id") or "")
    for field in SCHEMA_FIELDS:
        name = field["name"]
        if name in existing:
            already_present.append(name)
            continue
        result = _json_request(
            f"https://api.airtable.com/v0/meta/bases/{base_id}/tables/{table_id}/fields",
            method="POST",
            body=field,
        )
        created.append({"name": result.get("name"), "id": result.get("id"), "type": result.get("type")})
        time.sleep(0.25)
    verified_table = _table_schema(_base_schema())
    errors = _verify_schema(verified_table)
    if errors:
        raise RuntimeError("Airtable outbound schema verification failed: " + "; ".join(errors))
    return {
        "created": created,
        "already_present": already_present,
        "verified_names": list(SCHEMA_NAMES),
        "created_count": len(created),
        "additional_fields_created": 0,
    }


def _queued_records() -> list[Dict[str, Any]]:
    records: list[Dict[str, Any]] = []
    for status in ALLOWED_STATUSES:
        records.extend(_fetch_status_records(status))
    return records


def _rows_by_id(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    columns = manifest.get("columns") or []
    return {str(row[0]): dict(zip(columns, row)) for row in manifest.get("rows") or []}


def compare_manifests(reviewed: Dict[str, Any], fresh: Dict[str, Any]) -> list[Dict[str, Any]]:
    reviewed_rows = _rows_by_id(reviewed)
    fresh_rows = _rows_by_id(fresh)
    differences: list[Dict[str, Any]] = []
    for record_id in sorted(set(reviewed_rows) | set(fresh_rows)):
        if record_id not in reviewed_rows:
            differences.append({"record_id": record_id, "reason": "new_record"})
            continue
        if record_id not in fresh_rows:
            differences.append({"record_id": record_id, "reason": "missing_record"})
            continue
        changed = [
            field for field in MATERIAL_MANIFEST_FIELDS
            if reviewed_rows[record_id].get(field) != fresh_rows[record_id].get(field)
        ]
        if changed:
            differences.append({"record_id": record_id, "reason": "material_change", "fields": changed})
    return differences


def fresh_reconciliation(reviewed_path: str | Path) -> tuple[Dict[str, Any], list[Dict[str, Any]], Dict[str, Any]]:
    reviewed = json.loads(Path(reviewed_path).read_text(encoding="utf-8"))
    generated_at = str(reviewed.get("generated_at") or "")
    signing_key = str(os.getenv("VALIDATION_SIGNING_KEY") or "")
    if not generated_at or not signing_key:
        raise ValueError("Reviewed generated_at and VALIDATION_SIGNING_KEY are required")
    records = _queued_records()
    raw = prepare_records(records, signing_key=signing_key, generated_at=generated_at)
    instantly_audit = audit_instantly_records(records)
    fresh = apply_instantly_overlap_guards(raw, instantly_audit)
    differences = compare_manifests(reviewed, fresh)
    return {
        "passes": not differences,
        "reviewed_records": len(reviewed.get("rows") or []),
        "fresh_records": len(fresh.get("rows") or []),
        "differences_count": len(differences),
        "differences": differences[:50],
        "fresh_summary": fresh.get("summary") or {},
        "instantly": {
            key: instantly_audit.get(key) for key in (
                "airtable_queued_records", "airtable_contacts_queried", "instantly_matching_leads",
                "airtable_instantly_associations", "sent_or_processed_protected",
                "unsent_update_candidates", "held_overlap",
            )
        },
    }, records, fresh


def _selection(fresh: Dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    rows = _rows_by_id(fresh)
    send_safe = sorted(record_id for record_id, row in rows.items() if row.get("backfill_eligibility") == "eligible")
    held = sorted(record_id for record_id, row in rows.items() if row.get("backfill_eligibility") == "held")
    protected = sorted(record_id for record_id, row in rows.items() if "instantly_sent_or_processed" in str(row.get("backfill_eligibility")))
    return send_safe, held, protected


def _patch_batch(batch: list[Dict[str, Any]]) -> list[str]:
    _, base_id, table = _env()
    url = f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            payload = _json_request(url, method="PATCH", body={"records": batch, "typecast": False})
            return [str(record.get("id") or "") for record in payload.get("records") or []]
        except RuntimeError as exc:
            last_error = exc
            if attempt == 4 or not any(code in str(exc) for code in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")):
                break
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"Airtable batch patch failed after retries: {last_error}")


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def execute(reviewed_path: str | Path) -> Dict[str, Any]:
    reconciliation, records, fresh = fresh_reconciliation(reviewed_path)
    if not reconciliation["passes"]:
        return {"aborted": True, "reason": "reviewed_manifest_stale", "reconciliation": reconciliation, "external_writes": 0}
    send_safe, held, protected = _selection(fresh)
    if (len(send_safe), len(held), len(protected)) != (EXPECTED_SEND_SAFE, EXPECTED_HELD_PATCH, EXPECTED_SENT_PROTECTED):
        return {
            "aborted": True,
            "reason": "reviewed_partition_count_mismatch",
            "counts": {"send_safe": len(send_safe), "held": len(held), "protected": len(protected)},
            "external_writes": 0,
        }

    selected = set(send_safe) | set(held)
    records_by_id = {str(record.get("id") or ""): record for record in records}
    execution_at = datetime.now(timezone.utc).isoformat()
    signing_key = str(os.getenv("VALIDATION_SIGNING_KEY") or "")
    cache = _resolver_cache()
    expected_patches: Dict[str, Dict[str, Any]] = {}
    canonical_before: Dict[str, Dict[str, Any]] = {}
    batches: list[Dict[str, Any]] = []
    for record_id in sorted(selected):
        record = records_by_id[record_id]
        patch, meta = build_record_patch(
            record,
            signing_key=signing_key,
            generated_at=execution_at,
            cache=cache,
        )
        if record_id in send_safe and (meta["hold"] or not meta["safe"]):
            raise RuntimeError(f"Send-safe partition became unsafe: {record_id}")
        if record_id in held and not meta["hold"]:
            raise RuntimeError(f"Held partition lost hold: {record_id}")
        if set(patch) != set(PATCH_FIELDS):
            raise RuntimeError(f"Unexpected patch field set for {record_id}")
        expected_patches[record_id] = patch
        original = record.get("fields") or {}
        canonical_before[record_id] = {field: original.get(field) for field in CANONICAL_GUARD_FIELDS}
        batches.append({"id": record_id, "fields": patch})

    patched_ids: set[str] = set()
    failures: list[Dict[str, Any]] = []
    for batch in _chunks(batches, 10):
        try:
            returned = _patch_batch(batch)
            patched_ids.update(returned)
            missing = sorted({item["id"] for item in batch} - set(returned))
            if missing:
                failures.extend({"record_id": record_id, "reason": "missing_from_patch_response"} for record_id in missing)
        except Exception as exc:  # noqa: BLE001 - capture partial execution precisely
            failures.extend({"record_id": item["id"], "reason": str(exc)[:500]} for item in batch)
            break
        time.sleep(0.25)

    readback_records = _queued_records()
    readback_by_id = {str(record.get("id") or ""): record for record in readback_records}
    verification_failures: list[Dict[str, Any]] = []
    verified = 0
    for record_id in sorted(patched_ids):
        record = readback_by_id.get(record_id)
        if not record:
            verification_failures.append({"record_id": record_id, "reason": "missing_or_lifecycle_changed_on_readback"})
            continue
        fields = record.get("fields") or {}
        expected = expected_patches[record_id]
        mismatched = []
        for field, value in expected.items():
            actual = fields.get(field)
            if field == "Outbound Hold":
                actual = bool(actual)
            if actual != value:
                mismatched.append(field)
        drift = [
            field for field, value in canonical_before[record_id].items()
            if fields.get(field) != value
        ]
        supplied = str(fields.get("Validation Fingerprint") or "")
        fingerprint_ok = bool(supplied and supplied == _fingerprint(fields, signing_key))
        if mismatched or drift or not fingerprint_ok or fields.get("Validation Version") != VALIDATION_VERSION:
            verification_failures.append({
                "record_id": record_id,
                "reason": "readback_mismatch",
                "outbound_fields": mismatched,
                "canonical_drift": drift,
                "fingerprint_ok": fingerprint_ok,
            })
        else:
            verified += 1

    return {
        "aborted": False,
        "reconciliation": reconciliation,
        "execution_at": execution_at,
        "partition": {"send_safe": len(send_safe), "held": len(held), "sent_processed_protected": len(protected)},
        "rows_selected": len(selected),
        "rows_patched_successfully": len(patched_ids),
        "rows_patch_failed": len(failures),
        "patch_failures": failures,
        "readback_verified": verified,
        "readback_failed": len(verification_failures),
        "readback_failures": verification_failures,
        "canonical_field_drift": sum(bool(item.get("canonical_drift")) for item in verification_failures),
        "validation_version": VALIDATION_VERSION,
        "external_writes": len(patched_ids),
        "instantly_writes": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--create-schema", action="store_true")
    mode.add_argument("--reconcile-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--manifest", default="reports/outbound_display_backfill_manifest_20260817.json")
    args = parser.parse_args(argv)
    if args.create_schema:
        result = {"mode": "schema", **create_schema()}
    elif args.reconcile_only:
        reconciliation, _, _ = fresh_reconciliation(args.manifest)
        result = {"mode": "reconcile_only", **reconciliation, "external_writes": 0}
    else:
        result = {"mode": "execute", **execute(args.manifest)}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.reconcile_only and not result.get("passes"):
        return 2
    if args.execute and (result.get("aborted") or result.get("rows_patch_failed") or result.get("readback_failed")):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
