"""PHASE 1 (READ-ONLY): re-evaluate every existing Fantastic Airtable row against
the authoritative send_safe_facts rule. No writes. No Fantastic/Apollo calls."""
from __future__ import annotations
import json, collections, sys
import config
from airtable_client import _base_url, _headers, send_safe_facts
from http_utils import request_with_retry, safe_json

WRITE = "--write" in sys.argv  # PHASE 2 backfill flip (only with --write)


def is_fantastic(fields):
    for k in ("Job URL", "Official Source", "Job URL Source"):
        if "linkedin.com/jobs/view" in str(fields.get(k) or "").lower():
            return True
    return False


def fetch_all():
    rows, offset = [], None
    while True:
        params = [("pageSize", 100)]
        if offset:
            params.append(("offset", offset))
        data = safe_json(request_with_retry("GET", _base_url(), headers=_headers(), params=params))
        for rec in data.get("records", []):
            rows.append({"id": rec.get("id"), "fields": rec.get("fields") or {}})
        offset = data.get("offset")
        if not offset:
            break
    return rows


def run():
    rows = fetch_all()
    fant = [r for r in rows if is_fantastic(r["fields"])]
    print(f"TOTAL_TABLE_ROWS={len(rows)} FANTASTIC_ROWS={len(fant)}")

    by_status = collections.Counter(str(r["fields"].get("Status") or "?") for r in fant)
    print("FANTASTIC by Status:", dict(by_status))

    A = B = C = D = E = 0
    d_reasons = collections.Counter()
    flip_ids = []  # Pending + send-safe -> backfill candidates
    for r in fant:
        f = r["fields"]
        status = str(f.get("Status") or "")
        fd = str(f.get("Final Decision") or "")
        # E: already delivered/contacted or already approved -> never re-approve here
        if status in (config.AIRTABLE_STATUS_ENROLLED, config.AIRTABLE_STATUS_APPROVED):
            E += 1
            continue
        ok, reason = send_safe_facts(f)
        if ok:
            if fd == "FINAL_PASS":
                A += 1
            elif fd == "NEEDS_CHECK":
                B += 1
            elif fd == "UNVERIFIED":
                C += 1
            else:
                A += 1
            if status == config.AIRTABLE_STATUS_PENDING:
                flip_ids.append(r["id"])
        else:
            D += 1
            d_reasons[reason] += 1

    print(f"A_final_pass_send_safe={A}")
    print(f"B_needs_check_now_send_safe={B}")
    print(f"C_unverified_now_send_safe={C}")
    print(f"D_genuinely_unsafe={D}")
    print(f"E_already_approved_or_enrolled={E}")
    print(f"D_blocker_distribution={dict(d_reasons.most_common())}")
    print(f"PENDING_SEND_SAFE_BACKFILL_CANDIDATES={len(flip_ids)}")

    if WRITE and flip_ids:
        done = backfill_flip_to_approved(flip_ids)
        print(f"BACKFILL_FLIPPED_TO_APPROVED={done}")


def backfill_flip_to_approved(flip_ids) -> int:
    """PHASE 2: flip ONLY Status Pending -> Approved for the given send-safe row
    ids, in batches of 10 (Airtable's PATCH limit). Nothing else is written --
    the row's disposition/evidence/canonical fields are untouched. Uses the
    established request helper contract (``json_body=`` on request_with_retry;
    ``_headers()`` already carries Content-Type)."""
    approved = config.AIRTABLE_STATUS_APPROVED
    done = 0
    for i in range(0, len(flip_ids), 10):
        chunk = flip_ids[i:i + 10]
        body = {"records": [{"id": rid, "fields": {"Status": approved}} for rid in chunk]}
        resp = request_with_retry("PATCH", _base_url(), headers=_headers(), json_body=body)
        safe_json(resp)
        done += len(chunk)
    return done


if __name__ == "__main__":
    run()
