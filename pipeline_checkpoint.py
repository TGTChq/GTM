"""Crash-safe checkpoint for discovered jobs and top-up query progress."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List

import config
from job_filter import dedup_key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(job: Dict) -> str:
    job_id = str(job.get("job_id") or "").strip()
    if job_id:
        return f"id:{job_id}"
    company, title = dedup_key(job)
    return f"dedup:{company}|{title}"


class PipelineCheckpoint:
    def __init__(self, path: str | None = None):
        self.path = Path(path or config.PIPELINE_CHECKPOINT_FILE)
        self.payload = self._load()

    def _load(self) -> Dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"pending_jobs": {}, "query_metrics": {}}
        if not isinstance(data, dict):
            return {"pending_jobs": {}, "query_metrics": {}}
        updated = str(data.get("updated_at") or "")
        try:
            parsed = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return {"pending_jobs": {}, "query_metrics": {}}
        if parsed < _now() - timedelta(hours=48):
            return {"pending_jobs": {}, "query_metrics": {}}
        if str(data.get("validation_version") or "") != config.VALIDATION_VERSION:
            return {"pending_jobs": {}, "query_metrics": {}}
        return data

    def pending_jobs(self) -> List[Dict]:
        return [dict(value) for value in self.payload.get("pending_jobs", {}).values() if isinstance(value, dict)]

    def query_metrics(self) -> Dict:
        return dict(self.payload.get("query_metrics") or {})

    def append_jobs(self, jobs: Iterable[Dict], *, query_metrics: Dict | None = None) -> None:
        pending = self.payload.setdefault("pending_jobs", {})
        for job in jobs:
            pending[_key(job)] = dict(job)
        if query_metrics:
            self.payload["query_metrics"] = query_metrics
        self.save()

    def remove_jobs(self, jobs: Iterable[Dict]) -> None:
        """Remove jobs that reached a downstream disposition.

        Recoverable outcomes are persisted separately by RecoverableJobQueue,
        so retaining processed rows in the crash checkpoint would cause the
        same jobs to be re-injected on every SLA-miss run.
        """
        pending = self.payload.setdefault("pending_jobs", {})
        changed = False
        for job in jobs:
            key = _key(job)
            if key in pending:
                pending.pop(key, None)
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        self.payload["updated_at"] = _now().isoformat()
        self.payload["validation_version"] = config.VALIDATION_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=self.path.parent, suffix=".tmp"
        ) as handle:
            json.dump(self.payload, handle, indent=2)
            temp = handle.name
        os.replace(temp, self.path)

    def clear(self) -> None:
        self.payload = {"pending_jobs": {}, "query_metrics": {}}
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
