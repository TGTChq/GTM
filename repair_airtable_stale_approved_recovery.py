"""Recover Approved rows wrongly marked Error by the removed staleness gate.

Incident 2026-08-12 12:47 UTC: the Approved-sync worker re-ran the full
qualification pipeline before enrollment and rejected every row whose
``Validated At`` was older than ``APPROVED_REVALIDATION_MAX_AGE_HOURS`` (24h),
writing Status=Error with:

    "Validation is stale; rerun qualification before enrollment"

Proven about that incident:
  * the staleness gate returned BEFORE any Apollo/Hunter/JobSourceResolver call,
  * so ``safe_records`` was empty and ``enroll_approved_leads([])`` iterated an
    empty list -- ZERO Instantly calls were made,
  * therefore duplicate-enrollment risk from this incident is zero.

This script restores ONLY rows carrying that exact error string back to
Status=Approved (clearing the Error note) so the corrected delivery-only worker
can deliver them. It touches nothing else.

DRY RUN BY DEFAULT. Pass --apply to write. Always run without --apply first.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging

import airtable_client
import config
from http_utils import request_with_retry

logger = logging.getLogger(__name__)

#: The exact reason string written by the removed gate (approved_revalidation.py:41).
STALE_ERROR = "Validation is stale; rerun qualification before enrollment"


def select_recoverable() -> tuple[list[dict], dict]:
    """Read-only. Return (rows carrying exactly the stale error, reason histogram)."""
    errored = airtable_client.fetch_status_records(config.AIRTABLE_STATUS_ERROR)
    reasons: collections.Counter = collections.Counter()
    recoverable: list[dict] = []
    for record in errored:
        fields = record.get("fields") or {}
        error = str(fields.get("Error") or "").strip()
        reasons[error[:80] or "(empty)"] += 1
        if error == STALE_ERROR:
            recoverable.append(record)
    return recoverable, dict(reasons.most_common())


def _reset_batch(records: list[dict]) -> int:
    """Set Status=Approved and clear Error for a batch of <=10 records."""
    body = {"records": [{"id": r["id"],
                         "fields": {"Status": config.AIRTABLE_STATUS_APPROVED,
                                    "Error": ""}} for r in records],
            "typecast": True}
    response = request_with_retry("PATCH", airtable_client._base_url(),
                                  headers=airtable_client._headers(), json_body=body)
    return len((airtable_client.safe_json(response) or {}).get("records", []))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="perform the writes (default: dry run, zero writes)")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap the number of rows reset (0 = no cap)")
    args = parser.parse_args(argv)

    recoverable, reasons = select_recoverable()
    if args.limit:
        recoverable = recoverable[: args.limit]

    print("=== Airtable Error backlog by reason ===")
    for reason, count in reasons.items():
        marker = "  <-- RECOVERABLE" if reason == STALE_ERROR[:80] else ""
        print(f"  {count:>5}  {reason}{marker}")
    print(f"\nrows matching the exact stale-validation error: {len(recoverable)}")
    print(f"destination: base={config.AIRTABLE_BASE_ID} table={config.AIRTABLE_TABLE_NAME}")

    if not args.apply:
        print("\nDRY RUN - no writes performed. Re-run with --apply to reset these rows.")
        print(json.dumps({"would_reset": len(recoverable),
                          "sample_ids": [r["id"] for r in recoverable[:10]]}, indent=2))
        return 0

    reset = 0
    for index in range(0, len(recoverable), 10):
        reset += _reset_batch(recoverable[index:index + 10])
    print(f"\nreset {reset} rows to Status={config.AIRTABLE_STATUS_APPROVED} "
          f"(Error cleared). Instantly was NOT called.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    raise SystemExit(main())
