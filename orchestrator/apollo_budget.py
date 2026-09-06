"""A durable, aggregate ceiling on chargeable Apollo calls.

WHY THE EXISTING LIMITS ARE NOT A BUDGET.

``PENDING_WORK_RESUME_MAX_PER_RUN`` bounds how much WORK a run adopts. It says
nothing about money: 2,000 resumed postings can issue an organisation enrich, a
people search and one or more person matches each, plus the alternate-contact
cascade and the org-id fallback behind them. And
``APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN`` covers one endpoint, resets every run, and
-- worst of all -- its documented default of ``0`` means *no ceiling*, so an unset
budget spends without limit.

This is the opposite in every one of those respects:

* it counts **every chargeable path** -- organisation enrich, people search, person
  match -- wherever they are called from, including the cascade, the org-id fallback
  and any retry, because a retry costs exactly what the first attempt cost;
* it is **durable across runs**, so an interrupted run cannot restart the budget by
  restarting itself. That is the failure that turned a credit ceiling into a daily
  allowance the last time one existed;
* **unset means REFUSE, not unlimited.** A budget nobody granted is zero. Spending
  requires an explicit authorization id and a positive number of calls, and a new id
  is what resets the counter -- editing the number alone does not, so a grant is
  deliberate and auditable rather than a config drift.

DEFERRAL IS ALREADY BUILT. When the budget is reached the caller stops; the work it
had not finished never reaches a terminal disposition, so ``pending_work`` keeps it
and a later run resumes it. Exhaustion is therefore a pause, not a loss -- which is
the property that makes a hard ceiling safe to set low.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = "apollo-recovery-budget/1"

#: Chargeable call kinds. Named individually because they do not cost the same and a
#: future price change must be expressible without re-deriving history.
KIND_ORG_ENRICH = "organization_enrich"
KIND_PEOPLE_SEARCH = "people_search"
KIND_PERSON_MATCH = "person_match"
KINDS = (KIND_ORG_ENRICH, KIND_PEOPLE_SEARCH, KIND_PERSON_MATCH)


class BudgetExhausted(RuntimeError):
    """Raised when a chargeable call would exceed the authorized aggregate.

    Carries the ledger so the caller can record WHY it stopped rather than reporting
    a generic failure -- "deferred on budget" and "the provider refused" need very
    different responses and must never look alike in an artifact.
    """

    def __init__(self, state: Dict[str, Any]) -> None:
        super().__init__(
            f"apollo recovery budget exhausted: consumed {state.get('consumed')} of "
            f"{state.get('authorized')} under authorization "
            f"{state.get('authorization_id') or '(none)'}")
        self.state = dict(state)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(explicit: str = "") -> Path:
    import config

    return Path(explicit or getattr(config, "APOLLO_RECOVERY_BUDGET_STATE_PATH", "")
                or "apollo_recovery_budget.json")


def _read(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else {}


def _write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def load(path: str = "") -> Dict[str, Any]:
    """The durable ledger, reconciled against the CURRENT authorization.

    A new ``authorization_id`` starts a fresh count. Changing only the number does
    not: raising a ceiling silently is how an aggregate becomes an allowance.
    """
    import config

    target = _path(path)
    state = _read(target)
    authorized = max(0, int(getattr(config, "APOLLO_RECOVERY_BUDGET_CALLS", 0) or 0))
    auth_id = str(getattr(config, "APOLLO_RECOVERY_BUDGET_ID", "") or "").strip()
    if not state or state.get("authorization_id") != auth_id:
        state = {"schema": SCHEMA, "authorization_id": auth_id, "consumed": 0,
                 "by_kind": {k: 0 for k in KINDS}, "opened_at": _now(),
                 "last_charge_at": "", "deferrals": 0}
    state["authorized"] = authorized
    state["remaining"] = max(0, authorized - int(state.get("consumed", 0) or 0))
    state["enabled"] = bool(getattr(config, "APOLLO_RECOVERY_BUDGET_ENABLED", False))
    return state


def enabled() -> bool:
    import config

    return bool(getattr(config, "APOLLO_RECOVERY_BUDGET_ENABLED", False))


def charge(kind: str, count: int = 1, *, path: str = "") -> Dict[str, Any]:
    """Record ``count`` chargeable calls, or raise ``BudgetExhausted``.

    Charged BEFORE the call is issued. Charging afterwards would let the last request
    over the line be paid for and unrecorded if the process died in between, which is
    the one accounting error a spend ceiling must not make.
    """
    if not enabled():
        return {"charged": False, "reason": "budget_not_enabled"}
    target = _path(path)
    state = load(path)
    if str(kind) not in KINDS:
        raise ValueError(f"unknown chargeable kind: {kind!r}")
    want = max(0, int(count))
    if not state.get("authorization_id") or int(state.get("authorized", 0)) <= 0:
        state["deferrals"] = int(state.get("deferrals", 0)) + 1
        _write(target, state)
        raise BudgetExhausted(state)
    if int(state.get("consumed", 0)) + want > int(state["authorized"]):
        state["deferrals"] = int(state.get("deferrals", 0)) + 1
        _write(target, state)
        raise BudgetExhausted(state)
    state["consumed"] = int(state.get("consumed", 0)) + want
    by_kind = dict(state.get("by_kind") or {})
    by_kind[kind] = int(by_kind.get(kind, 0)) + want
    state["by_kind"] = by_kind
    state["last_charge_at"] = _now()
    state["remaining"] = max(0, int(state["authorized"]) - state["consumed"])
    _write(target, state)
    return {"charged": True, **state}


def summary(path: str = "") -> Dict[str, Any]:
    """Reportable state. Safe to call when the budget is off."""
    state = load(path)
    return {k: state.get(k) for k in
            ("enabled", "authorization_id", "authorized", "consumed", "remaining",
             "by_kind", "deferrals", "opened_at", "last_charge_at")}


def preflight(required: Optional[int] = None, *, path: str = "") -> Dict[str, Any]:
    """Can a recovery run start at all, and how much may it spend?

    Answered BEFORE any work is adopted, so a run with no authorization refuses at
    the top instead of discovering it one Apollo call in -- and a refusal at the top
    costs nothing, while a refusal partway through has already spent.
    """
    state = load(path)
    out = {"ok": False, "reason": "", **state}
    if not state.get("enabled"):
        out["reason"] = "APOLLO_RECOVERY_BUDGET_ENABLED is off"
        return out
    if not state.get("authorization_id"):
        out["reason"] = "no APOLLO_RECOVERY_BUDGET_ID -- an unset budget is zero"
        return out
    if int(state.get("authorized", 0)) <= 0:
        out["reason"] = "APOLLO_RECOVERY_BUDGET_CALLS is 0 -- nothing is authorized"
        return out
    if int(state.get("remaining", 0)) <= 0:
        out["reason"] = "authorized budget already consumed"
        return out
    if required is not None and int(state["remaining"]) < int(required):
        out["reason"] = (f"only {state['remaining']} calls remain, {required} "
                         "required for the requested workload")
        out["ok"] = True          # it may still run, just not the whole workload
        out["partial"] = True
        return out
    out["ok"] = True
    return out
