"""Apply a capacity plan (capacity_plan.py output) to Instantly. Dry run by default.

Writes ONLY what the 2026-09-21 decision approved, and only to Challenger campaigns:
  * OPERATIONS: add the plan's shared senders (add-only), daily_limit, daily_max_leads;
  * FINANCE and AI_TECHNICAL: daily_max_leads, raised only (never lowered).
Every other campaign and every sender account is read, never written.

Before and after, all 18 campaigns are read and compared:
  * no Control campaign is written (the write set is asserted Challenger-only);
  * no sender is removed from any campaign; non-OPERATIONS sender lists are identical;
  * every field other than the written ones is fingerprint-identical (copy, sequences,
    schedule, tracking, identity);
  * every sender account's daily_limit is <= 20.
Prints counts only. The before-state is saved for rollback.

Usage:
  python instantly_apply_capacity.py <plan.json> <before_state_out.json> [--apply]
  python instantly_apply_capacity.py - <before_state.json> --rollback
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import requests

from instantly_state import CHALLENGER, CONTROL  # noqa: E402  (same directory)

OPERATIONS = CHALLENGER["OPERATIONS"]
RAISE_ONLY = {CHALLENGER["FINANCE"], CHALLENGER["AI_TECHNICAL"]}
WRITABLE = {OPERATIONS} | RAISE_ONLY
WRITTEN_FIELDS = {"email_list", "daily_limit", "daily_max_leads"}
VOLATILE = {"timestamp_updated", "timestamp_leads_updated", "status"}
MAX_SENDER_LIMIT = 20

assert WRITABLE <= set(CHALLENGER.values()) and not (WRITABLE & set(CONTROL.values()))

key = os.environ["INSTANTLY_API_KEY"]
base = os.environ.get("INSTANTLY_BASE_URL", "https://api.instantly.ai/api/v2").rstrip("/")
H = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def get(cid: str) -> dict:
    r = requests.get(f"{base}/campaigns/{cid}", headers=H, timeout=60)
    r.raise_for_status()
    return r.json()


def fingerprint(camp: dict) -> str:
    kept = {k: v for k, v in camp.items() if k not in WRITTEN_FIELDS | VOLATILE}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, default=str).encode()).hexdigest()[:16]


def read_all() -> dict:
    out = {}
    for group, ids in (("challenger", CHALLENGER), ("control", CONTROL)):
        for name, cid in ids.items():
            c = get(cid)
            out[cid] = {"group": group, "name": name, "email_list": list(c.get("email_list") or []),
                        "daily_limit": c.get("daily_limit"), "daily_max_leads": c.get("daily_max_leads"),
                        "fingerprint": fingerprint(c)}
    return out


def sender_limits() -> dict:
    limits, after = {}, None
    for _ in range(100):
        params = {"limit": 100, **({"starting_after": after} if after else {})}
        r = requests.get(f"{base}/accounts", headers=H, params=params, timeout=60)
        r.raise_for_status()
        d = r.json()
        for a in d.get("items") or []:
            limits[a["email"]] = a.get("daily_limit")
        after = d.get("next_starting_after")
        if not after or not d.get("items"):
            break
    return limits


def patch(cid: str, body: dict) -> int:
    assert cid in WRITABLE, "refusing to write a campaign outside the approved set"
    r = requests.patch(f"{base}/campaigns/{cid}", headers=H, json=body, timeout=60)
    return r.status_code


def verify(before: dict, after: dict, intended: dict) -> list:
    problems = []
    for cid, b in before.items():
        a = after[cid]
        removed = set(b["email_list"]) - set(a["email_list"])
        if removed:
            problems.append(f"{b['name']} ({b['group']}): {len(removed)} senders removed")
        if a["fingerprint"] != b["fingerprint"]:
            problems.append(f"{b['name']} ({b['group']}): non-capacity fields changed")
        if cid not in intended:
            for f in WRITTEN_FIELDS:
                if (sorted(a[f]) if f == "email_list" else a[f]) != (sorted(b[f]) if f == "email_list" else b[f]):
                    problems.append(f"{b['name']} ({b['group']}): {f} changed but was not in the plan")
        else:
            for f, v in intended[cid].items():
                got = sorted(a[f]) if f == "email_list" else a[f]
                if got != (sorted(v) if f == "email_list" else v):
                    problems.append(f"{b['name']}: {f} not as intended")
    return problems


def main():
    plan_path, state_path = sys.argv[1], sys.argv[2]
    mode = sys.argv[3] if len(sys.argv) > 3 else "--dry-run"
    if mode == "--rollback":
        saved = json.load(open(state_path, encoding="utf-8"))
        for cid in WRITABLE:
            b = saved[cid]
            body = {"email_list": b["email_list"], "daily_limit": b["daily_limit"], "daily_max_leads": b["daily_max_leads"]}
            print("rollback", b["name"], patch(cid, body))
        after = read_all()
        print("rollback problems:", verify(saved, after, {c: {f: saved[c][f] for f in WRITTEN_FIELDS} for c in WRITABLE}) or "none")
        return
    plan = json.load(open(plan_path, encoding="utf-8"))
    before = read_all()
    json.dump(before, open(state_path, "w", encoding="utf-8"), indent=1)
    limits = sender_limits()
    over = [e for e, lim in limits.items() if lim is None or int(lim) > MAX_SENDER_LIMIT]
    if over:
        sys.exit(f"{len(over)} sender(s) above {MAX_SENDER_LIMIT}/day before any write; refusing")

    ops_before = before[OPERATIONS]["email_list"]
    added = [a for a in plan["operations_added_senders"] if a not in ops_before]
    unknown = [a for a in added if a not in limits]
    if unknown:
        sys.exit(f"{len(unknown)} planned sender(s) are not accounts of this workspace; refusing")
    intended = {}
    ops_new = int(plan["campaigns"][OPERATIONS]["daily_max_leads"])
    if added or ops_new > int(before[OPERATIONS]["daily_max_leads"] or 0):
        # Raise only: the measurement must show OPERATIONS needs more than it has.
        intended[OPERATIONS] = {"email_list": ops_before + added,
                                "daily_limit": MAX_SENDER_LIMIT * (len(ops_before) + len(added)),
                                "daily_max_leads": max(ops_new, int(before[OPERATIONS]["daily_max_leads"] or 0))}
    for cid in RAISE_ONLY:
        new = int(plan["campaigns"][cid]["daily_max_leads"])
        if new > int(before[cid]["daily_max_leads"] or 0):
            intended[cid] = {"daily_max_leads": new}

    for cid, fields in intended.items():
        b = before[cid]
        desc = ", ".join(f"{f} {len(b[f]) if f == 'email_list' else b[f]} -> {len(v) if f == 'email_list' else v}"
                         for f, v in fields.items())
        print(f"{b['name']}: {desc}")
    print(f"senders checked: {len(limits)}, all <= {MAX_SENDER_LIMIT}/day; campaigns read: {len(before)}")
    if mode != "--apply":
        print("dry run: nothing written")
        return
    for cid, fields in intended.items():
        print("patch", before[cid]["name"], patch(cid, fields))
    after = read_all()
    json.dump(after, open(state_path.replace(".json", "_after.json"), "w", encoding="utf-8"), indent=1)
    problems = verify(before, after, intended)
    over_after = [e for e, lim in sender_limits().items() if lim is None or int(lim) > MAX_SENDER_LIMIT]
    if over_after:
        problems.append(f"{len(over_after)} sender(s) above {MAX_SENDER_LIMIT}/day")
    print("verification:", "OK -- Challenger-only writes, no sender removed, copy/sequences/schedule unchanged, "
          "every sender <= 20/day" if not problems else problems)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
