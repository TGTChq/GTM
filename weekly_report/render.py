"""The human-readable summaries.

Two views over the same report document, for two different readers. Both are
*views*: they compute nothing, everything they print traces to a field in the
JSON, and a metric the report could not measure prints as ``not measured`` with
its reason -- never as ``0``.

* ``render_summary`` -- the full internal record: provenance, per-run ids, gaps,
  conversion rates, the declared NOT MEASURED block. This is what lands in the
  ``.txt`` artifact beside the JSON and in the Railway log.
* ``render_stakeholder_summary`` -- the five lines Brett asked for, plus the
  bottleneck and next week's plan. No run ids, no field paths, no evidence
  apparatus. A stakeholder asked "I have no idea what the numbers we are trying
  to measure are from that", and the answer is a shorter report, not a longer one.
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
    """The full internal record. Brett gets ``render_stakeholder_summary``."""
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
    unhealthy = {
        status: count
        for status, count in report.run_status_census.items()
        if status.lower() not in ("complete", "success")
    }
    if unhealthy:
        listed = ", ".join(f"{status}={count}" for status, count in sorted(unhealthy.items()))
        lines.append(f"Runs not complete: {listed}")
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


# -- the stakeholder view --------------------------------------------------
#
# THIS IS A FIXED FORMAT. Brett's message is exactly:
#
#     <one short period label>
#     Jobs: {X} captured / {Y} reviewed ({Z}%)
#     Qualified opportunities: {Q}
#     Contacts found: {C}
#     sent to Instantly: {I}
#
#     Biggest bottleneck from past week
#     <one short, evidence-backed explanation>
#
#     Action plan for the following week
#     <one to three concrete actions>
#
# Nothing else. No metric dashboard, no debug counters, no acquisition table, no
# FINAL_PASS census, no gate jargon, no provider-duplication line. All of that is
# already written on every run, to the JSON document and the internal summary,
# and adding it here is how a stakeholder message stops being read.
#
# ``render_summary`` above is the internal view and is deliberately unconstrained.

#: The three plain count lines, in Brett's order and with his exact wording --
#: including the lower-case "sent to Instantly", which is his, not a typo.
_STAKEHOLDER_ROWS = (
    ("qualified_opportunities", "Qualified opportunities"),
    ("contacts_found", "Contacts found"),
    ("sent_to_instantly", "sent to Instantly"),
)

#: At most this many actions reach the stakeholder message.
_MAX_ACTIONS = 3


def _count(metric: Optional[Metric]) -> str:
    """A stakeholder-facing count. Never invents a zero for a missing measurement."""
    if metric is None or metric.value is None:
        return "not measured"
    return f"{int(metric.value):,}"


def _pct(value: float) -> str:
    """``100%`` rather than ``100.0%``; one decimal only when it carries meaning."""
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


def _jobs_line(report: WeeklyReport) -> str:
    """``Jobs: X captured / Y reviewed (Z%)``, degrading honestly.

    Both sides count POSTINGS -- ``_dedup`` counts one per provider posting and
    ``qualification_pipeline`` reports ``input_jobs = len(jobs)`` -- so this is the
    one boundary in the message whose two numbers are the same population and
    whose ratio is meaningful.

    Equal non-zero populations render ``(100%)``. A zero denominator renders
    ``(N/A)``: a rate over an empty population is undefined, and printing 0%
    would assert that nothing captured was reviewed.
    """
    captured = report.metrics.get("jobs_captured")
    reviewed = report.metrics.get("jobs_reviewed")
    if captured is None or captured.value is None:
        return "Jobs: not measured"
    head = f"Jobs: {int(captured.value):,} captured"
    if reviewed is None or reviewed.value is None:
        return f"{head} / reviewed not measured"
    tail = f" / {int(reviewed.value):,} reviewed"
    rate = report.metrics.get("review_rate_pct")
    if rate is not None and rate.value is not None:
        return f"{head}{tail} ({_pct(rate.value)}%)"
    return f"{head}{tail} (N/A)"


def render_stakeholder_summary(report: WeeklyReport) -> str:
    """The weekly message Brett reads. Four numbers, a cause, a plan.

    Everything this omits -- run ids, artifact field paths, provenance, the gap
    register, per-source acquisition, loss-reason censuses -- stays in the JSON
    document and the internal summary, which are written on every run regardless.
    """
    lines: List[str] = [
        f"Week of {report.window.label}",
        _jobs_line(report),
    ]
    for key, label in _STAKEHOLDER_ROWS:
        lines.append(f"{label}: {_count(report.metrics.get(key))}")

    lines.append("")
    lines.append("Biggest bottleneck from past week")
    lines.extend(_wrap(report.bottleneck.statement or "not determined", indent=""))

    lines.append("")
    lines.append("Action plan for the following week")
    actions = list(report.actions)[:_MAX_ACTIONS]
    if actions:
        for index, action in enumerate(actions, start=1):
            lines.extend(_wrap(f"{index}. {action.action}", indent="", hanging="   "))
    else:
        lines.extend(_wrap(
            "No action is proposed: no comparable funnel boundary could be measured "
            "this window, so any plan would be guessing at a cause.", indent=""))
    return "\n".join(lines)
