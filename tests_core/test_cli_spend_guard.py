"""A refused execution must not connect, migrate or construct provider clients."""

import pytest

from tgtc_core import __main__ as cli


@pytest.mark.parametrize("command", [
    ["cycle"],
    ["work", "--kind", "classify"],
    ["deliver"],
])
def test_missing_spend_acknowledgement_refuses_before_database(command, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a refused command reached database or provider setup")

    monkeypatch.setattr(cli, "connect", forbidden)
    monkeypatch.setattr(cli, "apply_schema", forbidden)
    monkeypatch.setattr(cli, "_runner", forbidden)
    with pytest.raises(SystemExit, match="without --i-understand-spend"):
        cli.main(command)


@pytest.mark.parametrize("command", [
    ["cycle"],
    ["work", "--kind", "classify"],
    ["deliver"],
])
def test_acknowledged_command_reaches_database_setup(command, monkeypatch):
    class ReachedDatabase(Exception):
        pass

    def stop_before_connection(*args, **kwargs):
        raise ReachedDatabase

    monkeypatch.setattr(cli, "connect", stop_before_connection)
    with pytest.raises(ReachedDatabase):
        cli.main(command + ["--i-understand-spend"])
