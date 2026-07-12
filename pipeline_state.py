"""Persistent state for cross-day deduplication.

State is committed only after the lead has successfully reached Airtable. This
prevents a failed downstream run from permanently suppressing valid jobs.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import config


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _safe_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


class SeenJobsRegistry:
    def __init__(self, path: Optional[str] = None):
        self.path = path or config.SEEN_JOBS_FILE
        self.job_ids: Dict[str, str] = {}
        self.dedup_keys: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(self.path).exists():
            return
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Preserve the damaged file for inspection rather than crashing daily runs.
            backup = f"{self.path}.corrupt.{datetime.now():%Y%m%d%H%M%S}"
            os.replace(self.path, backup)
            return
        self.job_ids = dict(data.get("job_ids", {}))
        self.dedup_keys = dict(data.get("dedup_keys", {}))
        self._prune()

    def _prune(self) -> None:
        cutoff = datetime.now() - timedelta(days=config.SEEN_JOBS_RETENTION_DAYS)
        self.job_ids = {
            key: value
            for key, value in self.job_ids.items()
            if (parsed := _safe_date(value)) is not None and parsed >= cutoff
        }
        self.dedup_keys = {
            key: value
            for key, value in self.dedup_keys.items()
            if (parsed := _safe_date(value)) is not None and parsed >= cutoff
        }

    def save(self) -> None:
        payload = {
            "updated_at": datetime.now().isoformat(),
            "retention_days": config.SEEN_JOBS_RETENTION_DAYS,
            "job_ids": self.job_ids,
            "dedup_keys": self.dedup_keys,
        }
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=target.parent, suffix=".tmp"
        ) as handle:
            json.dump(payload, handle, indent=2)
            temp_path = handle.name
        os.replace(temp_path, target)

    @staticmethod
    def serialize_key(key: Tuple[str, str]) -> str:
        return f"{key[0]}|{key[1]}"

    def has_job_id(self, job_id: str) -> bool:
        return bool(job_id) and job_id in self.job_ids

    def has_dedup_key(self, key: Tuple[str, str]) -> bool:
        return self.serialize_key(key) in self.dedup_keys

    def mark_jobs(self, jobs: list, date: Optional[str] = None) -> None:
        from job_filter import dedup_key

        stamp = date or _today()
        for job in jobs:
            job_id = str(job.get("job_id") or "").strip()
            if job_id:
                self.job_ids.setdefault(job_id, stamp)
            key = dedup_key(job)
            if key[0] and key[1]:
                self.dedup_keys.setdefault(self.serialize_key(key), stamp)
        self.save()

    @property
    def total_tracked(self) -> int:
        return len(self.job_ids)
