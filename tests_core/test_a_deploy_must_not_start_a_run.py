"""A deployment must not begin a production run.

A Railway cron service runs its start command when a deployment goes live, not
only on the schedule. Measured 2026-10-02: with `cronSchedule` deliberately set
to `None`, deployments at 23:09:28Z and 23:33:22Z each started a full acquisition
run, and the second got past the capacity gate and began buying. Pausing the cron
does not make a deploy safe; this window does.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tgtc_core.services import run_guard as g


def at(hour, minute=0):
    return datetime(2026, 10, 2, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize("hour", [3, 4, 5])
def test_the_scheduled_hours_are_allowed(hour):
    allowed, reason = g.start_allowed(at(hour), env={})
    assert allowed and reason.startswith(g.REASON_INSIDE)


@pytest.mark.parametrize("hour", [0, 1, 2, 6, 12, 17, 22, 23])
def test_every_other_hour_is_declined(hour):
    allowed, reason = g.start_allowed(at(hour), env={})
    assert not allowed and reason.startswith(g.REASON_OUTSIDE)


def test_the_two_real_deploy_started_runs_would_have_been_declined():
    """23:09:28Z and 23:33:22Z on 2026-10-02 -- the ones that actually happened."""
    for moment in (datetime(2026, 10, 2, 23, 9, 28, tzinfo=timezone.utc),
                   datetime(2026, 10, 2, 23, 33, 22, tzinfo=timezone.utc)):
        allowed, reason = g.start_allowed(moment, env={})
        assert not allowed, moment
        assert "23Z_not_in_03-05" in reason


def test_the_0300z_cron_tick_is_still_allowed():
    """The guard must not break the thing it is protecting."""
    allowed, _ = g.start_allowed(datetime(2026, 10, 3, 3, 0, 0, tzinfo=timezone.utc), env={})
    assert allowed


def test_a_deliberate_manual_run_can_force_it():
    allowed, reason = g.start_allowed(at(17), env={g.FORCE_ENV: "1"})
    assert allowed and reason == g.REASON_FORCED


def test_an_unreadable_window_declines_rather_than_allowing_anything():
    """Failing open here would quietly restore the behaviour being prevented."""
    for raw in ("", "banana", "3-", "25-26", "-1"):
        allowed, reason = g.start_allowed(at(4), env={g.WINDOW_ENV: raw})
        assert not allowed, raw
        assert g.REASON_WINDOW_UNREADABLE in reason


@pytest.mark.parametrize("raw, allowed_hours", [
    ("3-5", {3, 4, 5}),
    ("7", {7}),
    ("22-2", {22, 23, 0, 1, 2}),
])
def test_the_window_is_configurable_and_wraps_midnight(raw, allowed_hours):
    for hour in range(24):
        allowed, _ = g.start_allowed(at(hour), env={g.WINDOW_ENV: raw})
        assert allowed == (hour in allowed_hours), (raw, hour)


def test_run_daily_declines_before_acknowledging_spend_or_touching_the_database(monkeypatch):
    """The decline must come first: no spend acknowledgement, no budget claim, no
    run lock. Any of those running on a deploy is the defect."""
    import tgtc_core.__main__ as m

    def boom(*a, **k):
        raise AssertionError("run-daily got past the window guard")

    monkeypatch.setattr(m, "_require_spend_acknowledgement", boom)
    monkeypatch.setattr(m, "_settings", boom)
    monkeypatch.setattr(g, "start_allowed", lambda *a, **k: (False, "outside_scheduled_window:23Z"))

    args = type("A", (), {"i_understand_spend": True, "budget_kind": "scheduled",
                          "database_url": None})()
    assert m.cmd_run_daily(args) == 0


def test_run_daily_proceeds_inside_the_window(monkeypatch):
    """And the guard is not a blanket refusal: inside the window it hands over."""
    import tgtc_core.__main__ as m

    reached = []
    monkeypatch.setattr(g, "start_allowed", lambda *a, **k: (True, "inside_scheduled_window:03-05"))
    monkeypatch.setattr(m, "_require_spend_acknowledgement",
                        lambda *a, **k: reached.append("spend_ack"))
    monkeypatch.setattr(m, "_settings", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")))

    args = type("A", (), {"i_understand_spend": True, "budget_kind": "scheduled",
                          "database_url": None})()
    with pytest.raises(RuntimeError, match="stop here"):
        m.cmd_run_daily(args)
    assert reached == ["spend_ack"]
