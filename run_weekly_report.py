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

from weekly_report.external import CollectorResult, collect_airtable, collect_instantly, disabled
from weekly_report.render import render_summary
from weekly_report.report import HEADLINE_ORDER, build_report
from weekly_report.timewindow import (
    PACIFIC_TZ_NAME,
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


def build_window(args: argparse.Namespace, now: datetime) -> ReportingWindow:
    """The window this invocation reports on."""
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

    if args.instantly:
        try:
            instantly = collect_instantly(
                window, cfg=cfg, campaign_ids=args.campaign_id or None
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


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

    window = build_window(args, now)
    roots = args.artifact_root or _default_artifact_roots()
    instantly, airtable = _collectors(args, window)

    report = build_report(
        window,
        artifact_roots=roots,
        instantly=instantly,
        airtable=airtable,
        now=now,
        include_simulated=args.include_simulated,
    )
    document = report.to_dict()
    summary = render_summary(report)

    if not args.quiet:
        print(summary)

    written: List[Path] = []
    if not args.no_write:
        stem = f"weekly_report_{window.iso_week}"
        out_dir = Path(args.out_dir) if args.out_dir else Path(roots[0]) / _DEFAULT_SUBDIR
        json_path = Path(args.json_out) if args.json_out else out_dir / f"{stem}.json"
        summary_path = Path(args.summary_out) if args.summary_out else out_dir / f"{stem}.txt"
        written.append(_write(json_path, json.dumps(document, indent=2, sort_keys=False)))
        written.append(_write(summary_path, summary))
        if not args.quiet:
            print()
            for path in written:
                print(f"wrote {path}")

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
