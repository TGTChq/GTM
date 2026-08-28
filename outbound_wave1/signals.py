"""Wave 1 signal resolution: T1 -> T2 -> T3, never a guess.

    T1  a verified second-order signal, campaign-specific
    T2  the job-age signal, only inside a 45-60 day window
    T3  the active-req fallback

Every tier is licensed by evidence that is already stored on the record. If the
stored evidence does not genuinely support the tier, resolution falls through to
the next one. Reposted and first-hire signals are out of Wave 1 scope and are
not implemented here at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from .campaigns import (
    SIGNAL_ACTIVE_REQ,
    SIGNAL_JOB_AGE,
    SIGNAL_MULTI_OPENING,
    SIGNAL_MULTI_OPENING_CROSS_BUCKET,
    SIGNAL_ROLE_FOCUS_MATCH,
    SIGNAL_SCOPE_COMBINATION,
    TIER_1,
    TIER_2,
    TIER_3,
    CampaignPolicy,
)
from .evidence import FocusEvidence
from .scope import ScopeCombination

#: Inclusive job-age window for the T2 signal.
T2_MIN_AGE_DAYS = 45
T2_MAX_AGE_DAYS = 60

#: A posted date older than this is treated as untrustworthy rather than ancient.
_MAX_TRUSTED_AGE_DAYS = 1100

#: URL states that positively say the opening is gone. Anything else (including
#: the pipeline's "unverified_review", which only means the URL was not probed)
#: leaves the posting eligible -- the row was approved by a human on that basis.
_DEAD_URL_STATES = frozenset({
    "expired", "closed", "inactive", "dead", "gone", "removed",
    "not_found", "404", "filled",
})

#: Freshness value that literally means "posted date missing or unparseable".
_UNKNOWN_FRESHNESS = "unknown_review"


@dataclass(frozen=True)
class CompanyContext:
    """Company-level facts derived from the batch being resolved.

    Cross-bucket signals need more than one row, so they are only available when
    the resolver is given a batch. With no batch index the cross-bucket signal
    simply does not fire -- it is never estimated.
    """

    buckets: Tuple[str, ...] = ()
    openings_by_bucket: Dict[str, int] = field(default_factory=dict)
    roles_by_bucket: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    indexed: bool = False

    @property
    def bucket_count(self) -> int:
        return len(self.buckets)

    @property
    def total_openings(self) -> int:
        return sum(self.openings_by_bucket.values())


EMPTY_COMPANY_CONTEXT = CompanyContext()


@dataclass(frozen=True)
class SignalResolution:
    tier: str
    signal_type: str
    #: Structured, auditable evidence for the tier that fired.
    evidence: Dict[str, Any]
    #: Human-readable trail of what was tried and why it did not fire.
    degrade_reasons: Tuple[str, ...]
    #: Openings counted for this record's own bucket (>= 1 when known).
    opening_count: int = 0
    #: Age in days when it could be trusted, else None.
    job_age_days: Optional[int] = None

    @property
    def evidence_present(self) -> bool:
        return bool(self.evidence)


def _text(value: Any) -> str:
    return str(value or "").strip()


def open_role_titles(fields: Dict) -> Tuple[str, ...]:
    """Distinct open roles stored on the row for this company and bucket.

    ``Outbound Roles`` is the send-safe display list; ``Open Roles`` is the raw
    canonical list. Both are pipe-joined by ``airtable_client._job_to_fields``.
    """
    for key in ("Outbound Roles", "Open Roles"):
        raw = _text(fields.get(key))
        if not raw:
            continue
        titles = tuple(
            dict.fromkeys(part.strip() for part in raw.split("|") if part.strip())
        )
        if titles:
            return titles
    single = _text(fields.get("Outbound Role")) or _text(fields.get("Open Role"))
    return (single,) if single else ()


def parse_posted_at(value: Any) -> Optional[datetime]:
    """Parse a stored ``Posted At`` value, or return ``None``.

    Unparseable is a hard stop for the job-age signal; it is never approximated
    from another field.
    """
    text = _text(value)
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def posting_is_eligible(fields: Dict) -> Tuple[bool, str]:
    """Is the posting still active/eligible on stored evidence alone?"""
    if fields.get("Outbound Hold") is True:
        return False, "outbound_hold_set"
    url_status = _text(fields.get("Job URL Status")).lower()
    if url_status in _DEAD_URL_STATES:
        return False, f"job_url_status_{url_status}"
    return True, "posting_eligible_on_stored_evidence"


def job_age_days(fields: Dict, *, as_of: Optional[datetime] = None) -> Tuple[Optional[int], str]:
    """Trusted age in days derived from ``Posted At``, or ``(None, reason)``."""
    freshness = _text(fields.get("Job Freshness")).lower()
    if freshness == _UNKNOWN_FRESHNESS:
        return None, "posted_at_unparseable_per_stored_freshness"
    posted = parse_posted_at(fields.get("Posted At"))
    if posted is None:
        return None, "posted_at_missing_or_unparseable"
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if posted > now + timedelta(days=1):
        return None, "posted_at_is_in_the_future"
    age = (now - posted).days
    if age > _MAX_TRUSTED_AGE_DAYS:
        return None, "posted_at_older_than_trusted_range"
    return max(0, age), "job_age_derived_from_posted_at"


def _t1_multi_opening(fields: Dict) -> Tuple[bool, Dict[str, Any], str, int]:
    titles = open_role_titles(fields)
    count = len(titles)
    if count < 2:
        return False, {}, f"multi_opening_needs_2_openings_found_{count}", count
    return (
        True,
        {"open_roles": list(titles), "opening_count": count, "source": "stored_open_roles"},
        "multi_opening_supported",
        count,
    )


def _t1_multi_opening_cross_bucket(
    fields: Dict, company: CompanyContext
) -> Tuple[bool, Dict[str, Any], str, int]:
    own_titles = open_role_titles(fields)
    own_count = len(own_titles)
    if not company.indexed:
        return False, {}, "cross_bucket_signal_needs_a_company_index", own_count
    if company.bucket_count < 2:
        return (
            False, {},
            f"cross_bucket_needs_2_functions_found_{company.bucket_count}",
            own_count,
        )
    return (
        True,
        {
            "functions": list(company.buckets),
            "function_count": company.bucket_count,
            "openings_by_function": dict(company.openings_by_bucket),
            "total_openings": company.total_openings,
            "source": "company_index_over_resolved_batch",
        },
        "cross_bucket_multi_opening_supported",
        own_count,
    )


def _t1_role_focus_match(
    policy: CampaignPolicy, focus: FocusEvidence
) -> Tuple[bool, Dict[str, Any], str]:
    if not focus.is_specific:
        return False, {}, "role_focus_is_a_role_level_fallback_not_matched_evidence"
    needed = max(1, policy.t1_min_evidence)
    if focus.usable_count < needed:
        return (
            False, {},
            f"role_focus_match_needs_{needed}_evidence_items_found_{focus.usable_count}",
        )
    return (
        True,
        {
            "focus_phrases": list(focus.renderable(limit=needed)),
            "usable_evidence_count": focus.usable_count,
            "focus_quality": focus.quality,
            "source": "stored_role_focus_and_focus_evidence",
        },
        "role_focus_match_supported",
    )


def _t1_scope_combination(
    policy: CampaignPolicy, focus: FocusEvidence, scope: ScopeCombination
) -> Tuple[bool, Dict[str, Any], str]:
    if not focus.is_specific:
        return False, {}, "role_focus_is_a_role_level_fallback_not_matched_evidence"
    needed = max(1, policy.t1_min_evidence)
    if focus.usable_count < needed:
        return (
            False, {},
            f"scope_combination_needs_{needed}_evidence_items_found_{focus.usable_count}",
        )
    if not scope.sufficient:
        return False, {}, scope.reason
    return (
        True,
        {
            "scope_facets": list(scope.facets),
            "scope_support": dict(scope.support),
            "usable_evidence_count": focus.usable_count,
            "source": "stored_role_focus_and_focus_evidence",
        },
        "scope_combination_supported",
    )


def resolve_signal(
    fields: Dict,
    *,
    policy: CampaignPolicy,
    focus: FocusEvidence,
    scope: ScopeCombination,
    company: CompanyContext = EMPTY_COMPANY_CONTEXT,
    as_of: Optional[datetime] = None,
) -> SignalResolution:
    """Resolve the strongest tier this record's stored evidence supports."""
    degrade: list[str] = []
    opening_count = len(open_role_titles(fields))

    # ---- T1: campaign-specific verified second-order signal ----------------
    if policy.t1_signal == SIGNAL_MULTI_OPENING:
        ok, evidence, reason, opening_count = _t1_multi_opening(fields)
    elif policy.t1_signal == SIGNAL_MULTI_OPENING_CROSS_BUCKET:
        ok, evidence, reason, opening_count = _t1_multi_opening_cross_bucket(fields, company)
    elif policy.t1_signal == SIGNAL_ROLE_FOCUS_MATCH:
        ok, evidence, reason = _t1_role_focus_match(policy, focus)
    elif policy.t1_signal == SIGNAL_SCOPE_COMBINATION:
        ok, evidence, reason = _t1_scope_combination(policy, focus, scope)
    else:  # pragma: no cover - policy table is closed
        ok, evidence, reason = False, {}, f"unknown_t1_signal_{policy.t1_signal}"

    if ok:
        return SignalResolution(
            TIER_1, policy.t1_signal, evidence, (), opening_count, None
        )
    degrade.append(f"T1:{reason}")

    # ---- T2: job-age window ------------------------------------------------
    eligible, eligibility_reason = posting_is_eligible(fields)
    age, age_reason = job_age_days(fields, as_of=as_of)
    if not eligible:
        degrade.append(f"T2:{eligibility_reason}")
    elif age is None:
        degrade.append(f"T2:{age_reason}")
    elif not (T2_MIN_AGE_DAYS <= age <= T2_MAX_AGE_DAYS):
        degrade.append(
            f"T2:job_age_{age}_days_outside_{T2_MIN_AGE_DAYS}_{T2_MAX_AGE_DAYS}_window"
        )
    else:
        return SignalResolution(
            TIER_2,
            SIGNAL_JOB_AGE,
            {
                "job_age_days": age,
                "posted_at": _text(fields.get("Posted At")),
                "job_freshness": _text(fields.get("Job Freshness")),
                "eligibility": eligibility_reason,
                "source": "stored_posted_at",
            },
            tuple(degrade),
            opening_count,
            age,
        )

    # ---- T3: active req fallback ------------------------------------------
    return SignalResolution(
        TIER_3,
        SIGNAL_ACTIVE_REQ,
        {
            "open_role": _text(fields.get("Outbound Role")) or _text(fields.get("Open Role")),
            "job_url": _text(fields.get("Job URL")),
            "job_source": _text(fields.get("Job Source")),
            "eligibility": eligibility_reason,
            "source": "stored_active_requisition",
        },
        tuple(degrade),
        opening_count,
        age,
    )
