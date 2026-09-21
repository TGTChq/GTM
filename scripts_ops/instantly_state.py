"""Read-only Instantly state for the nine Challenger (and nine Control) campaigns.

Counts only -- never an address, a name or the API key. Writes a JSON snapshot:

  per campaign: leads, contacted (received at least one email = entered active
  outreach), not_contacted (enrolled, waiting for a first email = Instantly
  backlog), completed, daily {date: {sent, new_leads_contacted}} for a date range,
  daily_limit, daily_max_leads, senders (count), schedule days.

Usage: python instantly_state.py <out.json> [start_date end_date]
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

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
#: Read only here, never written: residual follow-up load on the shared senders.
CONTROL = {
    "PRODUCT": "45ac1e03-67e7-4bdd-b372-808042104e4c",
    "OPERATIONS": "4effab2f-9073-46a9-b7ae-986ccc8f49c6",
    "FINANCE": "1db88bbe-b2cf-4574-a5b7-1cb948151a86",
    "PEOPLE_HR": "cf01e56b-e5ad-489e-a02c-c35c93cf3b53",
    "ECOMMERCE": "0f0f57d5-fab1-436d-b0d8-8cb43b031f03",
    "CUSTOMER_EXPERIENCE": "1747c87e-12e9-4477-bc4d-048223d39513",
    "MARKETING_CREATIVE": "165c9e87-c3e7-4e9c-9ccb-a8dbf5779726",
    "GTM_SYSTEMS": "917973f3-c282-4a84-8da4-525a7a91819b",
    "AI_TECHNICAL": "04670c6a-828b-42cd-9dad-904592a63d9b",
}

key = os.environ["INSTANTLY_API_KEY"]
base = os.environ.get("INSTANTLY_BASE_URL", "https://api.instantly.ai/api/v2").rstrip("/")
H = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def count(campaign_id: str, flt: str | None) -> int:
    n, after = 0, None
    for _ in range(500):
        body = {"campaign": campaign_id, "limit": 100}
        if flt:
            body["filter"] = flt
        if after:
            body["starting_after"] = after
        r = requests.post(f"{base}/leads/list", headers=H, json=body, timeout=60)
        r.raise_for_status()
        d = r.json()
        items = d.get("items") or []
        n += len(items)
        after = d.get("next_starting_after")
        if not after or not items:
            return n
    raise RuntimeError("lead paging did not terminate")


def campaign_ids():
    out = {("challenger", n): c for n, c in CHALLENGER.items()}
    out.update({("control", n): c for n, c in CONTROL.items()})
    return out


def main():
    out_path = sys.argv[1]
    start, end = (sys.argv[2], sys.argv[3]) if len(sys.argv) > 3 else (None, None)
    result = {"taken_at": datetime.now(timezone.utc).isoformat(), "campaigns": {}}
    for (group, name), cid in campaign_ids().items():
        camp = requests.get(f"{base}/campaigns/{cid}", headers=H, timeout=60).json()
        sched = ((camp.get("campaign_schedule") or {}).get("schedules") or [{}])[0]
        row = {"group": group, "name": name, "status": camp.get("status"),
               "daily_limit": camp.get("daily_limit"), "daily_max_leads": camp.get("daily_max_leads"),
               "senders": len(camp.get("email_list") or []),
               "send_days": sorted(k for k, v in (sched.get("days") or {}).items() if v)}
        if group == "challenger":
            row["leads"] = count(cid, None)
            row["contacted"] = count(cid, "FILTER_VAL_CONTACTED")
            row["not_contacted"] = count(cid, "FILTER_VAL_NOT_CONTACTED")
            row["completed"] = count(cid, "FILTER_VAL_COMPLETED")
        else:
            row["active"] = count(cid, "FILTER_VAL_ACTIVE")
        if start:
            r = requests.get(f"{base}/campaigns/analytics/daily", headers=H,
                             params={"campaign_id": cid, "start_date": start, "end_date": end}, timeout=60)
            days = r.json() if r.ok and isinstance(r.json(), list) else []
            row["daily"] = {d["date"]: {"sent": d.get("sent", 0), "new_leads_contacted": d.get("new_leads_contacted", 0)}
                            for d in days}
        result["campaigns"][cid] = row
    json.dump(result, open(out_path, "w", encoding="utf-8"), indent=1)
    print(f"{'campaign':<22}{'leads':>7}{'contacted':>10}{'waiting':>9}{'max_new':>8}{'limit':>7}{'senders':>8}")
    for cid, r in result["campaigns"].items():
        if r["group"] == "challenger":
            print(f"{r['name']:<22}{r['leads']:>7}{r['contacted']:>10}{r['not_contacted']:>9}"
                  f"{str(r['daily_max_leads']):>8}{str(r['daily_limit']):>7}{r['senders']:>8}")
    ctrl = sum(r.get("active", 0) for r in result["campaigns"].values() if r["group"] == "control")
    print(f"control campaigns: {ctrl} leads still active (residual follow-up load)")


if __name__ == "__main__":
    main()
