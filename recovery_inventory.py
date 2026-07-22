"""Persistent queues for recoverable jobs and not-yet-persisted FINAL_PASS leads.

These queues prevent temporary source/contact/API failures from turning into
permanent recall loss when the original JSearch date window moves on.
"""

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


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _key(job: Dict) -> str:
    job_id = str(job.get("job_id") or job.get("canonical_job_id") or "").strip()
    if job_id:
        return f"id:{job_id}"
    company, title = dedup_key(job)
    return f"dedup:{company}|{title}"


def _atomic_write(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp"
    ) as handle:
        json.dump(payload, handle, indent=2)
        temp = handle.name
    os.replace(temp, path)


class RecoverableJobQueue:
    def __init__(self, path: str | None = None):
        self.path = Path(path or config.RECOVERABLE_JOBS_FILE)
        self.payload = self._load()

    def _load(self) -> Dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"jobs": {}}
        except (OSError, json.JSONDecodeError):
            return {"jobs": {}}

    def due_jobs(self) -> List[Dict]:
        jobs = self.payload.setdefault("jobs", {})
        output: List[Dict] = []
        changed = False
        ttl_cutoff = _now() - timedelta(days=max(1, config.RECOVERABLE_JOB_TTL_DAYS))
        for key, record in list(jobs.items()):
            created = _parse(str(record.get("created_at") or ""))
            attempts = int(record.get("attempts") or 0)
            due = _parse(str(record.get("next_retry_at") or "")) or _now()
            if not created or created < ttl_cutoff or attempts >= max(1, config.RECOVERABLE_JOB_MAX_ATTEMPTS):
                jobs.pop(key, None)
                changed = True
                continue
            if due <= _now() and isinstance(record.get("job"), dict):
                item = dict(record["job"])
                item["_recovery_queue_key"] = key
                item["_recovery_attempt"] = attempts + 1
                output.append(item)
                record["attempts"] = attempts + 1
                delay_hours = min(48, 2 ** min(5, attempts))
                record["next_retry_at"] = (_now() + timedelta(hours=delay_hours)).isoformat()
                record["last_attempt_at"] = _now().isoformat()
                changed = True
        if changed:
            self.save()
        return output

    def upsert(self, jobs_to_retry: Iterable[Dict]) -> None:
        jobs = self.payload.setdefault("jobs", {})
        for job in jobs_to_retry:
            key = _key(job)
            current = jobs.get(key) or {}
            jobs[key] = {
                "created_at": current.get("created_at") or _now().isoformat(),
                "updated_at": _now().isoformat(),
                "next_retry_at": current.get("next_retry_at") or (_now() + timedelta(hours=1)).isoformat(),
                "attempts": int(current.get("attempts") or 0),
                "state": str(job.get("_final_state") or ""),
                "reason": str(job.get("_final_primary_reason") or ""),
                "job": dict(job),
            }
        self.save()

    def remove(self, jobs_to_remove: Iterable[Dict]) -> None:
        jobs = self.payload.setdefault("jobs", {})
        for job in jobs_to_remove:
            jobs.pop(_key(job), None)
            recovery_key = str(job.get("_recovery_queue_key") or "")
            if recovery_key:
                jobs.pop(recovery_key, None)
        self.save()

    def save(self) -> None:
        self.payload["updated_at"] = _now().isoformat()
        _atomic_write(self.path, self.payload)


class FinalPassInventory:
    """Short-lived safety inventory used only when Airtable persistence fails."""

    def __init__(self, path: str | None = None):
        self.path = Path(path or config.FINAL_PASS_INVENTORY_FILE)
        self.payload = self._load()

    def _load(self) -> Dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"leads": {}}
        except (OSError, json.JSONDecodeError):
            return {"leads": {}}

    def valid_leads(self) -> List[Dict]:
        leads = self.payload.setdefault("leads", {})
        cutoff = _now() - timedelta(days=max(1, config.FINAL_PASS_INVENTORY_TTL_DAYS))
        output: List[Dict] = []
        changed = False
        for key, record in list(leads.items()):
            stored = _parse(str(record.get("stored_at") or ""))
            if not stored or stored < cutoff:
                leads.pop(key, None)
                changed = True
                continue
            if isinstance(record.get("lead"), dict):
                output.append(dict(record["lead"]))
        if changed:
            self.save()
        return output

    def stage(self, leads_to_stage: Iterable[Dict]) -> None:
        leads = self.payload.setdefault("leads", {})
        for lead in leads_to_stage:
            if str(lead.get("_final_state") or "") != "FINAL_PASS":
                continue
            leads[_key(lead)] = {"stored_at": _now().isoformat(), "lead": dict(lead)}
        self.save()

    def remove(self, leads_to_remove: Iterable[Dict]) -> None:
        leads = self.payload.setdefault("leads", {})
        for lead in leads_to_remove:
            leads.pop(_key(lead), None)
        self.save()

    def save(self) -> None:
        self.payload["updated_at"] = _now().isoformat()
        _atomic_write(self.path, self.payload)
