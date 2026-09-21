"""Share healthy sender accounts from low-demand Challenger pools into OPERATIONS.

Dry run by default; ``--apply`` writes. Prints counts only, never an address.

Add-only: a donated account stays in its original campaign, so a lead already
contacted from it keeps its sender and thread. Every account keeps its own
daily_limit (the provider-safe per-sender ceiling); only the OPERATIONS campaign
daily_limit is raised, to exactly (assigned accounts x lowest sender limit).
Nothing else on any campaign is written: no copy, steps, schedule or identity.

Usage: python instantly_reallocate_operations.py <snapshot_before.json> [--apply]
Rollback: python instantly_reallocate_operations.py - --rollback
  Needs no snapshot: Control OPERATIONS (read only, never written) holds exactly the
  original 33 OPERATIONS senders, so rollback restores that list, daily_limit 550
  and daily_max_leads 100. Leads already in the campaign are untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import requests

OPERATIONS = "69def27c-7799-41a2-9ba8-205e54ab071b"
NEW_LEADS_PER_DAY = 350
CONTROL_OPERATIONS = "4effab2f-9073-46a9-b7ae-986ccc8f49c6"  # read-only source for rollback
#: donor Challenger campaign -> accounts shared into OPERATIONS. Chosen by spare
#: capacity at the measured 2026-09-21 mix scaled to 1,000/day (per-account load:
#: PRODUCT 3.3, CX 4.7, MARKETING 8.5, GTM 8.7 of 20). FINANCE, AI_TECHNICAL,
#: ECOMMERCE and PEOPLE_HR are not donors: their pools are near demand.
DONORS = {
    "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0": 20,  # PRODUCT
    "269cd138-00b1-48c3-9093-16c36120a20e": 17,  # CUSTOMER_EXPERIENCE
    "1feb6344-6065-49d7-9764-d125985fb9c9": 5,   # MARKETING_CREATIVE
    "8f25abd5-568a-4e88-b310-9acf85161c6c": 5,   # GTM_SYSTEMS
}
#: Fields that must be byte-identical before and after (everything but the two written).
_VOLATILE = {"email_list", "daily_limit", "daily_max_leads", "timestamp_updated", "timestamp_created", "status",
             "timestamp_leads_updated"}

key = os.environ["INSTANTLY_API_KEY"]
base = os.environ.get("INSTANTLY_BASE_URL", "https://api.instantly.ai/api/v2").rstrip("/")
H = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def get(cid):
    r = requests.get(f"{base}/campaigns/{cid}", headers=H, timeout=60)
    r.raise_for_status()
    return r.json()


def fingerprint(camp):
    kept = {k: v for k, v in camp.items() if k not in _VOLATILE}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, default=str).encode()).hexdigest()[:16]


def main():
    mode = sys.argv[2] if len(sys.argv) > 2 else "--dry-run"
    if mode == "--rollback":
        original = list(get(CONTROL_OPERATIONS).get("email_list") or [])
        if len(original) != 33:
            sys.exit(f"Control OPERATIONS has {len(original)} senders, expected 33; refusing")
        body = {"email_list": original, "daily_limit": 550, "daily_max_leads": 100}
        r = requests.patch(f"{base}/campaigns/{OPERATIONS}", headers=H, json=body, timeout=60)
        after = get(OPERATIONS)
        print("rollback", r.status_code, "senders", len(after.get("email_list") or []), "daily_limit", after.get("daily_limit"),
              "daily_max_leads", after.get("daily_max_leads"))
        return
    snap = json.load(open(sys.argv[1], encoding="utf-8"))
    accounts = {a["email"]: a for a in snap["accounts"]}
    ops = get(OPERATIONS)
    before = snap["campaigns"][OPERATIONS]
    current = list(ops.get("email_list") or [])
    if sorted(current) != sorted(before["email_list"]) or ops.get("daily_limit") != before["daily_limit"]:
        sys.exit("OPERATIONS drifted from the snapshot; refusing to write")
    donated = []
    for donor, n in DONORS.items():
        pool = [e for e in snap["campaigns"][donor]["email_list"] if e not in current and e not in donated]
        healthy = [e for e in pool if accounts.get(e, {}).get("status") == 1 and accounts[e].get("warmup_status") == 1
                   and (accounts[e].get("stat_warmup_score") or 0) >= 90]
        # Spread across domains: take one account per domain in turn.
        by_domain = {}
        for e in sorted(healthy):
            by_domain.setdefault(e.split("@")[1], []).append(e)
        picked = []
        while len(picked) < n and any(by_domain.values()):
            for dom in sorted(by_domain):
                if by_domain[dom] and len(picked) < n:
                    picked.append(by_domain[dom].pop(0))
        if len(picked) < n:
            sys.exit(f"donor {donor[:8]} has only {len(picked)} healthy accounts")
        donated += picked
    new_list = current + donated
    per_sender = min(int(accounts[e]["daily_limit"]) for e in new_list)
    new_limit = per_sender * len(new_list)
    # New leads per day: prioritize_new_leads is on and there are 4 steps, so the
    # steady state is 4 x new/day. Donor campaigns keep using the shared accounts
    # (~230 sends/day at the 1,000/day mix), so 1,600 nominal supports ~1,400.
    new_max_leads = NEW_LEADS_PER_DAY
    print(f"OPERATIONS senders {len(current)} -> {len(new_list)} (+{len(donated)} shared, add-only); "
          f"lowest sender limit {per_sender}; campaign daily_limit {ops.get('daily_limit')} -> {new_limit}; "
          f"daily_max_leads {ops.get('daily_max_leads')} -> {new_max_leads}")
    for donor, n in DONORS.items():
        print(f"  donor {snap['campaigns'][donor]['name']:<20} shares {n} of {len(snap['campaigns'][donor]['email_list'])}")
    fp_before = fingerprint(ops)
    print("OPERATIONS config fingerprint (excl. senders/limit):", fp_before)
    print("daily_max_leads:", ops.get("daily_max_leads"), "| schedule timezones:",
          sorted({s.get("timezone") for s in (ops.get("campaign_schedule") or {}).get("schedules", [])}))
    if mode != "--apply":
        print("dry run: nothing written")
        return
    r = requests.patch(f"{base}/campaigns/{OPERATIONS}", headers=H,
                       json={"email_list": new_list, "daily_limit": new_limit, "daily_max_leads": new_max_leads}, timeout=60)
    print("patch status", r.status_code)
    after = get(OPERATIONS)
    fp_after = fingerprint(after)
    ok = (sorted(after.get("email_list") or []) == sorted(new_list) and after.get("daily_limit") == new_limit
          and after.get("daily_max_leads") == new_max_leads and fp_after == fp_before)
    print(f"verify: senders={len(after.get('email_list') or [])} daily_limit={after.get('daily_limit')} "
          f"daily_max_leads={after.get('daily_max_leads')} "
          f"config_unchanged={fp_after == fp_before} -> {'OK' if ok else 'MISMATCH'}")
    for donor in DONORS:
        d = get(donor)
        print(f"  donor {snap['campaigns'][donor]['name']:<20} senders still {len(d.get('email_list') or [])}")


if __name__ == "__main__":
    main()
