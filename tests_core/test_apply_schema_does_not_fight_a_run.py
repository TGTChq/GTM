"""An hourly tick must not take a DDL lock on a table the daily run is using.

This is the 2026-10-05 outage as a test. `apply_schema` is written to be idempotent, which
made re-running it on every call look free. It is not: `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS` takes an AccessExclusiveLock even when the column already exists, because Postgres
has to hold the lock to look.

`cmd_replies_poll` called it on every tick, so the full DDL set ran against production once
an hour. Twice it deadlocked the daily run -- the tick's DDL transaction holding
`request_attempts` and wanting `provider_state`, the run holding `provider_state` from
`reserve_probe` and wanting `request_attempts` -- and both runs died unhandled.

The tests below hold the row lock a run holds, then check that the tick's call does not
block on it and that the forced call would. The second half is what proves the first half
is about something real.
"""

from __future__ import annotations

import psycopg
import pytest

from tgtc_core.db import apply_schema, schema_is_current
from tgtc_core.db.migrate import SCHEMA_VERSION


def _hold_provider_state(conn2):
    """What the daily run holds while acquiring: a row lock on `provider_state`."""
    with conn2.cursor() as cur:
        cur.execute(
            "INSERT INTO provider_state (provider, state) VALUES ('apollo', 'serving') "
            "ON CONFLICT (provider) DO UPDATE SET state = 'serving'")
        cur.execute("SELECT provider FROM provider_state WHERE provider = 'apollo' FOR UPDATE")
        assert cur.fetchone() is not None


def test_a_tick_on_a_current_schema_does_no_ddl_and_cannot_be_blocked(conn, conn2):
    """The fix. A caller merely making sure costs one SELECT and takes no DDL lock."""
    assert schema_is_current(conn), "the fixture already applied the schema"
    _hold_provider_state(conn2)                      # the run's row lock, still held

    with conn.cursor() as cur:
        cur.execute("SET lock_timeout = '1500ms'")   # a DDL attempt would fail, not hang
    assert apply_schema(conn) == SCHEMA_VERSION      # must return, not block

    conn2.rollback()


def test_forcing_it_does_try_the_ddl_and_that_is_the_hazard(conn, conn2):
    """Without the early return this is what every hourly tick was doing.

    It is asserted rather than described, because the whole case for the fix rests on the
    DDL lock being real. With the run's row lock held and a short lock_timeout, the forced
    apply cannot get AccessExclusiveLock and says so.
    """
    _hold_provider_state(conn2)

    with conn.cursor() as cur:
        cur.execute("SET lock_timeout = '1500ms'")
    with pytest.raises((psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled)):
        apply_schema(conn, force=True)
    conn.rollback()
    conn2.rollback()


def test_a_database_behind_the_code_still_gets_its_migrations(conn):
    """The early return must not strand a schema. A recorded version lower than the
    code's is exactly the case a deploy creates, and it has to apply."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM schema_migrations WHERE version = %s", (SCHEMA_VERSION,))
    conn.commit()
    assert not schema_is_current(conn)

    assert apply_schema(conn) == SCHEMA_VERSION      # not forced, and it still applies
    assert schema_is_current(conn)


def test_a_database_with_no_schema_at_all_is_not_mistaken_for_current(conn):
    """Not knowing is not the same as knowing it is current."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
    conn.commit()
    assert schema_is_current(conn) is False
    assert apply_schema(conn) == SCHEMA_VERSION
    assert schema_is_current(conn)


def test_the_reply_poll_is_the_caller_that_mattered(conn):
    """It calls apply_schema on every tick; the point is that the call is now cheap."""
    import inspect

    from tgtc_core import __main__ as cli

    source = inspect.getsource(cli.cmd_replies_poll)
    assert "apply_schema(conn)" in source, "still called, so a fresh database is served"
    assert "force=True" not in source, "but never forced from a tick"

    migrate_source = inspect.getsource(cli.cmd_migrate)
    assert "force=True" in migrate_source, "the explicit path still applies migrations"
