"""Typed records emitted by the retrieval measurement harness.

Every artifact the harness writes is built from these dataclasses, so the
on-disk schema is defined in exactly one place and versioned by
``SCHEMA_VERSION``.

The reconciliation identities at the bottom of this module are the harness's
integrity contract. If any of them fails, the run is marked ``incomplete`` and
the process exits non-zero: a measurement that cannot account for every record
it saw is not a measurement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from retrieval_measurement import HARNESS_VERSION, SCHEMA_VERSION

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Why a fetch loop stopped. ``configured_cap`` and ``provider_exhaustion`` are
#: never merged: the first means we chose to stop, the second means there was
#: nothing left. Conflating them is exactly how a retrieval ceiling becomes
#: invisible.
TRUNCATION_KINDS = (
    "configured_cap",
    "provider_exhaustion",
    "unexplained_shortfall",
    "empty_page",
    "duplicate_page",
    "quota_guard",
    "deadline_guard",
    "retry_exhaustion",
    "error_stop",
    "not_truncated",
)

#: The three discard classes that actually REMOVE a record in production
#: (multi_source_acquisition.py:989-997). Everything else in that loop is a
#: counter over records that are still kept.
REMOVING_DISCARD_REASONS = (
    "missing_job_id",
    "previously_seen",
    "excluded_by_seniority",
)

#: Counters in the same production loop that annotate but do not remove
#: (multi_source_acquisition.py:1000, 1005-1008). Kept separate so they are
#: never accidentally subtracted.
ANNOTATING_COUNTERS = (
    "role_accept",
    "role_review",
    "role_reject",
    "prefilter_viable",
    "prefilter_rejected",
)

#: Ordered identity ladder used for posting-level uniqueness. Earlier is
#: stronger; the label is recorded per record so the report can show how much
#: of the inventory estimate rests on weak identity.
IDENTITY_STRENGTHS = ("provider_job_id", "apply_url", "content_digest")

#: The acquisition lanes a run may select. Every lane a run touches is named
#: here and recorded in the manifest, so "which sources did this number come
#: from?" is answered by the artifact rather than by reading the code.
LANES = ("free_feeds", "jsearch", "adzuna", "ats")

#: Reported credential states. Only these three ever appear in an artifact --
#: a value never does, not even hashed or truncated.
CREDENTIAL_STATES = ("PRESENT", "ABSENT", "NOT_REQUIRED")


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(item) for item in value]
    return value


@dataclass
class _Record:
    def to_dict(self) -> Dict[str, Any]:
        return _clean(asdict(self))


# --------------------------------------------------------------------------
# Run identity
# --------------------------------------------------------------------------


@dataclass
class EffectiveConfigEntry(_Record):
    name: str
    value: Any
    source: str  # env | code_default | run_argument
    redacted: bool = False


#: The five units a run measures. They are never silently converted into one
#: another: 159 postings becoming 255 companies is a unit transition, not a
#: yield, and reporting it as a yield is how "10% survival" and "12% of
#: companies" get multiplied together into a number that means nothing.
UNITS = ("posting", "opportunity", "company", "contact", "final_lead")

#: Terminal record states. Kept distinct at every boundary; an Airtable row is
#: not a FINAL_PASS lead and neither is an enrolled contact.
RECORD_STATES = ("FINAL_PASS", "NEEDS_CHECK", "UNVERIFIED", "REJECT", "REROUTE")

#: Budget scopes, innermost first. A board budget stopping does not stop its
#: provider; a provider stopping does not stop its lane.
BUDGET_SCOPES = ("board", "provider", "lane", "run")


@dataclass
class WaterfallBoundary(_Record):
    """One stage boundary in a run, in one explicit unit."""

    name: str
    unit: str
    entered: int
    passed: int
    rejected: int = 0
    deferred: int = 0
    errored: int = 0
    primary_reasons: Dict[str, int] = field(default_factory=dict)
    secondary_reasons: Dict[str, int] = field(default_factory=dict)
    cumulative_survival: Optional[float] = None
    unit_transition: str = ""   # e.g. "posting->company" when the unit changes
    note: str = ""

    def __post_init__(self) -> None:
        if self.unit not in UNITS:
            raise ValueError(f"unknown unit {self.unit!r}; expected one of {UNITS}")

    @property
    def accounted(self) -> int:
        return self.passed + self.rejected + self.deferred + self.errored

    @property
    def reconciles(self) -> bool:
        return self.entered == self.accounted

    def reason_total(self) -> int:
        return sum(self.primary_reasons.values())


@dataclass
class StateCounts(_Record):
    """Terminal states, reported separately and never summed into one number."""

    final_pass: int = 0
    needs_check: int = 0
    unverified: int = 0
    reject: int = 0
    reroute: int = 0
    airtable_created: int = 0
    outbound_enrolled: int = 0

    @property
    def reviewable(self) -> int:
        """Airtable-reviewable rows. NOT a FINAL_PASS count."""
        return self.final_pass + self.needs_check


@dataclass
class FreshInventory(_Record):
    """New-versus-seen, measured in each unit that matters for capacity."""

    run_id: str = ""
    new_posting_identities: int = 0
    new_opportunities: int = 0
    new_companies: int = 0
    new_icp_eligible_companies: int = 0
    previously_processed_companies: int = 0
    suppressed_companies: int = 0
    total_companies_observed: int = 0
    snapshot_available: bool = False
    depletion: Dict[str, Any] = field(default_factory=dict)

    @property
    def reconciles(self) -> bool:
        return self.total_companies_observed == (
            self.new_companies + self.previously_processed_companies
        )


@dataclass
class BoardResult(_Record):
    """One ATS board, persisted the moment that board completes."""

    provider: str
    identifier: str
    company_name: str = ""
    started_at: str = ""
    completed_at: str = ""
    physical_requests: int = 0
    pages: int = 0
    redirects: int = 0
    retries: int = 0
    listing_records: int = 0
    detail_records: int = 0
    canonical_records: int = 0
    unique_posting_identity: int = 0
    unique_production_equivalent: int = 0
    truncation: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    error: str = ""
    checkpoint_path: str = ""
    #: Selection and skip provenance (Phase 1B-2B). ``attempted`` is False only
    #: for a board that was selected but skipped before any request; such a
    #: board sits outside ``boards_attempted``.
    attempted: bool = True
    skipped_by_budget: bool = False
    skipped_by_scheduler: bool = False
    selection_reason: str = ""
    exhausted_scope: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.identifier}"


@dataclass
class CredentialStatus(_Record):
    """Presence only. The value is never read into this record."""

    name: str
    lane: str
    status: str  # PRESENT | ABSENT | NOT_REQUIRED

    def __post_init__(self) -> None:
        if self.status not in CREDENTIAL_STATES:
            raise ValueError(f"unknown credential state: {self.status!r}")


@dataclass
class LanePreflight(_Record):
    """Whether one lane may run, decided before any outbound request."""

    lane: str
    selected: bool
    ready: bool
    blocking_reasons: List[str] = field(default_factory=list)
    credentials: List[Dict[str, Any]] = field(default_factory=list)
    detail: str = ""


@dataclass
class LaneFailure(_Record):
    """A lane that raised. Deliberately NOT a truncation record.

    A lane that blew up tells us nothing about whether the provider had more
    inventory, so it must never reach the truncation vocabulary -- an exception
    recorded as ``provider_exhaustion`` would read as "we got everything".
    """

    lane: str
    exception_type: str
    error: str  # redacted
    requests_attempted_before_failure: int = 0
    stage: str = "lane_execution"


@dataclass
class RunManifest(_Record):
    run_id: str
    mode: str  # fixture | offline_replay | live_acquisition
    started_at: str
    finished_at: str = ""
    schema_version: str = SCHEMA_VERSION
    harness_version: str = HARNESS_VERSION
    git_commit: str = ""
    git_branch: str = ""
    git_dirty: Optional[bool] = None
    python_version: str = ""
    platform: str = ""
    config_fingerprint: str = ""
    effective_config: List[Dict[str, Any]] = field(default_factory=list)
    seen_snapshot: Dict[str, Any] = field(default_factory=dict)
    lanes_selected: List[str] = field(default_factory=list)
    lane_selection_source: str = ""
    preflight: List[Dict[str, Any]] = field(default_factory=list)
    lane_failures: List[Dict[str, Any]] = field(default_factory=list)
    request_budget: Dict[str, Any] = field(default_factory=dict)
    status: str = "incomplete"
    exit_reason: str = ""
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Transport observations
# --------------------------------------------------------------------------


@dataclass
class RequestRecord(_Record):
    sequence: int
    source: str
    url: str
    method: str = "GET"
    param_keys: List[str] = field(default_factory=list)
    query_key: str = ""
    board_key: str = ""
    status_code: Optional[int] = None
    response_bytes: int = 0
    duration_seconds: float = 0.0
    error: str = ""


@dataclass
class DenominatorRecord(_Record):
    """A total that the provider itself published. Never inferred."""

    provider: str
    value: int
    field_name: str
    scope: str  # whole_feed | per_query | per_board
    scope_key: str
    semantics: str
    observed_at: str


@dataclass
class TruncationRecord(_Record):
    source: str
    scope_key: str
    kind: str
    detected: bool
    reason: str = ""
    applied_cap: Optional[int] = None
    known_unfetched: Optional[int] = None
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscardRecord(_Record):
    source: str
    reason: str
    count: int
    removes_record: bool
    query_key: str = ""
    board_key: str = ""


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


@dataclass
class UniquenessMetrics(_Record):
    """Two independent uniqueness definitions, reported side by side.

    ``unique_posting_identity`` is the inventory estimate: how many distinct
    job postings exist in what we retrieved.

    ``unique_production_equivalent`` is what the live pipeline's
    ``_dedupe`` would collapse those to, on (company_identity, normalized
    title). It is the funnel-equivalent number and is deliberately NOT used as
    an inventory estimate -- two genuinely different openings at one company
    with the same title collapse to one.
    """

    returned_total: int = 0
    unique_posting_identity: int = 0
    duplicates_posting_identity: int = 0
    unique_production_equivalent: int = 0
    duplicates_production_equivalent: int = 0
    collapse_delta: int = 0
    identity_strength_histogram: Dict[str, int] = field(default_factory=dict)


@dataclass
class BaselineMetrics(_Record):
    """Gross and production-effective retrieval, in the same run.

    Neither is presented as *the* number. ``gross_returned`` is what the
    providers gave us; ``incremental_new`` is what would actually reach the
    funnel today. Brett's question needs both.
    """

    gross_returned: int = 0
    unique_in_run: int = 0
    previously_seen: Optional[int] = None
    incremental_new: Optional[int] = None
    snapshot_available: bool = False
    basis: str = "posting_identity"


@dataclass
class SourceMetrics(_Record):
    source: str
    lane: str  # free_feed | adzuna | ats_board | jsearch
    success: bool = True
    requests_attempted: int = 0
    requests_succeeded: int = 0
    pages: int = 0
    provider_rows: int = 0
    canonical_records: int = 0
    adapter_dropped_or_capped: int = 0
    kept_after_removals: int = 0
    removals: Dict[str, int] = field(default_factory=dict)
    annotations: Dict[str, int] = field(default_factory=dict)
    uniqueness: Dict[str, Any] = field(default_factory=dict)
    baseline_posting_identity: Dict[str, Any] = field(default_factory=dict)
    baseline_production_equivalent: Dict[str, Any] = field(default_factory=dict)
    denominator: Optional[Dict[str, Any]] = None
    capture_rate: Optional[float] = None
    capture_rate_basis: str = "unavailable"
    truncation: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    first_posted_at: str = ""
    last_posted_at: str = ""


@dataclass
class QueryMetrics(_Record):
    source: str
    query_key: str
    requests: int = 0
    provider_rows: int = 0
    canonical_records: int = 0
    denominator: Optional[int] = None
    truncated: bool = False


@dataclass
class BoardMetrics(_Record):
    provider: str
    identifier: str
    company_name: str = ""
    requests: int = 0
    canonical_records: int = 0
    denominator: Optional[int] = None
    error: str = ""


@dataclass
class TitleCoverage(_Record):
    title: str
    matched_records: int = 0
    sources: List[str] = field(default_factory=list)


@dataclass
class ParityCheck(_Record):
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ReconciliationCheck(_Record):
    name: str
    scope: str
    passed: bool
    left: int
    right: int
    delta: int
    stage: str
    detail: str = ""


@dataclass
class ReconciliationResult(_Record):
    checks: List[Dict[str, Any]] = field(default_factory=list)
    passed: bool = True
    failed_scopes: List[str] = field(default_factory=list)


@dataclass
class CoverageSummary(_Record):
    run_id: str
    mode: str
    schema_version: str = SCHEMA_VERSION
    claim_boundary: str = ""
    run_baseline_posting_identity: Dict[str, Any] = field(default_factory=dict)
    run_baseline_production_equivalent: Dict[str, Any] = field(default_factory=dict)
    per_source: List[Dict[str, Any]] = field(default_factory=list)
    sources_with_denominator: List[str] = field(default_factory=list)
    sources_without_denominator: List[str] = field(default_factory=list)
    total_market_estimate: Optional[float] = None
    total_market_estimate_reason: str = (
        "Not derivable. No provider publishes a US-wide total for the targeted "
        "titles, and cross-provider overlap is unmeasured, so no defensible "
        "denominator exists. Reporting one would be a guess presented as a "
        "measurement."
    )
    reconciliation: Dict[str, Any] = field(default_factory=dict)
    parity: List[Dict[str, Any]] = field(default_factory=list)
    title_coverage: List[Dict[str, Any]] = field(default_factory=list)
    lanes_selected: List[str] = field(default_factory=list)
    lanes_completed: List[str] = field(default_factory=list)
    lane_failures: List[Dict[str, Any]] = field(default_factory=list)
    request_budget: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Reconciliation identities
# --------------------------------------------------------------------------

CLAIM_BOUNDARY = (
    "A single acquisition run measures ONE SNAPSHOT of one configuration at one "
    "moment. It is not a typical week and must not be reported as one. "
    "Weekly figures require the repeated-measurement protocol documented in "
    "TYPICAL_WEEK_PROTOCOL (see coverage_summary.md)."
)


def _check(name: str, scope: str, left: int, right: int, stage: str, detail: str = "") -> ReconciliationCheck:
    return ReconciliationCheck(
        name=name,
        scope=scope,
        passed=left == right,
        left=int(left),
        right=int(right),
        delta=int(left) - int(right),
        stage=stage,
        detail=detail,
    )


def reconcile_source(metrics: SourceMetrics) -> List[ReconciliationCheck]:
    """The per-source identities every measured lane must satisfy.

    1. Provider rows are fully accounted for: everything the provider returned
       either became a canonical record or was dropped/capped inside the
       adapter.
    2. Canonical records are fully accounted for by the three REMOVING discard
       classes plus what survived. Annotating counters are excluded by
       construction -- subtracting them here is the exact mistake that makes
       production's funnel numbers unreadable.
    3. Survivors split cleanly into unique + duplicates, independently for
       each of the two uniqueness definitions.
    """
    removals = sum(int(metrics.removals.get(reason, 0)) for reason in REMOVING_DISCARD_REASONS)
    uniq = metrics.uniqueness or {}
    checks = [
        _check(
            "provider_rows_accounted",
            metrics.source,
            metrics.provider_rows,
            metrics.canonical_records + metrics.adapter_dropped_or_capped,
            "adapter",
            "provider_rows == canonical_records + adapter_dropped_or_capped",
        ),
        _check(
            "canonical_records_accounted",
            metrics.source,
            metrics.canonical_records,
            removals + metrics.kept_after_removals,
            "discard",
            "canonical_records == removing_discards + kept_after_removals",
        ),
        _check(
            "posting_identity_split",
            metrics.source,
            metrics.kept_after_removals,
            int(uniq.get("unique_posting_identity", 0)) + int(uniq.get("duplicates_posting_identity", 0)),
            "uniqueness",
            "kept == unique_posting_identity + duplicates_posting_identity",
        ),
        _check(
            "production_equivalent_split",
            metrics.source,
            metrics.kept_after_removals,
            int(uniq.get("unique_production_equivalent", 0))
            + int(uniq.get("duplicates_production_equivalent", 0)),
            "uniqueness",
            "kept == unique_production_equivalent + duplicates_production_equivalent",
        ),
    ]
    return checks


def reconcile_run(
    per_source: Sequence[SourceMetrics],
    run_uniqueness: UniquenessMetrics,
    run_kept: int,
) -> ReconciliationResult:
    checks: List[ReconciliationCheck] = []
    for metrics in per_source:
        checks.extend(reconcile_source(metrics))

    checks.append(
        _check(
            "run_kept_equals_source_sum",
            "run",
            run_kept,
            sum(metric.kept_after_removals for metric in per_source),
            "aggregate",
            "run-level survivors == sum of per-source survivors",
        )
    )
    checks.append(
        _check(
            "run_posting_identity_split",
            "run",
            run_kept,
            run_uniqueness.unique_posting_identity + run_uniqueness.duplicates_posting_identity,
            "uniqueness",
            "run kept == unique + duplicates (posting identity, cross-source)",
        )
    )
    checks.append(
        _check(
            "run_production_equivalent_split",
            "run",
            run_kept,
            run_uniqueness.unique_production_equivalent
            + run_uniqueness.duplicates_production_equivalent,
            "uniqueness",
            "run kept == unique + duplicates (production dedupe, cross-source)",
        )
    )

    failed = sorted({check.scope for check in checks if not check.passed})
    return ReconciliationResult(
        checks=[check.to_dict() for check in checks],
        passed=not failed,
        failed_scopes=failed,
    )
