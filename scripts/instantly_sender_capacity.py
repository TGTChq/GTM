"""Sender capacity: measure what the inboxes can carry, and set it deliberately.

Measured 2026-09-24. 252 inboxes, every one at 20 a day, sending Monday to Friday
08:00-18:00 America/Chicago: **25,200 emails a week**. A four-step sequence at an intake
of 1,000 leads a day needs 7,000 x 4 = **28,000 a week**. The intake the current setting
can actually carry is therefore **900 a day**, and anything above it becomes an unsent
queue that only grows (it was already 2,234 contacts on that date).

Raising the per-inbox figure is the only lever that stays inside the existing senders --
no new domains, no new inboxes, no change to the sending window. 28,000 / 5 days / 252
inboxes = 22.2, so 23 carries 1,000 a day with a small margin.

Whether that is safe is a measurement, not an opinion, and `--plan` prints it: inbox
age, warmup score, bounce rate and the gap between sends (an 18-minute gap allows 33 a
day, so 23 is inside what the schedule already permits).

    python scripts/instantly_sender_capacity.py --plan
    python scripts/instantly_sender_capacity.py --set-daily-limit 23 --i-mean-it

It is reversible: run it again with the previous number.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests

BASE = "https://api.instantly.ai/api/v2"
SENDING_DAYS_PER_WEEK = 5
STEPS_PER_SEQUENCE = 4


def headers() -> Dict[str, str]:
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not key:
        sys.exit("INSTANTLY_API_KEY is not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def accounts(h) -> List[Dict[str, Any]]:
    out, after = [], None
    while True:
        params: Dict[str, Any] = {"limit": 100}
        if after:
            params["starting_after"] = after
        r = requests.get(BASE + "/accounts", headers=h, params=params, timeout=60)
        r.raise_for_status()
        body = r.json()
        items = body.get("items", body if isinstance(body, list) else [])
        out.extend(items)
        after = body.get("next_starting_after") if isinstance(body, dict) else None
        if not after or not items:
            return out


def plan(h, intake: int) -> Dict[str, Any]:
    rows = accounts(h)
    total = sum(int(a.get("daily_limit") or 0) for a in rows)
    need_week = intake * 7 * STEPS_PER_SEQUENCE
    have_week = total * SENDING_DAYS_PER_WEEK
    def bucket(score):
        try:
            return ">=90" if float(score) >= 90 else ("50-89" if float(score) >= 50 else "<50")
        except (TypeError, ValueError):
            return "none"
    return {
        "inboxes": len(rows),
        "daily_limit_distribution": dict(collections.Counter(int(a.get("daily_limit") or 0) for a in rows)),
        "sending_gap_minutes": dict(collections.Counter(a.get("sending_gap") for a in rows)),
        "warmup_score": dict(collections.Counter(bucket(a.get("stat_warmup_score")) for a in rows)),
        "oldest_inbox": min((str(a.get("timestamp_created")) for a in rows), default="")[:10],
        "emails_per_sending_day": total,
        "emails_per_week": have_week,
        "needed_per_week_at_intake": need_week,
        "intake_the_current_setting_carries": have_week // (7 * STEPS_PER_SEQUENCE),
        "per_inbox_needed_for_intake": -(-need_week // (SENDING_DAYS_PER_WEEK * max(1, len(rows)))),
        "gap_allows_per_day": [int(600 // int(g)) for g in {a.get("sending_gap") for a in rows} if g],
    }


def apply_limit(h, value: int, ledger: Path) -> Dict[str, Any]:
    rows = accounts(h)
    changed, failed, unchanged = 0, [], 0
    for a in rows:
        email = str(a.get("email") or "")
        if int(a.get("daily_limit") or 0) == value:
            unchanged += 1
            continue
        r = requests.patch(f"{BASE}/accounts/{email}", headers=h, json={"daily_limit": value}, timeout=60)
        ok = 200 <= r.status_code < 300
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "account_sha": hash(email) & 0xffffff,
                                 "from": a.get("daily_limit"), "to": value, "http": r.status_code,
                                 "ok": ok, "body": (r.text or "")[:120]}) + "\n")
        if ok:
            changed += 1
        else:
            failed.append({"http": r.status_code, "body": (r.text or "")[:140]})
            if len(failed) >= 3:
                break
        time.sleep(0.1)
    return {"changed": changed, "unchanged": unchanged, "failures": failed[:3]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--intake", type=int, default=1000, help="target new leads per calendar day")
    ap.add_argument("--set-daily-limit", type=int, default=0)
    ap.add_argument("--i-mean-it", action="store_true")
    ap.add_argument("--ledger", default="instantly_sender_limit.jsonl")
    args = ap.parse_args()
    h = headers()

    if args.set_daily_limit:
        if not args.i_mean_it:
            print("refusing: --i-mean-it is required to change live sending")
            return 2
        if not 1 <= args.set_daily_limit <= 40:
            print("refusing: a per-inbox daily limit outside 1..40 is not a small change")
            return 2
        before = plan(h, args.intake)
        result = apply_limit(h, args.set_daily_limit, Path(args.ledger))
        after = plan(h, args.intake)
        print(json.dumps({"applied": result, "before": before, "after": after}, indent=1))
        return 0

    print(json.dumps(plan(h, args.intake), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
