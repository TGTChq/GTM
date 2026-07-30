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
import freshness_policy
from company_identity import canonical_company_key
from job_filter import job_reference_key, normalize_text
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
    return job_reference_key(job)




def _account_key(lead: Dict) -> str:
    return canonical_company_key(
        domain=(
            lead.get("canonical_domain")
            or lead.get("company_domain")
            or lead.get("employer_website")
            or lead.get("website")
            or ""
        ),
        normalized_name=normalize_text(
            lead.get("canonical_employer_name")
            or lead.get("employer_name")
            or lead.get("company_name")
            or ""
        ),
        blocked_domains=config.INTERMEDIARY_JOB_DOMAINS,
    )


def _account_bucket_key(lead: Dict) -> str:
    """Same-account, same-function suppression key.

    A repeat of the *same* function at an already-contacted account is still
    suppressed (correct -- avoids duplicate/unjustified outbound). A *different*
    function/bucket at that same account is a genuinely distinct hiring signal
    per the spec's own explicit allowance and must not be silently dropped
    (ROOT_CAUSE_TABLE_STRUCTURAL.md row 4 / TECHNICAL_DESIGN.md D3 -- this
    replaces the previous account-only key that suppressed every future lead
    at an account regardless of function, with zero counter or reason code).
    """
    account = _account_key(lead)
    if not account:
        return ""
    bucket = normalize_text(str(lead.get("bucket") or ""))
    return f"{account}|{bucket}" if bucket else account


_FINAL_STATE_RANK = {"FINAL_PASS": 0, "NEEDS_CHECK": 1, "UNVERIFIED": 2, "REROUTE": 3}


def _final_state_rank(lead: Dict) -> int:
    """Lower is higher-priority. Unknown states sort after every known one."""
    return _FINAL_STATE_RANK.get(str(lead.get("_final_state") or "").upper(), 99)


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
                    # Use the tier the lead actually qualified under (0-14,
                    # 15-30, 31-60, 61-90) rather than the blanket primary
                    # ceiling -- a lead admitted through age/extended recovery
                    # has job_age_days >= 15 by construction, so checking it
                    # against the 14-day primary ceiling would always be true
                    # and prune every recovery-sourced lead on the very next
                    # call, before it could ever be reserved or delivered.
                    tier = str(lead.get("_freshness_tier") or "") or freshness_policy.classify_age_tier(
                        int(age_at_validation)
                    )
                    ceiling = freshness_policy.tier_max_age_days(tier)
                    # Strictly greater-than, matching job_filter.is_stale_job's
                    # admission boundary (age_days > effective_max). A job at
                    # exactly its tier ceiling remains eligible for that day and
                    # expires only after exceeding it -- so admission and pruning
                    # agree at 14 / 30 / 60 / 90, and a lead admitted at exactly
                    # its ceiling is never pruned in the same run (Phase 13 §3).
                    job_too_old = (float(age_at_validation) + elapsed_days) > ceiling
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

    def stage(self, leads_to_stage: Iterable[Dict]) -> Dict:
        leads = self.payload.setdefault("leads", {})
        sent_account_buckets = {
            _account_bucket_key(record.get("lead") or {})
            for record in leads.values()
            if isinstance(record, dict)
            and record.get("status") == self.SENT_TO_AIRTABLE
            and isinstance(record.get("lead"), dict)
        }
        staged_count = 0
        already_sent_suppressed = 0
        already_sent_suppressed_lead_keys: List[str] = []
        for lead in leads_to_stage:
            state = str(lead.get("_final_state") or "")
            if state == "FINAL_PASS":
                pass
            elif not is_airtable_reviewable(lead):
                continue
            account_bucket = _account_bucket_key(lead)
            if account_bucket and account_bucket in sent_account_buckets:
                already_sent_suppressed += 1
                already_sent_suppressed_lead_keys.append(str(lead.get("lead_key") or ""))
                continue
            key = self._inventory_key(lead)
            current = leads.get(key) or {}
            if current.get("status") == self.SENT_TO_AIRTABLE:
                already_sent_suppressed += 1
                already_sent_suppressed_lead_keys.append(str(lead.get("lead_key") or ""))
                continue
            staged = dict(lead)
            staged["priority_score"] = _priority_score(staged)
            leads[key] = {
                "stored_at": current.get("stored_at") or _now().isoformat(),
                "updated_at": _now().isoformat(),
                "status": self.READY_UNUSED,
                "lead": staged,
            }
            staged_count += 1
        self.save()
        return {
            "staged": staged_count,
            "already_sent_same_bucket_suppressed": already_sent_suppressed,
            "already_sent_suppressed_lead_keys": already_sent_suppressed_lead_keys,
        }

    def available(self, limit: int | None = None) -> List[Dict]:
        self._prune()
        records = self.payload.setdefault("leads", {})
        sent_account_buckets = {
            _account_bucket_key(record.get("lead") or {})
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
                account_bucket = _account_bucket_key(lead)
                if account_bucket and account_bucket in sent_account_buckets:
                    continue
                output.append(lead)
        output.sort(
            key=lambda lead: (
                # _final_state is the primary sort key so a delivery limit can
                # never bump a FINAL_PASS lead in favor of a NEEDS_CHECK one
                # (TECHNICAL_DESIGN.md D11) -- signal-confidence scoring is
                # only a tiebreaker within the same state tier.
                _final_state_rank(lead),
                -float(lead.get("priority_score") or 0),
                str(lead.get("_validation_timestamp") or ""),
            )
        )
        # Same-run collapse: one lead per (account, function/bucket) pair, not
        # one per account -- a distinct function at the same account is a
        # genuinely distinct hiring signal, not a duplicate (spec's explicit
        # allowance; ROOT_CAUSE_TABLE_STRUCTURAL.md row 4).
        unique_accounts: List[Dict] = []
        seen_account_buckets: set[str] = set()
        for lead in output:
            account_bucket = _account_bucket_key(lead)
            if account_bucket and account_bucket in seen_account_buckets:
                continue
            if account_bucket:
                seen_account_buckets.add(account_bucket)
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
