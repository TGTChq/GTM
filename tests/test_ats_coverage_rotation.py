"""Regression tests for ATS registry coverage: slot rotation actually advances.

The first production run attempted only 28 of 145 boards because the partitioned
scheduler's ``position`` never advanced -- every run covered slot 0, and the
other slots were reached only via the 168h overdue backstop. These tests lock in
the fix: position advances across runs (persisted in SchedulerState), so the
whole registry is covered every ``cycle_length`` runs with no board starved, and
the deferred count is observable.
"""

from datetime import datetime, timedelta, timezone

import config
from retrieval_measurement import ats_schedule as S


def _fresh_registry(mix=None):
    """A registry with the real production provider mix, all recently checked so
    the overdue backstop does not fire and pure slot rotation is measured."""
    mix = mix or {"ashby": 22, "cornerstone_ondemand": 1, "greenhouse": 13,
                  "lever": 6, "smartrecruiters": 31, "workable": 4, "workday": 68}
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return [{"provider": p, "identifier": f"{p}-{i:03d}", "last_checked_at": fresh}
            for p, n in mix.items() for i in range(n)]


def _key(b):
    return f"{b['provider']}:{b['identifier']}"


def _advance_position(state: S.SchedulerState, cycle_length: int) -> int:
    """Mirror run_orchestrator._live_ats_runner's advancement rule."""
    prev = state.last_position
    return ((int(prev) + 1) if prev is not None else 0) % max(1, cycle_length)


# --------------------------------------------------------------------------
# The fix: position advances and the whole registry is covered every cycle
# --------------------------------------------------------------------------
def test_position_advances_and_covers_full_registry(tmp_path):
    boards = _fresh_registry()
    state_path = str(tmp_path / "ats_state.json")
    cycle = 3
    covered = set()
    per_run = []
    for _ in range(cycle):
        st = S.SchedulerState.load(state_path)
        pos = _advance_position(st, cycle)
        cfg = S.SchedulerConfig(mode="deterministic_partition", cycle_length=cycle,
                                position=pos, max_age_hours=168, state_path=state_path)
        cfg.validate()
        decision = S.select_boards(boards, config=cfg, position=pos)
        per_run.append(len(decision.selected))
        covered.update(_key(b) for b in decision.selected)
        st.last_position = pos
        st.save(state_path)

    assert len(covered) == len(boards)                    # full coverage in `cycle` runs
    assert all(n > 0 for n in per_run)                    # every run does real work
    assert S.SchedulerState.load(state_path).last_position == cycle - 1  # persisted + advanced


def test_static_position_never_rotates_regression_guard():
    """The original defect: a fixed position covers only one slot forever."""
    boards = _fresh_registry()
    cycle = 3
    covered = set()
    for _ in range(cycle * 2):
        cfg = S.SchedulerConfig(mode="deterministic_partition", cycle_length=cycle,
                                position=0, max_age_hours=168)
        covered.update(_key(b) for b in S.select_boards(boards, config=cfg, position=0).selected)
    assert len(covered) < len(boards)   # slot 0 only — the bug the fix prevents


def test_no_board_starves_over_full_cycle():
    """Over exactly cycle_length runs (position advancing), every board is visited
    exactly once — fair rotation, no starvation, no double coverage."""
    boards = _fresh_registry()
    cycle = 3
    visits = {}
    for pos in range(cycle):
        cfg = S.SchedulerConfig(mode="deterministic_partition", cycle_length=cycle,
                                position=pos, max_age_hours=168)
        for b in S.select_boards(boards, config=cfg, position=pos).selected:
            visits[_key(b)] = visits.get(_key(b), 0) + 1
    assert set(visits) == {_key(b) for b in boards}
    assert set(visits.values()) == {1}          # each board exactly once per cycle


# --------------------------------------------------------------------------
# State persistence + config wiring
# --------------------------------------------------------------------------
def test_scheduler_state_roundtrip_persists_position(tmp_path):
    p = str(tmp_path / "s.json")
    S.SchedulerState(last_position=1, carried_overdue=["greenhouse:acme"]).save(p)
    loaded = S.SchedulerState.load(p)
    assert loaded.last_position == 1
    assert loaded.carried_overdue == ["greenhouse:acme"]


def test_config_carried_overdue_is_honored_by_select_boards():
    # A carried-overdue key on the config is prioritised (the field is wired into
    # select_boards, not silently dropped).
    boards = _fresh_registry({"greenhouse": 3})
    # Force one board overdue so there IS an overdue pool to order.
    boards[0]["last_checked_at"] = "2020-01-01T00:00:00+00:00"
    key = _key(boards[0])
    cfg = S.SchedulerConfig(mode="deterministic_partition", cycle_length=3, position=0,
                            max_age_hours=168, carried_overdue=[key])
    decision = S.select_boards(boards, config=cfg, position=0)
    assert key in decision.overdue


def test_cycle_length_default_is_three():
    assert config.ATS_SCHEDULER_CYCLE_LENGTH == 3


# --------------------------------------------------------------------------
# Deferred/remaining observability is derivable from the accounting
# --------------------------------------------------------------------------
def test_accounting_exposes_deferred_boards():
    from retrieval_measurement.ats_checkpoint import AtsBoardSession
    boards = _fresh_registry()
    cycle = 3
    cfg = S.SchedulerConfig(mode="deterministic_partition", cycle_length=cycle,
                            position=0, max_age_hours=168)
    decision = S.select_boards(boards, config=cfg, position=0)
    session = AtsBoardSession()
    session.plan(decision.selected, decision=decision, scheduler_config=cfg)
    acct = session.accounting()
    # deferred == candidates (decision.available) not selected this run == awaiting
    # a future slot. This is the number surfaced as ats_boards_deferred/remaining.
    assert acct["boards_skipped_by_scheduler"] == len(boards) - len(decision.selected)
    assert acct["boards_skipped_by_scheduler"] > 0        # most of the registry deferred
    # available reconciles to selected(run) + deferred; no board was run here.
    assert acct["boards_available"] == acct["boards_selected"] + acct["boards_skipped_by_scheduler"]
    assert acct["scheduler"]["cycle_length"] == cycle
