"""New capture cannot be the denominator of mixed new/recovered review."""
from pathlib import Path

from weekly_report.metrics import build_run_metrics
from weekly_report.run_artifacts import RunRecord


def test_recovery_makes_the_review_rate_incomparable_in_artifacts_and_ledger():
    # The bad 50% rate looks plausible: 5 reviewed includes recovered work while
    # some of the 10 newly captured postings never entered review.
    artifacts = RunRecord("r1", Path("unused"), artifacts={"orchestrator_result": {
        "acquisition": {"cumulative": {"net_new_jobs_captured": 10,
                                       "pending_work_resumed": {"adopted": 3}}},
        "enrichment": {"funnel": {"qualification_input": 5}}}})
    ledger = RunRecord("r1", Path("unused"), artifacts={"ledger": {"metrics": {
        "net_new_jobs_captured": 10, "jobs_reviewed": 5, "postings_resumed": 3}}})
    for run in (artifacts, ledger):
        metrics = build_run_metrics([run])
        assert metrics["jobs_captured"].value == 10
        assert metrics["jobs_reviewed"].value == 5
        assert metrics["review_rate_pct"].value is None
        assert metrics["jobs_captured"].cohort != metrics["jobs_reviewed"].cohort


def test_no_recovery_preserves_the_comparable_review_rate():
    run = RunRecord("r1", Path("unused"), artifacts={"ledger": {"metrics": {
        "net_new_jobs_captured": 10, "jobs_reviewed": 5, "postings_resumed": 0}}})
    assert build_run_metrics([run])["review_rate_pct"].value == 50


def test_zero_acquisition_with_recovered_review_is_not_an_empty_pipeline():
    from weekly_report.bottleneck import identify
    run = RunRecord("r1", Path("unused"), artifacts={"ledger": {"metrics": {
        "net_new_jobs_captured": 0, "jobs_reviewed": 3, "postings_resumed": 3,
        "contacts_found": 2}}})
    result = identify(build_run_metrics([run]), run_count=1, reasons={}, runs=[run])
    assert result.kind != "acquisition_entry"
    assert "no funnel stage had input" not in result.statement


def test_executed_recovery_report_equals_ledger_only_after_artifact_retention(tmp_path):
    import json
    import shutil
    from datetime import date
    from tests.test_throughput_contract import RecoveryProductionLoop
    from weekly_report.report import build_report
    from weekly_report.render import render_stakeholder_summary
    from weekly_report.timewindow import explicit_window

    _, _, _, root = RecoveryProductionLoop().exercise(7, 3, 1000)
    entry = json.loads(next((root / "reporting_ledger").glob("*.json")).read_text())
    assert entry["metrics"]["postings_resumed"] == 7
    window = explicit_window(date(2026, 9, 5), date(2026, 9, 12),
                             boundary_hour=0, tz_name="America/Los_Angeles")
    before = build_report(window, artifact_roots=[str(root)])
    retained = tmp_path / "ledger-only"
    shutil.copytree(root, retained)
    shutil.rmtree(retained / "run_artifacts")
    after = build_report(window, artifact_roots=[str(retained)])
    assert before.run_ids == after.run_ids
    assert render_stakeholder_summary(before) == render_stakeholder_summary(after)
    for key, metric in before.metrics.items():
        other = after.metrics[key]
        assert (metric.value, metric.status, metric.counted_unit, metric.cohort) == (
            other.value, other.status, other.counted_unit, other.cohort)
    assert before.metrics["jobs_captured"].value == 0
    assert before.metrics["jobs_reviewed"].value == 7
    assert before.metrics["review_rate_pct"].value is None
