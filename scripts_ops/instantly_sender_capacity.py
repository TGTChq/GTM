"""Read-only: sender-account capacity behind the nine Challenger campaigns.

Lists every sending account (GET /accounts, paginated) with its daily limit,
warmup and status, and every campaign's sender list, then computes per-campaign
capacity. An account's daily limit is shared by every ACTIVE campaign it sends
for, so a campaign's usable capacity is its share of each assigned account.

Prints sender DOMAINS and counts only, never a full address, and never the key.
Writes the full snapshot to the path in argv[1] (for rollback and planning).
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

import requests

CHALLENGER = {
    "PRODUCT": "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0",
    "OPERATIONS": "69def27c-7799-41a2-9ba8-205e54ab071b",
    "FINANCE": "7b319c7a-cc55-4e08-8a47-7058c345d8ae",
    "PEOPLE_HR": "d2326028-e312-405b-9e16-526bd309d4dd",
    "ECOMMERCE": "c3e81c21-db44-40f5-addc-d9945a78394b",
    "CUSTOMER_EXPERIENCE": "269cd138-00b1-48c3-9093-16c36120a20e",
    "MARKETING_CREATIVE": "1feb6344-6065-49d7-9764-d125985fb9c9",
    "GTM_SYSTEMS": "8f25abd5-568a-4e88-b310-9acf85161c6c",
    "AI_TECHNICAL": "8bfa0769-4b9a-4346-8e93-17ac8b726dce",
}
CONTROL = {
    "PRODUCT": "45ac1e03-67e7-4bdd-b372-808042104e4c", "OPERATIONS": "4effab2f-9073-46a9-b7ae-986ccc8f49c6",
    "FINANCE": "1db88bbe-b2cf-4574-a5b7-1cb948151a86", "PEOPLE_HR": "cf01e56b-e5ad-489e-a02c-c35c93cf3b53",
    "ECOMMERCE": "0f0f57d5-fab1-436d-b0d8-8cb43b031f03", "CUSTOMER_EXPERIENCE": "1747c87e-12e9-4477-bc4d-048223d39513",
    "MARKETING_CREATIVE": "165c9e87-c3e7-4e9c-9ccb-a8dbf5779726", "GTM_SYSTEMS": "917973f3-c282-4a84-8da4-525a7a91819b",
    "AI_TECHNICAL": "04670c6a-828b-42cd-9dad-904592a63d9b",
}

key = os.environ.get("INSTANTLY_API_KEY", "")
if not key:
    sys.exit("INSTANTLY_API_KEY not present")
base = os.environ.get("INSTANTLY_BASE_URL", "https://api.instantly.ai/api/v2").rstrip("/")
H = {"Authorization": f"Bearer {key}"}

accounts, cursor = [], None
while True:
    params = {"limit": 100, **({"starting_after": cursor} if cursor else {})}
    r = requests.get(f"{base}/accounts", headers=H, params=params, timeout=30)
    r.raise_for_status()
    body = r.json()
    items = body.get("items") or []
    accounts.extend(items)
    cursor = body.get("next_starting_after")
    if not cursor or not items:
        break

campaigns = {}
for group, ids in (("challenger", CHALLENGER), ("control", CONTROL)):
    for name, cid in ids.items():
        c = requests.get(f"{base}/campaigns/{cid}", headers=H, timeout=30).json()
        campaigns[cid] = {"group": group, "name": name, "status": c.get("status"), "daily_limit": c.get("daily_limit"),
                          "email_list": [e.lower() for e in (c.get("email_list") or [])],
                          "steps": len(((c.get("sequences") or [{}])[0] or {}).get("steps") or [])}

by_email = {str(a.get("email") or "").lower(): a for a in accounts}
active_campaigns_of = defaultdict(list)
for cid, c in campaigns.items():
    if c["status"] == 1:  # active
        for e in c["email_list"]:
            active_campaigns_of[e].append(cid)

print(f"sending accounts in workspace: {len(accounts)}")
print("account status:", dict(Counter(a.get("status") for a in accounts)))
print("warmup status:", dict(Counter(a.get("warmup_status") for a in accounts)))
print("account daily_limit distribution:", dict(sorted(Counter(a.get("daily_limit") for a in accounts).items(), key=lambda kv: str(kv[0]))))
print()
print(f"{'group':<10} {'campaign':<20} {'st':>2} {'limit':>6} {'senders':>7} {'shared_capacity':>15} {'steps':>5}")
for cid, c in sorted(campaigns.items(), key=lambda kv: (kv[1]["group"], kv[1]["name"])):
    share = 0.0
    for e in c["email_list"]:
        a = by_email.get(e) or {}
        n = max(1, len(active_campaigns_of.get(e) or [cid]))
        share += float(a.get("daily_limit") or 0) / n
    c["shared_capacity"] = round(share, 1)
    print(f"{c['group']:<10} {c['name']:<20} {str(c['status']):>2} {str(c['daily_limit']):>6} {len(c['email_list']):>7} "
          f"{share:>15.1f} {c['steps']:>5}")
print()
print("accounts by number of ACTIVE campaigns they serve:",
      dict(sorted(Counter(len(active_campaigns_of.get(e, [])) for e in by_email).items())))
total_account_capacity = sum(float(a.get("daily_limit") or 0) for a in accounts
                             if str(a.get("status")) in ("1", "active") )
print(f"sum of daily_limit over ACTIVE accounts: {total_account_capacity:.0f}")

snapshot = {"accounts": [{"email": str(a.get("email") or "").lower(), "status": a.get("status"),
                           "warmup_status": a.get("warmup_status"), "daily_limit": a.get("daily_limit"),
                           "stat_warmup_score": a.get("stat_warmup_score")} for a in accounts],
            "campaigns": campaigns}
if len(sys.argv) > 1:
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=1)
    print(f"snapshot written ({len(accounts)} accounts, {len(campaigns)} campaigns)")
