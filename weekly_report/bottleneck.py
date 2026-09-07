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
        "delivery_unreconciled",
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
#: Both counters remain measured, but a reason code cannot prove a subset
#: relationship. Report recorded delivery outcomes separately from conversion.
NON_NESTED_PAIRS = frozenset({("contacts_found", "sent_to_airtable")})

#: Reason codes whose remedy is well understood, mapped to the action to take.
REASON_ACTIONS = {
    "no_search_domain": (
        "Resolve the employer identities on affected postings before retrying contact discovery."
    ),
    "hiring_manager_not_found": (
        "Apollo returned no hiring manager for the company x role bucket. Review the "
        "alternate-hiring-manager recovery path before spending more acquisition budget."
    ),
    "email_unverified": (
        "Separate missing contacts, unverified addresses and identity conflicts before choosing a recovery action."
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
        "Check the affected employer records and resolve their identity conflicts before retrying."
    ),
    "company_function_suppressed": (
        "Deliverable leads were suppressed because the company+function already exists in "
        "Airtable. Confirm this is the intended dedupe policy for the week."
    ),
    "account_suppressed": (
        "Confirm the intended policy for employers already represented in Airtable."
    ),
    "not_final_pass": (
        "Check the missing approval facts on withheld leads and recover any that meet the sending requirements."
    ),
    "already_delivered": (
        "Leads were skipped as already delivered; this is idempotency working, not loss."
    ),
    "delivery_unreconciled": (
        "Reconcile the rows delivery could not account for against Airtable and recover any failed writes."
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
        "Check acquisition entry and the recovered-work queue to explain why no new postings were captured."
    ),
    "review": (
        "Resolve the interruptions that left newly acquired postings unreviewed, then resume them."
    ),
    "qualification": (
        "The qualification gate is the largest measured loss. Review acquisition targeting: "
        "a large share of the postings acquired this window were rejected by the role/ICP "
        "filters."
    ),
    "contact_discovery": (
        "Reconcile incomplete contact searches and recorded no-matches, then retry eligible unfinished work."
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
    stakeholder_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"priority": self.priority, "action": self.action, "basis": self.basis,
                "stakeholder_action": self.stakeholder_action or self.action}


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
            and int(captured.value or 0) == 0
            and captured.cohort != "new_capture_excluding_recovered_work"
            and not any(m.available and (m.value or 0) > 0 for key, m in available
                        if key != "jobs_captured")):
        stops = collections.Counter(
            str(getattr(run, "stop_reason", "") or "unrecorded") for run in (runs or ()))
        zero_budget = sum(n for reason, n in stops.items() if "governor_zero_budget" in reason)
        # A non-budget stop label does not prove that a provider call happened.
        # Only emitted acquisition request counters support that claim.
        request_counts = []
        for run in runs:
            ledger = run.artifact("ledger")
            result = run.artifact("orchestrator_result")
            count = ledger.get("physical_requests")
            if count is None:
                count = ((result.get("acquisition") or {}).get("cumulative") or {}).get("physical_requests")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                request_counts.append(count)
        reached = sum(count > 0 for count in request_counts)
        parts = [f"Acquisition captured 0 jobs across {run_count} "
                 f"run{'s' if run_count != 1 else ''}."]
        if zero_budget:
            parts.append(f"{zero_budget} recorded acquisition budget stops.")
        if reached:
            parts.append(f"{reached} run{'s' if reached != 1 else ''} recorded acquisition requests.")
        if len(request_counts) < run_count:
            parts.append("Provider request activity is not measured for every run.")
        if not stops or set(stops) == {"unrecorded"}:
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
            top_reasons=([{"reason": reason, "count": count}
                          for reason, count in stops.most_common(5)]
                         + ([{"reason": "acquisition_requests_observed", "count": reached}] if reached else [])),
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
    delivery_finding: Optional[Bottleneck] = None
    unattributed_delivery: set = set()
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
        if (from_key, to_key) in NON_NESTED_PAIRS:
            incomparable.append({"boundary": boundary_name, "reason": (
                "Contacts found and Airtable creations are not proven subsets; "
                "recorded skip reasons do not make their difference a measured loss.")})
            scoped = {r: c for r, c in reasons.items()
                      if r in REASONS_BY_BOUNDARY.get(boundary_name, ()) and int(c or 0) > 0}
            if scoped:
                top = sorted(scoped.items(), key=lambda item: (-int(item[1]), item[0]))[:5]
                if "delivery_unreconciled" in scoped:
                    statement = (f"Delivery could not account for {int(scoped['delivery_unreconciled']):,} "
                                 "submitted rows. Reconcile their outcomes against Airtable.")
                else:
                    reason, count = top[0]
                    statement = f"Delivery recorded {int(count):,} outcomes attributed to {reason.replace('_', ' ')}."
                delivery_finding = Bottleneck(
                    kind="delivery_outcomes", boundary=boundary_name,
                    from_metric=from_key, to_metric=to_key,
                    top_reasons=[{"reason": r, "count": c} for r, c in top],
                    statement=statement + " Contacts found and Airtable creations do not establish a conversion rate.",
                    evidence=sorted(set(from_metric.evidence) | set(to_metric.evidence)),
                    unmeasured_boundaries=unmeasured)
            elif entered > advanced:
                unattributed_delivery.add(boundary_name)
            continue
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
        # ...and only reasons that actually FIRED. A zero is not an explanation, and
        # this set is also the eligibility gate below for a non-nested pair, so an
        # all-zero set would let a boundary carry a headline nothing attributes.
        scoped = {r: c for r, c in reasons.items()
                  if (admissible is None or r in admissible) and int(c or 0) > 0}
        candidate.top_reasons = [
            {"reason": reason, "count": count} for reason, count in list(scoped.items())[:5]
        ]
        observed.append(candidate)

    # A reason code cannot make incompatible populations comparable. Those pairs
    # were kept out of the subtraction above; recorded outcomes remain separate.
    #
    # Checked on the METRIC PAIR, not the boundary label, because a label collapses
    # when the stage between two counters is unmeasured -- and the pair is what
    # decides whether the subtraction means anything.
    eligible = [c for c in observed
                if (c.from_metric, c.to_metric) not in NON_NESTED_PAIRS]
    for candidate in eligible:
        if worst is None or (candidate.lost or 0) > (worst.lost or 0):
            worst = candidate

    if delivery_finding is not None and (worst is None or int(reasons.get("delivery_unreconciled") or 0) > 0):
        delivery_finding.incomparable_boundaries = incomparable
        return delivery_finding

    if worst is None:
        ineligible = sorted({c.boundary for c in observed} | unattributed_delivery)
        if ineligible:
            return Bottleneck(
                kind="no_attributable_boundary",
                statement=(
                    "No boundary this window can be named as the bottleneck. "
                    + ", ".join(b.replace("_", " ") for b in ineligible)
                    + (" cannot be compared using the recorded units, populations "
                       "and run coverage. No loss rate can be established at those boundaries.")
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


#: Metrics derived from other metrics, and the inputs they are derived from. A rate
#: is incomplete exactly when its inputs are, so it carries no separate cause.
_DERIVED_INPUTS = {
    "review_rate_pct": ("jobs_captured", "jobs_reviewed"),
    "qualification_rate_pct": ("qualified_opportunities", "jobs_reviewed"),
}


def _silent_runs(metrics: Dict[str, Metric], bottleneck: Bottleneck) -> frozenset:
    """The runs whose silence the bottleneck is already reporting."""
    if bottleneck.kind not in ("entry_not_established", "insufficient_measurement"):
        return frozenset()
    entry = metrics.get("jobs_captured")
    return frozenset(entry.runs_missing_field) if entry is not None else frozenset()


def _shares_cause(key: str, metrics: Dict[str, Metric], silent: frozenset) -> bool:
    """True when this metric is incomplete for the reason already being reported."""
    inputs = _DERIVED_INPUTS.get(key)
    if inputs:
        return all(_shares_cause(name, metrics, silent) for name in inputs)
    metric = metrics.get(key)
    if metric is None:
        return False
    return bool(metric.runs_missing_field) and frozenset(metric.runs_missing_field) == silent


def action_plan(
    bottleneck: Bottleneck,
    metrics: Dict[str, Metric],
    *,
    gaps: Sequence[Any] = (),
    max_actions: int = 5,
) -> List[Action]:
    """A short, evidence-anchored plan. Deterministic for a given report."""
    actions: List[Action] = []

    def add(text: str, basis: str, stakeholder_text: str = "") -> None:
        if len(actions) < max_actions and not any(a.action == text for a in actions):
            actions.append(Action(priority=len(actions) + 1, action=text, basis=basis,
                                  stakeholder_action=stakeholder_text))

    if bottleneck.kind == "no_pipeline_activity":
        add(
            "Confirm the GTM cron actually executed this week and that the report can read "
            "the artifact root; a week with zero runs is a scheduling, start-command or "
            "artifact-access problem, not a yield problem.",
            "run_artifacts: 0 runs attributed to the window",
            "Confirm scheduled runs completed and that their results are accessible.",
        )
    elif bottleneck.kind == "acquisition_failure":
        add(
            "Fix the failing acquisition lane before drawing any conclusion from the rest "
            "of the funnel.",
            "lanes.json reported lane errors",
        )
    elif bottleneck.kind == "delivery_outcomes":
        add(STAGE_ACTIONS["airtable_delivery"], "recorded delivery outcomes; no comparable contact-to-creation rate")
        for entry in bottleneck.top_reasons:
            remedy = REASON_ACTIONS.get(str(entry.get("reason")))
            if remedy:
                add(remedy, f"recorded delivery reason {entry.get('reason')} = {entry.get('count')}")
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
                "Recover the missing outcome reasons before attributing the measured drop to a cause.",
            )
    elif bottleneck.kind == "acquisition_entry":
        add(
            STAGE_ACTIONS["acquisition"],
            f"jobs_captured = 0 across {sum(int(e.get('count') or 0) for e in bottleneck.top_reasons)} "
            "run(s) in this window",
        )
        reached_provider = any(entry.get("reason") == "acquisition_requests_observed"
                               for entry in bottleneck.top_reasons)
        for entry in bottleneck.top_reasons:
            reason = str(entry.get("reason") or "")
            remedy = next((text for key, text in ACQUISITION_STOP_ACTIONS.items()
                           if key in reason), "")
            if remedy:
                public = ("Reconcile the available acquisition budget and provider balance before resuming."
                          if "governor_zero_budget" in reason else
                          "Resolve the recorded source error before restarting acquisition.")
                add(remedy, f"stop_reason {reason} on {entry.get('count')} run(s)", public)
        if reached_provider and bottleneck.top_reasons:
            add(ACQUISITION_REACHED_PROVIDER_ACTION,
                "runs recorded acquisition requests yet captured 0 jobs",
                "Check requested date ranges, available inventory and duplicate counts to explain the empty acquisition.")
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
    if bottleneck.kind not in ("acquisition_entry", "acquisition_failure", "funnel_boundary", "delivery_outcomes"):
        # ONE CAUSE, ONE ACTION. When several metrics are incomplete because the SAME
        # runs were silent, they are not several problems -- they are one run that did
        # not report, seen from three angles. Emitting a chore per metric filled the
        # plan with three restatements of a single instrumentation gap and left no
        # room for anything Brett could act on.
        #
        # Two metrics belong to the same cause when the same runs are missing from
        # both; a derived rate belongs to it when both of its inputs do.
        silent = _silent_runs(metrics, bottleneck)
        for gap in gaps:
            remedy = getattr(gap, "remedy", None)
            metric = getattr(gap, "metric", "")
            if not remedy:
                continue
            if silent and _shares_cause(metric, metrics, silent):
                continue
            public = {
                "sent_to_instantly": "Confirm successful imports directly in Instantly for the reporting period.",
                "jobs_captured": "Record new postings from every run so the weekly total can be verified.",
                "jobs_reviewed": "Confirm which runs reached review and preserve their reviewed-posting counts.",
                "qualified_opportunities": "Record how many qualified company and role opportunities entered contact discovery.",
                "contacts_found": "Preserve the number of contacts found by every run.",
                "sent_to_airtable": "Reconcile successful Airtable writes and failed deliveries for every run.",
                "review_rate_pct": "Separate recovered postings from new acquisitions before calculating review rates.",
            }.get(metric, "Restore the missing measurements before comparing this period's results.")
            add(remedy, f"evidence gap on {metric}", public)

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
