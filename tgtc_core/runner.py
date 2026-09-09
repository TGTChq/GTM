"""The new runner. It never imports the legacy orchestrator.

One ``cycle`` = plan and run acquisition partitions (fresh first, then a backfill
share), then drain work items stage by stage with fair-share claims, then drain the
delivery outbox. Each stage is also runnable on its own (``work``/``deliver``) so the
stages can be separate processes sharing PostgreSQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import psycopg

from .config import Settings
from .db import work_queue
from .db.connection import jsonb, transaction
from .domain.inference import CachedInference, InferencePort, NullAdapter
from .providers.airtable import AirtableClient
from .providers.apollo import ApolloClient
from .providers.fantastic import FantasticClient
from .providers.http import Transport
from .providers.instantly import InstantlyClient
from .services.acquisition import SOURCE_JOB_BOARDS, AcquisitionService
from .services.classification_service import classify_one
from .services.delivery import DeliveryService
from .services.identity_service import resolve_posting_identity
from .services.metrics import ledger
from .services.opportunity import OpportunityService
from .services.scheduler import FairShare

log = logging.getLogger("tgtc_core.runner")

STAGES = ("resolve_identity", "classify", "qualify_opportunity")


@dataclass
class CycleReport:
    run_id: str
    acquisition: List[Dict[str, Any]] = field(default_factory=list)
    stages: Dict[str, Dict[str, int]] = field(default_factory=dict)
    delivery: Dict[str, Dict[str, int]] = field(default_factory=dict)
    scheduler: Dict[str, Any] = field(default_factory=dict)
    ledger: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "acquisition": self.acquisition, "stages": self.stages,
                "delivery": self.delivery, "scheduler": self.scheduler, "ledger": self.ledger}


class Runner:
    def __init__(self, conn: psycopg.Connection, settings: Settings, *, fantastic_transport: Optional[Transport],
                 apollo_transport: Optional[Transport], airtable_transport: Optional[Transport],
                 instantly_transport: Optional[Transport], inference: Optional[InferencePort] = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), run_id: str = ""):
        self.conn = conn
        self.s = settings
        self.now = now
        self.run_id = run_id or self.now().strftime("%Y%m%dT%H%M%SZ")
        self.fantastic = FantasticClient(fantastic_transport, base_url=settings.fantastic_base_url,
                                         api_key=settings.fantastic_api_key) if fantastic_transport else None
        self.apollo = ApolloClient(apollo_transport, base_url=settings.apollo_base_url, api_key=settings.apollo_api_key) if apollo_transport else None
        self.airtable = AirtableClient(airtable_transport, base_url=settings.airtable_base_url, token=settings.airtable_token,
                                       base_id=settings.airtable_base_id or "app_unset", table=settings.airtable_table_name) if airtable_transport else None
        self.instantly = InstantlyClient(instantly_transport, base_url=settings.instantly_base_url,
                                         api_key=settings.instantly_api_key) if instantly_transport else None
        base_port = inference or NullAdapter()
        self.inference: InferencePort = CachedInference(conn, base_port) if settings.inference_enabled else NullAdapter()
        self.scheduler = FairShare(fresh_share_pct=settings.fresh_share_pct)

    # --- logging ----------------------------------------------------------
    def _log(self, stage: str, event: str, details: Optional[Dict[str, Any]] = None) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO run_log (run_id, stage, event, details) VALUES (%s, %s, %s, %s)",
                            (self.run_id, stage, event, jsonb(details or {})))

    # --- acquisition ------------------------------------------------------
    def acquire(self, *, fresh_partitions: int = 24, backfill_partitions: int = 1, source: str = SOURCE_JOB_BOARDS) -> List[Dict[str, Any]]:
        if self.fantastic is None:
            self._log("acquisition", "skipped", {"reason": "no_fantastic_transport"})
            return []
        svc = AcquisitionService(
            self.conn, self.fantastic, page_limit=self.s.fantastic_page_limit, time_frame=self.s.fantastic_time_frame,
            fresh_window_minutes=self.s.fantastic_fresh_window_minutes, fresh_lag_minutes=self.s.fantastic_fresh_lag_minutes,
            backfill_window_hours=self.s.fantastic_backfill_window_hours, max_pages_per_partition=self.s.fantastic_max_pages_per_partition,
            min_jobs_quota_remaining=self.s.fantastic_min_jobs_quota_remaining,
            min_requests_quota_remaining=self.s.fantastic_min_requests_quota_remaining, now=self.now,
        )
        reports: List[Dict[str, Any]] = []
        svc.plan_fresh_partitions(source, max_new=fresh_partitions)
        for pid in svc.open_partitions("fresh", source, limit=fresh_partitions):
            run = svc.run_partition(pid)
            reports.append({"lane": "fresh", **run.__dict__})
            self._log("acquisition", "partition", {"lane": "fresh", "partition_id": pid, "stop": run.stop_reason,
                                                   "pages": run.pages, "rows": run.rows, "new": run.new_postings})
            if run.stop_reason in ("auth_refused", "quota_refused"):
                return reports
        for _ in range(backfill_partitions):
            svc.plan_backfill_partition(source)
        for pid in svc.open_partitions("backfill", source, limit=backfill_partitions):
            run = svc.run_partition(pid)
            reports.append({"lane": "backfill", **run.__dict__})
            self._log("acquisition", "partition", {"lane": "backfill", "partition_id": pid, "stop": run.stop_reason,
                                                   "pages": run.pages, "rows": run.rows, "new": run.new_postings})
            if run.stop_reason in ("auth_refused", "quota_refused"):
                break
        return reports

    # --- work items -------------------------------------------------------
    def _opportunity_service(self) -> OpportunityService:
        if self.apollo is None:
            raise RuntimeError("no Apollo transport configured")
        return OpportunityService(self.conn, self.apollo, campaign_env=self.s.campaign_env, signing_key=self.s.signing_key,
                                  retry_hours=self.s.apollo_availability_retry_hours,
                                  people_search_max_pages=self.s.apollo_people_search_max_pages,
                                  person_uniqueness=self.s.person_employer_uniqueness,
                                  verify_on_import=self.s.instantly_verify_on_import, now=self.now)

    def work(self, kind: str, *, max_items: int = 1000) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        opp_service = self._opportunity_service() if kind == "qualify_opportunity" and self.apollo else None
        for _ in range(max_items):
            item = self.scheduler.claim(self.conn, kind=kind, lease_seconds=self.s.lease_seconds, now=self.now())
            if item is None:
                break
            try:
                if kind == "resolve_identity":
                    out = resolve_posting_identity(self.conn, item.subject_id, now=self.now())
                    result = out.outcome
                elif kind == "classify":
                    out = classify_one(self.conn, item.subject_id, inference=self.inference, now=self.now())
                    result = out.outcome
                elif kind == "qualify_opportunity":
                    if opp_service is None:
                        with transaction(self.conn):
                            work_queue.wait(self.conn, item, "apollo_not_configured", self.now() + timedelta(hours=1))
                        counts["wait"] = counts.get("wait", 0) + 1
                        continue
                    out = opp_service.process(item.subject_id)
                    result = out.outcome
                    if result == "wait":
                        until = datetime.fromisoformat(out.details["until"]) if out.details.get("until") else self.now() + timedelta(hours=1)
                        with transaction(self.conn):
                            work_queue.wait(self.conn, item, out.reason, until)
                        counts["wait"] = counts.get("wait", 0) + 1
                        continue
                    if result == "retry":
                        with transaction(self.conn):
                            work_queue.retry(self.conn, item, out.reason, backoff_seconds=self.s.retry_backoff_seconds, now=self.now())
                        counts["retry"] = counts.get("retry", 0) + 1
                        continue
                else:
                    raise ValueError(f"unknown kind {kind}")
                with transaction(self.conn):
                    if result == "closed":
                        work_queue.close(self.conn, item, getattr(out, "reason", "closed"))
                    else:
                        work_queue.complete(self.conn, item)
                counts[result] = counts.get(result, 0) + 1
            except Exception as exc:  # noqa: BLE001 - a technical failure retries; it never approves or rejects
                self.conn.rollback()
                log.exception("work item %s failed", item.id)
                with transaction(self.conn):
                    work_queue.retry(self.conn, item, f"{type(exc).__name__}: {exc}", backoff_seconds=self.s.retry_backoff_seconds, now=self.now())
                counts["error_retry"] = counts.get("error_retry", 0) + 1
        self._log("work", kind, counts)
        return counts

    def deliver(self, *, max_items: int = 500) -> Dict[str, Dict[str, int]]:
        svc = DeliveryService(self.conn, airtable=self.airtable, instantly=self.instantly, lease_seconds=self.s.lease_seconds,
                              backoff_seconds=self.s.retry_backoff_seconds, now=self.now)
        report: Dict[str, Dict[str, int]] = {}
        for channel in ("airtable", "instantly"):
            counts: Dict[str, int] = {}
            for outcome in svc.drain(channel, max_items=max_items):
                counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
            report[channel] = counts
        self._log("delivery", "drain", report)
        return report

    def cycle(self, *, acquire: bool = True, max_items: int = 1000) -> CycleReport:
        report = CycleReport(run_id=self.run_id)
        self._log("cycle", "start", {"settings": self.s.describe()})
        if acquire:
            report.acquisition = self.acquire()
        for stage in STAGES:
            report.stages[stage] = self.work(stage, max_items=max_items)
        report.delivery = self.deliver(max_items=max_items)
        report.scheduler = self.scheduler.to_dict()
        report.ledger = ledger(self.conn)
        self.conn.commit()
        self._log("cycle", "end", {"stages": report.stages, "delivery": report.delivery})
        return report
