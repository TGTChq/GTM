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
