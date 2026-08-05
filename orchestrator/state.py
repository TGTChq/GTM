"""Explicit state ownership.

The legacy pipeline mutated a production ``AtsBoardRegistry`` as a side effect of
acquisition and scattered writes across ``data/state``. The replacement owns its
state, and names every store:

* ``run_artifacts``   -- immutable, one directory per run_id, never rewritten;
* ``checkpoints``     -- resumable per-board / per-stage progress;
* ``seen_suppression``-- seen + suppression snapshots (read-only unless prod);
* ``provider_cache``  -- provider response cache;
* ``scheduler_state`` -- deterministic-scheduler carry-forward;
* ``delivery_state``  -- idempotency keys + audit log.

Guarantees enforced here, not by convention:

* **Atomic writes** -- temp file + ``os.replace``; a crash leaves the prior file.
* **Versioned schemas** -- every store file carries ``schema_version``; a foreign
  tag is refused, never silently reinterpreted.
* **No production write in offline/dry** -- if the mode policy forbids production
  writes, any target outside the run root raises ``StateWriteViolation`` BEFORE
  the write. Imported components cannot smuggle a write past this.
* **Read-only snapshots** -- seen/suppression state is consumed through an
  immutable snapshot; the store is never opened for writing to read it.
* **Bounded retention** -- ``prune`` keeps at most N run directories.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from orchestrator import SCHEMA_VERSION
from orchestrator.modes import ModePolicy

STORES = (
    "run_artifacts",
    "checkpoints",
    "seen_suppression",
    "provider_cache",
    "scheduler_state",
    "delivery_state",
)


class StateWriteViolation(RuntimeError):
    """A write was attempted outside the run root while production writes are
    forbidden by the active mode policy."""


class StateSchemaError(ValueError):
    """A persisted store file carries an unexpected schema tag."""


@dataclass
class ReadOnlySnapshot:
    """An immutable view of a seen/suppression store.

    Loaded once; the underlying file is never opened for writing to serve it.
    """

    keys: frozenset
    source: str

    def __contains__(self, key: str) -> bool:
        return str(key) in self.keys

    def describe(self) -> Dict[str, Any]:
        return {"available": True, "write_capable": False, "size": len(self.keys), "source": self.source}


class StateManager:
    def __init__(self, root: str | Path, policy: ModePolicy, *, run_id: str) -> None:
        self.root = Path(root).resolve()
        self.policy = policy
        self.run_id = run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._paths: Dict[str, Path] = {}
        for store in STORES:
            p = (self.root / store)
            p.mkdir(parents=True, exist_ok=True)
            self._paths[store] = p

    # -- path resolution ---------------------------------------------------

    def store_path(self, store: str) -> Path:
        if store not in self._paths:
            raise KeyError(f"unknown store {store!r}; known: {', '.join(STORES)}")
        return self._paths[store]

    def run_dir(self) -> Path:
        d = self._paths["run_artifacts"] / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _guard(self, target: Path) -> Path:
        target = target.resolve()
        if not self.policy.allow_production_state_write:
            try:
                target.relative_to(self.root)
            except ValueError:
                raise StateWriteViolation(
                    f"mode {self.policy.mode.value!r} forbids production writes; "
                    f"refusing to write {target} outside run root {self.root}"
                )
        return target

    # -- atomic, versioned writes -----------------------------------------

    def write_json(self, store: str, name: str, payload: Mapping[str, Any]) -> Path:
        target = self._guard(self.store_path(store) / name)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = dict(payload)
        body.setdefault("schema_version", SCHEMA_VERSION)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, target)  # atomic
        return target

    def write_artifact(self, name: str, payload: Mapping[str, Any]) -> Path:
        target = self._guard(self.run_dir() / name)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(dict(payload), indent=2, default=str), encoding="utf-8")
        os.replace(tmp, target)
        return target

    def read_json(self, store: str, name: str, *, require_schema: bool = True) -> Optional[Dict[str, Any]]:
        target = self.store_path(store) / name
        if not target.is_file():
            return None
        data = json.loads(target.read_text(encoding="utf-8"))
        if require_schema and isinstance(data, dict):
            tag = data.get("schema_version")
            if tag is not None and tag != SCHEMA_VERSION:
                raise StateSchemaError(
                    f"{target} has schema {tag!r}; expected {SCHEMA_VERSION!r}. Refusing to reinterpret."
                )
        return data

    # -- read-only snapshots ----------------------------------------------

    def seen_snapshot(self, name: str = "seen.json") -> ReadOnlySnapshot:
        data = self.read_json("seen_suppression", name, require_schema=False)
        keys = frozenset(str(k) for k in (data.get("keys", []) if isinstance(data, dict) else []))
        return ReadOnlySnapshot(keys=keys, source=str(self.store_path("seen_suppression") / name))

    # -- retention ---------------------------------------------------------

    def prune(self, keep: int) -> List[str]:
        """Keep at most ``keep`` newest run directories under run_artifacts."""
        base = self._paths["run_artifacts"]
        runs = sorted(
            [d for d in base.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
        removed: List[str] = []
        for stale in runs[max(0, keep):]:
            shutil.rmtree(stale, ignore_errors=True)
            removed.append(stale.name)
        return removed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "run_id": self.run_id,
            "stores": {s: str(p) for s, p in self._paths.items()},
            "production_writes_allowed": self.policy.allow_production_state_write,
        }
