"""One authorised scheduled execution per day, surviving the process.

The run lock stops an OVERLAP and nothing else. A deployment starts a cron
service's command, so a second run can begin after the first released the lock —
which is exactly what happened twice on 2026-10-02 (23:09:28Z and 23:33:22Z, both
on budget `prod-scheduled-20261002`). The UTC-hour window narrows that but cannot
close it: a redeploy between 03:00 and 05:59Z is inside the window.

Completed, interrupted and rejected must be told apart BEFORE anything is spent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tgtc_core.services import run_guard
from tgtc_core.services import scheduled_execution as se

NOW = datetime(2026, 10, 3, 3, 0, 0, tzinfo=timezone.utc)


def test_the_first_start_of_the_day_claims_it(conn):
    d = se.claim(conn, run_id="run-a", now=NOW, env={})
    assert d.allowed and d.state == se.CLAIMED and d.attempt == 1
    assert se.state_of(conn, now=NOW)["state"] == "interrupted_or_running"


def test_a_redeploy_INSIDE_the_window_cannot_start_a_second_run(conn):
    """The case the hour window cannot catch."""
    first = se.claim(conn, run_id="run-a", now=NOW, env={})
    assert first.allowed
    assert se.mark_finished(conn, run_id="run-a", outcome="target_reached", now=NOW)

    # a redeploy 40 minutes later: still 03:xx, so the window allows it
    later = NOW + timedelta(minutes=40)
    allowed, _ = run_guard.start_allowed(later, env={})
    assert allowed, "the window alone would permit this start"

    second = se.claim(conn, run_id="run-b", now=later, env={})
    assert not second.allowed
    assert second.state == se.ALREADY_COMPLETED
    assert "run-a" in second.reason


def test_two_simultaneous_starts_yield_exactly_one_claim(conn):
    """The primary key decides, not the order of arrival."""
    a = se.claim(conn, run_id="run-a", now=NOW, env={})
    b = se.claim(conn, run_id="run-b", now=NOW, env={})
    assert [a.allowed, b.allowed] == [True, False]
    assert b.state == se.INTERRUPTED_NEEDS_AUTHORISATION
    assert se.state_of(conn, now=NOW)["run_id"] == "run-a"


def test_a_start_after_daily_end_is_refused_as_a_duplicate(conn):
    se.claim(conn, run_id="run-a", now=NOW, env={})
    se.mark_finished(conn, run_id="run-a", outcome="target_not_reached:x", now=NOW)
    again = se.claim(conn, run_id="run-c", now=NOW, env={})
    assert not again.allowed and again.state == se.ALREADY_COMPLETED


def test_an_interrupted_run_is_not_a_completed_one(conn):
    """A container that died mid-run leaves the claim OPEN. That is interrupted,
    and it must not be mistaken for either completed or free."""
    se.claim(conn, run_id="run-a", now=NOW, env={})      # never finished
    state = se.state_of(conn, now=NOW)
    assert state["state"] == "interrupted_or_running" and state["finished_at"] is None
    blocked = se.claim(conn, run_id="run-b", now=NOW, env={})
    assert not blocked.allowed and blocked.state == se.INTERRUPTED_NEEDS_AUTHORISATION


def test_a_recovery_must_name_exactly_what_it_recovers(conn):
    se.claim(conn, run_id="run-a", now=NOW, env={})
    for bad in ("", "run-a", "20261003:", ":run-a", "20261002:run-a", "20261003:run-z"):
        d = se.claim(conn, run_id="run-b", now=NOW, env={se.RECOVER_ENV: bad})
        assert not d.allowed, bad
        assert d.state in (se.INTERRUPTED_NEEDS_AUTHORISATION, se.RECOVERY_TOKEN_MISMATCH), bad


def test_an_authorised_recovery_after_a_failure_proceeds_and_is_audited(conn):
    se.claim(conn, run_id="run-a", now=NOW, env={})      # died
    d = se.claim(conn, run_id="run-b", now=NOW, env={se.RECOVER_ENV: "20261003:run-a"})
    assert d.allowed and d.state == se.RECOVERING
    assert d.attempt == 2 and d.holder_run_id == "run-a"
    state = se.state_of(conn, now=NOW)
    assert state["run_id"] == "run-b" and state["recovery_of"] == "run-a" and state["attempt"] == 2


def test_a_stale_recovery_token_authorises_nothing_once_the_day_is_done(conn):
    """So it cannot sit in production as a blanket permission."""
    se.claim(conn, run_id="run-a", now=NOW, env={})
    se.claim(conn, run_id="run-b", now=NOW, env={se.RECOVER_ENV: "20261003:run-a"})
    se.mark_finished(conn, run_id="run-b", outcome="target_reached", now=NOW)
    again = se.claim(conn, run_id="run-c", now=NOW, env={se.RECOVER_ENV: "20261003:run-a"})
    assert not again.allowed and again.state == se.ALREADY_COMPLETED


def test_the_day_boundary_matches_the_scheduled_budget_day(conn):
    """A recovery must use the remaining budget of the day it recovers, so the
    execution day and the scheduled budget day have to be the same basis."""
    from tgtc_core.services.budget_policy import budget_id_for

    for moment in (datetime(2026, 10, 3, 0, 1, tzinfo=timezone.utc),
                   datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc),
                   datetime(2026, 10, 3, 23, 59, tzinfo=timezone.utc)):
        assert budget_id_for("scheduled", moment).endswith(se.day_of(moment))
    # a new day is a new claim, and a new budget
    se.claim(conn, run_id="run-a", now=NOW, env={})
    se.mark_finished(conn, run_id="run-a", outcome="target_reached", now=NOW)
    tomorrow = NOW + timedelta(days=1)
    assert se.claim(conn, run_id="run-d", now=tomorrow, env={}).allowed


def test_only_the_holder_may_close_the_day(conn):
    se.claim(conn, run_id="run-a", now=NOW, env={})
    assert se.mark_finished(conn, run_id="run-b", outcome="target_reached", now=NOW) is False
    assert se.mark_finished(conn, run_id="run-a", outcome="target_reached", now=NOW) is True
    # and it closes once
    assert se.mark_finished(conn, run_id="run-a", outcome="target_reached", now=NOW) is False


def test_force_opens_the_hour_window_but_never_the_duplicate_guard(conn):
    """TGTC_RUN_FORCE must not become a way to run the day twice."""
    allowed, reason = run_guard.start_allowed(datetime(2026, 10, 3, 17, tzinfo=timezone.utc),
                                              env={run_guard.FORCE_ENV: "1"})
    assert allowed and reason == run_guard.REASON_FORCED
    se.claim(conn, run_id="run-a", now=NOW, env={})
    se.mark_finished(conn, run_id="run-a", outcome="target_reached", now=NOW)
    d = se.claim(conn, run_id="run-b", now=NOW, env={run_guard.FORCE_ENV: "1"})
    assert not d.allowed and d.state == se.ALREADY_COMPLETED


def test_the_guard_window_matches_the_start_command_s_own_hour_inference():
    """The service start command classifies a run by hour:
    `H=$(date -u +%H); if [ "$H" -ge 3 ] && [ "$H" -le 5 ]` -> scheduled.
    The new guard must agree with it, or the two disagree about what a scheduled
    execution is."""
    assert run_guard.parse_window(None) == (3, 5)
    assert run_guard.DEFAULT_WINDOW == "3-5"
    for hour in range(24):
        start_command_says_scheduled = 3 <= hour <= 5
        guard_allows, _ = run_guard.start_allowed(
            datetime(2026, 10, 3, hour, tzinfo=timezone.utc), env={})
        assert guard_allows == start_command_says_scheduled, hour


def test_a_deploy_inside_the_window_on_an_UNCLAIMED_day_DOES_start_a_run(conn):
    """The residual hole, asserted rather than glossed over.

    I described the two guards as closing "a deploy must not start a run". They do
    not, entirely. The hour window only refuses starts OUTSIDE 03:00-05:59Z, and the
    day claim only refuses a SECOND start. So a deploy between 03:00 and 05:59Z on a
    day whose scheduled run has not claimed yet starts a full run -- and takes the
    day's claim, after which the genuine 03:00Z tick is refused as the duplicate.

    What is genuinely closed: no day can ever run twice, and no start outside the
    window happens at all. What is not: inside the window, the FIRST start wins
    whether it came from the cron or from a deployment.

    Closing it needs something that distinguishes a cron-triggered start from a
    deploy-triggered one. Nothing inside the container does today, so narrowing the
    window to the cron's own minute is the available lever -- and that is a decision
    about the nightly run's start tolerance, not a free fix.
    """
    inside = datetime(2026, 10, 3, 4, 0, 0, tzinfo=timezone.utc)
    allowed, _ = run_guard.start_allowed(inside, env={})
    assert allowed, "04:00Z is inside the window"

    assert se.state_of(conn, now=inside)["state"] == "unclaimed"
    deploy = se.claim(conn, run_id="started-by-a-deployment", now=inside, env={})
    assert deploy.allowed, "the day claim does NOT stop the first start of the day"
    assert deploy.state == se.CLAIMED

    # And the real tick is now the one that loses.
    tick = se.claim(conn, run_id="the-0300z-cron-tick", now=inside + timedelta(minutes=30), env={})
    assert not tick.allowed
