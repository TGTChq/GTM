"""The bootstrap probe cannot spend, migrate, or reveal a DSN in an error."""

import json

import pytest

from tgtc_core import __main__ as cli
from tgtc_core.db import check


def test_missing_database_url_never_opens_a_connection(monkeypatch, capsys):
    monkeypatch.delenv("TGTC_DATABASE_URL", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("check-db must not run a provider or migration")

    monkeypatch.setattr(check.psycopg, "connect", forbidden)
    monkeypatch.setattr(cli, "_runner", forbidden)
    monkeypatch.setattr(cli, "apply_schema", forbidden)
    assert cli.main(["check-db"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "missing_database_url"


def test_connection_error_is_redacted_even_with_provider_keys_present(monkeypatch, capsys):
    dsn = "postgresql://sensitive_user:private_password@unreachable.invalid/private_db"
    for name in ("APOLLO_API_KEY", "FANTASTIC_JOBS_API_KEY", "ANTHROPIC_API_KEY",
                 "AIRTABLE_TOKEN", "INSTANTLY_API_KEY"):
        monkeypatch.setenv(name, "private_provider_secret")
    monkeypatch.setenv("TGTC_DATABASE_URL", dsn)

    def failed_connect(actual_dsn, **kwargs):
        assert actual_dsn == dsn
        assert kwargs["connect_timeout"] == 10
        assert "default_transaction_read_only=on" in kwargs["options"]
        assert "statement_timeout=5000" in kwargs["options"]
        raise check.psycopg.OperationalError(dsn + " private_provider_secret")

    def forbidden(*args, **kwargs):
        raise AssertionError("provider or migration called")

    monkeypatch.setattr(check.psycopg, "connect", failed_connect)
    monkeypatch.setattr(cli, "_runner", forbidden)
    monkeypatch.setattr(cli, "apply_schema", forbidden)
    assert cli.main(["check-db"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "status": "database_check_failed", "error_type": "OperationalError"}
    assert captured.err == ""
    for secret in (dsn, "private_password", "private_provider_secret", "sensitive_user"):
        assert secret not in captured.out


@pytest.mark.parametrize("has_ledger", [False, True])
def test_probe_reports_only_selected_metadata(monkeypatch, has_ledger):
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return self

        def execute(self, sql):
            calls.append(sql)

        def fetchone(self):
            return {"probe": 1, "server_version_num": 160015,
                    "transaction_read_only": "on", "has_migration_ledger": has_ledger}

        def fetchall(self):
            return [{"version": 1}, {"version": 2}, {"version": 3}]

    monkeypatch.setattr(check.psycopg, "connect", lambda *a, **k: Connection())
    result = check.check_database("dbname=disposable_test")
    assert result["status"] == "database_reachable"
    assert result["recorded_schema_versions"] == ([1, 2, 3] if has_ledger else None)
    assert result["read_only"] is True
    assert len(calls) == (2 if has_ledger else 1)
    assert all(sql.startswith("SELECT ") for sql in calls)


def test_real_database_probe_overrides_write_capable_dsn_options(pg_url):
    """The actual server, not a mock, must report a read-only transaction."""
    from psycopg.conninfo import make_conninfo

    dsn = make_conninfo(pg_url, options="-c default_transaction_read_only=off")
    result = check.check_database(dsn)
    assert result["status"] == "database_reachable"
    assert result["read_only"] is True
    assert result["server_version_num"] >= 160000


def test_real_database_probe_does_not_change_schema_or_history(conn, pg_url):
    from tgtc_core.db.migrate import applied_versions

    before = applied_versions(conn)
    conn.commit()
    result = check.check_database(pg_url)
    after = applied_versions(conn)
    assert result["status"] == "database_reachable"
    assert result["recorded_schema_versions"] == before == after
