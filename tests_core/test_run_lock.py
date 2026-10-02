"""One production run at a time: a second run-target exits without doing anything."""
from __future__ import annotations

from tgtc_core.db.connection import acquire_run_lock


def test_a_second_run_cannot_take_the_lock_while_the_first_holds_it(pg_url):
    first = acquire_run_lock(pg_url)
    assert first is not None
    try:
        assert acquire_run_lock(pg_url) is None
    finally:
        first.close()


def test_the_lock_is_released_when_the_holding_process_ends(pg_url):
    first = acquire_run_lock(pg_url)
    first.close()  # the process exiting closes its connection the same way
    again = acquire_run_lock(pg_url)
    assert again is not None
    again.close()


def test_run_target_exits_cleanly_when_another_run_is_active(pg_url, monkeypatch):
    import argparse

    from tgtc_core import __main__ as cli

    holder = acquire_run_lock(pg_url)
    called = []
    monkeypatch.setattr(cli, "_require_spend_acknowledgement", lambda ack: None)
    monkeypatch.setattr(cli, "_require_persistent_budget", lambda args, s: None)
    monkeypatch.setattr(cli, "_run_target_locked", lambda args, s: called.append(1) or 0)
    try:
        args = argparse.Namespace(i_understand_spend=True, database_url=pg_url)
        assert cli.cmd_run_target(args) == 0
        assert called == []  # nothing ran
    finally:
        holder.close()
    assert cli.cmd_run_target(args) == 0
    assert called == [1]  # with the lock free, the run proceeds
