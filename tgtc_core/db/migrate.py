"""Apply the schema and the ordered migrations idempotently.

``schema.sql`` is the full CURRENT schema (fresh installs). ``migrations/NNN_*.sql`` are
applied in order on top of any older database; each is written with IF NOT EXISTS /
DROP IF EXISTS guards so re-applying is harmless. ``schema_migrations`` records the
highest version applied.
"""

from __future__ import annotations

import re
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


def apply_schema(conn: psycopg.Connection) -> int:
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
    """TEST ONLY: drop and recreate the public schema. Refuses non-local hosts."""
    info = conn.info
    host = str(getattr(info, "host", "") or "")
    if host not in ("", "localhost", "127.0.0.1", "::1"):
        raise RuntimeError(f"refusing to reset a schema on non-local host {host!r}")
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    apply_schema(conn)
