"""One-run lock -- refuse a second definitive run while one is active.

Atomic (``O_CREAT|O_EXCL``) lock file recording pid + run_id + timestamp. A
second invocation refuses while the lock is held and fresh; a stale lock (older
than ``stale_seconds``, or held by a pid that is no longer alive on POSIX) is
recovered explicitly and loudly. Released in a ``finally``. The lock state is
recorded in the run artifacts so a run is always attributable to its lock.

No business logic here -- this only guards concurrency.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class RunLockHeld(RuntimeError):
    """Another run holds the lock and it is not stale."""


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _pid_alive(pid: int) -> Optional[bool]:
    """True/False on POSIX; None when it cannot be determined (e.g. Windows)."""
    if pid <= 0:
        return False
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


class RunLock:
    def __init__(self, lock_path: str | Path, run_id: str, *, stale_seconds: int = 6 * 3600) -> None:
        self.path = Path(lock_path)
        self.run_id = run_id
        self.stale_seconds = int(stale_seconds)
        self.acquired = False
        self.recovered_stale = False
        self.holder: Dict[str, Any] = {}

    # -- staleness ---------------------------------------------------------

    def _read(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt lock is treated as stale
            return {}

    def _is_stale(self, info: Dict[str, Any]) -> bool:
        age = _now() - float(info.get("acquired_at_epoch", 0) or 0)
        if age >= self.stale_seconds:
            return True
        alive = _pid_alive(int(info.get("pid", -1) or -1))
        return alive is False  # only reclaim when we can PROVE the pid is gone

    def _write(self) -> None:
        payload = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "acquired_at_epoch": _now(),
            "host": os.environ.get("RAILWAY_SERVICE_NAME", "") or os.uname().nodename
            if hasattr(os, "uname") else "",
        }
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        self.holder = payload

    # -- acquire / release -------------------------------------------------

    def acquire(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._write()
        except FileExistsError:
            info = self._read()
            if self._is_stale(info):
                # Explicit, loud stale recovery: remove and retry exactly once.
                self.recovered_stale = True
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                self._write()
            else:
                raise RunLockHeld(
                    f"a run is already active (run_id={info.get('run_id')}, "
                    f"pid={info.get('pid')}, since {info.get('acquired_at')}); refusing "
                    "to start a second run. Wait for it, or clear the lock if you are "
                    "certain it is dead."
                )
        self.acquired = True
        return self

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = self._read()
            if current.get("pid") == os.getpid() and current.get("run_id") == self.run_id:
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lock_path": str(self.path),
            "acquired": self.acquired,
            "recovered_stale": self.recovered_stale,
            "holder": dict(self.holder),
            "stale_seconds": self.stale_seconds,
        }
