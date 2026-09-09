"""Apply the schema idempotently and record the migration version."""

from __future__ import annotations

from pathlib import Path

import psycopg

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1


def apply_schema(conn: psycopg.Connection) -> int:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
            (SCHEMA_VERSION,),
        )
    conn.commit()
    return SCHEMA_VERSION


def reset_schema(conn: psycopg.Connection) -> None:
    """TEST ONLY: drop and recreate the public schema."""
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    apply_schema(conn)
