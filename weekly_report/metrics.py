"""Metric definitions: the authoritative source and timestamp for every number.

Each headline metric names, in priority order, the fields that may supply it. The
first field a run actually carries is used, and the choice is recorded per run, so
a report built from a mix of old and new shapes still declares exactly which field
produced each contribution.

The compact reporting ledger (``orchestrator/run_ledger.py``) is the FIRST
candidate for every metric. Heavy run artifacts are pruned down to a few runs by
storage retention -- reporting from them alone silently lost 3 of 7 runs in
2026-W36 -- while the ledger is kept for months. The heavy fields stay in the list
as fallbacks so runs written before the ledger existed still read unchanged.

Aggregation rule across the runs in a window: **sum over the runs that reported
the field**, and list the runs that did not. A run that is silent about a field is
never counted as a zero, because "the pipeline processed nothing" and "the
artifact does not carry this counter" are different facts and only one of them is
a business result.

Window attribution for every run-derived metric is the run's own completion
timestamp (see ``run_artifacts``). Job ``posted_at`` is never used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from weekly_report.evidence import (
    Metric,
    STATUS_MEASURED,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    as_count,
    dig,
    weakest,
)
from weekly_report.run_artifacts import LEDGER_STEM, RunRecord

#: Every run-derived metric now reads the compact ledger first and the heavy
#: artifacts second, so the label names both stores. ``Metric.evidence`` still
#: records the exact field that answered, per run.
SOURCE_RUN_ARTIFACTS = "run_ledger+run_artifacts"


def _field_label(stem: str, path: str) -> str:
    """Human-readable provenance for one candidate field."""
    return f"reporting_ledger:{path}" if stem == LEDGER_STEM else f"{stem}.json:{path}"


@dataclass(frozen=True)
class MetricSpec:
    """How one headline metric is reconstructed from a run's artifacts."""

    key: str
    label: str
    unit: str
    definition: str
    #: ``(artifact_stem, dotted_path)`` candidates, most authoritative first.
    fields: Tuple[Tuple[str, str], ...]

    def field_labels(self) -> Tuple[str, ...]:
        """Every candidate this metric may be read from, most authoritative first."""
        return tuple(_field_label(stem, path) for stem, path in self.fields)

    def read(self, run: RunRecord) -> Tuple[Optional[int], str]:
        """First present candidate as ``(value, "stem.json:path")``.

        The compact reporting ledger is listed first for every metric because it is
        the only store guaranteed to still exist: heavy artifacts are pruned to a
        handful of runs, the ledger is retained for months. The heavy fields remain
        as fallbacks so pre-ledger runs still read exactly as they did.
        """
        for stem, path in self.fields:
            raw = dig(run.artifact(stem), path)
            value = as_count(raw)
            if value is not None:
                return value, _field_label(stem, path)
        return None, ""


#: The headline funnel, in pipeline order. The dashboard renders this order too.
RUN_METRIC_SPECS: Tuple[MetricSpec, ...] = (
    MetricSpec(
        key="jobs_captured",
        label="Jobs captured",
        unit="posting",
        definition=(
            "Raw job postings acquired by pipeline runs that completed inside the "
            "reporting window. Counted per run at the acquisition boundary, not by "
            "the posting's own publication date."
        ),
        fields=(
            ("ledger", "metrics.jobs_captured"),
            ("waterfall", "unit_totals.postings"),
            ("capacity_report", "raw_postings"),
            ("orchestrator_result", "capacity.raw_postings"),
            ("orchestrator_result", "waterfall.unit_totals.postings"),
        ),
    ),
    MetricSpec(
        key="jobs_reviewed",
        label="Jobs reviewed",
        unit="posting",
        definition=(
            "Postings that actually entered the qualification/review stage in those "
            "runs. Captured postings that a run never reached -- because it stopped "
            "early, hit a provider limit, or deduped them away -- are excluded."
        ),
        fields=(
            ("ledger", "metrics.jobs_reviewed"),
            ("orchestrator_result", "enrichment.funnel.qualification_input"),
        ),
    ),
    MetricSpec(
        key="qualified_opportunities",
        label="Qualified opportunities",
        unit="opportunity",
        definition=(
            "Opportunities that cleared job/role policy AND the company/account ICP "
            "decision AND had a resolvable search domain, and therefore entered "
            "contact discovery. Counted by the hiring-manager stage at the moment a "
            "people search is issued -- never reconstructed by subtracting reason "
            "codes from a total."
        ),
        fields=(
            ("ledger", "metrics.qualified_opportunities"),
            ("orchestrator_result", "enrichment.funnel.contact_discovery_entered"),
        ),
    ),
    MetricSpec(
        key="contacts_found",
        label="Contacts found",
        unit="contact",
        definition=(
            "Leads carrying a resolved hiring-manager email address. Email presence, "
            "never a sum of disposition labels."
        ),
        fields=(
            ("ledger", "metrics.contacts_found"),
            ("waterfall", "unit_totals.contacts"),
            ("orchestrator_result", "waterfall.unit_totals.contacts"),
            ("orchestrator_result", "enrichment.funnel.contactable_hiring_managers"),
        ),
    ),
    MetricSpec(
        key="sent_to_airtable",
        label="Rows created in Airtable",
        unit="row",
        definition=(
            "Airtable rows the run created (review staging). Rows suppressed as "
            "duplicates or already-present are excluded."
        ),
        fields=(
            ("ledger", "metrics.sent_to_airtable"),
            ("delivery", "created"),
            ("orchestrator_result", "delivery.created"),
        ),
    ),
)

#: Secondary counters: useful context on the report and for the dashboard, but
#: not part of the headline funnel Brett asked for.
SUPPORTING_METRIC_SPECS: Tuple[MetricSpec, ...] = (
    MetricSpec(
        key="unique_opportunities",
        label="Unique opportunities after dedupe",
        unit="opportunity",
        definition="Postings remaining after cross-run and in-run deduplication.",
        fields=(
            ("ledger", "metrics.unique_opportunities"),
            ("waterfall", "unit_totals.opportunities"),
            ("orchestrator_result", "waterfall.unit_totals.opportunities"),
        ),
    ),
    MetricSpec(
        key="final_pass_leads",
        label="FINAL_PASS leads",
        unit="lead",
        definition="Leads whose validated disposition is FINAL_PASS (send-safe candidates).",
        fields=(
            ("ledger", "metrics.final_pass_leads"),
            ("waterfall", "final_pass_count"),
            ("orchestrator_result", "waterfall.final_pass_count"),
        ),
    ),
    MetricSpec(
        key="verified_emails",
        label="Apollo-verified emails",
        unit="contact",
        definition="Contacts whose Apollo email status is 'verified'.",
        fields=(
            ("ledger", "metrics.verified_emails"),
            ("orchestrator_result", "emails.verified"),
        ),
    ),
    MetricSpec(
        key="role_qualified_postings",
        label="Postings passing the role/source gate",
        unit="posting",
        definition=(
            "Postings the pre-contact JobGate and RoleGate did not reject. A loose "
            "upstream filter -- 92.6% of postings passed it on the 2026-09-04 control "
            "run -- kept as context, NOT as 'qualified opportunities'."
        ),
        fields=(
            ("ledger", "metrics.role_qualified_postings"),
            ("orchestrator_result", "enrichment.funnel.target_role_eligible"),
        ),
    ),
    MetricSpec(
        key="companies_considered",
        label="Companies considered",
        unit="company",
        definition="Distinct companies evaluated against ICP criteria.",
        fields=(
            ("ledger", "metrics.companies_considered"),
            ("orchestrator_result", "enrichment.funnel.companies_considered"),
        ),
    ),
    MetricSpec(
        key="airtable_suppressed",
        label="Airtable rows suppressed as existing",
        unit="row",
        definition=(
            "Deliverable leads not written because the company/function was already "
            "represented in Airtable."
        ),
        fields=(
            ("ledger", "metrics.airtable_suppressed"),
            ("delivery", "skipped_existing"),
            ("orchestrator_result", "delivery.skipped_existing"),
        ),
    ),
)


def aggregate(spec: MetricSpec, runs: Sequence[RunRecord]) -> Metric:
    """Sum ``spec`` across ``runs``, recording provenance and silence."""
    metric = Metric(
        key=spec.key,
        label=spec.label,
        unit=spec.unit,
        source=SOURCE_RUN_ARTIFACTS,
        definition=spec.definition,
        attribution="run completion timestamp (run_manifest.finished_at)",
    )
    if not runs:
        metric.status = STATUS_UNAVAILABLE
        metric.reason = "no pipeline run was attributed to this reporting window"
        return metric

    total = 0
    evidence: List[str] = []
    for run in runs:
        value, field_used = spec.read(run)
        if value is None:
            metric.runs_missing_field.append(run.run_id)
            continue
        total += value
        metric.contributing_run_ids.append(run.run_id)
        if field_used not in evidence:
            evidence.append(field_used)

    metric.evidence = evidence
    if not metric.contributing_run_ids:
        metric.status = STATUS_UNAVAILABLE
        # Name EVERY candidate, not just the most authoritative one: the reader's
        # next question is "where would this have come from?", and answering with
        # only the ledger path hides the artifact field that actually instruments it.
        metric.reason = (
            f"none of the {len(runs)} run(s) in this window carry any of: "
            + ", ".join(spec.field_labels())
        )
        return metric

    metric.value = total
    metric.status = STATUS_MEASURED if not metric.runs_missing_field else STATUS_PARTIAL
    if metric.runs_missing_field:
        metric.reason = (
            f"{len(metric.runs_missing_field)} of {len(runs)} runs did not report this "
            "counter; the total covers the runs that did"
        )
    return metric


def ratio_metric(
    key: str,
    label: str,
    numerator: Metric,
    denominator: Metric,
    *,
    definition: str,
) -> Metric:
    """A percentage derived from two metrics, inheriting their weakest status."""
    metric = Metric(
        key=key,
        label=label,
        unit="percent",
        source="derived",
        definition=definition,
        attribution=numerator.attribution or denominator.attribution,
        evidence=[f"{numerator.key} / {denominator.key}"],
        contributing_run_ids=sorted(
            set(numerator.contributing_run_ids) & set(denominator.contributing_run_ids)
        ),
    )
    if not numerator.available or not denominator.available:
        missing = [m.key for m in (numerator, denominator) if not m.available]
        metric.status = STATUS_UNAVAILABLE
        metric.reason = f"requires {' and '.join(missing)}, which {'is' if len(missing) == 1 else 'are'} unavailable"
        return metric
    if not denominator.value:
        metric.status = STATUS_UNAVAILABLE
        metric.reason = f"{denominator.key} is 0; a rate over an empty denominator is undefined"
        return metric
    metric.value = round(100.0 * float(numerator.value) / float(denominator.value), 1)
    metric.status = weakest(numerator.status, denominator.status)
    if metric.status == STATUS_PARTIAL:
        metric.reason = "derived from at least one partial input"
    return metric


def reason_census(runs: Sequence[RunRecord]) -> Dict[str, int]:
    """Aggregate loss reasons across the window, exactly as the run summary does.

    Merges waterfall stage ``primary_reasons``, qualification reason counts, and
    the enrichment loss census -- the three places the orchestrator records *why*
    a record did not advance.

    Per run the compact ledger's own copy wins when it exists, and the heavy
    artifacts are then NOT read for that run. Reading both would double-count,
    because the ledger copy is the same merge of the same three sources. This is
    what lets the action plan stay specific after retention deletes the evidence:
    before it, a pruned week lost every reason code and fell back to generic text
    that contradicted the measured bottleneck.
    """
    census: Dict[str, int] = {}

    def _add(mapping: Any) -> None:
        if not isinstance(mapping, dict):
            return
        for reason, count in mapping.items():
            value = as_count(count)
            if value is None:
                continue
            census[str(reason)] = census.get(str(reason), 0) + value

    for run in runs:
        from_ledger = dig(run.artifact(LEDGER_STEM), "loss_reasons")
        if isinstance(from_ledger, dict):
            _add(from_ledger)
            continue
        waterfall = run.artifact("waterfall") or dig(run.artifact("orchestrator_result"), "waterfall") or {}
        for stage in (waterfall.get("stages") or []) if isinstance(waterfall, dict) else []:
            if isinstance(stage, dict):
                _add(stage.get("primary_reasons"))
        result = run.artifact("orchestrator_result")
        _add(dig(result, "enrichment.funnel.qual_reason_counts"))
        _add(dig(result, "enrichment.loss_census"))
        delivery = run.artifact("delivery") or dig(result, "delivery") or {}
        if isinstance(delivery, dict):
            _add(delivery.get("skip_breakdown"))
    return dict(sorted(census.items(), key=lambda kv: (-kv[1], kv[0])))


def disposition_census(runs: Sequence[RunRecord]) -> Dict[str, int]:
    """FINAL_PASS / NEEDS_CHECK / UNVERIFIED / REJECT / REROUTE totals for the window."""
    census: Dict[str, int] = {}
    for run in runs:
        source = run.artifact("waterfall") or dig(run.artifact("orchestrator_result"), "waterfall") or {}
        mapping = source.get("disposition_census") if isinstance(source, dict) else None
        if not isinstance(mapping, dict):
            continue
        for label, count in mapping.items():
            value = as_count(count)
            if value is not None:
                census[str(label)] = census.get(str(label), 0) + value
    return dict(sorted(census.items(), key=lambda kv: (-kv[1], kv[0])))


def acquisition_failures(runs: Sequence[RunRecord]) -> List[Dict[str, Any]]:
    """Lane-level acquisition errors, which are the usual cause of a zero week."""
    failures: List[Dict[str, Any]] = []
    for run in runs:
        lanes = run.artifact("lanes") or dig(run.artifact("orchestrator_result"), "lanes") or {}
        if not isinstance(lanes, dict):
            continue
        for lane, detail in lanes.items():
            if not isinstance(detail, dict):
                continue
            errors = [str(e)[:300] for e in (detail.get("errors") or [])]
            if detail.get("status") == "failed" or errors:
                failures.append(
                    {
                        "run_id": run.run_id,
                        "lane": lane,
                        "status": detail.get("status"),
                        "jobs": detail.get("jobs"),
                        "errors": errors[:3],
                    }
                )
    return failures


def build_run_metrics(runs: Sequence[RunRecord]) -> Dict[str, Metric]:
    """Every run-artifact-derived metric, headline plus supporting."""
    metrics: Dict[str, Metric] = {}
    for spec in RUN_METRIC_SPECS + SUPPORTING_METRIC_SPECS:
        metrics[spec.key] = aggregate(spec, runs)
    metrics["review_rate_pct"] = ratio_metric(
        "review_rate_pct",
        "Review rate",
        metrics["jobs_reviewed"],
        metrics["jobs_captured"],
        definition="Jobs reviewed as a percentage of jobs captured in the same runs.",
    )
    metrics["qualification_rate_pct"] = ratio_metric(
        "qualification_rate_pct",
        "Qualification rate",
        metrics["qualified_opportunities"],
        metrics["jobs_reviewed"],
        definition="Qualified opportunities as a percentage of jobs reviewed.",
    )
    metrics["contact_rate_pct"] = ratio_metric(
        "contact_rate_pct",
        "Contact discovery rate",
        metrics["contacts_found"],
        metrics["qualified_opportunities"],
        definition="Contacts found as a percentage of qualified opportunities.",
    )
    return metrics
