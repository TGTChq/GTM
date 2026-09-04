"""Read-only discovery of orchestrator runs.

Runs are discovered from TWO stores, and the compact one is authoritative:

* ``<artifact-root>/reporting_ledger/<run_id>.json`` -- a ~1.5 KB durable business
  summary written at run START and updated as stages complete
  (``orchestrator/run_ledger.py``). This is the run INDEX. It is never pruned by
  heavy-artifact retention, so a week always has all of its runs.
* ``<artifact-root>/run_artifacts/<run_id>/`` -- the heavy evidence
  (``run_manifest.json``, ``run_status.json``, ``waterfall.json``, ``delivery.json``,
  ``capacity_report.json``, ``orchestrator_result.json``). Retained for a handful
  of runs only, because one productive run measured 233 MB. Used to enrich a
  ledger row when it is still on disk, never required for the row to exist.

Reading the heavy artifacts alone is what produced the 2026-W36 report: it found
4 of the week's 7 scheduled runs, because ``RETENTION_KEEP_RUNS=4`` had deleted
the rest, and reported ``problems: []`` while doing it. A run that has been pruned
is not "missing" to that code -- it is invisible, which is strictly worse.

A run whose ledger entry never reached a terminal state is reported as
``interrupted``: it started, it may have real counters, and it did not finish.
The Sep 4 control run acquired 6,206 jobs and left ~200 MB of evidence, yet
returned zero runs here because no top-level marker file had been written.

Window attribution uses the run's *completion* timestamp, falling back to its
start, and finally to the timestamp embedded in the sortable ``run_id``
(``%Y%m%dT%H%M%SZ-<hex8>``). The field actually used is recorded per run, so a
report never hides which clock placed a run in the week.

**A job's ``posted_at`` is deliberately never used for window attribution.** A
backlog of month-old postings processed on Tuesday is Tuesday's throughput, not
last month's.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from orchestrator.run_ledger import (
    LEDGER_STORE,
    STATE_INTERRUPTED,
    STATE_RUNNING,
    TERMINAL_STATES,
    read_entries,
)
from weekly_report.timewindow import ReportingWindow, iso_z, parse_instant

#: The stem the ledger entry is parked under inside ``RunRecord.artifacts``, so a
#: ``MetricSpec`` reads it with exactly the same ``(stem, path)`` machinery it uses
#: for every heavy artifact -- no parallel code path, no translation table.
LEDGER_STEM = "ledger"

#: Artifact files parsed into every ``RunRecord``, keyed by their stem.
ARTIFACT_FILES = (
    "run_manifest.json",
    "run_status.json",
    "waterfall.json",
    "delivery.json",
    "capacity_report.json",
    "acquisition.json",
    "topup.json",
    "lanes.json",
    "orchestrator_result.json",
)

#: A run directory must contain at least one of these to be a run at all.
_MARKER_FILES = ("run_status.json", "run_manifest.json", "orchestrator_result.json")

ATTR_FINISHED = "run_manifest.finished_at"
ATTR_STARTED = "run_manifest.started_at"
ATTR_RUN_ID = "run_id_prefix"
ATTR_NONE = "none"

#: How much a given clock is trusted to place a run in a week, best first. Used when
#: one run is described by two stores: a manifest with no timestamps must not beat a
#: ledger entry that recorded the real completion instant, or a run can land in the
#: wrong week at a boundary.
_ATTRIBUTION_RANK = {ATTR_FINISHED: 3, ATTR_STARTED: 2, ATTR_RUN_ID: 1, ATTR_NONE: 0}

#: Whether a run's counters describe real business activity.
REALISM_PRODUCTION = "production"
REALISM_SIMULATED = "simulated"

#: Mode substrings that mean the run did not do real work. ``full_dry_run`` writes
#: the same artifact shape as a live run -- including a non-zero ``delivery.enrolled``
#: against a *synthetic* lane -- so its counters must never be read as throughput.
_SIMULATED_MODE_MARKERS = ("dry_run", "dry-run", "dryrun", "simulat", "preflight", "fixture", "replay")

#: Lane names that carry manufactured postings rather than acquired ones.
_SYNTHETIC_LANES = ("synthetic", "fixture", "sample")


def classify_realism(artifacts: Dict[str, Any], mode: str) -> Tuple[str, str]:
    """``(realism, why)``. Conservative: a run is production unless it proves otherwise.

    The mode policy is authoritative. ``allow_network=False`` means nothing left the
    process, so every counter in that run is manufactured no matter what the delivery
    block claims.
    """
    lowered = mode.lower()
    for marker in _SIMULATED_MODE_MARKERS:
        if marker in lowered:
            return REALISM_SIMULATED, f"run mode is {mode!r}"

    policy = _get(artifacts, "run_manifest", "policy")
    if not isinstance(policy, dict):
        result_run = _get(artifacts, "orchestrator_result", "run")
        policy = result_run.get("policy") if isinstance(result_run, dict) else None
    if isinstance(policy, dict) and policy.get("allow_network") is False:
        return REALISM_SIMULATED, "run policy has allow_network=false"

    lanes = artifacts.get("lanes")
    if not isinstance(lanes, dict):
        lanes = _get(artifacts, "orchestrator_result", "lanes")
    if isinstance(lanes, dict) and lanes:
        names = {str(name).lower() for name in lanes}
        if names and names.issubset(set(_SYNTHETIC_LANES)):
            return REALISM_SIMULATED, f"every acquisition lane is synthetic ({', '.join(sorted(names))})"

    return REALISM_PRODUCTION, ""


@dataclass
class RunRecord:
    """One orchestrator run, parsed but not interpreted."""

    run_id: str
    path: Path
    artifacts: Dict[str, Any] = field(default_factory=dict)
    parse_errors: Dict[str, str] = field(default_factory=dict)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    attributed_at: Optional[datetime] = None
    attribution_field: str = ATTR_NONE
    realism: str = REALISM_PRODUCTION
    realism_reason: str = ""

    @property
    def ledger_state(self) -> str:
        """The compact ledger's own state, or ``""`` when there is no entry."""
        return str(_get(self.artifacts, LEDGER_STEM, "state") or "")

    @property
    def status(self) -> str:
        """The run's outcome, ledger first.

        A TERMINAL ledger state is authoritative: it is written in the same
        failure-safe ``finally`` as ``run_status.json``, so the two cannot
        disagree. An entry still in ``running`` means the process never reached
        that ``finally`` at all -- a hard kill. If no heavy artifact contradicts
        it, that run was INTERRUPTED, which is a fact worth reporting, not the
        "unknown" it used to degrade to.
        """
        state = self.ledger_state
        if state in TERMINAL_STATES:
            return state
        heavy = (
            _get(self.artifacts, "run_status", "status")
            or _get(self.artifacts, "run_manifest", "status")
        )
        if heavy:
            return str(heavy)
        if state == STATE_RUNNING:
            return STATE_INTERRUPTED
        return "unknown"

    @property
    def interrupted(self) -> bool:
        return self.status == STATE_INTERRUPTED

    @property
    def has_ledger(self) -> bool:
        return isinstance(self.artifacts.get(LEDGER_STEM), dict)

    @property
    def has_artifacts(self) -> bool:
        """Whether the heavy evidence directory is still on disk for this run."""
        return any(stem != LEDGER_STEM for stem in self.artifacts)

    @property
    def mode(self) -> str:
        return str(
            _get(self.artifacts, "run_manifest", "mode")
            or _get(self.artifacts, LEDGER_STEM, "mode")
            or "unknown"
        )

    @property
    def stop_reason(self) -> str:
        return str(
            _get(self.artifacts, "run_status", "stop_reason")
            or _get(self.artifacts, "run_manifest", "stop_reason")
            or _get(self.artifacts, LEDGER_STEM, "stop_reason")
            or ""
        )

    def artifact(self, stem: str) -> Dict[str, Any]:
        value = self.artifacts.get(stem)
        return value if isinstance(value, dict) else {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status,
            "mode": self.mode,
            "started_at": iso_z(self.started_at) if self.started_at else None,
            "finished_at": iso_z(self.finished_at) if self.finished_at else None,
            "attributed_at": iso_z(self.attributed_at) if self.attributed_at else None,
            "attribution_field": self.attribution_field,
            "realism": self.realism,
            "artifacts_present": sorted(self.artifacts),
            "path": str(self.path),
            # Which store answered for this run. "ledger" alone is the normal shape
            # of a run whose heavy evidence has been pruned -- expected, not a gap.
            "evidence_source": (
                "ledger+artifacts" if (self.has_ledger and self.has_artifacts)
                else "ledger" if self.has_ledger
                else "artifacts"
            ),
            "heavy_artifacts_present": self.has_artifacts,
        }
        if self.realism_reason:
            payload["realism_reason"] = self.realism_reason
        if self.stop_reason:
            payload["stop_reason"] = self.stop_reason
        if self.parse_errors:
            payload["parse_errors"] = dict(self.parse_errors)
        return payload


def _get(artifacts: Dict[str, Any], stem: str, key: str) -> Any:
    block = artifacts.get(stem)
    return block.get(key) if isinstance(block, dict) else None


def _run_id_instant(run_id: str) -> Optional[datetime]:
    """Recover the start instant from a sortable ``%Y%m%dT%H%M%SZ-<hex8>`` run id."""
    head = run_id.split("-", 1)[0]
    if len(head) != 16 or not head.endswith("Z"):
        return None
    try:
        return parse_instant(datetime.strptime(head, "%Y%m%dT%H%M%SZ"))
    except ValueError:
        return None


def resolve_run_artifact_dirs(roots: Sequence[str | os.PathLike]) -> Tuple[List[Path], List[str]]:
    """Map caller-supplied roots onto the directories that hold run folders.

    Accepts an artifact root (``.../orchestrator_v2``), the ``run_artifacts``
    directory itself, or a single run directory. Returns ``(dirs, problems)``.
    """
    resolved: List[Path] = []
    problems: List[str] = []
    for raw in roots:
        root = Path(raw)
        if not root.exists():
            problems.append(f"artifact root does not exist: {root}")
            continue
        if not root.is_dir():
            problems.append(f"artifact root is not a directory: {root}")
            continue
        nested = root / "run_artifacts"
        if nested.is_dir():
            resolved.append(nested)
        elif any((root / marker).is_file() for marker in _MARKER_FILES):
            # A single run directory: treat its parent as the container.
            resolved.append(root.parent)
        else:
            resolved.append(root)
    # Preserve order, drop duplicates.
    seen: set = set()
    unique: List[Path] = []
    for path in resolved:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique, problems


def load_run(run_dir: Path) -> Optional[RunRecord]:
    """Parse one run directory. Returns ``None`` if it is not a run at all."""
    if not run_dir.is_dir():
        return None
    if not any((run_dir / marker).is_file() for marker in _MARKER_FILES):
        return None
    artifacts: Dict[str, Any] = {}
    parse_errors: Dict[str, str] = {}
    for name in ARTIFACT_FILES:
        target = run_dir / name
        if not target.is_file():
            continue
        try:
            artifacts[target.stem] = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parse_errors[name] = str(exc)[:200]

    run_id = str(
        _get(artifacts, "run_status", "run_id")
        or _get(artifacts, "run_manifest", "run_id")
        or run_dir.name
    )
    started = parse_instant(_get(artifacts, "run_manifest", "started_at"))
    finished = parse_instant(_get(artifacts, "run_manifest", "finished_at"))

    if finished is not None:
        attributed, attribution = finished, ATTR_FINISHED
    elif started is not None:
        attributed, attribution = started, ATTR_STARTED
    else:
        from_id = _run_id_instant(run_id)
        attributed, attribution = (from_id, ATTR_RUN_ID) if from_id else (None, ATTR_NONE)

    mode = str(_get(artifacts, "run_manifest", "mode") or "")
    realism, realism_reason = classify_realism(artifacts, mode)

    return RunRecord(
        run_id=run_id,
        path=run_dir,
        artifacts=artifacts,
        parse_errors=parse_errors,
        started_at=started,
        finished_at=finished,
        attributed_at=attributed,
        attribution_field=attribution,
        realism=realism,
        realism_reason=realism_reason,
    )


def resolve_ledger_roots(roots: Sequence[str | os.PathLike]) -> List[Path]:
    """Artifact roots that may hold a ``reporting_ledger`` store.

    Accepts the same shapes ``resolve_run_artifact_dirs`` does -- an artifact root,
    the ``run_artifacts`` directory itself, or a single run directory -- and walks
    up to wherever the ledger would be a sibling of ``run_artifacts``.
    """
    candidates: List[Path] = []
    for raw in roots:
        root = Path(raw)
        if not root.is_dir():
            continue
        options = [root]
        if root.name == "run_artifacts":
            options.append(root.parent)
        elif any((root / marker).is_file() for marker in _MARKER_FILES):
            # A single run directory: the ledger sits beside its container.
            options.append(root.parent.parent)
        for option in options:
            if (option / LEDGER_STORE).is_dir():
                candidates.append(option)
    seen: set = set()
    unique: List[Path] = []
    for path in candidates:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _record_from_ledger(entry: Dict[str, Any], root: Path) -> RunRecord:
    """Build a ``RunRecord`` from a compact ledger entry alone.

    Realism is classified by the SAME rule the heavy artifacts use, fed from the
    policy and lane names the ledger preserved, so a dry run cannot be counted as
    throughput just because its evidence directory was pruned.
    """
    run_id = str(entry.get("run_id"))
    mode = str(entry.get("mode") or "")
    policy = entry.get("policy") if isinstance(entry.get("policy"), dict) else {}
    lane_names = entry.get("lane_names") or []
    shim = {
        "run_manifest": {"policy": dict(policy), "mode": mode},
        "lanes": {str(name): {} for name in lane_names},
    }
    realism, realism_reason = classify_realism(shim, mode)

    started = parse_instant(entry.get("started_at"))
    finished = parse_instant(entry.get("finished_at"))
    if finished is not None:
        attributed, attribution = finished, ATTR_FINISHED
    elif started is not None:
        attributed, attribution = started, ATTR_STARTED
    else:
        from_id = _run_id_instant(run_id)
        attributed, attribution = (from_id, ATTR_RUN_ID) if from_id else (None, ATTR_NONE)

    path = Path(entry.get("_path") or (root / LEDGER_STORE / f"{run_id}.json"))
    return RunRecord(
        run_id=run_id,
        path=path,
        artifacts={LEDGER_STEM: entry},
        started_at=started,
        finished_at=finished,
        attributed_at=attributed,
        attribution_field=attribution,
        realism=realism,
        realism_reason=realism_reason,
    )


def discover_ledger_runs(
    roots: Sequence[str | os.PathLike],
) -> Tuple[Dict[str, RunRecord], List[str]]:
    """Every run in the compact reporting ledger. Returns ``(by_run_id, problems)``."""
    found: Dict[str, RunRecord] = {}
    problems: List[str] = []
    for root in resolve_ledger_roots(roots):
        entries, issues = read_entries(root)
        problems.extend(issues)
        for entry in entries:
            record = _record_from_ledger(entry, root)
            if record.run_id:
                found.setdefault(record.run_id, record)
    return found, problems


def discover_runs(roots: Sequence[str | os.PathLike]) -> Tuple[List[RunRecord], List[str]]:
    """Every run under ``roots``, newest last. Returns ``(runs, problems)``.

    The compact ledger supplies the run INDEX; heavy artifacts are merged onto a
    ledger row when they are still on disk. A run present only in the ledger is a
    normal, fully reportable run whose evidence has aged out; a run present only as
    heavy artifacts is a pre-ledger run and is still read exactly as before.
    """
    directories, problems = resolve_run_artifact_dirs(roots)
    runs, ledger_problems = discover_ledger_runs(roots)
    problems.extend(ledger_problems)
    for directory in directories:
        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            problems.append(f"cannot list {directory}: {str(exc)[:160]}")
            continue
        for child in children:
            record = load_run(child)
            if record is None:
                # A run directory carrying no marker file -- exactly the shape the
                # interrupted Sep 4 control run left behind (200 MB of evidence, no
                # manifest). If the ledger already knows the run it is reported from
                # there; otherwise it must at least be DECLARED. Returning silently,
                # as this did, is how a real run became invisible.
                if child.is_dir() and child.name not in runs:
                    try:
                        non_empty = any(child.iterdir())
                    except OSError:
                        non_empty = False
                    if non_empty:
                        problems.append(
                            f"run directory {child.name} carries no run_status/"
                            "run_manifest/orchestrator_result and no reporting-ledger "
                            "entry: an unfinalized run whose counters cannot be read"
                        )
                continue
            existing = runs.get(record.run_id)
            if existing is None:
                runs[record.run_id] = record
                continue
            # One run, two stores. The ledger row is the index and must survive the
            # merge; the heavy artifacts attach to it as evidence.
            if existing.has_ledger:
                record.artifacts = {**record.artifacts,
                                    LEDGER_STEM: existing.artifacts[LEDGER_STEM]}
            elif len(existing.artifacts) >= len(record.artifacts):
                # A run_id seen under two roots is one run; keep the richer copy.
                continue
            record.started_at = record.started_at or existing.started_at
            record.finished_at = record.finished_at or existing.finished_at
            # Keep whichever store carries the more trustworthy clock, not simply
            # whichever was parsed second.
            if (_ATTRIBUTION_RANK.get(existing.attribution_field, 0)
                    > _ATTRIBUTION_RANK.get(record.attribution_field, 0)):
                record.attributed_at = existing.attributed_at
                record.attribution_field = existing.attribution_field
            runs[record.run_id] = record
    # Two passes: a None timestamp must never be compared against a datetime.
    dated = sorted(
        (r for r in runs.values() if r.attributed_at),
        key=lambda r: (r.attributed_at, r.run_id),
    )
    undated = sorted((r for r in runs.values() if not r.attributed_at), key=lambda r: r.run_id)
    return dated + undated, problems


def select_window(
    runs: Iterable[RunRecord], window: ReportingWindow
) -> Tuple[List[RunRecord], List[RunRecord]]:
    """Split runs into ``(inside_window, unattributable)``.

    Runs with a timestamp outside the window are simply dropped. Runs with **no**
    usable timestamp are returned separately so the report can declare them rather
    than quietly ignoring them.
    """
    inside: List[RunRecord] = []
    unattributable: List[RunRecord] = []
    for run in runs:
        if run.attributed_at is None:
            unattributable.append(run)
            continue
        if window.contains(run.attributed_at):
            inside.append(run)
    return inside, unattributable


def partition_realism(runs: Iterable[RunRecord]) -> Tuple[List[RunRecord], List[RunRecord]]:
    """Split runs into ``(production, simulated)``.

    A dry run writes a full, well-formed artifact set. Counting it would report
    manufactured throughput as a business result, so the two are never mixed.
    """
    production = [run for run in runs if run.realism == REALISM_PRODUCTION]
    simulated = [run for run in runs if run.realism != REALISM_PRODUCTION]
    return production, simulated
