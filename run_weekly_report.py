#!/usr/bin/env python
"""Generate the weekly pipeline report. Read-only, by construction.

    python -u run_weekly_report.py --artifact-root /app/data/state/orchestrator_v2

The default window is the most recently *closed* Friday-to-Friday week in
America/Los_Angeles, so a Friday-morning run reports the week that just ended and
never reaches into the day it is written.

This entry point cannot write to Airtable or Instantly, cannot enroll a lead,
cannot touch a campaign and sends nothing. ``--instantly`` and ``--airtable`` are
opt-in *listing* reads used to measure delivery; without them the affected metrics
are reported as unavailable with a reason rather than guessed.

Exit status is 0 whenever a report was produced, including a report full of gaps:
a reporting job must never take down the run that chains it. ``--strict`` opts
into a non-zero status when a headline metric could not be measured.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from weekly_report import anchor as anchor_mod
from weekly_report import slack
from weekly_report.external import CollectorResult, collect_airtable, collect_instantly, disabled
from weekly_report.render import render_summary
from weekly_report.report import HEADLINE_ORDER, build_report
from weekly_report.timewindow import (
    PACIFIC_TZ_NAME,
    anchored_window,
    ReportingWindow,
    explicit_window,
    resolve_timezone,
    weekly_window,
)

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

#: Where reports land when the caller does not say. Kept beside the run artifacts
#: so a history accumulates on the same volume the dashboard will later read.
_DEFAULT_SUBDIR = "weekly_reports"


def _default_artifact_roots() -> List[str]:
    """Roots to scan when the caller gives none: production config, then local data."""
    roots: List[str] = []
    try:
        import config  # noqa: PLC0415 - optional; the report must run without env

        configured = getattr(config, "ARTIFACT_ROOT", None)
        if configured:
            roots.append(str(configured))
    except Exception:  # noqa: BLE001 - a config import failure is not a reporting failure
        pass
    here = Path(__file__).resolve().parent
    for candidate in (here / "data" / "orchestrator", here / "data"):
        text = str(candidate)
        if candidate.is_dir() and text not in roots:
            roots.append(text)
    return roots


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - argparse surfaces the message
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from exc


def build_window(args: argparse.Namespace, now: datetime,
                 anchor_start: Optional[datetime] = None) -> ReportingWindow:
    """The window this invocation reports on.

    ``--anchored`` ends the window at THIS generation instant and starts it at the
    previously persisted one, so the run that just finished is inside it and the
    next window starts exactly where this one ends. Without a stored anchor (first
    ever anchored report) it falls back to the fixed weekly boundary, so the very
    first window is still a sane week rather than "all of history".
    """
    if getattr(args, "anchored", False) and not (args.start or args.end):
        if anchor_start is not None:
            return anchored_window(anchor_start, now, tz_name=args.timezone)
        seed = weekly_window(
            now, boundary_weekday=WEEKDAYS[args.boundary_day],
            boundary_hour=args.boundary_hour, weeks=args.weeks, tz_name=args.timezone)
        return anchored_window(seed.start_utc, now, tz_name=args.timezone)
    if args.start and args.end:
        return explicit_window(
            args.start, args.end, boundary_hour=args.boundary_hour, tz_name=args.timezone
        )
    if args.start or args.end:
        raise SystemExit("--start and --end must be given together")
    return weekly_window(
        now,
        boundary_weekday=WEEKDAYS[args.boundary_day],
        boundary_hour=args.boundary_hour,
        weeks=args.weeks,
        tz_name=args.timezone,
    )


def _is_due(args: argparse.Namespace, now: datetime) -> bool:
    """``--if-due`` gate: is today the local day this report is meant to run?"""
    if not args.if_due:
        return True
    tz, _ = resolve_timezone(args.timezone)
    return now.astimezone(tz).weekday() == WEEKDAYS[args.if_due]


def _collectors(
    args: argparse.Namespace, window: ReportingWindow
) -> tuple[Optional[CollectorResult], Optional[CollectorResult]]:
    instantly = airtable = None
    cfg = None
    if args.instantly or args.airtable:
        import config as cfg  # noqa: PLC0415

    deadline = None
    if getattr(args, "max_seconds", 0):
        import time  # noqa: PLC0415

        deadline = time.monotonic() + float(args.max_seconds)

    if args.instantly:
        try:
            instantly = collect_instantly(
                window, cfg=cfg, campaign_ids=args.campaign_id or None, deadline=deadline
            )
        except Exception as exc:  # noqa: BLE001 - a provider failure is a gap, not a crash
            instantly = disabled("instantly", f"collector raised: {str(exc)[:200]}")
            instantly.enabled = True
    if args.airtable:
        try:
            airtable = collect_airtable(window, cfg=cfg)
        except Exception as exc:  # noqa: BLE001
            airtable = disabled("airtable", f"collector raised: {str(exc)[:200]}")
            airtable.enabled = True
    return instantly, airtable


def _write(path: Path, text: str) -> Path:
    """Write atomically: a crash must never leave a half-written report readable.

    Reuses the project's temp-file + ``os.replace`` writer so a dashboard polling
    the output directory only ever sees a complete document.
    """
    from retrieval_measurement.artifacts import atomic_write_text  # noqa: PLC0415

    atomic_write_text(path, text)
    return path


def _output_paths(
    args: argparse.Namespace, window: ReportingWindow, roots: Sequence[str]
) -> tuple[Path, Path]:
    """Where this window's two files go. One deterministic pair per ISO week.

    Naming by ISO week is what makes a re-run idempotent: the same window always
    resolves to the same pair, so a second run overwrites rather than accumulating
    a second copy of the same report.
    """
    stem = f"weekly_report_{window.iso_week}"
    out_dir = Path(args.out_dir) if args.out_dir else Path(roots[0]) / _DEFAULT_SUBDIR
    json_path = Path(args.json_out) if args.json_out else out_dir / f"{stem}.json"
    summary_path = Path(args.summary_out) if args.summary_out else out_dir / f"{stem}.txt"
    return json_path, summary_path


def _deliver_existing(
    window: ReportingWindow, json_path: Path, summary_path: Path
) -> "slack.SlackDelivery":
    """Retry Slack for a report already on disk, recomputing nothing.

    Reached when a previous invocation wrote the report but did not record a
    delivery receipt. The summary text is read back rather than re-rendered, so no
    metric is recalculated and no provider is contacted.
    """
    if not summary_path.exists():
        return slack.SlackDelivery(
            status=slack.STATUS_FAILED,
            attempted=False,
            error=(
                f"the report JSON exists but {summary_path.name} does not, so there is no "
                "summary to deliver; re-run with --force to regenerate both"
            ),
        )
    try:
        summary = summary_path.read_text(encoding="utf-8")
    except OSError as exc:
        return slack.SlackDelivery(
            status=slack.STATUS_FAILED, attempted=False, error=slack.redact(exc)
        )
    return slack.deliver(
        summary,
        report_id=window.iso_week,
        report_path=json_path,
        window=window.to_dict(),
    )


def _report_slack(delivery: "slack.SlackDelivery", args: argparse.Namespace) -> None:
    """Print the (already redacted) delivery outcome. Never raises, never exits."""
    if args.quiet and delivery.delivered:
        return
    print(delivery.describe())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the weekly TGTC pipeline report (read-only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--artifact-root",
        action="append",
        default=[],
        metavar="PATH",
        help="Directory holding run_artifacts/ (repeatable). Defaults to the configured "
        "PIPELINE_ARTIFACT_ROOT plus local data directories.",
    )
    parser.add_argument("--start", type=_parse_date, help="Explicit window start, local date (YYYY-MM-DD).")
    parser.add_argument("--end", type=_parse_date, help="Explicit window end, local date, exclusive.")
    parser.add_argument("--weeks", type=int, default=1, help="How many weeks the default window spans.")
    parser.add_argument(
        "--boundary-day",
        choices=sorted(WEEKDAYS),
        default="friday",
        help="Local weekday the reporting week starts and ends on.",
    )
    parser.add_argument("--boundary-hour", type=int, default=0, help="Local hour of the boundary.")
    parser.add_argument("--timezone", default=PACIFIC_TZ_NAME, help="IANA zone the window is defined in.")
    parser.add_argument(
        "--if-due",
        choices=sorted(WEEKDAYS),
        help="Produce a report only when today is this local weekday; otherwise exit 0 "
        "silently. Lets the report be chained onto a daily cron.",
    )
    parser.add_argument(
        "--instantly",
        action="store_true",
        help="Read Instantly (POST /leads/list, listing only) to measure leads delivered "
        "in the window. Without it, sent_to_instantly is reported as unavailable.",
    )
    parser.add_argument(
        "--campaign-id",
        action="append",
        default=[],
        help="Restrict the Instantly read to these campaign ids (repeatable).",
    )
    parser.add_argument(
        "--airtable",
        action="store_true",
        help="Read Airtable (GET records, listing only) to cross-check rows created in "
        "the window by createdTime.",
    )
    parser.add_argument("--out-dir", help="Where to write the report files.")
    parser.add_argument("--json-out", help="Explicit path for the JSON document.")
    parser.add_argument("--summary-out", help="Explicit path for the human-readable summary.")
    parser.add_argument(
        "--include-simulated",
        action="store_true",
        help="Count dry-run/preflight runs too. Off by default: their counters are "
        "manufactured and would report fabricated throughput as a business result.",
    )
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=0,
        help="Wall-clock budget for provider reads (0 = unlimited). On expiry the "
        "Instantly count is reported as a declared floor instead of running long.",
    )
    parser.add_argument(
        "--once-per-window",
        action="store_true",
        help="Skip entirely (exit 0, no provider reads, no writes) when this window's "
        "report already exists. Makes a restart or a second cron firing a no-op.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even when --once-per-window would skip.",
    )
    parser.add_argument(
        "--anchored",
        action="store_true",
        help="End this window at the generation instant and start it at the previously "
        "persisted one (weekly_report_anchor.json beside the reports). Makes the run "
        "that just finished part of THIS report, with no gap or overlap against the "
        "next. Requires acquisition to run BEFORE the report.",
    )
    parser.add_argument(
        "--require-completed-run",
        action="store_true",
        help="Write and send nothing unless at least one COMPLETED pipeline run is "
        "attributed to the window. Stops a failed Friday acquisition from producing a "
        "report that implies the week finished; the anchor does not advance, so those "
        "runs are picked up by the next report instead of being lost.",
    )
    parser.add_argument(
        "--slack",
        action="store_true",
        help=f"POST the human summary to the Slack incoming webhook in ${slack.ENV_WEBHOOK}. "
        "Sends at most once per window, recorded by a receipt file written only after "
        "Slack accepts. A delivery failure never changes the exit status.",
    )
    parser.add_argument("--no-write", action="store_true", help="Print only; write no files.")
    parser.add_argument("--quiet", action="store_true", help="Do not print the summary to stdout.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when a headline metric could not be measured.",
    )
    parser.add_argument(
        "--now",
        help="Override 'now' as an ISO-8601 instant (testing and backfill).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.now:
        from weekly_report.timewindow import parse_instant  # noqa: PLC0415

        now = parse_instant(args.now)
        if now is None:
            raise SystemExit(f"--now is not an ISO-8601 instant: {args.now!r}")
    else:
        now = datetime.now(timezone.utc)

    if not _is_due(args, now):
        if not args.quiet:
            print(f"weekly report not due today (waiting for {args.if_due}); nothing written")
        return 0

    roots = args.artifact_root or _default_artifact_roots()
    anchor_start = None
    anchor_file = None
    if args.anchored:
        probe_dir = (Path(args.out_dir) if args.out_dir
                     else Path(roots[0]) / _DEFAULT_SUBDIR)
        anchor_file = anchor_mod.anchor_path_for(probe_dir)
        anchor_start = anchor_mod.read_anchor(anchor_file)
        # A second invocation at (or before) the stored boundary has nothing new to
        # cover. Exit cleanly rather than constructing an empty-or-inverted window:
        # this is the normal shape of a restart, a double cron firing, or a clock
        # that stepped backwards, and none of those is an error.
        if anchor_start is not None and now <= anchor_start:
            if not args.quiet:
                print(
                    f"weekly report: nothing new since the last anchored window ended at "
                    f"{anchor_start.isoformat()}; nothing written, boundary unchanged"
                )
            return 0
    window = build_window(args, now, anchor_start)
    json_path, summary_path = _output_paths(args, window, roots)

    # Idempotence gate, deliberately BEFORE the collectors: a container restart or a
    # second cron firing on the same day must not re-scan Instantly or rewrite a
    # report that already exists for this window.
    if args.once_per_window and not args.no_write and json_path.exists() and not args.force:
        if not args.quiet:
            print(
                f"weekly report for {window.iso_week} already exists at {json_path}; "
                "skipping regeneration (pass --force to regenerate)"
            )
        # Delivery is tracked separately from generation. A run that crashed after
        # writing the report but before Slack accepted it must still deliver -- from
        # the summary already on disk, without rebuilding anything or re-reading
        # Instantly. If the receipt exists too, this is a no-op.
        if args.slack:
            _report_slack(_deliver_existing(window, json_path, summary_path), args)
        return 0

    instantly, airtable = _collectors(args, window)

    report = build_report(
        window,
        artifact_roots=roots,
        instantly=instantly,
        airtable=airtable,
        now=now,
        include_simulated=args.include_simulated,
    )
    # FAIL-SAFE: a Friday whose acquisition did not complete must not produce a
    # report that implies the week finished. Nothing is written and the anchor does
    # NOT advance, so those runs simply belong to the next report rather than being
    # lost -- the window widens instead of a week going missing.
    if args.require_completed_run:
        completed = [r for r in report.runs
                     if str(r.get("status", "")).lower() == "complete"]
        if not completed:
            # The deferred span must stay covered. With no anchor yet, the window
            # was seeded from the fixed weekly boundary; if that seed is not
            # persisted, the NEXT report re-seeds from a later boundary and the
            # skipped span is silently jumped over. Pinning the window START (which
            # is exactly "where the next window begins") makes the fail-safe defer
            # rather than discard. Where an anchor already exists it equals this
            # value, so the write is a no-op.
            if args.anchored and anchor_file is not None and not args.no_write:
                anchor_mod.write_anchor(anchor_file, window.start_utc,
                                        window=window.to_dict())
            if not args.quiet:
                print(
                    f"weekly report for {window.iso_week}: no COMPLETED run is attributed "
                    f"to {window.start_utc.isoformat()} -> {window.end_utc.isoformat()}; "
                    "writing nothing and holding the reporting boundary at "
                    f"{window.start_utc.isoformat()} so those runs land in the next "
                    "report (--require-completed-run)"
                )
            return 0

    document = report.to_dict()
    summary = render_summary(report)

    if not args.quiet:
        print(summary)

    written: List[Path] = []
    if not args.no_write:
        written.append(_write(json_path, json.dumps(document, indent=2, sort_keys=False)))
        written.append(_write(summary_path, summary))
        # The boundary advances HERE -- once the artifact is durably on disk and
        # before Slack is attempted. A delivery failure then retries against this
        # same closed window (guarded by the receipt) instead of holding the window
        # open and merging next week into it. See weekly_report/anchor.py.
        if args.anchored and anchor_file is not None:
            written.append(anchor_mod.write_anchor(
                anchor_file, window.end_utc, window=window.to_dict(),
                report_path=json_path))
        if not args.quiet:
            print()
            for path in written:
                print(f"wrote {path}")

    # Slack runs only after the artifacts are safely on disk, so a delivery failure
    # can never cost us the report. It is also the last thing that can go wrong: it
    # cannot raise, and it does not influence the exit status below.
    if args.slack:
        if args.no_write:
            print(
                "slack: skipped -- --no-write means there is no report on disk to record "
                "a delivery receipt against"
            )
        else:
            _report_slack(
                slack.deliver(
                    summary,
                    report_id=window.iso_week,
                    report_path=json_path,
                    window=window.to_dict(),
                ),
                args,
            )

    if args.strict:
        unmeasured = [
            key
            for key in HEADLINE_ORDER
            if key in report.metrics and not report.metrics[key].available
        ]
        if unmeasured:
            print(
                f"strict: {len(unmeasured)} headline metric(s) unavailable: {', '.join(unmeasured)}",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
