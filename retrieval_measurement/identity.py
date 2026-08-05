"""Run lineage, redacted configuration capture, and read-only seen-state.

Three responsibilities that all answer "what exactly produced this number?":

* ``run_identity`` / ``git_identity`` -- which code, which commit, which run.
* ``effective_config_snapshot`` -- every config value that shaped the run, with
  provenance, secrets redacted, and a fingerprint so two runs can be compared
  without diffing 180 values by eye.
* ``ReadOnlySeenSnapshot`` -- the previously-seen baseline, read without any
  possibility of mutating production state.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config

from retrieval_measurement.schema import EffectiveConfigEntry

#: Config names whose VALUE must never be persisted. Matched case-insensitively
#: against the attribute name; the value is replaced outright rather than
#: hashed or truncated, because a partial secret is still a secret.
_SECRET_NAME = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|COOKIE|SESSION|SIGNATURE|"
    r"WEBHOOK|DSN|CONNECTION_STRING|APP_ID|API_ID|PAT)",
    re.I,
)

#: Names that match the secret pattern but are not secrets. Kept explicit and
#: short: a wrong entry here leaks a credential, so loosening the pattern is
#: never the right fix.
_SECRET_ALLOWLIST = frozenset({
    "JSEARCH_REMOTE_FILTER_PARAMETER",
})

#: ``*_KEYWORDS`` config lists (e.g. EXCLUDED_TITLE_KEYWORDS) trip the ``KEY``
#: token but are plain filter vocabulary and are needed to explain a run.
_NOT_SECRET = re.compile(r"KEYWORD", re.I)

#: Directories whose contents the harness treats as production state. Reading a
#: seen-jobs snapshot directly out of one of these is refused by default.
PRODUCTION_STATE_MARKERS = (
    Path("data") / "state",
    Path("data") / "raw",
    Path("data") / "filtered",
    Path("data") / "enriched",
    Path("data") / "evidence",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp(moment: Optional[datetime] = None) -> str:
    return (moment or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(moment: Optional[datetime] = None) -> str:
    """Sortable, collision-resistant, filesystem-safe."""
    return f"{(moment or utc_now()):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Code lineage
# --------------------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def git_identity(repo_root: Optional[Path] = None) -> Dict[str, Any]:
    root = Path(repo_root or Path(__file__).resolve().parent.parent)
    commit = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(root, "status", "--porcelain")
    return {
        "commit": commit,
        "branch": branch,
        # None (not False) when git is unavailable: "we could not tell" and
        # "the tree is clean" are different facts.
        "dirty": bool(status) if commit else None,
    }


def python_identity() -> Dict[str, str]:
    return {
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }


# --------------------------------------------------------------------------
# Effective configuration
# --------------------------------------------------------------------------


def _is_secret(name: str) -> bool:
    if name in _SECRET_ALLOWLIST or _NOT_SECRET.search(name):
        return False
    return bool(_SECRET_NAME.search(name))


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _provenance(name: str, environ: Optional[Dict[str, str]] = None) -> str:
    """env when the operator set it, code_default otherwise.

    config.py reads several values under an aliased environment variable
    (``FREE_JOB_SOURCES`` <- ``FREE_JOB_SOURCES_JSON``), so the common suffixes
    are checked too. This is a best-effort attribution and is labelled as such
    in the manifest rather than presented as certain.
    """
    env = os.environ if environ is None else environ
    for candidate in (name, f"{name}_JSON", f"{name}_LIST"):
        if candidate in env:
            return "env"
    return "code_default"


def effective_config_snapshot(
    run_arguments: Optional[Dict[str, Any]] = None,
    environ: Optional[Dict[str, str]] = None,
) -> List[EffectiveConfigEntry]:
    entries: List[EffectiveConfigEntry] = []
    for name in sorted(dir(config)):
        if not name.isupper():
            continue
        value = getattr(config, name)
        if callable(value):
            continue
        if _is_secret(name):
            entries.append(
                EffectiveConfigEntry(
                    name=name,
                    # Presence, not content: enough to explain a run, useless
                    # to an attacker.
                    value={"configured": bool(value)},
                    source=_provenance(name, environ),
                    redacted=True,
                )
            )
            continue
        entries.append(
            EffectiveConfigEntry(
                name=name,
                value=_json_safe(value),
                source=_provenance(name, environ),
                redacted=False,
            )
        )

    for key, value in sorted((run_arguments or {}).items()):
        entries.append(
            EffectiveConfigEntry(
                name=f"RUN_ARG_{key.upper()}",
                value=_json_safe(value),
                source="run_argument",
                redacted=False,
            )
        )
    return entries


def config_fingerprint(entries: List[EffectiveConfigEntry]) -> str:
    payload = json.dumps(
        [[entry.name, entry.value, entry.source] for entry in entries],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def assert_no_secret_values(entries: List[EffectiveConfigEntry]) -> None:
    """Belt and braces: fail loudly if a secret-looking name kept its value."""
    leaked = [
        entry.name
        for entry in entries
        if _is_secret(entry.name) and not entry.redacted
    ]
    if leaked:
        raise RuntimeError(f"refusing to persist unredacted secrets: {sorted(leaked)}")


def credential_state(name: str, *, required: bool = True) -> str:
    """PRESENT / ABSENT / NOT_REQUIRED -- never the value.

    ``bool()`` is the only operation performed on the value. Nothing is
    returned, logged or stored from which any part of it could be recovered.
    """
    if not required:
        return "NOT_REQUIRED"
    value = getattr(config, name, None)
    if value is None:
        value = os.environ.get(name, "")
    return "PRESENT" if str(value).strip() else "ABSENT"


def _secret_values() -> List[str]:
    values: List[str] = []
    for name in dir(config):
        if not name.isupper() or not _is_secret(name):
            continue
        value = getattr(config, name, "")
        if isinstance(value, str) and len(value.strip()) >= 8:
            values.append(value.strip())
    return sorted(set(values), key=len, reverse=True)


def redact_text(text: str, *, limit: int = 500) -> str:
    """Scrub any configured secret out of free text, then truncate.

    Provider libraries put request URLs into exception messages, and a URL can
    carry a key in its query string. An error message is the one artifact
    nobody reviews before it is written, so it is scrubbed unconditionally
    rather than when it looks risky.
    """
    cleaned = str(text)
    for secret in _secret_values():
        cleaned = cleaned.replace(secret, "<redacted>")
    cleaned = re.sub(
        r"((?:api[-_]?key|key|token|secret|password|app_key|app_id)=)[^&\s\"']+",
        r"\1<redacted>",
        cleaned,
        flags=re.I,
    )
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "... (truncated)"
    return cleaned


# --------------------------------------------------------------------------
# Read-only seen state
# --------------------------------------------------------------------------


class ProductionStatePathRefused(RuntimeError):
    """Raised when a snapshot path points into live production state."""


def _looks_like_production_state(path: Path) -> bool:
    resolved = path.resolve()
    parts = [part.lower() for part in resolved.parts]
    for marker in PRODUCTION_STATE_MARKERS:
        marker_parts = [part.lower() for part in marker.parts]
        for index in range(len(parts) - len(marker_parts) + 1):
            if parts[index:index + len(marker_parts)] == marker_parts:
                return True
    return False


class ReadOnlySeenSnapshot:
    """A previously-seen baseline that cannot mutate anything.

    ``pipeline_state.SeenJobsRegistry`` is unusable here. Constructing it calls
    ``_load()``, which creates the parent directory (pipeline_state.py:38) and,
    on a JSON error, MOVES the file aside with ``os.replace``
    (pipeline_state.py:45-46). A measurement run must never be able to do that
    to production state, so this class re-implements the two read paths --
    ``has_job_id`` and ``has_dedup_key`` -- and the 30-day prune, over a file it
    opens read-only and never writes back.
    """

    def __init__(
        self,
        job_ids: Optional[Dict[str, str]] = None,
        dedup_keys: Optional[Dict[str, str]] = None,
        *,
        path: str = "",
        retention_days: Optional[int] = None,
        pruned: int = 0,
    ) -> None:
        self.job_ids: Dict[str, str] = dict(job_ids or {})
        self.dedup_keys: Dict[str, str] = dict(dedup_keys or {})
        self.path = path
        self.retention_days = (
            config.SEEN_JOBS_RETENTION_DAYS if retention_days is None else int(retention_days)
        )
        self.pruned = int(pruned)

    # -- construction ------------------------------------------------------

    @classmethod
    def empty(cls) -> "ReadOnlySeenSnapshot":
        return cls()

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        *,
        allow_production_path: bool = False,
        now: Optional[datetime] = None,
        retention_days: Optional[int] = None,
    ) -> "ReadOnlySeenSnapshot":
        target = Path(path)
        if not allow_production_path and _looks_like_production_state(target):
            raise ProductionStatePathRefused(
                f"{target} resolves inside a production state directory. Copy the "
                "snapshot to a harness-owned path, or pass "
                "--allow-production-snapshot-copy if the path is already a copy."
            )
        if not target.is_file():
            raise FileNotFoundError(f"seen-jobs snapshot not found: {target}")

        # Read-only, and parsed defensively. A malformed snapshot is an error
        # to report, never a file to move.
        with open(target, "r", encoding="utf-8") as handle:
            raw = handle.read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"seen-jobs snapshot is not valid JSON: {target} ({exc})") from exc
        if not isinstance(data, dict):
            raise ValueError(f"seen-jobs snapshot must be a JSON object: {target}")

        days = (
            int(data.get("retention_days") or config.SEEN_JOBS_RETENTION_DAYS)
            if retention_days is None
            else int(retention_days)
        )
        job_ids = {str(k): str(v) for k, v in dict(data.get("job_ids") or {}).items()}
        dedup_keys = {str(k): str(v) for k, v in dict(data.get("dedup_keys") or {}).items()}
        before = len(job_ids) + len(dedup_keys)
        job_ids, dedup_keys = _prune(job_ids, dedup_keys, days, now)
        after = len(job_ids) + len(dedup_keys)
        return cls(
            job_ids,
            dedup_keys,
            path=str(target),
            retention_days=days,
            pruned=before - after,
        )

    # -- read paths (mirroring SeenJobsRegistry) ---------------------------

    @staticmethod
    def serialize_key(key: Tuple[str, str]) -> str:
        return f"{key[0]}|{key[1]}"

    def has_job_id(self, job_id: str) -> bool:
        return bool(job_id) and job_id in self.job_ids

    def has_dedup_key(self, key: Tuple[str, str]) -> bool:
        return self.serialize_key(key) in self.dedup_keys

    @property
    def total_tracked(self) -> int:
        return len(self.job_ids)

    def describe(self) -> Dict[str, Any]:
        return {
            "available": bool(self.path),
            "path": self.path,
            "job_ids": len(self.job_ids),
            "dedup_keys": len(self.dedup_keys),
            "retention_days": self.retention_days,
            "pruned_entries": self.pruned,
            "write_capable": False,
        }


class RegistryWriteRefused(RuntimeError):
    """Raised when anything tries to mutate the harness's stand-in registry."""


class NonWritingRegistry:
    """Explicit, non-writing substitute for ``pipeline_state.SeenJobsRegistry``.

    ``run_daily_scrape`` does ``registry = registry or SeenJobsRegistry()``
    (jsearch_scraper.py:627). Passing no registry therefore constructs the
    production one, whose ``_load`` creates ``data/state/`` and can ``os.replace``
    a corrupt seen-jobs file -- inside a run whose entire premise is that it
    touches no production state.

    Supplying this object explicitly removes that branch. It implements exactly
    the read surface the scrape path uses (``has_job_id``; ``has_dedup_key`` and
    ``serialize_key`` for callers that reach for them) and turns every write
    method into a loud failure. It opens no file and holds no path: its contents
    come only from a ``ReadOnlySeenSnapshot`` the operator supplied, or from
    nothing at all.
    """

    def __init__(self, snapshot: Optional["ReadOnlySeenSnapshot"] = None) -> None:
        self.snapshot = snapshot
        self.path = ""
        self.job_ids: Dict[str, str] = dict(snapshot.job_ids) if snapshot else {}
        self.dedup_keys: Dict[str, str] = dict(snapshot.dedup_keys) if snapshot else {}
        self.lookups = 0

    @staticmethod
    def serialize_key(key: Tuple[str, str]) -> str:
        return f"{key[0]}|{key[1]}"

    def has_job_id(self, job_id: str) -> bool:
        self.lookups += 1
        return bool(job_id) and job_id in self.job_ids

    def has_dedup_key(self, key: Tuple[str, str]) -> bool:
        self.lookups += 1
        return self.serialize_key(key) in self.dedup_keys

    # -- write surface: present so a call fails loudly instead of silently
    #    falling through to __getattr__ or to a real registry later ---------

    def save(self, *_args: Any, **_kwargs: Any) -> None:
        raise RegistryWriteRefused(
            "the measurement harness must never write seen-jobs state"
        )

    def mark_jobs(self, *_args: Any, **_kwargs: Any) -> None:
        raise RegistryWriteRefused(
            "the measurement harness must never record jobs as seen"
        )

    def mark_dedup_keys(self, *_args: Any, **_kwargs: Any) -> None:
        raise RegistryWriteRefused(
            "the measurement harness must never record dedupe keys as seen"
        )

    def _load(self) -> None:
        raise RegistryWriteRefused("NonWritingRegistry never loads from disk")

    def _prune(self) -> None:
        raise RegistryWriteRefused("NonWritingRegistry never rewrites state")

    def describe(self) -> Dict[str, Any]:
        return {
            "implementation": "NonWritingRegistry",
            "backed_by_snapshot": bool(self.snapshot),
            "job_ids": len(self.job_ids),
            "dedup_keys": len(self.dedup_keys),
            "lookups": self.lookups,
            "write_capable": False,
            "path": "",
        }


def _safe_date(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _prune(
    job_ids: Dict[str, str],
    dedup_keys: Dict[str, str],
    retention_days: int,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Identical retention semantics to SeenJobsRegistry._prune (lines 52-63):
    entries with an unparseable or expired stamp are dropped."""
    cutoff = (now or datetime.now()) - timedelta(days=retention_days)
    keep_ids = {
        key: value
        for key, value in job_ids.items()
        if (parsed := _safe_date(value)) is not None and parsed >= cutoff
    }
    keep_keys = {
        key: value
        for key, value in dedup_keys.items()
        if (parsed := _safe_date(value)) is not None and parsed >= cutoff
    }
    return keep_ids, keep_keys


def run_identity(
    mode: str,
    run_arguments: Optional[Dict[str, Any]] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    entries = effective_config_snapshot(run_arguments)
    assert_no_secret_values(entries)
    git = git_identity(repo_root)
    python = python_identity()
    return {
        "run_id": new_run_id(),
        "mode": mode,
        "started_at": utc_stamp(),
        "git_commit": git["commit"],
        "git_branch": git["branch"],
        "git_dirty": git["dirty"],
        "python_version": python["python_version"],
        "platform": python["platform"],
        "effective_config": [entry.to_dict() for entry in entries],
        "config_fingerprint": config_fingerprint(entries),
    }
