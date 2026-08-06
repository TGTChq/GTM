"""One-run lock -- refuse a second definitive run while one is active, but never
block forever on a lock left behind by a container that is already gone.

Lifecycle (one unambiguous order, owned by :meth:`RunLock.acquire`):

1. Read-only preflight happens in the caller BEFORE this.
2. Inspect any existing lock (:func:`read_lock` + :func:`classify_lock`).
3. Atomically (``O_CREAT|O_EXCL``) acquire the lock exactly once.
4. Record: schema version, run_id, pid, created_at, Railway deployment/replica/
   service identity when available, the container ``boot_id``, and a random
   per-invocation ``ownership_token``.
5. The invocation recognises its OWN lock only by that ``ownership_token``.
6. :meth:`release` unlinks only when the on-disk token still matches ours.
7. Never release or delete another live owner's lock.

Staleness is decided by *identity*, not just a pid: a lock whose ``boot_id`` (or,
lacking that, ``deployment_id``) differs from the current container's belongs to a
container that no longer exists, so it is provably stale regardless of pid --
which defeats PID reuse across containers (Railway processes are commonly pid 1).
A lock with no identity fields (a legacy lock) is only auto-recovered once it has
aged past ``stale_seconds``; otherwise it is ``indeterminate`` and requires an
explicit, audited operator recovery. An active or indeterminate lock is NEVER
removed automatically.

No business logic here -- this only guards concurrency.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: Bumped when the on-disk lock contract changes in a breaking way.
RUNLOCK_SCHEMA = "orchestrator-runlock/1"

#: A lock older than this is stale even if we cannot otherwise prove its owner is
#: gone (a safety net for locks that carry no container identity).
DEFAULT_STALE_SECONDS = 6 * 3600

#: Sentinel an operator passes for --expected-ownership-token to recover a legacy
#: lock that predates the ownership-token field.
LEGACY_TOKEN_SENTINEL = "LEGACY-NO-TOKEN"


class RunLockHeld(RuntimeError):
    """Another run holds the lock and it cannot be proven gone."""


# -- environment / process identity ---------------------------------------

def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _pid_alive(pid: int) -> Optional[bool]:
    """True/False on POSIX; None when it cannot be determined (e.g. Windows).

    Only meaningful for a pid in the CURRENT container -- a pid from another
    container is not comparable and must never be trusted (see classify_lock).
    """
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


def _boot_id() -> str:
    """A per-container-boot identifier on Linux; stable within one container,
    different after the container is recreated. Empty when unavailable."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001 - not Linux / not readable
        return ""


def _nodename() -> str:
    try:
        return os.uname().nodename  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no os.uname (Windows)
        return os.environ.get("COMPUTERNAME", "")


def current_identity() -> Dict[str, str]:
    """Identity of the container/deployment this process runs in. Zero network."""
    return {
        "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID", ""),
        "replica_id": os.environ.get("RAILWAY_REPLICA_ID", ""),
        "service": os.environ.get("RAILWAY_SERVICE_NAME", ""),
        "boot_id": _boot_id(),
        "nodename": _nodename(),
    }


# -- lock file read / classify --------------------------------------------

def read_lock(path: str | Path) -> Dict[str, Any]:
    """Parse the lock file. A missing/corrupt/empty lock yields ``{}``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - a corrupt lock is treated as empty
        return {}


def _lock_epoch(info: Dict[str, Any]) -> float:
    return float(info.get("created_at_epoch") or info.get("acquired_at_epoch") or 0)


def classify_lock(
    info: Dict[str, Any],
    *,
    self_identity: Dict[str, str],
    self_token: Optional[str],
    stale_seconds: int,
    now_epoch: float,
) -> Tuple[str, Optional[float]]:
    """Classify a lock relative to the current invocation. Returns
    ``(classification, age_seconds)`` where classification is one of:

    * ``current``        -- we own it (our ownership_token is on disk);
    * ``foreign_active`` -- a DIFFERENT owner we can prove is still alive;
    * ``stale``          -- the owner is provably gone (container/deployment
                            replaced, pid proven dead in-container, or aged out);
    * ``indeterminate``  -- present but we cannot prove alive or dead;
    * ``empty``          -- absent, unreadable, or corrupt.
    """
    if not info:
        return "empty", None

    token = info.get("ownership_token")
    if self_token and token and token == self_token:
        return "current", 0.0

    epoch = _lock_epoch(info)
    age = (now_epoch - epoch) if epoch > 0 else None

    lock_boot = str(info.get("boot_id") or "")
    self_boot = str(self_identity.get("boot_id") or "")
    lock_dep = str(info.get("deployment_id") or "")
    self_dep = str(self_identity.get("deployment_id") or "")

    # 1) Provably-gone container/deployment -> stale regardless of pid. This is
    #    what defeats PID reuse across containers.
    if lock_boot and self_boot and lock_boot != self_boot:
        return "stale", age
    if not lock_boot and lock_dep and self_dep and lock_dep != self_dep:
        return "stale", age

    # 2) Aged out beyond the safety window.
    if age is not None and age >= stale_seconds:
        return "stale", age

    # 3) Same container (boot_id present and matching): a pid check is meaningful.
    if lock_boot and self_boot and lock_boot == self_boot:
        alive = _pid_alive(int(info.get("pid", -1) or -1))
        if alive is False:
            return "stale", age
        if alive is True:
            return "foreign_active", age
        return "indeterminate", age

    # 4) No trustworthy identity to compare (legacy lock, or a different OS):
    #    refuse to guess from a bare pid -> needs explicit recovery.
    return "indeterminate", age


def describe_lock(
    path: str | Path,
    *,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    reveal_token: bool = False,
) -> Dict[str, Any]:
    """Redacted, zero-network description of a lock for inspection / logging.

    ``reveal_token`` is only set by the explicit ``--inspect-run-lock`` operator
    command; normal logs never expose the ownership token.
    """
    info = read_lock(path)
    classification, age = classify_lock(
        info, self_identity=current_identity(), self_token=None,
        stale_seconds=stale_seconds, now_epoch=_now())
    tok = info.get("ownership_token") if info else None
    return {
        "present": bool(info),
        "path": str(path),
        "schema_version": info.get("schema_version") if info else None,
        "run_id": info.get("run_id") if info else None,
        "pid": info.get("pid") if info else None,
        "created_at": (info.get("created_at") or info.get("acquired_at")) if info else None,
        "deployment_id": info.get("deployment_id") if info else None,
        "replica_id": info.get("replica_id") if info else None,
        "service": info.get("service") if info else None,
        "boot_id": info.get("boot_id") if info else None,
        "age_seconds": age,
        "classification": classification,
        "owned_by_current_invocation": False,  # a fresh inspector never owns it
        "has_ownership_token": bool(tok),
        "ownership_token": (tok if reveal_token else None),
        "ownership_token_fingerprint": ((str(tok)[:8] + "...") if tok else None),
    }


def recover_stale_lock(
    path: str | Path,
    *,
    expected_run_id: str,
    expected_token: str,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    audit_dir: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Explicit, audited operator recovery. Removes the lock ONLY when:

    * a lock is present; and
    * its ``run_id`` matches ``expected_run_id`` exactly; and
    * its ``ownership_token`` matches ``expected_token`` exactly (or the lock is a
      legacy lock with no token and ``expected_token`` is the legacy sentinel);
      and
    * it is NOT classifiable as an active owner (``current``/``foreign_active``).

    Any mismatch (the lock changed since inspection) or a verifiably active owner
    is refused. Zero network. Always writes an audit artifact on success.
    """
    path = Path(path)
    info = read_lock(path)
    if not info:
        return {"recovered": False, "reason": "no lock present (nothing to recover)",
                "classification": "empty"}

    classification, age = classify_lock(
        info, self_identity=current_identity(), self_token=None,
        stale_seconds=stale_seconds, now_epoch=_now())
    stored_run = str(info.get("run_id") or "")
    stored_tok = info.get("ownership_token")

    if expected_run_id != stored_run:
        return {"recovered": False, "classification": classification,
                "reason": "run_id mismatch: the lock changed since it was inspected"}
    if stored_tok:
        if expected_token != stored_tok:
            return {"recovered": False, "classification": classification,
                    "reason": "ownership token mismatch: the lock changed since it was inspected"}
    else:
        if expected_token not in (LEGACY_TOKEN_SENTINEL, ""):
            return {"recovered": False, "classification": classification,
                    "reason": f"legacy lock has no ownership token; pass "
                              f"--expected-ownership-token {LEGACY_TOKEN_SENTINEL} to recover it"}
    if classification in ("current", "foreign_active"):
        return {"recovered": False, "classification": classification,
                "reason": f"owner is still verifiable as active ({classification}); refusing"}

    audit = {
        "schema_version": RUNLOCK_SCHEMA,
        "event": "manual_stale_recovery",
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "recovered_by": current_identity(),
        "lock_run_id": stored_run,
        "lock_pid": info.get("pid"),
        "lock_created_at": info.get("created_at") or info.get("acquired_at"),
        "lock_deployment_id": info.get("deployment_id"),
        "lock_boot_id": info.get("boot_id"),
        "classification": classification,
        "age_seconds": age,
        "had_ownership_token": bool(stored_tok),
    }
    audit_path = _write_audit(audit_dir or (path.parent / "run_lock_audit"), audit)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return {"recovered": True, "classification": classification,
            "audit_path": str(audit_path) if audit_path else None, **audit}


def _write_audit(audit_dir: str | Path, payload: Dict[str, Any]) -> Optional[Path]:
    try:
        d = Path(audit_dir)
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = d / f"{stamp}-{payload.get('event', 'event')}.json"
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001 - auditing must never break the run
        return None


class RunLock:
    def __init__(
        self,
        lock_path: str | Path,
        run_id: str,
        *,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
        audit_dir: Optional[str | Path] = None,
    ) -> None:
        self.path = Path(lock_path)
        self.run_id = run_id
        self.stale_seconds = int(stale_seconds)
        self.audit_dir = Path(audit_dir) if audit_dir else (self.path.parent / "run_lock_audit")
        #: Definitive, per-invocation proof of ownership.
        self.ownership_token = secrets.token_hex(16)
        self.identity = current_identity()
        self.acquired = False
        self.recovered_stale = False
        self.recovered_classification = ""
        self.holder: Dict[str, Any] = {}

    # -- write -------------------------------------------------------------

    def _write(self) -> None:
        stamp = datetime.now(timezone.utc)
        payload = {
            "schema_version": RUNLOCK_SCHEMA,
            "run_id": self.run_id,
            "pid": os.getpid(),
            "ownership_token": self.ownership_token,
            "created_at": stamp.isoformat(),
            "created_at_epoch": stamp.timestamp(),
            # retained for backward compatibility with pre-hotfix readers
            "acquired_at": stamp.isoformat(),
            "acquired_at_epoch": stamp.timestamp(),
            **self.identity,
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
            info = read_lock(self.path)
            classification, age = classify_lock(
                info, self_identity=self.identity, self_token=self.ownership_token,
                stale_seconds=self.stale_seconds, now_epoch=_now())
            if classification == "current":
                self.acquired = True
                self.holder = info
                return self
            if classification in ("stale", "empty"):
                # Loud, audited auto-recovery of a provably-dead owner. Retry once.
                self.recovered_stale = True
                self.recovered_classification = classification
                _write_audit(self.audit_dir, {
                    "schema_version": RUNLOCK_SCHEMA,
                    "event": "auto_stale_recovery",
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                    "recovered_by": self.identity,
                    "new_run_id": self.run_id,
                    "lock_run_id": info.get("run_id"),
                    "lock_pid": info.get("pid"),
                    "lock_boot_id": info.get("boot_id"),
                    "lock_deployment_id": info.get("deployment_id"),
                    "classification": classification,
                    "age_seconds": age,
                })
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                self._write()
            else:  # foreign_active / indeterminate
                raise RunLockHeld(self._held_message(info, classification, age))
        self.acquired = True
        return self

    def release(self) -> None:
        """Unlink only when the on-disk ownership token is still ours."""
        if not self.acquired:
            return
        try:
            current = read_lock(self.path)
            if current.get("ownership_token") == self.ownership_token:
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    # -- diagnostics -------------------------------------------------------

    def _held_message(self, info: Dict[str, Any], classification: str,
                      age: Optional[float]) -> str:
        root = self.path.parent
        tok = (LEGACY_TOKEN_SENTINEL if not info.get("ownership_token")
               else "<TOKEN-from --inspect-run-lock>")
        return (
            "a run lock is held and cannot be proven gone "
            f"(classification={classification}, "
            f"run_id={info.get('run_id')}, pid={info.get('pid')}, "
            f"created_at={info.get('created_at') or info.get('acquired_at')}, "
            f"age_seconds={None if age is None else int(age)}, "
            f"deployment={info.get('deployment_id')}, replica={info.get('replica_id')}, "
            f"service={info.get('service')}); this invocation does NOT own it. "
            "Refusing to start a second run. If you are certain the owner is dead, "
            "recover it explicitly:\n"
            f"  python run_orchestrator.py --inspect-run-lock --artifact-root {root}\n"
            f"  python run_orchestrator.py --recover-stale-run-lock --artifact-root {root} "
            f"--expected-run-id {info.get('run_id')} --expected-ownership-token {tok}"
        )

    def to_dict(self) -> Dict[str, Any]:
        holder = dict(self.holder)
        tok = holder.pop("ownership_token", None)  # never surface the raw token
        if tok:
            holder["ownership_token_fingerprint"] = str(tok)[:8] + "..."
        return {
            "lock_path": str(self.path),
            "acquired": self.acquired,
            "recovered_stale": self.recovered_stale,
            "recovered_classification": self.recovered_classification,
            "holder": holder,
            "stale_seconds": self.stale_seconds,
        }
