"""Policy-aligned acquisition filters and immutable pagination queries."""
from __future__ import annotations

from datetime import datetime, timedelta
import csv
import io
import re

# Exact, case-sensitive LinkedIn labels. No positive size/full-time/remote/title
# gates: those would silently discard allowed jobs with missing provider fields.
#
# This list is the PROVIDER-SIDE half of the excluded-industry policy, and it is the one
# place an allowed industry can be lost invisibly: a label sent here means the jobs are
# never acquired, so no downstream eligibility change can recover them. It must therefore
# stay a subset of `policy.requirements.EXCLUDED_INDUSTRIES` --
# `test_filters_only_exclude_existing_policy_industries_and_agencies` asserts exactly that,
# and it is what caught "Online Media"/"Media Production" still being sent after Luis ruled
# online news, digital media and media production ALLOWED (OPEN_DECISIONS.md Q6/D3).
EXCLUDED_LINKEDIN_INDUSTRIES = (
    "Staffing and Recruiting", "Government Administration",
    "Non-profit Organization Management", "Hospital & Health Care",
    "Hospitals and Health Care", "Mental Health Care", "Medical Practice",
    "Human Resources Services", "Outsourcing/Offshoring", "Events Services",
    "Broadcast Media", "Newspapers", "Book Publishing", "Chemicals",
)


def policy_filters() -> dict:
    return {"organization_agency": "exclude",
            "exclude_organization_industry": ",".join(EXCLUDED_LINKEDIN_INDUSTRIES)}


LEGACY_PROFILE = "legacy_v1"
PRIORITY_PROFILE = "priority_v1"
DISCOVERY_PROFILE = "discovery_v1"
#: Exhaustive nine-campaign scope (2026-09-20). Affirmative employer exclusions
#: only -- no positive gate that a null provider field would fail. PRIORITY
#: sends ai_employment_type, organization_headcount_* and ai_taxonomies_a, and a
#: row missing any of those fields is dropped by the provider before we ever see
#: it (Wellfound and YC rows carry no firmographics at all, so those gates drop
#: 100% of them). Measured: 3,399 of 5,285 unique jobs ended UNDECIDED, against
#: 1,088 hard rejects -- the undecided majority is what this profile reaches.
EXHAUSTIVE_PROFILE = "exhaustive_v1"
QUERY_PROFILES = (LEGACY_PROFILE, PRIORITY_PROFILE, DISCOVERY_PROFILE, EXHAUSTIVE_PROFILE)
# Retrieval priorities, NOT an eligibility taxonomy. Product/operations/ecommerce
# can appear under Management, Retail or Logistics. Match ANY assigned category,
# not just the primary one. Do not infer physical work from an industry/category.
PRIORITY_TAXONOMIES = (
    "Technology", "Software", "Data & Analytics", "Engineering",
    "Finance & Accounting", "Human Resources", "Sales", "Marketing",
    "Customer Service & Support", "Administrative", "Creative & Media",
    "Art & Design", "Management & Leadership", "Consulting", "Retail", "Logistics",
)


def validate_profile(profile: str) -> str:
    if profile not in QUERY_PROFILES:
        raise ValueError("unknown_acquisition_profile")
    return profile


def provider_list(values) -> str:
    """Quote individual comma-containing values; HTTP encoding is the client's job."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(values)
    return buf.getvalue()


def profile_filters(profile: str = LEGACY_PROFILE) -> dict:
    """Versioned retrieval scope. Neither path requires a particular job title.

    Discovery relaxes ALL priority gates, not only taxonomies: this is essential
    for unknown headcounts and missing/mistagged employment data. It remains in
    the same business market and retains explicit employer-sector policy.
    """
    from ..policy.requirements import rule
    validate_profile(profile)
    params = policy_filters()
    if profile == PRIORITY_PROFILE:
        params.update(ai_employment_type="FULL_TIME",
                      organization_headcount_gte=int(rule("min_employees")),
                      organization_headcount_lt=int(rule("max_employees")) + 1,
                      ai_taxonomies_a=provider_list(PRIORITY_TAXONOMIES))
    if profile == EXHAUSTIVE_PROFILE:
        # Drops the two FIRMOGRAPHIC gates, which are the measured loss: a
        # Wellfound or YC row carries no headcount and no employment type, so
        # a range or an equality on those fields discards 100% of them.
        #
        # KEEPS the taxonomy filter. Measured in the first production canary:
        # removing it did not widen the knowledge-work universe, it imported
        # the frontline labour market -- Usher, Ramp Agent, Car Wash Associate,
        # Bartender, Teller, CDL-A Dump Truck Driver, Prepared Foods Cook --
        # and each of those costs a credit to buy and then rejects. Selecting
        # the professional labour market affirmatively, before a credit is
        # spent, is both cheaper and more accurate than a blocklist of titles.
        # A row with a NULL taxonomy is not lost: the discovery slot in
        # `balanced_slots` buys unfiltered.
        params.update(ai_taxonomies_a=provider_list(PRIORITY_TAXONOMIES))
    return params


def count_query(endpoint: str, params: dict) -> tuple[str, dict]:
    """Pure preflight builder. Does NOT execute a count or spend request credits."""
    if endpoint not in ("/v1/active-ats", "/v1/active-jb"):
        raise ValueError("unsupported_count_endpoint")
    strip = {"endpoint", "limit", "offset", "cursor", "id", "description_format"}
    strip |= ({"include_basic_organization_details"} if endpoint.endswith("-ats")
              else {"exclude_recruiter_fields", "employment_type"})
    return endpoint + "-count", {k: v for k, v in params.items() if k not in strip}


def balanced_slots(sources: tuple[str, ...], pages: int, env=None):
    """80/20 REQUEST SLOTS (not a dollar/row percentage), round-robin feeds.

    A discovery slot arrives third so small budgets can observe both paths. Empty
    or blocked slots aren't transferred to another scope behind the user's back.
    """
    if not sources or len(set(sources)) != len(sources) or not 5 * len(sources) <= pages <= 100:
        raise ValueError("balanced_acquisition_requires_5_slots_per_source_up_to_100")
    # Measured, first full production run (2026-09-21), eligible contacts per
    # billed record: priority 0.315, discovery 0.108, exhaustive 0.077. The
    # widened arm was dominated by both, so the exhaustive flag no longer
    # changes acquisition. EXHAUSTIVE_PROFILE stays defined for a future
    # measured arm; `env` is accepted and deliberately unused.
    del env
    for slot in range(pages):
        yield sources[slot % len(sources)], (DISCOVERY_PROFILE if slot % 5 == 2 else PRIORITY_PROFILE)


def balanced_slot(sources: tuple[str, ...], index: int):
    """Slot ``index`` of the same 4 priority : 1 discovery round-robin as
    ``balanced_slots``, for acquisition in small blocks: a run that buys two pages at
    a time continues the pattern across blocks instead of restarting it."""
    if not sources or index < 0:
        raise ValueError("balanced_slot_requires_sources_and_a_non_negative_index")
    return sources[index % len(sources)], (DISCOVERY_PROFILE if index % 5 == 2 else PRIORITY_PROFILE)


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
