"""The paid acceptance CLI must expose provider failures in its exit status."""

import json
from types import SimpleNamespace

import pytest

from tgtc_core import __main__ as cli


def report(acquisition, *, withheld=None, stages=None):
    payload = {"acquisition": acquisition, "acquisition_withheld": withheld or {}, "stages": stages or {}}
    return SimpleNamespace(
        acquisition=acquisition,
        acquisition_withheld=withheld or {},
        stages=stages or {},
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


@pytest.mark.parametrize("acquire", [False, True])
@pytest.mark.parametrize("stage", ["resolve_identity", "classify", "qualify_opportunity"])
@pytest.mark.parametrize("counter", ["technical_failure", "error_retry", "retry", "lease_lost"])
def test_gate_fails_stage_errors_even_without_acquisition(monkeypatch, acquire, stage, counter):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    failed = report([{"stop_reason": "complete", "pages": 1, "rows": 50}],
                    stages={stage: {counter: 4, "closed": 2, "approved": 1}})
    assert cli._bounded_acceptance_failure(failed, acquire=acquire) == f"{stage}: {counter}=4"


def test_gate_ignores_business_outcomes_and_budget_stops(monkeypatch):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    valid = report([], stages={
        "classify": {"closed": 8, "classified": 4, "wait": 1, "budget_exhausted": 2},
        "qualify_opportunity": {"closed": 12, "wait": 2, "budget_exhausted": 1, "technical_failure": 0},
    })
    assert cli._bounded_acceptance_failure(valid, acquire=False) == ""


def test_gate_reports_each_failed_stage(monkeypatch):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    failed = report([], stages={"classify": {"technical_failure": 4}, "qualify_opportunity": {"retry": 2}})
    assert cli._bounded_acceptance_failure(failed, acquire=False) == (
        "classify: technical_failure=4; qualify_opportunity: retry=2"
    )


@pytest.mark.parametrize("no_acquire, failure_reason", [
    (False, "request_error:http_400"),
    (True, "classify: technical_failure=4"),
])
def test_cmd_cycle_prints_report_and_returns_failure(monkeypatch, capsys, no_acquire, failure_reason):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    failed = (report([], stages={"classify": {"technical_failure": 4, "wait": 4}}) if no_acquire else
              report([{"stop_reason": "request_error:http_400", "pages": 0, "rows": 0}]))
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
        no_acquire=no_acquire, no_deliver=True, max_items=10,
    )

    assert cli.cmd_cycle(args) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["acquisition"] == failed.acquisition
    assert captured.err.strip() == f"bounded acceptance failed: {failure_reason}"
    assert calls == {"acquire": not no_acquire, "deliver": False, "max_items": 10}


@pytest.mark.parametrize("mode, counts, expected", [
    ("bounded", {"technical_failure": 4, "wait": 4}, 1),
    ("bounded", {"error_retry": 4}, 1),
    ("bounded", {"retry": 1}, 1),
    ("bounded", {"closed": 50}, 0),
    ("bounded", {"budget_exhausted": 1, "approved": 2}, 0),
    ("", {"error_retry": 4, "technical_failure": 2}, 0),
])
@pytest.mark.parametrize("kind", ["classify", "qualify_opportunity"])
def test_cmd_work_preserves_report_and_gates_only_bounded_failures(monkeypatch, capsys, mode, counts, expected, kind):
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", mode)
    calls = {}

    class Runner:
        def work(self, kind, *, max_items):
            calls.update(kind=kind, max_items=max_items)
            return counts

    monkeypatch.setattr(cli, "_settings", lambda: SimpleNamespace(database_url="postgresql://test", spend_budget_id=""))
    monkeypatch.setattr(cli, "connect", lambda url: object())
    monkeypatch.setattr(cli, "_runner", lambda conn, settings, *, allow_spend: Runner())
    args = SimpleNamespace(i_understand_spend=True, budget_id="bounded-test", database_url=None,
                           kind=kind, max_items=50)
    assert cli.cmd_work(args) == expected
    captured = capsys.readouterr()
    assert json.loads(captured.out) == counts
    assert bool(captured.err) == bool(expected)
    assert calls == {"kind": kind, "max_items": 50}


def test_unbounded_cycle_preserves_best_effort_stage_semantics(monkeypatch):
    monkeypatch.delenv("TGTC_ACCEPTANCE_MODE", raising=False)
    failed = report([], stages={"classify": {"error_retry": 4, "technical_failure": 4}})
    assert cli._bounded_acceptance_failure(failed, acquire=False) == ""
