"""The paid acceptance CLI must expose provider failures in its exit status."""

import json
from types import SimpleNamespace

from tgtc_core import __main__ as cli


def report(acquisition, *, withheld=None):
    payload = {"acquisition": acquisition, "acquisition_withheld": withheld or {}}
    return SimpleNamespace(
        acquisition=acquisition,
        acquisition_withheld=withheld or {},
        to_dict=lambda: payload,
    )


def test_gate_is_scoped_to_bounded_mode(monkeypatch):
    monkeypatch.delenv("TGTC_ACCEPTANCE_MODE", raising=False)
    failed = report([{"stop_reason": "request_error:http_400", "pages": 0, "rows": 0}])
    assert cli._bounded_acceptance_failure(failed, acquire=True) == ""


def test_gate_fails_a_rejected_provider_request(monkeypatch):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    failed = report([{"stop_reason": "request_error:http_400", "pages": 0, "rows": 0}])
    assert cli._bounded_acceptance_failure(failed, acquire=True) == "request_error:http_400"


def test_gate_allows_a_valid_empty_page_and_a_budget_stop_after_a_page(monkeypatch):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    empty = report([{"stop_reason": "complete", "pages": 1, "rows": 0}])
    bounded = report([{"stop_reason": "spend_budget_exhausted:fantastic:requests", "pages": 1, "rows": 50}])
    assert cli._bounded_acceptance_failure(empty, acquire=True) == ""
    assert cli._bounded_acceptance_failure(bounded, acquire=True) == ""


def test_gate_does_not_fail_if_another_source_served_a_page(monkeypatch):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    mixed = report([
        {"stop_reason": "request_error:http_400", "pages": 0, "rows": 0},
        {"stop_reason": "complete", "pages": 1, "rows": 2},
    ])
    assert cli._bounded_acceptance_failure(mixed, acquire=True) == ""


def test_gate_allows_no_acquire_followup(monkeypatch):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    assert cli._bounded_acceptance_failure(report([]), acquire=False) == ""


def test_cmd_cycle_prints_report_and_returns_failure(monkeypatch, capsys):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    failed = report([{"stop_reason": "request_error:http_400", "pages": 0, "rows": 0}])
    calls = {}

    class Runner:
        def cycle(self, *, acquire, deliver, max_items):
            calls.update(acquire=acquire, deliver=deliver, max_items=max_items)
            return failed

    monkeypatch.setattr(cli, "_settings", lambda: SimpleNamespace(
        database_url="postgresql://test", spend_budget_id="",
    ))
    monkeypatch.setattr(cli, "connect", lambda url: object())
    monkeypatch.setattr(cli, "apply_schema", lambda conn: 4)
    monkeypatch.setattr(cli, "_runner", lambda conn, settings, *, allow_spend: Runner())
    args = SimpleNamespace(
        i_understand_spend=True, budget_id="bounded-test", database_url=None,
        no_acquire=False, no_deliver=True, max_items=10,
    )

    assert cli.cmd_cycle(args) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["acquisition"] == failed.acquisition
    assert captured.err.strip() == "bounded acceptance failed: request_error:http_400"
    assert calls == {"acquire": True, "deliver": False, "max_items": 10}
