"""psycopg 3 connection helpers.

One rule from the blueprint is enforced by construction here: a transaction is
never held open across a provider call. Services open a transaction, write intent,
commit, call the provider outside any transaction, then open a new transaction to
write the result.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    return conn


@contextlib.contextmanager
def transaction(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Commit on success, roll back on any exception."""
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def jsonb(value) -> Jsonb:
    return Jsonb(value)


#: One production run at a time, whatever started it (the daily cron or a manual
#: execution). A session-level advisory lock on its own connection: it is released
#: when the process exits, however it exits, so it can never be left stale.
RUN_LOCK_KEY = 0x74677463  # "tgtc"


def acquire_run_lock(database_url: str, *, connector=None):
    """Return a connection holding the production run lock, or None if another
    run holds it. The caller keeps the connection open for the whole run."""
    lock_conn = (connector or connect)(database_url)
    lock_conn.autocommit = True
    got = lock_conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (RUN_LOCK_KEY,)).fetchone()["got"]
    if not got:
        lock_conn.close()
        return None
    return lock_conn
