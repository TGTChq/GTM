"""Read-only: lead queue state for the nine Challenger and nine Control campaigns.

Uses the campaign analytics endpoint. Prints counts only.
"""
from __future__ import annotations

import os
import sys

import requests

import json

_SNAP = sys.argv[1] if len(sys.argv) > 1 else ""
_camps = json.load(open(_SNAP, encoding="utf-8"))["campaigns"] if _SNAP else {}
CHALLENGER = {v["name"]: k for k, v in _camps.items() if v["group"] == "challenger"}
CONTROL = {v["name"]: k for k, v in _camps.items() if v["group"] == "control"}

key = os.environ["INSTANTLY_API_KEY"]
base = os.environ.get("INSTANTLY_BASE_URL", "https://api.instantly.ai/api/v2").rstrip("/")
H = {"Authorization": f"Bearer {key}"}

ids = [("challenger", n, c) for n, c in CHALLENGER.items()] + [("control", n, c) for n, c in CONTROL.items()]
rows = {}
for _, _, cid in ids:
    r = requests.get(f"{base}/campaigns/analytics", headers=H, params={"id": cid}, timeout=60)
    body = r.json() if r.ok else []
    item = body[0] if isinstance(body, list) and body else (body if isinstance(body, dict) else {})
    if not r.ok:
        print("analytics", cid, r.status_code, r.text[:160])
    rows[cid] = item
fields = ("leads_count", "contacted_count", "completed_count", "emails_sent_count", "reply_count", "bounced_count",
          "unsubscribed_count")
print(f"{'group':<10} {'campaign':<20} " + " ".join(f"{f.replace('_count',''):>10}" for f in fields) + "   in_sequence")
for group, name, cid in ids:
    x = rows.get(cid, {})
    vals = [x.get(f) for f in fields]
    leads, completed = x.get("leads_count") or 0, x.get("completed_count") or 0
    print(f"{group:<10} {name:<20} " + " ".join(f"{str(v):>10}" for v in vals) + f"   {leads - completed:>10}")
