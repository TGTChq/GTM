"""Execute the reviewed Instantly outbound-display correction, fail closed.

This is an operator utility, not a production worker.  It deliberately requires
the reviewed manifest SHA-256 and an explicit ``--execute`` flag.  Airtable is
read only.  Instantly writes are limited to pausing/activating Finance, creating
or reusing one hold list, moving reviewed held leads to that list, and patching
the three reviewed outbound display values on reviewed unsent leads.
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
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import config
from audit_instantly_campaign_outbound import (
    CAMPAIGN_STATUS,
    MUTABLE_UNSENT_LEAD_STATUSES,
    _airtable_records,
    _email,
    _record_campaign_id,
    _resolve_target,
    _text,
)
from prepare_outbound_backfill import _resolver_cache


FINANCE_CAMPAIGN_ID = "1db88bbe-b2cf-4574-a5b7-1cb948151a86"
HOLD_LIST_NAME = "TGTC Outbound Hold v1"
REPORT_SCHEMA = "tgtc-instantly-outbound-correction/v1"
ROLE_KEYS = {"open_role", "open_roles"}


class GuardFailure(RuntimeError):
    """A fail-closed safety gate rejected the operation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_contact(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InstantlyAPI:
    def __init__(self) -> None:
        key = _text(os.getenv("INSTANTLY_API_KEY"))
        if not key:
            raise GuardFailure("INSTANTLY_API_KEY is missing")
        self.base = (_text(os.getenv("INSTANTLY_BASE_URL")) or "https://api.instantly.ai/api/v2").rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "TGTC-controlled-instantly-correction/1.0",
        }

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        retry_idempotent: bool = True,
    ) -> Any:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempts = 5 if retry_idempotent else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                request = Request(f"{self.base}{path}", data=data, headers=headers, method=method)
                with urlopen(request, timeout=60) as response:  # noqa: S310 - configured provider host
                    raw = response.read()
                    if not raw:
                        return None
                    return json.loads(raw.decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                last_error = RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}")
                if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                    break
                time.sleep(attempt + 1)
            except Exception as exc:  # noqa: BLE001 - provider boundary
                last_error = exc
                if attempt == attempts - 1:
                    break
                time.sleep(attempt + 1)
        raise GuardFailure(f"Instantly request failed: {last_error}")

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        return self.request("GET", f"/campaigns/{quote(campaign_id, safe='')}")

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        return self.request("GET", f"/leads/{quote(lead_id, safe='')}")

    def pause_campaign(self, campaign_id: str) -> None:
        self.request("POST", f"/campaigns/{quote(campaign_id, safe='')}/pause", retry_idempotent=False)

    def activate_campaign(self, campaign_id: str) -> None:
        self.request("POST", f"/campaigns/{quote(campaign_id, safe='')}/activate", retry_idempotent=False)

    def patch_lead(self, lead_id: str, patch: dict[str, Any]) -> None:
        # A repeated PATCH with the exact same values is idempotent.
        self.request("PATCH", f"/leads/{quote(lead_id, safe='')}", patch)

    def list_lead_lists(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params: list[tuple[str, str | int]] = [("limit", 100)]
            if cursor:
                params.append(("starting_after", cursor))
            payload = self.request("GET", f"/lead-lists?{urlencode(params)}")
            rows.extend(payload.get("items") or [])
            next_cursor = _text(payload.get("next_starting_after"))
            if not next_cursor:
                return rows
            if next_cursor == cursor:
                raise GuardFailure("Instantly lead-list pagination cursor did not advance")
            cursor = next_cursor

    def create_hold_list(self) -> dict[str, Any]:
        return self.request(
            "POST",
            "/lead-lists",
            {"name": HOLD_LIST_NAME, "has_enrichment_task": False},
            retry_idempotent=False,
        )

    def move_lead_to_list(self, *, lead_id: str, campaign_id: str, list_id: str) -> str:
        payload = self.request(
            "POST",
            "/leads/move",
            {
                "campaign": campaign_id,
                "ids": [lead_id],
                "to_list_id": list_id,
                "ignore_resource_filter_clauses": True,
                "skip_leads_in_verification": False,
            },
            retry_idempotent=False,
        )
        job_id = _text((payload or {}).get("id"))
        if not job_id:
            raise GuardFailure(f"Move response for lead {lead_id} did not contain a background job ID")
        return job_id

    def wait_for_job(self, job_id: str, *, timeout_seconds: int = 90) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            payload = self.request("GET", f"/background-jobs/{quote(job_id, safe='')}")
            status = _text(payload.get("status")).lower()
            if status == "success":
                return payload
            if status in {"failed", "cancelled"}:
                raise GuardFailure(f"Instantly move job {job_id} failed: {payload.get('data')!r}")
            time.sleep(2)
        raise GuardFailure(f"Timed out waiting for Instantly move job {job_id}")

    def list_campaign_leads(self, campaign_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        while True:
            body: dict[str, Any] = {"campaign": campaign_id, "limit": 100, "distinct_contacts": False}
            if cursor:
                body["starting_after"] = cursor
            payload = self.request("POST", "/leads/list", body)
            rows.extend(payload.get("items") or [])
            next_cursor = _text(payload.get("next_starting_after"))
            if not next_cursor:
                return rows
            if next_cursor == cursor:
                raise GuardFailure(f"Instantly lead cursor did not advance for campaign {campaign_id}")
            cursor = next_cursor


def _payload(lead: dict[str, Any]) -> dict[str, Any]:
    value = lead.get("payload")
    return deepcopy(value) if isinstance(value, dict) else {}


def _display_values(lead: dict[str, Any]) -> dict[str, str]:
    payload = _payload(lead)
    return {
        "company_name": _text(lead.get("company_name")),
        "open_role": _text(payload.get("open_role")),
        "open_roles": _text(payload.get("open_roles")),
    }


def _campaign_ids(manifest: dict[str, Any]) -> list[str]:
    return sorted({_text(row.get("id")) for row in manifest.get("campaigns") or []})


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def execute(
    *,
    manifest_path: Path,
    expected_sha256: str,
    report_path: Path,
    resume_checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    actual_sha256 = _sha256(manifest_path)
    if actual_sha256.lower() != expected_sha256.lower():
        raise GuardFailure(
            f"Reviewed manifest SHA-256 mismatch: expected {expected_sha256.lower()}, got {actual_sha256.lower()}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    safe_rows = list(manifest.get("safe_updates") or [])
    held_rows = list(manifest.get("held_unsent") or [])
    if manifest.get("schema") != "tgtc-instantly-campaign-outbound-audit/v1":
        raise GuardFailure(f"Unexpected manifest schema: {manifest.get('schema')!r}")
    if len(safe_rows) != 53 or len(held_rows) != 6:
        raise GuardFailure(f"Reviewed manifest must contain exactly 53 safe and 6 held rows; got {len(safe_rows)} and {len(held_rows)}")
    rows = [("safe", row) for row in safe_rows] + [("held", row) for row in held_rows]
    lead_ids = [_text(row.get("instantly_lead_id")) for _, row in rows]
    if not all(lead_ids) or len(set(lead_ids)) != 59:
        raise GuardFailure("Reviewed candidate set does not contain 59 unique Instantly lead IDs")

    checkpoint: dict[str, Any] | None = None
    checkpoint_hold_list_id = ""
    if resume_checkpoint_path is not None:
        checkpoint = json.loads(resume_checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("schema") != REPORT_SCHEMA or checkpoint.get("status") != "failed_closed":
            raise GuardFailure("Resume checkpoint is not a failed-closed correction report")
        if _text(checkpoint.get("reviewed_manifest_sha256")).lower() != actual_sha256.lower():
            raise GuardFailure("Resume checkpoint does not reference the reviewed manifest SHA-256")
        if int((checkpoint.get("finance") or {}).get("status_after_failure") or 0) != 2:
            raise GuardFailure("Resume checkpoint does not prove Finance was left paused")
        checkpoint_hold_list_id = _text((checkpoint.get("hold_list") or {}).get("id"))
        if not checkpoint_hold_list_id or len(checkpoint.get("held") or []) != 6:
            raise GuardFailure("Resume checkpoint does not prove all six held leads were isolated")

    api = InstantlyAPI()
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "started_at": _utc_now(),
        "completed_at": None,
        "status": "running",
        "reviewed_manifest": str(manifest_path.resolve()),
        "reviewed_manifest_sha256": actual_sha256,
        "resumed_from_checkpoint": str(resume_checkpoint_path.resolve()) if resume_checkpoint_path else None,
        "hold_list": {},
        "summary": {
            "candidates_expected": 59,
            "candidates_rechecked": 0,
            "excluded_became_contacted": 0,
            "safe_patched": 0,
            "safe_already_at_target": 0,
            "safe_verified_total": 0,
            "safe_failed": 0,
            "company_name_corrections": 0,
            "open_role_corrections": 0,
            "open_roles_corrections": 0,
            "held_prevented_from_sending": 0,
            "readback_verified": 0,
            "remaining_unsent_in_nine_campaigns": None,
        },
        "finance": {"campaign_id": FINANCE_CAMPAIGN_ID},
        "campaign_states": {},
        "excluded": [],
        "patched": [],
        "held": [],
        "error": None,
    }
    _write_report(report_path, report)

    try:
        if _text(os.getenv("INSTANTLY_CAMPAIGN_FINANCE")) != FINANCE_CAMPAIGN_ID:
            raise GuardFailure("Configured Finance campaign ID does not equal the reviewed Finance campaign ID")

        campaign_ids = _campaign_ids(manifest)
        if len(campaign_ids) != 9 or FINANCE_CAMPAIGN_ID not in campaign_ids:
            raise GuardFailure("Reviewed manifest does not contain the expected nine campaigns including Finance")
        initial_campaigns = {campaign_id: api.get_campaign(campaign_id) for campaign_id in campaign_ids}
        report["campaign_states"]["initial"] = {
            campaign_id: {
                "name": campaign.get("name"),
                "status": int(campaign.get("status") or 0),
                "status_label": CAMPAIGN_STATUS.get(int(campaign.get("status") or 0), "unknown"),
            }
            for campaign_id, campaign in initial_campaigns.items()
        }
        for campaign_id, campaign in initial_campaigns.items():
            status = int(campaign.get("status") or 0)
            if campaign_id != FINANCE_CAMPAIGN_ID and status in {1, 4}:
                raise GuardFailure(
                    f"Non-Finance campaign unexpectedly active/running: {campaign.get('name')} ({campaign_id})"
                )
        finance_initial = int(initial_campaigns[FINANCE_CAMPAIGN_ID].get("status") or 0)
        report["finance"]["initial_status"] = finance_initial
        if finance_initial == 1:
            api.pause_campaign(FINANCE_CAMPAIGN_ID)
            report["finance"]["pause_requested"] = True
        elif finance_initial == 2 and checkpoint is not None:
            # The previous fail-closed run deliberately left Finance paused.
            report["finance"]["pause_requested"] = False
            report["finance"]["pause_inherited_from_checkpoint"] = True
        else:
            raise GuardFailure(
                f"Finance must be active, or paused by the verified checkpoint, before correction; found status {finance_initial}"
            )
        finance_paused = api.get_campaign(FINANCE_CAMPAIGN_ID)
        report["finance"]["paused_status"] = int(finance_paused.get("status") or 0)
        if report["finance"]["paused_status"] != 2:
            raise GuardFailure("Finance did not verify as paused before candidate recheck")
        _write_report(report_path, report)

        airtable = _airtable_records()
        records_by_id = {_text(record.get("id")): record for record in airtable}
        cache = _resolver_cache()
        eligible_safe: list[dict[str, Any]] = []
        eligible_held: list[dict[str, Any]] = []
        already_held: list[dict[str, Any]] = []
        initial_snapshots: dict[str, dict[str, Any]] = {}

        lead_lists = api.list_lead_lists()
        named_hold_lists = [row for row in lead_lists if _text(row.get("name")) == HOLD_LIST_NAME]
        if len(named_hold_lists) > 1:
            raise GuardFailure(f"Multiple Instantly lead lists are named {HOLD_LIST_NAME!r}")
        if checkpoint_hold_list_id:
            if len(named_hold_lists) != 1 or _text(named_hold_lists[0].get("id")) != checkpoint_hold_list_id:
                raise GuardFailure("Checkpoint hold list no longer verifies by exact ID and name")

        for kind, reviewed in rows:
            lead_id = _text(reviewed.get("instantly_lead_id"))
            lead = api.get_lead(lead_id)
            if _text(lead.get("id")) != lead_id:
                raise GuardFailure(f"Lead ID mismatch while rechecking {lead_id}")
            report["summary"]["candidates_rechecked"] += 1
            if lead.get("timestamp_last_contact"):
                report["summary"]["excluded_became_contacted"] += 1
                report["excluded"].append({
                    "instantly_lead_id": lead_id,
                    "airtable_record_id": reviewed.get("airtable_record_id"),
                    "campaign_id": reviewed.get("campaign_id"),
                    "reason": "timestamp_last_contact_populated_before_mutation",
                })
                continue
            campaign_id = _text(reviewed.get("campaign_id"))
            live_campaign_id = _text(lead.get("campaign"))
            live_list_id = _text(lead.get("list_id"))
            held_from_checkpoint = (
                kind == "held"
                and bool(checkpoint_hold_list_id)
                and not live_campaign_id
                and live_list_id == checkpoint_hold_list_id
            )
            if not held_from_checkpoint and live_campaign_id != campaign_id:
                raise GuardFailure(f"Campaign mismatch for lead {lead_id}")
            record_id = _text(reviewed.get("airtable_record_id"))
            record = records_by_id.get(record_id)
            if not record:
                raise GuardFailure(f"Airtable record {record_id} no longer exists for lead {lead_id}")
            fields = record.get("fields") or {}
            if _email(lead.get("email")) != _email(fields.get("Email")):
                raise GuardFailure(f"Contact identity mismatch for lead {lead_id} and Airtable record {record_id}")
            if _record_campaign_id(fields) != campaign_id:
                raise GuardFailure(f"Airtable campaign identity mismatch for lead {lead_id}")
            target = _resolve_target(fields, cache)
            proposed = {
                "company_name": _text(target.get("company_name")),
                "open_role": _text(target.get("open_role")),
                "open_roles": _text(target.get("open_roles")),
            }
            reviewed_proposed = {key: _text((reviewed.get("proposed") or {}).get(key)) for key in proposed}
            if proposed != reviewed_proposed:
                raise GuardFailure(f"Proposed outbound values drifted for lead {lead_id}")
            if kind == "safe" and target.get("hold"):
                raise GuardFailure(f"Reviewed safe lead now resolves to hold: {lead_id}")
            if kind == "held" and not target.get("hold"):
                raise GuardFailure(f"Reviewed held lead no longer resolves to hold: {lead_id}")
            status = int(lead.get("status") or 0)
            if status not in MUTABLE_UNSENT_LEAD_STATUSES:
                raise GuardFailure(f"Lead {lead_id} has non-mutable status {status}")
            snapshot = {
                "kind": kind,
                "lead_id": lead_id,
                "record_id": record_id,
                "campaign_id": campaign_id,
                "already_held": held_from_checkpoint,
                "contact_hash": _hash_contact(_email(lead.get("email"))),
                "proposed": proposed,
                "lead": lead,
            }
            initial_snapshots[lead_id] = snapshot
            if kind == "safe":
                eligible_safe.append(snapshot)
            elif held_from_checkpoint:
                already_held.append(snapshot)
            else:
                eligible_held.append(snapshot)

        if report["summary"]["candidates_rechecked"] != 59:
            raise GuardFailure("Not all 59 candidates were rechecked")
        all_held = eligible_held + already_held
        finance_held = [row for row in all_held if row["campaign_id"] == FINANCE_CAMPAIGN_ID]
        if not finance_held:
            raise GuardFailure("The reviewed Finance held lead is not eligible to be placed on hold")
        report["summary"]["safe_eligible_after_recheck"] = len(eligible_safe)
        report["summary"]["held_eligible_after_recheck"] = len(all_held)
        report["summary"]["held_already_isolated_at_recheck"] = len(already_held)
        _write_report(report_path, report)

        existing_hold_lists = named_hold_lists
        if len(existing_hold_lists) > 1:
            raise GuardFailure(f"Multiple Instantly lead lists are named {HOLD_LIST_NAME!r}")
        if existing_hold_lists:
            hold_list = existing_hold_lists[0]
            report["hold_list"]["created"] = False
        else:
            hold_list = api.create_hold_list()
            report["hold_list"]["created"] = True
        hold_list_id = _text(hold_list.get("id"))
        if not hold_list_id or _text(hold_list.get("name")) != HOLD_LIST_NAME:
            raise GuardFailure("Dedicated hold list did not verify by ID and exact name")
        report["hold_list"].update({"id": hold_list_id, "name": HOLD_LIST_NAME})
        _write_report(report_path, report)

        for row in already_held:
            report["summary"]["held_prevented_from_sending"] += 1
            report["held"].append({
                "instantly_lead_id": row["lead_id"],
                "airtable_record_id": row["record_id"],
                "source_campaign_id": row["campaign_id"],
                "hold_list_id": hold_list_id,
                "move_job_id": None,
                "verified_not_in_campaign": True,
                "already_isolated_at_recheck": True,
            })

        # Move held leads one at a time so every reviewed identity has its own
        # provider job and read-back gate.
        for row in eligible_held:
            lead_id = row["lead_id"]
            before = api.get_lead(lead_id)
            if before.get("timestamp_last_contact"):
                raise GuardFailure(f"Held lead became contacted during correction: {lead_id}")
            if _text(before.get("campaign")) != row["campaign_id"]:
                raise GuardFailure(f"Held lead campaign drifted during correction: {lead_id}")
            if _hash_contact(_email(before.get("email"))) != row["contact_hash"]:
                raise GuardFailure(f"Held lead identity drifted during correction: {lead_id}")
            job_id = api.move_lead_to_list(
                lead_id=lead_id,
                campaign_id=row["campaign_id"],
                list_id=hold_list_id,
            )
            api.wait_for_job(job_id)
            after = api.get_lead(lead_id)
            if after.get("timestamp_last_contact"):
                raise GuardFailure(f"Held lead was contacted while being moved: {lead_id}")
            if _text(after.get("campaign")):
                raise GuardFailure(f"Held lead still has a campaign after move: {lead_id}")
            if _text(after.get("list_id")) != hold_list_id:
                raise GuardFailure(f"Held lead does not verify in the dedicated hold list: {lead_id}")
            if _hash_contact(_email(after.get("email"))) != row["contact_hash"]:
                raise GuardFailure(f"Held lead identity changed after move: {lead_id}")
            report["summary"]["held_prevented_from_sending"] += 1
            report["held"].append({
                "instantly_lead_id": lead_id,
                "airtable_record_id": row["record_id"],
                "source_campaign_id": row["campaign_id"],
                "hold_list_id": hold_list_id,
                "move_job_id": job_id,
                "verified_not_in_campaign": True,
                "already_isolated_at_recheck": False,
            })
            _write_report(report_path, report)

        for row in eligible_safe:
            lead_id = row["lead_id"]
            before = api.get_lead(lead_id)
            if before.get("timestamp_last_contact"):
                raise GuardFailure(f"Safe lead became contacted during correction: {lead_id}")
            if _text(before.get("campaign")) != row["campaign_id"]:
                raise GuardFailure(f"Safe lead campaign drifted during correction: {lead_id}")
            if _hash_contact(_email(before.get("email"))) != row["contact_hash"]:
                raise GuardFailure(f"Safe lead identity drifted during correction: {lead_id}")
            current = _display_values(before)
            proposed = row["proposed"]
            patch: dict[str, Any] = {}
            corrected_fields: list[str] = []
            if current["company_name"] != proposed["company_name"]:
                patch["company_name"] = proposed["company_name"]
                corrected_fields.append("company_name")
            before_payload = _payload(before)
            merged_payload = deepcopy(before_payload)
            if current["open_role"] != proposed["open_role"]:
                merged_payload["open_role"] = proposed["open_role"]
                corrected_fields.append("custom_variables.open_role")
            if current["open_roles"] != proposed["open_roles"]:
                merged_payload["open_roles"] = proposed["open_roles"]
                corrected_fields.append("custom_variables.open_roles")
            if any(field.startswith("custom_variables.") for field in corrected_fields):
                patch["custom_variables"] = merged_payload
            if not patch:
                # This can occur only when resuming an idempotent checkpoint or
                # if an operator independently applied the exact reviewed values.
                if _display_values(before) != proposed:
                    raise GuardFailure(f"Lead has no computed patch but is not at target: {lead_id}")
                report["patched"].append({
                    "instantly_lead_id": lead_id,
                    "airtable_record_id": row["record_id"],
                    "campaign_id": row["campaign_id"],
                    "corrected_fields": [],
                    "already_at_target": True,
                    "verified": True,
                })
                report["summary"]["safe_already_at_target"] += 1
                report["summary"]["readback_verified"] += 1
                continue
            api.patch_lead(lead_id, patch)
            after = api.get_lead(lead_id)
            after_values = _display_values(after)
            if after_values != proposed:
                raise GuardFailure(f"Outbound value read-back mismatch for lead {lead_id}")
            if after.get("timestamp_last_contact"):
                raise GuardFailure(f"Lead became contacted during patch/read-back: {lead_id}")
            if _text(after.get("campaign")) != row["campaign_id"]:
                raise GuardFailure(f"Campaign changed during patch/read-back: {lead_id}")
            if _hash_contact(_email(after.get("email"))) != row["contact_hash"]:
                raise GuardFailure(f"Contact identity changed during patch/read-back: {lead_id}")
            after_payload = _payload(after)
            allowed_payload_changes = set(ROLE_KEYS)
            if "company_name" in corrected_fields:
                # Instantly maintains payload.companyName as a provider-managed
                # mirror of top-level company_name.  It is related to the
                # requested correction, not an unrelated custom variable.
                allowed_payload_changes.add("companyName")
                if _text(after_payload.get("companyName")) != proposed["company_name"]:
                    raise GuardFailure(f"Provider companyName mirror did not match company_name for lead {lead_id}")
            before_unrelated = {
                key: value for key, value in before_payload.items() if key not in allowed_payload_changes
            }
            after_unrelated = {
                key: value for key, value in after_payload.items() if key not in allowed_payload_changes
            }
            if after_unrelated != before_unrelated:
                raise GuardFailure(f"Unrelated custom variables changed for lead {lead_id}")
            report["summary"]["safe_patched"] += 1
            report["summary"]["company_name_corrections"] += int("company_name" in corrected_fields)
            report["summary"]["open_role_corrections"] += int("custom_variables.open_role" in corrected_fields)
            report["summary"]["open_roles_corrections"] += int("custom_variables.open_roles" in corrected_fields)
            report["summary"]["readback_verified"] += 1
            report["patched"].append({
                "instantly_lead_id": lead_id,
                "airtable_record_id": row["record_id"],
                "campaign_id": row["campaign_id"],
                "corrected_fields": corrected_fields,
                "already_at_target": False,
                "verified": True,
            })
            _write_report(report_path, report)

        # Final read-back of every candidate that was still unsent at the first
        # gate.  This catches a race after each individual immediate read-back.
        for row in eligible_safe:
            lead = api.get_lead(row["lead_id"])
            if lead.get("timestamp_last_contact"):
                raise GuardFailure(f"Safe lead became contacted before final verification: {row['lead_id']}")
            if _text(lead.get("campaign")) != row["campaign_id"]:
                raise GuardFailure(f"Safe lead campaign drifted before final verification: {row['lead_id']}")
            if _display_values(lead) != row["proposed"]:
                raise GuardFailure(f"Safe lead outbound values drifted before final verification: {row['lead_id']}")
        for row in all_held:
            lead = api.get_lead(row["lead_id"])
            if lead.get("timestamp_last_contact"):
                raise GuardFailure(f"Held lead became contacted before final verification: {row['lead_id']}")
            if _text(lead.get("campaign")) or _text(lead.get("list_id")) != hold_list_id:
                raise GuardFailure(f"Held lead is not safely isolated at final verification: {row['lead_id']}")

        remaining_unsent = 0
        for campaign_id in campaign_ids:
            remaining_unsent += sum(
                1 for lead in api.list_campaign_leads(campaign_id) if not lead.get("timestamp_last_contact")
            )
        report["summary"]["remaining_unsent_in_nine_campaigns"] = remaining_unsent
        report["summary"]["uncontacted_preserved_in_hold_list"] = len(all_held)
        report["summary"]["safe_verified_total"] = len(eligible_safe)

        # Other campaign states must remain unchanged throughout.  Finance is
        # activated only after all patch/hold/race gates have passed.
        before_resume = {campaign_id: api.get_campaign(campaign_id) for campaign_id in campaign_ids}
        for campaign_id, campaign in before_resume.items():
            if campaign_id == FINANCE_CAMPAIGN_ID:
                if int(campaign.get("status") or 0) != 2:
                    raise GuardFailure("Finance is no longer paused at the resume gate")
            elif int(campaign.get("status") or 0) != int(initial_campaigns[campaign_id].get("status") or 0):
                raise GuardFailure(f"Non-Finance campaign state drifted: {campaign_id}")

        api.activate_campaign(FINANCE_CAMPAIGN_ID)
        finance_resumed = api.get_campaign(FINANCE_CAMPAIGN_ID)
        report["finance"]["resumed_status"] = int(finance_resumed.get("status") or 0)
        if report["finance"]["resumed_status"] != 1:
            raise GuardFailure("Finance did not verify as active after the resume request")
        final_campaigns = {campaign_id: api.get_campaign(campaign_id) for campaign_id in campaign_ids}
        for campaign_id, campaign in final_campaigns.items():
            expected = 1 if campaign_id == FINANCE_CAMPAIGN_ID else int(initial_campaigns[campaign_id].get("status") or 0)
            if int(campaign.get("status") or 0) != expected:
                raise GuardFailure(f"Final campaign-state verification failed for {campaign_id}")
        report["campaign_states"]["final"] = {
            campaign_id: {
                "name": campaign.get("name"),
                "status": int(campaign.get("status") or 0),
                "status_label": CAMPAIGN_STATUS.get(int(campaign.get("status") or 0), "unknown"),
            }
            for campaign_id, campaign in final_campaigns.items()
        }
        report["finance"]["pause_verified"] = True
        report["finance"]["resume_verified"] = True
        report["status"] = "success"
        report["completed_at"] = _utc_now()
        _write_report(report_path, report)
        return report
    except Exception as exc:
        report["status"] = "failed_closed"
        report["completed_at"] = _utc_now()
        report["error"] = str(exc)
        # Do not resume Finance on any failure after pausing it.
        try:
            report["finance"]["status_after_failure"] = int(api.get_campaign(FINANCE_CAMPAIGN_ID).get("status") or 0)
        except Exception as state_exc:  # noqa: BLE001 - retain the primary failure
            report["finance"]["status_after_failure_error"] = str(state_exc)
        _write_report(report_path, report)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="reports/instantly_campaign_outbound_audit_20260817.json")
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--report", default="reports/instantly_outbound_correction_20260817.json")
    parser.add_argument("--resume-checkpoint")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("Refusing to mutate Instantly without --execute")
    try:
        result = execute(
            manifest_path=Path(args.manifest),
            expected_sha256=args.expected_manifest_sha256,
            report_path=Path(args.report),
            resume_checkpoint_path=Path(args.resume_checkpoint) if args.resume_checkpoint else None,
        )
    except Exception as exc:  # noqa: BLE001 - concise operator failure
        print(json.dumps({"status": "failed_closed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({
        "status": result["status"],
        "report": str(Path(args.report).resolve()),
        "summary": result["summary"],
        "finance": result["finance"],
        "hold_list": result["hold_list"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
