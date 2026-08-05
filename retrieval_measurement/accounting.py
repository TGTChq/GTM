"""Discard attribution, dual uniqueness, and the two baselines.

Two ideas carry this module.

**Attribution.** Production tallies its discard reasons globally
(``multi_source_acquisition.py:987-1009``), so "we lost 1,400 rows" can never be
traced to a source. ``attribute_discards`` runs the *same* loop, in the *same*
order, with the *same* semantics, but keyed by source. Critically, only three
of its counters actually remove a record -- ``missing_job_id``,
``previously_seen``, ``excluded_by_seniority``. ``role_reject`` and
``prefilter_rejected`` are annotations on records that are still kept
(``:1000``, ``:1008``, appended at ``:1009``). Treating those as removals is the
single easiest way to produce a funnel that does not add up, so they are
carried in a separate ``annotations`` map that no identity subtracts.

**Two uniqueness definitions.** Production's ``_dedupe`` collapses on
``(company_identity, normalized_title)``. That is right for the funnel -- one
outreach per company/role -- and wrong as an inventory estimate, because three
genuinely distinct openings for "Software Engineer" at one company become one.
So the harness computes both, and reports the gap (``collapse_delta``) rather
than picking a favourite.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from job_filter import assess_pre_enrichment_viability, dedup_key
from jsearch_scraper import is_excluded_title
from multi_source_acquisition import _classify, _dedupe

from retrieval_measurement.identity import ReadOnlySeenSnapshot
from retrieval_measurement.schema import (
    ANNOTATING_COUNTERS,
    REMOVING_DISCARD_REASONS,
    BaselineMetrics,
    DiscardRecord,
    TitleCoverage,
    UniquenessMetrics,
)

_WHITESPACE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# Posting identity
# --------------------------------------------------------------------------


def normalize_apply_url(value: Any) -> str:
    """Canonicalize an apply URL without discarding meaning.

    Host and scheme are lowercased and a trailing slash dropped; the query
    string is KEPT, because for most ATS providers the posting id lives there
    (``?gh_jid=...``). Stripping it would merge distinct postings and silently
    understate inventory.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    if not parts.netloc:
        return raw.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        path,
        parts.query,
        "",
    ))


def posting_identity(job: Mapping[str, Any]) -> Tuple[str, str]:
    """Strongest available identity for one posting.

    Ladder: provider job id -> canonical apply URL -> content digest. The
    strength label travels with the key so the report can show how much of the
    inventory number rests on the weakest rung.
    """
    job_id = str(job.get("job_id") or "").strip()
    if job_id:
        return "provider_job_id", job_id

    url = normalize_apply_url(job.get("canonical_source_url") or job.get("job_apply_link"))
    if url:
        return "apply_url", url

    parts = "|".join(
        _WHITESPACE.sub(" ", str(job.get(field) or "")).strip().lower()
        for field in ("_acquisition_source", "employer_name", "job_title", "job_location")
    )
    return "content_digest", hashlib.sha256(parts.encode("utf-8")).hexdigest()[:32]


def posting_identity_uniqueness(jobs: Sequence[Mapping[str, Any]]) -> Tuple[int, int, Dict[str, int]]:
    seen: set[str] = set()
    histogram: Dict[str, int] = {}
    duplicates = 0
    for job in jobs:
        strength, key = posting_identity(job)
        histogram[strength] = histogram.get(strength, 0) + 1
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return len(seen), duplicates, histogram


def production_equivalent_uniqueness(jobs: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Run production's own ``_dedupe`` -- behaviour unchanged.

    ``_dedupe`` mutates the dicts it is given (it writes ``_discovery_sources``
    and merges apply options), so it is fed copies. The harness measures
    production; it does not get to alter it.
    """
    return _dedupe([dict(job) for job in jobs])


def dual_uniqueness(jobs: Sequence[Mapping[str, Any]]) -> UniquenessMetrics:
    unique_posting, dup_posting, histogram = posting_identity_uniqueness(jobs)
    selected, dup_production = production_equivalent_uniqueness(jobs)
    return UniquenessMetrics(
        returned_total=len(jobs),
        unique_posting_identity=unique_posting,
        duplicates_posting_identity=dup_posting,
        unique_production_equivalent=len(selected),
        duplicates_production_equivalent=dup_production,
        collapse_delta=unique_posting - len(selected),
        identity_strength_histogram=histogram,
    )


# --------------------------------------------------------------------------
# Baselines
# --------------------------------------------------------------------------


def baselines(
    jobs: Sequence[Mapping[str, Any]],
    snapshot: Optional[ReadOnlySeenSnapshot] = None,
) -> Tuple[BaselineMetrics, BaselineMetrics]:
    """Gross and production-effective retrieval, for both uniqueness bases.

    Both are always returned. ``previously_seen`` is ``None`` -- not zero --
    when no snapshot was supplied, because "nothing was seen before" and "we do
    not know what was seen before" are different claims and only one of them is
    honest without a snapshot.
    """
    available = snapshot is not None and bool(getattr(snapshot, "path", ""))

    unique_posting, dup_posting, _ = posting_identity_uniqueness(jobs)
    posting_seen: Optional[int] = None
    if available:
        seen_keys: set[str] = set()
        for job in jobs:
            _strength, key = posting_identity(job)
            if key in seen_keys:
                continue
            seen_keys.add(key)
        posting_seen = sum(
            1 for key in seen_keys if snapshot.has_job_id(key)  # type: ignore[union-attr]
        )

    posting_baseline = BaselineMetrics(
        gross_returned=len(jobs),
        unique_in_run=unique_posting,
        previously_seen=posting_seen,
        incremental_new=(unique_posting - posting_seen) if posting_seen is not None else None,
        snapshot_available=available,
        basis="posting_identity",
    )

    selected, _dup = production_equivalent_uniqueness(jobs)
    production_seen: Optional[int] = None
    if available:
        production_seen = sum(
            1
            for job in selected
            if snapshot.has_dedup_key(dedup_key(dict(job)))  # type: ignore[union-attr]
        )

    production_baseline = BaselineMetrics(
        gross_returned=len(jobs),
        unique_in_run=len(selected),
        previously_seen=production_seen,
        incremental_new=(len(selected) - production_seen) if production_seen is not None else None,
        snapshot_available=available,
        basis="production_equivalent",
    )
    return posting_baseline, production_baseline


# --------------------------------------------------------------------------
# Discard attribution
# --------------------------------------------------------------------------


def attribute_discards(
    jobs: Iterable[Mapping[str, Any]],
    snapshot: Optional[ReadOnlySeenSnapshot] = None,
    *,
    source_of: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], List[DiscardRecord], Dict[str, Dict[str, int]]]:
    """Replay production's classification loop, attributed per source.

    Mirrors ``multi_source_acquisition.py:987-1009`` statement for statement,
    including the order of the three removing checks -- ``missing_job_id``
    before ``previously_seen`` before ``excluded_by_seniority`` -- because the
    order determines which counter a doubly-disqualified record lands in, and
    changing it would make these numbers non-comparable with production's.
    """
    resolve = source_of or (lambda job: str(job.get("_acquisition_source") or "unknown"))
    kept: List[Dict[str, Any]] = []
    per_source: Dict[str, Dict[str, int]] = {}

    def bucket(source: str) -> Dict[str, int]:
        if source not in per_source:
            per_source[source] = {reason: 0 for reason in REMOVING_DISCARD_REASONS}
            per_source[source].update({counter: 0 for counter in ANNOTATING_COUNTERS})
        return per_source[source]

    for original in jobs:
        job = dict(original)
        source = resolve(job)
        counters = bucket(source)

        if not str(job.get("job_id") or "").strip():
            counters["missing_job_id"] += 1
            continue
        if snapshot is not None and snapshot.has_job_id(str(job.get("job_id"))):
            counters["previously_seen"] += 1
            continue
        if is_excluded_title(str(job.get("job_title") or "")):
            counters["excluded_by_seniority"] += 1
            continue

        _classify(job)
        status = str(job.get("_role_relevance_status") or "reject")
        key = f"role_{status}"
        counters[key] = counters.get(key, 0) + 1

        assessment = assess_pre_enrichment_viability(job)
        job["_prefilter_viable"] = assessment.eligible
        job["_prefilter_stat"] = assessment.stat_name
        job["_prefilter_reason"] = assessment.reason
        if assessment.eligible and status in {"accept", "review"}:
            counters["prefilter_viable"] += 1
        elif status in {"accept", "review"}:
            counters["prefilter_rejected"] += 1

        # Kept regardless of role status -- exactly as production does at
        # multi_source_acquisition.py:1009.
        kept.append(job)

    records: List[DiscardRecord] = []
    for source in sorted(per_source):
        for reason, count in sorted(per_source[source].items()):
            records.append(
                DiscardRecord(
                    source=source,
                    reason=reason,
                    count=count,
                    removes_record=reason in REMOVING_DISCARD_REASONS,
                )
            )
    return kept, records, per_source


def split_counters(counters: Mapping[str, int]) -> Tuple[Dict[str, int], Dict[str, int]]:
    removals = {reason: int(counters.get(reason, 0)) for reason in REMOVING_DISCARD_REASONS}
    annotations = {name: int(counters.get(name, 0)) for name in ANNOTATING_COUNTERS}
    return removals, annotations


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


def title_coverage(jobs: Sequence[Mapping[str, Any]], titles: Sequence[str]) -> List[TitleCoverage]:
    """Which targeted titles the run actually reached, and via which sources.

    Titles with zero matches are kept in the output. A title that returned
    nothing is the most interesting row in the table.
    """
    tally: Dict[str, TitleCoverage] = {
        str(title): TitleCoverage(title=str(title)) for title in titles
    }
    for job in jobs:
        matched = str(job.get("_matched_role") or "").strip()
        if not matched or matched not in tally:
            continue
        entry = tally[matched]
        entry.matched_records += 1
        source = str(job.get("_acquisition_source") or "unknown")
        if source not in entry.sources:
            entry.sources.append(source)
    for entry in tally.values():
        entry.sources.sort()
    return [tally[key] for key in sorted(tally)]


def posted_at_bounds(jobs: Sequence[Mapping[str, Any]]) -> Tuple[str, str]:
    """First and last provider-declared posting date in the retrieved set.

    Recorded now because the typical-week protocol needs posting-date buckets,
    and the right-censoring at the window edges cannot be assessed after the
    fact if the bounds were never captured.
    """
    stamps = sorted(
        stamp for stamp in (
            str(job.get("job_posted_at_datetime_utc") or "").strip() for job in jobs
        ) if stamp
    )
    return (stamps[0], stamps[-1]) if stamps else ("", "")
