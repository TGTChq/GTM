"""Raise the per-campaign ceiling on NEW leads contacted per day, with evidence.

Measured 2026-09-22..24: the nine Challenger campaigns started 775-779 new leads a day
against a `daily_max_leads` total of 1,150, and the three campaigns that actually had
inventory sat exactly on their ceiling every single day -- OPERATIONS 350/350, AI &
TECHNICAL 100/100, FINANCE 100/100. The inboxes were never the constraint: 252 accounts
at 20 a day is 5,040 emails, and the campaigns were allowed to start at most 1,150.

So this raises `daily_max_leads` and nothing else. It does NOT touch the per-inbox daily
limit, the copy, the schedule, or any Control campaign -- the nine Challenger ids are an
allow-list and anything else is refused. Emails per day stay bounded by what already
bounds them: each account's own 20, and each campaign's `daily_limit`.

    python scripts/instantly_campaign_intake.py --plan
    python scripts/instantly_campaign_intake.py --apply --i-mean-it

Reversible: run it again with the previous numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict

import requests

BASE = "https://api.instantly.ai/api/v2"

#: The nine Challenger campaigns and the ceiling each one should carry. OPERATIONS holds
#: most of the inventory (2,525 of 4,598 creations) and has 80 inboxes, so it gets the
#: larger share; the rest are lifted off 100 so supply, not the ceiling, decides.
TARGET: Dict[str, Dict[str, object]] = {
    "69def27c-7799-41a2-9ba8-205e54ab071b": {"name": "OPERATIONS", "daily_max_leads": 600},
    "8bfa0769-4b9a-4346-8e93-17ac8b726dce": {"name": "AI & TECHNICAL", "daily_max_leads": 250},
    "7b319c7a-cc55-4e08-8a47-7058c345d8ae": {"name": "FINANCE", "daily_max_leads": 250},
    "269cd138-00b1-48c3-9093-16c36120a20e": {"name": "CUSTOMER EXPERIENCE", "daily_max_leads": 250},
    "8f25abd5-568a-4e88-b310-9acf85161c6c": {"name": "GTM SYSTEMS", "daily_max_leads": 250},
    "1feb6344-6065-49d7-9764-d125985fb9c9": {"name": "MARKETING", "daily_max_leads": 250},
    "d2326028-e312-405b-9e16-526bd309d4dd": {"name": "PEOPLE & HR", "daily_max_leads": 250},
    "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0": {"name": "PRODUCT", "daily_max_leads": 250},
    "c3e81c21-db44-40f5-addc-d9945a78394b": {"name": "ECOMMERCE", "daily_max_leads": 250},
}


def headers() -> Dict[str, str]:
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not key:
        sys.exit("INSTANTLY_API_KEY is not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def read(h, cid: str) -> Dict[str, object]:
    r = requests.get(f"{BASE}/campaigns/{cid}", headers=h, timeout=45)
    r.raise_for_status()
    c = r.json()
    return {"name": c.get("name"), "status": c.get("status"), "daily_limit": c.get("daily_limit"),
            "daily_max_leads": c.get("daily_max_leads"), "inboxes": len(c.get("email_list") or [])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="", help="one campaign id, for a canary")
    ap.add_argument("--i-mean-it", action="store_true")
    args = ap.parse_args()
    h = headers()

    ids = [args.only] if args.only else list(TARGET)
    for cid in ids:
        if cid not in TARGET:
            sys.exit(f"refusing {cid}: not one of the nine Challenger campaigns")

    out = []
    for cid in ids:
        before = read(h, cid)
        want = int(TARGET[cid]["daily_max_leads"])
        row = {"campaign": before["name"], "inboxes": before["inboxes"],
               "daily_limit": before["daily_limit"], "before": before["daily_max_leads"], "target": want}
        if args.apply:
            if not args.i_mean_it:
                sys.exit("refusing to change live campaigns without --i-mean-it")
            r = requests.patch(f"{BASE}/campaigns/{cid}", headers=h, json={"daily_max_leads": want}, timeout=45)
            row["http"] = r.status_code
            row["body"] = (r.text or "")[:120] if r.status_code >= 300 else ""
            row["after"] = read(h, cid)["daily_max_leads"]
            row["applied"] = row["after"] == want
        out.append(row)
    print(json.dumps({"campaigns": out,
                      "total_before": sum(int(r["before"] or 0) for r in out),
                      "total_target": sum(int(r["target"]) for r in out)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
