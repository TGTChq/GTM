"""An exhausted ACQUISITION budget must not strand inventory already bought.

Measured, production 2026-09-21: the run stopped with
stop_reason=spend_budget_exhausted the moment Fantastic spent its 3,300
credits, although Apollo had 515 of 1,000 credits left and hundreds of bought
opportunities were still unqualified. Buying and processing are different
budgets; running out of the first is a reason to stop BUYING, not to stop.

The loop now turns acquisition off and keeps processing and delivering until
the backlog stops moving. A processing-stage budget (inference, Apollo) still
stops the run at once, exactly as before.
"""
from __future__ import annotations

from types import SimpleNamespace

from tgtc_core.runner import CycleReport, Runner


def _runner(counts, cycles, *, stall_rounds=1):
    runner = object.__new__(Runner)
    runner.run_id = "t"
    runner.s = SimpleNamespace(approved_target_per_run=1000, target_max_rounds=10, target_stall_rounds=stall_rounds)
    count_iter, cycle_iter = iter(counts), iter(cycles)
    runner._target_counts = lambda: next(count_iter)
    runner.calls = []

    def cycle(**kwargs):
        runner.calls.append(kwargs)
        return next(cycle_iter)

    runner.cycle = cycle
    runner._log = lambda *a, **k: None
    return runner


FANTASTIC_SPENT = CycleReport(
    run_id="t",
    acquisition=[{"new_postings": 500, "stop_reason": "spend_budget_exhausted:fantastic:credits"}],
    stages={"qualify_opportunity": {"approved": 40}}, delivery={"airtable": {"delivered": 40}},
)
DRAINING = CycleReport(run_id="t", stages={"qualify_opportunity": {"approved": 30}},
                       delivery={"airtable": {"delivered": 30}})
IDLE = CycleReport(run_id="t")


def test_acquisition_exhaustion_turns_buying_off_and_keeps_processing():
    runner = _runner(
        [{"approvals_created": 0, "airtable_created": 0}, {"approvals_created": 40, "airtable_created": 40},
         {"approvals_created": 40, "airtable_created": 40}, {"approvals_created": 70, "airtable_created": 70},
         {"approvals_created": 70, "airtable_created": 70}, {"approvals_created": 70, "airtable_created": 70}],
        [FANTASTIC_SPENT, DRAINING, IDLE],
    )
    out = runner.run_to_target(target=1000)
    assert [c["acquire"] for c in runner.calls] == [True, False, False]
    assert out.rounds_completed == 3
    assert out.airtable_created == 70
    assert out.stop_reason == "acquisition_budget_exhausted_backlog_drained"
    assert out.result == "target_not_reached"


def test_a_processing_budget_still_stops_the_run_immediately():
    report = CycleReport(run_id="t", stages={"qualify_opportunity": {"budget_exhausted": 3}},
                         delivery={"airtable": {}})
    runner = _runner([{"approvals_created": 0, "airtable_created": 0}, {"approvals_created": 5, "airtable_created": 5}],
                     [report])
    out = runner.run_to_target(target=1000)
    assert out.stop_reason == "spend_budget_exhausted"
    assert out.rounds_completed == 1


def test_target_reached_while_draining_still_wins():
    runner = _runner(
        [{"approvals_created": 0, "airtable_created": 0}, {"approvals_created": 990, "airtable_created": 990},
         {"approvals_created": 990, "airtable_created": 990}, {"approvals_created": 1000, "airtable_created": 1000}],
        [FANTASTIC_SPENT, DRAINING],
    )
    out = runner.run_to_target(target=1000)
    assert out.target_met is True and out.stop_reason == "target_reached"


def test_no_progress_without_any_budget_stop_is_unchanged():
    runner = _runner([{"approvals_created": 0, "airtable_created": 0}, {"approvals_created": 0, "airtable_created": 0}],
                     [IDLE])
    assert runner.run_to_target(target=1000).stop_reason == "no_progress"
