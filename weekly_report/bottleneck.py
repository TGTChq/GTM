"""Bottleneck identification and the next-week action plan.

The bottleneck is *measured*, not narrated: it is the funnel boundary that lost
the most records in absolute terms among the stages the report could actually
measure **and legitimately compare**. Two counters summed over different run sets
are not subtracted -- that boundary is declared incomparable instead. The stage is then annotated with the loss reason codes the orchestrator
itself recorded at that boundary, so the finding is traceable to artifact fields
rather than to an opinion.

Actions are a fixed, auditable mapping from the identified stage (and the top
observed reason codes) to concrete work. Every action names the evidence that
produced it. Nothing is invented: when the report cannot identify a bottleneck,
it says so, and the plan says what to instrument instead.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from weekly_report.evidence import Metric, STATUS_MEASURED, STATUS_PARTIAL
from weekly_report.metrics import SOURCE_RUN_ARTIFACTS

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

#: Which recorded reason codes may explain WHICH boundary.
#:
#: ``reason_census`` merges every reason the run recorded, at every stage. Handing
#: that whole census to one boundary is how a delivery drop got explained by
#: ``email_unverified`` and ``not_icp`` -- both decided at qualification, hundreds
#: of records upstream, about a different population. A reason that cannot be
#: recorded AT a boundary cannot be evidence about it.
#:
#: A boundary with no admissible reason present says so, rather than borrowing.
REASONS_BY_BOUNDARY = {
    "review": frozenset({
        "previously_seen", "duplicate_in_run", "missing_job_id",
    }),
    "contact_discovery": frozenset({
        "hiring_manager_not_found", "contact_not_found", "no_search_domain",
        "zero_apollo_people", "company_unresolved", "not_icp",
        "company_size_rejected", "in_crm",
    }),
    "airtable_delivery": frozenset({
        "skipped_existing", "updated_existing", "company_function_suppressed",
        "account_suppressed", "no_contact", "send_safe_withheld", "other",
        "already_delivered", "adapter_error", "person_employer_duplicate",
    }),
}

#: Boundaries whose ADVANCED side is a strict subset of the ENTERED side, so the
#: difference is a set of records that entered and did not advance.
#:
#: ``airtable_delivery`` is deliberately absent. Every created row passes
#: ``send_safe_facts``, which requires a non-empty verified email, so created rows
#: ARE a subset of contacts-with-email -- but the population handed to the writer
#: is larger than that subset in the other direction: on 2026-09-04 the writer
#: received 1,681 candidates against 1,048 contacts, because leads with no contact
#: are still submitted for review. The difference between contacts and created is
#: therefore a mixture of never-submitted, suppressed-as-existing, updated and
#: withheld records, and only the delivery skip breakdown separates them.
SUBSET_BOUNDARIES = frozenset({"review", "contact_discovery"})

#: Metric pairs whose difference is NOT a set of records that failed, so the pair
#: may not be named as the bottleneck on size alone. Keyed on the metrics rather
#: than the boundary label, which collapses when an intervening stage is unmeasured.
#:
#: ``contacts_found -> sent_to_airtable`` is the one that matters. Created rows are
#: not a subset of contacts: on 2026-09-04 the writer received 1,681 candidates
#: against 1,048 contacts, because leads with NO contact are still submitted for
#: review, and some of those become rows. So 1,048 - 781 is not "267 contacts lost";
#: it is a difference between two populations that are not nested in either
#: direction. Ranking it by size made it the headline of a report where the entry
#: population had not even been established.
#:
#: ``qualified_opportunities -> sent_to_airtable`` is deliberately NOT here: rows
#: created are nested under opportunities that entered contact discovery, so that
#: collapsed pair remains a real, if multi-stage, loss.
#:
#: A pair listed here is still measured, still reported in the document, and still
#: eligible to be the headline the moment a reason code recorded at that boundary
#: makes the difference attributable.
NON_NESTED_PAIRS = frozenset({("contacts_found", "sent_to_airtable")})

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
        "not_icp is a leading recorded reason. Re-check the acquisition query mix: budget "
        "is being spent acquiring postings the ICP filter rejects."
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
#: Stop reasons that mean the run never reached the provider at all, mapped to the
#: work that clears them. Keyed on the substring the orchestrator actually writes.
ACQUISITION_STOP_ACTIONS = {
    "governor_zero_budget": (
        "The credit governor granted zero budget, so no provider request was made. Check "
        "the persisted Fantastic quota snapshot against the provider's own "
        "x-api-jobs-remaining: a stale snapshot below "
        "FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING blocks every run until it is refreshed."
    ),
    "acquisition_failed": (
        "An acquisition lane errored. Fix the lane before reading any downstream counter."
    ),
}

#: Said of a run that DID reach the provider with budget and still captured nothing.
ACQUISITION_REACHED_PROVIDER_ACTION = (
    "Acquisition had budget and reached the provider but captured no jobs. Check the "
    "date_created watermark: a window left in flight and older than "
    "FANTASTIC_JOBS_TIME_FRAME can only return empty pages, because the provider "
    "intersects the window with the time frame."
)

STAGE_ACTIONS = {
    "acquisition": (
        "Acquisition captured nothing this window, so no funnel stage had input. Fix "
        "acquisition entry before reading or acting on any downstream number."
    ),
    "review": (
        "Runs captured more postings than they reviewed. Check each run's stop_reason and "
        "topup.final_stop_reason to find out why the run did not finish the batch it "
        "acquired."
    ),
    "qualification": (
        "The qualification gate is the largest measured loss. Review acquisition targeting: "
        "a large share of the postings acquired this window were rejected by the role/ICP "
        "filters."
    ),
    "contact_discovery": (
        "Contact discovery is the largest measured loss. The constraint is the "
        "hiring-manager/domain layer rather than acquisition volume, so adding jobs is "
        "unlikely to increase output until this boundary improves."
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
    incomparable_boundaries: List[Dict[str, str]] = field(default_factory=list)

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
            "incomparable_boundaries": list(self.incomparable_boundaries),
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
    runs: Sequence[Any] = (),
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

    # The period's entry total is not established: some runs reported net-new and
    # some did not. Every downstream comparison inherits that, so name it rather
    # than reporting the largest drop among a population of unknown size.
    entry = metrics.get("jobs_captured")
    if entry is not None and entry.status == STATUS_PARTIAL:
        silent = list(entry.runs_missing_field)
        return Bottleneck(
            kind="entry_not_established",
            boundary="acquisition",
            statement=(
                f"{len(silent)} of {len(silent) + len(entry.contributing_run_ids)} "
                "runs in this period did not record net-new captured postings, so the "
                "period's entry total is not established and no downstream rate can "
                "be read against it. The runs that did record it are in the report "
                "document."
            ),
            evidence=sorted(entry.evidence),
            unmeasured_boundaries=unmeasured,
        )

    # ZERO CAPTURE IS NOT "NO LOSS". The boundary search below skips every boundary
    # where lost <= 0, so a window that captured nothing produced no loss anywhere
    # and used to fall through to "no funnel boundary could be shown to lose
    # records" -- i.e. a total acquisition outage was reported as clean throughput.
    # Acquisition entry IS the bottleneck when nothing was captured, and the run
    # stop reasons say which kind of failure it was.
    captured = metrics.get("jobs_captured")
    # MEASURED, not merely available. A PARTIAL zero is a sum over the runs that
    # reported the counter, and says nothing about the ones that did not -- on
    # 2026-09-04/05 that would have announced "acquisition captured 0 jobs across
    # 2 runs" when only one of the two had reported at all, and the silent one had
    # bought 6,205 provider rows. A total outage and an unmeasured period are
    # different findings with different fixes.
    if (captured is not None and captured.status == STATUS_MEASURED
            and int(captured.value or 0) == 0):
        stops = collections.Counter(
            str(getattr(run, "stop_reason", "") or "unrecorded") for run in (runs or ()))
        zero_budget = sum(n for reason, n in stops.items() if "governor_zero_budget" in reason)
        reached = max(0, run_count - zero_budget)
        parts = [f"Acquisition captured 0 jobs across {run_count} "
                 f"run{'s' if run_count != 1 else ''}, so no funnel stage had input."]
        if zero_budget:
            parts.append(f"The credit governor granted zero budget on {zero_budget} of them "
                         f"(stop_reason governor_zero_budget), so no provider request was made.")
        if reached:
            parts.append(f"{reached} run{'s' if reached != 1 else ''} had budget and reached "
                         f"the provider but still captured nothing.")
        if not stops:
            parts.append("No run stop reason was recorded, so the failure mode cannot be "
                         "narrowed from the artifacts in this window.")
        return Bottleneck(
            kind="acquisition_entry",
            boundary="acquisition",
            entered=0,
            advanced=0,
            lost=0,
            statement=" ".join(parts),
            evidence=sorted(set(captured.evidence)) + ["run_manifest.stop_reason"],
            top_reasons=[{"reason": reason, "count": count}
                         for reason, count in stops.most_common(5)],
            unmeasured_boundaries=unmeasured,
        )

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
    #: Every boundary that showed a positive difference, eligible or not. The
    #: document keeps all of them; only an eligible one may be the headline.
    observed: List[Bottleneck] = []
    incomparable: List[Dict[str, str]] = []
    for (from_key, from_metric), (to_key, to_metric) in zip(available, available[1:]):
        boundary_name = BOUNDARY_NAMES.get((from_key, to_key), f"{from_key}->{to_key}")
        # UNIT. The two sides must count the SAME KIND OF THING. They frequently do
        # not: acquisition counts postings, the hiring-manager stage counts
        # company x role-bucket opportunities. On 2026-09-04 that was 6,205
        # postings and 2,410 opportunities, and subtracting them yields a
        # 61% "review loss" that nobody lost -- the second number is not a subset
        # of the first, it is a different population.
        #
        # A metric with no declared unit is treated as incomparable rather than
        # assumed compatible: an undeclared unit is exactly the state that produced
        # the wrong number in the first place.
        from_unit = getattr(from_metric, "counted_unit", "") or ""
        to_unit = getattr(to_metric, "counted_unit", "") or ""
        if from_unit != to_unit or not from_unit:
            incomparable.append({
                "boundary": boundary_name,
                "reason": (
                    f"{from_key} counts {from_unit or 'an undeclared unit'} and "
                    f"{to_key} counts {to_unit or 'an undeclared unit'}; one is not a "
                    "subset of the other, so their difference is not a loss"
                ),
            })
            continue
        # COHORT. Same unit is not enough. ``sent_to_instantly`` is observed at the
        # provider and drawn from the Airtable Approved backlog, which accumulates
        # across windows -- the 2026-09-05 sync delivered 770 leads from 781
        # Approved rows built up over the preceding fortnight. Subtracting that
        # from this window's contacts compares two unrelated cohorts.
        from_cohort = getattr(from_metric, "cohort", "") or ""
        to_cohort = getattr(to_metric, "cohort", "") or ""
        if from_cohort != to_cohort:
            incomparable.append({
                "boundary": boundary_name,
                "reason": (
                    f"{from_key} is the {from_cohort or 'undeclared'} cohort and "
                    f"{to_key} is the {to_cohort or 'undeclared'} cohort; they are "
                    "different populations measured over the same dates, not two "
                    "points on one funnel"
                ),
            })
            continue
        # Two run-derived counters may have been summed over DIFFERENT run sets when
        # one of them is partial. Subtracting those is arithmetic on incomparable
        # populations, so the boundary is declared rather than measured.
        if from_metric.source == to_metric.source == SOURCE_RUN_ARTIFACTS:
            from_runs = set(from_metric.contributing_run_ids)
            to_runs = set(to_metric.contributing_run_ids)
            if from_runs != to_runs:
                only_from = sorted(from_runs - to_runs)
                only_to = sorted(to_runs - from_runs)
                incomparable.append(
                    {
                        "boundary": boundary_name,
                        "reason": (
                            f"{from_key} and {to_key} were summed over different run sets "
                            f"(only in {from_key}: {', '.join(only_from) or 'none'}; "
                            f"only in {to_key}: {', '.join(only_to) or 'none'}), so their "
                            "difference is not a loss"
                        ),
                    }
                )
                continue
        entered = int(from_metric.value or 0)
        advanced = int(to_metric.value or 0)
        lost = entered - advanced
        if lost <= 0:
            continue
        candidate = Bottleneck(
            kind="funnel_boundary",
            boundary=boundary_name,
            from_metric=from_key,
            to_metric=to_key,
            entered=entered,
            advanced=advanced,
            lost=lost,
            loss_pct=round(100.0 * lost / entered, 1) if entered else None,
            evidence=sorted(set(from_metric.evidence) | set(to_metric.evidence)),
            unmeasured_boundaries=unmeasured,
        )
        # Only reasons that can be RECORDED at this boundary may explain it.
        admissible = REASONS_BY_BOUNDARY.get(boundary_name)
        scoped = {r: c for r, c in reasons.items()
                  if admissible is None or r in admissible}
        candidate.top_reasons = [
            {"reason": reason, "count": count} for reason, count in list(scoped.items())[:5]
        ]
        observed.append(candidate)

    # ELIGIBILITY. A boundary may be named as THE bottleneck only if its difference
    # is actionable: either the two counters are nested, so the difference is a set
    # of records that entered and did not advance, or a reason code recorded AT that
    # boundary attributes it.
    #
    # Checked on the METRIC PAIR, not the boundary label, because a label collapses
    # when the stage between two counters is unmeasured -- and the pair is what
    # decides whether the subtraction means anything.
    eligible = [c for c in observed
                if (c.from_metric, c.to_metric) not in NON_NESTED_PAIRS
                or c.top_reasons]
    for candidate in eligible:
        if worst is None or (candidate.lost or 0) > (worst.lost or 0):
            worst = candidate

    if worst is None:
        ineligible = sorted({c.boundary for c in observed})
        if ineligible:
            return Bottleneck(
                kind="no_attributable_boundary",
                statement=(
                    "No boundary this window can be named as the bottleneck. "
                    + ", ".join(b.replace("_", " ") for b in ineligible)
                    + (" shows a difference, but its two counters are not a proven "
                       "subset and no reason code was recorded there, so the "
                       "difference is a transition and not a measured loss.")
                ),
                unmeasured_boundaries=unmeasured,
                incomparable_boundaries=incomparable,
            )
        return Bottleneck(
            kind="no_measured_loss",
            statement=(
                "No funnel boundary could be shown to lose records this window. Either "
                "throughput was clean end to end, or the stages that lose records are the "
                "ones this window could not measure."
            ),
            unmeasured_boundaries=unmeasured,
            incomparable_boundaries=incomparable,
        )

    worst.incomparable_boundaries = incomparable
    compared = len(available) - 1 - len(incomparable)
    plural = "y" if compared == 1 else "ies"
    if worst.boundary in SUBSET_BOUNDARIES:
        worst.statement = (
            f"The {worst.boundary.replace('_', ' ')} boundary lost {worst.lost} of "
            f"{worst.entered} records ({worst.loss_pct}%), the largest drop among the "
            f"{compared} funnel boundar{plural} this window could compare."
        )
    else:
        # NOT a subset boundary: state the transition that was observed, and stop.
        # "Lost N" asserts that N records entered and failed, which this boundary
        # does not establish -- a record suppressed because it already exists in
        # Airtable did not fail delivery, and one never submitted did not reach it.
        worst.statement = (
            f"{worst.advanced} of {worst.entered} "
            f"{worst.from_metric.replace('_', ' ')} became {worst.to_metric.replace('_', ' ')} "
            f"({100 - (worst.loss_pct or 0):.1f}%), the largest observed drop among the "
            f"{compared} boundar{plural} this window could compare. The remaining "
            f"{worst.lost} did not all fail: suppressed-as-existing, updated, "
            "withheld as not send-safe and never-submitted records are all in that "
            "difference, and only the delivery skip breakdown separates them."
        )
    if not worst.top_reasons:
        worst.statement += (
            " No reason code recorded at this boundary is present, so the size of "
            "the transition is measured and its cause is not attributable."
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
            "Confirm the GTM cron actually executed this week and that the report can read "
            "the artifact root; a week with zero runs is a scheduling, start-command or "
            "artifact-access problem, not a yield problem.",
            "run_artifacts: 0 runs attributed to the window",
        )
    elif bottleneck.kind == "acquisition_failure":
        add(
            "Fix the failing acquisition lane before drawing any conclusion from the rest "
            "of the funnel.",
            "lanes.json reported lane errors",
        )
    elif bottleneck.kind == "funnel_boundary":
        basis = (f"largest measured loss: {bottleneck.lost} records at the "
                 f"{bottleneck.boundary} boundary ({bottleneck.loss_pct}%)")
        stage_action = STAGE_ACTIONS.get(bottleneck.boundary)
        if stage_action:
            add(stage_action, basis)
        else:
            # A COLLAPSED boundary: the stages between these two could not be
            # measured, so the pair is not adjacent in the canonical funnel and
            # has no single owning stage. Dropping the stage-level action here
            # (as this did) left the plan with reason codes only -- and with none
            # of those, with nothing at all. Name the span instead of skipping it.
            add(
                "This loss spans more than one stage, because the funnel stages "
                "between the two measured ends were not recorded this window. "
                "Instrument the intermediate stages before tuning any single one; "
                "until then the loss cannot be attributed to a specific boundary.",
                basis,
            )
        for entry in bottleneck.top_reasons:
            remedy = REASON_ACTIONS.get(str(entry.get("reason")))
            if remedy:
                add(remedy, f"reason code {entry.get('reason')} = {entry.get('count')} this window")
        if not bottleneck.top_reasons:
            add(
                "No loss reason codes survived for this window, so the size of the "
                "drop is known but its root cause is not attributable. Check that the "
                "runs in this window carry a reporting-ledger loss_reasons block.",
                f"{bottleneck.lost} records lost at {bottleneck.boundary} with no "
                "recorded reason codes",
            )
    elif bottleneck.kind == "acquisition_entry":
        add(
            STAGE_ACTIONS["acquisition"],
            f"jobs_captured = 0 across {sum(int(e.get('count') or 0) for e in bottleneck.top_reasons)} "
            "run(s) in this window",
        )
        reached_provider = True
        for entry in bottleneck.top_reasons:
            reason = str(entry.get("reason") or "")
            remedy = next((text for key, text in ACQUISITION_STOP_ACTIONS.items()
                           if key in reason), "")
            if remedy:
                reached_provider = False
                add(remedy, f"stop_reason {reason} on {entry.get('count')} run(s)")
        if reached_provider and bottleneck.top_reasons:
            add(ACQUISITION_REACHED_PROVIDER_ACTION,
                "runs had budget and reached the provider yet captured 0 jobs")
    elif bottleneck.kind == "entry_not_established":
        add(
            "Some runs in this period did not record net-new captured postings, so "
            "the period total cannot be stated. Confirm every run in the window is "
            "on a build that emits it before reading this report's rates.",
            bottleneck.statement,
        )
    elif bottleneck.kind == "insufficient_measurement":
        add(
            "Instrument the missing funnel stages before the next report; the week cannot "
            "currently be diagnosed from the artifacts that exist.",
            "fewer than two consecutive stages measurable",
        )

    # Evidence-gap chores are appended ONLY when no concrete production problem was
    # identified. Telling Brett to "instrument sent_to_instantly" while acquisition
    # is down buries the finding that matters under housekeeping.
    if bottleneck.kind not in ("acquisition_entry", "acquisition_failure", "funnel_boundary"):
        for gap in gaps:
            remedy = getattr(gap, "remedy", None)
            metric = getattr(gap, "metric", "")
            if remedy:
                add(remedy, f"evidence gap on {metric}")

    if not actions:
        # This text may ONLY be used when nothing was measured to be lost. Emitting
        # it under a bottleneck that reports a positive loss produced a report whose
        # two halves contradicted each other -- "lost 5157 of 6205 (83.1%)" directly
        # above "no measured loss at any funnel boundary".
        if (bottleneck.lost or 0) > 0:
            add(
                "The largest measured drop is known but its cause is not "
                "attributable from this window's evidence; treat the number as a "
                "measurement, not yet as a diagnosis.",
                f"{bottleneck.lost} records lost at the {bottleneck.boundary} boundary "
                f"({bottleneck.loss_pct}%), with no reason codes or stage mapping available",
            )
        else:
            add(
                "Hold the current configuration and re-measure next week; no boundary lost "
                "enough records this week to justify a change.",
                "no measured loss at any funnel boundary",
            )
    return actions
