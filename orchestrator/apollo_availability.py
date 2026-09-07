"""Is Apollo serving chargeable calls, and when may a run find out?

Permanent operation needs recovery to happen by itself. Requiring a human to issue a
new authorization after every top-up is a manual gate on an automatic system, and the
gate that guards spending is the wrong place to also decide whether the provider has
money again -- those are different questions and only one of them can be answered
from outside.

WHAT MAKES THE CHECK FREE. Apollo's refusal is returned BEFORE any work is done, so a
credit-exhausted 422 costs nothing. The run's own first chargeable call is therefore
already the availability check; no separate probe endpoint is needed and no extra
credit is spent learning the answer. What this module adds is not a probe, it is the
THROTTLE: a durable record of the last refusal, so a refusing provider is retried on a
schedule instead of on every code path that happens to want a contact.

WHAT IT DELIBERATELY DOES NOT DO. It never decides that credits exist. Only a served
call establishes that, and only for the moment it was served. A stored ``serving``
state is a memory of the last answer, never a prediction of the next one, and nothing
here converts a balance into a number of calls or a number of calls into credits.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = "apollo-availability/1"

UNKNOWN = "unknown"
SERVING = "serving"
REFUSING = "refusing"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: Optional[datetime] = None) -> str:
    return (moment or _now()).isoformat()


def _parse(stamp: Any) -> Optional[datetime]:
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _path(explicit: str = "") -> Path:
    import config

    return Path(explicit or getattr(config, "APOLLO_AVAILABILITY_STATE_PATH", "")
                or "apollo_availability.json")


def _blank() -> Dict[str, Any]:
    return {"schema": SCHEMA, "state": UNKNOWN, "refusing_since": "",
            "last_attempt_at": "", "last_served_at": "", "last_error_code": "",
            "consecutive_refusals": 0, "attempts": 0}


def load(path: str = "") -> Dict[str, Any]:
    """The durable record. A missing or unreadable file reads as UNKNOWN.

    Unknown means "try" -- the conservative direction here is to attempt, because a
    failed attempt is free and a wrongly withheld attempt costs a whole day.
    """
    target = _path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blank()
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return _blank()
    state = _blank()
    state.update({k: v for k, v in data.items() if k in state})
    if state["state"] not in (UNKNOWN, SERVING, REFUSING):
        state["state"] = UNKNOWN
    return state


def _write(path: str, state: Dict[str, Any]) -> Dict[str, Any]:
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)
    return state


def record_refusal(error_code: str = "", *, path: str = "",
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Apollo declined a chargeable call for want of credit."""
    state = load(path)
    moment = _iso(now)
    if state["state"] != REFUSING or not state["refusing_since"]:
        state["refusing_since"] = moment
    state["state"] = REFUSING
    state["last_attempt_at"] = moment
    state["last_error_code"] = str(error_code or "")
    state["consecutive_refusals"] = int(state.get("consecutive_refusals", 0) or 0) + 1
    state["attempts"] = int(state.get("attempts", 0) or 0) + 1
    return _write(path, state)


def record_served(*, path: str = "", now: Optional[datetime] = None) -> Dict[str, Any]:
    """A chargeable call RETURNED SUCCESSFULLY. Recovery is automatic from here.

    CALL THIS ONLY FROM A RESPONSE. Not when reserving budget, not in a finally, not
    on an exception path, and never because a timer expired. It was briefly called at
    reservation time, which meant zero HTTP requests could turn REFUSING into SERVING
    and clear the retry block -- the record then described an intention rather than
    anything the provider had done.

    Idempotent and cheap: an already-serving state returns without writing, so this
    can sit on the success path of every chargeable call.
    """
    state = load(path)
    moment = _iso(now)
    if state["state"] == SERVING and state.get("last_served_at"):
        return state
    state.update({"state": SERVING, "refusing_since": "", "last_served_at": moment,
                  "last_attempt_at": moment, "consecutive_refusals": 0,
                  "last_error_code": ""})
    state["attempts"] = int(state.get("attempts", 0) or 0) + 1
    return _write(path, state)


def retry_interval_hours() -> float:
    import config

    return max(0.0, float(getattr(config, "APOLLO_AVAILABILITY_RETRY_HOURS", 6) or 0))


def may_attempt(*, path: str = "", now: Optional[datetime] = None) -> Dict[str, Any]:
    """May this run make a chargeable attempt at all?

    Yes unless Apollo is on record as refusing and the retry interval has not
    elapsed. The interval is what makes the check CONTROLLED: a refusing provider is
    asked again on a schedule, not once per company, and never in a loop.

    A refusal costs nothing, so the interval is a courtesy to the provider and a
    guard against a fast-cycling caller -- not a spend control. Spend is controlled
    by the aggregate ceiling and by Apollo's own refusal.
    """
    state = load(path)
    moment = now or _now()
    if state["state"] != REFUSING:
        return {"allowed": True, "reason": f"provider_state_{state['state']}", **state}
    since = _parse(state.get("last_attempt_at"))
    hours = retry_interval_hours()
    if since is None or moment - since >= timedelta(hours=hours):
        return {"allowed": True, "reason": "retry_interval_elapsed",
                "retry_interval_hours": hours, **state}
    resume_at = since + timedelta(hours=hours)
    return {"allowed": False, "reason": "provider_refusing_within_retry_interval",
            "retry_interval_hours": hours, "next_attempt_after": _iso(resume_at),
            **state}


def acquisition_allowed(*, path: str = "") -> Dict[str, Any]:
    """May PAID INVENTORY be bought? A different question from "may we check".

    The interval elapsing permits one controlled attempt at Apollo. It does not
    permit buying postings, because nothing new has been learned yet -- the clock is
    not a response. Only a satisfactory response, recorded from the response itself,
    lifts a refusal for acquisition.

    Getting this wrong is how a refusing provider silently re-enabled purchases: the
    run stood down at hour 1, the same run stood down at hour 5, and at hour 7 it
    bought a window of postings it still could not enrich.
    """
    state = load(path)
    if state["state"] == REFUSING:
        return {"allowed": False,
                "reason": "provider_refusing_until_a_served_response",
                "detail": ("Apollo refused a chargeable call at "
                           f"{state.get('refusing_since')}. A retry may be attempted "
                           "once the interval elapses, but inventory is not bought "
                           "until a call actually succeeds."),
                **state}
    return {"allowed": True, "reason": f"provider_state_{state['state']}", **state}


def summary(path: str = "") -> Dict[str, Any]:
    state = load(path)
    return {k: state.get(k) for k in
            ("state", "refusing_since", "last_attempt_at", "last_served_at",
             "last_error_code", "consecutive_refusals", "attempts")}
