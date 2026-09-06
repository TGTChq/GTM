"""Distinct new APPROVED leads per business day, and the rolling reserve.

WHY THE TARGET IS NOT A RUN COUNTER.

The objective is at least 1,000 distinct new approved leads **per day** -- not per
run, not postings reviewed, not Apollo calls made, not leads attempted. Those are all
inputs, and every one of them has at some point been mistaken for the output.

Three consequences shape this module:

* **Distinct, and durable across runs.** A day may take several runs -- a batch that
  exhausts its budget, a retry after a provider stop, a top-up in the evening. The
  day's total is the size of the union of what they approved, not the sum of their
  counters, because the same lead approved twice is one lead.
* **New.** A lead already approved on an earlier day is not today's output. Recycling
  the backlog to reach a number is the one way to hit the target while delivering
  nothing, so the store keeps every key it has ever seen and counts only first
  sightings.
* **A day is a business day in the reporting timezone.** The reporting week is
  Friday-to-Friday `America/Los_Angeles`; a target counted in UTC would roll over
  mid-afternoon Pacific and split a working day in two.

THE ROLLING RESERVE. Inventory varies -- the measured weekend window held a fifth of
a weekday's postings -- so a system that produces exactly 1,000 on its best day
produces far fewer on its worst. The reserve is the buffer of approved leads that are
current and not yet consumed downstream, and the daily goal is therefore
``target + (reserve_floor - reserve_on_hand)``: a strong day is asked to bank the
difference, a weak day draws it down. It is not a licence to recycle -- every lead in
the reserve was approved once, counted once, on the day it was approved.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = "daily-approved-target/1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def business_day(now: Optional[datetime] = None, tz_name: str = "America/Los_Angeles") -> str:
    """The local calendar date the reporting layer would file this moment under."""
    moment = now or _now()
    try:
        from zoneinfo import ZoneInfo

        local = moment.astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 - a missing tzdata must not stop a run
        local = moment.astimezone(timezone(timedelta(hours=-8)))
    return local.date().isoformat()


def _path(root: str | os.PathLike) -> Path:
    return Path(root) / "daily_approved.json"


def _read(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else {}


def _write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _load(root: str | os.PathLike) -> Dict[str, Any]:
    state = _read(_path(root))
    if not state:
        state = {"schema": SCHEMA, "days": {}, "ever": []}
    state.setdefault("days", {})
    state.setdefault("ever", [])
    return state


def record_approved(root: str | os.PathLike, keys: Iterable[str], *,
                    now: Optional[datetime] = None,
                    retain_days: int = 45) -> Dict[str, Any]:
    """Add approved lead keys to today's total. Returns the day's state.

    Only keys never seen before count: `ever` is the lifetime set, so a lead approved
    again -- by a retry, a re-delivery, a later run touching the same company -- adds
    nothing. That is what makes the number "distinct NEW approved leads" rather than
    a delivery counter.
    """
    day = business_day(now)
    state = _load(root)
    ever = set(state.get("ever") or [])
    today = set((state["days"].get(day) or {}).get("keys") or [])
    added = 0
    for key in keys:
        key = str(key or "").strip().lower()
        if not key or key in ever:
            continue
        ever.add(key)
        today.add(key)
        added += 1
    state["days"][day] = {"keys": sorted(today), "count": len(today),
                          "updated_at": _now().isoformat()}
    # Bounded: the lifetime set is what makes "new" meaningful, so it is trimmed by
    # DAY rather than by size -- dropping a recent key would let a lead be counted
    # twice, which is the one error this store exists to prevent.
    cutoff = (datetime.fromisoformat(day) - timedelta(days=max(1, retain_days))).date().isoformat()
    kept_days = {d: v for d, v in state["days"].items() if d >= cutoff}
    state["days"] = kept_days
    state["ever"] = sorted(set().union(*[set(v.get("keys") or []) for v in kept_days.values()])
                           if kept_days else set())
    _write(_path(root), state)
    return {"day": day, "added": added, "approved_today": len(today)}


def approved_today(root: str | os.PathLike, *, now: Optional[datetime] = None) -> int:
    day = business_day(now)
    return int((_load(root)["days"].get(day) or {}).get("count") or 0)


def history(root: str | os.PathLike, *, days: int = 14) -> List[Dict[str, Any]]:
    state = _load(root)
    rows = [{"day": d, "approved": int(v.get("count") or 0)}
            for d, v in sorted(state["days"].items(), reverse=True)]
    return rows[:days]


def goal_for_today(root: str | os.PathLike, *, target: int, reserve_floor: int,
                   reserve_on_hand: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """How many more distinct new approved leads today should produce.

    ``target`` plus whatever the reserve is short, minus what today already has. A
    strong day is asked to bank the shortfall so a weak one can draw it down; the
    reserve is never met by re-counting yesterday's leads, because `record_approved`
    only counts first sightings.
    """
    have = approved_today(root, now=now)
    shortfall = max(0, int(reserve_floor) - max(0, int(reserve_on_hand)))
    goal = max(0, int(target)) + shortfall
    return {"day": business_day(now), "target": int(target),
            "reserve_floor": int(reserve_floor),
            "reserve_on_hand": int(reserve_on_hand),
            "reserve_shortfall": shortfall,
            "goal_today": goal, "approved_today": have,
            "remaining": max(0, goal - have), "met": have >= goal}
