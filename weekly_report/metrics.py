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
from orchestrator.run_ledger import STATE_COMPLETE
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
    #: Candidates, most authoritative first. Each is ``(artifact_stem, dotted_path)``
    #: or ``(artifact_stem, dotted_path, guard)``.
    #:
    #: A guard is ``(stem, path)`` -- that field must be PRESENT on the run -- or
    #: ``(stem, path, expected)`` -- it must be present AND equal to ``expected``.
    #:
    #: The guard exists because a field NAME is not a contract. ``jobs_captured``
    #: meant unique-kept postings before 2026-09-04T19:03 and net-new postings
    #: after it, on the same key, in the same store. Reading it without proof of
    #: which build wrote it silently mixes two populations into one total.
    #:
    #: The value form exists for a second reason: some candidates are only
    #: EQUIVALENT to the metric under a stated execution condition. A candidate that
    #: is exact for a completed run and a lower bound for an interrupted one must
    #: not answer for the interrupted one, because an aggregate cannot carry "this
    #: contribution is a floor" -- it would be summed as though it were exact.
    fields: Tuple[Tuple, ...]
    #: The population this metric counts, as a machine key. ``unit`` is a display
    #: word; this is the identity a boundary subtraction must match on. Getting it
    #: wrong is how "3,000 postings minus 400 opportunities = 2,600 lost" happens.
    counted_unit: str = ""
    #: Where the population came from. ``run_window`` = produced by the runs in
    #: this window. ``external_backlog`` = observed at a provider, drawn from work
    #: accumulated over previous windows.
    cohort: str = "run_window"

    def field_labels(self) -> Tuple[str, ...]:
        """Every candidate this metric may be read from, most authoritative first."""
        return tuple(_field_label(c[0], c[1]) for c in self.fields)

    def read(self, run: RunRecord) -> Tuple[Optional[int], str]:
        """First present candidate as ``(value, "stem.json:path")``.

        The compact reporting ledger is listed first for every metric because it is
        the only store guaranteed to still exist: heavy artifacts are pruned to a
        handful of runs, the ledger is retained for months. The heavy fields remain
        as fallbacks so pre-ledger runs still read exactly as they did.
        """
        for candidate in self.fields:
            stem, path = candidate[0], candidate[1]
            guard = candidate[2] if len(candidate) > 2 else None
            if guard is not None:
                seen = dig(run.artifact(guard[0]), guard[1])
                # The candidate is present but nothing proves it counts what this
                # metric needs. Silence is the correct answer: an unguarded read
                # here is how a pre-2026-09-04 unique-kept total gets summed into a
                # net-new figure and reported as one number.
                if seen is None:
                    continue
                # Value form: the candidate is only equivalent under this condition.
                if len(guard) > 2 and str(seen) != str(guard[2]):
                    continue
            raw = dig(run.artifact(stem), path)
            value = as_count(raw)
            if value is not None:
                return value, _field_label(stem, path)
        return None, ""


#: COUNTED UNITS -- the identity a boundary subtraction must match on.
#:
#: These were read from the producers, not chosen to make the funnel look tidy:
#:
#:   UNIT_POSTING       ``_dedup`` counts one per provider posting, and
#:                      ``qualification_pipeline`` reports ``input_jobs = len(jobs)``;
#:   UNIT_OPPORTUNITY   ``hiring_manager`` increments ``contact_discovery_entered``
#:                      once per COMPANY x ROLE BUCKET about to run a people search,
#:                      and produces one Lead per that same key -- so "qualified
#:                      opportunities" and "contacts found" are the same population
#:                      at two points, and one Airtable row is created per Lead;
#:   UNIT_INSTANTLY_LEAD an Instantly lead is a PERSON in a campaign.
#:
#: A run acquiring 6,205 postings and entering 2,410 opportunities has not "lost
#: 3,795": the second number counts a different thing. That subtraction is what
#: these constants exist to make impossible.
UNIT_POSTING = "posting"
UNIT_OPPORTUNITY = "company_role_bucket_opportunity"
UNIT_INSTANTLY_LEAD = "instantly_lead"

#: Cohorts. Two counters can share a unit and still be incomparable.
COHORT_RUN_WINDOW = "run_window"
COHORT_EXTERNAL_BACKLOG = "external_backlog"

#: The headline funnel, in pipeline order. The dashboard renders this order too.
RUN_METRIC_SPECS: Tuple[MetricSpec, ...] = (
    MetricSpec(
        key="jobs_captured",
        label="Jobs captured",
        unit="posting",
        definition=(
            "NET-NEW job postings captured by runs in this window: rows the provider "
            "returned minus the ones a previous run had already processed to "
            "completion. This is the population that becomes this week's work, so it "
            "is directly comparable with jobs reviewed. Provider rows bought a second "
            "time are acquisition cost and are reported separately as "
            "provider_jobs_returned / historical_duplicates -- counting them here "
            "made a re-bought posting look like a review-stage loss."
        ),
        fields=(
            ("ledger", "metrics.net_new_jobs_captured"),
            ("orchestrator_result", "acquisition.cumulative.net_new_jobs_captured"),
            # ``jobs_captured`` is only net-new on a build that ALSO records
            # ``net_new_jobs_captured`` -- which is exactly the build that made it
            # net-new. Before 2026-09-04T19:03 the same key held unique-kept
            # postings. The guard is redundant in practice (net-new is listed
            # first) and kept because a future reordering would otherwise
            # reintroduce the mix silently.
            ("ledger", "metrics.jobs_captured",
             ("ledger", "metrics.net_new_jobs_captured")),
            # RECOVERY for runs whose build predated the net-new counter, and the
            # PREFERRED one: it is a directly-emitted counter, in the right unit,
            # from the run itself.
            #
            # VERIFIED against the build that actually ran 20260904T130130Z-13b44a0c
            # (commit 8291a09): `_dedup` there ends
            #     stage = reconcile_stage("acquisition_dedup", "posting", dispo)
            # -- and the UNIT is matched, not assumed. `reconcile_stage` takes the
            # unit as an argument, so a stage of the same name reconciled in some
            # other unit would otherwise be summed straight into a posting total.
            # with one PASSED appended per surviving posting, and `WaterfallReport.
            # to_dict` writes every stage. So `passed` is exactly the population
            # `net_new_jobs_captured` counts today, declared "posting", written by
            # the same call. That run wrote no ledger at all -- `run_ledger.py` did
            # not exist until b332577, four hours after it finished -- so this
            # heavy-artifact field is the ONLY thing that can measure its capture.
            #
            # NOT `unit_totals.opportunities`: on the top-up path that run took,
            # that key is `len(all_leads)` (2,410 leads), not postings.
            ("waterfall", "stages[all:stage=acquisition_dedup,unit=posting].passed"),
            ("orchestrator_result",
             "waterfall.stages[all:stage=acquisition_dedup,unit=posting].passed"),
            # LAST-RESORT RECOVERY, and legitimate because it is the SAME LIST
            # measured twice. `_dedup` returns `opportunities`; the pipeline adds
            # `len(opportunities)` to net_new_jobs_captured and hands that identical
            # list to the enrichment engine, which writes it verbatim as
            # qualification's input file, whose length becomes qualification_input.
            # Nothing filters between the two `len()` calls, so in a run that reached
            # enrichment they are equal by construction -- pinned by
            # test_captured_and_reviewed_are_the_same_list.
            #
            # They are NOT redundant, and the asymmetry runs one way only: a run that
            # stops between the acquisition checkpoint and the enrichment funnel --
            # or between two slices of a multi-slice run -- emits the first and not
            # the second, so reviewed <= captured always. This candidate therefore
            # recovers a floor and can never overstate captured work. When the run
            # never reviewed anything there is nothing here to read at all, so a
            # genuinely silent run still reads as silent.
            #
            # It is also last, so it is consulted only when the run emitted no
            # capture counter of its own. Current builds checkpoint acquisition
            # before enrichment, so a run that stopped early still answers directly;
            # in practice this reaches only builds that predate the net-new counter.
            #
            # GUARDED ON COMPLETION, and that guard is the whole reason this is
            # safe. The equality is per-slice: acquisition accumulates
            # `len(opportunities)` for every slice it ran, the funnel accumulates
            # `qualification_input` for every slice that finished enrichment. A run
            # that stopped in between -- a crash, a raised iteration guard mid-slice
            # -- has run more acquisition slices than enrichment slices, so reviewed
            # is a FLOOR, not the count. An aggregate has nowhere to record "this
            # contribution is a floor": it would be summed as though exact and the
            # period total would silently understate captured work. So a run that
            # did not complete does not answer here at all, and reads as silent.
            ("ledger", "metrics.jobs_reviewed",
             ("ledger", "state", STATE_COMPLETE)),
            ("orchestrator_result", "enrichment.funnel.qualification_input",
             ("run_manifest", "status", "complete")),
            # REMOVED, deliberately: waterfall.unit_totals.postings,
            # capacity_report.raw_postings and the orchestrator_result copy of the
            # first. All three count rows the lanes KEPT, before cross-run dedupe.
            # On 2026-09-04 that was 6,205 against a net-new figure the run never
            # emitted. A run that cannot answer must read as unavailable, not as
            # provider volume wearing the throughput label.
        ),
        counted_unit=UNIT_POSTING,
    ),
    MetricSpec(
        key="jobs_reviewed",
        label="Jobs reviewed",
        unit="posting",
        definition=(
            "Postings that entered the qualification/review stage in those runs. "
            "This is the SAME population as jobs captured, measured one stage later: "
            "the deduped opportunity list is handed straight to qualification, so in "
            "a run that reached enrichment the two are equal. They differ only when a "
            "run stopped in between, where this one is absent rather than smaller. "
            "There is no attrition between them, and no rate to read across them."
        ),
        fields=(
            ("ledger", "metrics.jobs_reviewed"),
            ("orchestrator_result", "enrichment.funnel.qualification_input"),
        ),
        counted_unit=UNIT_POSTING,
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
        counted_unit=UNIT_OPPORTUNITY,
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
        counted_unit=UNIT_OPPORTUNITY,
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
        counted_unit=UNIT_OPPORTUNITY,
    ),
)

#: Secondary counters: useful context on the report and for the dashboard, but
#: not part of the headline funnel Brett asked for.
SUPPORTING_METRIC_SPECS: Tuple[MetricSpec, ...] = (
    MetricSpec(
        key="provider_jobs_returned",
        label="Provider rows returned",
        unit="row",
        definition=(
            "Rows the provider returned and billed, before any of our deduplication. "
            "Acquisition volume, not throughput."
        ),
        fields=(
            ("ledger", "metrics.provider_jobs_returned"),
            ("orchestrator_result", "acquisition.cumulative.jobs_returned_billed"),
            ("waterfall", "unit_totals.postings"),
        ),
    ),
    MetricSpec(
        key="provider_jobs_billed",
        label="Provider credits consumed",
        unit="credit",
        definition=(
            "Jobs credits the provider actually charged this window. The denominator "
            "of every yield-per-credit number."
        ),
        fields=(
            ("ledger", "metrics.provider_jobs_billed"),
            ("orchestrator_result", "acquisition.cumulative.jobs_quota_consumed"),
        ),
    ),
    MetricSpec(
        key="historical_duplicates",
        label="Rows already processed in a previous run",
        unit="row",
        definition=(
            "Provider rows dropped because an earlier run had already carried that "
            "exact posting to a terminal outcome. The cost of the acquisition "
            "overlap, and the number that tells us whether the window is draining. "
            "Counted at the dedupe decision point: it no longer absorbs rows this "
            "run simply bought twice (see canonical_duplicates_in_run), which is a "
            "different problem with a different fix."
        ),
        fields=(
            ("ledger", "metrics.historical_previously_seen_duplicates"),
            ("ledger", "metrics.historical_duplicates"),
            ("orchestrator_result",
             "acquisition.cumulative.historical_previously_seen_duplicates"),
            ("orchestrator_result", "acquisition.cumulative.historical_duplicates"),
        ),
    ),
    MetricSpec(
        key="canonical_duplicates_in_run",
        label="Rows bought twice within one run",
        unit="row",
        definition=(
            "Postings whose canonical identity was already present EARLIER IN THE "
            "SAME RUN. Pure acquisition waste: we paid for the row twice inside one "
            "window. Distinct from a row a PREVIOUS run processed."
        ),
        fields=(
            ("ledger", "metrics.canonical_duplicates_in_run"),
            ("orchestrator_result", "acquisition.cumulative.canonical_duplicates_in_run"),
        ),
    ),
    MetricSpec(
        key="cross_query_duplicates",
        label="Rows returned twice by the provider",
        unit="row",
        definition="The same posting billed more than once within this run's queries.",
        fields=(
            ("ledger", "metrics.cross_query_duplicates"),
            ("orchestrator_result", "acquisition.cumulative.cross_query_duplicates"),
        ),
    ),
    MetricSpec(
        key="cross_source_duplicates",
        label="Rows seen from a second source",
        unit="row",
        definition="The same posting reaching us through more than one source.",
        fields=(
            ("ledger", "metrics.cross_source_duplicates"),
            ("orchestrator_result", "acquisition.cumulative.cross_source_duplicates"),
        ),
    ),
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
        key="airtable_candidates",
        label="Rows submitted to Airtable",
        unit="row",
        definition=(
            "Send-safe leads handed to the Airtable writer. The row above it "
            "(created) plus every suppression reason must account for all of these; "
            "an unexplained gap is a measurement defect, not a business result."
        ),
        fields=(
            ("ledger", "metrics.airtable_candidates"),
            ("delivery", "reviewable_submitted"),
            ("orchestrator_result", "delivery.reviewable_submitted"),
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
    MetricSpec(
        key="airtable_write_failures",
        label="Airtable writes that failed",
        unit="row",
        definition="Rows the Airtable writer accepted but could not create.",
        fields=(
            ("ledger", "metrics.airtable_write_failures"),
            ("delivery", "failed"),
            ("orchestrator_result", "delivery.failed"),
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
        counted_unit=spec.counted_unit,
        cohort=spec.cohort,
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
    # UNIT. A rate between two different populations is not a conversion rate. It
    # is the same rule the bottleneck search applies to a subtraction, and for the
    # same reason: postings divided by company x role-bucket opportunities is a
    # number with no referent.
    num_unit = numerator.counted_unit or ""
    den_unit = denominator.counted_unit or ""
    if num_unit != den_unit or not num_unit:
        metric.status = STATUS_UNAVAILABLE
        metric.reason = (
            f"{numerator.key} counts {num_unit or 'an undeclared unit'} and "
            f"{denominator.key} counts {den_unit or 'an undeclared unit'}; a ratio "
            "between different populations is not a conversion rate")
        return metric
    if (numerator.cohort or "") != (denominator.cohort or ""):
        metric.status = STATUS_UNAVAILABLE
        metric.reason = (
            f"{numerator.key} is the {numerator.cohort or 'undeclared'} cohort and "
            f"{denominator.key} is the {denominator.cohort or 'undeclared'} cohort; "
            "they share a date range, not a population")
        return metric
    # RUN SET. Dividing a total summed over runs {A,B} by one summed over {A}
    # produces a rate that describes neither. Recording the intersection in
    # metadata does not make the divided totals comparable -- the division has
    # already happened by then.
    #
    # A same-cohort subset rate IS legitimate, but only when BOTH sides are
    # recomputed from that exact subset. That is a different metric with a
    # different denominator, and it must never be presented as the full-period
    # rate, so it is not silently substituted here.
    num_runs = set(numerator.contributing_run_ids)
    den_runs = set(denominator.contributing_run_ids)
    if num_runs != den_runs:
        only_num = sorted(num_runs - den_runs)
        only_den = sorted(den_runs - num_runs)
        metric.status = STATUS_UNAVAILABLE
        metric.reason = (
            f"{numerator.key} and {denominator.key} were summed over different run "
            f"sets (only in {numerator.key}: {', '.join(only_num) or 'none'}; only "
            f"in {denominator.key}: {', '.join(only_den) or 'none'}), so their "
            "ratio is not a rate for either population")
        return metric
    if not denominator.value:
        metric.status = STATUS_UNAVAILABLE
        metric.reason = f"{denominator.key} is 0; a rate over an empty denominator is undefined"
        return metric
    metric.value = round(100.0 * float(numerator.value) / float(denominator.value), 1)
    # Both sides now cover the SAME runs, so a partial pair is partial in the same
    # way on both sides and the rate is exact over the runs it covers. It is still
    # labelled, because it is not a rate for the full period.
    metric.status = weakest(numerator.status, denominator.status)
    if metric.status == STATUS_PARTIAL:
        metric.reason = (
            f"exact over the {len(num_runs)} run(s) that reported both counters; "
            "not the full-period rate")
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
        definition=(
            "Jobs reviewed as a percentage of jobs captured in the same runs. 100% "
            "means every captured posting entered review -- which is what a run that "
            "completed its slices looks like, because the deduped opportunity list is "
            "handed straight to qualification. It is not automatic: a run interrupted "
            "between acquiring a slice and enriching it reviews fewer than it "
            "captured, and the rate falls below 100 accordingly."),
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
