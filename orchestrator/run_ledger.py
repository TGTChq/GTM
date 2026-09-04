"""Compact, durable per-run reporting ledger.

Heavy run artifacts (``run_artifacts/<run_id>/``) are *diagnostic evidence*: a
single productive run measured 233 MB, so retention has to prune them
aggressively and does. That is fine for debugging and fatal for reporting -- the
2026-W36 weekly report discovered 4 of the week's 7 scheduled runs because
``RETENTION_KEEP_RUNS=4`` had already deleted the other three, and declared no
problem while doing it.

This module separates the two concerns. Every run also writes a **tiny** durable
summary carrying only the business counters the weekly report needs:

* one file per run, ``reporting_ledger/<run_id>.json``, ~1-2 KB;
* written at run START and updated as each stage completes, so a run that is
  killed mid-flight still leaves an entry saying what it had achieved;
* finalized atomically at run completion (temp file + ``os.replace``);
* never touched by ``StateManager.prune``, which only deletes under
  ``run_artifacts``;
* retained by its own, far longer policy (see ``prune_ledger``).

Two invariants make the ledger safe to report from:

**A metric key is present only when it was measured.** A stage that never ran
contributes no key at all -- it is never written as ``0``. "The pipeline
processed nothing" and "this counter does not exist" are different facts, and
only the first is a business result.

**A run that started is visible forever after.** The Sep 4 control run acquired
6,206 jobs and left ~200 MB of evidence on disk, yet ``discover_runs()`` returned
zero runs because the process was killed before any top-level marker file was
written. An entry created at run start cannot fail that way: it is still there,
in state ``running``, which every reader renders as INTERRUPTED.

Metric keys are deliberately the *weekly report's own* metric keys
(``jobs_captured``, ``sent_to_airtable``, ...) so the reporting layer reads them
with no translation table in between.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

#: Store directory under the orchestrator artifact root. Deliberately a sibling of
#: ``run_artifacts`` rather than a child: pruning must not be able to reach it.
LEDGER_STORE = "reporting_ledger"

#: Bump only for a breaking change to the entry shape.
LEDGER_SCHEMA = "tgtc-run-reporting-ledger/1"

STATE_RUNNING = "running"
STATE_COMPLETE = "complete"
STATE_INCOMPLETE = "incomplete"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"

#: A run whose entry is still ``running`` when someone reads it did not finish.
#: The pipeline finalizes in a ``finally``, so even an exception records ``failed``;
#: only a hard kill (SIGKILL, OOM, container stop, power loss) leaves ``running``.
TERMINAL_STATES = (STATE_COMPLETE, STATE_INCOMPLETE, STATE_FAILED, STATE_INTERRUPTED)

#: The business counters the weekly report reads, in pipeline order. Names match
#: ``weekly_report.metrics`` spec keys exactly so there is no mapping layer.
LEDGER_METRIC_KEYS = (
    "jobs_captured",
    # Acquisition efficiency, kept separate from throughput. A posting the
    # provider returns for the second time is an acquisition cost, not a funnel loss.
    "provider_jobs_returned",
    "provider_jobs_billed",
    # The three MUTUALLY EXCLUSIVE exits at the acquisition-dedupe boundary, each
    # counted where the decision is taken. ``historical_duplicates`` is the
    # long-standing name for the previously-seen count and is kept so an existing
    # report reads the same key; the two below used to be folded into it by a
    # ``kept - opportunities`` subtraction that could not tell them apart.
    "historical_duplicates",
    "historical_previously_seen_duplicates",
    "canonical_duplicates_in_run",
    "postings_missing_identity",
    "cross_query_duplicates",
    "cross_source_duplicates",
    "net_new_jobs_captured",
    "unique_opportunities",
    "jobs_reviewed",
    "qualified_opportunities",
    "companies_considered",
    "contacts_found",
    "final_pass_leads",
    "verified_emails",
    "airtable_candidates",
    "sent_to_airtable",
    "airtable_suppressed",
    "airtable_write_failures",
    "sent_to_instantly",
)

#: Stage names recorded in ``stages_recorded``, in the order they complete.
STAGE_START = "start"
STAGE_ACQUISITION = "acquisition"
STAGE_ENRICHMENT = "enrichment"
STAGE_DELIVERY = "delivery"
STAGE_FINAL = "final"
#: Counters lifted out of heavy artifacts after the fact (see ``backfill_from_artifacts``).
STAGE_BACKFILL = "backfill_from_artifacts"

#: Ledger retention. One entry per run per day is ~1.5 KB, so 180 days of daily
#: runs is ~270 KB -- three orders of magnitude below one heavy run directory.
#: Far longer than the 8 weeks the weekly report needs, because the whole point of
#: this store is that history must outlive the evidence it summarises.
LEDGER_KEEP_DAYS = 180
LEDGER_KEEP_MAX = 2000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(moment: Optional[datetime]) -> Optional[str]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_dir(root: str | os.PathLike) -> Path:
    """The ledger store under an orchestrator artifact root."""
    return Path(root) / LEDGER_STORE


def _as_count(value: Any) -> Optional[int]:
    """Coerce to a non-negative count, or ``None``. Booleans are not numbers here."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


class RunLedger:
    """One run's compact reporting entry.

    Every mutation rewrites the whole (tiny) file atomically, so there is no
    read-modify-write window and a crash mid-write leaves the previous complete
    entry rather than a truncated one. The in-memory payload is authoritative
    during the run, so a write failure never loses data that a later stage write
    can still persist.

    Fail-open by construction: a ledger problem must never take down a production
    run. Every public method swallows its own I/O errors and records them in
    ``self.errors`` for the run summary to print.
    """

    def __init__(self, root: str | os.PathLike, run_id: str, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.run_id = str(run_id)
        self.enabled = bool(enabled)
        self.errors: List[str] = []
        self._payload: Dict[str, Any] = {
            "schema": LEDGER_SCHEMA,
            "run_id": self.run_id,
            "state": STATE_RUNNING,
            "metrics": {},
            "metric_sources": {},
            "stages_recorded": [],
        }

    # -- location ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return ledger_dir(self.root) / f"{self.run_id}.json"

    # -- writing -----------------------------------------------------------

    def _flush(self) -> None:
        """Atomic whole-file write. Never raises."""
        if not self.enabled:
            return
        self._payload["updated_at"] = _iso_z(_utc_now())
        target = self.path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp = tempfile.mkstemp(
                dir=str(target.parent), prefix="." + self.run_id + "-", suffix=".json"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as fh:
                    json.dump(self._payload, fh, indent=2, default=str, sort_keys=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:  # noqa: BLE001 - reporting must never break a run
            message = type(exc).__name__ + ": " + str(exc)[:160]
            if message not in self.errors:
                self.errors.append(message)

    def _mark_stage(self, stage: str) -> None:
        if stage and stage not in self._payload["stages_recorded"]:
            self._payload["stages_recorded"].append(stage)

    def begin(
        self,
        *,
        started_at: Optional[datetime] = None,
        mode: str = "",
        allow_network: Optional[bool] = None,
        allow_enrichment: Optional[bool] = None,
        allow_instantly_enrollment: Optional[bool] = None,
        lanes: Iterable[str] = (),
        artifact_dir: str = "",
    ) -> "RunLedger":
        """Create the entry BEFORE any work happens.

        This is the whole defence against an invisible run: after this call the run
        exists in the reporting record no matter how the process dies.
        """
        self._payload["started_at"] = _iso_z(started_at or _utc_now())
        self._payload["state"] = STATE_RUNNING
        self._payload["mode"] = str(mode or "")
        self._payload["policy"] = {
            "allow_network": allow_network,
            "allow_enrichment": allow_enrichment,
            "allow_instantly_enrollment": allow_instantly_enrollment,
        }
        self._payload["lane_names"] = sorted({str(lane) for lane in lanes})
        self._payload["artifact_dir"] = str(artifact_dir or "run_artifacts/" + self.run_id)
        self._mark_stage(STAGE_START)
        self._flush()
        return self

    def record(self, stage: str, metrics: Optional[Mapping[str, Any]] = None, **extra: Any) -> None:
        """Merge measured counters for one completed stage and persist immediately.

        ``None`` values are DROPPED, never stored as zero: a key is present only
        when it was measured. ``extra`` carries non-metric context (source counts,
        ``acquisition_entered``, ``stop_reason`` ...) recorded alongside.
        """
        if not self.enabled:
            return
        for key, raw in dict(metrics or {}).items():
            count = _as_count(raw)
            if count is None:
                continue
            self._payload["metrics"][key] = count
            self._payload["metric_sources"][key] = "pipeline:" + str(stage)
        for key, value in extra.items():
            if value is None:
                continue
            self._payload[key] = value
        self._mark_stage(stage)
        self._flush()

    def finalize(
        self,
        *,
        state: str,
        status: str = "",
        stop_reason: str = "",
        finished_at: Optional[datetime] = None,
        artifacts_written: Optional[bool] = None,
    ) -> None:
        """Close the entry. Called from the pipeline's failure-safe ``finally``."""
        if not self.enabled:
            return
        self._payload["state"] = str(state or STATE_INCOMPLETE)
        self._payload["status"] = str(status or state or "")
        self._payload["stop_reason"] = str(stop_reason or "")
        self._payload["finished_at"] = _iso_z(finished_at or _utc_now())
        if artifacts_written is not None:
            self._payload["artifacts_written"] = bool(artifacts_written)
        self._mark_stage(STAGE_FINAL)
        self._flush()

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._payload)


#: Loss reasons carried in the ledger, capped so the entry stays tiny. Counts
#: only -- never a lead payload.
_LEDGER_REASON_LIMIT = 12


def reason_census_from_parts(waterfall: Any, loss_census: Any, delivery: Any,
                          qual_reasons: Any = None) -> Dict[str, int]:
    """The run's loss-reason counts, merged exactly as the weekly report merges them.

    The report used to rebuild this from heavy artifacts, so once retention
    deleted them the action plan lost every reason code and fell back to generic
    text. Carrying a bounded copy here keeps next week's plan specific after the
    evidence is gone. The three sources and their merge order match
    ``weekly_report.metrics.reason_census`` so a ledger-only week and an
    artifact-backed week produce identical totals.
    """
    census: Dict[str, int] = {}

    def _add(mapping: Any) -> None:
        if not isinstance(mapping, dict):
            return
        for reason, count in mapping.items():
            if isinstance(count, bool) or not isinstance(count, (int, float)):
                continue
            census[str(reason)] = census.get(str(reason), 0) + int(count)

    if isinstance(waterfall, dict):
        for stage in waterfall.get("stages") or []:
            if isinstance(stage, dict):
                _add(stage.get("primary_reasons"))
    _add(qual_reasons)
    _add(loss_census)
    if isinstance(delivery, dict):
        _add(delivery.get("skip_breakdown"))
    ranked = sorted(census.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(ranked[:_LEDGER_REASON_LIMIT])


# -- reading ---------------------------------------------------------------


def read_entry(path: str | os.PathLike) -> Optional[Dict[str, Any]]:
    """Parse one ledger file. Returns ``None`` for anything that is not an entry.

    A foreign or future schema is refused rather than reinterpreted: reporting a
    number under the wrong contract is worse than reporting it as missing.
    """
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != LEDGER_SCHEMA:
        return None
    if not data.get("run_id"):
        return None
    return data


def read_entries(root: str | os.PathLike) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every ledger entry under ``root``, oldest first. Returns ``(entries, problems)``.

    Unreadable files are reported as problems rather than skipped in silence --
    the failure mode this whole module exists to prevent.
    """
    directory = ledger_dir(root)
    problems: List[str] = []
    if not directory.is_dir():
        return [], problems
    entries: List[Dict[str, Any]] = []
    try:
        children = sorted(directory.iterdir())
    except OSError as exc:
        return [], ["cannot list reporting ledger " + str(directory) + ": " + str(exc)[:160]]
    for child in children:
        if not child.is_file() or child.suffix != ".json" or child.name.startswith("."):
            continue
        entry = read_entry(child)
        if entry is None:
            problems.append("unreadable reporting-ledger entry: " + child.name)
            continue
        entry["_path"] = str(child)
        entries.append(entry)
    entries.sort(key=lambda e: (str(e.get("started_at") or ""), str(e.get("run_id") or "")))
    return entries, problems


# -- retention -------------------------------------------------------------


def prune_ledger(
    root: str | os.PathLike,
    *,
    keep_days: int = LEDGER_KEEP_DAYS,
    keep_max: int = LEDGER_KEEP_MAX,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Age-based retention for the compact ledger, independent of heavy artifacts.

    Deliberately generous: the store exists so that reporting history outlives the
    evidence, and it is small enough that keeping months of it costs nothing. Never
    raises -- retention is housekeeping, not a run outcome.
    """
    directory = ledger_dir(root)
    removed: List[str] = []
    if not directory.is_dir():
        return {"removed": removed, "kept": 0, "total_bytes": 0}
    moment = now or _utc_now()
    cutoff = moment - timedelta(days=max(1, int(keep_days)))
    entries, _ = read_entries(root)

    def _started(entry: Dict[str, Any]) -> Optional[datetime]:
        text = str(entry.get("started_at") or "")
        if not text:
            return None
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc)

    survivors: List[Dict[str, Any]] = []
    for entry in entries:
        started = _started(entry)
        if started is not None and started < cutoff:
            try:
                Path(entry["_path"]).unlink()
                removed.append(str(entry.get("run_id")))
            except OSError:
                pass
            continue
        survivors.append(entry)

    # Count guard, newest kept. Only reachable if runs are far more frequent than daily.
    overflow = len(survivors) - max(1, int(keep_max))
    if overflow > 0:
        for entry in survivors[:overflow]:
            try:
                Path(entry["_path"]).unlink()
                removed.append(str(entry.get("run_id")))
            except OSError:
                pass
        survivors = survivors[overflow:]

    total = 0
    for child in directory.glob("*.json"):
        try:
            total += child.stat().st_size
        except OSError:
            pass
    return {"removed": removed, "kept": len(survivors), "total_bytes": total}


# -- backfill --------------------------------------------------------------

#: ledger metric key -> (artifact stem, dotted path) candidates, most authoritative
#: first. Mirrors ``weekly_report.metrics`` deliberately: the orchestrator must not
#: import the reporting layer, so the field list is restated rather than shared.
_BACKFILL_FIELDS = (
    # Net-new first: a run written after 2026-09-05 records it directly. Older
    # runs fall back to the raw posting count, which is the only thing they know.
    ("jobs_captured", (("orchestrator_result", "acquisition.cumulative.net_new_jobs_captured"),
                       ("waterfall", "unit_totals.postings"),
                       ("capacity_report", "raw_postings"))),
    ("net_new_jobs_captured",
     (("orchestrator_result", "acquisition.cumulative.net_new_jobs_captured"),)),
    ("provider_jobs_returned",
     (("orchestrator_result", "acquisition.cumulative.jobs_returned_billed"),)),
    ("provider_jobs_billed",
     (("orchestrator_result", "acquisition.cumulative.jobs_quota_consumed"),)),
    # Pre-2026-09-05 runs wrote ``historical_duplicates`` as a broad subtraction
    # that also absorbed in-run canonical duplicates, so it is backfilled from the
    # narrow field FIRST and only falls back to the broad one for older runs --
    # never the other way round, which would re-widen a corrected number.
    ("historical_duplicates",
     (("orchestrator_result",
       "acquisition.cumulative.historical_previously_seen_duplicates"),
      ("orchestrator_result", "acquisition.cumulative.historical_duplicates"))),
    ("historical_previously_seen_duplicates",
     (("orchestrator_result",
       "acquisition.cumulative.historical_previously_seen_duplicates"),)),
    ("canonical_duplicates_in_run",
     (("orchestrator_result", "acquisition.cumulative.canonical_duplicates_in_run"),)),
    ("postings_missing_identity",
     (("orchestrator_result", "acquisition.cumulative.postings_missing_identity"),)),
    ("cross_query_duplicates",
     (("orchestrator_result", "acquisition.cumulative.cross_query_duplicates"),)),
    ("cross_source_duplicates",
     (("orchestrator_result", "acquisition.cumulative.cross_source_duplicates"),)),
    ("unique_opportunities", (("waterfall", "unit_totals.opportunities"),)),
    ("jobs_reviewed", (("orchestrator_result", "enrichment.funnel.qualification_input"),)),
    # Only the contact-discovery entry counter may answer "qualified
    # opportunities". A pre-2026-09-05 run does not carry it, and backfilling the
    # looser target_role_eligible in its place would reintroduce exactly the
    # semantics this change removed -- through the back door, and silently.
    ("qualified_opportunities",
     (("orchestrator_result", "enrichment.funnel.contact_discovery_entered"),)),
    ("role_qualified_postings",
     (("orchestrator_result", "enrichment.funnel.target_role_eligible"),)),
    ("companies_considered",
     (("orchestrator_result", "enrichment.funnel.companies_considered"),)),
    ("contacts_found", (("waterfall", "unit_totals.contacts"),)),
    ("final_pass_leads", (("waterfall", "final_pass_count"),)),
    ("verified_emails", (("orchestrator_result", "emails.verified"),)),
    ("airtable_candidates", (("delivery", "reviewable_submitted"),)),
    ("sent_to_airtable", (("delivery", "created"),)),
    ("airtable_suppressed", (("delivery", "skipped_existing"),)),
    ("airtable_write_failures", (("delivery", "failed"),)),
)

_BACKFILL_STATES = {
    "complete": STATE_COMPLETE,
    "incomplete": STATE_INCOMPLETE,
    "failed": STATE_FAILED,
    "resumed": STATE_INCOMPLETE,
    "running": STATE_RUNNING,
}


def _dig(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def backfill_from_artifacts(
    root: str | os.PathLike, *, run_artifacts_dirname: str = "run_artifacts"
) -> Dict[str, Any]:
    """Write a ledger entry for any run directory that does not have one yet.

    Bridges the deploy boundary: runs that completed before the ledger existed
    still have their heavy artifacts on disk for a few days, and this lifts their
    counters into the durable store BEFORE retention deletes the evidence. Without
    it the first productive run would vanish from the very report this store was
    built to fix.

    Idempotent (an existing entry is never overwritten) and fail-open: a run that
    cannot be read is skipped, never raised.
    """
    base = Path(root) / run_artifacts_dirname
    written: List[str] = []
    existence_only: List[str] = []
    if not base.is_dir():
        return {"written": written, "existence_only": existence_only}

    existing = {entry.get("run_id") for entry in read_entries(root)[0]}
    try:
        children = sorted(d for d in base.iterdir() if d.is_dir())
    except OSError:
        return {"written": written, "existence_only": existence_only}

    for run_dir in children:
        run_id = run_dir.name
        if run_id in existing:
            continue
        artifacts: Dict[str, Any] = {}
        for name in ("run_manifest", "run_status", "waterfall", "delivery",
                     "capacity_report", "lanes", "orchestrator_result"):
            target = run_dir / f"{name}.json"
            if not target.is_file():
                continue
            try:
                artifacts[name] = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        if not artifacts:
            # A run directory with no readable top-level artifact: an interrupted
            # run whose counters live only in per-stage files. Record that it
            # EXISTED -- that alone is more than the old reader managed.
            existence_only.append(run_id)

        manifest = artifacts.get("run_manifest") or {}
        status = str(_dig(artifacts, "run_status.status")
                     or manifest.get("status") or "").lower()
        policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}

        ledger = RunLedger(root, run_id)
        ledger._payload["started_at"] = manifest.get("started_at")
        ledger._payload["mode"] = str(manifest.get("mode") or "")
        ledger._payload["policy"] = {
            "allow_network": policy.get("allow_network"),
            "allow_enrichment": policy.get("allow_enrichment"),
            "allow_instantly_enrollment": policy.get("allow_instantly_enrollment"),
        }
        lanes = artifacts.get("lanes") or _dig(artifacts, "orchestrator_result.lanes") or {}
        ledger._payload["lane_names"] = sorted(str(n) for n in lanes) if isinstance(lanes, dict) else []
        ledger._payload["artifact_dir"] = f"{run_artifacts_dirname}/{run_id}"
        ledger._payload["backfilled_from_artifacts"] = True
        ledger._mark_stage(STAGE_START)

        measured: Dict[str, Any] = {}
        for key, candidates in _BACKFILL_FIELDS:
            for stem, path in candidates:
                value = _as_count(_dig(artifacts.get(stem), path))
                if value is not None:
                    measured[key] = value
                    break
        if policy.get("allow_instantly_enrollment") is True:
            enrolled = _as_count(_dig(artifacts.get("delivery"), "enrolled"))
            if enrolled is not None:
                measured["sent_to_instantly"] = enrolled
        if measured:
            # Provenance says "artifacts", not "pipeline": these counters were
            # lifted from the evidence after the fact, not observed as it ran.
            ledger.record(STAGE_BACKFILL, measured)

        # Carry the loss reasons across too, or a backfilled run would still lose
        # its action plan the moment its evidence is pruned -- the exact failure
        # this store exists to prevent, just one week later.
        result_block = artifacts.get("orchestrator_result") or {}
        census = reason_census_from_parts(
            artifacts.get("waterfall") or _dig(result_block, "waterfall"),
            _dig(result_block, "enrichment.loss_census"),
            artifacts.get("delivery") or _dig(result_block, "delivery"),
            qual_reasons=_dig(result_block, "enrichment.funnel.qual_reason_counts"),
        )
        if census:
            ledger._payload["loss_reasons"] = census

        if status:
            ledger.finalize(
                state=_BACKFILL_STATES.get(status, STATE_INCOMPLETE),
                status=status,
                stop_reason=str(_dig(artifacts, "run_status.stop_reason")
                                or manifest.get("stop_reason") or ""),
                finished_at=None,
                artifacts_written=bool(artifacts),
            )
            # finalize() stamps "now"; the run's own clock is the correct one.
            if manifest.get("finished_at"):
                ledger._payload["finished_at"] = manifest["finished_at"]
                ledger._flush()
        else:
            # No status anywhere: the run never finalized. Leave it RUNNING so
            # every reader renders it as interrupted.
            ledger._flush()
        written.append(run_id)

    return {"written": written, "existence_only": existence_only}
