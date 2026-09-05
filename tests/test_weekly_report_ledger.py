"""The compact reporting ledger, and the weekly report built on top of it.

These tests encode the 2026-W36 incident as executable requirements. That report
told a stakeholder the week had 4 pipeline runs. It had 7. The other three had
been deleted by ``RETENTION_KEEP_RUNS=4`` before the report ran, and the reporter
declared ``problems: []`` while missing them -- a pruned run was not "missing" to
it, it was invisible.

The headline that week happened to be correct (all 7 runs captured 0 jobs), which
is exactly why the architecture had to be fixed rather than the number: the same
code would have silently under-reported any productive run that aged out.

So the contract under test is:

* heavy artifacts may be pruned as aggressively as storage requires;
* the compact ledger is a separate store that pruning cannot reach;
* a week is reconstructed from the ledger, not from surviving evidence;
* a run that started is visible even if it was killed mid-flight;
* a counter that was never measured stays unavailable, never becomes 0.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import run_weekly_report
from orchestrator.modes import ExecutionMode, policy_for
from orchestrator.run_ledger import (
    LEDGER_SCHEMA,
    LEDGER_STORE,
    STATE_COMPLETE,
    STATE_INTERRUPTED,
    STATE_RUNNING,
    RunLedger,
    prune_ledger,
    read_entries,
)
from orchestrator.state import StateManager
from test_weekly_report import write_run
from weekly_report.evidence import STATUS_MEASURED, STATUS_PARTIAL, STATUS_UNAVAILABLE
from weekly_report.render import render_stakeholder_summary
from weekly_report.report import build_report
from weekly_report.run_artifacts import discover_runs, select_window
from weekly_report.timewindow import explicit_window, weekly_window

UTC = timezone.utc


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _instant(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def write_ledger_run(
    root: Path,
    run_id: str,
    *,
    started: str,
    finished: Optional[str] = None,
    state: str = STATE_COMPLETE,
    metrics: Optional[Dict[str, Any]] = None,
    stop_reason: str = "",
    mode: str = "live_acquisition_and_enrichment",
    allow_enrollment: bool = False,
    lanes: tuple = ("fantastic",),
) -> RunLedger:
    """One ledger entry, written through the REAL writer the pipeline uses.

    ``state=STATE_RUNNING`` leaves the entry unfinalized, which is exactly what a
    hard-killed run leaves behind.
    """
    ledger = RunLedger(root, run_id)
    ledger.begin(
        started_at=_instant(started),
        mode=mode,
        allow_network=True,
        allow_enrichment=True,
        allow_instantly_enrollment=allow_enrollment,
        lanes=lanes,
    )
    if metrics:
        ledger.record("acquisition", metrics)
    if state != STATE_RUNNING:
        ledger.finalize(
            state=state,
            status=state,
            stop_reason=stop_reason,
            finished_at=_instant(finished or started),
        )
    return ledger


def _week_window():
    """The Aug 28 - Sep 3 2026 Pacific week the incident report covered."""
    return explicit_window(
        datetime(2026, 8, 28).date(), datetime(2026, 9, 4).date(),
        boundary_hour=0, tz_name="America/Los_Angeles",
    )


# --------------------------------------------------------------------------
# the ledger store itself
# --------------------------------------------------------------------------


def test_ledger_entry_exists_before_any_work_happens(tmp_path):
    """The entry is created at begin(), not at the end. That is the whole point."""
    ledger = RunLedger(tmp_path, "20260904T064411Z-66ea967e")
    ledger.begin(started_at=_instant("2026-09-04T06:44:11Z"), mode="live", lanes=["fantastic"])
    assert ledger.path.is_file()
    entry = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert entry["schema"] == LEDGER_SCHEMA
    assert entry["state"] == STATE_RUNNING
    assert entry["started_at"] == "2026-09-04T06:44:11Z"
    assert ledger.errors == []


def test_a_measured_zero_and_an_absent_counter_are_different_facts(tmp_path):
    ledger = RunLedger(tmp_path, "r1")
    ledger.begin(started_at=_instant("2026-09-01T13:00:00Z"))
    ledger.record("acquisition", {"jobs_captured": 0, "jobs_reviewed": None})
    entry = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert entry["metrics"]["jobs_captured"] == 0, "a real zero is recorded"
    assert "jobs_reviewed" not in entry["metrics"], "an unmeasured counter is absent, not 0"


def test_ledger_writes_are_atomic_and_leave_no_temp_files(tmp_path):
    ledger = RunLedger(tmp_path, "r1")
    ledger.begin(started_at=_instant("2026-09-01T13:00:00Z"))
    for n in range(5):
        ledger.record("acquisition", {"jobs_captured": n})
    ledger.finalize(state=STATE_COMPLETE, finished_at=_instant("2026-09-01T13:05:00Z"))
    files = sorted(p.name for p in (tmp_path / LEDGER_STORE).iterdir())
    assert files == ["r1.json"], "no .tmp residue survives a sequence of updates"
    assert json.loads(ledger.path.read_text(encoding="utf-8"))["metrics"]["jobs_captured"] == 4


def test_a_foreign_schema_is_refused_rather_than_reinterpreted(tmp_path):
    directory = tmp_path / LEDGER_STORE
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text(
        json.dumps({"schema": "something-else/9", "run_id": "bad", "metrics": {"jobs_captured": 999}}),
        encoding="utf-8",
    )
    entries, problems = read_entries(tmp_path)
    assert entries == []
    assert problems and "bad.json" in problems[0], "declared, never silently skipped"


# --------------------------------------------------------------------------
# retention: heavy artifacts are pruned, the ledger is not
# --------------------------------------------------------------------------


def test_heavy_artifact_pruning_cannot_delete_the_reporting_ledger(tmp_path):
    """The requirement in one test: prune everything it is allowed to, keep the ledger.

    Seven daily runs, each with a heavy run directory well over the size cap, and a
    ledger entry. Retention is run with the production policy.
    """
    root = tmp_path / "orchestrator_v2"
    state = StateManager(root, policy_for(ExecutionMode.PRODUCTION), run_id="latest")
    for day in range(28, 32):
        run_id = f"2026083{day - 28}T130000Z-{day:08x}"
        write_ledger_run(
            root, run_id,
            started=f"2026-08-{day:02d}T13:00:00Z",
            finished=f"2026-08-{day:02d}T13:05:00Z",
            metrics={"jobs_captured": 100},
        )
        run_dir = root / "run_artifacts" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_status.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        # A heavy artifact, comfortably over the byte budget used below.
        (run_dir / "enrichment_progress.json").write_bytes(b"x" * 200_000)

    ledger_before = sorted(p.name for p in (root / LEDGER_STORE).iterdir())
    assert len(ledger_before) == 4

    result = state.prune(keep=1, max_bytes=1_000, protect={"latest"})

    assert result["removed"], "heavy artifacts were pruned aggressively, as intended"
    ledger_after = sorted(p.name for p in (root / LEDGER_STORE).iterdir())
    assert ledger_after == ledger_before, "the compact ledger is untouchable by prune"


def test_the_size_budget_is_measured_against_what_prune_can_actually_delete(tmp_path):
    """Regression: ``dir_size()`` used to measure the WHOLE root.

    ``checkpoints`` alone was 166 MB against a 600 MB cap in production, so the
    size loop could delete every non-keeper run and still be over budget -- a
    retention policy that empties the store it is meant to bound.
    """
    root = tmp_path / "orchestrator_v2"
    state = StateManager(root, policy_for(ExecutionMode.PRODUCTION), run_id="newest")
    # An unprunable neighbour far larger than the cap.
    (root / "checkpoints" / "big.json").write_bytes(b"x" * 500_000)
    for name in ("run_a", "run_b", "run_c"):
        run_dir = root / "run_artifacts" / name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_status.json").write_text(json.dumps({"run_id": name}), encoding="utf-8")
        (run_dir / "blob.bin").write_bytes(b"y" * 1_000)

    state.prune(keep=3, max_bytes=100_000, protect={"newest"})

    survivors = sorted(p.name for p in (root / "run_artifacts").iterdir())
    assert survivors == ["run_a", "run_b", "run_c"], (
        "run_artifacts is far under the cap; the oversized checkpoints store must "
        "not cause its runs to be deleted"
    )


def test_ledger_retention_is_generous_and_bounded(tmp_path):
    now = datetime(2026, 9, 4, tzinfo=UTC)
    for age in (400, 200, 100, 10):
        stamp = (now - timedelta(days=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_ledger_run(tmp_path, f"run-{age}", started=stamp, finished=stamp)

    result = prune_ledger(tmp_path, keep_days=180, now=now)

    kept = {p.stem for p in (tmp_path / LEDGER_STORE).iterdir()}
    assert kept == {"run-100", "run-10"}, "entries inside the retention horizon survive"
    assert sorted(result["removed"]) == ["run-200", "run-400"]


def test_eight_weeks_of_daily_entries_cost_almost_nothing(tmp_path):
    """Disk-cost check: the store must be negligible next to one heavy run (233 MB)."""
    start = datetime(2026, 7, 1, 13, tzinfo=UTC)
    for day in range(56):
        stamp = (start + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_ledger_run(
            tmp_path, f"run{day:03d}", started=stamp, finished=stamp,
            metrics={"jobs_captured": 6206, "jobs_reviewed": 5746,
                     "qualified_opportunities": 1264, "contacts_found": 613,
                     "sent_to_airtable": 400},
        )
    total = sum(p.stat().st_size for p in (tmp_path / LEDGER_STORE).glob("*.json"))
    assert total < 1_000_000, f"8 weeks of daily entries must stay under 1 MB, got {total}"


# --------------------------------------------------------------------------
# weekly reconstruction
# --------------------------------------------------------------------------


def _seven_run_week(root: Path, *, captured: int, reviewed: Optional[int] = None,
                    heavy_for: Optional[List[str]] = None) -> List[str]:
    """The Aug 28 - Sep 3 week: seven daily runs at 13:0xZ."""
    run_ids: List[str] = []
    for offset in range(7):
        day = datetime(2026, 8, 28, 13, 1, tzinfo=UTC) + timedelta(days=offset)
        stamp = day.strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = day.strftime("%Y%m%dT%H%M%SZ") + f"-{offset:08x}"
        run_ids.append(run_id)
        metrics: Dict[str, Any] = {"jobs_captured": captured}
        if reviewed is not None:
            metrics["jobs_reviewed"] = reviewed
        write_ledger_run(
            root, run_id, started=stamp,
            finished=(day + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            metrics=metrics, stop_reason="topup:governor_zero_budget" if not captured else "",
        )
        if heavy_for is not None and run_id in heavy_for:
            write_run(root, run_id, finished=stamp, postings=captured,
                      reviewed=reviewed, qualified=None, contacts=None, created=None)
    return run_ids


def test_a_full_week_is_reported_when_only_the_last_runs_still_have_artifacts(tmp_path):
    """The headline requirement: 7 daily runs, heavy evidence for only the last 4.

    This is the exact production shape on 2026-09-04, and the old reader saw 4.
    """
    root = tmp_path / "orchestrator_v2"
    run_ids = _seven_run_week(root, captured=100, reviewed=90, heavy_for=None)
    for run_id in run_ids[3:]:  # only the newest four keep their evidence
        write_run(root, run_id, finished=None, started=None,
                  postings=100, reviewed=90, qualified=None, contacts=None, created=None)

    report = build_report(_week_window(), artifact_roots=[root])

    assert report.metrics["jobs_captured"].value == 700, "all seven runs contribute"
    assert len(report.runs) == 7
    assert set(report.run_ids) == set(run_ids)
    ledger_only = report.to_dict()["provenance"]["runs_reported_from_ledger_only"]
    assert set(ledger_only) == set(run_ids[:3]), "the pruned runs are named, not hidden"


def test_the_zero_capture_week_reconstructs_to_seven_scheduled_runs(tmp_path):
    """2026-W36 as it actually was: 7 runs, every one of them capturing nothing.

    The delivered report said 4. The headline (0) was right; the run count was not.
    """
    root = tmp_path / "orchestrator_v2"
    run_ids = _seven_run_week(root, captured=0)

    report = build_report(_week_window(), artifact_roots=[root])

    assert len(report.runs) == 7, "the week had seven scheduled runs"
    assert report.metrics["jobs_captured"].status == STATUS_MEASURED
    assert report.metrics["jobs_captured"].value == 0, "a genuine, measured zero"
    assert report.metrics["jobs_reviewed"].status == STATUS_UNAVAILABLE, (
        "enrichment never ran on any input, so jobs_reviewed is unavailable -- not 0"
    )
    assert report.metrics["jobs_reviewed"].value is None
    assert set(report.run_ids) == set(run_ids)


def test_a_positive_week_sums_across_every_run(tmp_path):
    root = tmp_path / "orchestrator_v2"
    _seven_run_week(root, captured=6206, reviewed=5746)

    report = build_report(_week_window(), artifact_roots=[root])

    assert report.metrics["jobs_captured"].value == 7 * 6206
    assert report.metrics["jobs_reviewed"].value == 7 * 5746
    assert report.metrics["jobs_captured"].status == STATUS_MEASURED
    assert report.metrics["review_rate_pct"].value == pytest.approx(92.6, abs=0.1)


def test_a_completed_run_carries_every_counter_it_measured(tmp_path):
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(
        root, "20260903T130019Z-65ca91e1",
        started="2026-09-03T13:00:19Z", finished="2026-09-03T13:01:26Z",
        metrics={
            "jobs_captured": 6206, "unique_opportunities": 6000, "jobs_reviewed": 5746,
            "qualified_opportunities": 1264, "contacts_found": 613,
            "final_pass_leads": 425, "verified_emails": 606,
            "sent_to_airtable": 400, "airtable_suppressed": 13,
        },
    )
    report = build_report(_week_window(), artifact_roots=[root])
    values = {key: report.metrics[key].value for key in (
        "jobs_captured", "jobs_reviewed", "qualified_opportunities", "contacts_found",
        "sent_to_airtable", "final_pass_leads", "verified_emails", "airtable_suppressed",
    )}
    assert values == {
        "jobs_captured": 6206, "jobs_reviewed": 5746, "qualified_opportunities": 1264,
        "contacts_found": 613, "sent_to_airtable": 400, "final_pass_leads": 425,
        "verified_emails": 606, "airtable_suppressed": 13,
    }
    row = report.runs[0]
    assert row["evidence_source"] == "ledger"
    assert row["metric_sources"]["jobs_captured"] == "reporting_ledger:metrics.jobs_captured"


# --------------------------------------------------------------------------
# interrupted runs
# --------------------------------------------------------------------------


def test_an_interrupted_run_stays_visible_with_the_counters_it_did_record(tmp_path):
    """The Sep 4 control run: 6,206 jobs acquired, killed before any final marker.

    Under the old reader this run did not exist. Under the ledger it exists, is
    labelled interrupted, and contributes exactly what it measured.
    """
    root = tmp_path / "orchestrator_v2"
    ledger = RunLedger(root, "20260904T064411Z-66ea967e")
    ledger.begin(started_at=_instant("2026-09-04T06:44:11Z"), mode="live_acquisition_and_enrichment",
                 allow_network=True, allow_enrichment=True, lanes=["fantastic"])
    ledger.record("acquisition", {"jobs_captured": 6206})
    ledger.record("enrichment", {"jobs_reviewed": 5746, "contacts_found": 613})
    # No finalize(): the process was killed.

    window = explicit_window(
        datetime(2026, 9, 3).date(), datetime(2026, 9, 5).date(),
        boundary_hour=0, tz_name="America/Los_Angeles",
    )
    report = build_report(window, artifact_roots=[root])

    assert len(report.runs) == 1, "a killed run is still a run"
    assert report.runs[0]["status"] == STATE_INTERRUPTED
    assert report.metrics["jobs_captured"].value == 6206
    assert report.metrics["jobs_reviewed"].value == 5746
    assert report.metrics["sent_to_airtable"].status == STATUS_UNAVAILABLE, (
        "delivery never ran; that is unavailable, never a zero"
    )
    assert report.metrics["sent_to_airtable"].value is None
    interrupted_gap = [g for g in report.gaps if g.metric == "interrupted_runs"]
    assert interrupted_gap, "the interruption is declared to the reader"


def test_an_unfinalized_run_directory_without_a_ledger_entry_is_declared(tmp_path):
    """Belt and braces: the pre-ledger failure mode must at least be reported."""
    root = tmp_path / "orchestrator_v2"
    orphan = root / "run_artifacts" / "20260904T064411Z-66ea967e" / "enrichment"
    orphan.mkdir(parents=True)
    (orphan / "postings.json").write_text('{"jobs": []}', encoding="utf-8")

    runs, problems = discover_runs([root])

    assert runs == []
    assert any("no run_status" in problem for problem in problems), (
        "returning silently is how a real run became invisible"
    )


# --------------------------------------------------------------------------
# silence is never zero
# --------------------------------------------------------------------------


def test_a_counter_only_some_runs_measured_is_partial_not_a_total(tmp_path):
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260901T130000Z-aaaaaaaa", started="2026-09-01T13:00:00Z",
                     finished="2026-09-01T13:05:00Z",
                     metrics={"jobs_captured": 100, "jobs_reviewed": 90})
    write_ledger_run(root, "20260902T130000Z-bbbbbbbb", started="2026-09-02T13:00:00Z",
                     finished="2026-09-02T13:05:00Z",
                     metrics={"jobs_captured": 500})

    report = build_report(_week_window(), artifact_roots=[root])

    captured = report.metrics["jobs_captured"]
    reviewed = report.metrics["jobs_reviewed"]
    assert captured.value == 600 and captured.status == STATUS_MEASURED
    assert reviewed.value == 90, "the total covers only the run that measured it"
    assert reviewed.status == STATUS_PARTIAL
    assert reviewed.runs_missing_field == ["20260902T130000Z-bbbbbbbb"]
    assert report.metrics["review_rate_pct"].status != STATUS_MEASURED, (
        "600 - 90 spans different run sets, so the rate is not a clean measurement"
    )


def test_a_dry_run_in_the_ledger_is_not_counted_as_throughput(tmp_path):
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260901T130000Z-aaaaaaaa", started="2026-09-01T13:00:00Z",
                     finished="2026-09-01T13:05:00Z", mode="full_dry_run",
                     metrics={"jobs_captured": 9999})

    report = build_report(_week_window(), artifact_roots=[root])

    assert report.runs == []
    assert report.excluded_simulated and report.excluded_simulated[0]["run_id"].endswith("aaaaaaaa")


# --------------------------------------------------------------------------
# the stakeholder message
# --------------------------------------------------------------------------


def test_the_stakeholder_message_uses_ledger_totals_in_bretts_format(tmp_path):
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(
        root, "20260901T130000Z-aaaaaaaa",
        started="2026-09-01T13:00:00Z", finished="2026-09-01T13:05:00Z",
        metrics={"jobs_captured": 6206, "jobs_reviewed": 5746,
                 "qualified_opportunities": 1264, "contacts_found": 613,
                 "sent_to_airtable": 400},
    )
    report = build_report(_week_window(), artifact_roots=[root])
    text = render_stakeholder_summary(report)

    assert "Jobs: 6,206 captured / 5,746 reviewed (92.6%)" in text
    assert "Qualified opportunities: 1,264" in text
    assert "Contacts found: 613" in text
    assert "sent to Instantly:" in text, "Brett's wording is lower-case here"
    assert "Biggest bottleneck from past week" in text
    assert "Action plan for the following week" in text
    # Internal evidence must not leak into the stakeholder view.
    assert "20260901T130000Z-aaaaaaaa" not in text
    assert "reporting_ledger:" not in text
    assert "NOT MEASURED" not in text


def test_the_stakeholder_message_never_prints_a_zero_it_did_not_measure(tmp_path):
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260901T130000Z-aaaaaaaa", started="2026-09-01T13:00:00Z",
                     finished="2026-09-01T13:05:00Z", metrics={"jobs_captured": 0},
                     stop_reason="topup:governor_zero_budget")

    text = render_stakeholder_summary(build_report(_week_window(), artifact_roots=[root]))

    assert "Jobs: 0 captured / reviewed not measured" in text
    assert "Qualified opportunities: not measured" in text


# --------------------------------------------------------------------------
# windows and delivery
# --------------------------------------------------------------------------


def test_consecutive_weekly_windows_meet_exactly_once(tmp_path):
    """No gap, no overlap: a run on the boundary belongs to the later window only."""
    first = weekly_window(datetime(2026, 9, 4, 3, tzinfo=UTC), boundary_weekday=4,
                          boundary_hour=0, weeks=1, tz_name="America/Los_Angeles")
    second = weekly_window(datetime(2026, 9, 11, 3, tzinfo=UTC), boundary_weekday=4,
                           boundary_hour=0, weeks=1, tz_name="America/Los_Angeles")

    assert first.end_utc == second.start_utc, "the windows meet at a point"
    boundary = first.end_utc
    assert not first.contains(boundary), "half-open: the boundary is not in the earlier week"
    assert second.contains(boundary), "and is in the later one"


def test_a_ledger_only_week_delivers_to_slack_exactly_once(tmp_path, monkeypatch):
    """Idempotent delivery still holds when the week is reconstructed from the ledger."""
    from weekly_report import slack

    root = tmp_path / "orchestrator_v2"
    out = tmp_path / "weekly_reports"
    _seven_run_week(root, captured=0)

    calls: List[Dict[str, Any]] = []

    class _Response:
        status_code = 200
        text = "ok"

    def poster(url, payload, timeout):
        calls.append({"url": url, "payload": payload})
        return _Response()

    monkeypatch.setenv(slack.ENV_WEBHOOK,
                       "https://hooks.slack.com/services/T0/B1/ZZZZsecretZZZZ")
    monkeypatch.setattr(slack, "_default_poster", poster)
    monkeypatch.setattr(slack, "DEFAULT_BACKOFF_SECONDS", 0)

    argv = [
        "--artifact-root", str(root),
        "--start", "2026-08-28", "--end", "2026-09-04",
        "--out-dir", str(out), "--slack", "--once-per-window", "--quiet",
    ]
    assert run_weekly_report.main(argv) == 0
    assert run_weekly_report.main(argv) == 0
    assert run_weekly_report.main(argv) == 0

    assert len(calls) == 1, "the receipt prevents a duplicate weekly message"
    delivered = calls[0]["payload"]["text"]
    assert delivered == (out / "weekly_report_2026-W36.slack.txt").read_text(encoding="utf-8")
    assert "Jobs: 0 captured" in delivered
    # The full evidence record is still written, just not delivered.
    assert (out / "weekly_report_2026-W36.txt").exists()
    assert "Pipeline runs    : 7" in (out / "weekly_report_2026-W36.txt").read_text(encoding="utf-8")


def test_the_stronger_clock_wins_when_both_stores_describe_one_run(tmp_path):
    """A manifest with no timestamps must not outrank a ledger completion instant.

    At a window edge this is the difference between a run landing in the right
    week and the wrong one.
    """
    root = tmp_path / "orchestrator_v2"
    run_id = "20260828T130100Z-00000001"
    write_ledger_run(root, run_id, started="2026-08-28T13:01:00Z",
                     finished="2026-08-28T13:05:00Z", metrics={"jobs_captured": 42})
    # Heavy artifacts that carry NO started_at/finished_at at all.
    write_run(root, run_id, finished=None, started=None, postings=42,
              reviewed=None, qualified=None, contacts=None, created=None)

    runs, _ = discover_runs([root])
    record = next(r for r in runs if r.run_id == run_id)

    assert record.attribution_field == "run_manifest.finished_at"
    assert record.finished_at is not None
    inside, _ = select_window(runs, _week_window())
    assert [r.run_id for r in inside] == [run_id]


def test_sibling_stores_are_not_mistaken_for_unfinalized_runs(tmp_path):
    """A ledger-only root has no run_artifacts/ yet, so the ROOT gets scanned.

    Without an exclusion list every sibling store there -- reporting_ledger and
    weekly_reports included -- would be reported as an unfinalized run.
    """
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260901T130000Z-aaaaaaaa", started="2026-09-01T13:00:00Z",
                     finished="2026-09-01T13:05:00Z", metrics={"jobs_captured": 5})
    (root / "weekly_reports").mkdir(parents=True, exist_ok=True)
    (root / "weekly_reports" / "weekly_report_2026-W36.json").write_text("{}", encoding="utf-8")
    (root / "checkpoints").mkdir(parents=True, exist_ok=True)
    (root / "checkpoints" / "x.json").write_text("{}", encoding="utf-8")
    assert not (root / "run_artifacts").exists()

    runs, problems = discover_runs([root])

    assert [r.run_id for r in runs] == ["20260901T130000Z-aaaaaaaa"]
    assert problems == [], f"sibling stores must not be reported as runs: {problems}"


# --------------------------------------------------------------------------
# backfill across the deploy boundary
# --------------------------------------------------------------------------


def test_runs_written_before_the_ledger_existed_are_backfilled(tmp_path):
    """The deploy boundary: today's productive run predates the ledger.

    Its heavy artifacts survive for a few days, so its counters are lifted into
    the durable store before retention deletes them. Without this the first
    productive run would vanish from the very report the ledger exists to fix.
    """
    from orchestrator.run_ledger import backfill_from_artifacts

    root = tmp_path / "orchestrator_v2"
    write_run(root, "20260904T130130Z-13b44a0c", finished="2026-09-04T16:00:00Z",
              started="2026-09-04T13:01:30Z", postings=6205, reviewed=None,
              qualified=None, contacts=1048, created=90)

    result = backfill_from_artifacts(root)

    assert result["written"] == ["20260904T130130Z-13b44a0c"]
    entries, problems = read_entries(root)
    assert problems == []
    entry = entries[0]
    assert entry["state"] == STATE_COMPLETE
    assert entry["backfilled_from_artifacts"] is True
    assert entry["started_at"] == "2026-09-04T13:01:30Z"
    assert entry["finished_at"] == "2026-09-04T16:00:00Z", "the run's own clock, not now()"
    assert entry["metrics"]["jobs_captured"] == 6205
    assert entry["metrics"]["contacts_found"] == 1048
    assert entry["metrics"]["sent_to_airtable"] == 90
    assert "jobs_reviewed" not in entry["metrics"], "an absent funnel stays absent"
    assert entry["metric_sources"]["jobs_captured"] == "pipeline:backfill_from_artifacts"


def test_backfill_is_idempotent_and_never_overwrites_a_live_entry(tmp_path):
    from orchestrator.run_ledger import backfill_from_artifacts

    root = tmp_path / "orchestrator_v2"
    run_id = "20260904T130130Z-13b44a0c"
    write_ledger_run(root, run_id, started="2026-09-04T13:01:30Z",
                     finished="2026-09-04T16:00:00Z", metrics={"jobs_captured": 6205})
    write_run(root, run_id, finished="2026-09-04T16:00:00Z", postings=999,
              reviewed=None, qualified=None, contacts=None, created=None)

    first = backfill_from_artifacts(root)
    second = backfill_from_artifacts(root)

    assert first["written"] == [] and second["written"] == []
    entry = read_entries(root)[0][0]
    assert entry["metrics"]["jobs_captured"] == 6205, "the live entry wins over the artifacts"
    assert "backfilled_from_artifacts" not in entry


def test_a_backfilled_week_reports_exactly_like_a_native_one(tmp_path):
    from orchestrator.run_ledger import backfill_from_artifacts

    root = tmp_path / "orchestrator_v2"
    for offset in range(3):
        day = datetime(2026, 9, 1, 13, 1, tzinfo=UTC) + timedelta(days=offset)
        write_run(root, day.strftime("%Y%m%dT%H%M%SZ") + f"-{offset:08x}",
                  finished=day.strftime("%Y-%m-%dT%H:%M:%SZ"), postings=100,
                  reviewed=None, qualified=None, contacts=12, created=10)
    backfill_from_artifacts(root)

    report = build_report(_week_window(), artifact_roots=[root])
    assert report.metrics["jobs_captured"].value == 300
    assert len(report.runs) == 3
    # Both stores describe these runs, so they are not "ledger only".
    assert report.to_dict()["provenance"]["runs_reported_from_ledger_only"] == []
