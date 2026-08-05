"""Lane drivers and run assembly.

Each acquisition lane is invoked through its existing production entry point,
with the measuring fetcher supplied at the seam. The harness plans nothing that
production already plans: query portfolios, pagination, board rotation and stop
conditions all stay where they are. That is deliberate -- a second
implementation of retrieval would be a second thing to be wrong.

Modes
-----
``fixture``
    Adapters run against recorded HTTP payloads. Exercises transport,
    denominators, truncation and per-source attribution end to end.
``offline_replay``
    Real production raw corpora are replayed as already-canonical records.
    Exercises reconciliation, dual uniqueness and determinism at real scale,
    but NOT the adapters or the transport -- the corpora are post-adapter
    output. Reported as such; never presented as an adapter test.
``live_acquisition``
    Real providers. Implemented, and not executed in Milestone 1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import config
from adzuna_client import AdzunaAdapter
from ats_board_registry import fetch_board_jobs
from free_job_sources import FetchPayload, SourceResult, build_adapters

from retrieval_measurement.accounting import (
    attribute_discards,
    baselines,
    dual_uniqueness,
    posted_at_bounds,
    split_counters,
    title_coverage,
)
from retrieval_measurement.identity import ReadOnlySeenSnapshot
from retrieval_measurement.instrument import (
    MeasuringFetcher,
    classify_truncation,
)
from retrieval_measurement.schema import (
    BoardMetrics,
    CoverageSummary,
    SourceMetrics,
    reconcile_run,
)

FREE_LANE = "free_feed"
ADZUNA_LANE = "adzuna"
ATS_LANE = "ats_board"
JSEARCH_LANE = "jsearch"


@dataclass
class LaneOutput:
    source: str
    lane: str
    result: SourceResult
    scope_key: str = ""
    boards: List[BoardMetrics] = field(default_factory=list)
    board_results: List[Any] = field(default_factory=list)
    partial: bool = False
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Fixture transport
# --------------------------------------------------------------------------


class FixtureFetcher:
    """Serve recorded HTTP payloads, matched by URL prefix.

    Deliberately strict: an unmatched URL yields a 599 with an explicit error
    rather than an empty 200. A fixture gap must look like a failure, not like
    a provider that had nothing.
    """

    def __init__(self, payloads: Mapping[str, Any]) -> None:
        self.payloads = dict(payloads)
        self.unmatched: List[str] = []

    @staticmethod
    def key_for(url: str, params: Optional[Mapping[str, Any]]) -> str:
        """Fixtures may be keyed with query parameters so that paginated
        adapters see genuinely different pages. Without this, a fixture serves
        page 1 forever and an adapter that paginates by offset looks like it
        retrieved 25 identical pages."""
        if not params:
            return str(url)
        encoded = "&".join(f"{name}={params[name]}" for name in sorted(params, key=str))
        return f"{url}?{encoded}"

    def _match(self, url: str, params: Optional[Mapping[str, Any]]) -> Optional[Any]:
        keyed = self.key_for(url, params)
        if keyed in self.payloads:
            return self.payloads[keyed]
        if url in self.payloads:
            return self.payloads[url]
        for prefix, payload in self.payloads.items():
            if "?" not in prefix and url.startswith(prefix):
                return payload
        return None

    def __call__(self, url: str, **kwargs: Any) -> FetchPayload:
        payload = self._match(str(url), kwargs.get("params"))
        if payload is None:
            self.unmatched.append(str(url))
            return FetchPayload(status_code=599, url=str(url), error="fixture_not_found")
        if isinstance(payload, FetchPayload):
            return payload
        if isinstance(payload, (dict, list)):
            return FetchPayload(status_code=200, url=str(url), text=json.dumps(payload))
        return FetchPayload(status_code=200, url=str(url), text=str(payload))


# --------------------------------------------------------------------------
# Lanes
# --------------------------------------------------------------------------


def run_free_source_lane(
    fetcher: MeasuringFetcher,
    sources: Optional[Sequence[str]] = None,
) -> List[LaneOutput]:
    names = list(sources if sources is not None else config.FREE_JOB_SOURCES)
    outputs: List[LaneOutput] = []
    for adapter in build_adapters(names):
        with fetcher.context(source=adapter.name):
            result = adapter.fetch(fetcher)
        outputs.append(LaneOutput(source=adapter.name, lane=FREE_LANE, result=result))
    return outputs


def run_adzuna_lane(fetcher: MeasuringFetcher) -> List[LaneOutput]:
    adapter = AdzunaAdapter()
    with fetcher.context(source="adzuna"):
        result = adapter.fetch(fetcher)
    return [LaneOutput(source="adzuna", lane=ADZUNA_LANE, result=result)]


def load_boards_readonly(path: str | Path, limit: Optional[int] = None) -> Tuple[List[Dict[str, Any]], str]:
    """Read the ATS board registry without constructing ``AtsBoardRegistry``.

    The registry class owns write paths and rotation state. The harness only
    needs the board list, so it reads the file directly and refuses to guess if
    the shape is unfamiliar.
    """
    target = Path(path)
    if not target.is_file():
        return [], f"board_registry_not_found:{target}"
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"board_registry_unreadable:{exc}"

    rows: Any = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("entries", "boards", "registry"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
            if isinstance(candidate, dict):
                rows = list(candidate.values())
                break
    if rows is None:
        return [], "board_registry_unrecognized_shape"

    boards = [
        row for row in rows
        if isinstance(row, Mapping)
        and str(row.get("provider") or "").strip()
        and str(row.get("identifier") or "").strip()
    ]
    cap = int(limit if limit is not None else config.ATS_MAX_BOARDS_PER_RUN)
    return [dict(board) for board in boards[:max(0, cap)]], ""


def run_ats_lane(
    fetcher: MeasuringFetcher,
    boards: Sequence[Mapping[str, Any]],
    *,
    budget: Optional[Any] = None,
    checkpoint_dir: Optional[str | Path] = None,
    on_board: Optional[Callable[[Any], None]] = None,
) -> List[LaneOutput]:
    """One LaneOutput per ATS provider, with per-board detail retained.

    Every board is persisted the moment it completes. Run
    20260805T015708Z-1ad3ef58 spent 968 requests across 30 boards and recorded
    NOTHING, because the lane raised on board 31 and the whole accumulated list
    went with it. Work that has already been paid for is now written to disk
    before the next board is attempted, so a later failure cannot retract it.

    A board-scoped or provider-scoped budget stop ends that scope only; the loop
    continues to the next board. Only a run-scoped stop aborts the lane, and it
    aborts it by raising after everything finished so far is already durable.
    """
    from retrieval_measurement.identity import utc_stamp
    from retrieval_measurement.instrument import RequestCeilingReached
    from retrieval_measurement.schema import BoardResult

    by_provider: Dict[str, SourceResult] = {}
    board_metrics: Dict[str, List[BoardMetrics]] = {}
    persisted: List[BoardResult] = []
    checkpoints = Path(checkpoint_dir) if checkpoint_dir else None
    if checkpoints is not None:
        checkpoints.mkdir(parents=True, exist_ok=True)
    exhausted_providers: set[str] = set()
    run_stop: Optional[BaseException] = None

    for board in boards:
        provider = str(board.get("provider") or "unknown")
        source = f"ats_{provider}"
        identifier = str(board.get("identifier") or "")
        board_key = f"{provider}:{identifier}"
        result = by_provider.setdefault(source, SourceResult(source=source))
        metrics = board_metrics.setdefault(source, [])
        if source in exhausted_providers:
            continue

        started = utc_stamp()
        before_requests = len(fetcher.requests)
        before_physical = getattr(budget, "count", 0) if budget is not None else 0
        jobs: List[Dict[str, Any]] = []
        error = ""
        stop_reason = ""
        try:
            if budget is not None:
                with budget.context(lane=ATS_LANE, source=source, board=board_key):
                    with fetcher.context(source=source, board_key=board_key):
                        jobs, error = fetch_board_jobs(dict(board), fetcher)
            else:
                with fetcher.context(source=source, board_key=board_key):
                    jobs, error = fetch_board_jobs(dict(board), fetcher)
        except RequestCeilingReached as exc:
            scope = (getattr(budget, "blocked_next_request", None) or {}).get("scope", "run")
            stop_reason = f"budget_exhausted:{scope}"
            error = stop_reason
            if scope == "provider":
                exhausted_providers.add(source)
            elif scope in ("run", "lane", "lane_reservation"):
                run_stop = exc
        except Exception as exc:  # noqa: BLE001 - one board must not cost the rest
            stop_reason = "board_error"
            error = f"{type(exc).__name__}: {exc}"

        seam_used = len(fetcher.requests) - before_requests
        physical = (getattr(budget, "count", 0) - before_physical) if budget is not None else seam_used

        result.requests_attempted += seam_used
        result.pages += seam_used
        if error:
            result.errors.append(f"{board_key}:{error}")
        else:
            result.requests_succeeded += seam_used
        result.raw_records += len(jobs)
        result.jobs.extend(jobs)
        metrics.append(BoardMetrics(
            provider=provider,
            identifier=identifier,
            company_name=str(board.get("company_name") or ""),
            requests=seam_used,
            canonical_records=len(jobs),
            error=error,
        ))

        record = BoardResult(
            provider=provider,
            identifier=identifier,
            company_name=str(board.get("company_name") or ""),
            started_at=started,
            completed_at=utc_stamp(),
            physical_requests=physical,
            pages=seam_used,
            # Redirects and retries happen below the seam; the gap between the
            # physical count and the seam count is exactly their number.
            redirects=max(0, physical - seam_used),
            retries=0,
            listing_records=len(jobs),
            detail_records=max(0, physical - seam_used),
            canonical_records=len(jobs),
            stop_reason=stop_reason,
            error=error,
        )
        if jobs:
            uniqueness = dual_uniqueness(jobs)
            record.unique_posting_identity = uniqueness.unique_posting_identity
            record.unique_production_equivalent = uniqueness.unique_production_equivalent
        if checkpoints is not None:
            target = checkpoints / f"{provider}__{identifier or 'unknown'}.json".replace("/", "_")
            payload = {"board": record.to_dict(), "jobs": jobs}
            temp = target.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            import os as _os
            _os.replace(temp, target)
            record.checkpoint_path = str(target)
        persisted.append(record)
        if on_board is not None:
            on_board(record)
        if run_stop is not None:
            break

    outputs: List[LaneOutput] = []
    for source in sorted(by_provider):
        result = by_provider[source]
        result.success = bool(result.jobs) or not result.errors
        outputs.append(LaneOutput(
            source=source,
            lane=ATS_LANE,
            result=result,
            boards=board_metrics.get(source, []),
            board_results=[r for r in persisted if f"ats_{r.provider}" == source],
            partial=run_stop is not None or bool(exhausted_providers),
        ))
    if run_stop is not None:
        # Everything above is already durable and already returned to the caller
        # via the partial-results hook, so raising here cannot retract it.
        raise PartialAtsLane(outputs, run_stop)
    return outputs


class PartialAtsLane(Exception):
    """Carries completed board work out with the exception that ended the lane."""

    def __init__(self, outputs: Sequence[LaneOutput], cause: BaseException) -> None:
        super().__init__(str(cause))
        self.outputs = list(outputs)
        self.cause = cause


def run_jsearch_lane(
    transport: Callable[..., Any],
    *,
    output_dir: str,
    search_roles: Optional[Sequence[str]] = None,
    max_queries: Optional[int] = None,
    registry: Optional[Any] = None,
) -> List[LaneOutput]:
    """Drive the real JSearch lane through ``run_daily_scrape``.

    ``run_daily_scrape`` calls ``validate_preflight()``, which requires a live
    RAPIDAPI key, so this path belongs to live acquisition only. Offline modes
    do not fabricate a credential to get around it, and the report states
    plainly that JSearch query planning was not exercised offline.

    ``registry`` is REQUIRED in practice and defaults to a fresh
    ``NonWritingRegistry``. Omitting it would let ``run_daily_scrape`` fall
    through to ``registry or SeenJobsRegistry()`` and construct the production
    registry, which creates ``data/state/`` and can move a corrupt seen-jobs
    file aside. The harness never permits that branch to be taken.
    """
    if max_queries is not None and int(max_queries) == 0:
        # Zero means zero. Passing 0 down would hit
        # `planned_roles[:effective_max] if effective_max else planned_roles`
        # (jsearch_scraper.py:640), where 0 is falsy and therefore means ALL
        # roles -- the largest possible run from the safest-looking argument.
        # Nothing is imported and no request is made.
        return [LaneOutput(
            source="jsearch",
            lane=JSEARCH_LANE,
            result=SourceResult(source="jsearch", success=True),
            notes=["jsearch lane skipped: --max-queries 0 means zero JSearch queries"],
        )]

    from jsearch_scraper import run_daily_scrape  # imported late: live-only path

    from retrieval_measurement.identity import NonWritingRegistry

    scrape = run_daily_scrape(
        registry if registry is not None else NonWritingRegistry(),
        search_roles=list(search_roles) if search_roles is not None else None,
        max_queries=max_queries,
        transport=transport,
        output_dir=output_dir,
    )
    stats = scrape.stats or {}
    result = SourceResult(
        source="jsearch",
        jobs=list(_load_scraped_jobs(scrape.output_path)),
        requests_attempted=int(stats.get("queries_attempted", 0)),
        requests_succeeded=int(stats.get("queries_succeeded", 0)),
        raw_records=int(stats.get("total_raw_jobs", 0) or 0),
        pages=int(stats.get("queries_attempted", 0)),
        success=bool(scrape.success),
        errors=list(scrape.errors or []),
        metadata={"stats": stats},
    )
    notes: List[str] = []
    if not result.raw_records:
        # total_raw_jobs is never assigned anywhere in the production codebase
        # (read at multi_source_acquisition.py:678). The harness reports the
        # gap instead of quietly substituting the post-filter count.
        result.raw_records = len(result.jobs)
        notes.append(
            "jsearch stats.total_raw_jobs was absent or zero; provider_rows fell back "
            "to selected-job count, so JSearch pre-filter loss is NOT measurable from "
            "this lane's own stats."
        )
    return [LaneOutput(source="jsearch", lane=JSEARCH_LANE, result=result, notes=notes)]


def _load_scraped_jobs(path: str) -> List[Dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    with open(target, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("jobs") if isinstance(payload, dict) else payload
    return [dict(row) for row in rows or [] if isinstance(row, Mapping)]


def replay_corpus_lane(paths: Sequence[str | Path]) -> List[LaneOutput]:
    """Replay real production raw artifacts as already-canonical records.

    Every locally available raw corpus predates multi-source acquisition, so
    records carry ``_acquisition_source: None``. Rather than invent a source,
    such records are attributed to ``jsearch_legacy`` and the report says why.
    """
    grouped: Dict[str, SourceResult] = {}
    notes: List[str] = []
    for raw_path in paths:
        target = Path(raw_path)
        if not target.is_file():
            notes.append(f"corpus_missing:{target}")
            continue
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("jobs") if isinstance(payload, dict) else payload
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            source = str(row.get("_acquisition_source") or "").strip() or "jsearch_legacy"
            result = grouped.setdefault(source, SourceResult(source=source))
            result.jobs.append(dict(row))
            result.raw_records += 1
    if "jsearch_legacy" in grouped:
        notes.append(
            "Records without _acquisition_source were attributed to 'jsearch_legacy': "
            "every local raw corpus predates multi-source acquisition."
        )
    return [
        LaneOutput(source=source, lane="replay", result=grouped[source], notes=list(notes))
        for source in sorted(grouped)
    ]


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def assemble(
    outputs: Sequence[LaneOutput],
    fetcher: Optional[MeasuringFetcher],
    snapshot: Optional[ReadOnlySeenSnapshot],
    *,
    run_id: str,
    mode: str,
    titles: Optional[Sequence[str]] = None,
) -> Tuple[List[SourceMetrics], List[Dict[str, Any]], CoverageSummary]:
    """Turn lane outputs into per-source metrics, then reconcile the whole run."""
    per_source: List[SourceMetrics] = []
    all_kept: List[Dict[str, Any]] = []
    notes: List[str] = []

    for output in outputs:
        result = output.result
        notes.extend(output.notes)
        gross = list(result.jobs)

        kept, _records, counters = attribute_discards(
            gross, snapshot, source_of=lambda _job, name=output.source: name
        )
        removals, annotations = split_counters(counters.get(output.source, {}))
        uniqueness = dual_uniqueness(kept)
        posting_baseline, production_baseline = baselines(gross, snapshot)

        denominator = fetcher.denominator_for(output.source) if fetcher else None
        truncation = classify_truncation(
            output.source, result, denominator=denominator, scope_key=output.scope_key
        )

        capture_rate: Optional[float] = None
        capture_basis = "unavailable"
        if denominator and denominator.value > 0:
            # Rate against the PROVIDER's own declared total for the scope we
            # queried. Never against an invented market size.
            capture_rate = round(min(1.0, len(gross) / denominator.value), 4)
            capture_basis = f"{denominator.field_name}@{denominator.scope}"

        first_posted, last_posted = posted_at_bounds(gross)
        per_source.append(SourceMetrics(
            source=output.source,
            lane=output.lane,
            success=bool(result.success),
            requests_attempted=int(result.requests_attempted),
            requests_succeeded=int(result.requests_succeeded),
            pages=int(result.pages),
            provider_rows=int(result.raw_records),
            canonical_records=len(gross),
            adapter_dropped_or_capped=int(result.raw_records) - len(gross),
            kept_after_removals=len(kept),
            removals=removals,
            annotations=annotations,
            uniqueness=uniqueness.to_dict(),
            baseline_posting_identity=posting_baseline.to_dict(),
            baseline_production_equivalent=production_baseline.to_dict(),
            denominator=denominator.to_dict() if denominator else None,
            capture_rate=capture_rate,
            capture_rate_basis=capture_basis,
            truncation=[record.to_dict() for record in truncation],
            errors=list(result.errors),
            first_posted_at=first_posted,
            last_posted_at=last_posted,
        ))
        all_kept.extend(kept)

    run_uniqueness = dual_uniqueness(all_kept)
    run_posting_baseline, run_production_baseline = baselines(
        [job for output in outputs for job in output.result.jobs], snapshot
    )
    reconciliation = reconcile_run(per_source, run_uniqueness, len(all_kept))

    with_denominator = sorted(m.source for m in per_source if m.denominator)
    without_denominator = sorted(m.source for m in per_source if not m.denominator)

    summary = CoverageSummary(
        run_id=run_id,
        mode=mode,
        run_baseline_posting_identity=run_posting_baseline.to_dict(),
        run_baseline_production_equivalent=run_production_baseline.to_dict(),
        per_source=[metric.to_dict() for metric in per_source],
        sources_with_denominator=with_denominator,
        sources_without_denominator=without_denominator,
        reconciliation=reconciliation.to_dict(),
        title_coverage=[
            entry.to_dict()
            for entry in title_coverage(all_kept, list(titles or config.ROLES))
        ],
        notes=notes,
    )
    summary.notes.append(
        f"cross-source uniqueness of the {len(all_kept)} records that survived the "
        f"removing discards: {run_uniqueness.unique_posting_identity} distinct postings "
        f"collapse to {run_uniqueness.unique_production_equivalent} company+title rows "
        f"(delta {run_uniqueness.collapse_delta}). The run totals table above is "
        "measured over gross returned records, before removals."
    )
    return per_source, all_kept, summary
