"""Read the nine Challenger campaigns' sending configuration. Read-only.

Prints campaign name, status, daily_limit and the number of sending accounts,
never a lead and never the API key.
"""
from __future__ import annotations

import os
import sys

import requests

IDS = {
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
key = os.environ.get("INSTANTLY_API_KEY", "")
if not key:
    sys.exit("INSTANTLY_API_KEY not present")
base = os.environ.get("INSTANTLY_BASE_URL", "https://api.instantly.ai/api/v2").rstrip("/")
headers = {"Authorization": f"Bearer {key}"}
total_limit = 0
accounts = set()
for name, cid in IDS.items():
    r = requests.get(f"{base}/campaigns/{cid}", headers=headers, timeout=30)
    d = r.json() if r.ok else {}
    lim = d.get("daily_limit")
    senders = d.get("email_list") or []
    accounts.update(senders)
    total_limit += int(lim or 0)
    print(f"{name:<20} status={d.get('status')} daily_limit={lim} senders={len(senders)} "
          f"stop_on_reply={d.get('stop_on_reply')} steps={len(((d.get('sequences') or [{}])[0] or {}).get('steps') or [])} "
          f"delays={[s.get('delay') for s in (((d.get('sequences') or [{}])[0] or {}).get('steps') or [])]}")
print(f"sum of campaign daily_limit: {total_limit}")
print(f"distinct sending accounts across the nine: {len(accounts)}")
