"""Run identity, status and stop-reason ownership.

One ``RunContext`` per run. It carries the single ``run_id`` every artifact is
stamped with, the redacted effective configuration, the config fingerprint, the
git provenance, and the run's status/stop-reason. It reuses the already-validated
``retrieval_measurement.identity`` primitives rather than re-deriving them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from retrieval_measurement.identity import (
    redact_text,
    run_identity,
    utc_stamp,
)

from orchestrator import ORCHESTRATOR_VERSION, SCHEMA_VERSION
from orchestrator.modes import ExecutionMode, ModePolicy, policy_for


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    RESUMED = "resumed"


@dataclass
class RunContext:
    mode: ExecutionMode
    policy: ModePolicy
    run_id: str
    started_at: str
    git_commit: str
    git_branch: str
    git_dirty: bool
    python_version: str
    platform: str
    config_fingerprint: str
    effective_config: Dict[str, Any]
    run_arguments: Dict[str, Any]
    finished_at: str = ""
    status: RunStatus = RunStatus.RUNNING
    stop_reason: str = ""
    resumed_from: str = ""
    notes: list = field(default_factory=list)

    @classmethod
    def create(
        cls,
        mode: ExecutionMode,
        run_arguments: Optional[Dict[str, Any]] = None,
        *,
        run_id: Optional[str] = None,
    ) -> "RunContext":
        mode = ExecutionMode(mode)
        args = dict(run_arguments or {})
        args.setdefault("mode", mode.value)
        identity = run_identity(mode.value, args)
        return cls(
            mode=mode,
            policy=policy_for(mode),
            run_id=run_id or identity["run_id"],
            started_at=identity["started_at"],
            git_commit=identity["git_commit"],
            git_branch=identity["git_branch"],
            git_dirty=identity["git_dirty"],
            python_version=identity["python_version"],
            platform=identity["platform"],
            config_fingerprint=identity["config_fingerprint"],
            effective_config=identity["effective_config"],
            run_arguments=args,
        )

    def finish(self, status: RunStatus, stop_reason: str = "") -> None:
        self.status = status
        self.stop_reason = redact_text(stop_reason)
        self.finished_at = utc_stamp()

    def note(self, message: str) -> None:
        self.notes.append(redact_text(str(message)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "mode": self.mode.value,
            "policy": self.policy.to_dict(),
            "status": self.status.value,
            "stop_reason": self.stop_reason,
            "resumed_from": self.resumed_from,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
            "git_dirty": self.git_dirty,
            "python_version": self.python_version,
            "platform": self.platform,
            "config_fingerprint": self.config_fingerprint,
            "effective_config": self.effective_config,
            "run_arguments": self.run_arguments,
            "notes": list(self.notes),
        }
