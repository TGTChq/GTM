"""Remove backed-up, individually judged contacts -- and only those.

Input is `candidates.jsonl` from `instantly_rotation_export.py`: every row was written
after its full record was backed up, and carries its own verdict. This never decides
anything itself; it refuses a row that is not marked removable, re-checks the campaign
against the live campaign list, and refuses the nine Challenger campaigns by id.

The ledger is written BEFORE each call, so an interrupted session resumes without
repeating a deletion, and an id that is in the ledger is never attempted again.

    python scripts/instantly_rotation_apply.py --backup <dir> --canary --i-mean-it
    python scripts/instantly_rotation_apply.py --backup <dir> --max 1500 --i-mean-it

Deletion is permanent in Instantly. The backup is the only copy of what went.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

import requests

BASE = "https://api.instantly.ai/api/v2"
CHALLENGER = {
    "269cd138-00b1-48c3-9093-16c36120a20e", "c3e81c21-db44-40f5-addc-d9945a78394b",
    "8bfa0769-4b9a-4346-8e93-17ac8b726dce", "7b319c7a-cc55-4e08-8a47-7058c345d8ae",
    "8f25abd5-568a-4e88-b310-9acf85161c6c", "1feb6344-6065-49d7-9764-d125985fb9c9",
    "69def27c-7799-41a2-9ba8-205e54ab071b", "d2326028-e312-405b-9e16-526bd309d4dd",
    "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0",
}
CAMPAIGN_COMPLETED = 3


def headers() -> Dict[str, str]:
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not key:
        sys.exit("INSTANTLY_API_KEY is not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def delete_headers() -> Dict[str, str]:
    """A DELETE carries no body, and the API refuses a JSON content type without one
    (FST_ERR_CTP_EMPTY_JSON_BODY)."""
    return {"Authorization": headers()["Authorization"]}


def live_campaign_state(h) -> Dict[str, int]:
    out, after = {}, None
    while True:
        params: Dict[str, Any] = {"limit": 100}
        if after:
            params["starting_after"] = after
        r = requests.get(BASE + "/campaigns", headers=h, params=params, timeout=60)
        r.raise_for_status()
        body = r.json()
        items = body.get("items", body if isinstance(body, list) else [])
        for c in items:
            out[str(c.get("id"))] = c.get("status")
        after = body.get("next_starting_after") if isinstance(body, dict) else None
        if not after or not items:
            return out


def stored_total(h) -> int:
    r = requests.get(BASE + "/campaigns/analytics", headers=h, timeout=60)
    r.raise_for_status()
    rows = r.json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    return sum(int(x.get("leads_count") or 0) for x in rows)


def load_candidates(backup: Path) -> List[Dict[str, Any]]:
    rows = [json.loads(line) for line in (backup / "candidates.jsonl").open(encoding="utf-8")]
    return [r for r in rows if r.get("removable")]


def done_ids(ledger: Path) -> Set[str]:
    """Ids that must not be attempted again: one that was removed, and one whose attempt
    has no recorded outcome (the process died mid-call, so we cannot know). A refusal
    that provably changed nothing stays retryable."""
    if not ledger.exists():
        return set()
    last: Dict[str, str] = {}
    for line in ledger.open(encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        last[row["lead_id"]] = row.get("state", "")
    return {lead for lead, state in last.items() if state in ("removed", "attempting")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", required=True)
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--canary", action="store_true", help="exactly one legacy lead, then measure the counter")
    ap.add_argument("--group", default="legacy_completed", choices=("legacy_completed", "control_completed", "any"))
    ap.add_argument("--i-mean-it", action="store_true")
    ap.add_argument("--settle-seconds", type=int, default=0, help="wait before re-reading the counter")
    ap.add_argument("--sleep", type=float, default=0.12, help="pause between calls")
    args = ap.parse_args()
    if not args.i_mean_it:
        print("refusing: --i-mean-it is required")
        return 2
    if args.canary:
        args.max, args.group = 1, "legacy_completed"
    if args.max < 1:
        print("refusing: --max must be at least 1")
        return 2

    backup = Path(args.backup)
    ledger = backup / "removed.jsonl"
    h, dh = headers(), delete_headers()
    live = live_campaign_state(h)
    already = done_ids(ledger)
    pool = [r for r in load_candidates(backup)
            if r["lead_id"] not in already and (args.group == "any" or r["group"] == args.group)]
    # oldest contact first: the least recently touched people go before anyone else
    pool.sort(key=lambda r: (str(r.get("last_contact") or ""), str(r.get("created") or "")))

    before = stored_total(h)
    removed, failed = 0, []
    for row in pool:
        if removed >= args.max:
            break
        cid = row["campaign"]
        if cid in CHALLENGER or live.get(cid) != CAMPAIGN_COMPLETED:
            failed.append({"campaign": cid[:8], "why": f"campaign_state:{live.get(cid)}"})
            continue
        with ledger.open("a", encoding="utf-8") as fh:                       # intent, before the call
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "lead_id": row["lead_id"],
                                 "campaign": cid, "group": row["group"], "state": "attempting"}) + "\n")
        r = requests.delete(f"{BASE}/leads/{row['lead_id']}", headers=dh, timeout=60)
        ok = 200 <= r.status_code < 300
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "lead_id": row["lead_id"],
                                 "campaign": cid, "group": row["group"],
                                 "state": "removed" if ok else "failed", "http": r.status_code,
                                 "body": (r.text or "")[:120]}) + "\n")
        if ok:
            removed += 1
        else:
            failed.append({"http": r.status_code, "body": (r.text or "")[:120]})
            if len(failed) >= 3:
                break
        time.sleep(max(0.0, args.sleep))

    if args.settle_seconds:
        time.sleep(args.settle_seconds)
    after = stored_total(h)
    print(json.dumps({"removed": removed, "failures": failed[:3], "pool_left": max(0, len(pool) - removed),
                      "stored_before": before, "stored_after": after, "counter_delta": before - after,
                      "settled_seconds": args.settle_seconds, "ledger": str(ledger)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
