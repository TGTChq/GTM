"""The GTM Start Command, tested as a string rather than as an intention.

The command is service-managed: it lives in Railway, not in this repository, and
nothing here could catch a mistake in it. That gap has already cost a working
report once -- moving the cron from ``0 13 * * *`` to ``0 3 * * *`` silently
stopped the weekly report firing, because ``--if-due friday`` was being evaluated
in Pacific and 03:00 UTC Friday is Thursday 20:00 there. The job printed "not due
today" and exited 0, every day.

So the literal command string lives here, its report half is parsed with the real
argument parser, and ``run_weekly_report.main`` is driven at the instants the real
cron actually produces -- including the long-acquisition case, which is the whole
reason the due gate and the anchored window have to agree.

Nothing in this file performs a network call, writes to a production path, or
sends anything: ``--slack`` is stripped from the argv under test and the isolated
tmp root is asserted to hold no receipt.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import run_weekly_report
from weekly_report import anchor as anchor_mod

#: The EXACT string configured on the Railway GTM service. Keep byte-identical.
GTM_START_COMMAND = (
    "sh -c 'python -u run_orchestrator.py --mode live_acquisition_and_enrichment "
    "--lanes fantastic --target 300 --airtable-write --global-budget 1500 "
    "--artifact-root /app/data/state/orchestrator_v2; rc=$?; "
    "python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2 "
    "--if-due friday --once-per-window --anchored --require-completed-run "
    "--instantly --slack --max-seconds 480 || true; exit $rc'"
)

#: The Railway cron for the GTM service, in UTC (Railway cron is UTC only).
GTM_CRON_UTC_HOUR = 3

#: Measured acquisition durations. 3.3 h was the 2026-09-04 run; 6.7 h is the 2x
#: case the top-up loop can reach. The report fires at cron + one of these, and
#: the due gate must give the same answer across the whole range.
ACQUISITION_HOURS = (0.05, 3.3, 6.7)

REPORT_DIR = "weekly_reports"


def _inner_script() -> str:
    """The shell script inside ``sh -c '...'``."""
    parts = shlex.split(GTM_START_COMMAND)
    assert parts[:2] == ["sh", "-c"], GTM_START_COMMAND
    return parts[2]


#: Shell operators that END an argv rather than belonging to it. ``|| true`` is
#: part of the command, not part of the report's arguments, and handing it to
#: argparse is precisely the class of mistake this file exists to catch.
_SHELL_OPERATORS = ("||", "&&", "|", ">", ">>", "2>", "2>&1", "&", ";")


def _argv_after(script_segment: str, entrypoint: str):
    tokens = shlex.split(script_segment.strip())
    if entrypoint not in tokens:
        return None
    argv = tokens[tokens.index(entrypoint) + 1:]
    for index, token in enumerate(argv):
        if token in _SHELL_OPERATORS:
            return argv[:index]
    return argv


def report_argv() -> list:
    """The weekly-report argv the deployed command actually passes."""
    for segment in _inner_script().split(";"):
        argv = _argv_after(segment, "run_weekly_report.py")
        if argv is not None:
            return argv
    raise AssertionError("the command does not invoke run_weekly_report.py")


def acquisition_argv() -> list:
    for segment in _inner_script().split(";"):
        argv = _argv_after(segment, "run_orchestrator.py")
        if argv is not None:
            return argv
    raise AssertionError("the command does not invoke run_orchestrator.py")


def _offline_argv(root: Path, now: datetime) -> list:
    """The deployed argv, minus the two flags that would reach a provider."""
    argv = [a for a in report_argv() if a not in ("--instantly", "--slack")]
    argv = [str(root) if a == "/app/data/state/orchestrator_v2" else a for a in argv]
    return argv + ["--now", now.strftime("%Y-%m-%dT%H:%M:%SZ"), "--quiet"]


def write_run(root: Path, run_id: str, finished: datetime, *, status: str = "complete",
              postings: int = 412) -> None:
    d = root / "run_artifacts" / run_id
    d.mkdir(parents=True, exist_ok=True)
    stamp = finished.strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / "run_manifest.json").write_text(json.dumps({
        "run_id": run_id, "started_at": stamp, "finished_at": stamp,
        "status": status, "mode": "live_acquisition_and_enrichment",
        "policy": {"allow_instantly_enrollment": False}}), encoding="utf-8")
    (d / "waterfall.json").write_text(json.dumps(
        {"unit_totals": {"postings": postings, "contacts": postings // 4}}),
        encoding="utf-8")
    (d / "orchestrator_result.json").write_text(json.dumps({
        "acquisition": {"cumulative": {"net_new_jobs_captured": postings}},
        "enrichment": {"funnel": {"qualification_input": postings,
                                  "contact_discovery_entered": postings // 3}},
        "run": {"policy": {"allow_instantly_enrollment": False}}, "lanes": {}}),
        encoding="utf-8")
    (d / "delivery.json").write_text(json.dumps({"created": postings // 10}),
                                     encoding="utf-8")


def cron_fire(day: int) -> datetime:
    """The 03:00 UTC firing on 2026-09-<day>."""
    return datetime(2026, 9, day, GTM_CRON_UTC_HOUR, 0, tzinfo=timezone.utc)


def documents(root: Path) -> list:
    """Report documents only. The anchor is ``weekly_report_anchor.json``, which
    the obvious glob also matches -- and counting it as a report would make a
    held boundary look like a written report, which is the opposite of the fact."""
    return sorted(p for p in (root / REPORT_DIR).glob("weekly_report_*.json")
                  if p.name != anchor_mod.ANCHOR_FILENAME)


def anchor_of(root: Path):
    return anchor_mod.read_anchor(anchor_mod.anchor_path_for(root / REPORT_DIR))


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "run_artifacts").mkdir(parents=True, exist_ok=True)
    return tmp_path


# --------------------------------------------------------------------------
# the string itself
# --------------------------------------------------------------------------

def test_the_command_runs_acquisition_before_the_report():
    """``--anchored`` ends the window at the generation instant, so a report that
    runs FIRST can never contain the run in its own container."""
    script = _inner_script()
    assert script.index("run_orchestrator.py") < script.index("run_weekly_report.py")


def test_the_acquisition_arguments_are_unchanged():
    """The closeout may reorder the two halves; it may not retune acquisition."""
    assert acquisition_argv() == [
        "--mode", "live_acquisition_and_enrichment",
        "--lanes", "fantastic",
        "--target", "300",
        "--airtable-write",
        "--global-budget", "1500",
        "--artifact-root", "/app/data/state/orchestrator_v2",
    ]


def test_the_report_half_passes_every_required_flag():
    argv = report_argv()
    for flag in ("--if-due", "--once-per-window", "--anchored",
                 "--require-completed-run", "--instantly", "--slack"):
        assert flag in argv, f"{flag} is missing from the deployed command"
    assert argv[argv.index("--if-due") + 1] == "friday"
    assert argv[argv.index("--artifact-root") + 1] == "/app/data/state/orchestrator_v2"
    # Parsed by the real parser, so a typo is a failure rather than a silent
    # unknown-argument exit at 03:00.
    args = run_weekly_report.build_parser().parse_args(argv)
    assert args.anchored and args.require_completed_run and args.once_per_window
    assert args.slack and args.instantly and args.if_due == "friday"


def test_the_report_cannot_change_the_containers_exit_status():
    """``exec`` had to go, because something now runs after acquisition. The
    container's exit code must still be the PIPELINE's, not the report's."""
    script = _inner_script()
    assert "rc=$?" in script and script.rstrip().endswith("exit $rc")
    assert "exec " not in script, "exec would prevent the report from running at all"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("pipeline_rc", [0, 7, 42])
@pytest.mark.parametrize("report_rc", [0, 1, 137])
def test_the_wrapper_propagates_the_pipeline_exit_code(tmp_path, pipeline_rc, report_rc):
    """Every combination, because this is the one property `exec` gave for free."""
    for name, code in (("pipe.sh", pipeline_rc), ("report.sh", report_rc)):
        path = tmp_path / name
        path.write_text(f"#!/bin/sh\nexit {code}\n")
        # Git Bash on Windows ignores the mode bit; Linux does not, and without
        # this every case returns 126 (permission denied) rather than the code
        # under test -- which is a green-looking pass locally and a red CI.
        path.chmod(0o755)
    script = "./pipe.sh; rc=$?; ./report.sh || true; exit $rc"
    result = subprocess.run(["sh", "-c", script], cwd=tmp_path)
    assert result.returncode == pipeline_rc


# --------------------------------------------------------------------------
# the due gate at the ACTUAL schedule
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hours", ACQUISITION_HOURS)
def test_the_report_fires_on_friday_however_long_acquisition_took(root, hours):
    """2026-09-11 is a Friday. The report runs at 03:00 UTC + the run duration,
    and the gate must not flip inside that range."""
    fire = cron_fire(11)
    at = fire + timedelta(hours=hours)
    write_run(root, "fri", at - timedelta(minutes=5))
    assert run_weekly_report.main(_offline_argv(root, at)) == 0
    assert documents(root), f"the report must fire at cron+{hours}h"


@pytest.mark.parametrize("day", [5, 6, 7, 8, 9, 10])
def test_the_report_stays_silent_on_every_other_day(root, day):
    at = cron_fire(day)
    write_run(root, f"r{day}", at - timedelta(minutes=5))
    assert run_weekly_report.main(_offline_argv(root, at)) == 0
    assert documents(root) == []
    assert anchor_of(root) is None, "a skipped day must not move the boundary"


def test_a_pacific_due_gate_would_have_missed_every_firing(root):
    """The regression this file exists for, stated as a fact rather than a note."""
    argv = _offline_argv(root, cron_fire(11)) + \
        ["--if-due-timezone", "America/Los_Angeles"]
    write_run(root, "fri", cron_fire(11) - timedelta(minutes=5))
    assert run_weekly_report.main(argv) == 0
    assert documents(root) == [], "03:00 UTC Friday is Thursday 20:00 in Pacific"


# --------------------------------------------------------------------------
# anchor + once-per-window + require-completed-run, together
# --------------------------------------------------------------------------

def test_fridays_own_acquisition_is_inside_fridays_report(root):
    fire = cron_fire(11)
    finished = fire + timedelta(hours=3.3)
    write_run(root, "fri_own_run", finished, postings=2411)
    run_weekly_report.main(_offline_argv(root, finished + timedelta(minutes=2)))
    doc = json.loads(documents(root)[0].read_text(encoding="utf-8"))
    assert doc["included_run_ids"] == ["fri_own_run"]
    assert doc["metrics"]["jobs_captured"]["value"] == 2411


def test_a_second_firing_in_the_same_window_writes_nothing_new(root):
    """A redeploy or a double cron must not re-scan Instantly or rewrite the
    report -- ``--once-per-window`` is checked BEFORE any collector runs."""
    fire = cron_fire(11)
    write_run(root, "fri", fire + timedelta(hours=1))
    run_weekly_report.main(_offline_argv(root, fire + timedelta(hours=3.3)))
    first_anchor = anchor_of(root)
    before = [(p.name, p.read_bytes()) for p in documents(root)]

    assert run_weekly_report.main(_offline_argv(root, fire + timedelta(hours=5))) == 0

    assert [(p.name, p.read_bytes()) for p in documents(root)] == before
    assert anchor_of(root) == first_anchor, "the boundary must not double-advance"


def test_a_failed_acquisition_writes_no_report_and_holds_the_boundary(root):
    """``--require-completed-run``: a Friday whose acquisition failed must not
    produce a report implying the week finished, and those runs must land in the
    NEXT report rather than being skipped over."""
    fire = cron_fire(11)
    write_run(root, "fri_failed", fire + timedelta(hours=1), status="failed")
    assert run_weekly_report.main(_offline_argv(root, fire + timedelta(hours=1.5))) == 0
    assert documents(root) == [], "no report is written for a failed week"
    held = anchor_of(root)
    assert held is not None, "the boundary is pinned so the span stays covered"

    # The following Friday succeeds: the deferred run is still inside the window.
    next_fire = cron_fire(18)
    write_run(root, "next_fri", next_fire + timedelta(hours=1))
    run_weekly_report.main(_offline_argv(root, next_fire + timedelta(hours=1.5)))
    doc = json.loads(documents(root)[0].read_text(encoding="utf-8"))
    assert "next_fri" in doc["included_run_ids"]
    assert anchor_of(root) > held


def test_consecutive_weeks_meet_exactly_with_no_gap_and_no_overlap(root):
    for day, run_id in ((11, "w1"), (18, "w2")):
        at = cron_fire(day) + timedelta(hours=3.3)
        write_run(root, run_id, at - timedelta(minutes=10))
        run_weekly_report.main(_offline_argv(root, at))
    first, second = (json.loads(p.read_text(encoding="utf-8"))
                     for p in documents(root))
    assert first["reporting_window_end"] == second["reporting_window_start"]
    assert set(first["included_run_ids"]).isdisjoint(second["included_run_ids"])


def test_the_offline_argv_under_test_sends_nothing(root):
    """Guard the guard: no receipt, and no Slack flag reached the parser."""
    at = cron_fire(11) + timedelta(hours=1)
    write_run(root, "fri", at - timedelta(minutes=5))
    argv = _offline_argv(root, at)
    assert "--slack" not in argv and "--instantly" not in argv
    run_weekly_report.main(argv)
    assert list(root.rglob("*.slack_sent.json")) == []


# --------------------------------------------------------------------------
# the FIRST anchored report, on a volume that already holds older reports
# --------------------------------------------------------------------------
#
# Anchoring was introduced after reports had been going out on a fixed
# Friday-to-Friday boundary, and 2026-W36 was delivered to Slack on 2026-09-04
# covering Aug 28 - Sep 4. Seeding the first anchored window from
# ``weekly_window`` would re-cover that span: at the real firing instants it
# produces a 13.9-day window labelled as a week, containing a report Brett has
# already read.

def _existing_report(root: Path, iso_week: str, start: str, end: str) -> None:
    d = root / REPORT_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"weekly_report_{iso_week}.json").write_text(json.dumps({
        "schema": "tgtc-weekly-report/1", "iso_week": iso_week,
        "reporting_window_start": start, "reporting_window_end": end,
        "included_run_ids": [], "metrics": {}}), encoding="utf-8")


def test_the_first_anchored_window_starts_where_the_last_report_ended(root):
    """The real shape: 2026-W36 already went out, ending 2026-09-04T07:00:00Z."""
    _existing_report(root, "2026-W36",
                     "2026-08-28T07:00:00Z", "2026-09-04T07:00:00Z")
    fire = cron_fire(11)
    at = fire + timedelta(hours=3.3)
    write_run(root, "fri", at - timedelta(minutes=10))

    assert run_weekly_report.main(_offline_argv(root, at)) == 0
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in documents(root)]
    # The pre-existing 2026-W36 stub is the fixture; the report just written is
    # the one carrying real content.
    fresh = [d for d in docs if d.get("included_run_ids")]
    assert len(fresh) == 1, [d.get("reporting_window_start") for d in docs]
    assert fresh[0]["reporting_window_start"] == "2026-09-04T07:00:00Z", (
        "the new window must begin where the delivered one ended -- no gap, no "
        "re-report"
    )
    # Exactly the un-reported span: the previous window's end to this generation
    # instant (03:00 UTC cron + a 3.3 h acquisition).
    assert fresh[0]["reporting_window_end"] == "2026-09-11T06:18:00Z"


def test_without_the_seed_the_first_window_would_have_been_two_weeks(root):
    """The defect, stated as a measurement rather than a claim."""
    from weekly_report.anchor import seed_from_last_report

    _existing_report(root, "2026-W36",
                     "2026-08-28T07:00:00Z", "2026-09-04T07:00:00Z")
    at = cron_fire(11) + timedelta(hours=3.3)
    args = run_weekly_report.build_parser().parse_args(_offline_argv(root, at))

    unseeded = run_weekly_report.build_window(args, at, None)
    seeded = run_weekly_report.build_window(
        args, at, seed_from_last_report(root / REPORT_DIR))

    unseeded_days = (unseeded.end_utc - unseeded.start_utc).total_seconds() / 86400
    seeded_days = (seeded.end_utc - seeded.start_utc).total_seconds() / 86400
    assert 13.9 < unseeded_days < 14.0, f"{unseeded_days:.2f} days labelled as a week"
    assert 6.9 < seeded_days < 7.0, f"{seeded_days:.2f} days"
    assert unseeded.start_utc.isoformat().startswith("2026-08-28")
    assert seeded.start_utc.isoformat().startswith("2026-09-04")


def test_a_genuinely_first_ever_run_still_seeds_from_the_weekly_boundary(root):
    """No reports on disk: the weekly seed is correct and must not be lost."""
    from weekly_report.anchor import seed_from_last_report

    assert seed_from_last_report(root / REPORT_DIR) is None
    at = cron_fire(11) + timedelta(hours=3.3)
    write_run(root, "fri", at - timedelta(minutes=10))
    assert run_weekly_report.main(_offline_argv(root, at)) == 0
    assert documents(root), "a first-ever anchored report still writes"


def test_a_corrupt_report_on_disk_does_not_stop_the_seed(root):
    from weekly_report.anchor import seed_from_last_report

    d = root / REPORT_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "weekly_report_2026-W35.json").write_text("{ not json", encoding="utf-8")
    _existing_report(root, "2026-W36",
                     "2026-08-28T07:00:00Z", "2026-09-04T07:00:00Z")

    seed = seed_from_last_report(d)
    assert seed is not None and seed.isoformat().startswith("2026-09-04T07:00")


def test_the_latest_report_wins_when_several_are_present(root):
    from weekly_report.anchor import seed_from_last_report

    _existing_report(root, "2026-W34", "2026-08-14T07:00:00Z", "2026-08-21T07:00:00Z")
    _existing_report(root, "2026-W36", "2026-08-28T07:00:00Z", "2026-09-04T07:00:00Z")
    _existing_report(root, "2026-W35", "2026-08-21T07:00:00Z", "2026-08-28T07:00:00Z")

    seed = seed_from_last_report(root / REPORT_DIR)
    assert seed.isoformat().startswith("2026-09-04T07:00")
