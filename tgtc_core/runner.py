"""The new runner. It never imports the legacy orchestrator.

One ``cycle`` = lifecycle pass (expire postings, reopen work whose dependency came
back), acquisition over EVERY configured Fantastic source (fresh first, then a backfill
share) unless a persisted Apollo refusal withholds paid inventory (R08), then the
stages with fair-share claims, then the delivery outbox. Each stage is also runnable on
its own (``work``/``deliver``) so the stages can be separate processes sharing
PostgreSQL.
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
from .services import provider_state
from .services.acquisition import AcquisitionService
from .services.classification_service import classify_one, reopen_for_inference
from .services.delivery import DeliveryService
from .services.identity_service import resolve_posting_identity
from .services.lifecycle import expire_postings
from .services.metrics import ledger
from .services.opportunity import OpportunityService
from .services.scheduler import FairShare

log = logging.getLogger("tgtc_core.runner")

STAGES = ("resolve_identity", "classify", "qualify_opportunity")


@dataclass
class CycleReport:
    run_id: str
    lifecycle: Dict[str, Any] = field(default_factory=dict)
    acquisition: List[Dict[str, Any]] = field(default_factory=list)
    acquisition_withheld: Dict[str, Any] = field(default_factory=dict)
    stages: Dict[str, Dict[str, int]] = field(default_factory=dict)
    delivery: Dict[str, Dict[str, int]] = field(default_factory=dict)
    scheduler: Dict[str, Any] = field(default_factory=dict)
    ledger: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "lifecycle": self.lifecycle, "acquisition": self.acquisition,
                "acquisition_withheld": self.acquisition_withheld, "stages": self.stages, "delivery": self.delivery,
                "scheduler": self.scheduler, "ledger": self.ledger}


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
        self.inference_configured = bool(inference is not None and settings.inference_enabled and getattr(base_port, "model_version", ""))
        self.scheduler = FairShare(fresh_share_pct=settings.fresh_share_pct)

    # --- logging ----------------------------------------------------------
    def _log(self, stage: str, event: str, details: Optional[Dict[str, Any]] = None) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO run_log (run_id, stage, event, details) VALUES (%s, %s, %s, %s)",
                            (self.run_id, stage, event, jsonb(details or {})))

    # --- lifecycle --------------------------------------------------------
    def lifecycle(self) -> Dict[str, Any]:
        out: Dict[str, Any] = dict(expire_postings(self.conn, now=self.now()))
        if self.inference_configured:
            out["reopened_for_inference"] = reopen_for_inference(self.conn, model_version=self.inference.model_version, now=self.now())
        self._log("lifecycle", "pass", out)
        return out

    # --- acquisition ------------------------------------------------------
    def _acquisition_service(self) -> AcquisitionService:
        return AcquisitionService(
            self.conn, self.fantastic, page_limit=self.s.fantastic_page_limit, time_frame=self.s.fantastic_time_frame,
            fresh_window_minutes=self.s.fantastic_fresh_window_minutes, fresh_lag_minutes=self.s.fantastic_fresh_lag_minutes,
            backfill_window_hours=self.s.fantastic_backfill_window_hours, max_pages_per_partition=self.s.fantastic_max_pages_per_partition,
            min_jobs_quota_remaining=self.s.fantastic_min_jobs_quota_remaining,
            min_requests_quota_remaining=self.s.fantastic_min_requests_quota_remaining, sources=self.s.fantastic_sources,
            lease_seconds=self.s.partition_lease_seconds, quota_max_age_hours=self.s.fantastic_quota_max_age_hours,
            provider_retry_hours=self.s.fantastic_availability_retry_hours, now=self.now,
        )

    def acquisition_gate(self) -> Dict[str, Any]:
        """R08: paid inventory depends on Apollo enrichment. While Apollo is on record as
        refusing or unauthorized, no inventory is bought; available data keeps being
        processed. Only a served chargeable Apollo call (made by qualification) lifts this."""
        gate = provider_state.acquisition_allowed(self.conn, "apollo")
        self.conn.commit()
        return gate

    def acquire(self, *, fresh_partitions: int = 24, backfill_partitions: int = 1, sources: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if self.fantastic is None:
            self._log("acquisition", "skipped", {"reason": "no_fantastic_transport"})
            return []
        gate = self.acquisition_gate()
        if not gate["allowed"]:
            self._log("acquisition", "withheld", {"reason": f"apollo_{gate['state']}", "since": str(gate.get("refusing_since"))})
            return []
        svc = self._acquisition_service()
        reports: List[Dict[str, Any]] = []
        for source in (sources or list(svc.sources)):
            recovered = svc.recover_partitions(source)
            if recovered.get("reopened"):
                self._log("acquisition", "recovered_partitions", {"source": source, **recovered})
            svc.plan_fresh_partitions(source, max_new=fresh_partitions)
            stop_source = False
            for pid in svc.open_partitions("fresh", source, limit=fresh_partitions):
                run = svc.run_partition(pid)
                reports.append({"source": source, "lane": "fresh", **run.__dict__})
                self._log("acquisition", "partition", {"source": source, "lane": "fresh", "partition_id": pid, "stop": run.stop_reason,
                                                       "pages": run.pages, "rows": run.rows, "new": run.new_postings})
                if run.stop_reason in ("auth_refused", "quota_refused"):
                    stop_source = True
                    break
            if stop_source:
                continue
            for _ in range(backfill_partitions):
                svc.plan_backfill_partition(source)
            for pid in svc.open_partitions("backfill", source, limit=backfill_partitions):
                run = svc.run_partition(pid)
                reports.append({"source": source, "lane": "backfill", **run.__dict__})
                self._log("acquisition", "partition", {"source": source, "lane": "backfill", "partition_id": pid, "stop": run.stop_reason,
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
                    out = classify_one(self.conn, item.subject_id, inference=self.inference, now=self.now(),
                                       transient_backoff_minutes=self.s.inference_retry_minutes)
                    result = out.outcome
                    if result == "wait":
                        with transaction(self.conn):
                            work_queue.wait(self.conn, item, out.reason, out.retry_after or (self.now() + timedelta(minutes=self.s.inference_retry_minutes)))
                        counts["wait"] = counts.get("wait", 0) + 1
                        continue
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
        report.lifecycle = self.lifecycle()
        if acquire:
            gate = self.acquisition_gate()
            if not gate["allowed"]:
                report.acquisition_withheld = {"reason": f"apollo_{gate['state']}", "since": str(gate.get("refusing_since"))}
            report.acquisition = self.acquire()
        for stage in STAGES:
            report.stages[stage] = self.work(stage, max_items=max_items)
        report.delivery = self.deliver(max_items=max_items)
        report.scheduler = self.scheduler.to_dict()
        report.ledger = ledger(self.conn)
        self.conn.commit()
        self._log("cycle", "end", {"stages": report.stages, "delivery": report.delivery})
        return report
