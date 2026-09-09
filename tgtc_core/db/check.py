"""Read-only database connection check, independent of every provider client.

This can run before schema installation. It never applies migrations, drains a
queue, or reports general application readiness. Error text is deliberately not
returned: libpq errors can contain connection parameters.
"""

from __future__ import annotations

import re
from typing import Any, Dict

import psycopg
from psycopg.rows import dict_row


def check_database(database_url: str) -> Dict[str, Any]:
    if not database_url.strip():
        return {"status": "missing_database_url", "variable": "TGTC_DATABASE_URL"}

    try:
        # Keyword options override DSN options. The first transaction is already
        # read-only; there is no write-capable probe before setting that guard.
        with psycopg.connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=10,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
            application_name="tgtc-core-check-db",
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 AS probe, "
                    "current_setting('server_version_num')::integer AS server_version_num, "
                    "current_setting('transaction_read_only') AS transaction_read_only, "
                    "to_regclass('public.schema_migrations') IS NOT NULL AS has_migration_ledger"
                )
                row = cur.fetchone()
                if not row or row["probe"] != 1 or row["transaction_read_only"] != "on":
                    return {"status": "database_check_failed", "error_type": "UnexpectedProbeResult"}
                versions = None
                if row["has_migration_ledger"]:
                    cur.execute("SELECT version FROM public.schema_migrations ORDER BY version")
                    versions = [int(item["version"]) for item in cur.fetchall()]
                return {
                    "status": "database_reachable",
                    "read_only": True,
                    "server_version_num": row["server_version_num"],
                    "migration_ledger_present": row["has_migration_ledger"],
                    "recorded_schema_versions": versions,
                }
    except Exception as exc:  # noqa: BLE001 - structured, secret-free CLI diagnostics
        result = {"status": "database_check_failed", "error_type": type(exc).__name__}
        sqlstate = str(getattr(exc, "sqlstate", "") or "")
        if re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
            result["sqlstate"] = sqlstate
        return result
