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
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg

from .config import Settings
from .db import work_queue
from .db.connection import jsonb, transaction
from .domain.inference import BudgetedInference, CachedInference, InferencePort, NullAdapter
from .domain.acquisition_query import EXHAUSTIVE_PROFILE, PRIORITY_PROFILE, DISCOVERY_PROFILE, balanced_slot, balanced_slots
from .domain.exhaustive_routing import exhaustive_enabled

#: `run-target` counts AIRTABLE leads, which is a reason to measure Airtable,
#: never a reason to leave the Instantly outbox undrained. Measured in the
#: production canary: 252 Instantly rows sat at attempts = 0, never claimed,
#: while Airtable delivered in the same run, because this was hardcoded to
#: ("airtable",). That is an independent cause of production sending zero.
# Instantly first: an Airtable record follows a GENUINE Instantly creation (2026-09-22),
# so draining Instantly first completes both channels in one pass.
TARGET_DELIVERY_CHANNELS: tuple[str, ...] = ("instantly", "airtable")

#: Run outcome contract. A completed run below its business target exits 0 and
#: says so in the ledger; only a broken system exits non-zero.
EXIT_OK = 0
EXIT_TECHNICAL_FAILURE = 2
RESULT_TARGET_REACHED = "target_reached"
RESULT_TARGET_NOT_REACHED = "target_not_reached"
RESULT_TECHNICAL_FAILURE = "technical_failure"
_SYSTEMIC_ACQUISITION_PREFIXES = ("request_error:", "provider_")
_DELIVERY_SUCCESS_OUTCOMES = ("delivered", "reconciled")
_DELIVERY_FAILURE_OUTCOMES = ("failed", "uncertain")
from .providers.airtable import AirtableClient
from .providers.apollo import ApolloClient
from .providers.fantastic import FantasticClient
from .providers.http import Transport
from .providers.instantly import InstantlyClient
from .services import provider_state
from .services.acquisition import AcquisitionService, SOURCE_SPECS
from .services.classification_service import classify_one, reopen_for_inference
from .services.compliance_recheck import recheck_unknown_jurisdiction
from .services.mail_domain_recheck import release_mail_domain_recoverable
from .services.delivery import DeliveryService
from .services.identity_service import resolve_posting_identity
from .services.lifecycle import expire_postings
from .services.metrics import ledger
from .services.opportunity import OpportunityService, reopen_recoverable_opportunities
from .services.scheduler import FairShare
from .services.spend_budget import BudgetExceeded, SpendBudget, release_budget_waits

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


@dataclass
class TargetRunReport:
    """Auditable result for one target-seeking production run.

    ``target_met`` is intentionally based on terminal Airtable create/reconcile
    receipts attributed to this run.  Database approvals alone never satisfy the
    external acceptance target.
    """

    run_id: str
    target: int
    target_met: bool = False
    stop_reason: str = ""
    rounds_completed: int = 0
    approvals_created: int = 0
    airtable_created: int = 0
    rounds: List[Dict[str, Any]] = field(default_factory=list)

    # --- outcome classification -------------------------------------------
    # A completed run below target is a SUCCESSFUL process: Railway marks any
    # non-zero exit CRASHED, and the 2026-09-18 run that finished normally at
    # 0/1000 (spend_budget_exhausted) read as a code defect for two days.
    # Only a genuinely broken system exits non-zero.

    def _classify(self) -> Tuple[List[str], List[str]]:
        failures: List[str] = []
        warnings: List[str] = []
        acquired = sum(int(r.get("acquisition_new_postings") or 0) for r in self.rounds)
        delivery_totals: Dict[str, Dict[str, int]] = {}
        for rnd in self.rounds:
            for stop in rnd.get("acquisition_stops") or ():
                stop = str(stop or "")
                if stop == "auth_refused":
                    # Refused credentials never heal on retry.
                    failures.append("provider:auth_refused")
                elif stop.startswith(_SYSTEMIC_ACQUISITION_PREFIXES) or stop == "timeout_uncertain":
                    (failures if acquired == 0 else warnings).append(f"provider:{stop}")
            for channel, counts in (rnd.get("delivery") or {}).items():
                bucket = delivery_totals.setdefault(channel, {})
                for outcome, n in (counts or {}).items():
                    bucket[outcome] = bucket.get(outcome, 0) + int(n or 0)
        for channel, counts in sorted(delivery_totals.items()):
            succeeded = sum(counts.get(k, 0) for k in _DELIVERY_SUCCESS_OUTCOMES)
            broken = sum(counts.get(k, 0) for k in _DELIVERY_FAILURE_OUTCOMES)
            if broken and not succeeded:
                # Every write this channel attempted failed: delivery is broken.
                failures.append(f"delivery:{channel}:no_successful_write:{broken}")
            elif broken:
                # Working channel with retryable failures; the rows retry.
                warnings.append(f"delivery:{channel}:retryable:{broken}")
        return failures, warnings

    @property
    def technical_failures(self) -> List[str]:
        return self._classify()[0]

    @property
    def result(self) -> str:
        if self.technical_failures:
            return RESULT_TECHNICAL_FAILURE
        return RESULT_TARGET_REACHED if self.target_met else RESULT_TARGET_NOT_REACHED

    def to_dict(self) -> Dict[str, Any]:
        failures, warnings = self._classify()
        return {
            "run_id": self.run_id,
            "target": self.target,
            "target_met": self.target_met,
            "result": self.result,
            "technical_failures": failures,
            "technical_warnings": warnings,
            "stop_reason": self.stop_reason,
            "rounds_completed": self.rounds_completed,
            "approvals_created": self.approvals_created,
            "airtable_created": self.airtable_created,
            "rounds": self.rounds,
        }


def run_exit_code(report: "TargetRunReport") -> int:
    """Process exit code for a finished target run. Business shortfall is 0."""
    return EXIT_TECHNICAL_FAILURE if report.technical_failures else EXIT_OK


class Runner:
    def __init__(self, conn: psycopg.Connection, settings: Settings, *, fantastic_transport: Optional[Transport],
                 apollo_transport: Optional[Transport], airtable_transport: Optional[Transport],
                 instantly_transport: Optional[Transport], inference: Optional[InferencePort] = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), run_id: str = ""):
        self.conn = conn
        self.s = settings
        self.now = now
        self.run_id = run_id or f"{self.now().strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:8]}"
        self.spend_budget = SpendBudget(conn, settings.spend_budget_id, now=now) if settings.spend_budget_id else None
        self.fantastic = FantasticClient(fantastic_transport, base_url=settings.fantastic_base_url,
                                         api_key=settings.fantastic_api_key,
                                         max_retries=0 if self.spend_budget else 2) if fantastic_transport else None
        self.apollo = ApolloClient(apollo_transport, base_url=settings.apollo_base_url, api_key=settings.apollo_api_key) if apollo_transport else None
        self.airtable = AirtableClient(airtable_transport, base_url=settings.airtable_base_url, token=settings.airtable_token,
                                       base_id=settings.airtable_base_id or "app_unset", table=settings.airtable_table_name) if airtable_transport else None
        self.instantly = InstantlyClient(instantly_transport, base_url=settings.instantly_base_url,
                                         api_key=settings.instantly_api_key) if instantly_transport else None
        base_port = inference or NullAdapter()
        if self.spend_budget and getattr(base_port, "model_version", ""):
            base_port = BudgetedInference(conn, base_port, self.spend_budget)
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
        if self.spend_budget is not None:
            # Work deferred on an EARLIER budget's exhaustion resumes as soon as
            # the active budget has headroom for that provider.
            out["released_budget_waits"] = release_budget_waits(self.conn, self.spend_budget.budget_id, now=self.now())
        # Units whose stored, already-paid people now pass the mail-domain rule
        # are re-judged now rather than after a 24-hour buyer-search wait.
        out["mail_domain_released"] = release_mail_domain_recoverable(self.conn, now=self.now(), env=os.environ)
        if exhaustive_enabled(os.environ):
            # Re-decide unknown contact jurisdictions from the person's own
            # stored Apollo evidence, before delivery drains. Zero paid calls.
            out["compliance_recheck"] = recheck_unknown_jurisdiction(self.conn, now=self.now())
        out["reopened_recoverable_opportunities"] = reopen_recoverable_opportunities(
            self.conn, campaign_env=self.s.campaign_env, signing_key=self.s.signing_key,
            now=self.now(), max_contacts_per_opportunity=self.s.max_contacts_per_opportunity,
        )
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
            spend_budget=self.spend_budget,
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
        if self.s.acquisition_strategy == "balanced_v1":
            return self._acquire_balanced(svc, fresh_partitions=fresh_partitions,
                                          backfill_partitions=backfill_partitions, sources=sources)
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

    def acquire_block(self, pages: int) -> List[Dict[str, Any]]:
        """Buy ONE small block (``pages`` pages) for the daily controller: the same
        balanced path, gates and fail-closed stops, continuing the 4:1 slot pattern."""
        if self.fantastic is None:
            self._log("acquisition", "skipped", {"reason": "no_fantastic_transport"})
            return []
        gate = self.acquisition_gate()
        if not gate["allowed"]:
            self._log("acquisition", "withheld", {"reason": f"apollo_{gate['state']}"})
            return []
        svc = self._acquisition_service()
        selected = tuple(svc.sources)
        start = getattr(self, "_slot_index", 0)
        slots = [balanced_slot(selected, start + i) for i in range(max(1, int(pages)))]
        self._slot_index = start + len(slots)
        return self._acquire_balanced(svc, fresh_partitions=24, backfill_partitions=1, sources=None, slots=slots)

    def _acquire_balanced(self, svc: AcquisitionService, *, fresh_partitions: int,
                          backfill_partitions: int, sources: Optional[List[str]],
                          slots: Optional[List[tuple]] = None) -> List[Dict[str, Any]]:
        """Priority + independent broad recovery, without a title gate.

        One page per slot; all paths use the SAME persistent spend ceiling. Broad
        recovery can rebuy priority rows: report unchanged rows, don't count those
        as new leads. Its older cursors resume before newer recovery windows. Old
        legacy partitions are deliberately NOT silently repurposed or marked done.
        """
        if self.spend_budget is None:
            raise RuntimeError("balanced_acquisition_requires_persistent_spend_budget")
        selected = tuple(sources or svc.sources)
        if any(source not in SOURCE_SPECS for source in selected):
            raise ValueError("unknown_fantastic_source")
        if slots is None:
            slots = list(balanced_slots(selected, self.s.fantastic_cycle_page_slots, env=os.environ))
        # A slot whose profile has no open partition is skipped outright, so the
        # profiles planned here must match the ones the slots ask for.
        planned_profiles = (PRIORITY_PROFILE, DISCOVERY_PROFILE)
        svc.sources = selected  # don't exclude ATS duplicates when only JB is scheduled
        for source in selected:
            svc.recover_partitions(source)
            for profile in planned_profiles:
                if fresh_partitions:
                    svc.plan_fresh_partitions(source, max_new=fresh_partitions, profile=profile)
                for _ in range(backfill_partitions):
                    svc.plan_backfill_partition(source, profile=profile)
        reports: List[Dict[str, Any]] = []
        deferred: set[int] = set()
        for source, profile in slots:
            chosen = None
            lanes = [("fresh", fresh_partitions), ("backfill", backfill_partitions)]
            if backfill_partitions and svc.prefer_backfill(source, profile):
                lanes.reverse()
            for lane, count in lanes:
                if not count:
                    continue
                ids = svc.open_partitions(lane, source, limit=max(count, self.s.fantastic_cycle_page_slots),
                                          profile=profile, oldest_first=profile == DISCOVERY_PROFILE)
                chosen = next((pid for pid in ids if pid not in deferred), None)
                if chosen is not None:
                    break
            if chosen is None:
                continue
            run = svc.run_partition(chosen, max_pages=1)
            details = {"source": source, "lane": lane, **run.__dict__}
            reports.append(details)
            self._log("acquisition", "partition", details)
            if run.stop_reason != "page_budget":
                deferred.add(chosen)
            # Fail closed across feeds/profiles instead of cascading paid probes.
            if (run.stop_reason in {"auth_refused", "quota_refused", "timeout_uncertain"}
                    or run.stop_reason.startswith(("spend_budget_exhausted:", "request_error:", "provider_", "quota_reserve:"))):
                break
        return reports

    # --- work items -------------------------------------------------------
    def _opportunity_service(self) -> OpportunityService:
        if self.apollo is None:
            raise RuntimeError("no Apollo transport configured")
        return OpportunityService(self.conn, self.apollo, campaign_env=self.s.campaign_env, signing_key=self.s.signing_key,
                                  retry_hours=self.s.apollo_availability_retry_hours,
                                  people_search_max_pages=self.s.apollo_people_search_max_pages,
                                  people_search_page_size=self.s.apollo_people_search_page_size,
                                  run_id=self.run_id,
                                  max_contacts_per_opportunity=self.s.max_contacts_per_opportunity,
                                  person_uniqueness=self.s.person_employer_uniqueness,
                                  verify_on_import=self.s.instantly_verify_on_import,
                                  spend_budget=self.spend_budget,
                                  outreach_legal_basis=self.s.outreach_legal_basis,
                                  outreach_legal_basis_evidence=self.s.outreach_legal_basis_evidence,
                                  outreach_privacy_notice_configured=self.s.outreach_privacy_notice_configured,
                                  outreach_privacy_notice_days=self.s.outreach_privacy_notice_days,
                                  now=self.now)

    def work(self, kind: str, *, max_items: int = 1000) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        approved_leads = 0
        opp_service = self._opportunity_service() if kind == "qualify_opportunity" and self.apollo else None
        for _ in range(max_items):
            item = self.scheduler.claim(self.conn, kind=kind, lease_seconds=self.s.lease_seconds, now=self.now())
            if item is None:
                break
            try:
                if kind == "resolve_identity":
                    out = resolve_posting_identity(self.conn, item.subject_id, now=self.now(), work_item=item)
                    result = out.outcome
                elif kind == "classify":
                    out = classify_one(self.conn, item.subject_id, inference=self.inference, now=self.now(),
                                       transient_backoff_minutes=self.s.inference_retry_minutes, work_item=item)
                    result = out.outcome
                    if result == "wait":
                        with transaction(self.conn):
                            work_queue.wait(self.conn, item, out.reason, out.retry_after or (self.now() + timedelta(minutes=self.s.inference_retry_minutes)))
                        counts["wait"] = counts.get("wait", 0) + 1
                        if out.reason == "inference_transient:spend_budget_exhausted":
                            counts["budget_exhausted"] = counts.get("budget_exhausted", 0) + 1
                        elif out.reason.startswith(("inference_transient:", "inference_config:", "inference_unavailable:")):
                            counts["technical_failure"] = counts.get("technical_failure", 0) + 1
                        continue
                elif kind == "qualify_opportunity":
                    if opp_service is None:
                        with transaction(self.conn):
                            work_queue.wait(self.conn, item, "apollo_not_configured", self.now() + timedelta(hours=1))
                        counts["wait"] = counts.get("wait", 0) + 1
                        counts["technical_failure"] = counts.get("technical_failure", 0) + 1
                        continue
                    out = opp_service.process(item.subject_id, work_item=item)
                    result = out.outcome
                    if result == "approved":
                        approved_leads += int(out.details.get("approvals_created", 1))
                    if result == "wait":
                        until = datetime.fromisoformat(out.details["until"]) if out.details.get("until") else self.now() + timedelta(hours=1)
                        with transaction(self.conn):
                            work_queue.wait(self.conn, item, out.reason, until)
                        counts["wait"] = counts.get("wait", 0) + 1
                        if not out.reason.startswith("buyer_search_pending:"):
                            counts["technical_failure"] = counts.get("technical_failure", 0) + 1
                        else:
                            counts["deferred_search"] = counts.get("deferred_search", 0) + 1
                        continue
                    if result == "retry":
                        with transaction(self.conn):
                            work_queue.retry(self.conn, item, out.reason, backoff_seconds=self.s.retry_backoff_seconds, now=self.now())
                        counts["retry"] = counts.get("retry", 0) + 1
                        counts["technical_failure"] = counts.get("technical_failure", 0) + 1
                        continue
                else:
                    raise ValueError(f"unknown kind {kind}")
                with transaction(self.conn):
                    if result == "closed":
                        work_queue.close(self.conn, item, getattr(out, "reason", "closed"))
                    else:
                        work_queue.complete(self.conn, item)
                counts[result] = counts.get(result, 0) + 1
                if result == "closed" and getattr(out, "reason", "").startswith("inference_unavailable:"):
                    counts["technical_failure"] = counts.get("technical_failure", 0) + 1
            except work_queue.LeaseLost:
                self.conn.rollback()
                counts["lease_lost"] = counts.get("lease_lost", 0) + 1
            except BudgetExceeded as exc:
                self.conn.rollback()
                with transaction(self.conn):
                    work_queue.wait(self.conn, item, str(exc), exc.retry_after)
                counts["budget_exhausted"] = counts.get("budget_exhausted", 0) + 1
            except Exception as exc:  # noqa: BLE001 - a technical failure retries; it never approves or rejects
                self.conn.rollback()
                # No traceback/message: DB/provider exceptions can echo secrets or PII.
                error_class = type(exc).__name__
                if error_class not in {"ValueError", "TypeError", "KeyError", "LookupError", "RuntimeError",
                                        "OperationalError", "InterfaceError", "IntegrityError", "EvidenceChanged"}:
                    error_class = "UnexpectedError"
                log.error("work item %s failed: %s", item.id, error_class)
                with transaction(self.conn):
                    work_queue.retry(self.conn, item, error_class, backoff_seconds=self.s.retry_backoff_seconds, now=self.now())
                counts["error_retry"] = counts.get("error_retry", 0) + 1
                counts["technical_failure"] = counts.get("technical_failure", 0) + 1
        # Preserve the historical one-result-per-item report shape when every
        # approved opportunity creates exactly one lead.  Surface the distinct
        # lead count only when multi-contact recovery makes it differ.
        if kind == "qualify_opportunity" and approved_leads != counts.get("approved", 0):
            counts["approved_leads"] = approved_leads
        self._log("work", kind, counts)
        return counts

    def deliver(self, *, max_items: int = 500,
                channels: tuple[str, ...] = TARGET_DELIVERY_CHANNELS) -> Dict[str, Dict[str, int]]:
        svc = DeliveryService(self.conn, airtable=self.airtable, instantly=self.instantly, lease_seconds=self.s.lease_seconds,
                              backoff_seconds=self.s.retry_backoff_seconds,
                              max_contacts_per_opportunity=self.s.max_contacts_per_opportunity,
                              campaign_env=self.s.campaign_env, env=os.environ, now=self.now)
        report: Dict[str, Dict[str, int]] = {}
        unknown = set(channels) - {"airtable", "instantly"}
        if unknown:
            raise ValueError(f"unknown delivery channels: {sorted(unknown)}")
        for channel in channels:
            counts: Dict[str, int] = {}
            for outcome in svc.drain(channel, max_items=max_items):
                counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
            report[channel] = counts
        self._log("delivery", "drain", report)
        return report

    def cycle(self, *, acquire: bool = True, deliver: bool = True, max_items: int = 1000,
              delivery_channels: tuple[str, ...] = TARGET_DELIVERY_CHANNELS) -> CycleReport:
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
        if deliver:
            report.delivery = self.deliver(max_items=max_items, channels=delivery_channels)
        else:
            report.delivery = {"airtable": {"withheld": 1}, "instantly": {"withheld": 1}}
        report.scheduler = self.scheduler.to_dict()
        report.ledger = ledger(self.conn)
        self.conn.commit()
        self._log("cycle", "end", {"stages": report.stages, "delivery": report.delivery})
        return report

    def _target_counts(self) -> Dict[str, int]:
        """Current non-revoked approvals and Airtable creations for this run only."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM approvals WHERE run_id = %s AND state <> 'revoked'",
                (self.run_id,),
            )
            approvals = int(cur.fetchone()["n"])
            cur.execute(
                """
                SELECT count(DISTINCT a.id) AS n
                FROM approvals a
                JOIN delivery_outbox o ON o.approval_id = a.id AND o.channel = 'airtable'
                JOIN delivery_receipts r ON r.outbox_id = o.id AND r.channel = 'airtable'
                WHERE a.run_id = %s AND a.state <> 'revoked'
                  AND r.receipt_kind IN ('created', 'reconciled')
                """,
                (self.run_id,),
            )
            airtable = int(cur.fetchone()["n"])
        self.conn.commit()
        return {"approvals_created": approvals, "airtable_created": airtable}

    @staticmethod
    def _acquisition_budget_stopped(report: CycleReport) -> bool:
        return any(str(item.get("stop_reason") or "").startswith("spend_budget_exhausted:")
                   for item in report.acquisition)

    @staticmethod
    def _processing_budget_stopped(report: CycleReport) -> bool:
        return any(int(counts.get("budget_exhausted") or 0) > 0 for counts in report.stages.values())

    @classmethod
    def _budget_stopped(cls, report: CycleReport) -> bool:
        return cls._acquisition_budget_stopped(report) or cls._processing_budget_stopped(report)

    @staticmethod
    def _round_activity(report: CycleReport) -> int:
        acquired = sum(int(item.get("new_postings") or 0) for item in report.acquisition)
        stage_items = sum(sum(int(value or 0) for value in counts.values()) for counts in report.stages.values())
        delivered = sum(
            int(value or 0)
            for counts in report.delivery.values()
            for key, value in counts.items()
            if key not in {"withheld", "deferred"}
        )
        return acquired + stage_items + delivered

    def run_to_target(self, *, target: Optional[int] = None, max_rounds: Optional[int] = None,
                      max_items: int = 10000, acquire: bool = True, deliver: bool = True) -> TargetRunReport:
        """Keep cycling until the run itself creates ``target`` Airtable leads.

        The controller is deliberately bounded by the persistent spend budget,
        ``max_rounds`` and a no-progress detector.  Reaching any boundary is a
        visible non-success; it is never reported as meeting the target.
        Instantly is not drained here: target acceptance is isolated to Airtable.
        """
        wanted = int(self.s.approved_target_per_run if target is None else target)
        rounds_limit = int(self.s.target_max_rounds if max_rounds is None else max_rounds)
        if wanted < 1 or rounds_limit < 1 or max_items < 1:
            raise ValueError("target, max_rounds and max_items must be positive")
        out = TargetRunReport(run_id=self.run_id, target=wanted)
        stalled = 0
        acquisition_spent = False
        self._log("target", "start", {"target": wanted, "max_rounds": rounds_limit,
                                        "max_items": max_items, "deliver": deliver})
        for round_number in range(1, rounds_limit + 1):
            before = self._target_counts()
            report = self.cycle(acquire=acquire, deliver=deliver, max_items=max_items,
                                delivery_channels=TARGET_DELIVERY_CHANNELS)
            after = self._target_counts()
            activity = self._round_activity(report)
            gained = after["airtable_created"] - before["airtable_created"]
            out.rounds.append({
                "round": round_number,
                "approvals_created": after["approvals_created"],
                "airtable_created": after["airtable_created"],
                "new_approvals": after["approvals_created"] - before["approvals_created"],
                "new_airtable": gained,
                "acquisition_new_postings": sum(int(item.get("new_postings") or 0) for item in report.acquisition),
                "acquisition_stops": [str(item.get("stop_reason") or "") for item in report.acquisition],
                "stages": report.stages,
                "delivery": report.delivery,
            })
            out.rounds_completed = round_number
            out.approvals_created = after["approvals_created"]
            out.airtable_created = after["airtable_created"]
            if deliver and out.airtable_created >= wanted:
                out.target_met = True
                out.stop_reason = "target_reached"
                break
            if not deliver and out.approvals_created >= wanted:
                out.stop_reason = "approval_target_reached_without_airtable_delivery"
                break
            if self._processing_budget_stopped(report):
                out.stop_reason = "spend_budget_exhausted"
                break
            if acquire and self._acquisition_budget_stopped(report):
                # The ACQUISITION budget is spent; the inventory it bought is
                # not. Stop buying and keep qualifying and delivering what is
                # already paid for. Measured 2026-09-21: the run stopped here
                # with Apollo at 485 of 1,000 credits and bought work queued.
                acquire = False
                acquisition_spent = True
            stalled = stalled + 1 if activity == 0 else 0
            if stalled >= self.s.target_stall_rounds:
                out.stop_reason = ("acquisition_budget_exhausted_backlog_drained" if acquisition_spent
                                   else "no_progress")
                break
        if not out.stop_reason:
            out.stop_reason = "max_rounds_reached"
        self._log("target", "end", out.to_dict())
        return out
