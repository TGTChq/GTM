"""North-star yield ledger: one durable, append-safe row per PAID Fantastic job.

Purpose: finally compute **net-new send-safe / Fantastic job credit** by source,
title family, role bucket, industry, headcount band, job age and acquisition mode.

Guarantees:
* Analytics only -- a ledger failure NEVER affects pipeline execution (every write
  is wrapped; errors are counted, not raised).
* Idempotent -- keyed by ``(run_id, provider_job_id)``; re-running a run rewrites
  the same logical rows (a JSONL append plus a per-run dedupe index), never
  double-counts credits.
* No secrets / PII -- emails, names and keys are never stored; only outcomes.
* One row per PAID job -- rows the provider returned (and billed) but that never
  became a Lead (qualification reject, dedupe) are still recorded, with the stage
  at which they exited. Collapsed postings (N postings -> 1 lead) are attributed
  so that send-safe credit lands on exactly ONE posting (the lead's primary) and
  the others carry ``collapsed_into`` -- per-credit yields are never inflated.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

LEDGER_SCHEMA = "yield-ledger/1"


def _headcount_band(n: Any) -> str:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return "unknown"
    if v < 11:
        return "1-10"
    if v < 51:
        return "11-50"
    if v < 201:
        return "51-200"
    if v < 1001:
        return "201-1000"
    if v < 5001:
        return "1001-5000"
    return "5000+"


def _age_days(posted_iso: str, now: datetime) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(str(posted_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((now - dt).total_seconds() // 86400))
    except (ValueError, TypeError):
        return None


@dataclass
class LedgerRow:
    run_id: str
    provider_job_id: str
    source: str = ""                 # _acquisition_source label
    provider_dataset: str = ""       # jb | ats
    acquisition_mode: str = ""       # head | deep | watermark | ats | plain
    title_family: str = ""
    role_bucket: str = ""
    company_domain: str = ""         # normalized employer domain (not PII)
    industry: str = ""
    headcount_band: str = "unknown"
    date_posted: str = ""
    date_created: str = ""
    job_age_days: Optional[int] = None
    fantastic_credits: int = 1       # 1 per returned row
    # outcomes (filled as the pipeline progresses; defaults = "not reached")
    exit_stage: str = "acquired"     # acquired|dedup_previously_seen|dedup_in_run|qual_reject|collapsed|enriched
    previously_seen: bool = False
    collapsed_into: str = ""         # primary posting id when this posting folded into a lead
    icp_outcome: str = ""            # pass|reject|unresolved|""
    hm_outcome: str = ""             # found|not_found|""
    zero_apollo_people: bool = False
    email_outcome: str = ""          # verified|unverified|none|""
    send_safe: bool = False
    airtable_created: bool = False
    net_new_send_safe: bool = False
    apollo_calls: int = 0
    apollo_credits: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.run_id}|{self.provider_job_id}"


class YieldLedger:
    """JSONL writer with a per-run in-memory index for idempotent updates.

    Usage pattern (all calls are safe to skip/fail):
        ledger = YieldLedger(path, run_id)
        ledger.record_acquired(jobs, mode=...)        # one row per paid job
        ledger.mark(job_id, exit_stage=..., ...)       # progressive outcome updates
        ledger.flush()                                  # append rows (idempotent per key)
    """

    def __init__(self, path: str, run_id: str, *, enabled: bool = True,
                 now: Optional[datetime] = None) -> None:
        self.path = str(path or "")
        self.run_id = str(run_id)
        self.enabled = bool(enabled and self.path)
        self.now = now or datetime.now(timezone.utc)
        self.rows: Dict[str, LedgerRow] = {}
        self.errors = 0
        self.written = 0

    # -- building -----------------------------------------------------------
    def record_acquired(self, jobs: Iterable[Dict[str, Any]], *, mode: str = "") -> int:
        if not self.enabled:
            return 0
        n = 0
        try:
            for j in jobs:
                pid = str(j.get("_fantastic_internal_id") or j.get("job_id") or "").strip()
                if not pid:
                    continue
                row = LedgerRow(
                    run_id=self.run_id, provider_job_id=pid,
                    source=str(j.get("_acquisition_source") or ""),
                    provider_dataset=str(j.get("_provider_dataset") or ""),
                    acquisition_mode=str(j.get("_acquisition_mode") or mode or ""),
                    title_family=str(j.get("_title_family") or ""),
                    role_bucket=str(j.get("_role_bucket") or ""),
                    company_domain=str(j.get("employer_website") or "").lower(),
                    industry=str(j.get("_org_industry") or ""),
                    headcount_band=_headcount_band(j.get("_org_headcount")),
                    date_posted=str(j.get("_fantastic_date_posted") or j.get("job_posted_at_datetime_utc") or ""),
                    date_created=str(j.get("_fantastic_date_created") or ""),
                    job_age_days=_age_days(str(j.get("job_posted_at_datetime_utc") or ""), self.now),
                )
                self.rows[row.key()] = row
                n += 1
        except Exception as exc:  # noqa: BLE001 - analytics never raises
            self.errors += 1
            logger.warning("yield ledger record_acquired failed: %s", type(exc).__name__)
        return n

    def mark(self, job_id: str, **fields: Any) -> None:
        """Update outcome fields for a posting by job_id (accepts either the
        canonical ``fantastic_<id>`` or the raw provider id)."""
        if not self.enabled:
            return
        try:
            pid = str(job_id or "")
            if pid.startswith("fantastic_"):
                pid = pid[len("fantastic_"):]
            row = self.rows.get(f"{self.run_id}|{pid}")
            if row is None:
                return
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)
                else:
                    row.meta[k] = v
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            logger.warning("yield ledger mark failed: %s", type(exc).__name__)

    def mark_collapsed(self, primary_job_id: str, related_job_ids: Iterable[str]) -> None:
        """Attribute N->1 collapse: related postings point at the primary and carry
        no send-safe credit of their own."""
        for rid in related_job_ids or []:
            if str(rid) != str(primary_job_id):
                self.mark(rid, exit_stage="collapsed", collapsed_into=str(primary_job_id))

    # -- persistence --------------------------------------------------------
    def flush(self) -> int:
        """Append all rows for this run; rewrites prior rows of the SAME run_id by
        filtering them out first (idempotent), bounded and best-effort."""
        if not self.enabled or not self.rows:
            return 0
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            existing: List[str] = []
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("run_id") != self.run_id:
                            existing.append(line)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for line in existing:
                    fh.write(line + "\n")
                for row in self.rows.values():
                    rec = asdict(row)
                    rec["schema"] = LEDGER_SCHEMA
                    rec["written_at"] = datetime.now(timezone.utc).isoformat()
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            os.replace(tmp, self.path)
            self.written = len(self.rows)
            return self.written
        except Exception as exc:  # noqa: BLE001 - analytics never raises
            self.errors += 1
            logger.warning("yield ledger flush failed: %s", type(exc).__name__)
            return 0

    def summary(self) -> Dict[str, Any]:
        rows = list(self.rows.values())
        return {
            "enabled": self.enabled, "rows": len(rows), "written": self.written,
            "errors": self.errors,
            "credits": sum(r.fantastic_credits for r in rows),
            "send_safe": sum(1 for r in rows if r.send_safe),
            "net_new_send_safe": sum(1 for r in rows if r.net_new_send_safe),
        }


def aggregate_yield(path: str, *, by: str = "title_family") -> Dict[str, Dict[str, Any]]:
    """Offline analysis helper: net-new send-safe per credit grouped by a dimension
    over the whole ledger file. Never used on the hot path."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                k = str(rec.get(by) or "unknown")
                g = out.setdefault(k, {"credits": 0, "send_safe": 0, "net_new_send_safe": 0})
                g["credits"] += int(rec.get("fantastic_credits", 1) or 0)
                g["send_safe"] += 1 if rec.get("send_safe") else 0
                g["net_new_send_safe"] += 1 if rec.get("net_new_send_safe") else 0
    except OSError:
        return out
    for g in out.values():
        g["yield"] = (g["net_new_send_safe"] / g["credits"]) if g["credits"] else 0.0
    return out
