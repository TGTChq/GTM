"""Assemble a window report: the document the summary renders and a dashboard reads.

The output is deliberately *not* a weekly-only shape. It is "the pipeline's
measured behaviour over an arbitrary ``[start, end)`` window", carrying:

* ``window``        -- boundaries in UTC and Pacific local time, plus the tz resolver;
* ``runs``          -- one row per contributing run, with per-run metric values, so a
  dashboard can draw a time series without re-reading artifacts;
* ``daily``         -- the same counters bucketed by Pacific calendar day;
* ``metrics``       -- typed, provenance-carrying numbers (see ``evidence.Metric``);
* ``bottleneck`` / ``action_plan`` -- the measured largest loss and what to do;
* ``gaps``          -- every metric the report refused to guess, and how to fix it.

Nothing here writes to Airtable or Instantly, sends anything, or mutates pipeline
state. Reading artifacts and (optionally) listing remote records is the whole job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from weekly_report import REPORT_BUILDER_VERSION, REPORT_SCHEMA
from weekly_report.bottleneck import Action, Bottleneck, action_plan, identify
from weekly_report.evidence import (
    Gap,
    Metric,
    STATUS_MEASURED,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    dig,
)
from weekly_report.external import CollectorResult
from weekly_report.metrics import (
    COHORT_EXTERNAL_BACKLOG,
    COHORT_RUN_WINDOW,
    MetricSpec,
    RUN_METRIC_SPECS,
    SUPPORTING_METRIC_SPECS,
    UNIT_INSTANTLY_LEAD,
    acquisition_failures,
    aggregate,
    build_run_metrics,
    disposition_census,
    reason_census,
)
from weekly_report.run_artifacts import (
    LEDGER_STEM,
    RunRecord,
    discover_runs,
    partition_realism,
    select_window,
)
from weekly_report.timewindow import ReportingWindow, TZ_SOURCE_FALLBACK, iso_z, local_date_key

#: The headline funnel, in the order Brett reads it.
HEADLINE_ORDER = (
    "jobs_captured",
    "jobs_reviewed",
    "review_rate_pct",
    "qualified_opportunities",
    "contacts_found",
    "sent_to_airtable",
    "sent_to_instantly",
)

#: Orchestrator-side enrollment, used only when a run was actually permitted to enroll.
_ENROLLED_SPEC = MetricSpec(
    key="sent_to_instantly",
    label="Sent to Instantly",
    unit="lead",
    definition="Leads the orchestrator itself enrolled into Instantly during the run.",
    fields=(
        ("ledger", "metrics.sent_to_instantly"),
        ("delivery", "enrolled"),
        ("orchestrator_result", "delivery.enrolled"),
        ("waterfall", "unit_totals.enrolled_contacts"),
    ),
    counted_unit=UNIT_INSTANTLY_LEAD,
    # This variant IS same-window (the run enrolled them itself), but production
    # never takes it: GTM runs with allow_instantly_enrollment=false.
    cohort=COHORT_RUN_WINDOW,
)

_INSTANTLY_DEFINITION = (
    "Leads that entered an Instantly campaign during the window, counted by the "
    "lead's own timestamp_created. Instantly answers 200 for an address already in "
    "the workspace, so an accepted API call is not a delivery; a new "
    "timestamp_created is."
)

_APPROVED_SYNC_REASON = (
    "the orchestrator ran with allow_instantly_enrollment=false in every run this "
    "window, so its artifacts provably cannot answer this. Enrollment is performed "
    "by the separate GTM Approved Sync service (run_approved.py), which writes no "
    "run artifact and has no volume -- its log lines do not survive the container. "
    "Run with --instantly to read the count from Instantly itself."
)


def _policy_allows_enrollment(run: RunRecord) -> Optional[bool]:
    """Whether this run was permitted to enroll, or ``None`` if unrecorded."""
    for stem, path in (
        ("ledger", "policy.allow_instantly_enrollment"),
        ("run_manifest", "policy.allow_instantly_enrollment"),
        ("orchestrator_result", "run.policy.allow_instantly_enrollment"),
    ):
        value = dig(run.artifact(stem), path)
        if isinstance(value, bool):
            return value
    return None


def instantly_metric(
    runs: Sequence[RunRecord],
    collector: Optional[CollectorResult],
) -> Metric:
    """``sent_to_instantly``, from Instantly when available and never invented."""
    metric = Metric(
        key="sent_to_instantly",
        label="Sent to Instantly",
        unit="lead",
        definition=_INSTANTLY_DEFINITION,
        source="instantly",
        attribution="Instantly lead.timestamp_created",
        counted_unit=UNIT_INSTANTLY_LEAD,
        # A DIFFERENT COHORT, and the most consequential declaration in this file.
        # Enrollment is performed by GTM Approved Sync from the Airtable Approved
        # backlog, which accumulates across weeks: on 2026-09-05 it delivered 770
        # leads from 781 Approved rows built up over the preceding fortnight.
        # Subtracting that from THIS window's contacts is not a delivery loss, it
        # is two unrelated populations.
        cohort=COHORT_EXTERNAL_BACKLOG,
    )
    if collector is not None and collector.enabled and collector.ok and collector.count is not None:
        metric.value = collector.count
        metric.status = STATUS_PARTIAL if collector.errors else STATUS_MEASURED
        metric.evidence = ["instantly:POST /leads/list -> item.timestamp_created"]
        detail = collector.detail or {}
        campaigns = detail.get("campaigns_read") or []
        metric.notes = [f"read {len(campaigns)} campaign(s): {', '.join(map(str, campaigns))}"]
        if collector.errors:
            metric.reason = "; ".join(collector.errors)[:400]
        return metric

    enrolling = [run for run in runs if _policy_allows_enrollment(run) is True]
    if enrolling:
        derived = aggregate(_ENROLLED_SPEC, enrolling)
        derived.definition = _ENROLLED_SPEC.definition
        derived.notes.append(
            f"counted from run artifacts for the {len(enrolling)} of {len(runs)} run(s) that "
            "were permitted to enroll; leads enrolled by GTM Approved Sync are NOT included"
        )
        if len(enrolling) != len(runs):
            derived.status = (
                STATUS_PARTIAL if derived.status == STATUS_MEASURED else derived.status
            )
        return derived

    metric.status = STATUS_UNAVAILABLE
    if collector is not None and collector.enabled and not collector.ok:
        metric.reason = "the Instantly collector could not read the workspace: " + (
            "; ".join(collector.errors)[:400] or "unknown error"
        )
    elif not runs:
        metric.reason = (
            "no pipeline run was attributed to this window and Instantly was not read; "
            "run with --instantly to measure deliveries independently of the pipeline"
        )
    else:
        metric.reason = _APPROVED_SYNC_REASON
    return metric


def airtable_snapshot_metric(collector: Optional[CollectorResult]) -> Optional[Metric]:
    """Airtable rows *created* in the window, when the collector was run."""
    if collector is None or not collector.enabled:
        return None
    metric = Metric(
        key="airtable_rows_created_observed",
        label="Airtable rows created (observed in base)",
        unit="row",
        source="airtable",
        definition=(
            "Rows whose Airtable createdTime falls inside the window, counted in the "
            "base itself rather than from run artifacts. Cross-checks sent_to_airtable "
            "and captures rows created by any writer, not only the runs in this window."
        ),
        attribution="Airtable record.createdTime",
        evidence=["airtable:GET records -> record.createdTime"],
    )
    if collector.ok and collector.count is not None:
        metric.value = collector.count
        metric.status = STATUS_PARTIAL if collector.errors else STATUS_MEASURED
    else:
        metric.status = STATUS_UNAVAILABLE
        metric.reason = "; ".join(collector.errors)[:400] or "Airtable could not be read"
    return metric


def _run_row(run: RunRecord, window: ReportingWindow) -> Dict[str, Any]:
    """One dashboard-ready row: identity, timing, and every per-run counter."""
    row = run.to_dict()
    values: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for spec in RUN_METRIC_SPECS + SUPPORTING_METRIC_SPECS + (_ENROLLED_SPEC,):
        value, field_used = spec.read(run)
        values[spec.key] = value
        if field_used:
            sources[spec.key] = field_used
    row["metrics"] = values
    row["metric_sources"] = sources
    row["allow_instantly_enrollment"] = _policy_allows_enrollment(run)
    if run.attributed_at is not None:
        row["local_day"] = local_date_key(run.attributed_at, window.start_local.tzinfo)
    return row


#: The counters a census reconciles. Each is summed over the runs that reported it,
#: which is the same rule ``aggregate`` applies -- so a disagreement means discovery
#: and aggregation saw different run sets, which is the failure this exists to catch.
_CENSUS_METRICS = ("jobs_captured", "jobs_reviewed", "qualified_opportunities",
                   "contacts_found", "sent_to_airtable")


def run_census(
    *,
    all_runs: Sequence[RunRecord],
    in_window: Sequence[RunRecord],
    unattributable: Sequence[RunRecord],
    simulated: Sequence[RunRecord],
    window: ReportingWindow,
    metrics: Dict[str, Metric],
) -> Dict[str, Any]:
    """Every discovered run, why it was included or not, and what it contributed.

    A total is only trustworthy if you can name the runs behind it. "Seven runs, 6,205
    captured" is not checkable; a row per run, each with its own decision and its own
    contribution, is -- and it is the only way to see that two runs on the same day
    were both counted, or that one of them was quietly dropped.

    ``reconciles`` recomputes each headline from the census rows and compares it with
    the metric the report will render. They are computed by different code over the
    same runs, so a mismatch means discovery and aggregation disagree about the run
    set -- the exact class of fault that made a report say four runs when there were
    seven.

    Internal. It is never rendered into the stakeholder message.
    """
    excluded_ids = {run.run_id for run in unattributable} | {run.run_id for run in simulated}
    included_ids = {run.run_id for run in in_window}
    rows: List[Dict[str, Any]] = []

    for run in sorted(all_runs, key=lambda r: r.run_id):
        if run.run_id in included_ids:
            decision, reason = "included", ""
        elif run.run_id in {r.run_id for r in unattributable}:
            decision, reason = "excluded", "no usable completion timestamp"
        elif run.run_id in {r.run_id for r in simulated}:
            decision, reason = "excluded", "simulated run: " + (run.realism_reason or "dry run")
        else:
            decision = "excluded"
            reason = ("finished outside the window ("
                      + (run.attributed_at.isoformat() if run.attributed_at else "no timestamp")
                      + ")")
        contributions: Dict[str, Any] = {}
        for spec in RUN_METRIC_SPECS:
            if spec.key not in _CENSUS_METRICS:
                continue
            value, field_used = spec.read(run)
            # None means SILENT, and stays None. A run that did not report a counter
            # contributes nothing to it, which is not the same as contributing zero.
            contributions[spec.key] = {"value": value, "field": field_used or None}
        rows.append({
            "run_id": run.run_id,
            "state": run.status,
            "attributed_at": run.attributed_at.isoformat() if run.attributed_at else None,
            "attribution_field": run.attribution_field,
            "local_day": (local_date_key(run.attributed_at, window.start_local.tzinfo)
                          if run.attributed_at else None),
            "evidence": ("ledger+artifacts" if run.has_ledger and len(run.artifacts) > 1
                         else "ledger" if run.has_ledger else "artifacts"),
            "reconstructed": bool(dig(run.artifact(LEDGER_STEM),
                                      "backfilled_from_artifacts") or False),
            "decision": decision,
            "reason": reason,
            "contributes": contributions,
        })

    reconciles: Dict[str, Any] = {}
    for key in _CENSUS_METRICS:
        contributing = [r for r in rows if r["decision"] == "included"
                        and r["contributes"].get(key, {}).get("value") is not None]
        total = sum(int(r["contributes"][key]["value"]) for r in contributing)
        metric = metrics.get(key)
        reconciles[key] = {
            "census_total": total if contributing else None,
            "census_runs": sorted(r["run_id"] for r in contributing),
            "reported_value": None if metric is None else metric.value,
            "reported_runs": sorted(metric.contributing_run_ids) if metric else [],
            "agrees": bool(metric is not None
                           and (metric.value if contributing else None)
                           == (total if contributing else None)
                           and sorted(metric.contributing_run_ids)
                           == sorted(r["run_id"] for r in contributing)),
        }

    by_day: Dict[str, List[str]] = {}
    for row in rows:
        if row["decision"] == "included" and row["local_day"]:
            by_day.setdefault(row["local_day"], []).append(row["run_id"])
    return {
        "runs": rows,
        "included": sorted(included_ids),
        "excluded": sorted(excluded_ids | {r["run_id"] for r in rows
                                           if r["decision"] == "excluded"}),
        "included_runs_by_local_day": {d: sorted(v) for d, v in sorted(by_day.items())},
        "reconciles": reconciles,
        "all_reconcile": all(v["agrees"] for v in reconciles.values()),
    }


def _daily_buckets(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-Pacific-day totals, for the dashboard's time series."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        day = row.get("local_day")
        if not day:
            continue
        bucket = buckets.setdefault(day, {"local_day": day, "run_ids": [], "metrics": {}})
        bucket["run_ids"].append(row["run_id"])
        for key, value in (row.get("metrics") or {}).items():
            if value is None:
                continue
            bucket["metrics"][key] = bucket["metrics"].get(key, 0) + int(value)
    return [buckets[day] for day in sorted(buckets)]


def _collect_gaps(
    metrics: Dict[str, Metric],
    *,
    runs: Sequence[RunRecord],
    unattributable: Sequence[RunRecord],
    problems: Sequence[str],
    window: ReportingWindow,
    excluded_simulated: Sequence[Dict[str, str]] = (),
    status_census: Optional[Dict[str, int]] = None,
) -> List[Gap]:
    """Everything the report could not stand behind, with a concrete remedy."""
    gaps: List[Gap] = []

    for key in HEADLINE_ORDER:
        metric = metrics.get(key)
        if metric is None or metric.status == STATUS_MEASURED:
            continue
        if metric.status == STATUS_UNAVAILABLE:
            impact = f"'{metric.label}' is reported as unavailable, not as zero."
            remedy = _remedy_for(key, metric.reason)
            gaps.append(Gap(metric=key, reason=metric.reason or "not reconstructible", impact=impact, remedy=remedy))
        elif metric.status == STATUS_PARTIAL:
            gaps.append(
                Gap(
                    metric=key,
                    reason=metric.reason or "some runs did not report this counter",
                    impact=f"'{metric.label}' is a floor over {len(metric.contributing_run_ids)} run(s), not a total.",
                    remedy=(
                        "Identify which runs are silent and why. A partial total is a "
                        "floor over the runs that answered; it is never presented as the "
                        "period's number, and it must not be quoted as one."
                    ),
                )
            )

    if unattributable:
        gaps.append(
            Gap(
                metric="run_attribution",
                reason=(
                    f"{len(unattributable)} run director(ies) carry no usable timestamp "
                    f"({', '.join(r.run_id for r in unattributable[:5])})"
                ),
                impact="Those runs are excluded from every metric in this window.",
                remedy=(
                    "They predate run_manifest.finished_at or their manifest is unreadable; "
                    "no action is needed for current runs, which always stamp both ends."
                ),
            )
        )

    unhealthy = {
        status: count
        for status, count in (status_census or {}).items()
        if status.lower() not in ("complete", "success")
    }
    if unhealthy:
        listed = ", ".join(f"{status}={count}" for status, count in sorted(unhealthy.items()))
        gaps.append(
            Gap(
                metric="run_completeness",
                reason=f"{sum(unhealthy.values())} of {len(runs)} run(s) did not complete ({listed})",
                impact=(
                    "Counters from a run that stopped early describe a partial run, so the "
                    "week's totals understate what the configuration would otherwise produce."
                ),
                remedy=(
                    "Read stop_reason on those runs in runs[]; a stopped run is an execution "
                    "problem, not a yield result, and should be fixed before re-tuning targeting."
                ),
            )
        )

    interrupted = [run for run in runs if run.interrupted]
    if interrupted:
        gaps.append(
            Gap(
                metric="interrupted_runs",
                reason=(
                    f"{len(interrupted)} run(s) started but never finalized "
                    f"({', '.join(r.run_id for r in interrupted[:5])}); their reporting-ledger "
                    "entry is still in state 'running'"
                ),
                impact=(
                    "The counters those runs DID record are included. The stages they never "
                    "reached report as unavailable, never as zero, so the week's totals are a "
                    "floor for those runs rather than a final figure."
                ),
                remedy=(
                    "A ledger entry left in 'running' means the process was killed outright "
                    "(SIGKILL/OOM/container stop) -- an ordinary exception still finalizes as "
                    "'failed'. Check the container's exit reason for those run ids."
                ),
            )
        )

    if excluded_simulated:
        gaps.append(
            Gap(
                metric="simulated_runs",
                reason=(
                    f"{len(excluded_simulated)} run(s) in this window were dry runs and were "
                    f"excluded: {', '.join(entry['run_id'] for entry in excluded_simulated[:5])}"
                ),
                impact=(
                    "Their counters are manufactured and are not included in any metric; the "
                    "window may therefore show less activity than the artifact count suggests."
                ),
                remedy=(
                    "Expected when a dry run shares the artifact root with production runs. "
                    "Point --artifact-root at the production root only, or pass "
                    "--include-simulated when you deliberately want them counted."
                ),
            )
        )

    if not runs:
        gaps.append(
            Gap(
                metric="pipeline_activity",
                reason="no run artifact directory was attributed to this window",
                impact="Every run-derived metric is unavailable for this window.",
                remedy=(
                    "Confirm the GTM cron executed, and that --artifact-root points at the "
                    "volume path the runs actually wrote to "
                    "(production: /app/data/state/orchestrator_v2)."
                ),
            )
        )

    for problem in problems:
        gaps.append(
            Gap(
                metric="artifact_access",
                reason=problem,
                impact="Runs under that root, if any, are missing from the report.",
                remedy="Point --artifact-root at a readable run_artifacts location.",
            )
        )

    if window.timezone_source == TZ_SOURCE_FALLBACK:
        gaps.append(
            Gap(
                metric="timezone_source",
                reason="no IANA tz database was available; the codified US DST rule was used",
                impact=(
                    "Boundaries are correct for the current federal rule but will not track a "
                    "future change to it."
                ),
                remedy="Install tzdata in the runtime image (it is already in requirements.txt).",
            )
        )
    return gaps


_REMEDIES = {
    "sent_to_instantly": (
        "Run the report with --instantly so the count comes from Instantly's own "
        "lead.timestamp_created; the Approved Sync service writes no artifact to read."
    ),
    "jobs_reviewed": (
        "jobs_reviewed comes from orchestrator_result.json:enrichment.funnel.qualification_input. "
        "A run that stopped before enrichment never produced it -- check that run's stop_reason."
    ),
    "jobs_captured": (
        "jobs_captured comes from waterfall.json:unit_totals.postings. Confirm the run "
        "completed its acquisition stage."
    ),
    "qualified_opportunities": (
        "Confirm enrichment ran; the counter lives in "
        "enrichment.funnel.contact_discovery_entered, emitted by the hiring-manager "
        "stage when an opportunity enters contact discovery. Runs written before "
        "2026-09-05 do not carry it and cannot be backfilled with it."
    ),
    "contacts_found": "Confirm enrichment ran; the counter lives in waterfall unit_totals.contacts.",
    "sent_to_airtable": (
        "delivery.json is only written when a run reaches delivery; check the run's stop_reason."
    ),
    "review_rate_pct": (
        "Both jobs_captured and jobs_reviewed must be measurable for a rate to exist. "
        "A rate of 100% means every captured posting entered review; below 100 means "
        "a run stopped between acquiring postings and reviewing them."
    ),
}

#: When the collector *was* asked and failed, telling the reader to pass --instantly
#: again is useless; the fix is the credential, on the service running the report.
_COLLECTOR_FAILURE_REMEDY = (
    "The Instantly read was attempted and failed. Set INSTANTLY_API_KEY and at least one "
    "INSTANTLY_CAMPAIGN_* id on the service running the report (they live on GTM Approved "
    "Sync today; Railway variables are per service), then re-run."
)


def _remedy_for(key: str, reason: str) -> str:
    """The fix to print for an unavailable metric, given why it was unavailable."""
    if key == "sent_to_instantly" and reason.startswith("the Instantly collector could not read"):
        return _COLLECTOR_FAILURE_REMEDY
    return _REMEDIES.get(key, "Instrument this counter before the next report.")


@dataclass
class WeeklyReport:
    """The full document. ``to_dict()`` is the dashboard/JSON contract."""

    window: ReportingWindow
    generated_at: datetime
    metrics: Dict[str, Metric]
    runs: List[Dict[str, Any]]
    daily: List[Dict[str, Any]]
    bottleneck: Bottleneck
    actions: List[Action]
    gaps: List[Gap]
    collectors: List[CollectorResult] = field(default_factory=list)
    #: Internal run-by-run accounting: every discovered run, why it was included or
    #: excluded, what it contributed, and whether those contributions add back up to
    #: the rendered totals. Never rendered into the stakeholder message.
    census: Dict[str, Any] = field(default_factory=dict)
    reasons: Dict[str, int] = field(default_factory=dict)
    dispositions: Dict[str, int] = field(default_factory=dict)
    run_status_census: Dict[str, int] = field(default_factory=dict)
    lane_failures: List[Dict[str, Any]] = field(default_factory=list)
    unattributable_run_ids: List[str] = field(default_factory=list)
    excluded_simulated: List[Dict[str, str]] = field(default_factory=list)
    artifact_roots: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def run_ids(self) -> List[str]:
        return [row["run_id"] for row in self.runs]

    def headline(self) -> List[Metric]:
        return [self.metrics[key] for key in HEADLINE_ORDER if key in self.metrics]

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "builder_version": REPORT_BUILDER_VERSION,
            "generated_at": iso_z(self.generated_at),
            "included_run_ids": self.run_ids,
            "run_count": len(self.runs),
        }
        payload.update(self.window.to_dict())
        payload["metric_rules"] = {
            "window_attribution": (
                "run completion timestamp (run_manifest.finished_at), falling back to "
                "started_at, then to the run_id prefix"
            ),
            "posted_at_excluded": (
                "A job's posted_at is never used to place work in a week; backlog processed "
                "this week counts as this week's throughput."
            ),
            "silence_is_not_zero": (
                "A run that does not carry a counter is listed in runs_missing_field; it is "
                "never summed as zero."
            ),
            "interval": "half-open [start, end) so consecutive reports never double-count a run",
            "simulated_runs_excluded": (
                "A dry-run/preflight run writes the same artifact shape as a live run. "
                "Its counters are excluded from every metric and listed under "
                "provenance.excluded_simulated_runs."
            ),
        }
        payload["headline"] = [self.metrics[k].to_dict() for k in HEADLINE_ORDER if k in self.metrics]
        payload["metrics"] = {key: metric.to_dict() for key, metric in sorted(self.metrics.items())}
        payload["runs"] = list(self.runs)
        payload["daily"] = list(self.daily)
        payload["bottleneck"] = self.bottleneck.to_dict()
        payload["action_plan"] = [action.to_dict() for action in self.actions]
        payload["gaps"] = [gap.to_dict() for gap in self.gaps]
        payload["run_status_census"] = dict(self.run_status_census)
        if self.census:
            payload["run_census"] = self.census
        payload["loss_reasons"] = dict(self.reasons)
        payload["disposition_census"] = dict(self.dispositions)
        payload["acquisition_lane_failures"] = list(self.lane_failures)
        payload["collectors"] = [collector.to_dict() for collector in self.collectors]
        payload["provenance"] = {
            "artifact_roots": list(self.artifact_roots),
            "unattributable_run_ids": list(self.unattributable_run_ids),
            "excluded_simulated_runs": list(self.excluded_simulated),
            "problems": list(self.problems),
            # Runs reported entirely from the compact ledger because storage
            # retention has already deleted their heavy evidence. This is the
            # NORMAL steady state for any run older than a few days, and is
            # recorded rather than flagged: the counters are unaffected.
            "runs_reported_from_ledger_only": [
                row["run_id"] for row in self.runs
                if row.get("evidence_source") == "ledger"
            ],
            "runs_with_heavy_artifacts": [
                row["run_id"] for row in self.runs if row.get("heavy_artifacts_present")
            ],
            "writes_performed": "none (read-only report)",
        }
        return payload


def build_report(
    window: ReportingWindow,
    *,
    artifact_roots: Sequence[str],
    instantly: Optional[CollectorResult] = None,
    airtable: Optional[CollectorResult] = None,
    now: Optional[datetime] = None,
    include_simulated: bool = False,
) -> WeeklyReport:
    """Discover runs, measure the window, and assemble the document.

    ``include_simulated`` is a local debugging switch only. By default a dry-run's
    counters are excluded: they are manufactured, and reporting them as throughput
    would be a fabricated business result.
    """
    generated_at = now or datetime.now(timezone.utc)
    all_runs, problems = discover_runs(artifact_roots)
    attributed, unattributable = select_window(all_runs, window)
    production, simulated = partition_realism(attributed)
    in_window = attributed if include_simulated else production
    excluded = (
        []
        if include_simulated
        else [
            {"run_id": run.run_id, "mode": run.mode, "reason": run.realism_reason}
            for run in simulated
        ]
    )

    metrics = build_run_metrics(in_window)
    metrics["sent_to_instantly"] = instantly_metric(in_window, instantly)
    observed_airtable = airtable_snapshot_metric(airtable)
    if observed_airtable is not None:
        metrics[observed_airtable.key] = observed_airtable

    reasons = reason_census(in_window)
    dispositions = disposition_census(in_window)
    lane_failures = acquisition_failures(in_window)
    status_census: Dict[str, int] = {}
    for run in in_window:
        status_census[run.status] = status_census.get(run.status, 0) + 1

    rows = [_run_row(run, window) for run in in_window]
    census = run_census(all_runs=all_runs, in_window=in_window,
                        unattributable=unattributable, simulated=simulated,
                        window=window, metrics=metrics)
    gaps = _collect_gaps(
        metrics,
        runs=in_window,
        unattributable=unattributable,
        problems=problems,
        window=window,
        excluded_simulated=excluded,
        status_census=status_census,
    )
    found = identify(
        metrics,
        run_count=len(in_window),
        reasons=reasons,
        acquisition_errors=lane_failures,
        runs=in_window,
    )
    actions = action_plan(found, metrics, gaps=gaps)

    collectors = [c for c in (instantly, airtable) if c is not None]
    return WeeklyReport(
        window=window,
        generated_at=generated_at,
        metrics=metrics,
        runs=rows,
        daily=_daily_buckets(rows),
        bottleneck=found,
        actions=actions,
        gaps=gaps,
        collectors=collectors,
        census=census,
        reasons=reasons,
        dispositions=dispositions,
        run_status_census=status_census,
        lane_failures=lane_failures,
        unattributable_run_ids=[run.run_id for run in unattributable],
        excluded_simulated=excluded,
        artifact_roots=[str(root) for root in artifact_roots],
        problems=problems,
    )
