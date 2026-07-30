"""Tiered job-freshness policy beyond the base 0-14/15-30 day window.

A single hard age cutoff cannot distinguish "too old to bother with" from
"old but demonstrably still an active, worthwhile opening." This module gives
every job an explicit tier and, for the two extended tiers, an explicit
eligibility check based on evidence already carried on the job record --
it does not perform any new live lookups.

Tiers (spec: FINAL_30_PLUS_SYSTEM_SPEC.md section 7):
  0-14   -- primary window, no extra evidence required.
  15-30  -- standard recovery window, no extra evidence required.
  31-60  -- extended: requires confirmed-active evidence.
  61-90  -- deep: requires confirmed-active evidence AND a difficult-to-fill
            signal (role or repost pattern), since age alone stops being a
            reliable proxy for a genuinely open, hard-to-source role.
  90+    -- excluded by default; eligible only with recent-refresh/repost
            evidence, which is a materially different signal from "active."
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import config

TIER_PRIMARY = "0-14"
TIER_RECOVERY = "15-30"
TIER_EXTENDED = "31-60"
TIER_DEEP = "61-90"
TIER_BEYOND = "90+"

_TIERS_REQUIRING_EVIDENCE = {TIER_EXTENDED, TIER_DEEP, TIER_BEYOND}

_DIFFICULT_TITLE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in config.DIFFICULT_TO_FILL_TITLE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def classify_age_tier(age_days: Optional[int]) -> str:
    if age_days is None:
        return TIER_PRIMARY
    age_days = int(age_days)
    if age_days <= config.PRIMARY_MAX_JOB_AGE_DAYS:
        return TIER_PRIMARY
    if age_days <= config.RECOVERY_MAX_JOB_AGE_DAYS:
        return TIER_RECOVERY
    if age_days <= config.RECOVERY_EXTENDED_MAX_JOB_AGE_DAYS:
        return TIER_EXTENDED
    if age_days <= config.RECOVERY_DEEP_MAX_JOB_AGE_DAYS:
        return TIER_DEEP
    return TIER_BEYOND


def tier_max_age_days(tier: str) -> float:
    """Ceiling used to decide whether a *stored* lead has aged out of a tier.

    TIER_BEYOND has no meaningful age ceiling of its own -- eligibility there
    comes from repost/refresh recency, not raw age -- so it is given an
    effectively unbounded ceiling and left to TTL-based expiry instead.
    """
    return {
        # MAX_JOB_AGE_DAYS (not PRIMARY_MAX_JOB_AGE_DAYS) is deliberate here:
        # it is the long-standing compatibility alias existing tests and
        # callers override directly for the primary tier.
        TIER_PRIMARY: float(config.MAX_JOB_AGE_DAYS),
        TIER_RECOVERY: float(config.RECOVERY_MAX_JOB_AGE_DAYS),
        TIER_EXTENDED: float(config.RECOVERY_EXTENDED_MAX_JOB_AGE_DAYS),
        TIER_DEEP: float(config.RECOVERY_DEEP_MAX_JOB_AGE_DAYS),
        TIER_BEYOND: float("inf"),
    }.get(tier, float(config.MAX_JOB_AGE_DAYS))


def is_source_confirmed_active(job: Dict) -> bool:
    """Reuse the same conservative "this listing is live right now" evidence
    already trusted elsewhere in this codebase (the Greenhouse
    reviewable-active-listing check in job_filter.is_stale_job), rather than
    inventing a new trust signal.
    """
    if job.get("_ats_board_identity_verified") is True:
        return True
    return bool(
        job.get("job_apply_is_direct") is True
        and job.get("_provider_record_structured") is True
        and str(job.get("job_apply_link") or job.get("official_job_url") or "").strip()
    )


def has_difficult_to_fill_signal(job: Dict) -> bool:
    title = str(job.get("job_title") or "")
    if _DIFFICULT_TITLE_RE.search(title):
        return True
    return bool(job.get("_repeated_posting_signal") or job.get("_multi_location_signal"))


def _parse(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_recently_refreshed_or_reposted(job: Dict, *, now: Optional[datetime] = None) -> bool:
    current = now or datetime.now(timezone.utc)
    updated = _parse(job.get("_ats_source_updated_at")) or _parse(job.get("job_posted_at_datetime_utc"))
    if not updated:
        return False
    return (current - updated) <= timedelta(days=config.PRIMARY_MAX_JOB_AGE_DAYS)


def is_age_tier_eligible(tier: str, job: Dict) -> Tuple[bool, str]:
    """Return (eligible, reason_if_rejected). Empty reason means eligible."""
    if tier not in _TIERS_REQUIRING_EVIDENCE:
        return True, ""
    active = is_source_confirmed_active(job)
    if tier == TIER_EXTENDED:
        return (True, "") if active else (False, "tier_31_60_requires_active_evidence")
    if tier == TIER_DEEP:
        if not active:
            return False, "tier_61_90_requires_active_evidence"
        if not has_difficult_to_fill_signal(job):
            return False, "tier_61_90_requires_difficult_to_fill_signal"
        return True, ""
    if tier == TIER_BEYOND:
        if not is_recently_refreshed_or_reposted(job):
            return False, "tier_90_plus_excluded_by_default"
        return True, ""
    return True, ""
