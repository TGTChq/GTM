"""Build a read-only Airtable queued-record outbound-display backfill manifest.

The command issues Airtable GET requests only and writes no external state.  It
is deliberately limited to Pending/Error lifecycle rows.  The emitted manifest
contains no contact names or email addresses.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from audit_outbound_displays import _fetch_status_records
from company_display_resolver import CompanyDisplayCache, resolve_company_display
from role_display_resolver import resolve_role_display
from validation_integrity import fingerprint_payload


ALLOWED_STATUSES = ("Pending", "Error")
VALIDATION_VERSION = "tgtc-ready-v1.4.7-role-display-2"
PATCH_FIELD_SET = "outbound_display_and_validation_v1"
PATCH_FIELDS = (
    "Outbound Company",
    "Outbound Company Confidence",
    "Outbound Company Identity",
    "Outbound Company Evidence",
    "Outbound Hold",
    "Outbound Role",
    "Outbound Roles",
    "Outbound Role Confidence",
    "Outbound Role Evidence",
    "Validation Version",
    "Validated At",
    "Validation Fingerprint",
)
SIGNED_FIELDS = (
    "Company", "Website", "Open Role", "Open Roles", "Role Focus",
    "Outbound Company", "Outbound Company Confidence", "Outbound Company Identity",
    "Outbound Company Evidence", "Outbound Hold",
    "Outbound Role", "Outbound Roles", "Outbound Role Confidence", "Outbound Role Evidence",
    "Matched Role", "Role Bucket", "Campaign ID", "Employees",
    "Job URL", "Job URL Status", "Job URL Source", "Job ID",
    "Location", "Employment Type",
    "Hiring Manager", "HM Title", "LinkedIn", "Apollo Person ID", "Email",
    "Final Decision", "Validation Version", "Validated At",
)
PROTECTED_CANONICAL_FIELDS = frozenset({
    "Company", "Website", "Open Role", "Open Roles", "Matched Role", "Role Bucket",
    "Domain", "Hiring Manager", "HM Title", "LinkedIn", "Apollo Person ID", "Email",
    "Lead Key", "Campaign ID", "CRM Exclusion", "Final Decision", "Status", "Error",
})

assert not (set(PATCH_FIELDS) & PROTECTED_CANONICAL_FIELDS)


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(fields: Dict[str, Any], signing_key: str) -> str:
    payload = fingerprint_payload(fields)
    serialized = _compact_json(payload)
    return hmac.new(signing_key.encode(), serialized.encode(), hashlib.sha256).hexdigest()


def _resolver_cache() -> CompanyDisplayCache:
    root = Path(__file__).resolve().parent
    cache_path = os.getenv(
        "OUTBOUND_COMPANY_CACHE_PATH",
        str(root / "data" / "state" / "company_display_cache.json"),
    )
    overrides_path = os.getenv(
        "OUTBOUND_COMPANY_OVERRIDES_PATH",
        str(root / "company_display_overrides.json"),
    )
    return CompanyDisplayCache(cache_path, overrides_path=overrides_path)


def build_record_patch(
    record: Dict[str, Any],
    *,
    signing_key: str,
    generated_at: str,
    cache: CompanyDisplayCache | None = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Resolve one record and return its exact outbound-only patch plus metadata."""
    if not signing_key:
        raise ValueError("VALIDATION_SIGNING_KEY is required to prepare exact fingerprints")
    fields = dict(record.get("fields") or {})
    status = str(fields.get("Status") or "").strip()
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Refusing out-of-scope lifecycle status: {status or '<missing>'}")
    company = str(fields.get("Company") or "").strip()
    current_role = str(fields.get("Open Role") or "").strip()
    prior_identity = str(fields.get("Outbound Company Identity") or "").strip()
    linkedin_slug = prior_identity.split(":", 1)[1] if prior_identity.startswith("linkedin:") else ""
    company_result = resolve_company_display(
        organization=company,
        canonical_company_name=company,
        org_linkedin_slug=linkedin_slug,
        employer_domain=str(fields.get("Website") or ""),
        canonical_identity_verified=str(fields.get("Firmographics Status") or "").upper() == "PASS",
        cache=cache,
        persist=False,
    )
    role_result = resolve_role_display({
        "job_title": current_role,
        "canonical_job_title": current_role,
        "_matched_role": fields.get("Matched Role"),
        "role_focus": fields.get("Role Focus"),
        "job_location": fields.get("Location"),
        "canonical_employment_type": fields.get("Employment Type"),
        "job_employment_type": fields.get("Employment Type"),
    })
    hold = bool(
        company_result.hold
        or not company_result.identity_safe
        or role_result.hold
    )
    safe = bool(
        not hold
        and company_result.confidence in {"high", "medium"}
        and company_result.identity_safe
    )
    proposed = {
        "Outbound Company": company_result.name,
        "Outbound Company Confidence": company_result.confidence,
        "Outbound Company Identity": company_result.identity_key,
        "Outbound Company Evidence": _compact_json(company_result.evidence),
        "Outbound Hold": hold,
        "Outbound Role": role_result.name,
        # Existing Open Roles uses a delimiter that can also be meaningful
        # inside a title. Backfill the reviewed primary display only.
        "Outbound Roles": role_result.name,
        "Outbound Role Confidence": role_result.confidence,
        "Outbound Role Evidence": _compact_json(role_result.evidence),
        "Validation Version": VALIDATION_VERSION,
        "Validated At": generated_at,
    }
    signed = dict(fields)
    signed.update(proposed)
    proposed["Validation Fingerprint"] = _fingerprint(signed, signing_key)
    unexpected = set(proposed) - set(PATCH_FIELDS)
    if unexpected:
        raise AssertionError(f"Unexpected patch fields: {sorted(unexpected)}")
    return proposed, {
        "record_id": record.get("id"),
        "status": status,
        "company": company,
        "current_role": current_role,
        "company_name": company_result.name,
        "role_name": role_result.name,
        "company_confidence": company_result.confidence,
        "role_confidence": role_result.confidence,
        "role_hold": role_result.hold,
        "hold": hold,
        "safe": safe,
        "identity_key": company_result.identity_key,
    }


def prepare_records(
    records: Iterable[Dict[str, Any]],
    *,
    signing_key: str,
    generated_at: str | None = None,
) -> Dict[str, Any]:
    if not signing_key:
        raise ValueError("VALIDATION_SIGNING_KEY is required to prepare exact fingerprints")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    cache = _resolver_cache()
    rows: list[list[Any]] = []
    safe_count = held_count = 0
    status_counts = {status: 0 for status in ALLOWED_STATUSES}

    for record in records:
        proposed, meta = build_record_patch(
            record,
            signing_key=signing_key,
            generated_at=generated_at,
            cache=cache,
        )
        status_counts[meta["status"]] += 1
        safe_count += int(meta["safe"])
        held_count += int(meta["hold"])
        rows.append([
            meta["record_id"],
            meta["status"],
            meta["company"],
            meta["current_role"],
            meta["company_name"],
            meta["role_name"],
            meta["company_confidence"],
            meta["role_confidence"],
            meta["hold"],
            meta["safe"],
            meta["identity_key"],
            proposed["Validation Fingerprint"],
            PATCH_FIELD_SET,
        ])

    return {
        "schema": "tgtc-outbound-display-backfill-manifest/v1",
        "mode": "dry_run_get_only",
        "generated_at": generated_at,
        "scope": {"included_statuses": list(ALLOWED_STATUSES), "excluded_statuses": ["Approved", "Enrolled", "Rejected"]},
        "validation_version": VALIDATION_VERSION,
        "patch_field_sets": {PATCH_FIELD_SET: list(PATCH_FIELDS)},
        "protected_canonical_fields": sorted(PROTECTED_CANONICAL_FIELDS),
        "columns": [
            "airtable_record_id",
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
            "patch_field_set",
        ],
        "summary": {
            "total_inspected": len(rows),
            "safe_backfill": safe_count,
            "held": held_count,
            "status_counts": status_counts,
            "external_writes": 0,
        },
        "rows": rows,
    }


def apply_instantly_overlap_guards(
    manifest: Dict[str, Any],
    overlap_audit: Dict[str, Any],
) -> Dict[str, Any]:
    """Fail closed for queued rows already represented in Instantly.

    Sent/processed contacts can never remain backfill-eligible.  An unsent
    overlap remains eligible only when it is otherwise send-safe; held rows
    stay held.  This function is pure and performs no API calls or writes.
    """
    guarded = copy.deepcopy(manifest)
    columns = list(guarded.get("columns") or [])
    if "instantly_overlap_state" in columns:
        raise ValueError("Instantly overlap guards have already been applied")
    id_index = columns.index("airtable_record_id")
    hold_index = columns.index("outbound_hold")
    safe_index = columns.index("safe_backfill")
    overlap_by_id: Dict[str, list[Dict[str, Any]]] = {}
    for item in overlap_audit.get("overlaps") or []:
        overlap_by_id.setdefault(str(item.get("airtable_record_id") or ""), []).append(item)

    resolver_safe = sum(bool(row[safe_index]) for row in guarded.get("rows") or [])
    protected_rows = protected_safe = unsent_held = unsent_update = 0
    for row in guarded.get("rows") or []:
        items = overlap_by_id.get(str(row[id_index]), [])
        has_protected = any(bool(item.get("sent_or_processed")) for item in items)
        has_unsent = any(not bool(item.get("sent_or_processed")) for item in items)
        if has_protected:
            state = "sent_or_processed"
            protected_rows += 1
            if row[safe_index]:
                protected_safe += 1
            row[safe_index] = False
            eligibility = (
                "held_and_instantly_sent_or_processed"
                if row[hold_index]
                else "excluded_instantly_sent_or_processed"
            )
        elif has_unsent and row[hold_index]:
            state = "unsent_held"
            eligibility = "held"
            row[safe_index] = False
            unsent_held += 1
        elif has_unsent:
            state = "unsent_reconciliation_required"
            eligibility = "eligible_after_instantly_reconciliation"
            unsent_update += int(bool(row[safe_index]))
        else:
            state = "none"
            eligibility = "held" if row[hold_index] else "eligible"
        row.extend((state, eligibility))

    columns.extend(("instantly_overlap_state", "backfill_eligibility"))
    guarded["columns"] = columns
    summary = dict(guarded.get("summary") or {})
    summary.update({
        "resolver_safe_before_instantly_guard": resolver_safe,
        "safe_backfill": sum(bool(row[safe_index]) for row in guarded.get("rows") or []),
        "instantly_sent_or_processed_rows": protected_rows,
        "sent_or_processed_resolver_safe_excluded": protected_safe,
        "instantly_unsent_held_rows": unsent_held,
        "instantly_unsent_update_candidates": unsent_update,
    })
    guarded["summary"] = summary
    return guarded


def run() -> Dict[str, Any]:
    signing_key = str(os.getenv("VALIDATION_SIGNING_KEY") or "")
    records: list[Dict[str, Any]] = []
    for status in ALLOWED_STATUSES:
        records.extend(_fetch_status_records(status))
    return prepare_records(records, signing_key=signing_key)


def fetch_status_page(status: str, *, offset: str = "", page_size: int = 100) -> tuple[list[Dict[str, Any]], str]:
    """Fetch exactly one Airtable page for bounded, resumable dry-run output."""
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Refusing out-of-scope lifecycle status: {status}")
    token = str(os.getenv("AIRTABLE_TOKEN") or "").strip()
    base_id = str(os.getenv("AIRTABLE_BASE_ID") or "").strip()
    table = str(os.getenv("AIRTABLE_TABLE_NAME") or "Leads").strip()
    if not token or not base_id or not table:
        raise ValueError("AIRTABLE_TOKEN, AIRTABLE_BASE_ID, and AIRTABLE_TABLE_NAME are required")
    params = [("filterByFormula", f"{{Status}} = '{status}'"), ("pageSize", str(page_size))]
    if offset:
        params.append(("offset", offset))
    url = f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}?{urlencode(params)}"
    request = Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed Airtable host
        payload = json.loads(response.read().decode("utf-8"))
    return list(payload.get("records") or []), str(payload.get("offset") or "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    parser.add_argument("--page-status", choices=ALLOWED_STATUSES, help="Fetch exactly one status page")
    parser.add_argument("--page-offset", default="", help="Opaque Airtable page offset")
    parser.add_argument("--generated-at", default="", help="Fixed manifest timestamp for page assembly")
    parser.add_argument("--instantly-audit", default="", help="Apply a saved read-only Instantly overlap audit")
    args = parser.parse_args(argv)
    if args.page_status:
        records, next_offset = fetch_status_page(args.page_status, offset=args.page_offset)
        manifest = prepare_records(
            records,
            signing_key=str(os.getenv("VALIDATION_SIGNING_KEY") or ""),
            generated_at=args.generated_at or None,
        )
        manifest["next_offset"] = next_offset
    else:
        manifest = run()
    if args.instantly_audit:
        overlap_audit = json.loads(Path(args.instantly_audit).read_text(encoding="utf-8"))
        manifest = apply_instantly_overlap_guards(manifest, overlap_audit)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=None if args.compact else 2, separators=(",", ":") if args.compact else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
