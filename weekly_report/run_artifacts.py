"""Read-only discovery of orchestrator run artifacts.

The orchestrator writes one immutable directory per run under
``<artifact-root>/run_artifacts/<run_id>/`` containing ``run_manifest.json``,
``run_status.json``, ``waterfall.json``, ``delivery.json``, ``capacity_report.json``
and ``orchestrator_result.json`` (see ``orchestrator/state.py`` and
``orchestrator/pipeline.py``). Those directories are the authoritative record of
**what the pipeline actually did**, and each is stamped with the run's own
``started_at`` / ``finished_at``.

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

from weekly_report.timewindow import ReportingWindow, iso_z, parse_instant

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
    def status(self) -> str:
        return str(
            _get(self.artifacts, "run_status", "status")
            or _get(self.artifacts, "run_manifest", "status")
            or "unknown"
        )

    @property
    def mode(self) -> str:
        return str(_get(self.artifacts, "run_manifest", "mode") or "unknown")

    @property
    def stop_reason(self) -> str:
        return str(
            _get(self.artifacts, "run_status", "stop_reason")
            or _get(self.artifacts, "run_manifest", "stop_reason")
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


def discover_runs(roots: Sequence[str | os.PathLike]) -> Tuple[List[RunRecord], List[str]]:
    """Every run under ``roots``, newest last. Returns ``(runs, problems)``."""
    directories, problems = resolve_run_artifact_dirs(roots)
    runs: Dict[str, RunRecord] = {}
    for directory in directories:
        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            problems.append(f"cannot list {directory}: {str(exc)[:160]}")
            continue
        for child in children:
            record = load_run(child)
            if record is None:
                continue
            # A run_id seen under two roots is one run; keep the richer copy.
            existing = runs.get(record.run_id)
            if existing is None or len(record.artifacts) > len(existing.artifacts):
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
