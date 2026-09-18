"""The production target is run-attributed Airtable creations, not activity."""

from types import SimpleNamespace

from tgtc_core.runner import CycleReport, Runner


def _runner(counts, cycles, *, stall_rounds=2):
    runner = object.__new__(Runner)
    runner.run_id = "target-test"
    runner.s = SimpleNamespace(
        approved_target_per_run=1000,
        target_max_rounds=10,
        target_stall_rounds=stall_rounds,
    )
    count_iter = iter(counts)
    cycle_iter = iter(cycles)
    runner._target_counts = lambda: next(count_iter)
    runner.cycle = lambda **kwargs: next(cycle_iter)
    runner._log = lambda *args, **kwargs: None
    return runner


def test_target_requires_attributed_airtable_receipts():
    report = CycleReport(
        run_id="target-test",
        stages={"qualify_opportunity": {"approved": 500, "approved_leads": 1000}},
        delivery={"airtable": {"delivered": 1000}},
    )
    runner = _runner(
        [
            {"approvals_created": 0, "airtable_created": 0},
            {"approvals_created": 1000, "airtable_created": 1000},
        ],
        [report],
    )
    out = runner.run_to_target(target=1000, max_rounds=5)
    assert out.target_met is True
    assert out.stop_reason == "target_reached"
    assert out.approvals_created == out.airtable_created == 1000


def test_dry_run_never_claims_the_airtable_target():
    report = CycleReport(
        run_id="target-test",
        stages={"qualify_opportunity": {"approved": 400, "approved_leads": 1000}},
        delivery={"airtable": {"withheld": 1}, "instantly": {"withheld": 1}},
    )
    runner = _runner(
        [
            {"approvals_created": 0, "airtable_created": 0},
            {"approvals_created": 1000, "airtable_created": 0},
        ],
        [report],
    )
    out = runner.run_to_target(target=1000, deliver=False)
    assert out.target_met is False
    assert out.stop_reason == "approval_target_reached_without_airtable_delivery"


def test_budget_boundary_is_visible_non_success():
    report = CycleReport(
        run_id="target-test",
        stages={"classify": {"budget_exhausted": 1}},
        delivery={"airtable": {}},
    )
    runner = _runner(
        [
            {"approvals_created": 0, "airtable_created": 0},
            {"approvals_created": 87, "airtable_created": 87},
        ],
        [report],
    )
    out = runner.run_to_target(target=1000)
    assert out.target_met is False
    assert out.stop_reason == "spend_budget_exhausted"
    assert out.airtable_created == 87


def test_target_delivery_is_airtable_only():
    calls = []
    report = CycleReport(run_id="target-test", delivery={"airtable": {}})
    runner = _runner(
        [
            {"approvals_created": 0, "airtable_created": 0},
            {"approvals_created": 1, "airtable_created": 1},
        ],
        [report],
    )

    def cycle(**kwargs):
        calls.append(kwargs)
        return report

    runner.cycle = cycle
    runner.run_to_target(target=1)
    assert calls[0]["delivery_channels"] == ("airtable",)

