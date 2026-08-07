"""Deterministic, distributed ATS board scheduling.

The problem being replaced
--------------------------
``AtsBoardRegistry.due_entries`` selects boards whose ``last_checked_at`` is
older than ``ATS_BOARD_REFRESH_INTERVAL_HOURS`` (20). Against an 8-hour cron
this does not rotate; it oscillates. Measured on the real registry snapshot
(145 boards, sha ddd9d11b...):

* at the registry's own write time, **1 of 145** boards was due;
* 35 hours later, **145 of 145** were due;
* every board's age sat between 17.4 and 17.5 hours -- one tight cohort.

Production runs attempted 5, 7 and 0 boards. Those are three samples of the
same herd arriving or not arriving together, which is why "how many boards
does a run cover?" has no stable answer today.

The replacement
---------------
Each board is assigned a slot by hashing ``provider:identifier`` -- stable
across runs, independent of timing, and uncorrelated with when a board was last
seen. A run covers slot ``position % cycle_length``. Over one full cycle every
board is visited exactly once, and no two boards can synchronise because their
slots were fixed before the first run.

Two explicit modes, never combined in one run:

* ``legacy_interval`` -- the production default. ``select_boards`` returns the
  ``due_entries`` selection untouched; nothing here runs, no state is written.
* ``deterministic_partition`` -- opt-in. Slot partitioning with a bounded
  overdue quota (no thundering herd), bounded failed-board retries, an explicit
  per-run board cap, and a per-board selection reason.

Overdue fairness (Phase 1B-2B)
------------------------------
The current registry can place nearly every board into overdue status at once.
An unbounded overdue rule would then reproduce the herd it was meant to fix. So
overdue selection is quota-limited: at most ``overdue_cap`` overdue boards per
run, ordered deterministically (carried-forward first, then oldest first), and
the excess is *carried forward* -- persisted and given priority next run -- so
no overdue board can starve while the quota holds the herd back.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: Default cycle. With ~145 boards and 2 runs/day this covers the registry
#: roughly every 3.5 days while touching ~21 boards per run.
DEFAULT_CYCLE_LENGTH = 7

#: A board older than this is overdue and jumps its slot. Prevents a long cycle
#: from silently letting freshness decay without bound.
DEFAULT_MAX_AGE_HOURS = 168

#: The two -- and only two -- selection algorithms. A run picks exactly one.
SCHEDULER_MODES = ("legacy_interval", "deterministic_partition")

#: Schema tag stamped into every persisted scheduler-state file. A file whose
#: tag is not this is refused rather than silently reinterpreted.
SCHEDULER_STATE_SCHEMA = "ats-scheduler-state/1"


class SchedulerConfigError(ValueError):
    """An invalid scheduler configuration. Raised before any acquisition."""


class SchedulerStateError(ValueError):
    """A scheduler-state file that cannot be trusted (wrong schema, corrupt)."""


def slot_for(provider: str, identifier: str, cycle_length: int) -> int:
    """Stable slot assignment. Same board, same slot, forever."""
    if cycle_length < 1:
        raise ValueError("cycle_length must be at least 1")
    key = f"{str(provider).strip().lower()}:{str(identifier).strip()}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % cycle_length


def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class SchedulerConfig:
    """Effective scheduler configuration for one run.

    ``board_cap`` and ``overdue_cap`` are ``None`` when disabled, so a caller can
    tell "no cap" apart from "a cap of zero", which would select nothing.
    """

    mode: str = "legacy_interval"
    cycle_length: int = DEFAULT_CYCLE_LENGTH
    position: int = 0
    board_cap: Optional[int] = None
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS
    max_retry_attempts: int = 2
    overdue_cap: Optional[int] = None
    state_path: str = ""
    #: Overdue keys carried forward from a prior run (loaded from SchedulerState),
    #: given priority next run. Empty while overdue_cap is disabled.
    carried_overdue: Sequence[str] = ()

    @classmethod
    def from_config(cls, cfg: Any) -> "SchedulerConfig":
        """Read the ``ATS_SCHEDULER_*`` values from a config module/object.

        A ``board_cap`` or ``overdue_cap`` of 0 in config means "unset"; it maps
        to ``None`` here so 0 can never be mistaken for "select nothing".
        """
        board_cap = int(getattr(cfg, "ATS_SCHEDULER_BOARD_CAP", 0) or 0)
        overdue_cap = int(getattr(cfg, "ATS_SCHEDULER_OVERDUE_CAP", 0) or 0)
        return cls(
            mode=str(getattr(cfg, "ATS_SCHEDULER_MODE", "legacy_interval")).strip().lower(),
            cycle_length=int(getattr(cfg, "ATS_SCHEDULER_CYCLE_LENGTH", DEFAULT_CYCLE_LENGTH)),
            position=int(getattr(cfg, "ATS_SCHEDULER_POSITION", 0)),
            board_cap=board_cap or None,
            max_age_hours=int(getattr(cfg, "ATS_SCHEDULER_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS)),
            max_retry_attempts=int(getattr(cfg, "ATS_SCHEDULER_MAX_RETRY_ATTEMPTS", 2)),
            overdue_cap=overdue_cap or None,
            state_path=str(getattr(cfg, "ATS_SCHEDULER_STATE_PATH", "")).strip(),
        )

    def validate(self) -> "SchedulerConfig":
        """Fail loudly and early on any invalid combination.

        Called before acquisition begins, so a misconfiguration can never reach
        the wire as a half-selected run.
        """
        errors: List[str] = []
        if self.mode not in SCHEDULER_MODES:
            errors.append(
                f"ATS_SCHEDULER_MODE={self.mode!r} is not one of {SCHEDULER_MODES}"
            )
        if self.cycle_length < 1:
            errors.append("ATS_SCHEDULER_CYCLE_LENGTH must be at least 1")
        if self.position < 0:
            errors.append("ATS_SCHEDULER_POSITION must be >= 0")
        if self.board_cap is not None and self.board_cap < 1:
            errors.append("ATS_SCHEDULER_BOARD_CAP must be >= 1 when set")
        if self.max_age_hours < 0:
            errors.append("ATS_SCHEDULER_MAX_AGE_HOURS must be >= 0")
        if self.max_retry_attempts < 0:
            errors.append("ATS_SCHEDULER_MAX_RETRY_ATTEMPTS must be >= 0")
        if self.overdue_cap is not None and self.overdue_cap < 0:
            errors.append("ATS_SCHEDULER_OVERDUE_CAP must be >= 0")
        if (
            self.overdue_cap is not None
            and self.board_cap is not None
            and self.overdue_cap >= self.board_cap
        ):
            errors.append(
                "ATS_SCHEDULER_OVERDUE_CAP must be strictly less than "
                "ATS_SCHEDULER_BOARD_CAP so normal-slot boards are never fully "
                "displaced by the overdue quota"
            )
        if errors:
            raise SchedulerConfigError("; ".join(errors))
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "cycle_length": self.cycle_length,
            "position": self.position,
            "board_cap": self.board_cap,
            "max_age_hours": self.max_age_hours,
            "max_retry_attempts": self.max_retry_attempts,
            "overdue_cap": self.overdue_cap,
            "state_path": self.state_path,
            "state_enabled": bool(self.state_path),
        }


# --------------------------------------------------------------------------
# Minimal, bounded, schema-versioned persisted state
# --------------------------------------------------------------------------


@dataclass
class SchedulerState:
    """The only state deterministic scheduling genuinely needs to persist.

    It carries the keys of overdue boards that the per-run quota could not fit,
    so the next run can give them priority. Each run OVERWRITES this list with
    its own excess, so the file is bounded by the registry size and never grows
    cumulatively. Absent entirely when no ``state_path`` is configured.
    """

    schema_version: str = SCHEDULER_STATE_SCHEMA
    carried_overdue: List[str] = field(default_factory=list)
    last_position: Optional[int] = None
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "carried_overdue": list(self.carried_overdue),
            "last_position": self.last_position,
            "updated_at": self.updated_at,
        }

    @classmethod
    def load(cls, path: str | Path) -> "SchedulerState":
        p = Path(path)
        if not p.is_file():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SchedulerStateError(f"scheduler state {p} is unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise SchedulerStateError(f"scheduler state {p} is not an object")
        schema = data.get("schema_version")
        if schema != SCHEDULER_STATE_SCHEMA:
            raise SchedulerStateError(
                f"scheduler state {p} has schema {schema!r}; expected "
                f"{SCHEDULER_STATE_SCHEMA!r}. Refusing to reinterpret it."
            )
        carried = data.get("carried_overdue") or []
        if not isinstance(carried, list):
            raise SchedulerStateError(f"scheduler state {p} carried_overdue is not a list")
        return cls(
            carried_overdue=[str(k) for k in carried],
            last_position=data.get("last_position"),
            updated_at=str(data.get("updated_at") or ""),
        )

    def save(self, path: str | Path) -> str:
        """Atomic write: a crash leaves the previous file, never a half-file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat()
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return str(p)


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


@dataclass
class ScheduleDecision:
    """Why each board was or was not selected -- schedules must be explainable."""

    position: int
    cycle_length: int
    slot: int
    mode: str = "deterministic_partition"
    selected: List[Dict[str, Any]] = field(default_factory=list)
    reasons: Dict[str, int] = field(default_factory=dict)
    overdue: List[str] = field(default_factory=list)
    retry: List[str] = field(default_factory=list)
    normal_slot: List[str] = field(default_factory=list)
    carried_forward: List[str] = field(default_factory=list)
    capped_out: int = 0
    available: int = 0
    #: Category counts of the boards that were actually selected (post-cap).
    selected_overdue: int = 0
    selected_retry: int = 0
    selected_normal: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "position": self.position,
            "cycle_length": self.cycle_length,
            "slot": self.slot,
            "available_boards": self.available,
            "selected_boards": len(self.selected),
            "reasons": dict(self.reasons),
            "overdue": list(self.overdue),
            "retry": list(self.retry),
            "normal_slot": list(self.normal_slot),
            "carried_forward": list(self.carried_forward),
            "capped_out": self.capped_out,
            "selected_overdue": self.selected_overdue,
            "selected_retry": self.selected_retry,
            "selected_normal": self.selected_normal,
        }


def _board_key(board: Mapping[str, Any]) -> str:
    return f"{board.get('provider') or ''}:{board.get('identifier') or ''}"


def partitioned_schedule(
    boards: Sequence[Mapping[str, Any]],
    *,
    position: int,
    cycle_length: int = DEFAULT_CYCLE_LENGTH,
    max_boards_per_run: Optional[int] = None,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    max_retry_attempts: int = 2,
    overdue_cap: Optional[int] = None,
    carried_overdue: Sequence[str] = (),
    now: Optional[datetime] = None,
) -> ScheduleDecision:
    """Select this run's board subset. Deterministic for identical inputs.

    Selection order, highest priority first:

    1. **overdue** -- older than ``max_age_hours`` regardless of slot, so a long
       cycle can never let freshness decay without limit. Quota-limited by
       ``overdue_cap``; excess is carried forward, not dropped;
    2. **retry** -- failed within a bounded attempt count, placed in the very
       next run rather than waiting a whole cycle;
    3. **slot** -- boards whose stable slot matches this run's position.

    Within each group boards are ordered oldest-checked first, so a
    ``max_boards_per_run`` cap sheds the freshest normal-slot board rather than
    starving whichever sorts last. No board can be permanently starved: its slot
    comes round every ``cycle_length`` runs, the overdue rule fires first, and
    overdue excess is carried forward with priority.
    """
    moment = now or datetime.now(timezone.utc)
    slot = int(position) % max(1, int(cycle_length))
    decision = ScheduleDecision(
        position=int(position), cycle_length=int(cycle_length), slot=slot
    )

    overdue: List[Mapping[str, Any]] = []
    retry: List[Mapping[str, Any]] = []
    scheduled: List[Mapping[str, Any]] = []
    valid = 0

    for board in boards:
        provider = str(board.get("provider") or "")
        identifier = str(board.get("identifier") or "")
        if not provider or not identifier:
            decision.reasons["missing_identity"] = decision.reasons.get("missing_identity", 0) + 1
            continue
        valid += 1
        key = f"{provider}:{identifier}"
        checked = _parse(board.get("last_checked_at"))
        age_hours = (
            (moment - checked).total_seconds() / 3600.0 if checked is not None else None
        )
        failures = int(board.get("consecutive_failures") or 0)

        if age_hours is None or (max_age_hours > 0 and age_hours >= max_age_hours):
            overdue.append(board)
            decision.overdue.append(key)
            decision.reasons["overdue"] = decision.reasons.get("overdue", 0) + 1
            continue
        if 0 < failures <= max_retry_attempts:
            retry.append(board)
            decision.retry.append(key)
            decision.reasons["bounded_retry"] = decision.reasons.get("bounded_retry", 0) + 1
            continue
        if slot_for(provider, identifier, cycle_length) == slot:
            scheduled.append(board)
            decision.normal_slot.append(key)
            decision.reasons["slot"] = decision.reasons.get("slot", 0) + 1

    decision.available = valid

    def age_key(board: Mapping[str, Any]):
        checked = _parse(board.get("last_checked_at"))
        return checked or datetime.min.replace(tzinfo=timezone.utc)

    # Overdue ordering: boards carried forward from a previous run's excess come
    # first (so they cannot starve), then oldest-checked first.
    carried = set(str(k) for k in carried_overdue)
    overdue_ordered = sorted(
        overdue, key=lambda b: (0 if _board_key(b) in carried else 1, age_key(b))
    )

    # Overdue quota: keep the herd bounded, carry the rest forward.
    if overdue_cap is not None and overdue_cap >= 0 and len(overdue_ordered) > overdue_cap:
        overdue_take = overdue_ordered[:overdue_cap]
        excess = overdue_ordered[overdue_cap:]
        decision.carried_forward = [_board_key(b) for b in excess]
        decision.reasons["overdue_carried_forward"] = len(excess)
    else:
        overdue_take = overdue_ordered

    overdue_keys = {_board_key(b) for b in overdue_take}
    retry_keys = {_board_key(b) for b in retry}

    ordered = overdue_take + sorted(retry, key=age_key) + sorted(scheduled, key=age_key)
    if max_boards_per_run is not None and max_boards_per_run >= 0:
        if len(ordered) > max_boards_per_run:
            decision.capped_out = len(ordered) - max_boards_per_run
            ordered = ordered[:max_boards_per_run]

    # Tag each selected board with its explicit reason and tally categories.
    selected: List[Dict[str, Any]] = []
    for board in ordered:
        key = _board_key(board)
        copy = dict(board)
        if key in overdue_keys:
            copy["scheduler_reason"] = "overdue"
            decision.selected_overdue += 1
        elif key in retry_keys:
            copy["scheduler_reason"] = "bounded_retry"
            decision.selected_retry += 1
        else:
            copy["scheduler_reason"] = "slot"
            decision.selected_normal += 1
        selected.append(copy)
    decision.selected = selected
    return decision


def select_boards(
    boards: Sequence[Mapping[str, Any]],
    *,
    legacy_due: Optional[Sequence[Mapping[str, Any]]] = None,
    enabled: bool = False,
    mode: Optional[str] = None,
    config: Optional[SchedulerConfig] = None,
    position: int = 0,
    carried_overdue: Sequence[str] = (),
    **kwargs: Any,
) -> ScheduleDecision:
    """The single board-selection entry point. Exactly one algorithm runs.

    Mode resolution, highest precedence first: an explicit ``config.mode``, then
    an explicit ``mode=``, then the legacy ``enabled`` flag
    (``True``->partition, ``False``->legacy). The production default resolves to
    ``legacy_interval`` and returns the ``due_entries`` selection untouched.
    """
    if config is not None:
        resolved_mode = config.mode
        position = config.position
        if not carried_overdue and getattr(config, "carried_overdue", None):
            carried_overdue = config.carried_overdue
        kwargs.setdefault("cycle_length", config.cycle_length)
        kwargs.setdefault("max_boards_per_run", config.board_cap)
        kwargs.setdefault("max_age_hours", config.max_age_hours)
        kwargs.setdefault("max_retry_attempts", config.max_retry_attempts)
        kwargs.setdefault("overdue_cap", config.overdue_cap)
    elif mode is not None:
        resolved_mode = str(mode).strip().lower()
    else:
        resolved_mode = "deterministic_partition" if enabled else "legacy_interval"

    if resolved_mode == "legacy_interval":
        selected = list(legacy_due if legacy_due is not None else boards)
        decision = ScheduleDecision(
            position=int(position), cycle_length=0, slot=-1, mode="legacy_interval"
        )
        decision.selected = [dict(board) for board in selected]
        decision.available = len(decision.selected)
        decision.reasons["legacy_interval_scheduler"] = len(selected)
        return decision
    if resolved_mode == "deterministic_partition":
        return partitioned_schedule(
            boards, position=position, carried_overdue=carried_overdue, **kwargs
        )
    raise SchedulerConfigError(
        f"unknown scheduler mode {resolved_mode!r}; expected one of {SCHEDULER_MODES}"
    )


def simulate(
    boards: Sequence[Mapping[str, Any]],
    *,
    cycle_length: int = DEFAULT_CYCLE_LENGTH,
    cycles: int = 3,
    max_boards_per_run: Optional[int] = None,
    max_age_hours: int = 0,
    overdue_cap: Optional[int] = None,
    carry_forward: bool = False,
) -> Dict[str, Any]:
    """Run the scheduler over whole cycles and report coverage.

    ``max_age_hours=0`` disables the overdue rule so a simulation measures the
    partitioning itself rather than the freshness backstop. Set ``overdue_cap``
    and ``carry_forward=True`` to exercise the quota and carry-forward path -- a
    thundering-herd registry then never selects the whole registry in one run,
    and every carried-forward board is eventually covered.
    """
    visits: Dict[str, int] = {}
    per_run: List[int] = []
    carried: List[str] = []
    max_carried = 0
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # A working copy the simulation may freshen. In carry-forward mode a visited
    # board's ``last_checked_at`` is set to the run moment, so it leaves the
    # overdue pool exactly as a real fetch would -- otherwise a synthetic herd
    # in which nothing ever gets fresher never drains.
    working = [dict(b) for b in boards]
    by_key = {f"{b.get('provider')}:{b.get('identifier')}": b for b in working}
    for run in range(cycle_length * cycles):
        moment = base + timedelta(hours=run)
        decision = partitioned_schedule(
            working,
            position=run,
            cycle_length=cycle_length,
            max_boards_per_run=max_boards_per_run,
            max_age_hours=max_age_hours,
            overdue_cap=overdue_cap,
            carried_overdue=carried if carry_forward else (),
            now=moment,
        )
        per_run.append(len(decision.selected))
        max_carried = max(max_carried, len(decision.carried_forward))
        if carry_forward:
            carried = list(decision.carried_forward)
        for board in decision.selected:
            key = f"{board.get('provider')}:{board.get('identifier')}"
            visits[key] = visits.get(key, 0) + 1
            if carry_forward and key in by_key:
                by_key[key]["last_checked_at"] = moment.isoformat()
    total = len([b for b in boards if b.get("provider") and b.get("identifier")])
    covered = len(visits)
    return {
        "boards": total,
        "cycle_length": cycle_length,
        "cycles": cycles,
        "runs": cycle_length * cycles,
        "boards_per_run": per_run,
        "min_per_run": min(per_run) if per_run else 0,
        "max_per_run": max(per_run) if per_run else 0,
        "covered_boards": covered,
        "full_coverage": covered == total,
        "starved_boards": total - covered,
        "visits_per_board": sorted(set(visits.values())) if visits else [],
        "visits_equal_cycles": all(v == cycles for v in visits.values()) if visits else False,
        "max_carried_forward": max_carried,
        "carry_forward": carry_forward,
        "overdue_cap": overdue_cap,
    }
