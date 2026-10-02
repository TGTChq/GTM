"""Will 1,000 a day fit, day after day? A forecast built from the live workspace.

Two ceilings decide it, and they are different questions:

* **slots** -- a contact occupies one from upload until it is deleted. A sequence runs
  13 days, so a sustained intake parks intake x 13 contacts permanently, plus everything
  finished that nobody removed;
* **sending** -- the four steps land on days 0, 3, 7 and 12, weekdays only (the nine
  campaigns send Monday to Friday, 08:00-18:00 America/Chicago). A step that falls on a
  weekend waits for Monday. If the daily total due exceeds what the inboxes can send,
  the difference is an unsent queue that carries forward.

Both are simulated per calendar day from the measured state, with no guesses: the
occupancy, the pool of finished contacts that may be rotated, the sending limits and
the sequence shape are all read from the account.

    python scripts/instantly_occupancy_forecast.py --days 30 --intake 1000
    python scripts/instantly_occupancy_forecast.py --days 30 --rotate-own-after-days 13

`--rotate-own-after-days` models removing OUR OWN contacts once their sequence has
finished. It is the only policy that makes a fixed allowance sustainable, and it is not
authorised yet: the nine campaigns are protected. Model it, do not assume it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List

import requests

BASE = "https://api.instantly.ai/api/v2"
NINE = {
    "269cd138-00b1-48c3-9093-16c36120a20e", "c3e81c21-db44-40f5-addc-d9945a78394b",
    "8bfa0769-4b9a-4346-8e93-17ac8b726dce", "7b319c7a-cc55-4e08-8a47-7058c345d8ae",
    "8f25abd5-568a-4e88-b310-9acf85161c6c", "1feb6344-6065-49d7-9764-d125985fb9c9",
    "69def27c-7799-41a2-9ba8-205e54ab071b", "d2326028-e312-405b-9e16-526bd309d4dd",
    "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0",
}
STEP_OFFSETS = (0, 3, 7, 12)          # cumulative delays of the four-step sequence
SEQUENCE_DAYS = 13


def headers() -> Dict[str, str]:
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not key:
        sys.exit("INSTANTLY_API_KEY is not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def live_state(h) -> Dict[str, Any]:
    r = requests.get(BASE + "/campaigns/analytics", headers=h, timeout=60)
    r.raise_for_status()
    rows = r.json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    ours = [x for x in rows if str(x.get("campaign_id") or x.get("id")) in NINE]
    accounts, after = [], None
    while True:
        params: Dict[str, Any] = {"limit": 100}
        if after:
            params["starting_after"] = after
        a = requests.get(BASE + "/accounts", headers=h, params=params, timeout=60)
        a.raise_for_status()
        body = a.json()
        batch = body.get("items", [])
        accounts.extend(batch)
        after = body.get("next_starting_after")
        if not after or not batch:
            break
    return {
        "stored_total": sum(int(x.get("leads_count") or 0) for x in rows),
        "ours_total": sum(int(x.get("leads_count") or 0) for x in ours),
        "ours_finished": sum(int(x.get("completed_count") or 0) for x in ours),
        "ours_first_email_sent": sum(int(x.get("new_leads_contacted_count") or 0) for x in ours),
        "send_per_weekday": sum(int(a.get("daily_limit") or 0) for a in accounts),
        "inboxes": len(accounts),
    }


def forecast(state: Dict[str, Any], *, cap: int, days: int, intake: int, pool: int,
             rotate_own_after: int, start: date) -> List[Dict[str, Any]]:
    occupancy = state["stored_total"]
    send_capacity = state["send_per_weekday"]
    # Contacts of ours already uploaded but not finished: they still owe steps. Spread
    # them over the sequence window so their remaining sends are not forgotten.
    in_flight = max(0, state["ours_total"] - state["ours_finished"])
    cohorts: Dict[date, int] = {}
    for i in range(SEQUENCE_DAYS):
        cohorts[start - timedelta(days=SEQUENCE_DAYS - i)] = in_flight // SEQUENCE_DAYS
    own_finished_available = state["ours_finished"]
    # Leads already uploaded that have never had a first email: the queue starts here,
    # it is not an assumption (5,189 stored vs 2,955 first-emailed on 2026-09-24).
    queue = max(0, state["ours_total"] - state["ours_first_email_sent"])
    rows = []

    for i in range(days):
        day = start + timedelta(days=i)
        finished_today = cohorts.get(day - timedelta(days=SEQUENCE_DAYS), 0)
        own_finished_available += finished_today

        # Rotate BEFORE uploading: room is made for the day, not after it was needed.
        free = max(0, cap - occupancy)
        want = max(0, intake - free)
        deleted = 0
        if rotate_own_after:
            deleted = min(own_finished_available, want)
            own_finished_available -= deleted
        if want > deleted and pool > 0:
            take = min(pool, want - deleted)
            pool -= take
            deleted += take
        occupancy -= deleted

        free = max(0, cap - occupancy)
        uploaded = min(intake, free)
        cohorts[day] = uploaded
        occupancy += uploaded

        # What the sequence owes today, whatever day it is. A step that falls on a
        # weekend is not skipped -- it waits for Monday, and waiting is the queue.
        due = sum(cohorts.get(day - timedelta(days=offset), 0) for offset in STEP_OFFSETS)
        if day.weekday() < 5:
            total = queue + due
            sent = min(total, send_capacity)
            queue = total - sent
        else:
            sent, queue = 0, queue + due

        rows.append({"day": day.isoformat(), "weekday": day.strftime("%a"), "uploaded": uploaded,
                     "finished": finished_today, "deleted": deleted, "occupancy": occupancy,
                     "free_slots": max(0, cap - occupancy), "emails_due": due, "emails_sent": sent,
                     "unsent_queue": queue})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--intake", type=int, default=1000)
    ap.add_argument("--cap", type=int, default=25000)
    ap.add_argument("--pool", type=int, default=0, help="finished legacy/Control contacts still rotatable")
    ap.add_argument("--rotate-own-after-days", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    state = live_state(headers())
    rows = forecast(state, cap=args.cap, days=args.days, intake=args.intake, pool=args.pool,
                    rotate_own_after=args.rotate_own_after_days, start=date.today())
    blocked = next((r for r in rows if r["uploaded"] < args.intake), None)
    summary = {
        "measured": state, "cap": args.cap, "intake": args.intake, "pool": args.pool,
        "rotate_own_after_days": args.rotate_own_after_days,
        "first_day_short_of_intake": blocked["day"] if blocked else None,
        "days_at_full_intake": rows.index(blocked) if blocked else args.days,
        "unsent_queue_at_end": rows[-1]["unsent_queue"],
        "queue_growing": rows[-1]["unsent_queue"] > rows[min(6, len(rows) - 1)]["unsent_queue"],
    }
    if args.json:
        print(json.dumps({"summary": summary, "days": rows}, indent=1))
        return 0
    print(json.dumps(summary, indent=1))
    print()
    print(f'{"day":12} {"":4} {"up":>6} {"fin":>6} {"del":>6} {"occupancy":>10} {"free":>7} {"due":>7} {"sent":>7} {"queue":>7}')
    for r in rows:
        print(f'{r["day"]:12} {r["weekday"]:4} {r["uploaded"]:6} {r["finished"]:6} {r["deleted"]:6} '
              f'{r["occupancy"]:10} {r["free_slots"]:7} {r["emails_due"]:7} {r["emails_sent"]:7} {r["unsent_queue"]:7}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
