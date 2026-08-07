"""Approved-sync worker: enroll manually approved Airtable leads into Instantly.

Definitive scheduled worker (Railway service "GTM Approved Sync",
``python -u run_approved.py``, cron ``*/5 * * * *``, restart NEVER). One execution
per cron tick; the process exits after each sync (no internal loop, no whole-job
retry). It NEVER runs acquisition, ATS, JSearch or free-feed collection.

Selection is fail-closed and manual-only: only Airtable rows an operator set to
``Status = Approved`` are considered. Before enrollment each record is
revalidated -- with the live Apollo/Hunter re-check when
``APPROVED_SYNC_REVALIDATE_PROVIDERS`` is true (default, production), otherwise
with the zero-network validation-fingerprint + actionable-state + email/campaign/
suppression checks. Instantly enrollment is idempotent; a record's Airtable
status advances to Enrolled only after confirmed Instantly success, and a failed
revalidation or enrollment leaves the record unchanged.

``--preflight-only`` performs a zero-write backlog audit: it reads Airtable and
classifies the approved backlog, calling neither Instantly nor any write.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import airtable_client
import config
import instantly_client
from approved_revalidation import revalidate_approved_record
from validation_integrity import fingerprint_matches

Path(config.LOG_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(
            Path(config.LOG_DIR) / f"approved_{datetime.now():%Y-%m-%d}.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def _local_revalidate(record: dict) -> tuple[bool, str]:
    """Zero-network fail-closed revalidation used when provider re-check is off.

    Enforces the validation-integrity fingerprint (critical fields unchanged) and
    the same actionable-state / required-field / campaign / suppression rules the
    Instantly builder applies -- all without any Apollo/Hunter call.
    """
    fields = record.get("fields") or {}
    if not fingerprint_matches(fields):
        return False, "Validation fingerprint mismatch; critical Airtable fields changed"
    try:
        instantly_client.airtable_record_to_lead(record, probe=False)
    except Exception as exc:  # noqa: BLE001 - any builder rejection is fail-closed
        return False, f"Local revalidation failed: {exc}"
    return True, "approved_record_locally_revalidated"


def _revalidate(record: dict, *, use_providers: bool) -> tuple[bool, str]:
    if use_providers:
        # Fail-closed live re-check (Apollo employment/email, gates). Production.
        return revalidate_approved_record(record)
    return _local_revalidate(record)


def run(*, revalidate_providers: bool | None = None) -> dict:
    if revalidate_providers is None:
        revalidate_providers = config.APPROVED_SYNC_REVALIDATE_PROVIDERS

    approved = airtable_client.get_approved_leads()
    if not approved:
        result = {"approved": 0, "enrolled": 0, "duplicates": 0, "failed": 0,
                  "revalidation_failed": 0}
        logger.info("No approved leads waiting")
        return result

    safe_records = []
    revalidation_failures = []
    for record in approved:
        try:
            valid, reason = _revalidate(record, use_providers=revalidate_providers)
        except Exception as exc:  # noqa: BLE001 - a revalidation crash is fail-closed
            valid, reason = False, f"Approved revalidation error: {exc}"
        if valid:
            safe_records.append(record)
        else:
            # Fail-closed: the record is NOT enrolled and its approval is not
            # consumed. It is marked Error with the reason; nothing is enrolled.
            revalidation_failures.append({
                "record_id": record.get("id", ""),
                "email": (record.get("fields") or {}).get("Email", ""),
                "error": reason,
            })
            airtable_client.mark_error([record.get("id", "")], reason)

    # Instantly enrollment: per-record, idempotent (skip_if_in_workspace/campaign +
    # 409/422 duplicate handling). One record's failure never aborts the batch.
    result = instantly_client.enroll_approved_leads(safe_records)

    # Airtable status advances to Enrolled ONLY for confirmed successes (enrolled
    # or confirmed-duplicate). Failures are left unchanged apart from an Error note.
    if result["enrolled_record_ids"]:
        airtable_client.mark_enrolled(result["enrolled_record_ids"])
    for failure in result["failures"]:
        airtable_client.mark_error([failure["record_id"]], failure["error"])

    result["failures"] = [*revalidation_failures, *result["failures"]]
    result["failed"] = len(result["failures"])
    result["revalidation_failed"] = len(revalidation_failures)
    result["approved"] = len(approved)
    logger.info("Enrollment result: %s", json.dumps(result, indent=2))
    return result


def _classify_for_preflight(record: dict) -> str:
    """Zero-network eligibility bucket for one Approved record.

    Reuses the pure Instantly lead builder (no provider call) so the audit mirrors
    real enrollment gating exactly. Returns a coarse category -- never PII.
    """
    try:
        lead = instantly_client.airtable_record_to_lead(record, probe=False)
    except Exception as exc:  # noqa: BLE001
        message = str(exc).lower()
        if "missing required" in message and "email" in message:
            return "blocked_missing_email"
        if "campaign" in message:
            return "blocked_no_campaign"
        if "not actionable" in message or "validation version" in message:
            return "blocked_not_actionable"
        # enrollment_block_reason (suppression / signal) and anything else.
        return "blocked_suppressed_or_signal"
    if not str(lead.get("email") or "").strip():
        return "blocked_missing_email"
    return "eligible"


def preflight() -> dict:
    """Zero-write backlog audit. Reads Airtable only; never calls Instantly."""
    approved = airtable_client.fetch_status_records(config.AIRTABLE_STATUS_APPROVED)
    enrolled = airtable_client.fetch_status_records(config.AIRTABLE_STATUS_ENROLLED)

    counts = {
        "approved_total": len(approved),
        "already_enrolled": len(enrolled),
        "eligible": 0,
        "blocked_missing_email": 0,
        "blocked_no_campaign": 0,
        "blocked_not_actionable": 0,
        "blocked_suppressed_or_signal": 0,
    }
    for record in approved:
        counts[_classify_for_preflight(record)] += 1

    summary = {
        "mode": "preflight_only",
        "writes": 0,
        "instantly_called": False,
        "revalidate_providers_default": config.APPROVED_SYNC_REVALIDATE_PROVIDERS,
        "canonical_approved_status": config.AIRTABLE_STATUS_APPROVED,
        "counts": counts,
    }
    logger.info("Approved-sync preflight: %s", json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Enroll manually approved Airtable leads into Instantly.")
    p.add_argument(
        "--preflight-only",
        action="store_true",
        help="Zero-write backlog audit: read Airtable, classify the approved "
             "backlog, and exit. Never calls Instantly or writes to Airtable.",
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.preflight_only:
        try:
            preflight()
        except Exception:
            logger.exception("Approved-sync preflight failed")
            return 1
        return 0

    try:
        result = run()
    except Exception:
        logger.exception("Approved-lead sync crashed")
        return 1

    return 1 if int(result.get("failed", 0)) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
