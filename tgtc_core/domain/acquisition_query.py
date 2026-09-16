"""Policy-aligned acquisition filters and immutable pagination queries."""
from __future__ import annotations

from datetime import datetime, timedelta
import re

# Exact, case-sensitive LinkedIn labels. No positive size/full-time/remote/title
# gates: those would silently discard allowed jobs with missing provider fields.
EXCLUDED_LINKEDIN_INDUSTRIES = (
    "Staffing and Recruiting", "Government Administration",
    "Non-profit Organization Management", "Hospital & Health Care",
    "Hospitals and Health Care", "Mental Health Care", "Medical Practice",
    "Human Resources Services", "Outsourcing/Offshoring", "Events Services",
    "Broadcast Media", "Online Media", "Media Production", "Newspapers",
    "Book Publishing", "Chemicals",
)


def policy_filters() -> dict:
    return {"organization_agency": "exclude",
            "exclude_organization_industry": ",".join(EXCLUDED_LINKEDIN_INDUSTRIES)}


def recent_size_exclusions(employers: list[dict], *, now: datetime, minimum: int, maximum: int) -> list[str]:
    """Exclude exact known companies, never every job with an unknown headcount.

    Expire this acquisition optimization after seven days so a stale company
    estimate does not become a permanent blacklist. Query snapshots pin the list
    for the life of an existing partition.
    """
    found = set()
    for employer in employers:
        count, observed = employer.get("employee_count"), employer.get("created_at")
        slug = str(employer.get("linkedin_slug") or "")
        if (type(count) is int and (count < minimum or count > maximum)
                and isinstance(observed, datetime) and timedelta() <= now - observed < timedelta(days=7)
                and re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", slug)):
            found.add(slug)
    return sorted(found)[:100]


def covering_time_frame(preferred: str, lower: datetime, upper: datetime, now: datetime) -> str:
    """Choose a larger documented feed only BEFORE a partition's first request."""
    for frame in dict.fromkeys((preferred, "7d", "6m")):
        if feed_covers_window({"time_frame": frame}, lower, upper, now):
            return frame
    return preferred  # caller records a coverage stop; never pretends empty means complete


def frozen_page_query(proposed: dict, previous: dict | None, *, offset: int, limit: int) -> dict:
    """Resume the same result set. Only page size/offset may change.

    A missing historical query at a nonzero offset cannot be safely guessed.
    The first intended/uncertain/served request pins the query, including old
    unfiltered partitions. Definite failed/refused requests are not anchors.
    """
    if offset and not previous:
        raise ValueError("partition_query_history_missing")
    query = dict(previous if previous is not None else proposed)
    if not query.get("endpoint") or not query.get("time_frame"):
        raise ValueError("partition_query_history_incomplete")
    if "cursor" in query:
        raise ValueError("partition_cursor_mode_unsupported")
    query.update(offset=offset, limit=limit)
    return query


def feed_covers_window(query: dict, lower: datetime, upper: datetime, now: datetime) -> bool:
    # Never label a historical partition complete after its rows age out of the
    # moving provider feed. Replanning needs a distinct backfill, not a reset.
    frame = query.get("time_frame")
    if frame in {"1h", "24h"}:
        end = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        start = end - timedelta(hours=1 if frame == "1h" else 24)
        return start <= lower < upper <= end
    duration = {"7d": timedelta(days=7), "6m": timedelta(days=180)}.get(frame)
    return (duration is not None and now - lower < duration
            and lower < upper <= now - timedelta(hours=1))
