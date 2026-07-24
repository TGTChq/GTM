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
from domain_utils import normalize_company_domain
from job_filter import dedup_key, normalize_text
from review_policy import is_airtable_reviewable


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




def _account_key(lead: Dict) -> str:
    domain = normalize_company_domain(
        lead.get("canonical_domain")
        or lead.get("company_domain")
        or lead.get("employer_website")
        or lead.get("website")
        or ""
    )
    if domain:
        return f"domain:{domain}"
    name = normalize_text(
        lead.get("canonical_employer_name")
        or lead.get("employer_name")
        or lead.get("company_name")
        or ""
    )
    return f"name:{name}" if name else ""


def _priority_score(lead: Dict) -> float:
    """Rank READY leads without turning ranking into another qualification gate."""
    score = 0.0
    confidence = str(lead.get("job_signal_confidence") or "").lower()
    score += 30 if confidence == "official" else 18 if confidence == "corroborated" else 0
    try:
        age = float(lead.get("job_age_days"))
        score += max(0.0, 16.0 - min(8.0, age) * 2.0)
    except (TypeError, ValueError):
        pass
    tier = str(lead.get("hiring_manager_selection_tier") or "").lower()
    score += {"direct": 15, "functional_exec": 10, "founder_fallback": 4}.get(tier, 0)
    apollo = str(lead.get("apollo_email_status") or "").lower()
    hunter = str(lead.get("hunter_email_status") or "").lower()
    score += 15 if apollo == "verified" and hunter == "valid" else 10 if (apollo == "verified" or hunter == "valid") else 0
    try:
        employees = int(lead.get("company_employee_count"))
        score += 5 if 50 <= employees <= 500 else 2 if 25 <= employees <= 1000 else 0
    except (TypeError, ValueError):
        pass
    return round(score, 2)


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
    """Persistent inventory of actionable leads waiting for Airtable delivery.

    The inventory is deliberately small: READY leads are retained across runs,
    reserved before a push, and marked sent only after Airtable confirms that
    they were created or already existed. A transport failure therefore cannot
    silently discard a qualified lead.
    """

    READY_UNUSED = "READY_UNUSED"
    RESERVED_FOR_PUSH = "RESERVED_FOR_PUSH"
    SENT_TO_AIRTABLE = "SENT_TO_AIRTABLE"

    def __init__(self, path: str | None = None):
        self.path = Path(path or config.FINAL_PASS_INVENTORY_FILE)
        self.payload = self._load()

    def _load(self) -> Dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"leads": {}}
            # A reservation has meaning only inside one live process. If the
            # process restarted, any persisted reservation is stale and must be
            # available again rather than disappearing until TTL expiry.
            for record in (data.get("leads") or {}).values():
                if not isinstance(record, dict):
                    continue
                if not record.get("status") or record.get("status") == self.RESERVED_FOR_PUSH:
                    record["status"] = self.READY_UNUSED
            return data
        except (OSError, json.JSONDecodeError):
            return {"leads": {}}

    @staticmethod
    def _inventory_key(lead: Dict) -> str:
        lead_key = str(lead.get("lead_key") or "").strip()
        return f"lead:{lead_key}" if lead_key else _key(lead)

    def _prune(self) -> None:
        leads = self.payload.setdefault("leads", {})
        ttl_days = max(1, int(getattr(config, "READY_INVENTORY_TTL_DAYS", 7)))
        cutoff = _now() - timedelta(days=ttl_days)
        changed = False
        for key, record in list(leads.items()):
            stored = _parse(str(record.get("stored_at") or ""))
            status = str(record.get("status") or self.READY_UNUSED)
            lead = record.get("lead") if isinstance(record.get("lead"), dict) else {}
            validation_time = _parse(str(lead.get("_validation_timestamp") or "")) or stored
            age_at_validation = lead.get("job_age_days")
            job_too_old = False
            try:
                if age_at_validation is not None and validation_time is not None:
                    elapsed_days = max(0, (_now() - validation_time).days)
                    job_too_old = (float(age_at_validation) + elapsed_days) >= float(config.MAX_JOB_AGE_DAYS)
            except (TypeError, ValueError):
                job_too_old = False
            # Sent records are retained for the TTL as a local idempotency aid;
            # unsent records also expire when the hiring signal exceeds the
            # rolling freshness window.
            if not stored or stored < cutoff or job_too_old or status not in {
                self.READY_UNUSED,
                self.RESERVED_FOR_PUSH,
                self.SENT_TO_AIRTABLE,
            }:
                leads.pop(key, None)
                changed = True
        if changed:
            self.save()

    def stage(self, leads_to_stage: Iterable[Dict]) -> None:
        leads = self.payload.setdefault("leads", {})
        sent_accounts = {
            _account_key(record.get("lead") or {})
            for record in leads.values()
            if isinstance(record, dict)
            and record.get("status") == self.SENT_TO_AIRTABLE
            and isinstance(record.get("lead"), dict)
        }
        for lead in leads_to_stage:
            state = str(lead.get("_final_state") or "")
            if state == "FINAL_PASS":
                pass
            elif not is_airtable_reviewable(lead):
                continue
            account = _account_key(lead)
            if account and account in sent_accounts:
                continue
            key = self._inventory_key(lead)
            current = leads.get(key) or {}
            if current.get("status") == self.SENT_TO_AIRTABLE:
                continue
            staged = dict(lead)
            staged["priority_score"] = _priority_score(staged)
            leads[key] = {
                "stored_at": current.get("stored_at") or _now().isoformat(),
                "updated_at": _now().isoformat(),
                "status": self.READY_UNUSED,
                "lead": staged,
            }
        self.save()

    def available(self, limit: int | None = None) -> List[Dict]:
        self._prune()
        records = self.payload.setdefault("leads", {})
        sent_accounts = {
            _account_key(record.get("lead") or {})
            for record in records.values()
            if isinstance(record, dict)
            and record.get("status") == self.SENT_TO_AIRTABLE
            and isinstance(record.get("lead"), dict)
        }
        output: List[Dict] = []
        for record in records.values():
            if record.get("status") != self.READY_UNUSED:
                continue
            if isinstance(record.get("lead"), dict):
                lead = dict(record["lead"])
                account = _account_key(lead)
                if account and account in sent_accounts:
                    continue
                output.append(lead)
        output.sort(
            key=lambda lead: (
                -float(lead.get("priority_score") or 0),
                str(lead.get("_validation_timestamp") or ""),
            )
        )
        unique_accounts: List[Dict] = []
        seen_accounts: set[str] = set()
        for lead in output:
            account = _account_key(lead)
            if account and account in seen_accounts:
                continue
            if account:
                seen_accounts.add(account)
            unique_accounts.append(lead)
            if limit and limit > 0 and len(unique_accounts) >= limit:
                break
        return unique_accounts

    def reserve(self, leads_to_reserve: Iterable[Dict]) -> None:
        selected = {self._inventory_key(lead) for lead in leads_to_reserve}
        for key, record in self.payload.setdefault("leads", {}).items():
            if key in selected and record.get("status") == self.READY_UNUSED:
                record["status"] = self.RESERVED_FOR_PUSH
                record["updated_at"] = _now().isoformat()
        self.save()

    def mark_persisted(self, lead_keys: Iterable[str]) -> None:
        wanted = {str(value or "").strip() for value in lead_keys if value}
        for record in self.payload.setdefault("leads", {}).values():
            lead = record.get("lead") or {}
            if str(lead.get("lead_key") or "").strip() in wanted:
                record["status"] = self.SENT_TO_AIRTABLE
                record["updated_at"] = _now().isoformat()
        self.save()

    def release_failed(self, lead_keys: Iterable[str]) -> None:
        wanted = {str(value or "").strip() for value in lead_keys if value}
        for record in self.payload.setdefault("leads", {}).values():
            lead = record.get("lead") or {}
            if (
                str(lead.get("lead_key") or "").strip() in wanted
                and record.get("status") == self.RESERVED_FOR_PUSH
            ):
                record["status"] = self.READY_UNUSED
                record["updated_at"] = _now().isoformat()
        self.save()

    def valid_leads(self) -> List[Dict]:
        """Backward-compatible alias for callers/tests using the old API."""
        return self.available()

    def remove(self, leads_to_remove: Iterable[Dict]) -> None:
        leads = self.payload.setdefault("leads", {})
        for lead in leads_to_remove:
            leads.pop(self._inventory_key(lead), None)
        self.save()

    def save(self) -> None:
        self.payload["updated_at"] = _now().isoformat()
        _atomic_write(self.path, self.payload)
