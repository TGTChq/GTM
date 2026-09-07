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

* it reserves **potentially paid physical requests** -- organisation enrich and
  person match, including retries. People API Search is documented as 0 credits.
  Reservations are an upper bound on request attempts, NOT measured provider
  credits: response data and the workspace plan determine the actual charge;
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
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise BudgetExhausted({"authorization_id": "unreadable-ledger",
                               "authorized": 0, "consumed": 0,
                               "state_error": type(exc).__name__}) from exc
    if (not isinstance(data, dict) or data.get("schema") != SCHEMA
            or type(data.get("consumed")) is not int or data["consumed"] < 0
            or not isinstance(data.get("by_kind"), dict)):
        raise BudgetExhausted({"authorization_id": "invalid-ledger",
                               "authorized": 0, "consumed": 0,
                               "state_error": "invalid_budget_state"})
    return data


def _write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _opened_authorized(state: Dict[str, Any]) -> int:
    """The largest ceiling this authorization was ever given.

    Ledgers written before this field existed do not carry it. They are migrated to
    the only safe lower bound available: a grant cannot have consumed more calls than
    it was authorized, so ``consumed`` is a floor, and the last persisted
    ``authorized`` is the other candidate. Taking the maximum can only ever make an
    old grant harder to reopen, never easier.
    """
    recorded = state.get("opened_authorized")
    if isinstance(recorded, int) and recorded >= 0:
        return recorded
    return max(int(state.get("authorized", 0) or 0), int(state.get("consumed", 0) or 0))


def load(path: str = "") -> Dict[str, Any]:
    """The durable ledger, reconciled against the CURRENT authorization.

    A new ``authorization_id`` starts a fresh count. Changing only the number does
    not: raising a ceiling silently is how an aggregate becomes an allowance.
    """
    import config

    target = _path(path)
    state = _read(target)
    requested = max(0, int(getattr(config, "APOLLO_RECOVERY_BUDGET_CALLS", 0) or 0))
    auth_id = str(getattr(config, "APOLLO_RECOVERY_BUDGET_ID", "") or "").strip()
    if not state or state.get("authorization_id") != auth_id:
        state = {"schema": SCHEMA, "authorization_id": auth_id, "consumed": 0,
                 "by_kind": {k: 0 for k in KINDS}, "opened_at": _now(),
                 "last_charge_at": "", "deferrals": 0, "authorized": requested,
                 "opened_authorized": requested}
        state["requested_calls"] = requested
        state["raised_under_same_id"] = False
    else:
        # AN AUTHORIZATION'S CEILING IS A HIGH-WATER MARK FOR ITS ID.
        #
        # Raising the number under an id that already has a ledger does not enlarge
        # it, and under a SPENT id it does not revive it. Setting 2,000 beside a
        # spent 200-call grant would otherwise produce 1,800 calls of fresh room
        # under an authorization that was already closed -- a grant nobody issued,
        # indistinguishable in the ledger from one that was.
        #
        # THE HIGH-WATER MARK IS THE POINT. Comparing against the LAST PERSISTED
        # ceiling is not enough, because a refused call persists the ceiling that
        # refused it: set the limit to 0, let one call defer, and the ledger now
        # says the grant was opened with 0 -- after which any number reads as a
        # first grant. `opened_authorized` is written when the id is first seen and
        # never rises, so that erasure cannot happen.
        #
        # Lowering is still honoured: a ceiling may always be tightened. Only a NEW
        # authorization id adopts a larger number, which is what makes a grant a
        # deliberate, auditable act rather than a config edit.
        opened_with = _opened_authorized(state)
        state["opened_authorized"] = opened_with
        state["requested_calls"] = requested
        state["raised_under_same_id"] = requested > opened_with
        state["authorized"] = min(requested, opened_with)
    state["remaining"] = max(0, int(state["authorized"]) - int(state.get("consumed", 0) or 0))
    state["spent"] = state["remaining"] <= 0 and int(state.get("consumed", 0) or 0) > 0
    state["enabled"] = bool(getattr(config, "APOLLO_RECOVERY_BUDGET_ENABLED", False))
    return state


def enabled() -> bool:
    import config

    return bool(getattr(config, "APOLLO_RECOVERY_BUDGET_ENABLED", False))


def continuous_mode() -> bool:
    """Spend whatever the provider serves, with no per-top-up manual grant.

    Deliberately a separate switch from the ceiling. Turning the ceiling OFF would
    also remove the durable record of what was spent; this keeps the ledger and
    changes only what gates ACQUISITION, so a deployment can run continuously and
    still cap a grant if it wants to.
    """
    import config

    return bool(getattr(config, "APOLLO_CONTINUOUS_MODE", False))


def charge(kind: str, count: int = 1, *, path: str = "") -> Dict[str, Any]:
    """Record ``count`` chargeable calls, or raise ``BudgetExhausted``.

    Charged BEFORE the call is issued. Charging afterwards would let the last request
    over the line be paid for and unrecorded if the process died in between, which is
    the one accounting error a spend ceiling must not make.
    """
    if not enabled():
        return {"charged": False, "reason": "budget_not_enabled"}
    if kind == KIND_PEOPLE_SEARCH:
        # Retain the legacy kind for historical ledger compatibility; never refund
        # earlier reservations or reset an exhausted grant during migration.
        return {"charged": False, "reason": "zero_credit_endpoint"}
    target = _path(path)
    state = load(path)
    if str(kind) not in KINDS:
        raise ValueError(f"unknown chargeable kind: {kind!r}")
    want = max(0, int(count))
    # CONTINUOUS MODE WITHOUT AN AGGREGATE: the PROVIDER is the ceiling, so a call is
    # RECORDED rather than refused. Running without a cap must not also mean running
    # without a count -- the durable ledger still receives every call, which is what
    # keeps spend auditable when nobody issued a number.
    #
    # A configured aggregate still refuses exactly as before, in continuous mode or
    # out of it: `uncapped` means "no aggregate was set", never "the aggregate was
    # ignored".
    uncapped = continuous_mode() and int(state.get("authorized", 0) or 0) <= 0
    if not uncapped:
        if not state.get("authorization_id") or int(state.get("authorized", 0)) <= 0:
            state["deferrals"] = int(state.get("deferrals", 0)) + 1
            _write(target, state)
            raise BudgetExhausted(state)
        if int(state.get("consumed", 0)) + want > int(state["authorized"]):
            state["deferrals"] = int(state.get("deferrals", 0)) + 1
            _write(target, state)
            raise BudgetExhausted(state)
    else:
        state["uncapped_continuous_calls"] = int(
            state.get("uncapped_continuous_calls", 0) or 0) + want
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
             "by_kind", "deferrals", "opened_at", "last_charge_at", "spent",
             "requested_calls", "raised_under_same_id", "opened_authorized")}


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
        out["reason"] = ("authorized budget already consumed; a NEW "
                         "APOLLO_RECOVERY_BUDGET_ID is required"
                         if state.get("raised_under_same_id") else
                         "authorized budget already consumed")
        return out
    if required is not None and int(state["remaining"]) < int(required):
        out["reason"] = (f"only {state['remaining']} calls remain, {required} "
                         "required for the requested workload")
        out["ok"] = True          # it may still run, just not the whole workload
        out["partial"] = True
        return out
    out["ok"] = True
    return out


#: Lanes whose rows cost the provider nothing. A free lane may run with no
#: enrichment authorization: it accumulates no billed inventory, and the postings it
#: finds are queued in custody at no cost until a grant exists.
FREE_LANE_PREFIXES = ("ats",)


def paid_acquisition_allowed(path: str = "") -> Dict[str, Any]:
    """May a run BUY postings it has no authorization to enrich?

    No. A posting is only worth its price once a contact can be found for it, and
    contact discovery is what the recovery grant authorizes. Buying ahead of the
    grant converts money into backlog: custody preserves every row, so nothing is
    lost, but nothing is produced either and the next authorization is spent
    enriching inventory bought at a worse moment.

    This is a gate on PAID acquisition only. It never stops custody, enrichment,
    delivery or a free lane, and it is not a run failure -- a run with no
    authorization is a recoverable wait that costs nothing.

    Deliberately permissive when the ceiling is switched off: with
    ``APOLLO_RECOVERY_BUDGET_ENABLED`` false there is no authorization model in
    force at all, and this must not become a second, silent way to stop acquisition.
    """
    # CONTINUOUS MODE. A standing authorization to spend whatever the provider will
    # serve, within every other limit. The gate becomes "is Apollo serving?" rather
    # than "did someone issue a grant today", which is what removes the manual step
    # after each top-up -- and the answer is free, because a credit refusal is
    # returned before any work is done.
    #
    # It does NOT remove the ceiling: with a positive grant still configured the
    # aggregate applies as before, and every other control -- quality gates, dedupe,
    # custody, suppression, the Fantastic governor -- is untouched.
    if continuous_mode():
        from orchestrator import apollo_availability

        attempt = apollo_availability.may_attempt()
        if not attempt["allowed"]:
            return {"allowed": False, "reason": "provider_refusing",
                    "detail": ("Apollo refused a chargeable call at "
                               f"{attempt.get('refusing_since')}; the next attempt is "
                               f"due after {attempt.get('next_attempt_after')}. "
                               "Buying postings now would be buying work that cannot "
                               "be enriched."),
                    "provider": attempt}
        return {"allowed": True, "reason": "continuous_mode", "provider": attempt,
                **summary(path)}
    if not enabled():
        return {"allowed": True, "reason": "budget_not_enabled",
                "detail": "no authorization model in force; acquisition is unchanged"}
    state = load(path)
    if not state.get("authorization_id"):
        return {"allowed": False, "reason": "no_enrichment_authorization",
                "detail": "APOLLO_RECOVERY_BUDGET_ID is unset; an unset grant is zero",
                **summary(path)}
    if int(state.get("authorized", 0) or 0) <= 0:
        return {"allowed": False, "reason": "no_enrichment_authorization",
                "detail": "APOLLO_RECOVERY_BUDGET_CALLS is 0; nothing is authorized",
                **summary(path)}
    if int(state.get("remaining", 0) or 0) <= 0:
        return {"allowed": False, "reason": "enrichment_authorization_spent",
                "detail": (f"authorization {state.get('authorization_id')} is spent "
                           f"({state.get('consumed')} of {state.get('authorized')}); "
                           "a NEW id is required. Raising the call count under this "
                           "id grants nothing -- an open authorization keeps the "
                           "size it was opened with"),
                **summary(path)}
    return {"allowed": True, "reason": "authorized", **summary(path)}


def is_free_lane(name: Any) -> bool:
    text = str(name or "").strip().lower()
    return any(text == prefix or text.startswith(f"{prefix}_")
               for prefix in FREE_LANE_PREFIXES)
