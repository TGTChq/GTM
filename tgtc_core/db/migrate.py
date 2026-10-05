"""Apply the schema and the ordered migrations idempotently.

``schema.sql`` is the full CURRENT schema (fresh installs). ``migrations/NNN_*.sql`` are
applied in order on top of any older database; each is written with IF NOT EXISTS /
DROP IF EXISTS guards so re-applying is harmless. ``schema_migrations`` records the
highest version applied.
"""

from __future__ import annotations

import re
from ipaddress import ip_address
from pathlib import Path
from typing import List, Tuple

import psycopg

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def migrations() -> List[Tuple[int, Path]]:
    out: List[Tuple[int, Path]] = []
    if MIGRATIONS_DIR.is_dir():
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            m = re.match(r"^(\d+)_", path.name)
            if m:
                out.append((int(m.group(1)), path))
    return out


SCHEMA_VERSION = max([1] + [v for v, _ in migrations()])


def schema_is_current(conn: psycopg.Connection) -> bool:
    """Is every migration already recorded? One cheap read, and no DDL lock.

    A missing table, or anything else that cannot be read, answers False: not knowing
    whether the schema is current is not the same as knowing that it is.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT max(version) AS v FROM schema_migrations")
            row = cur.fetchone()
    except psycopg.Error:
        conn.rollback()
        return False
    try:
        recorded = int((row or {}).get("v") or 0)
    except (TypeError, ValueError):
        return False
    return recorded >= SCHEMA_VERSION


def apply_schema(conn: psycopg.Connection, *, force: bool = False) -> int:
    """Bring the database up to the code's schema. A no-op when it already is.

    The early return is not an optimisation; it is the fix for an outage. Every statement
    below is written to be idempotent, which made re-running them on every call look free.
    It is not: ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` takes an AccessExclusiveLock
    even when the column already exists, because Postgres has to hold the lock to look.

    ``cmd_replies_poll`` called this on every hourly tick, so the full DDL set ran against
    production once an hour. On 2026-10-04 and again on 2026-10-05 it deadlocked the daily
    run: the tick's DDL transaction held ``request_attempts`` and wanted ``provider_state``
    while the run held ``provider_state`` from ``reserve_probe`` and wanted
    ``request_attempts`` to record an attempt. Both runs died unhandled, 70 minutes into
    the second one, after spending 1,932 Fantastic credits.

    So a caller that is merely making sure now costs one SELECT. ``force=True`` keeps the
    explicit path -- ``python -m tgtc_core migrate`` uses it -- so a deploy that adds a
    migration still applies it, and so does the first non-forced call after one, because
    the recorded version is then behind.
    """
    if not force and schema_is_current(conn):
        return SCHEMA_VERSION
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute("INSERT INTO schema_migrations (version) VALUES (1) ON CONFLICT (version) DO NOTHING")
        for version, path in migrations():
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING", (version,))
    conn.commit()
    return SCHEMA_VERSION


def applied_versions(conn: psycopg.Connection) -> List[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        return [int(r["version"]) for r in cur.fetchall()]


def reset_schema(conn: psycopg.Connection) -> None:
    """TEST ONLY: reset a disposable local database, over TCP or a Unix socket."""
    info = conn.info
    host = str(getattr(info, "host", "") or "")
    hostaddr = str(getattr(info, "hostaddr", "") or "")
    # pgserver uses an absolute Unix socket directory on Linux/macOS, with no
    # hostaddr. Windows uses loopback TCP. Check the actual address as well:
    # libpq allows hostaddr to override a seemingly local host name.
    socket_local = host.startswith("/") and not hostaddr
    tcp_local = host in ("", "localhost", "127.0.0.1", "::1")
    if hostaddr:
        try:
            tcp_local = tcp_local and ip_address(hostaddr).is_loopback
        except ValueError:
            tcp_local = False
    if not (socket_local or tcp_local):
        raise RuntimeError(f"refusing to reset a schema on non-local host {host!r}")
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    apply_schema(conn)
