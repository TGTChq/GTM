"""Rotation of finished Instantly contacts. Reads by default; removes only when told.

The workspace holds 25,000 contacts against an allowance of 25,000 (measured
2026-09-24, see INSTANTLY_CAPACITY.md). The nine live campaigns are 5,189 of that;
the rest is finished work from campaigns that no longer send anything.

Nothing here runs by accident:

* ``--plan`` (the default) makes no change at all;
* a removal needs ``--i-mean-it`` AND an explicit ``--max``;
* the nine Challenger campaigns and the active Control campaign are refused by id,
  whatever else is passed;
* only a campaign whose own status is `completed` is eligible, and inside it only a
  contact that finished the sequence and never replied;
* every removed id is appended to a local JSONL before the next one is attempted, so
  an interrupted sweep is still a complete record of what went.

``--verify-one`` is the open question in one contact: remove a single finished legacy
contact and read the workspace total again. If the total drops, the allowance is a
stock that rotation can recover. If it does not, only a plan change can help.

    python scripts/instantly_rotation.py --plan
    python scripts/instantly_rotation.py --verify-one --i-mean-it
    python scripts/instantly_rotation.py --sweep legacy_completed --max 500 --i-mean-it

Run it with the workspace credentials, never with a key typed on a command line:

    railway run -s <a service holding INSTANTLY_API_KEY> -- python scripts/instantly_rotation.py --plan
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "https://api.instantly.ai/api/v2"

#: Live outreach. Never eligible, whatever a campaign's status says.
CHALLENGER = {
    "269cd138-00b1-48c3-9093-16c36120a20e", "c3e81c21-db44-40f5-addc-d9945a78394b",
    "8bfa0769-4b9a-4346-8e93-17ac8b726dce", "7b319c7a-cc55-4e08-8a47-7058c345d8ae",
    "8f25abd5-568a-4e88-b310-9acf85161c6c", "1feb6344-6065-49d7-9764-d125985fb9c9",
    "69def27c-7799-41a2-9ba8-205e54ab071b", "d2326028-e312-405b-9e16-526bd309d4dd",
    "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0",
}
CONTROL = {
    "45ac1e03-67e7-4bdd-b372-808042104e4c", "4effab2f-9073-46a9-b7ae-986ccc8f49c6",
    "1db88bbe-b2cf-4574-a5b7-1cb948151a86", "cf01e56b-e5ad-489e-a02c-c35c93cf3b53",
    "0f0f57d5-fab1-436d-b0d8-8cb43b031f03", "1747c87e-12e9-4477-bc4d-048223d39513",
    "165c9e87-c3e7-4e9c-9ccb-a8dbf5779726", "917973f3-c282-4a84-8da4-525a7a91819b",
    "04670c6a-828b-42cd-9dad-904592a63d9b",
}
CAMPAIGN_COMPLETED = 3          # the campaign itself is finished
LEAD_FINISHED = 3               # the contact reached the end of the sequence
GROUPS = ("legacy_completed", "control_completed")
LEDGER = Path("instantly_rotation_removed.jsonl")


def _headers() -> Dict[str, str]:
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not key:
        sys.exit("INSTANTLY_API_KEY is not set; run this through `railway run -s <service>`")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def campaigns(h) -> List[Dict[str, Any]]:
    out, after = [], None
    while True:
        params = {"limit": 100}
        if after:
            params["starting_after"] = after
        r = requests.get(BASE + "/campaigns", headers=h, params=params, timeout=60)
        r.raise_for_status()
        body = r.json()
        items = body.get("items", body if isinstance(body, list) else [])
        out.extend(items)
        after = body.get("next_starting_after") if isinstance(body, dict) else None
        if not after or not items:
            return out


def analytics(h) -> Dict[str, Dict[str, Any]]:
    r = requests.get(BASE + "/campaigns/analytics", headers=h, timeout=60)
    r.raise_for_status()
    rows = r.json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    return {str(x.get("campaign_id") or x.get("id")): x for x in rows}


def group_of(campaign: Dict[str, Any]) -> Optional[str]:
    cid = str(campaign.get("id") or "")
    if cid in CHALLENGER:
        return None
    if campaign.get("status") != CAMPAIGN_COMPLETED:
        return None
    return "control_completed" if cid in CONTROL else "legacy_completed"


def plan(h) -> Dict[str, Any]:
    stats = analytics(h)
    rows, totals = [], {g: {"campaigns": 0, "contacts": 0} for g in GROUPS}
    stored = 0
    for c in campaigns(h):
        cid = str(c.get("id") or "")
        held = int((stats.get(cid) or {}).get("leads_count") or 0)
        stored += held
        g = group_of(c)
        if not g:
            continue
        rows.append({"group": g, "id": cid, "name": str(c.get("name") or "")[:50], "contacts": held,
                     "finished": (stats.get(cid) or {}).get("completed_count")})
        totals[g]["campaigns"] += 1
        totals[g]["contacts"] += held
    rows.sort(key=lambda r: -r["contacts"])
    return {"stored_now": stored, "eligible": totals, "campaigns": rows,
            "frees_if_both_groups_go": sum(t["contacts"] for t in totals.values())}


def finished_leads(h, campaign_id: str, limit: int) -> List[Dict[str, Any]]:
    """Contacts that finished the sequence and never replied, in that campaign only."""
    out, after = [], None
    while len(out) < limit:
        payload: Dict[str, Any] = {"campaign": campaign_id, "limit": 100}
        if after:
            payload["starting_after"] = after
        r = requests.post(BASE + "/leads/list", headers=h, json=payload, timeout=60)
        r.raise_for_status()
        body = r.json()
        items = body.get("items", [])
        for lead in items:
            if lead.get("status") != LEAD_FINISHED:
                continue
            if lead.get("timestamp_last_reply") or int(lead.get("email_reply_count") or 0):
                continue
            out.append(lead)
            if len(out) >= limit:
                break
        after = body.get("next_starting_after")
        if not after or not items:
            break
    return out


def remove(h, lead: Dict[str, Any], campaign_id: str, group: str) -> Tuple[bool, str]:
    """Remove ONE contact, recording it before the next attempt is made."""
    lead_id = str(lead.get("id") or "")
    if not lead_id:
        return False, "no id"
    with LEDGER.open("a", encoding="utf-8") as fh:                     # record BEFORE removing
        fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "lead_id": lead_id,
                             "campaign": campaign_id, "group": group,
                             "created": lead.get("timestamp_created"),
                             "last_contact": lead.get("timestamp_last_contact")}) + "\n")
    r = requests.delete(f"{BASE}/leads/{lead_id}", headers=h, timeout=60)
    return (200 <= r.status_code < 300), f"{r.status_code}:{(r.text or '')[:120]}"


def eligible_campaigns(h, group: str) -> List[Dict[str, Any]]:
    return [c for c in campaigns(h) if group_of(c) == group]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true", help="read-only: what is eligible and how much it frees")
    ap.add_argument("--verify-one", action="store_true", help="remove ONE finished legacy contact and re-read the total")
    ap.add_argument("--sweep", choices=GROUPS, help="remove finished contacts of a whole group")
    ap.add_argument("--max", type=int, default=0, help="hard ceiling on contacts removed (required to remove)")
    ap.add_argument("--i-mean-it", action="store_true", help="required for any removal")
    args = ap.parse_args()
    h = _headers()

    if args.verify_one or args.sweep:
        if not args.i_mean_it:
            return int(bool(print("refusing to remove anything without --i-mean-it")))
        if args.verify_one:
            args.sweep, args.max = "legacy_completed", 1
        if args.max < 1:
            return int(bool(print("--max is required and must be at least 1")))
        before = plan(h)
        removed, failures = [], []
        for c in sorted(eligible_campaigns(h, args.sweep), key=lambda x: str(x.get("name") or "")):
            if len(removed) >= args.max:
                break
            cid = str(c["id"])
            assert cid not in CHALLENGER, "refusing a live campaign"
            for lead in finished_leads(h, cid, args.max - len(removed)):
                ok, detail = remove(h, lead, cid, args.sweep)
                (removed if ok else failures).append({"campaign": str(c.get("name"))[:40], "detail": detail})
                if not ok:
                    break
            if failures:
                break
        after = plan(h)
        print(json.dumps({"removed": len(removed), "failures": failures[:3],
                          "stored_before": before["stored_now"], "stored_after": after["stored_now"],
                          "slots_returned": before["stored_now"] - after["stored_now"],
                          "ledger": str(LEDGER)}, indent=1))
        return 0

    print(json.dumps(plan(h), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
