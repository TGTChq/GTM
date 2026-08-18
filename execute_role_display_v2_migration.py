"""Controlled executor for the reviewed role-display/2 migration.

The existing outbound-display executors are bound to a different migration:
``execute_airtable_outbound_backfill`` rewrites all nine Outbound fields from a
tabular ``columns/rows`` manifest with a 519/43/9 partition, and
``execute_instantly_outbound_correction`` requires the
``tgtc-instantly-campaign-outbound-audit/v1`` schema with a fixed 53/6 lead set
and patches ``company_name``.  Neither can consume the role-display/2 manifests
produced by :mod:`prepare_role_display_v2_migration`.

This utility is the smallest dedicated, fail-closed execution path for that
migration.  It is manifest-bound (SHA-256 gated), re-plans against live provider
state before writing, and:

* Airtable writes are limited to the role-only display fields plus the signed
  validation triple; the Validation Fingerprint is regenerated with
  ``VALIDATION_SIGNING_KEY`` and read-back verified.  Canonical fields
  (Open Role, Open Roles, Matched Role, Role Focus, and identity) are never
  patched and are proven unchanged on read-back.
* Instantly writes are limited to ``custom_variables.open_role`` /
  ``open_roles`` on leads that are still uncontacted at an immediate pre-write
  recheck; contacted leads are excluded and never mutated.  Unrelated custom
  variables are preserved and verified.
* Ambiguous/held records are never sent: held rows are excluded from every
  write partition.

It never changes an Airtable lifecycle field and never sends email.  Writing
requires an explicit ``--execute`` flag; the default is a read-only
reconciliation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import config
from audit_instantly_campaign_outbound import (
    LEAD_STATUS,
    MUTABLE_UNSENT_LEAD_STATUSES,
    _airtable_records,
    _campaigns,
    _email,
    _list_campaign_leads,
    _record_campaign_id,
    _text,
)
from prepare_role_display_v2_migration import (
    PROTECTED_CANONICAL_FIELDS,
    ROLE_PATCH_CANDIDATES,
    _canonical_guard_hash,
    _fingerprint,
    _role_input,
    build_airtable_patch,
    build_manifests,
)
from role_display_resolver import RESOLVER_VERSION, resolve_role_display

AIRTABLE_SUMMARY_SCHEMA = "tgtc-role-display-v2-migration-summary/v1"
INSTANTLY_ROLE_KEYS = ("open_role", "open_roles")
# Instantly read-after-write is eventually consistent; poll the post-patch
# read-back this many times before failing closed on a value mismatch.
READBACK_ATTEMPTS = 6
# Role-semantic fields that must be identical between the reviewed manifest and
# a fresh re-plan.  Time-dependent fields (Validated At, Validation Fingerprint)
# are intentionally excluded: they always change and are regenerated at write.
AIRTABLE_MATERIAL_FIELDS = (
    "proposed_outbound_role",
    "proposed_outbound_roles",
    "role_confidence",
    "role_status",
    "role_hold",
    "proposed_hold",
    "current_hold",
    "canonical_guard_hash",
)
INSTANTLY_MATERIAL_FIELDS = (
    "airtable_record_id",
    "campaign_id",
    "proposed_open_role",
    "proposed_open_roles",
    "role_status",
)


class GuardFailure(RuntimeError):
    """A fail-closed safety gate rejected the operation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _signing_key() -> str:
    key = _text(os.getenv("VALIDATION_SIGNING_KEY") or config.VALIDATION_SIGNING_KEY)
    if not key:
        raise GuardFailure("VALIDATION_SIGNING_KEY is required")
    return key


# --------------------------------------------------------------------------- #
# Pure reconciliation / safety logic (unit tested without provider I/O)
# --------------------------------------------------------------------------- #
def reconcile_rows(
    reviewed_rows: Dict[str, Dict[str, Any]],
    fresh_rows: Dict[str, Dict[str, Any]],
    material_fields: tuple[str, ...],
) -> list[Dict[str, Any]]:
    """Return the material differences between two keyed manifest row maps.

    A reviewed row that is missing from the fresh re-plan, or whose material
    fields changed, is a stale-record failure.  Fresh-only rows are reported as
    ``new_record`` so the operator re-reviews rather than silently widening.
    """
    differences: list[Dict[str, Any]] = []
    for key in sorted(set(reviewed_rows) | set(fresh_rows)):
        if key not in fresh_rows:
            differences.append({"key": key, "reason": "missing_from_fresh_replan"})
            continue
        if key not in reviewed_rows:
            differences.append({"key": key, "reason": "new_record"})
            continue
        changed = [
            field for field in material_fields
            if reviewed_rows[key].get(field) != fresh_rows[key].get(field)
        ]
        if changed:
            differences.append({"key": key, "reason": "material_change", "fields": changed})
    return differences


def rows_by_key(rows: list[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        identifier = _text(row.get(key))
        if not identifier:
            raise GuardFailure(f"Manifest row missing {key}")
        if identifier in out:
            raise GuardFailure(f"Duplicate manifest row for {key}={identifier!r}")
        out[identifier] = row
    return out


def canonical_drift(before_fields: Dict[str, Any], after_fields: Dict[str, Any]) -> list[str]:
    """Protected canonical fields that differ after a write (must be empty)."""
    return sorted(
        field for field in PROTECTED_CANONICAL_FIELDS
        if before_fields.get(field) != after_fields.get(field)
    )


def build_instantly_role_patch(
    current_payload: Dict[str, Any],
    proposed_name: str,
) -> tuple[Dict[str, Any], list[str]]:
    """Build a role-only merged custom_variables patch.

    Only ``open_role`` / ``open_roles`` may change; every other custom variable
    is carried through unchanged.  ``company_name`` is never touched.
    """
    merged = deepcopy(current_payload) if isinstance(current_payload, dict) else {}
    corrected: list[str] = []
    if _text(merged.get("open_role")) != _text(proposed_name):
        merged["open_role"] = proposed_name
        corrected.append("custom_variables.open_role")
    if _text(merged.get("open_roles")) != _text(proposed_name):
        merged["open_roles"] = proposed_name
        corrected.append("custom_variables.open_roles")
    if not corrected:
        return {}, []
    return {"custom_variables": merged}, corrected


def unrelated_custom_vars_changed(
    before_payload: Dict[str, Any],
    after_payload: Dict[str, Any],
) -> bool:
    """Whether any non-role custom variable changed (fail closed if True)."""
    before = {k: v for k, v in (before_payload or {}).items() if k not in INSTANTLY_ROLE_KEYS}
    after = {k: v for k, v in (after_payload or {}).items() if k not in INSTANTLY_ROLE_KEYS}
    return before != after


# --------------------------------------------------------------------------- #
# Fresh live re-plan (thin provider I/O around the reviewed planner)
# --------------------------------------------------------------------------- #
def fresh_plan(*, generated_at: str, signing_key: str) -> Dict[str, Any]:
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
    result["_records_by_id"] = {_text(r.get("id")): r for r in records}
    result["_campaigns"] = campaigns
    return result


def _load_manifest(path: Path, expected_sha256: str) -> Dict[str, Any]:
    actual = _sha256_file(path)
    if actual.lower() != expected_sha256.lower():
        raise GuardFailure(
            f"Reviewed manifest SHA-256 mismatch: expected {expected_sha256.lower()}, got {actual}"
        )
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _summary_schema_ok(manifest: Dict[str, Any]) -> bool:
    return _text((manifest.get("summary") or {}).get("schema")) == AIRTABLE_SUMMARY_SCHEMA


# --------------------------------------------------------------------------- #
# Airtable execution
# --------------------------------------------------------------------------- #
class AirtableAPI:
    def __init__(self) -> None:
        self.token = _text(os.getenv("AIRTABLE_TOKEN") or config.AIRTABLE_TOKEN)
        self.base_id = _text(os.getenv("AIRTABLE_BASE_ID") or config.AIRTABLE_BASE_ID)
        self.table = _text(os.getenv("AIRTABLE_TABLE_NAME") or config.AIRTABLE_TABLE_NAME) or "Leads"
        if not self.token or not self.base_id or not self.table:
            raise GuardFailure("AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME are required")

    def _request(self, method: str, body: Dict[str, Any] | None = None) -> Dict[str, Any]:
        url = f"https://api.airtable.com/v0/{self.base_id}/{quote(self.table, safe='')}"
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        request = Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "TGTC-role-display-v2-executor/1.0",
        })
        for attempt in range(5):
            try:
                with urlopen(request, timeout=45) as response:  # noqa: S310 - fixed provider host
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                    raise GuardFailure(f"Airtable {method} failed: HTTP {exc.code}: {detail}") from exc
                time.sleep(attempt + 1)
        raise GuardFailure("Airtable request failed after retries")

    def patch_batch(self, batch: list[Dict[str, Any]]) -> list[str]:
        payload = self._request("PATCH", {"records": batch, "typecast": False})
        return [_text(record.get("id")) for record in payload.get("records") or []]


def reconcile_airtable(reviewed: Dict[str, Any], fresh: Dict[str, Any]) -> Dict[str, Any]:
    """Compare the reviewed Airtable safe rows against a fresh re-plan."""
    reviewed_safe = rows_by_key(list(reviewed.get("rows") or []), "airtable_record_id")
    fresh_safe = rows_by_key(list(fresh.get("airtable_safe") or []), "airtable_record_id")
    fresh_held = {_text(r.get("airtable_record_id")) for r in fresh.get("airtable_holds") or []}
    fresh_protected = {_text(r.get("airtable_record_id")) for r in fresh.get("protected_contacted") or []}

    differences = reconcile_rows(reviewed_safe, fresh_safe, AIRTABLE_MATERIAL_FIELDS)
    # A reviewed-safe record that became held or contacted-protected is stale.
    for record_id in reviewed_safe:
        if record_id in fresh_held:
            differences.append({"key": record_id, "reason": "became_held"})
        elif record_id in fresh_protected:
            differences.append({"key": record_id, "reason": "became_contacted_protected"})
    return {
        "passes": not differences,
        "reviewed_safe": len(reviewed_safe),
        "fresh_safe": len(fresh_safe),
        "fresh_held": len(fresh_held),
        "fresh_protected": len(fresh_protected),
        "differences_count": len(differences),
        "differences": differences[:100],
    }


def execute_airtable(
    *,
    manifest_path: Path,
    expected_sha256: str,
    do_write: bool,
    api_factory: Callable[[], AirtableAPI] = AirtableAPI,
    plan_fn: Callable[..., Dict[str, Any]] = fresh_plan,
) -> Dict[str, Any]:
    reviewed = _load_manifest(manifest_path, expected_sha256)
    if not _summary_schema_ok(reviewed):
        raise GuardFailure(f"Manifest is not a {AIRTABLE_SUMMARY_SCHEMA} artifact")
    signing_key = _signing_key()
    execution_at = _utc_now()
    fresh = plan_fn(generated_at=execution_at, signing_key=signing_key)
    reconciliation = reconcile_airtable(reviewed, fresh)
    if not reconciliation["passes"]:
        return {"mode": "airtable", "aborted": True, "reason": "reviewed_manifest_stale",
                "reconciliation": reconciliation, "external_writes": 0}
    if not do_write:
        return {"mode": "airtable", "aborted": False, "would_write": reconciliation["reviewed_safe"],
                "reconciliation": reconciliation, "external_writes": 0}

    records_by_id = fresh["_records_by_id"]
    reviewed_safe_ids = sorted(rows_by_key(list(reviewed.get("rows") or []), "airtable_record_id"))
    expected_patches: Dict[str, Dict[str, Any]] = {}
    canonical_before: Dict[str, Dict[str, Any]] = {}
    batches: list[Dict[str, Any]] = []
    for record_id in reviewed_safe_ids:
        record = records_by_id.get(record_id)
        if not record:
            raise GuardFailure(f"Record vanished after reconcile: {record_id}")
        patch, meta = build_airtable_patch(record, generated_at=execution_at, signing_key=signing_key)
        if meta["role_hold"]:
            raise GuardFailure(f"Safe partition became held at write time: {record_id}")
        if set(patch) - set(ROLE_PATCH_CANDIDATES):
            raise GuardFailure(f"Out-of-contract patch field for {record_id}")
        expected_patches[record_id] = patch
        original = record.get("fields") or {}
        canonical_before[record_id] = {f: original.get(f) for f in PROTECTED_CANONICAL_FIELDS}
        batches.append({"id": record_id, "fields": patch})

    api = api_factory()
    patched: set[str] = set()
    failures: list[Dict[str, Any]] = []
    for start in range(0, len(batches), 10):
        chunk = batches[start:start + 10]
        try:
            returned = api.patch_batch(chunk)
            patched.update(returned)
            missing = sorted({item["id"] for item in chunk} - set(returned))
            failures.extend({"record_id": rid, "reason": "missing_from_patch_response"} for rid in missing)
        except Exception as exc:  # noqa: BLE001 - capture partial execution precisely
            failures.extend({"record_id": item["id"], "reason": str(exc)[:300]} for item in chunk)
            break
        time.sleep(0.25)

    # Read-back verification against a fresh fetch.
    readback = {_text(r.get("id")): r for r in _airtable_records()}
    verified = 0
    readback_failures: list[Dict[str, Any]] = []
    for record_id in sorted(patched):
        record = readback.get(record_id)
        if not record:
            readback_failures.append({"record_id": record_id, "reason": "missing_on_readback"})
            continue
        fields = record.get("fields") or {}
        mismatched = []
        for field, value in expected_patches[record_id].items():
            actual = bool(fields.get(field)) if field == "Outbound Hold" else fields.get(field)
            if actual != value:
                mismatched.append(field)
        drift = canonical_drift(canonical_before[record_id], fields)
        fingerprint_ok = _text(fields.get("Validation Fingerprint")) == _fingerprint(fields, signing_key)
        version_ok = _text(fields.get("Validation Version")) == config.VALIDATION_VERSION
        if mismatched or drift or not fingerprint_ok or not version_ok:
            readback_failures.append({"record_id": record_id, "reason": "readback_mismatch",
                                      "fields": mismatched, "canonical_drift": drift,
                                      "fingerprint_ok": fingerprint_ok, "version_ok": version_ok})
        else:
            verified += 1
    return {
        "mode": "airtable", "aborted": False, "execution_at": execution_at,
        "reconciliation": reconciliation, "rows_selected": len(reviewed_safe_ids),
        "rows_patched": len(patched), "rows_failed": len(failures), "failures": failures,
        "readback_verified": verified, "readback_failed": len(readback_failures),
        "readback_failures": readback_failures,
        "canonical_field_drift": sum(bool(f.get("canonical_drift")) for f in readback_failures),
        "external_writes": len(patched),
    }


# --------------------------------------------------------------------------- #
# Instantly execution
# --------------------------------------------------------------------------- #
class InstantlyAPI:
    def __init__(self) -> None:
        key = _text(os.getenv("INSTANTLY_API_KEY"))
        if not key:
            raise GuardFailure("INSTANTLY_API_KEY is required")
        self.base = (_text(os.getenv("INSTANTLY_BASE_URL")) or "https://api.instantly.ai/api/v2").rstrip("/")
        self.headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
                        "User-Agent": "TGTC-role-display-v2-executor/1.0"}

    def _request(self, method: str, path: str, body: Dict[str, Any] | None = None,
                 *, retry: bool = True) -> Any:
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempts = 5 if retry else 1
        for attempt in range(attempts):
            try:
                request = Request(f"{self.base}{path}", data=data, headers=headers, method=method)
                with urlopen(request, timeout=60) as response:  # noqa: S310 - configured provider host
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else None
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                    raise GuardFailure(f"Instantly {method} {path} failed: HTTP {exc.code}: {detail}") from exc
                time.sleep(attempt + 1)
        raise GuardFailure("Instantly request failed after retries")

    def get_campaign(self, campaign_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/campaigns/{quote(campaign_id, safe='')}")

    def get_lead(self, lead_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/leads/{quote(lead_id, safe='')}")

    def patch_lead(self, lead_id: str, patch: Dict[str, Any]) -> None:
        self._request("PATCH", f"/leads/{quote(lead_id, safe='')}", patch)


def reconcile_instantly(reviewed: Dict[str, Any], fresh: Dict[str, Any]) -> Dict[str, Any]:
    reviewed_rows = rows_by_key(list(reviewed.get("rows") or []), "instantly_lead_id")
    fresh_rows = rows_by_key(list(fresh.get("instantly_safe_updates") or []), "instantly_lead_id")
    fresh_hold = {_text(r.get("instantly_lead_id")) for r in fresh.get("instantly_holds") or []}
    differences = reconcile_rows(reviewed_rows, fresh_rows, INSTANTLY_MATERIAL_FIELDS)
    for lead_id in reviewed_rows:
        if lead_id in fresh_hold:
            differences.append({"key": lead_id, "reason": "became_held"})
    return {
        "passes": not differences,
        "reviewed_safe": len(reviewed_rows),
        "fresh_safe": len(fresh_rows),
        "differences_count": len(differences),
        "differences": differences[:100],
    }


def execute_instantly(
    *,
    manifest_path: Path,
    expected_sha256: str,
    do_write: bool,
    api_factory: Callable[[], InstantlyAPI] = InstantlyAPI,
    plan_fn: Callable[..., Dict[str, Any]] = fresh_plan,
) -> Dict[str, Any]:
    reviewed = _load_manifest(manifest_path, expected_sha256)
    if not _summary_schema_ok(reviewed):
        raise GuardFailure(f"Manifest is not a {AIRTABLE_SUMMARY_SCHEMA} artifact")
    signing_key = _signing_key()
    execution_at = _utc_now()
    fresh = plan_fn(generated_at=execution_at, signing_key=signing_key)
    reconciliation = reconcile_instantly(reviewed, fresh)
    if not reconciliation["passes"]:
        return {"mode": "instantly", "aborted": True, "reason": "reviewed_manifest_stale",
                "reconciliation": reconciliation, "external_writes": 0}

    # Fail closed if any of the nine campaigns is active/running: an active
    # campaign can contact an unsent lead between recheck and send.
    campaigns = fresh["_campaigns"]
    active = [c for c in campaigns if int(c.get("status") or 0) in {1, 4}]
    if active:
        return {"mode": "instantly", "aborted": True, "reason": "campaigns_active_pause_first",
                "active_campaigns": [{"id": c["id"], "name": c.get("name")} for c in active],
                "external_writes": 0}
    if not do_write:
        return {"mode": "instantly", "aborted": False, "would_write": reconciliation["reviewed_safe"],
                "reconciliation": reconciliation, "external_writes": 0}

    records_by_id = fresh["_records_by_id"]
    reviewed_rows = rows_by_key(list(reviewed.get("rows") or []), "instantly_lead_id")
    api = api_factory()
    excluded: list[Dict[str, Any]] = []
    patched: list[Dict[str, Any]] = []
    failures: list[Dict[str, Any]] = []
    for lead_id in sorted(reviewed_rows):
        reviewed_row = reviewed_rows[lead_id]
        record_id = _text(reviewed_row.get("airtable_record_id"))
        campaign_id = _text(reviewed_row.get("campaign_id"))
        before = api.get_lead(lead_id)
        if _text(before.get("id")) != lead_id:
            raise GuardFailure(f"Lead identity mismatch on recheck: {lead_id}")
        if before.get("timestamp_last_contact"):
            excluded.append({"instantly_lead_id": lead_id, "airtable_record_id": record_id,
                             "reason": "became_contacted_before_write"})
            continue
        if int(before.get("status") or 0) not in MUTABLE_UNSENT_LEAD_STATUSES:
            excluded.append({"instantly_lead_id": lead_id, "airtable_record_id": record_id,
                             "reason": f"non_mutable_status:{LEAD_STATUS.get(int(before.get('status') or 0))}"})
            continue
        record = records_by_id.get(record_id)
        if not record:
            raise GuardFailure(f"Airtable record vanished for lead {lead_id}: {record_id}")
        fields = record.get("fields") or {}
        if _email(before.get("email")) != _email(fields.get("Email")):
            raise GuardFailure(f"Contact identity mismatch for lead {lead_id}")
        if _text(before.get("campaign")) != campaign_id or _record_campaign_id(fields) != campaign_id:
            raise GuardFailure(f"Campaign identity mismatch for lead {lead_id}")
        result = resolve_role_display(_role_input(record))
        if result.hold:
            raise GuardFailure(f"Reviewed-safe lead now resolves to hold: {lead_id}")
        if _text(result.name) != _text(reviewed_row.get("proposed_open_role")):
            raise GuardFailure(f"Proposed role drifted for lead {lead_id}")
        before_payload = before.get("payload") if isinstance(before.get("payload"), dict) else {}
        patch, corrected = build_instantly_role_patch(before_payload, result.name)
        if not patch:
            patched.append({"instantly_lead_id": lead_id, "airtable_record_id": record_id,
                            "corrected_fields": [], "already_at_target": True})
            continue
        api.patch_lead(lead_id, patch)
        # Instantly's read-after-write is eventually consistent: an immediate GET
        # can still return the pre-patch payload.  Poll until the role values
        # match rather than failing closed on propagation lag.  Every safety
        # invariant (uncontacted, unchanged campaign) is re-checked on each read,
        # so a genuinely wrong or contacted lead still fails closed.
        after: Dict[str, Any] = {}
        after_payload: Dict[str, Any] = {}
        for attempt in range(READBACK_ATTEMPTS):
            after = api.get_lead(lead_id)
            after_payload = after.get("payload") if isinstance(after.get("payload"), dict) else {}
            if after.get("timestamp_last_contact"):
                raise GuardFailure(f"Lead became contacted during patch: {lead_id}")
            if _text(after.get("campaign")) != campaign_id:
                raise GuardFailure(f"Campaign changed during patch: {lead_id}")
            if _text(after_payload.get("open_role")) == _text(result.name) and \
                    _text(after_payload.get("open_roles")) == _text(result.name):
                break
            if attempt == READBACK_ATTEMPTS - 1:
                raise GuardFailure(f"Role read-back mismatch for lead {lead_id}")
            time.sleep(1.0 + attempt)
        if _text(after.get("company_name")) != _text(before.get("company_name")):
            raise GuardFailure(f"company_name changed for lead {lead_id}")
        if unrelated_custom_vars_changed(before_payload, after_payload):
            raise GuardFailure(f"Unrelated custom variables changed for lead {lead_id}")
        patched.append({"instantly_lead_id": lead_id, "airtable_record_id": record_id,
                        "corrected_fields": corrected, "already_at_target": False})
        time.sleep(0.2)
    return {
        "mode": "instantly", "aborted": False, "execution_at": execution_at,
        "reconciliation": reconciliation, "rows_selected": len(reviewed_rows),
        "leads_patched": len([p for p in patched if not p["already_at_target"]]),
        "leads_already_at_target": len([p for p in patched if p["already_at_target"]]),
        "excluded_became_contacted": len([e for e in excluded if "contacted" in e["reason"]]),
        "excluded": excluded, "patched": patched, "failures": failures,
        "external_writes": len([p for p in patched if not p["already_at_target"]]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("airtable", "instantly"), required=True)
    parser.add_argument("--manifest", required=True,
                        help="Reviewed *_safe_updates.json manifest from prepare_role_display_v2_migration")
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--execute", action="store_true", help="Perform writes (default is reconcile-only)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        runner = execute_airtable if args.target == "airtable" else execute_instantly
        result = runner(
            manifest_path=Path(args.manifest),
            expected_sha256=args.expected_manifest_sha256,
            do_write=args.execute,
        )
    except GuardFailure as exc:
        print(json.dumps({"status": "failed_closed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("aborted") or result.get("rows_failed") or result.get("readback_failed") or result.get("failures"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
