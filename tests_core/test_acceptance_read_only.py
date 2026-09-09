"""Acceptance CLI isolation: no provider calls or DB writes are permitted."""

import pytest

from tgtc_core import __main__ as cli


@pytest.mark.parametrize("command", [
    ["cycle"], ["work", "--kind", "classify"], ["deliver"], ["migrate"],
    ["import-airtable"], ["prune"], ["demo"], ["ledger"],
])
def test_read_only_mode_blocks_before_dispatch_even_with_spend_ack(monkeypatch, command):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "read_only")
    name = command[0].replace("-", "_")
    invoked = []
    monkeypatch.setattr(cli, "cmd_" + name, lambda args: invoked.append(True))
    with pytest.raises(SystemExit, match="allows only describe and check-db"):
        cli.main(command + ["--i-understand-spend"])
    assert invoked == []


@pytest.mark.parametrize("command", ["describe", "check-db"])
def test_read_only_mode_allows_only_the_two_inspections(monkeypatch, command):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "read_only")
    invoked = []

    def inspect(args):
        invoked.append(args.cmd)
        return 0

    monkeypatch.setattr(cli, "cmd_" + command.replace("-", "_"), inspect)
    assert cli.main([command]) == 0
    assert invoked == [command]


def test_unknown_mode_fails_closed_without_echoing_value(monkeypatch):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "private-invalid-value")
    monkeypatch.setattr(cli, "cmd_describe", lambda args: pytest.fail("must not dispatch"))
    with pytest.raises(SystemExit, match="invalid TGTC_ACCEPTANCE_MODE") as caught:
        cli.main(["describe"])
    assert "private-invalid-value" not in str(caught.value)


def test_normal_runtime_keeps_existing_dispatch_when_mode_is_unset(monkeypatch):
    monkeypatch.delenv("TGTC_ACCEPTANCE_MODE", raising=False)
    monkeypatch.setattr(cli, "cmd_migrate", lambda args: 17)
    assert cli.main(["migrate"]) == 17
