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
    "unique_opportunities",
    "jobs_reviewed",
    "qualified_opportunities",
    "companies_considered",
    "contacts_found",
    "final_pass_leads",
    "verified_emails",
    "sent_to_airtable",
    "airtable_suppressed",
    "sent_to_instantly",
)

#: Stage names recorded in ``stages_recorded``, in the order they complete.
STAGE_START = "start"
STAGE_ACQUISITION = "acquisition"
STAGE_ENRICHMENT = "enrichment"
STAGE_DELIVERY = "delivery"
STAGE_FINAL = "final"

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
