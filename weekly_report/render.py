"""The human-readable summary.

One screen, plain text, safe for a Railway log, an email body or a Slack paste.
It is a *view* over the report document -- it computes nothing. Anything the
renderer prints can be traced to a field in the JSON, and a metric the report
could not measure prints as ``not measured`` with its reason, never as ``0``.
"""

from __future__ import annotations

import textwrap
from typing import List, Optional

from weekly_report.evidence import Metric, STATUS_MEASURED, STATUS_PARTIAL
from weekly_report.report import HEADLINE_ORDER, WeeklyReport
from weekly_report.timewindow import iso_z

_WIDTH = 78
_LABEL_WIDTH = 32


def _rule(char: str = "-") -> str:
    return char * _WIDTH


def _wrap(text: str, *, indent: str = "  ", hanging: Optional[str] = None) -> List[str]:
    """Wrap a long line to the report width, so nothing is truncated or runs off."""
    return textwrap.wrap(
        text,
        width=_WIDTH,
        initial_indent=indent,
        subsequent_indent=hanging if hanging is not None else indent,
    ) or [indent + text]


def _fmt_value(metric: Metric) -> str:
    if metric.value is None:
        return "not measured"
    if metric.unit == "percent":
        return f"{metric.value:.1f}%"
    return f"{int(metric.value):,}"


def _fmt_metric_line(metric: Metric) -> str:
    suffix = ""
    if metric.status == STATUS_PARTIAL:
        suffix = "  (partial: not every run reported it)"
    elif metric.value is None:
        suffix = "  (see NOT MEASURED below)"
    return f"  {metric.label:<{_LABEL_WIDTH}}{_fmt_value(metric):>10}{suffix}"


def _stage_change(report: WeeklyReport, from_key: str, to_key: str) -> Optional[str]:
    a, b = report.metrics.get(from_key), report.metrics.get(to_key)
    if a is None or b is None or not a.available or not b.available or not a.value:
        return None
    return f"{100.0 * float(b.value) / float(a.value):.1f}% of {a.label.lower()}"


def render_summary(report: WeeklyReport) -> str:
    """The report Brett reads."""
    window = report.window
    lines: List[str] = []
    lines.append(_rule("="))
    lines.append("TGTC WEEKLY PIPELINE REPORT")
    lines.append(f"Week of {window.label}  ({window.iso_week})")
    lines.append(_rule("="))
    lines.append(
        f"Reporting window : {window.start_local:%Y-%m-%d %H:%M %Z} -> "
        f"{window.end_local:%Y-%m-%d %H:%M %Z}  ({window.timezone_name})"
    )
    lines.append(
        f"                   {iso_z(window.start_utc)} -> {iso_z(window.end_utc)}  (half-open)"
    )
    lines.append(f"Generated at     : {iso_z(report.generated_at)}")
    run_ids = report.run_ids
    lines.append(f"Pipeline runs    : {len(run_ids)}")
    if run_ids:
        for run_id in run_ids:
            lines.append(f"                   {run_id}")
    if report.excluded_simulated:
        excluded = ", ".join(entry["run_id"] for entry in report.excluded_simulated)
        lines.extend(
            _wrap(f"Dry runs excluded : {excluded}", indent="", hanging="                    ")
        )
    lines.append("")

    lines.append("FUNNEL")
    for key in HEADLINE_ORDER:
        metric = report.metrics.get(key)
        if metric is None:
            continue
        lines.append(_fmt_metric_line(metric))
    lines.append("")

    conversions = [
        ("jobs_captured", "jobs_reviewed", "reviewed"),
        ("jobs_reviewed", "qualified_opportunities", "qualified"),
        ("qualified_opportunities", "contacts_found", "contacted"),
        ("contacts_found", "sent_to_airtable", "written to Airtable"),
    ]
    conversion_lines = [
        f"  {label:<{_LABEL_WIDTH}}{change}"
        for from_key, to_key, label in conversions
        if (change := _stage_change(report, from_key, to_key))
    ]
    if conversion_lines:
        lines.append("CONVERSION")
        lines.extend(conversion_lines)
        lines.append("")

    lines.append("BIGGEST MEASURABLE BOTTLENECK")
    bottleneck = report.bottleneck
    lines.extend(_wrap(bottleneck.statement or "not determined"))
    if bottleneck.top_reasons:
        shown = [
            f"{entry.get('reason')}={entry.get('count')}"
            for entry in bottleneck.top_reasons
            if entry.get("count") is not None
        ]
        if shown:
            lines.extend(_wrap(f"Top recorded loss reasons: {', '.join(shown[:5])}"))
    lines.append("")

    lines.append("NEXT WEEK")
    for action in report.actions:
        lines.extend(_wrap(f"{action.priority}. {action.action}", indent="  ", hanging="     "))
        lines.extend(_wrap(f"basis: {action.basis}", indent="     ", hanging="            "))
    lines.append("")

    if report.gaps:
        lines.append("NOT MEASURED (declared, never guessed)")
        for gap in report.gaps:
            lines.extend(_wrap(f"- {gap.metric}: {gap.reason}", indent="  ", hanging="    "))
            lines.extend(_wrap(f"fix: {gap.remedy}", indent="    ", hanging="         "))
        lines.append("")

    measured = sum(
        1
        for key in HEADLINE_ORDER
        if (m := report.metrics.get(key)) and m.status in (STATUS_MEASURED, STATUS_PARTIAL)
    )
    lines.append(_rule())
    lines.append(
        f"Headline metrics measured: {measured}/{len(HEADLINE_ORDER)}  |  "
        f"tz source: {window.timezone_source}  |  writes performed: none"
    )
    lines.append(_rule())
    return "\n".join(lines)
