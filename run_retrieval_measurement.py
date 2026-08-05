#!/usr/bin/env python
"""Retrieval Measurement Harness -- Milestone 1 candidate.

Measures one acquisition run and reports, per source: how much was returned,
how much is unique (two ways), how much was already seen, how much is
genuinely new, and -- only where the provider publishes its own total -- what
share of that provider's inventory we retrieved.

It does not estimate the total US market. That number is not derivable from
anything available here, and a fabricated denominator would make every capture
rate meaningless.

Usage
-----
    python run_retrieval_measurement.py --mode fixture --fixture tests/fixtures/...
    python run_retrieval_measurement.py --mode offline_replay --corpus PATH [--corpus PATH]
    python run_retrieval_measurement.py --mode live_acquisition --lanes free_feeds
    python run_retrieval_measurement.py --retention-report

Exit status
-----------
``0``   every selected lane completed and every identity holds
``1``   a reconciliation identity failed, a parity check failed, or a selected
        lane raised -- partial artifacts are still written
``2``   the run was refused before any outbound request (bad arguments, no lane
        selected in live mode, or a selected lane missing a prerequisite)

A run that cannot account for every record it saw is not a measurement and must
not be quoted as one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import config
from free_job_sources import build_adapters

from retrieval_measurement import DeliveryImportGuard
from retrieval_measurement.accounting import posting_identity
from retrieval_measurement.artifacts import (
    evaluate_retention,
    run_dir,
    write_run_artifacts,
)
from retrieval_measurement.drivers import (
    FixtureFetcher,
    assemble,
    load_boards_readonly,
    replay_corpus_lane,
    run_adzuna_lane,
    run_ats_lane,
    run_free_source_lane,
    run_jsearch_lane,
)
from retrieval_measurement.identity import (
    NonWritingRegistry,
    ReadOnlySeenSnapshot,
    credential_state,
    redact_text,
    run_identity,
    utc_stamp,
)
from retrieval_measurement.instrument import (
    JSearchTransport,
    MeasuringFetcher,
    RequestBudget,
    RequestCeilingReached,
)
from retrieval_measurement.schema import (
    CLAIM_BOUNDARY,
    LANES,
    CredentialStatus,
    LaneFailure,
    LanePreflight,
    ParityCheck,
    RunManifest,
)

MODES = ("fixture", "offline_replay", "live_acquisition")

#: Credentials each lane genuinely needs before it can make a request. Only
#: SELECTED lanes are checked -- a run that never touches JSearch has no
#: business demanding a JSearch key.
LANE_CREDENTIALS: Dict[str, tuple] = {
    "free_feeds": (),            # public, unauthenticated endpoints
    "jsearch": ("RAPIDAPI_KEY",),
    "adzuna": ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"),
    "ats": (),                   # public board APIs
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_retrieval_measurement",
        description="Measure one acquisition run. Read-only with respect to production state.",
    )
    parser.add_argument("--mode", choices=MODES, default="fixture")
    parser.add_argument(
        "--fixture",
        help="JSON file mapping URL prefixes to recorded provider payloads (fixture mode).",
    )
    parser.add_argument(
        "--corpus",
        action="append",
        default=[],
        help="Raw job artifact to replay (offline_replay mode). Repeatable.",
    )
    parser.add_argument(
        "--seen-snapshot",
        help="Read-only seen-jobs snapshot. Never opened for writing; never modified.",
    )
    parser.add_argument(
        "--allow-production-snapshot-copy",
        action="store_true",
        help="Permit a snapshot path inside a production state directory. Only for a "
             "path that is already a copy.",
    )
    parser.add_argument("--artifact-root", default=str(config.ARTIFACT_ROOT))
    parser.add_argument(
        "--lanes",
        help="Comma-separated acquisition lanes: " + ", ".join(LANES) + ". "
             "REQUIRED in live_acquisition mode -- no lane is ever enabled "
             "implicitly there. Optional offline, where it narrows the default.",
    )
    parser.add_argument("--sources", help="Comma-separated free source subset.")
    parser.add_argument("--boards", help="ATS board registry JSON to read (read-only).")
    parser.add_argument(
        "--max-queries",
        type=int,
        default=None,
        help="Cap JSearch queries. 0 means ZERO queries (the lane is skipped "
             "entirely); N>0 means at most N; omitted keeps the configured default.",
    )
    parser.add_argument(
        "--retention-report",
        action="store_true",
        help="Report what retention would remove, then exit. Deletes nothing.",
    )
    parser.add_argument(
        "--retention-apply",
        action="store_true",
        help="Actually delete retention candidates inside the harness artifact root. "
             "Off by default; never touches production directories.",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Global ceiling on outbound HTTP requests for the whole run, across "
             "every lane, provider and transport, counting redirects and retries. "
             "The Nth request is allowed; the N+1th is refused BEFORE it is sent. "
             "Omitted means no ceiling, which is the prior behaviour.",
    )
    parser.add_argument("--titles", help="Comma-separated titles for coverage reporting.")
    return parser


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def load_fixture(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"fixture must be a JSON object of url -> payload: {path}")
    return data


def load_snapshot(args: argparse.Namespace) -> Optional[ReadOnlySeenSnapshot]:
    if not args.seen_snapshot:
        return None
    return ReadOnlySeenSnapshot.load(
        args.seen_snapshot,
        allow_production_path=bool(args.allow_production_snapshot_copy),
    )


# --------------------------------------------------------------------------
# Lane selection (R1)
# --------------------------------------------------------------------------


def resolve_lanes(args: argparse.Namespace) -> Tuple[List[str], str, str]:
    """Return ``(lanes, selection_source, error)``.

    Live acquisition demands an explicit ``--lanes``. There is no default worth
    having when the consequence of guessing is real requests to a metered API:
    the previous behaviour ran JSearch unconditionally, with no way to turn it
    off, which is what made the first live validation impossible to approve.

    Offline modes keep their existing default so fixture and replay runs behave
    exactly as before. That default is still *recorded* in the manifest, so
    nothing about which lanes ran is implicit in the artifact even when it was
    implicit on the command line.
    """
    if args.lanes is not None:
        requested = [name.strip().lower() for name in args.lanes.split(",") if name.strip()]
        unknown = sorted({name for name in requested if name not in LANES})
        if unknown:
            return [], "explicit", (
                f"unknown lane(s): {', '.join(unknown)}. Supported: {', '.join(LANES)}"
            )
        if not requested:
            return [], "explicit", "--lanes was given but selected nothing"
        ordered = [lane for lane in LANES if lane in set(requested)]
        return ordered, "explicit", ""

    if args.mode == "live_acquisition":
        return [], "none", (
            "live_acquisition requires an explicit --lanes selection "
            f"({', '.join(LANES)}). No lane is enabled implicitly."
        )

    if args.mode == "offline_replay":
        return [], "mode_default", ""

    # fixture: preserve the historical default exactly.
    lanes = ["free_feeds"]
    if config.ADZUNA_ENABLED:
        lanes.append("adzuna")
    if args.boards:
        lanes.append("ats")
    return [lane for lane in LANES if lane in set(lanes)], "mode_default", ""


# --------------------------------------------------------------------------
# Pre-network validation (R5)
# --------------------------------------------------------------------------


def lane_preflight(
    lanes: Sequence[str],
    args: argparse.Namespace,
    *,
    require_credentials: bool,
) -> List[LanePreflight]:
    """Decide whether every selected lane may run, before any request.

    ``require_credentials`` is on only for live acquisition: fixture mode
    serves recorded payloads through ``FixtureFetcher`` and legitimately needs
    no key at all. Structural prerequisites (an unknown source name, an
    unreadable board registry) are enforced in every mode, because those are
    wrong regardless of where the bytes come from.
    """
    selected = set(lanes)
    checks: List[LanePreflight] = []

    for lane in LANES:
        is_selected = lane in selected
        reasons: List[str] = []
        detail = ""

        credentials = [
            CredentialStatus(
                name=name,
                lane=lane,
                status=credential_state(name, required=is_selected and require_credentials),
            ).to_dict()
            for name in LANE_CREDENTIALS[lane]
        ]
        if is_selected and require_credentials:
            reasons.extend(
                f"{entry['name']} is ABSENT"
                for entry in credentials
                if entry["status"] == "ABSENT"
            )

        if is_selected and lane == "free_feeds":
            names = _source_names(args)
            if not names:
                reasons.append("no free sources selected")
            unknown = sorted(set(names) - set(_known_free_sources()))
            if unknown:
                reasons.append(f"unknown free source(s): {', '.join(unknown)}")
            detail = f"{len(names)} source(s)"

        if is_selected and lane == "jsearch":
            if not config.ROLES:
                reasons.append("config.ROLES is empty")
            if args.max_queries is not None and args.max_queries < 0:
                reasons.append("--max-queries cannot be negative")
            if args.max_queries == 0:
                detail = "selected but capped at 0 queries; the lane will be skipped"
            else:
                planned = (
                    len(config.ROLES) if args.max_queries is None
                    else min(int(args.max_queries), len(config.ROLES))
                )
                detail = f"up to {planned} base quer(y|ies)"

        if is_selected and lane == "adzuna":
            if not config.ADZUNA_ENABLED:
                reasons.append("config.ADZUNA_ENABLED is False")

        if is_selected and lane == "ats":
            if not args.boards:
                reasons.append("--boards is required for the ats lane")
            else:
                boards, error = load_boards_readonly(args.boards)
                if error:
                    reasons.append(f"board registry unusable: {error}")
                elif not boards:
                    reasons.append("board registry contains no usable boards")
                else:
                    detail = f"{len(boards)} board(s)"

        checks.append(LanePreflight(
            lane=lane,
            selected=is_selected,
            # Only meaningful for a selected lane. An unselected lane is not
            # "broken", it simply is not part of this run -- which the detail
            # says out loud so preflight.json cannot be misread.
            ready=is_selected and not reasons,
            blocking_reasons=reasons,
            credentials=credentials,
            detail=detail if is_selected else "not selected for this run",
        ))
    return checks


def _known_free_sources() -> List[str]:
    from free_job_sources import ADAPTERS

    return sorted(ADAPTERS)


def _source_names(args: argparse.Namespace) -> List[str]:
    if args.sources:
        return [name.strip().lower() for name in args.sources.split(",") if name.strip()]
    return [str(name).strip().lower() for name in config.FREE_JOB_SOURCES]


def parity_checks(fixture: Dict[str, Any], sources: Sequence[str]) -> List[ParityCheck]:
    """Prove the measuring fetcher is observationally transparent.

    Each adapter runs twice over identical fixtures -- once through a plain
    fetcher, once through the measuring wrapper -- and the two SourceResults
    must be identical. If they are not, every number this harness produces is
    describing a different pipeline than the one in production.
    """
    checks: List[ParityCheck] = []
    for adapter in build_adapters(list(sources)):
        plain = FixtureFetcher(fixture)
        wrapped = MeasuringFetcher(FixtureFetcher(fixture))
        baseline = adapter.fetch(plain)
        with wrapped.context(source=adapter.name):
            measured = adapter.fetch(wrapped)
        same = (
            baseline.jobs == measured.jobs
            and baseline.raw_records == measured.raw_records
            and baseline.requests_attempted == measured.requests_attempted
            and baseline.requests_succeeded == measured.requests_succeeded
            and baseline.pages == measured.pages
            and baseline.success == measured.success
            and baseline.errors == measured.errors
            and baseline.metadata == measured.metadata
        )
        checks.append(ParityCheck(
            name=f"adapter_transparency:{adapter.name}",
            passed=same,
            detail=(
                "SourceResult identical with and without the measuring fetcher"
                if same
                else f"divergence: {baseline.raw_records} vs {measured.raw_records} raw records"
            ),
        ))
    return checks


def lineage_rows(run_id: str, jobs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from job_filter import dedup_key

    rows: List[Dict[str, Any]] = []
    for job in jobs:
        strength, key = posting_identity(job)
        company, title = dedup_key(dict(job))
        rows.append({
            "run_id": run_id,
            "source": str(job.get("_acquisition_source") or "unknown"),
            "identity_strength": strength,
            "identity_key": key,
            "production_dedup_key": f"{company}|{title}",
            "job_title": str(job.get("job_title") or ""),
            "employer_name": str(job.get("employer_name") or ""),
            "posted_at": str(job.get("job_posted_at_datetime_utc") or ""),
            "role_status": str(job.get("_role_relevance_status") or ""),
            "prefilter_viable": bool(job.get("_prefilter_viable")),
        })
    return rows


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.retention_report or args.retention_apply:
        report = evaluate_retention(args.artifact_root, apply=bool(args.retention_apply))
        print(json.dumps(report.to_dict(), indent=2))
        if not args.retention_apply and report.candidates:
            print(
                f"\n{len(report.candidates)} run(s) exceed retention bounds. "
                "Nothing was deleted. Re-run with --retention-apply to remove them.",
                file=sys.stderr,
            )
        return 0

    with DeliveryImportGuard() as guard:
        preexisting = guard.preexisting()
        return _run(args, preexisting)


def _run(args: argparse.Namespace, preexisting_delivery_modules: List[str]) -> int:
    # ---- R1 / R5: everything that can refuse the run happens up here, before
    # a fetcher exists and therefore before any request can possibly be made.
    lanes, selection_source, lane_error = resolve_lanes(args)
    if lane_error:
        print(lane_error, file=sys.stderr)
        return 2

    if args.mode == "fixture" and not args.fixture:
        print("fixture mode requires --fixture", file=sys.stderr)
        return 2
    if args.mode == "offline_replay" and not args.corpus:
        print("offline_replay mode requires at least one --corpus", file=sys.stderr)
        return 2

    preflight = lane_preflight(
        lanes, args, require_credentials=(args.mode == "live_acquisition")
    )
    blocked = [check for check in preflight if check.selected and not check.ready]
    if blocked:
        print("preflight refused the run; no request was made:", file=sys.stderr)
        for check in blocked:
            for reason in check.blocking_reasons:
                print(f"  [{check.lane}] {reason}", file=sys.stderr)
        _print_credential_table(preflight)
        return 2

    run_arguments = {
        "mode": args.mode,
        "lanes": ",".join(lanes),
        "sources": args.sources or "",
        "max_queries": args.max_queries,
        "seen_snapshot_supplied": bool(args.seen_snapshot),
    }
    identity = run_identity(args.mode, run_arguments)
    run_id = identity["run_id"]
    snapshot = load_snapshot(args)

    sources = _source_names(args)
    titles = (
        [title.strip() for title in args.titles.split(",") if title.strip()]
        if args.titles
        else list(config.ROLES)
    )

    notes: List[str] = []
    if preexisting_delivery_modules:
        notes.append(
            "delivery modules were already imported before the guard engaged: "
            + ", ".join(preexisting_delivery_modules)
        )
    notes.append(
        f"lanes selected ({selection_source}): "
        + (", ".join(lanes) or "none")
        + f". Lanes NOT run: {', '.join(lane for lane in LANES if lane not in lanes) or 'none'}."
    )

    fetcher: Optional[MeasuringFetcher] = None
    parity: List[ParityCheck] = []
    outputs: List[Any] = []
    failures: List[LaneFailure] = []

    completed: List[str] = []
    budget = RequestBudget(args.max_requests)

    def run_lane(lane: str, call: Callable[[], Sequence[Any]]) -> None:
        """R3: a lane that raises costs its own measurements and nothing else."""
        if budget.exhausted:
            # The ceiling is global. Once it is reached, later lanes are not
            # attempted at all rather than each being sent to fail.
            failures.append(LaneFailure(
                lane=lane,
                exception_type="RequestCeilingReached",
                error=f"not attempted: global request ceiling of {budget.limit} already reached",
                requests_attempted_before_failure=0,
                stage="skipped_after_ceiling",
            ))
            return
        before_seam = len(fetcher.requests) if fetcher is not None else 0
        before_budget = budget.count
        try:
            # The counter lives on the budget object, so it stays global across
            # lanes and fetcher instances; only the interception is scoped.
            with budget.context(lane=lane), budget.installed():
                outputs.extend(call())
        except Exception as exc:  # noqa: BLE001 - containment is the point
            after_seam = len(fetcher.requests) if fetcher is not None else 0
            spent = budget.count - before_budget
            failures.append(LaneFailure(
                lane=lane,
                exception_type=type(exc).__name__,
                error=redact_text(str(exc)),
                requests_attempted_before_failure=spent or (after_seam - before_seam),
                stage="request_ceiling" if isinstance(exc, RequestCeilingReached) else "lane_execution",
            ))
        else:
            completed.append(lane)

    if args.mode == "fixture":
        fixture = load_fixture(args.fixture)
        if not fixture:
            print("fixture mode requires --fixture", file=sys.stderr)
            return 2
        fetcher = MeasuringFetcher(FixtureFetcher(fixture))
        if "free_feeds" in lanes:
            run_lane("free_feeds", lambda: run_free_source_lane(fetcher, sources))
        if "adzuna" in lanes:
            run_lane("adzuna", lambda: run_adzuna_lane(fetcher))
        if "ats" in lanes:
            boards, board_error = load_boards_readonly(args.boards)
            if board_error:
                notes.append(f"ats boards not loaded: {board_error}")
            if boards:
                run_lane("ats", lambda: run_ats_lane(fetcher, boards))
        if "free_feeds" in lanes:
            parity = parity_checks(fixture, sources)
        notes.append(
            "fixture mode: adapters and transport exercised against recorded payloads; "
            "JSearch query planning is NOT exercised."
        )

    elif args.mode == "offline_replay":
        run_lane("replay", lambda: replay_corpus_lane(args.corpus))
        notes.append(
            "offline_replay mode: real raw corpora replayed as already-canonical records. "
            "Reconciliation, dual uniqueness and determinism are exercised at real scale; "
            "adapters, transport, denominators and truncation are NOT."
        )

    else:  # live_acquisition
        fetcher = MeasuringFetcher()
        if "free_feeds" in lanes:
            run_lane("free_feeds", lambda: run_free_source_lane(fetcher, sources))
        if "adzuna" in lanes:
            run_lane("adzuna", lambda: run_adzuna_lane(fetcher))
        if "ats" in lanes:
            boards, board_error = load_boards_readonly(args.boards)
            if board_error:
                notes.append(f"ats boards not loaded: {board_error}")
            if boards:
                run_lane("ats", lambda: run_ats_lane(fetcher, boards))
        if "jsearch" in lanes:
            # R2: an explicit non-writing registry, so run_daily_scrape can
            # never fall through to `registry or SeenJobsRegistry()`.
            registry = NonWritingRegistry(snapshot)
            transport = JSearchTransport(inner=_live_request)
            run_lane("jsearch", lambda: run_jsearch_lane(
                transport,
                output_dir=str(run_dir(args.artifact_root, run_id) / "jsearch_raw"),
                max_queries=args.max_queries,
                registry=registry,
            ))
        notes.append(
            "live_acquisition mode: real provider requests were made to the selected "
            "lanes only."
        )

    source_metrics, kept, summary = assemble(
        outputs, fetcher, snapshot, run_id=run_id, mode=args.mode, titles=titles
    )
    summary.claim_boundary = CLAIM_BOUNDARY
    summary.parity = [check.to_dict() for check in parity]
    summary.lanes_selected = list(lanes)
    summary.lanes_completed = list(completed)
    summary.lane_failures = [failure.to_dict() for failure in failures]
    summary.request_budget = budget.to_dict()
    summary.notes.extend(notes)
    if budget.exhausted:
        summary.notes.append(
            f"STOPPED BY REQUEST CEILING: {budget.count} requests completed against a "
            f"limit of {budget.limit}; the next request was refused before it was sent. "
            "This is OUR budget stopping OUR run -- it is not provider exhaustion, an "
            "empty page, a provider quota, or a network error, and nothing may be "
            "inferred from it about how much inventory the provider still had."
        )
    if failures:
        summary.notes.append(
            "PARTIAL RUN: "
            + ", ".join(f"{failure.lane} raised {failure.exception_type}" for failure in failures)
            + ". Measurements from the lanes that completed are still reconciled and "
            "reported; the failed lanes contribute nothing and are NOT recorded as "
            "truncation or provider exhaustion."
        )

    manifest = RunManifest(
        run_id=run_id,
        mode=args.mode,
        started_at=identity["started_at"],
        finished_at=utc_stamp(),
        git_commit=identity["git_commit"],
        git_branch=identity["git_branch"],
        git_dirty=identity["git_dirty"],
        python_version=identity["python_version"],
        platform=identity["platform"],
        config_fingerprint=identity["config_fingerprint"],
        effective_config=identity["effective_config"],
        seen_snapshot=(snapshot.describe() if snapshot else {"available": False, "write_capable": False}),
        lanes_selected=list(lanes),
        lane_selection_source=selection_source,
        preflight=[check.to_dict() for check in preflight],
        lane_failures=[failure.to_dict() for failure in failures],
        request_budget=budget.to_dict(),
        notes=list(notes),
    )

    reconciliation = summary.reconciliation or {}
    parity_failed = [check for check in parity if not check.passed]
    ok = bool(reconciliation.get("passed")) and not parity_failed and not failures
    manifest.status = "complete" if ok else "incomplete"
    reasons: List[str] = []
    if budget.exhausted:
        reasons.append(
            f"{budget.stop_reason}: {budget.count}/{budget.limit} requests, "
            f"blocked {budget.blocked_next_request}"
        )
    if failures:
        reasons.append(
            "lane failure: " + ", ".join(f"{f.lane}/{f.exception_type}" for f in failures)
        )
    if not reconciliation.get("passed"):
        reasons.append(f"reconciliation failed in scopes: {reconciliation.get('failed_scopes')}")
    if parity_failed:
        reasons.append(f"parity failed: {[check.name for check in parity_failed]}")
    manifest.exit_reason = "; ".join(reasons)

    discard_metrics = [
        {"source": metric.source, "removals": metric.removals, "annotations": metric.annotations}
        for metric in source_metrics
    ]
    board_metrics = [board.to_dict() for output in outputs for board in output.boards]
    request_ledger = [record.to_dict() for record in (fetcher.requests if fetcher else [])]

    written = write_run_artifacts(
        args.artifact_root,
        run_id,
        manifest=manifest.to_dict(),
        summary=summary.to_dict(),
        source_metrics=[metric.to_dict() for metric in source_metrics],
        board_metrics=board_metrics,
        discard_metrics=discard_metrics,
        parity=summary.parity,
        request_ledger=request_ledger,
        posting_lineage=lineage_rows(run_id, kept),
        lane_failures=manifest.lane_failures,
        preflight=manifest.preflight,
    )

    directory = run_dir(args.artifact_root, run_id)
    print(f"run_id           {run_id}")
    print(f"mode             {args.mode}")
    print(f"lanes            {', '.join(lanes) or 'none'} ({selection_source})")
    print(f"requests         {budget.count}" + (f" / {budget.limit}" if budget.limit else " (no ceiling)"))
    print(f"status           {manifest.status}")
    print(f"artifacts        {directory} ({written['_total_bytes']:,} bytes)")
    posting = summary.run_baseline_posting_identity
    print(
        "gross returned   {gross}\nunique postings  {unique}\npreviously seen  {seen}\n"
        "incremental new  {new}".format(
            gross=posting.get("gross_returned"),
            unique=posting.get("unique_in_run"),
            seen=posting.get("previously_seen"),
            new=posting.get("incremental_new"),
        )
    )
    if failures:
        print(
            f"\n{len(failures)} lane(s) failed; this run is PARTIAL. "
            f"Artifacts persisted: {written.get('_total_bytes', 0):,} bytes.",
            file=sys.stderr,
        )
        for failure in failures:
            print(
                f"  [{failure.lane}] {failure.exception_type}: {failure.error} "
                f"({failure.requests_attempted_before_failure} request(s) before failure)",
                file=sys.stderr,
            )
    if not ok:
        print(f"\nFAILED: {manifest.exit_reason}", file=sys.stderr)
    retention = evaluate_retention(args.artifact_root, apply=False)
    if retention.candidates:
        print(
            f"\nretention: {len(retention.candidates)} run(s) exceed bounds "
            f"({retention.total_bytes:,} bytes total). Nothing deleted; "
            "run --retention-apply to remove them."
        )
    return 0 if ok else 1


def _print_credential_table(preflight: Sequence[LanePreflight]) -> None:
    """Presence only. No value is read, printed, hashed, or truncated here."""
    rows = [
        (entry["name"], check.lane, entry["status"])
        for check in preflight
        for entry in check.credentials
    ]
    if not rows:
        return
    print("\ncredential status (presence only, no values):", file=sys.stderr)
    for name, lane, status in sorted(rows):
        print(f"  {name:<24} {lane:<12} {status}", file=sys.stderr)


def _live_request(method: str, url: str, **kwargs: Any):
    """Live JSearch transport. Imported lazily so offline runs never touch it."""
    from http_utils import request_with_retry

    return request_with_retry(method, url, **kwargs)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
