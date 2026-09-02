"""Bottleneck identification and the next-week action plan.

The bottleneck is *measured*, not narrated: it is the funnel boundary that lost
the most records in absolute terms among the stages the report could actually
measure. The stage is then annotated with the loss reason codes the orchestrator
itself recorded at that boundary, so the finding is traceable to artifact fields
rather than to an opinion.

Actions are a fixed, auditable mapping from the identified stage (and the top
observed reason codes) to concrete work. Every action names the evidence that
produced it. Nothing is invented: when the report cannot identify a bottleneck,
it says so, and the plan says what to instrument instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from weekly_report.evidence import Metric

#: The ordered funnel the bottleneck search walks.
FUNNEL_ORDER = (
    "jobs_captured",
    "jobs_reviewed",
    "qualified_opportunities",
    "contacts_found",
    "sent_to_airtable",
    "sent_to_instantly",
)

#: Boundary id -> human name, used for both the JSON and the summary.
BOUNDARY_NAMES = {
    ("jobs_captured", "jobs_reviewed"): "review",
    ("jobs_reviewed", "qualified_opportunities"): "qualification",
    ("qualified_opportunities", "contacts_found"): "contact_discovery",
    ("contacts_found", "sent_to_airtable"): "airtable_delivery",
    ("sent_to_airtable", "sent_to_instantly"): "instantly_delivery",
}

#: Reason codes whose remedy is well understood, mapped to the action to take.
REASON_ACTIONS = {
    "no_search_domain": (
        "Employer domain is unresolved on most postings, so no hiring-manager search "
        "can run. Extend COMPANY_DOMAIN_ALIASES_JSON for the recognisable employers in "
        "reports/domain_alias_candidates_*.csv, or shift acquisition toward sources "
        "that expose a first-party domain."
    ),
    "hiring_manager_not_found": (
        "Apollo returned no hiring manager for the company x role bucket. Review the "
        "alternate-hiring-manager recovery path before spending more acquisition budget."
    ),
    "email_unverified": (
        "Contacts resolve but their email never reaches verified status. Only Apollo "
        "can promote an email to verified, so check Apollo credit state before assuming "
        "a coverage problem."
    ),
    "apollo_email_not_verified": (
        "Rows are blocked on Apollo verification. Confirm Apollo credit availability "
        "for the week before attributing this to data coverage."
    ),
    "not_icp": (
        "Most postings are rejected as out-of-ICP. Re-check the acquisition query mix: "
        "budget is being spent acquiring jobs the ICP filter will always reject."
    ),
    "company_unresolved": (
        "Company identity could not be resolved from the posting. This is a source-quality "
        "problem; prefer feeds that carry the employer over aggregator feeds."
    ),
    "company_function_suppressed": (
        "Deliverable leads were suppressed because the company+function already exists in "
        "Airtable. Confirm this is the intended dedupe policy for the week."
    ),
    "account_suppressed": (
        "Account-level suppression removed deliverable leads. Verify "
        "AIRTABLE_SUPPRESS_ACCOUNT_LEVEL is deliberately on."
    ),
    "not_final_pass": (
        "Leads reached delivery but were not FINAL_PASS, so they could not auto-approve."
    ),
    "already_delivered": (
        "Leads were skipped as already delivered; this is idempotency working, not loss."
    ),
}

#: Stage -> the action to take when that boundary is the largest loss.
STAGE_ACTIONS = {
    "review": (
        "Runs captured more postings than they reviewed. Check each run's stop_reason and "
        "topup.final_stop_reason: the pipeline is stopping before it finishes the batch it "
        "paid for."
    ),
    "qualification": (
        "The qualification gate is the largest loss. Re-tune acquisition targeting so budget "
        "is not spent on postings the role/ICP filters reject."
    ),
    "contact_discovery": (
        "Contact discovery is the largest loss. This is the hiring-manager/domain layer, not "
        "acquisition volume - adding jobs will not move the output."
    ),
    "airtable_delivery": (
        "Contacts are found but not written to Airtable. Review the delivery skip breakdown "
        "before adding acquisition volume."
    ),
    "instantly_delivery": (
        "Rows reach Airtable but do not reach Instantly. This is the Approved -> Enrolled "
        "boundary; inspect approved-row eligibility rather than the acquisition pipeline."
    ),
}


@dataclass
class Bottleneck:
    """The measured largest loss in the funnel."""

    kind: str
    boundary: str = ""
    from_metric: str = ""
    to_metric: str = ""
    entered: Optional[int] = None
    advanced: Optional[int] = None
    lost: Optional[int] = None
    loss_pct: Optional[float] = None
    top_reasons: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    statement: str = ""
    unmeasured_boundaries: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "boundary": self.boundary,
            "from_metric": self.from_metric,
            "to_metric": self.to_metric,
            "entered": self.entered,
            "advanced": self.advanced,
            "lost": self.lost,
            "loss_pct": self.loss_pct,
            "top_reasons": list(self.top_reasons),
            "evidence": list(self.evidence),
            "statement": self.statement,
            "unmeasured_boundaries": list(self.unmeasured_boundaries),
        }


@dataclass
class Action:
    """One next-week action, with the evidence that produced it."""

    priority: int
    action: str
    basis: str

    def to_dict(self) -> Dict[str, Any]:
        return {"priority": self.priority, "action": self.action, "basis": self.basis}


def identify(
    metrics: Dict[str, Metric],
    *,
    run_count: int,
    reasons: Dict[str, int],
    acquisition_errors: Sequence[Dict[str, Any]] = (),
) -> Bottleneck:
    """Find the largest measured loss, or explain why none could be measured."""
    if run_count == 0:
        return Bottleneck(
            kind="no_pipeline_activity",
            statement=(
                "No pipeline run was attributed to this window, so the pipeline produced "
                "nothing to measure. The bottleneck is execution, not any funnel stage."
            ),
            evidence=["run_artifacts: 0 runs in window"],
        )

    if acquisition_errors:
        lanes = sorted({str(e.get("lane")) for e in acquisition_errors})
        return Bottleneck(
            kind="acquisition_failure",
            boundary="acquisition",
            statement=(
                f"Acquisition lane(s) {', '.join(lanes)} reported errors, so downstream "
                "counts understate capability. Fix acquisition before reading the funnel."
            ),
            evidence=["lanes.json:errors"],
            top_reasons=[{"reason": str(e.get("lane")), "detail": (e.get("errors") or [""])[0]} for e in acquisition_errors[:3]],
        )

    available = [(key, metrics[key]) for key in FUNNEL_ORDER if key in metrics and metrics[key].available]
    unmeasured = [key for key in FUNNEL_ORDER if key in metrics and not metrics[key].available]

    if len(available) < 2:
        return Bottleneck(
            kind="insufficient_measurement",
            statement=(
                "Fewer than two consecutive funnel stages could be measured for this "
                "window, so no boundary loss can be compared."
            ),
            evidence=[f"available stages: {', '.join(k for k, _ in available) or 'none'}"],
            unmeasured_boundaries=unmeasured,
        )

    worst: Optional[Bottleneck] = None
    for (from_key, from_metric), (to_key, to_metric) in zip(available, available[1:]):
        entered = int(from_metric.value or 0)
        advanced = int(to_metric.value or 0)
        lost = entered - advanced
        if lost <= 0:
            continue
        candidate = Bottleneck(
            kind="funnel_boundary",
            boundary=BOUNDARY_NAMES.get((from_key, to_key), f"{from_key}->{to_key}"),
            from_metric=from_key,
            to_metric=to_key,
            entered=entered,
            advanced=advanced,
            lost=lost,
            loss_pct=round(100.0 * lost / entered, 1) if entered else None,
            evidence=sorted(set(from_metric.evidence) | set(to_metric.evidence)),
            unmeasured_boundaries=unmeasured,
        )
        if worst is None or (candidate.lost or 0) > (worst.lost or 0):
            worst = candidate

    if worst is None:
        return Bottleneck(
            kind="no_measured_loss",
            statement=(
                "No measured funnel boundary lost records this window. Either throughput "
                "was clean end to end, or the stages that lose records are the unmeasured ones."
            ),
            unmeasured_boundaries=unmeasured,
        )

    worst.top_reasons = [
        {"reason": reason, "count": count} for reason, count in list(reasons.items())[:5]
    ]
    worst.statement = (
        f"The {worst.boundary.replace('_', ' ')} boundary lost {worst.lost} of {worst.entered} "
        f"records ({worst.loss_pct}%), the largest measured drop in the funnel."
    )
    return worst


def action_plan(
    bottleneck: Bottleneck,
    metrics: Dict[str, Metric],
    *,
    gaps: Sequence[Any] = (),
    max_actions: int = 5,
) -> List[Action]:
    """A short, evidence-anchored plan. Deterministic for a given report."""
    actions: List[Action] = []

    def add(text: str, basis: str) -> None:
        if len(actions) < max_actions and not any(a.action == text for a in actions):
            actions.append(Action(priority=len(actions) + 1, action=text, basis=basis))

    if bottleneck.kind == "no_pipeline_activity":
        add(
            "Confirm the GTM cron actually executed every day this week; a week with zero "
            "runs is a scheduling or start-command failure, not a yield problem.",
            "run_artifacts: 0 runs attributed to the window",
        )
    elif bottleneck.kind == "acquisition_failure":
        add(
            "Fix the failing acquisition lane before drawing any conclusion from the rest "
            "of the funnel.",
            "lanes.json reported lane errors",
        )
    elif bottleneck.kind == "funnel_boundary":
        stage_action = STAGE_ACTIONS.get(bottleneck.boundary)
        if stage_action:
            add(
                stage_action,
                f"largest measured loss: {bottleneck.lost} records at the "
                f"{bottleneck.boundary} boundary ({bottleneck.loss_pct}%)",
            )
        for entry in bottleneck.top_reasons:
            remedy = REASON_ACTIONS.get(str(entry.get("reason")))
            if remedy:
                add(remedy, f"reason code {entry.get('reason')} = {entry.get('count')} this window")
    elif bottleneck.kind == "insufficient_measurement":
        add(
            "Instrument the missing funnel stages before the next report; the week cannot "
            "currently be diagnosed from the artifacts that exist.",
            "fewer than two consecutive stages measurable",
        )

    for gap in gaps:
        remedy = getattr(gap, "remedy", None)
        metric = getattr(gap, "metric", "")
        if remedy:
            add(remedy, f"evidence gap on {metric}")

    if not actions:
        add(
            "Hold the current configuration and re-measure next week; no boundary lost "
            "enough records this week to justify a change.",
            "no measured loss at any funnel boundary",
        )
    return actions
