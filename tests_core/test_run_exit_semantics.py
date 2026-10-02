"""A completed run below its business target is a SUCCESSFUL process.

Railway marks any non-zero exit CRASHED. `run-target` used to return 1 whenever
the target was unmet, so the 2026-09-18 production run -- which completed
normally at 0/1000 with stop_reason=spend_budget_exhausted -- showed as CRASHED
and read like a code defect for two days.

The contract now:

* completed, no technical failure, target reached      -> exit 0, result target_reached
* completed, no technical failure, below target        -> exit 0, result target_not_reached
* provider credentials refused, a delivery channel that
  achieved nothing, or a systemic provider failure       -> exit 2, result technical_failure
* unhandled exception, database or migration failure    -> propagates, non-zero (unchanged)

The result lands in the run ledger (`run_log`, stage `target`, event `end`)
through TargetRunReport.to_dict().
"""
from __future__ import annotations

from tgtc_core.runner import (
    EXIT_OK, EXIT_TECHNICAL_FAILURE, RESULT_TARGET_NOT_REACHED, RESULT_TARGET_REACHED,
    RESULT_TECHNICAL_FAILURE, TargetRunReport, run_exit_code,
)


def _report(*, target_met=False, stop_reason="spend_budget_exhausted", rounds=None):
    r = TargetRunReport(run_id="t", target=1000, target_met=target_met, stop_reason=stop_reason)
    r.rounds = rounds or [{"round": 1, "acquisition_new_postings": 900, "acquisition_stops": ["page_budget"],
                           "stages": {}, "delivery": {"instantly": {"delivered": 70}, "airtable": {"delivered": 60}}}]
    return r


# --- business outcomes are never a crash -------------------------------------


def test_target_reached_exits_zero():
    r = _report(target_met=True, stop_reason="target_reached")
    assert r.result == RESULT_TARGET_REACHED
    assert run_exit_code(r) == EXIT_OK == 0


def test_budget_exhausted_below_target_exits_zero():
    r = _report(stop_reason="spend_budget_exhausted")
    assert r.result == RESULT_TARGET_NOT_REACHED
    assert run_exit_code(r) == 0


def test_no_progress_below_target_exits_zero():
    assert run_exit_code(_report(stop_reason="no_progress")) == 0


def test_max_rounds_below_target_exits_zero():
    assert run_exit_code(_report(stop_reason="max_rounds_reached")) == 0


def test_the_ledger_records_target_not_reached_explicitly():
    d = _report(stop_reason="spend_budget_exhausted").to_dict()
    assert d["result"] == "target_not_reached"
    assert d["technical_failures"] == []
    assert d["target_met"] is False


def test_a_few_retryable_delivery_failures_beside_successes_are_not_fatal():
    """A working channel with a transient retry is working: the row retries."""
    r = _report(rounds=[{"round": 1, "acquisition_new_postings": 900, "acquisition_stops": [],
                         "stages": {}, "delivery": {"instantly": {"delivered": 70, "failed": 2}}}])
    assert run_exit_code(r) == 0
    assert r.to_dict()["technical_warnings"]


# --- technical failures are non-zero -----------------------------------------


def test_refused_provider_credentials_are_a_technical_failure():
    r = _report(rounds=[{"round": 1, "acquisition_new_postings": 0, "acquisition_stops": ["auth_refused"],
                         "stages": {}, "delivery": {}}])
    assert r.result == RESULT_TECHNICAL_FAILURE
    assert run_exit_code(r) == EXIT_TECHNICAL_FAILURE != 0
    assert any("auth_refused" in f for f in r.technical_failures)


def test_a_delivery_channel_that_achieved_nothing_is_a_technical_failure():
    r = _report(rounds=[{"round": 1, "acquisition_new_postings": 900, "acquisition_stops": [],
                         "stages": {}, "delivery": {"instantly": {"failed": 40}}}])
    assert run_exit_code(r) != 0
    assert any("instantly" in f for f in r.technical_failures)


def test_uncertain_writes_with_no_success_are_a_technical_failure():
    r = _report(rounds=[{"round": 1, "acquisition_new_postings": 900, "acquisition_stops": [],
                         "stages": {}, "delivery": {"instantly": {"uncertain": 5}}}])
    assert run_exit_code(r) != 0


def test_a_systemic_provider_error_with_nothing_acquired_is_a_technical_failure():
    r = _report(rounds=[{"round": 1, "acquisition_new_postings": 0,
                         "acquisition_stops": ["request_error:ConnectionError"], "stages": {}, "delivery": {}}])
    assert run_exit_code(r) != 0


def test_a_provider_error_after_records_were_acquired_is_only_a_warning():
    r = _report(rounds=[{"round": 1, "acquisition_new_postings": 900,
                         "acquisition_stops": ["request_error:ReadTimeout"], "stages": {},
                         "delivery": {"instantly": {"delivered": 10}}}])
    assert run_exit_code(r) == 0
    assert r.to_dict()["technical_warnings"]


def test_blocked_and_deferred_deliveries_are_never_failures():
    """Compliance blocks and canary deferrals are policy working, not breakage."""
    r = _report(rounds=[{"round": 1, "acquisition_new_postings": 900, "acquisition_stops": [],
                         "stages": {}, "delivery": {"instantly": {"blocked": 100, "deferred": 30}}}])
    assert run_exit_code(r) == 0


def test_a_run_whose_rounds_predate_acquisition_stops_still_classifies():
    """Older reports carry no acquisition_stops key; that is not a failure."""
    r = _report(rounds=[{"round": 1, "acquisition_new_postings": 10, "stages": {}, "delivery": {}}])
    assert run_exit_code(r) == 0
