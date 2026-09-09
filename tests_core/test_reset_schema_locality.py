"""Exercise the test-only reset guard without opening or deleting any database."""
from importlib import import_module
from types import SimpleNamespace

import pytest

migration = import_module("tgtc_core.db.migrate")


class RecordingConnection:
    def __init__(self, host, hostaddr):
        self.info = SimpleNamespace(host=host, hostaddr=hostaddr)
        self.statements = []
        self.commits = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self.statements.append(sql)

    def commit(self):
        self.commits += 1


@pytest.mark.parametrize("host, hostaddr", [
    ("/tmp/tgtc_pgtest_4acs7up7", ""),       # pgserver on Linux: Unix socket
    ("/tmp/tgtc_pgtest_with space", ""),
    ("127.0.0.1", "127.0.0.1"),            # pgserver on Windows: loopback TCP
    ("localhost", "127.0.0.1"),
    ("::1", "::1"),
    ("", ""),                              # libpq default local socket
])
def test_reset_accepts_local_socket_and_loopback_connections(monkeypatch, host, hostaddr):
    connection = RecordingConnection(host, hostaddr)
    applied = []
    monkeypatch.setattr(migration, "apply_schema", lambda conn: applied.append(conn))
    migration.reset_schema(connection)
    assert connection.statements == ["DROP SCHEMA public CASCADE; CREATE SCHEMA public;"]
    assert connection.commits == 1 and applied == [connection]


@pytest.mark.parametrize("host, hostaddr", [
    ("db.example.com", "203.0.113.12"),
    ("203.0.113.12", "203.0.113.12"),
    ("10.0.0.2", "10.0.0.2"),
    ("relative/socket", ""),
    ("localhost", "203.0.113.12"),          # hostaddr can override host
    ("/tmp/tgtc_pgtest_fixture", "203.0.113.12"),
    ("", "203.0.113.12"),
])
def test_reset_refuses_nonlocal_connections_before_any_sql(monkeypatch, host, hostaddr):
    connection = RecordingConnection(host, hostaddr)
    applied = []
    monkeypatch.setattr(migration, "apply_schema", lambda conn: applied.append(conn))
    with pytest.raises(RuntimeError, match="refusing to reset"):
        migration.reset_schema(connection)
    assert connection.statements == [] and connection.commits == 0 and applied == []
