"""Approved-sync worker: DELIVERY-ONLY enrollment of approved Airtable leads.

Definitive scheduled worker (Railway service "GTM Approved Sync",
``python -u run_approved.py``, restart NEVER). One execution per cron tick; the
process exits after each sync. It NEVER runs acquisition, ATS, JSearch or
free-feed collection.

**Approved is the authorization boundary.** A row an operator set to
``Status = Approved`` has already passed the full qualification/enrichment
pipeline upstream. This worker therefore DELIVERS it; it does not re-qualify it.

Explicitly NOT done here (all of it happened upstream):
  * no Apollo organization enrichment, no Apollo person match
  * no Hunter verification
  * no JobSourceResolver / job-URL probing / network corroboration
  * no re-run of the qualification gates
  * no validation-age ("staleness") rejection -- age is not a delivery failure

Zero provider credits are spent by this worker.

What DOES gate enrollment is local and derived only from stored Airtable
evidence: the row must be Approved, carry a current correctly-signed actionable
decision, and contain the fields needed to build a valid enrollment payload
(email, company, role, role focus, resolvable campaign). Instantly enrollment is
idempotent; a record advances to Enrolled only after confirmed Instantly success,
and a delivery failure leaves the row retryable with an Error note.

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
from validation_integrity import fingerprint_matches

#: Bounded failure reporting. The previous implementation dumped every failure
#: object through ``json.dumps(..., indent=2)``; with 627 failures that emitted
#: ~3,100 log lines in one burst, tripped Railway's 500 logs/sec replica limit
#: and dropped 2,602 messages -- including the run summary.
_MAX_LOGGED_FAILURE_EXAMPLES = 5

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


def _delivery_precheck(record: dict) -> tuple[bool, str]:
    """Local, zero-network delivery readiness check.

    This is the ONLY pre-enrollment gate. It answers one question: "can a valid
    Instantly payload be built from the evidence already stored on this row?" It
    deliberately does NOT re-assess whether the lead is still a good lead --
    that decision was made upstream and ratified by the operator's approval.

    Checks retained, and why each is necessary:
      * ``fingerprint_matches`` -- the row's critical fields must not have been
        edited since validation signed them, or the payload would not match what
        was approved. Pure local comparison.
      * ``airtable_record_to_lead(probe=False)`` -- constructs the real payload,
        so a row that cannot produce one is caught before Instantly rather than
        failing mid-delivery. ``probe=False`` keeps it hermetic: no job-URL
        probe, no network.
    """
    fields = record.get("fields") or {}
    if not fingerprint_matches(fields):
        return False, "Validation fingerprint mismatch; critical Airtable fields changed"
    try:
        instantly_client.airtable_record_to_lead(record, probe=False)
    except Exception as exc:  # noqa: BLE001 - any builder rejection is fail-closed
        return False, f"Delivery precheck failed: {exc}"
    return True, "approved_record_ready_for_delivery"


def _log_enrollment_result(result: dict) -> None:
    """Compact, bounded run summary.

    Emits counts plus a per-reason histogram and at most
    ``_MAX_LOGGED_FAILURE_EXAMPLES`` example record ids -- never the full failure
    list, and never PII beyond the ids already present in Airtable.
    """
    failures = result.get("failures") or []
    reasons: dict[str, int] = {}
    for failure in failures:
        key = str(failure.get("error") or "unknown").split(":", 1)[0][:80]
        reasons[key] = reasons.get(key, 0) + 1
    logger.info(
        "Approved-sync result: approved=%d attempted=%d enrolled=%d duplicates=%d "
        "failed=%d marked_enrolled=%d marked_error=%d",
        int(result.get("approved", 0)), int(result.get("approved_attempted", 0)),
        int(result.get("enrolled", 0)), int(result.get("duplicates", 0)),
        int(result.get("failed", 0)), int(result.get("airtable_marked_enrolled", 0)),
        int(result.get("airtable_mark_error", 0)))
    if reasons:
        logger.info("Approved-sync failure reasons: %s",
                    dict(sorted(reasons.items(), key=lambda kv: -kv[1])))
        examples = [f.get("record_id", "") for f in failures[:_MAX_LOGGED_FAILURE_EXAMPLES]]
        logger.info("Approved-sync failure examples (%d of %d): %s",
                    len(examples), len(failures), examples)


def run(*, revalidate_providers: bool | None = None) -> dict:
    # ``revalidate_providers`` is accepted for backward compatibility only and is
    # deliberately ignored: Approved Sync is delivery-only and never calls a
    # provider. Passing True no longer re-enables Apollo/Hunter revalidation.
    if revalidate_providers:
        logger.warning(
            "revalidate_providers=True is ignored: Approved Sync is delivery-only "
            "and performs no provider revalidation.")

    # Eligibility partition first: legacy/invalid Approved rows (the ~42-row
    # legacy backlog without current validation metadata) are dropped here with NO
    # Airtable write and NO Instantly call. Only current, authorized rows proceed.
    approved, eligibility = airtable_client.select_eligible_approved()
    base_metrics = {
        "approved_seen": eligibility["approved_seen"],
        "approved_eligible": eligibility["approved_eligible"],
        "approved_skipped_legacy": eligibility["approved_skipped_legacy"],
        "approved_skipped_invalid": eligibility["approved_skipped_invalid"],
        "approved_attempted": 0,
        "instantly_success": 0, "instantly_failed": 0,
        "airtable_marked_enrolled": 0, "airtable_mark_error": 0,
        "duplicates_suppressed": 0,
    }
    if not approved:
        result = {"approved": 0, "enrolled": 0, "duplicates": 0, "failed": 0,
                  "revalidation_failed": 0, **base_metrics}
        logger.info("No eligible approved leads waiting (seen=%d, skipped_legacy=%d, "
                    "skipped_invalid=%d)", eligibility["approved_seen"],
                    eligibility["approved_skipped_legacy"],
                    eligibility["approved_skipped_invalid"])
        return result

    safe_records = []
    revalidation_failures = []
    precheck_failed_ids: list[str] = []
    for record in approved:
        try:
            valid, reason = _delivery_precheck(record)
        except Exception as exc:  # noqa: BLE001 - a precheck crash is fail-closed
            valid, reason = False, f"Delivery precheck error: {exc}"
        if valid:
            safe_records.append(record)
        else:
            # A genuine data-integrity/config failure: the payload cannot be
            # built. Not enrolled, approval not consumed, marked Error so an
            # operator can fix the row. Age alone can never land here.
            revalidation_failures.append({
                "record_id": record.get("id", ""),
                "email": (record.get("fields") or {}).get("Email", ""),
                "error": reason,
            })
            precheck_failed_ids.append(record.get("id", ""))
    # One batched write instead of one PATCH per record (was 627 single-record
    # PATCHes on the incident run). mark_error already chunks in tens.
    if precheck_failed_ids:
        airtable_client.mark_error(
            precheck_failed_ids, "Delivery precheck failed; see Airtable row fields")

    # Instantly enrollment: per-record, idempotent (skip_if_in_workspace/campaign +
    # 409/422 duplicate handling). One record's failure never aborts the batch.
    result = instantly_client.enroll_approved_leads(safe_records)

    # Airtable status advances to Enrolled ONLY for confirmed successes (enrolled
    # or confirmed-duplicate). Failures are left unchanged apart from an Error note.
    if result["enrolled_record_ids"]:
        airtable_client.mark_enrolled(result["enrolled_record_ids"])
    for failure in result["failures"]:
        airtable_client.mark_error([failure["record_id"]], failure["error"])

    instantly_failures = list(result["failures"])
    result["failures"] = [*revalidation_failures, *instantly_failures]
    result["failed"] = len(result["failures"])
    result["revalidation_failed"] = len(revalidation_failures)
    result["approved"] = len(approved)
    # Explicit, unit-labelled metrics (eligible/legacy/invalid never conflated).
    result.update(base_metrics)
    result["approved_attempted"] = len(safe_records)
    result["instantly_success"] = int(result.get("enrolled", 0)) + int(result.get("duplicates", 0))
    result["instantly_failed"] = len(instantly_failures)
    result["airtable_marked_enrolled"] = len(result["enrolled_record_ids"])
    # mark_error writes only for ELIGIBLE rows that failed (revalidation or Instantly)
    # -- never for legacy/invalid rows, which were skipped before this point.
    result["airtable_mark_error"] = len(revalidation_failures) + len(instantly_failures)
    result["duplicates_suppressed"] = int(result.get("duplicates", 0))
    _log_enrollment_result(result)
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
        "delivery_only": True,
        "provider_revalidation": "disabled (Approved is the authorization boundary)",
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

    _print_sync_summary(result)
    return 1 if int(result.get("failed", 0)) > 0 else 0


def _print_sync_summary(result: dict) -> None:
    """Emit the Approved-Sync business metrics to stdout so the Railway operator
    can read the worker's outcome from logs. Counts only -- never PII."""
    approved = int(result.get("approved", 0) or 0)
    revalidation_failed = int(result.get("revalidation_failed", 0) or 0)
    print("============ APPROVED SYNC SUMMARY ============")
    print(f"{'APPROVED_FOUND':<24}{approved}")
    print(f"{'REVALIDATED':<24}{approved - revalidation_failed}")
    print(f"{'REVALIDATION_FAILED':<24}{revalidation_failed}")
    print(f"{'SENT_TO_INSTANTLY':<24}{int(result.get('enrolled', 0) or 0)}")
    print(f"{'DUPLICATES_SKIPPED':<24}{int(result.get('duplicates', 0) or 0)}")
    print(f"{'FAILED':<24}{int(result.get('failed', 0) or 0)}")
    print("==============================================")


if __name__ == "__main__":
    raise SystemExit(main())
