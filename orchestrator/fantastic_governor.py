"""Monthly Fantastic.jobs credit governor (P0).

Answers ONE question before any top-up acquisition begins:

    HOW MANY FANTASTIC JOB CREDITS MAY THIS RUN SPEND?

It exists because the 20,000 Jobs-credit monthly plan is a HARD binding
constraint: a per-run yield target (``NET_NEW_SEND_SAFE_TARGET``) on its own is an
unlimited spending incentive (at ~10% yield it always bills the full per-run
ceiling and can burn most of a cycle in 2-3 days, then starve). The governor is
the spending AUTHORITY; the net-new target remains an *aspiration* that may stop a
run EARLY but can never increase the permitted budget.

Design (pure + deterministic; all I/O isolated in ``GovernorLedger``):

* Monthly ceiling   -- ``monthly_limit`` (plan), authoritative provider headers
                       (``jobs_remaining``) override the local ledger when present.
* Reserve           -- a late-cycle percentage never spent by ordinary pacing.
* Pacing            -- spendable = max(0, remaining - reserve); base allowance
                       = spendable / days_remaining (fractional-day aware).
* Carry-forward     -- unused allowance from prior days accrues (bounded), so a
                       strong inventory day may spend more than a weak one.
* Inventory hint    -- an optional 0-credit count-endpoint hint caps the grant at
                       what actually exists (never over-grants on an empty day).
* Hard clamps       -- per-run ceiling, monthly remaining, provider quota floor.
* Conservative fail -- with NO provider metadata and NO ledger the governor grants
                       only the configured daily MINIMUM (never the full ceiling).

Ledger state is persisted in its OWN file (never coupled to the continuation
cursor), keyed by billing cycle so a reset is handled automatically; it survives
container restarts, missed crons, duplicate deployments and manual runs (every run
records its spend against the same cycle key).
"""
from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

LEDGER_SCHEMA = "fantastic-governor-ledger/1"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------
# Inputs / outputs (pure)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class GovernorInputs:
    monthly_limit: int                  # plan size (e.g. 20000)
    now: datetime                       # run start (UTC)
    cycle_reset_at: Optional[datetime]  # next billing reset; None => unknown
    ledger_used_this_cycle: int         # local ledger spend in this cycle
    provider_jobs_remaining: Optional[int]  # authoritative header, if known
    reserve_pct: float                  # 0.10 = keep 10% back
    daily_min_jobs: int                 # conservative floor grant
    daily_max_jobs: int                 # per-day ceiling (0 = no daily ceiling)
    per_run_ceiling: int                # FANTASTIC_JOBS_MAX_JOBS_PER_RUN
    quota_floor: int                    # FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING
    carry_forward: int = 0              # accrued unused allowance (credits)
    carry_forward_cap_days: float = 3.0 # carry can't exceed N days of base allowance
    inventory_hint: Optional[int] = None  # 0-credit count of available fresh rows
    spent_today: int = 0                # credits already spent today (manual+cron)
    ledger_has_runs: bool = False       # any run recorded this cycle (evidence exists)


@dataclass(frozen=True)
class GovernorDecision:
    run_budget: int                     # credits THIS run may bill (>= 0)
    reason: str                         # dominant clamp / rationale
    remaining_credits: int              # authoritative remaining (provider or ledger)
    spendable_credits: int              # remaining minus reserve
    reserve_credits: int
    base_daily_allowance: int
    carry_forward_applied: int
    days_remaining: float
    inventory_capped: bool
    provider_authoritative: bool
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _days_remaining(now: datetime, reset_at: Optional[datetime]) -> float:
    """Fractional days until the cycle resets. Unknown reset => assume a full
    30-day cycle (conservative spreading, never a burst). A reset date that is in
    the PAST (stale header / stale env) is ALSO treated as unknown: otherwise the
    pace would collapse toward a 1-hour horizon and authorize a cycle-boundary
    burst up to the per-run ceiling (Gate-A BUG 2). Within the last day of a cycle
    the horizon floors at 1 day so the final day never spends more than one day's
    pace of the remaining spendable budget."""
    if reset_at is None or reset_at <= now:
        return 30.0
    delta = (reset_at - now).total_seconds() / 86400.0
    return max(delta, 1.0)


# Zero-grant reasons that are DERIVED FROM PROVIDER QUOTA METADATA (the
# authoritative ``remaining`` and the floor/reserve clamps applied to it). Only
# these justify spending one 0-row count request to re-read the provider's quota
# headers -- a stale snapshot is the one input a refresh can actually correct.
# Deliberately EXCLUDED, because re-reading quota cannot change them:
#   daily_allowance_spent (today's grant already spent), inventory_hint (no
#   inventory), per_run_ceiling / daily_max (operator configuration).
# An exhaustive sweep of decide() (606,528 input combinations) shows the ONLY
# zero-budget reasons reachable today are: provider_quota_floor and reserve (both
# quota-derived, both listed) and daily_allowance_spent / inventory_hint (both
# excluded). monthly_remaining and exhausted are currently UNREACHABLE as zero
# reasons -- the final block re-labels those cases provider_quota_floor -- and are
# listed defensively so a future change to the clamp order cannot silently
# reintroduce an unrecoverable deadlock.
QUOTA_METADATA_ZERO_REASONS = frozenset({
    "provider_quota_floor",   # remaining <= FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING
    "reserve",                # remaining <= reserve  => spendable 0
    "monthly_remaining",      # (defensive) remaining itself is 0
    "exhausted",              # (defensive) nothing left to spend this cycle
})


def decide(inp: GovernorInputs) -> GovernorDecision:
    """Pure allocation. Order of clamps (most-conservative wins):

    1. authoritative remaining (provider header if present, else ledger)
    2. reserve held back
    3. pace = spendable / days_remaining + bounded carry-forward
    4. daily min/max, inventory hint, today's prior spend
    5. per-run ceiling, quota floor, monthly remaining
    """
    detail: Dict[str, Any] = {}
    provider_auth = inp.provider_jobs_remaining is not None
    if provider_auth:
        # A negative header means EXHAUSTED, never "ignore the header" (Gate-A BUG 3).
        remaining = max(0, int(inp.provider_jobs_remaining))
    else:
        remaining = max(0, int(inp.monthly_limit) - int(inp.ledger_used_this_cycle))
    detail["remaining_source"] = "provider_header" if provider_auth else "local_ledger"
    # CONSERVATIVE FAIL (Gate-A BUG 1): with NO provider metadata AND NO ledger
    # evidence for this cycle we cannot know the true remaining; never assume the
    # full plan. Grant at most the daily minimum until a header is observed.
    blind = (not provider_auth) and int(inp.ledger_used_this_cycle) <= 0 and not inp.ledger_has_runs
    detail["blind_mode"] = blind

    reserve = int(round(max(0.0, min(1.0, inp.reserve_pct)) * inp.monthly_limit))
    spendable = max(0, remaining - reserve)
    days = _days_remaining(inp.now, inp.cycle_reset_at)
    base = int(spendable / days) if days > 0 else 0

    # Carry-forward is bounded so a long idle stretch can never authorize a burst
    # larger than a few days of pace.
    carry_cap = int(base * max(0.0, inp.carry_forward_cap_days))
    carry = max(0, min(int(inp.carry_forward), carry_cap))

    grant = base + carry
    reason = "pace"

    if inp.daily_max_jobs > 0 and grant > inp.daily_max_jobs:
        grant, reason = inp.daily_max_jobs, "daily_max"
    if blind and grant > inp.daily_min_jobs:
        grant, reason = inp.daily_min_jobs, "blind_conservative"
    # Floor: guarantee at least the daily minimum when budget allows (never starve
    # discovery on a trickle), but NEVER above spendable. If spendable itself is
    # below the minimum the binding reason is the reserve, not the floor.
    if grant < inp.daily_min_jobs:
        if spendable >= inp.daily_min_jobs:
            grant, reason = inp.daily_min_jobs, "daily_min"
        else:
            grant, reason = spendable, "reserve"

    # Same-day prior spend (manual run + cron): today's allowance is shared.
    if inp.spent_today > 0:
        grant = max(0, grant - inp.spent_today)
        if grant == 0:
            reason = "daily_allowance_spent"
    inventory_capped = False
    if inp.inventory_hint is not None and inp.inventory_hint >= 0 and grant > inp.inventory_hint:
        grant, inventory_capped, reason = int(inp.inventory_hint), True, "inventory_hint"

    # Hard clamps.
    if inp.per_run_ceiling > 0 and grant > inp.per_run_ceiling:
        grant, reason = inp.per_run_ceiling, "per_run_ceiling"
    floor_room = max(0, remaining - max(0, inp.quota_floor))
    if grant > floor_room:
        grant, reason = floor_room, "provider_quota_floor"
    if grant > spendable:
        grant, reason = spendable, "reserve"
    if grant > remaining:
        grant, reason = remaining, "monthly_remaining"
    grant = max(0, int(grant))
    if grant == 0:
        # Name the binding zero-cause precisely (ops dashboards read this).
        if remaining <= max(0, inp.quota_floor):
            reason = "provider_quota_floor"
        elif spendable == 0:
            reason = "reserve" if remaining > 0 else "exhausted"
        elif reason == "pace":
            reason = "exhausted"

    return GovernorDecision(
        run_budget=grant, reason=reason, remaining_credits=remaining,
        spendable_credits=spendable, reserve_credits=reserve,
        base_daily_allowance=base, carry_forward_applied=carry, days_remaining=round(days, 3),
        inventory_capped=inventory_capped, provider_authoritative=provider_auth, detail=detail)


# --------------------------------------------------------------------------
# Persisted ledger (the only I/O; never coupled to the continuation cursor)
# --------------------------------------------------------------------------
class GovernorLedger:
    """Append-safe monthly spend ledger keyed by billing cycle.

    File shape (additive, schema-versioned)::

        {"schema": "fantastic-governor-ledger/1",
         "cycle_key": "2026-09-17",          # the reset date this cycle ends at
         "cycle_reset_at": "2026-09-17T00:00:00+00:00",
         "used": 1234, "runs": [...last N run records...],
         "carry_forward": 0, "last_allowance_day": "2026-08-22",
         "updated_at": "..."}

    A run against a DIFFERENT cycle_key (reset passed) starts a fresh cycle
    automatically; nothing is ever wiped by hand.
    """

    def __init__(self, path: str, keep_runs: int = 120) -> None:
        self.path = str(path or "")
        self.keep_runs = int(keep_runs)
        self.state: Dict[str, Any] = self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        if not self.path:
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("schema") == LEDGER_SCHEMA:
                return data
        except (OSError, ValueError):
            pass
        return {}

    def save(self) -> None:
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = f"{self.path}.tmp"
            self.state["schema"] = LEDGER_SCHEMA
            self.state["updated_at"] = _utcnow().isoformat()
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.path)
        except OSError as exc:  # never fail a run on ledger I/O
            logger.warning("governor ledger not persisted: %s", type(exc).__name__)

    # -- cycle handling ------------------------------------------------------
    @staticmethod
    def cycle_key_for(reset_at: Optional[datetime]) -> str:
        return reset_at.date().isoformat() if reset_at else "unknown"

    # Arm state must SURVIVE a cycle rollover (it is what detects the rollover),
    # so it is carried across every reset of the per-cycle spend counters.
    _ARM_KEYS = ("armed", "arm_pending_cycle_key", "armed_at_cycle_key")

    def _carry_arm(self, fresh: Dict[str, Any]) -> Dict[str, Any]:
        for k in self._ARM_KEYS:
            if k in self.state:
                fresh[k] = self.state[k]
        return fresh

    def ensure_cycle(self, reset_at: Optional[datetime], now: datetime) -> bool:
        """Roll to a new cycle when the reset date changed OR the recorded reset
        has passed. Returns True when a rollover happened."""
        key = self.cycle_key_for(reset_at)
        recorded_reset = _parse_iso(self.state.get("cycle_reset_at"))
        rolled = False
        if self.state.get("cycle_key") != key and key != "unknown":
            rolled = bool(self.state.get("cycle_key"))
            self.state = self._carry_arm({
                "schema": LEDGER_SCHEMA, "cycle_key": key,
                "cycle_reset_at": reset_at.isoformat() if reset_at else "",
                "used": 0, "runs": [], "carry_forward": 0, "last_allowance_day": ""})
        elif recorded_reset is not None and now >= recorded_reset and key == "unknown":
            # Reset passed but provider didn't tell us the next date: start fresh.
            rolled = True
            self.state = self._carry_arm({
                "schema": LEDGER_SCHEMA, "cycle_key": "unknown",
                "cycle_reset_at": "", "used": 0, "runs": [],
                "carry_forward": 0, "last_allowance_day": ""})
        if not self.state:
            self.state = {"schema": LEDGER_SCHEMA, "cycle_key": key,
                          "cycle_reset_at": reset_at.isoformat() if reset_at else "",
                          "used": 0, "runs": [], "carry_forward": 0, "last_allowance_day": ""}
        return rolled

    # -- accessors ------------------------------------------------------------
    @property
    def used(self) -> int:
        return int(self.state.get("used", 0) or 0)

    @property
    def carry_forward(self) -> int:
        return int(self.state.get("carry_forward", 0) or 0)

    def spent_on_day(self, day_iso: str) -> int:
        return sum(int(r.get("billed", 0) or 0) for r in (self.state.get("runs") or [])
                   if str(r.get("day", "")) == day_iso)

    def accrue_carry_forward(self, base_allowance: int, now: datetime,
                             cap_days: float = 3.0) -> None:
        """Once per calendar day: roll UNUSED allowance of the elapsed days forward.

        Rules (load-bearing for pacing correctness):
        * Carry accrues ONLY for whole days elapsed INSIDE the current cycle since
          the cycle's first allowance day -- never from a prior cycle (rollover
          clears both carry and the day stamp), and never on a cycle's first grant.
        * Each elapsed day contributes max(0, base - spent_that_day); a gap of N
          idle days therefore contributes N days of allowance, but the total is
          CAPPED at ``cap_days`` x base so a long idle stretch can never authorize a
          burst (the same bound ``decide`` applies).
        """
        today = now.date()
        last = str(self.state.get("last_allowance_day") or "")
        if not last:
            self.state["last_allowance_day"] = today.isoformat()   # cycle's first day: zero carry
            return
        last_day = datetime.fromisoformat(last).date()
        if last_day >= today:
            return
        carry = self.carry_forward
        day = last_day
        while day < today:
            carry += max(0, int(base_allowance) - self.spent_on_day(day.isoformat()))
            day += timedelta(days=1)
        cap = int(max(0.0, cap_days) * int(base_allowance))
        self.state["carry_forward"] = min(carry, cap)
        self.state["last_allowance_day"] = today.isoformat()

    def record_run(self, run_id: str, billed: int, granted: int, now: datetime,
                   decision_reason: str = "") -> None:
        """Idempotent per run_id: a replayed/duplicate record does not double-count."""
        runs = list(self.state.get("runs") or [])
        for r in runs:
            if r.get("run_id") == run_id:
                r["billed"] = int(billed); r["granted"] = int(granted)
                self.state["used"] = sum(int(x.get("billed", 0) or 0) for x in runs)
                self.state["runs"] = runs[-self.keep_runs:]
                return
        runs.append({"run_id": str(run_id), "day": now.date().isoformat(),
                     "at": now.isoformat(), "billed": int(billed), "granted": int(granted),
                     "reason": decision_reason})
        self.state["runs"] = runs[-self.keep_runs:]
        self.state["used"] = self.used + int(billed)
        # Spending consumes carry-forward first (it was accrued from unused pace).
        consumed_carry = min(self.carry_forward, int(billed))
        self.state["carry_forward"] = self.carry_forward - consumed_carry

    def to_dict(self) -> Dict[str, Any]:
        s = dict(self.state)
        s["runs"] = len(self.state.get("runs") or [])
        return s


# --------------------------------------------------------------------------
# Facade used by the pipeline
# --------------------------------------------------------------------------
@dataclass
class GovernorContext:
    enabled: bool
    decision: Optional[GovernorDecision]
    ledger: Optional[GovernorLedger]
    cycle_rolled: bool = False
    armed: bool = True          # False => pre-arm legacy drain (no budget cap)
    arm_state: str = ""

    @property
    def run_budget(self) -> Optional[int]:
        """None => impose NO governor cap (feature off, or deliberately not yet
        armed for the cycle that was already running when it was switched on)."""
        if not (self.enabled and self.decision and self.armed):
            return None
        return self.decision.run_budget

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "armed": self.armed, "arm_state": self.arm_state,
                "decision": self.decision.to_dict() if self.decision else None,
                "ledger": self.ledger.to_dict() if self.ledger else None,
                "cycle_rolled": self.cycle_rolled}


def resolve_arm_state(ledger: "GovernorLedger", cycle_key: str, *, auto_arm: bool) -> tuple:
    """Decide whether the governor governs THIS cycle, and persist the decision.

    Problem: switching the governor on mid-cycle, when the remaining quota is
    already below the reserve, would grant 0 and STRAND the remainder. Waiting and
    flipping a variable later requires a human at reset. Auto-arm removes both:

      * first sight of the flag -> record ``arm_pending_cycle_key`` = the cycle that
        is ALREADY running, and stay UNARMED so that cycle drains under legacy
        behaviour (exactly as before the flag was set);
      * any later cycle (billing reset detected via a changed cycle key) -> ARMED,
        permanently, with no configuration change;
      * missing/corrupt arm state -> ARMED. Governing is the bounded, conservative
        outcome; the only cost of being wrong is stranding an already-expiring
        remainder, whereas failing open risks unbounded spend.

    Returns ``(armed: bool, state: str)``.
    """
    if not auto_arm:
        return True, "auto_arm_disabled_always_armed"
    st = ledger.state
    pending = str(st.get("arm_pending_cycle_key") or "")
    if st.get("armed") is True:
        return True, "already_armed"
    if not pending:
        # First run since the flag was enabled: protect the in-flight cycle.
        st["arm_pending_cycle_key"] = str(cycle_key)
        st["armed"] = False
        return False, "pre_arm_current_cycle_drains"
    if str(cycle_key) != pending:
        st["armed"] = True
        st["armed_at_cycle_key"] = str(cycle_key)
        return True, "armed_on_cycle_rollover"
    return False, "pre_arm_current_cycle_drains"


def build_context(cfg, *, run_id: str, provider_jobs_remaining: Optional[int] = None,
                  provider_reset_at: Optional[datetime] = None,
                  inventory_hint: Optional[int] = None,
                  now: Optional[datetime] = None) -> GovernorContext:
    """Construct the governor decision for this run from config + persisted ledger.

    ``provider_jobs_remaining`` / ``provider_reset_at`` come from the LAST KNOWN
    provider headers (e.g. a 0-credit count probe or the previous run's headers),
    never from a row-producing call made just to ask. Missing values fail
    conservatively (daily minimum only)."""
    if not bool(getattr(cfg, "FANTASTIC_MONTHLY_GOVERNOR_ENABLED", False)):
        return GovernorContext(enabled=False, decision=None, ledger=None)
    now = now or _utcnow()
    ledger = GovernorLedger(str(getattr(cfg, "FANTASTIC_GOVERNOR_LEDGER_PATH", "") or ""))
    use_headers = bool(getattr(cfg, "FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS", True))
    reset_at = provider_reset_at if use_headers else None
    if reset_at is None:
        reset_at = _parse_iso(getattr(cfg, "FANTASTIC_BILLING_RESET_AT", "") or "")
    rolled = ledger.ensure_cycle(reset_at, now)

    monthly_limit = int(getattr(cfg, "FANTASTIC_MONTHLY_JOBS_LIMIT", 20000) or 20000)
    reserve_pct = float(getattr(cfg, "FANTASTIC_MONTHLY_RESERVE_PCT", 0.10) or 0.0)
    # Pre-compute base allowance to accrue yesterday's unused pace.
    prelim_remaining = (provider_jobs_remaining if (use_headers and provider_jobs_remaining is not None)
                        else max(0, monthly_limit - ledger.used))
    prelim_spendable = max(0, prelim_remaining - int(round(reserve_pct * monthly_limit)))
    prelim_base = int(prelim_spendable / _days_remaining(now, reset_at))
    ledger.accrue_carry_forward(prelim_base, now,
                                cap_days=float(getattr(cfg, "FANTASTIC_GOVERNOR_CARRY_CAP_DAYS", 3.0) or 0.0))

    use_hint = bool(getattr(cfg, "FANTASTIC_GOVERNOR_USE_COUNT_HINT", False))
    inp = GovernorInputs(
        monthly_limit=monthly_limit, now=now, cycle_reset_at=reset_at,
        ledger_used_this_cycle=ledger.used,
        provider_jobs_remaining=(provider_jobs_remaining if use_headers else None),
        reserve_pct=reserve_pct,
        daily_min_jobs=int(getattr(cfg, "FANTASTIC_DAILY_MIN_JOBS", 100) or 0),
        daily_max_jobs=int(getattr(cfg, "FANTASTIC_DAILY_MAX_JOBS", 0) or 0),
        per_run_ceiling=int(getattr(cfg, "FANTASTIC_JOBS_MAX_JOBS_PER_RUN", 0) or 0),
        quota_floor=int(getattr(cfg, "FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING", 0) or 0),
        carry_forward=ledger.carry_forward,
        carry_forward_cap_days=float(getattr(cfg, "FANTASTIC_GOVERNOR_CARRY_CAP_DAYS", 3.0) or 0.0),
        inventory_hint=(inventory_hint if use_hint else None),
        spent_today=ledger.spent_on_day(now.date().isoformat()),
        ledger_has_runs=bool(ledger.state.get("runs")),
    )
    decision = decide(inp)
    armed, arm_state = resolve_arm_state(
        ledger, ledger.cycle_key_for(reset_at),
        auto_arm=bool(getattr(cfg, "FANTASTIC_GOVERNOR_AUTO_ARM", True)))
    ledger.save()
    return GovernorContext(enabled=True, decision=decision, ledger=ledger,
                           cycle_rolled=rolled, armed=armed, arm_state=arm_state)


def commit_run(ctx: GovernorContext, *, run_id: str, billed: int, now: Optional[datetime] = None) -> None:
    """Record the credits actually billed by this run (idempotent per run_id)."""
    if not ctx.enabled or ctx.ledger is None or ctx.decision is None:
        return
    now = now or _utcnow()
    ctx.ledger.record_run(run_id, int(billed), ctx.decision.run_budget, now, ctx.decision.reason)
    ctx.ledger.save()
