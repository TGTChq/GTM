"""Run-anchored weekly windows: Friday's own acquisition, counted exactly once.

A fixed wall-clock boundary cannot include the Friday run in the Friday report.
The report is generated at ``S + D``, where the run duration ``D`` varies (measured
2026-09-04: 3.3 h expected, 6.7 h at 2x), so a fixed boundary ``B`` would have to
satisfy ``S + D_max < B <= S + D_min``. These tests pin the replacement: the
boundary is the previous report's generation instant, persisted durably.

The invariants under test are the ones an operator cannot check by eye --
contiguity across cycles, a run landing exactly on a boundary, and every failure
path leaving the boundary somewhere recoverable.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import run_weekly_report
from weekly_report import anchor as anchor_mod
from weekly_report.timewindow import anchored_window

PACIFIC = ZoneInfo("America/Los_Angeles")
MERIDA = ZoneInfo("America/Merida")
REPORT_DIR = "weekly_reports"


def write_run(root: Path, run_id: str, finished: str, *, postings: int = 100,
              status: str = "complete", stop_reason: str = "") -> Path:
    d = root / "run_artifacts" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run_manifest.json").write_text(json.dumps({
        "run_id": run_id, "started_at": finished, "finished_at": finished,
        "status": status, "mode": "live_acquisition_and_enrichment",
        "stop_reason": stop_reason,
        "policy": {"allow_instantly_enrollment": False}}), encoding="utf-8")
    (d / "waterfall.json").write_text(json.dumps({"unit_totals": {
        "postings": postings, "opportunities": postings // 3,
        "contacts": postings // 8}}), encoding="utf-8")
    (d / "orchestrator_result.json").write_text(json.dumps({
        # Net-new is the ONLY field that may answer jobs_captured. The old
        # waterfall.unit_totals.postings fallback counted rows the lanes kept,
        # before cross-run dedupe, and was removed for exactly that reason.
        "acquisition": {"cumulative": {"net_new_jobs_captured": postings}},
        "enrichment": {"funnel": {"qualification_input": postings,
                                  "target_role_eligible": postings // 3}},
        "run": {"policy": {"allow_instantly_enrollment": False}}, "lanes": {}}),
        encoding="utf-8")
    (d / "delivery.json").write_text(json.dumps(
        {"created": postings // 10, "existing": 0, "failed": 0}), encoding="utf-8")
    return d


def report_at(root: Path, now_iso: str, *, extra=()) -> int:
    argv = ["--artifact-root", str(root), "--anchored", "--require-completed-run",
            "--now", now_iso, "--quiet", *extra]
    return run_weekly_report.main(argv)


def anchor_of(root: Path):
    return anchor_mod.read_anchor(anchor_mod.anchor_path_for(root / REPORT_DIR))


def documents(root: Path):
    out = []
    for path in sorted((root / REPORT_DIR).glob("weekly_report_*.json")):
        if path.name == anchor_mod.ANCHOR_FILENAME:
            continue
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return sorted(out, key=lambda d: d["reporting_window_start"])


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "run_artifacts").mkdir(parents=True, exist_ok=True)
    return tmp_path


# --------------------------------------------------------------------------
# the point of the change
# --------------------------------------------------------------------------
def test_friday_acquisition_is_inside_that_fridays_report(root):
    """The whole reason the boundary moved: a run that finished a few hours before
    the report must be IN it. Under the old Friday-00:00-Pacific boundary this run
    fell outside its own week."""
    write_run(root, "fri", "2026-09-04T06:30:00Z", postings=3164)
    assert report_at(root, "2026-09-04T07:00:00Z") == 0
    doc = documents(root)[0]
    assert doc["included_run_ids"] == ["fri"]
    assert doc["metrics"]["jobs_captured"]["value"] == 3164


def test_adjacent_windows_meet_exactly_with_no_gap_and_no_overlap(root):
    write_run(root, "w1_fri", "2026-09-04T06:30:00Z")
    report_at(root, "2026-09-04T07:00:00Z")
    write_run(root, "w2_mon", "2026-09-07T06:00:00Z")
    write_run(root, "w2_fri", "2026-09-11T06:30:00Z")
    report_at(root, "2026-09-11T07:00:00Z")

    first, second = documents(root)
    assert first["reporting_window_end"] == second["reporting_window_start"]
    assert set(first["included_run_ids"]).isdisjoint(second["included_run_ids"])
    assert first["included_run_ids"] == ["w1_fri"]
    assert sorted(second["included_run_ids"]) == ["w2_fri", "w2_mon"]


def test_a_run_finishing_exactly_on_the_boundary_is_counted_once(root):
    write_run(root, "before", "2026-09-04T06:30:00Z")
    report_at(root, "2026-09-04T07:00:00Z")
    write_run(root, "exactly_on_boundary", "2026-09-04T07:00:00Z")
    write_run(root, "later", "2026-09-11T06:30:00Z")
    report_at(root, "2026-09-11T07:00:00Z")

    first, second = documents(root)
    # Half-open [start, end): the boundary instant belongs to the LATER window.
    assert "exactly_on_boundary" not in first["included_run_ids"]
    assert "exactly_on_boundary" in second["included_run_ids"]
    appearances = sum(d["included_run_ids"].count("exactly_on_boundary")
                      for d in documents(root))
    assert appearances == 1


def test_no_run_is_ever_dropped_between_consecutive_cycles(root):
    """Contiguity is only useful if it actually conserves runs."""
    written = []
    for day, hour in ((4, 6), (5, 6), (7, 6), (9, 6), (11, 6)):
        rid = f"r{day}"
        write_run(root, rid, f"2026-09-{day:02d}T{hour:02d}:30:00Z")
        written.append(rid)
    report_at(root, "2026-09-04T07:00:00Z")
    report_at(root, "2026-09-11T07:00:00Z")
    covered = [rid for doc in documents(root) for rid in doc["included_run_ids"]]
    assert sorted(covered) == sorted(written)
    assert len(covered) == len(set(covered))       # each exactly once


# --------------------------------------------------------------------------
# boundary persistence + idempotency
# --------------------------------------------------------------------------
def test_the_boundary_advances_to_the_generation_instant(root):
    write_run(root, "fri", "2026-09-04T06:30:00Z")
    report_at(root, "2026-09-04T07:00:00Z")
    assert anchor_of(root) == datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)


def test_a_second_invocation_at_the_same_instant_is_a_clean_no_op(root):
    write_run(root, "fri", "2026-09-04T06:30:00Z")
    report_at(root, "2026-09-04T07:00:00Z")
    before = anchor_of(root)
    assert report_at(root, "2026-09-04T07:00:00Z") == 0     # restart / double cron
    assert anchor_of(root) == before
    assert len(documents(root)) == 1


def test_a_clock_that_stepped_backwards_does_not_rewind_the_boundary(root):
    write_run(root, "fri", "2026-09-04T06:30:00Z")
    report_at(root, "2026-09-04T07:00:00Z")
    assert report_at(root, "2026-09-04T05:00:00Z") == 0
    assert anchor_of(root) == datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)


def test_once_per_window_regenerates_nothing_and_does_not_double_advance(root):
    write_run(root, "fri", "2026-09-04T06:30:00Z")
    report_at(root, "2026-09-04T07:00:00Z")
    first_anchor = anchor_of(root)
    # A later retry inside the same ISO week resolves to the same report path.
    assert report_at(root, "2026-09-04T09:00:00Z", extra=["--once-per-window"]) == 0
    assert anchor_of(root) == first_anchor
    assert len(documents(root)) == 1


def test_a_corrupt_boundary_file_falls_back_instead_of_failing_the_report(root):
    write_run(root, "fri", "2026-09-04T06:30:00Z")
    path = anchor_mod.anchor_path_for(root / REPORT_DIR)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert anchor_mod.read_anchor(path) is None
    assert report_at(root, "2026-09-04T07:00:00Z") == 0
    assert documents(root)[0]["included_run_ids"] == ["fri"]


def test_a_wrong_schema_boundary_file_is_ignored(root):
    path = anchor_mod.anchor_path_for(root / REPORT_DIR)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "something-else/9",
                                "report_end": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    assert anchor_mod.read_anchor(path) is None


# --------------------------------------------------------------------------
# failure semantics
# --------------------------------------------------------------------------
def test_a_friday_whose_acquisition_did_not_complete_reports_nothing(root):
    write_run(root, "crashed", "2026-09-04T06:30:00Z", status="incomplete",
              stop_reason="acquisition_failed")
    assert report_at(root, "2026-09-04T07:00:00Z") == 0
    assert documents(root) == []
    # The boundary must NOT advance to the generation instant -- that would close a
    # window no report covered. It is held at the window START so the deferred span
    # stays inside the next report.
    held = anchor_of(root)
    assert held is not None and held < datetime(2026, 9, 4, 6, 30, tzinfo=timezone.utc)


def test_runs_from_a_skipped_week_are_picked_up_by_the_next_report(root):
    """The fail-safe defers, it does not discard."""
    write_run(root, "crashed", "2026-09-04T06:30:00Z", status="incomplete")
    report_at(root, "2026-09-04T07:00:00Z")
    write_run(root, "recovered", "2026-09-11T06:30:00Z")
    report_at(root, "2026-09-11T07:00:00Z")
    doc = documents(root)[0]
    assert "recovered" in doc["included_run_ids"]
    # The skipped span must be COVERED by the next window, not jumped over: its
    # start is still before the crashed run, so nothing fell between the reports.
    assert doc["reporting_window_start"] <= "2026-09-04T06:30:00Z"
    assert "crashed" in doc["included_run_ids"]


def test_report_generation_failure_leaves_the_boundary_untouched(root, monkeypatch):
    write_run(root, "fri", "2026-09-04T06:30:00Z")
    monkeypatch.setattr(run_weekly_report, "render_summary",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        report_at(root, "2026-09-04T07:00:00Z")
    assert anchor_of(root) is None
    assert documents(root) == []


def test_no_write_never_advances_the_boundary(root):
    write_run(root, "fri", "2026-09-04T06:30:00Z")
    assert report_at(root, "2026-09-04T07:00:00Z", extra=["--no-write"]) == 0
    assert anchor_of(root) is None


# --------------------------------------------------------------------------
# calendar / timezone
# --------------------------------------------------------------------------
def test_thursday_evening_pacific_start_belongs_to_the_friday_cycle(root):
    """The 03:00 UTC cron starts Thursday 20:00 Pacific; the run it produces must
    land in the Friday report generated a few hours later."""
    start_utc = datetime(2026, 9, 4, 3, 0, tzinfo=timezone.utc)
    assert start_utc.astimezone(PACIFIC).strftime("%a %H:%M") == "Thu 20:00"
    assert start_utc.astimezone(MERIDA).strftime("%a %H:%M") == "Thu 21:00"
    write_run(root, "thu_evening_start", "2026-09-04T06:30:00Z")
    report_at(root, "2026-09-04T07:00:00Z")
    doc = documents(root)[0]
    assert doc["included_run_ids"] == ["thu_evening_start"]
    assert doc["reporting_window_end_local"].startswith("2026-09-04")


def test_merida_is_represented_independently_of_pacific(root):
    """Mérida does not observe US DST, so the two offsets must be resolved
    separately rather than derived from one another."""
    summer = datetime(2026, 9, 4, 3, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 12, 4, 3, 0, tzinfo=timezone.utc)
    assert summer.astimezone(PACIFIC).hour == 20      # PDT, UTC-7
    assert winter.astimezone(PACIFIC).hour == 19      # PST, UTC-8
    assert summer.astimezone(MERIDA).hour == 21       # CST, UTC-6 all year
    assert winter.astimezone(MERIDA).hour == 21


def test_windows_stay_contiguous_across_the_pacific_dst_transition(root):
    """DST ends 2026-11-01 in the US. An anchored window is bounded by instants, so
    the transition must not create an hour of gap or of double counting."""
    before = datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc)
    after = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    w = anchored_window(before, after)
    assert w.start_utc == before and w.end_utc == after
    assert w.start_local.utcoffset() == timedelta(hours=-7)   # PDT
    assert w.end_local.utcoffset() == timedelta(hours=-8)     # PST
    nxt = anchored_window(after, after + timedelta(days=7))
    assert w.end_utc == nxt.start_utc


def test_an_inverted_window_is_refused_rather_than_silently_empty(root):
    moment = datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        anchored_window(moment, moment - timedelta(hours=1))
    with pytest.raises(ValueError):
        anchored_window(moment, moment)


# --------------------------------------------------------------------------
# the --if-due gate must not drift across local midnight
# --------------------------------------------------------------------------
@pytest.mark.parametrize("hours", [3.3, 5.0, 6.7])
def test_due_gate_in_utc_is_stable_across_every_measured_run_length(root, hours):
    """With the cron at 03:00 UTC the Pacific weekday at report time flips between
    Thursday and Friday depending on how long acquisition took. UTC does not."""
    cron = datetime(2026, 9, 11, 3, 0, tzinfo=timezone.utc)
    at = cron + timedelta(hours=hours)
    write_run(root, "fri", (cron + timedelta(hours=hours - 0.5))
              .strftime("%Y-%m-%dT%H:%M:%SZ"))
    rc = run_weekly_report.main([
        "--artifact-root", str(root), "--anchored", "--require-completed-run",
        "--if-due", "friday", "--if-due-timezone", "UTC",
        "--now", at.strftime("%Y-%m-%dT%H:%M:%SZ"), "--quiet"])
    assert rc == 0
    assert documents(root), f"report must fire at +{hours}h"
    assert documents(root)[0]["included_run_ids"] == ["fri"]


def test_due_gate_defaults_to_utc(root):
    """The default is UTC, the zone Railway cron schedules in."""
    write_run(root, "fri", "2026-09-04T06:30:00Z")
    # 2026-09-04T07:00Z is Friday in UTC (and, coincidentally, Friday 00:00 PDT).
    rc = run_weekly_report.main([
        "--artifact-root", str(root), "--anchored", "--require-completed-run",
        "--if-due", "friday", "--now", "2026-09-04T07:00:00Z", "--quiet"])
    assert rc == 0 and documents(root)


def test_the_0300_utc_cron_fires_the_friday_gate(root, capsys):
    """The exact production shape, and the exact way it broke.

    GTM's cron moved from ``0 13 * * *`` to ``0 3 * * *`` on 2026-09-04. 13:00 UTC
    Friday is 06:00 Friday Pacific, so a Pacific-evaluated gate matched. 03:00 UTC
    Friday is 20:00 THURSDAY Pacific, so it stopped matching -- and the only
    symptom was an ordinary "not due today" line, printed every day, on a job
    whose exit code was 0. The report simply never ran again.
    """
    write_run(root, "fri", "2026-09-11T02:30:00Z")
    rc = run_weekly_report.main([
        "--artifact-root", str(root), "--anchored", "--require-completed-run",
        "--if-due", "friday", "--now", "2026-09-11T03:00:00Z"])
    assert rc == 0
    assert documents(root), "the 03:00 UTC Friday cron must produce a report"

    # ...and the skip message on a genuinely wrong day names the zone it used, so
    # a future mismatch is legible in the log instead of looking routine.
    rc = run_weekly_report.main([
        "--artifact-root", str(root), "--anchored", "--require-completed-run",
        "--if-due", "friday", "--now", "2026-09-14T03:00:00Z"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Monday" in out and "UTC" in out


def test_the_pacific_gate_is_still_available_explicitly(root):
    """Overriding is not removed -- only the silent default changed."""
    write_run(root, "fri", "2026-09-11T02:30:00Z")
    rc = run_weekly_report.main([
        "--artifact-root", str(root), "--anchored", "--require-completed-run",
        "--if-due", "friday", "--if-due-timezone", "America/Los_Angeles",
        "--now", "2026-09-11T03:00:00Z", "--quiet"])
    assert rc == 0
    assert documents(root) == [], "03:00 UTC Friday is Thursday in Pacific"


def test_due_gate_skips_non_matching_days_without_touching_the_boundary(root):
    write_run(root, "thu", "2026-09-10T06:30:00Z")
    rc = run_weekly_report.main([
        "--artifact-root", str(root), "--anchored", "--require-completed-run",
        "--if-due", "friday", "--if-due-timezone", "UTC",
        "--now", "2026-09-10T07:00:00Z", "--quiet"])
    assert rc == 0
    assert documents(root) == []
    assert anchor_of(root) is None
