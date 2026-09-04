"""The replacement orchestrator: one run, every boundary reconciled.

Flow: acquisition (selected lanes, isolated) -> postings deduped to
production-equivalent opportunities -> enrichment -> delivery -> capacity. Every
boundary produces a self-checked ``StageResult``; the run is ``complete`` only if
every stage reconciles and no lane silently lost work.

Resumability: after acquisition the collected postings are checkpointed. A resume
loads that checkpoint instead of re-fetching, so an interrupted run continues
without new provider requests. Recovery is idempotent -- re-running with the same
run_id and the same checkpoint yields the same artifacts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import config

from retrieval_measurement.accounting import posting_identity

from orchestrator.capacity import build_capacity_report
from orchestrator.delivery import DeliveryManager
from orchestrator.enrichment import EnrichmentEngine
from orchestrator.lanes import LaneManager, LaneResult
from orchestrator.reasons import ReasonCode, StageOutcome
from orchestrator.run_ledger import (
    STAGE_ACQUISITION,
    STAGE_DELIVERY,
    STAGE_ENRICHMENT,
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_INCOMPLETE,
    RunLedger,
    prune_ledger,
)
from orchestrator.runcontrol import RunContext, RunStatus
from orchestrator.runlock import RunLock, RunLockHeld
from orchestrator.state import StateManager
from orchestrator.suppression import SuppressionStore
from orchestrator.topup import TopUpController
from orchestrator.waterfall import WaterfallReport, reconcile_stage

#: Bounded retention sized for the ~1.3 GB volume headroom: keep at most this many
#: completed runs AND at most this many bytes under run_artifacts (whichever binds
#: first). ~150 MB/run worst case -> 4 runs <= ~600 MB, leaving >700 MB emergency
#: headroom for one more complete run on the existing gtm-volume.
RETENTION_KEEP_RUNS = 4
RETENTION_MAX_BYTES = 600 * 1024 * 1024

#: Heavy-artifact retention is a STORAGE policy and stays aggressive. Reporting
#: reads the compact ledger instead, which this map closes out on every run.
#: ``RESUMED``/``RUNNING`` reaching the finally means the body returned without a
#: terminal verdict -- reported as incomplete, never as success.
_LEDGER_STATE_FOR_RUN_STATUS = {
    RunStatus.COMPLETE: STATE_COMPLETE,
    RunStatus.FAILED: STATE_FAILED,
    RunStatus.INCOMPLETE: STATE_INCOMPLETE,
    RunStatus.RESUMED: STATE_INCOMPLETE,
    RunStatus.RUNNING: STATE_INCOMPLETE,
}


#: ledger metric key -> enrichment funnel key. A funnel key that is absent stays
#: absent in the ledger: an enrichment pass that never qualified anything must not
#: manufacture a zero for a stage it did not reach.
_FUNNEL_TO_LEDGER = (
    ("jobs_reviewed", "qualification_input"),
    ("qualified_opportunities", "target_role_eligible"),
    ("companies_considered", "companies_considered"),
)


def _accumulate_counts(into: Dict[str, Any], source: Any) -> Dict[str, Any]:
    """Sum one funnel/census mapping into a cumulative one, one level deep.

    The top-up loop enriches once per slice, so the run's funnel is the SUM of its
    slices. Nested mappings (``qual_reason_counts``, ``hm_observability``) are
    merged key-wise; anything non-numeric is carried by last-writer, which is only
    ever descriptive text.
    """
    if not isinstance(source, dict):
        return into
    for key, value in source.items():
        if isinstance(value, bool):
            into[key] = value
        elif isinstance(value, (int, float)):
            into[key] = into.get(key, 0) + value
        elif isinstance(value, dict):
            nested = into.get(key)
            if not isinstance(nested, dict):
                nested = {}
            into[key] = _accumulate_counts(nested, value)
        else:
            into[key] = value
    return into


def _enrichment_ledger_metrics(funnel: Any, leads: Sequence[Any]) -> Dict[str, Any]:
    """Compact reporting counters for the enrichment stage.

    ``contacts_found`` is email PRESENCE on the resolved leads -- the same rule the
    waterfall uses -- never a sum of disposition labels.
    """
    from orchestrator.enrichment import Disposition  # noqa: PLC0415 - avoids a cycle

    counts: Dict[str, Any] = {
        "contacts_found": len([l for l in leads if l.contact.get("email")]),
        "final_pass_leads": len([l for l in leads if l.disposition is Disposition.FINAL_PASS]),
    }
    source = funnel if isinstance(funnel, dict) else {}
    for ledger_key, funnel_key in _FUNNEL_TO_LEDGER:
        if funnel_key in source:
            counts[ledger_key] = source[funnel_key]
    return counts


@dataclass
class OrchestratorPlan:
    """Everything injected into one run. Nothing is imported for side effects."""

    lanes: Sequence[str]
    #: lane name -> callable(LaneManager) -> LaneResult
    lane_runners: Dict[str, Callable[[LaneManager], LaneResult]]
    enrichment_engine: EnrichmentEngine
    delivery_manager: DeliveryManager
    target: int = 300
    runs_per_day: int = 1
    inventory_remaining: int = -1
    quota_consumed: int = 0


class Orchestrator:
    def __init__(self, ctx: RunContext, state: StateManager, budget: Any) -> None:
        self.ctx = ctx
        self.state = state
        self.budget = budget
        #: Compact reporting entry for THIS run. Created in ``run()`` before any
        #: work, updated as stages complete, finalized in the failure-safe
        #: ``finally``. Heavy artifacts are pruned; this is not.
        self.ledger = RunLedger(self.state.root, self.ctx.run_id)

    # -- acquisition -------------------------------------------------------

    def _acquire(self, plan: OrchestratorPlan, *, resume: bool) -> Dict[str, LaneResult]:
        checkpoint = self.state.read_json("checkpoints", f"{self.ctx.run_id}.acquisition.json",
                                          require_schema=False)
        if resume and checkpoint:
            self.ctx.resumed_from = "acquisition_checkpoint"
            self.ctx.status = RunStatus.RESUMED
            results: Dict[str, LaneResult] = {}
            for lane, blob in checkpoint.get("lanes", {}).items():
                results[lane] = LaneResult(
                    lane=lane, status=blob["status"], jobs=blob["jobs"],
                    errors=blob.get("errors", []),
                    physical_requests=blob.get("physical_requests", 0),
                    attribution=blob.get("attribution", {}),
                    accounting=blob.get("accounting", {}),
                )
            return results

        manager = LaneManager(budget=self.budget)
        results = {}
        for lane in plan.lanes:
            runner = plan.lane_runners.get(lane)
            if runner is None:
                continue
            try:
                results[lane] = runner(manager)   # each lane isolated
            except Exception as exc:  # noqa: BLE001 - a lane never erases another
                results[lane] = LaneResult(lane=lane, status="failed",
                                           errors=[f"{type(exc).__name__}: {exc}"])
        # Checkpoint acquisition so a later interruption resumes without refetch.
        self.state.write_json("checkpoints", f"{self.ctx.run_id}.acquisition.json", {
            "lanes": {lane: {
                "status": r.status, "jobs": r.jobs, "errors": r.errors,
                "physical_requests": r.physical_requests,
                "attribution": r.attribution, "accounting": r.accounting,
            } for lane, r in results.items()},
        })
        return results

    # -- postings -> opportunities ----------------------------------------

    def _dedup(self, postings: List[Dict[str, Any]], seen) -> tuple:
        dispo = []
        opportunities: List[Dict[str, Any]] = []
        run_keys: set = set()
        for job in postings:
            strength, key = posting_identity(job)
            if not key or strength == "none":
                dispo.append((StageOutcome.ERRORED, ReasonCode.MISSING_JOB_ID, None))
                continue
            if key in run_keys:
                dispo.append((StageOutcome.REJECTED, ReasonCode.DUPLICATE_IN_RUN, None))
                continue
            if seen is not None and key in seen:
                dispo.append((StageOutcome.REJECTED, ReasonCode.PREVIOUSLY_SEEN, None))
                continue
            run_keys.add(key)
            opp = dict(job)
            opp.setdefault("posting_id", key)
            opportunities.append(opp)
            dispo.append((StageOutcome.PASSED, ReasonCode.OK, None))
        stage = reconcile_stage("acquisition_dedup", "posting", dispo)
        return opportunities, stage

    # -- run ---------------------------------------------------------------

    def run(self, plan: OrchestratorPlan, *, resume: bool = False,
            retention_keep: int = RETENTION_KEEP_RUNS) -> Dict[str, Any]:
        """Acquire the one-run lock, run, and ALWAYS write run_status + release the
        lock + prune retention in a failure-safe finally."""
        lock = RunLock(self.state.root / ".run.lock", self.ctx.run_id)
        lock.acquire()  # raises RunLockHeld if another run is active
        # The reporting entry is created BEFORE any work and only once the lock is
        # ours, so a run that is killed at any later point is still visible to the
        # weekly report as an interrupted run rather than vanishing entirely.
        self.ledger.begin(
            mode=self.ctx.mode.value,
            allow_network=self.ctx.policy.allow_network,
            allow_enrichment=self.ctx.policy.allow_enrichment,
            allow_instantly_enrollment=self.ctx.policy.allow_instantly_enrollment,
            lanes=plan.lanes,
        )
        try:
            return self._run_body(plan, resume=resume, lock=lock)
        except Exception as exc:  # noqa: BLE001 - failure-safe status still written
            self.ctx.finish(RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            try:
                self.state.write_artifact("run_status.json", {
                    "run_id": self.ctx.run_id, "status": self.ctx.status.value,
                    "stop_reason": self.ctx.stop_reason, "run_lock": lock.to_dict()})
            except Exception:  # noqa: BLE001 - never mask the original outcome
                pass
            try:
                self.ledger.finalize(
                    state=_LEDGER_STATE_FOR_RUN_STATUS.get(self.ctx.status, STATE_INCOMPLETE),
                    status=self.ctx.status.value,
                    stop_reason=self.ctx.stop_reason,
                )
            except Exception:  # noqa: BLE001 - reporting must never mask the outcome
                pass
            lock.release()
            try:
                self.state.prune(keep=int(retention_keep), max_bytes=RETENTION_MAX_BYTES,
                                 protect={self.ctx.run_id})
            except Exception:  # noqa: BLE001
                pass
            # Ledger retention is separate and far longer: the compact record must
            # outlive the heavy evidence it summarises (that is the entire point).
            try:
                prune_ledger(self.state.root)
            except Exception:  # noqa: BLE001
                pass

    def _existing_identity_snapshot(self) -> Optional[Dict[str, Any]]:
        """Read Airtable existing-lead identity ONCE for pre-Apollo dedupe.

        Returns ``None`` (feature off, or any read failure) so the caller proceeds
        with no pre-suppression -- delivery's suppression is the authoritative
        backstop, so a snapshot failure never blocks a run or drops a lead."""
        if not config.PRE_APOLLO_EXISTING_DEDUPE:
            return None
        try:
            import airtable_client
            return airtable_client.snapshot_existing_identity()
        except Exception as exc:  # noqa: BLE001 - fail-open on any snapshot error
            print(f"[pre-apollo-dedupe] existing-identity snapshot unavailable; "
                  f"proceeding without pre-suppression (delivery backstop intact): {exc}")
            return None

    def _run_body(self, plan: OrchestratorPlan, *, resume: bool = False,
                  lock: Optional[RunLock] = None) -> Dict[str, Any]:
        # Adaptive net-new top-up runs a DISTINCT loop; the normal single-pass body
        # below is left byte-for-byte unchanged so it (and every test that exercises
        # it) is unaffected when NET_NEW_SEND_SAFE_TARGET is 0 (the default).
        if config.NET_NEW_SEND_SAFE_TARGET > 0 and self.ctx.policy.allow_enrichment:
            return self._run_body_topup(plan, resume=resume, lock=lock)

        started = time.perf_counter()
        report = WaterfallReport()

        lane_results = self._acquire(plan, resume=resume)
        postings: List[Dict[str, Any]] = []
        for r in lane_results.values():
            postings.extend(r.jobs)
        report.set_unit("postings", len(postings))

        # Cross-run dedup: skip postings whose exact identity was processed to
        # completion in a prior run (a NEW posting from the same company is not
        # blocked). Read before acquisition-dedup, delivery reads its own set.
        supp = SuppressionStore(self.state)
        seen_postings = supp.seen_postings()
        opportunities, dedup_stage = self._dedup(postings, seen_postings)
        report.add(dedup_stage)
        report.set_unit("opportunities", len(opportunities))
        acquisition_requests = sum(int(r.physical_requests or 0) for r in lane_results.values())
        self.ledger.record(
            STAGE_ACQUISITION,
            {"jobs_captured": len(postings), "unique_opportunities": len(opportunities)},
            acquisition_entered=bool(acquisition_requests),
            physical_requests=acquisition_requests,
            source_counts={lane: len(r.jobs) for lane, r in lane_results.items()},
        )

        # Enrichment (skipped if the mode forbids it, e.g. live_acquisition_only)
        enrichment = None
        delivery = None
        capacity = None
        if self.ctx.policy.allow_enrichment:
            # Pre-Apollo existing-lead dedupe (config.PRE_APOLLO_EXISTING_DEDUPE):
            # snapshot Airtable identity ONCE, feed the function/account exclusion
            # sets into enrichment BEFORE any Apollo spend, and hand the same
            # snapshot to delivery so it never reads Airtable a second time. The
            # feature is fully fail-open: any snapshot failure proceeds with no
            # pre-suppression, and delivery's own suppression remains the backstop.
            existing_snapshot = self._existing_identity_snapshot()
            enr_kwargs: Dict[str, Any] = {}
            deliver_kwargs: Dict[str, Any] = {}
            if existing_snapshot is not None:
                if config.AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION:
                    enr_kwargs["exclude_company_function_keys"] = (
                        existing_snapshot["company_function_keys"])
                if config.AIRTABLE_SUPPRESS_ACCOUNT_LEVEL:
                    enr_kwargs["exclude_company_keys"] = (
                        existing_snapshot["company_bare_keys"])
                deliver_kwargs["existing"] = existing_snapshot["existing"]

            enrichment = plan.enrichment_engine.run(opportunities, **enr_kwargs)
            for st in enrichment.stages:
                report.add(st)
            report.census(enrichment.dispositions())
            report.set_unit("companies", len({l.company.get("name") for l in enrichment.leads
                                               if l.company and l.company.get("name")}))
            # A "contact" is a lead we actually resolved an email for (usable or
            # pending verification) -- not merely a lead that carries a row.
            report.set_unit("contacts", len([l for l in enrichment.leads if l.contact.get("email")]))
            report.set_unit("final_pass_leads", len(enrichment.final_pass()))
            self.ledger.record(
                STAGE_ENRICHMENT,
                _enrichment_ledger_metrics(getattr(enrichment, "funnel", {}), enrichment.leads),
            )

            delivery = plan.delivery_manager.deliver(
                enrichment.leads, run_id=self.ctx.run_id,
                known_delivered=supp.delivered_leads(), **deliver_kwargs)
            report.set_unit("delivered_rows", delivery.created)
            report.set_unit("enrolled_contacts", delivery.enrolled)
            self.ledger.record(
                STAGE_DELIVERY,
                {
                    "sent_to_airtable": getattr(delivery, "created", None),
                    "airtable_suppressed": getattr(delivery, "skipped_existing", None),
                    # Only a run that was PERMITTED to enroll can answer this; every
                    # other run leaves the key absent so the report reads it as
                    # unavailable rather than as a measured zero.
                    "sent_to_instantly": (getattr(delivery, "enrolled", None)
                                          if self.ctx.policy.allow_instantly_enrollment else None),
                },
            )

            # Safe commit: only AFTER the stages completed do we mark postings
            # processed. Crucially, ONLY postings that reached a safe-terminal
            # disposition (delivered FINAL_PASS or a genuine business REJECT) are
            # committed to cross-run suppression. Provider-deferred outcomes
            # (NEEDS_CHECK / UNVERIFIED / REROUTE) stay retryable, so an Apollo or
            # Hunter outage never permanently suppresses an unprocessed posting
            # (Defect B). Failed deliveries are never recorded as delivered
            # (RealDelivery excludes failed_lead_keys).
            supp.commit_postings(enrichment.terminal_posting_ids())
            supp.commit_delivered(getattr(delivery, "delivered_lead_keys", []) or [])

            capacity = build_capacity_report(
                raw_postings=len(postings),
                opportunities=len(opportunities),
                enrichment=enrichment,
                delivered_final_pass=delivery.created,
                acquisition_requests=sum(r.physical_requests for r in lane_results.values()),
                enrichment_calls=len(opportunities) * 6,
                runtime_seconds=time.perf_counter() - started,
                quota_consumed=plan.quota_consumed,
                inventory_remaining=plan.inventory_remaining,
                runs_per_day=plan.runs_per_day,
                target=plan.target,
            )

        # -- reconciliation verdict ---------------------------------------
        target_ok = None
        if enrichment is not None and delivery is not None:
            # The target may be satisfied ONLY by delivered FINAL_PASS records.
            target_ok = report.target_satisfied(plan.target, delivered_final_pass=delivery.created)

        all_reconcile = report.all_reconcile()
        if enrichment is not None and delivery is not None:
            all_reconcile = all_reconcile and delivery.reconciles() and delivery.enrollment_reconciles()

        status = RunStatus.COMPLETE if all_reconcile else RunStatus.INCOMPLETE
        stop = "" if all_reconcile else "a stage or delivery boundary failed to reconcile"
        # A provider-capacity stop (Apollo circuit / enrichment runtime budget)
        # leaves eligible companies un-enriched and resumable. The boundary
        # accounting still reconciles (completed leads were delivered), so force an
        # explicit INCOMPLETE with the capacity stop reason -- never a false success.
        if enrichment is not None and getattr(enrichment, "enrichment_incomplete", False):
            status = RunStatus.INCOMPLETE
            stop = f"enrichment_incomplete:{enrichment.stop_reason or 'provider_capacity'}"
        resumed = self.ctx.resumed_from
        self.ctx.finish(status, stop)
        self.ctx.resumed_from = resumed  # finish() must not erase resume provenance

        result = {
            "run": self.ctx.to_dict(),
            "lanes": {lane: r.to_dict() for lane, r in lane_results.items()},
            "waterfall": report.to_dict(),
            "enrichment": enrichment.to_dict() if enrichment else None,
            "delivery": delivery.to_dict() if delivery else None,
            "capacity": capacity.to_dict() if capacity else None,
            "target_satisfied_by_final_pass_only": target_ok,
            "budget": self.budget.to_dict(),
            "run_lock": lock.to_dict() if lock is not None else None,
            "suppression": supp.to_dict(),
            "all_reconcile": all_reconcile,
            "enrichment_incomplete": bool(getattr(enrichment, "enrichment_incomplete", False)) if enrichment else False,
            "enrichment_stop_reason": getattr(enrichment, "stop_reason", "") if enrichment else "",
        }
        # Immutable run artifacts.
        self.state.write_artifact("run_status.json", {"run_id": self.ctx.run_id,
                                                      "status": self.ctx.status.value,
                                                      "stop_reason": self.ctx.stop_reason})
        self.state.write_artifact("run_manifest.json", self.ctx.to_dict())
        self.state.write_artifact("waterfall.json", report.to_dict())
        self.state.write_artifact("lanes.json", {lane: r.to_dict() for lane, r in lane_results.items()})
        if capacity is not None:
            self.state.write_artifact("capacity_report.json", capacity.to_dict())
        if delivery is not None:
            self.state.write_artifact("delivery.json", delivery.to_dict())
        self.state.write_artifact("orchestrator_result.json", result)
        return result

    # -- adaptive net-new top-up ------------------------------------------
    class _FantasticSliceCap:
        """Set a per-iteration RUNTIME slice budget so one iteration bills at most `n`
        jobs. It sets ``FANTASTIC_JOBS_RUN_SLICE_CAP`` -- NOT the config-validated
        global ``FANTASTIC_JOBS_MAX_JOBS_PER_RUN`` -- so a slice smaller than a segment
        limit (e.g. 500 vs LINKEDIN_LIMIT=6000) stays valid: the adapter clamps this
        iteration's acquisition to the slice while config validation still sees the real
        ceiling. The controller separately clamps CUMULATIVE billing to the safety cap.
        Restores on exit."""
        def __init__(self, n: int) -> None:
            self.n = int(n)

        def __enter__(self):
            self._orig = getattr(config, "FANTASTIC_JOBS_RUN_SLICE_CAP", 0)
            config.FANTASTIC_JOBS_RUN_SLICE_CAP = self.n
            return self

        def __exit__(self, *exc):
            config.FANTASTIC_JOBS_RUN_SLICE_CAP = self._orig
            return False

    class _FantasticAcquireMode:
        """Select the Fantastic acquisition phase for one slice. The FIRST slice runs
        the fresh-edge head pass (then deep to fill); later slices run DEEP only, so
        the top-of-feed head query is billed at most ONCE per run. Restores on exit."""
        def __init__(self, mode: str) -> None:
            self.mode = str(mode)

        def __enter__(self):
            self._orig = getattr(config, "FANTASTIC_JOBS_ACQUIRE_MODE", "head_then_deep")
            config.FANTASTIC_JOBS_ACQUIRE_MODE = self.mode
            return self

        def __exit__(self, *exc):
            config.FANTASTIC_JOBS_ACQUIRE_MODE = self._orig
            return False

    @staticmethod
    def _count_net_new_send_safe(leads, delivery) -> int:
        """The top-up target unit: leads CREATED this slice whose stored facts pass
        send_safe_facts. Duplicates/existing/suppressed/non-send-safe never count."""
        import airtable_client
        created = set((getattr(delivery, "detail", {}) or {})
                      .get("airtable", {}).get("created_lead_keys", []) or [])
        if not created:
            return 0
        n = 0
        for lead in leads:
            if lead.contact_key not in created:
                continue
            row = lead.contact.get("_airtable_row") or {}
            try:
                if airtable_client.send_safe_facts(airtable_client._job_to_fields(row))[0]:
                    n += 1
            except Exception:  # noqa: BLE001 - a malformed row never inflates net-new
                continue
        return n

    def _ledger_record_slice(self, acq_cum: Dict[str, Any], funnel_cum: Dict[str, Any],
                             leads: Sequence[Any], agg: Any) -> None:
        """Persist CUMULATIVE top-up counters to the compact reporting ledger.

        Called at every slice boundary so an interrupted run still reports the work
        it completed. Values mirror the waterfall the run report is built from, so
        the ledger and the heavy artifacts can never disagree.
        """
        requests = int(acq_cum.get("physical_requests") or 0)
        self.ledger.record(
            STAGE_ACQUISITION,
            {"jobs_captured": acq_cum.get("jobs_unique_kept"),
             "unique_opportunities": len(leads)},
            acquisition_entered=bool(requests),
            physical_requests=requests,
            source_counts={str(src): dict(vals)
                           for src, vals in (acq_cum.get("per_source") or {}).items()},
        )
        self.ledger.record(STAGE_ENRICHMENT, _enrichment_ledger_metrics(funnel_cum, leads))
        self.ledger.record(
            STAGE_DELIVERY,
            {"sent_to_airtable": getattr(agg, "created", None),
             "airtable_suppressed": getattr(agg, "skipped_existing", None),
             "sent_to_instantly": (getattr(agg, "enrolled", None)
                                   if self.ctx.policy.allow_instantly_enrollment else None)},
        )

    def _run_body_topup(self, plan: OrchestratorPlan, *, resume: bool = False,
                        lock: Optional[RunLock] = None) -> Dict[str, Any]:
        from orchestrator.enrichment import Disposition, EnrichmentReport
        from orchestrator.adapters_real import RealDeliveryReport

        started = time.perf_counter()
        report = WaterfallReport()
        supp = SuppressionStore(self.state)

        # MONTHLY CREDIT GOVERNOR (P0): the spending AUTHORITY for this run. The
        # controller's cumulative billing cap becomes
        #     min(FANTASTIC_JOBS_MAX_JOBS_PER_RUN, governor.run_budget)
        # (the provider quota floor is enforced by both the controller and the
        # adapter). NET_NEW_SEND_SAFE_TARGET may stop the loop EARLY but can never
        # raise this cap. Inputs are the LAST KNOWN provider headers (0-credit quota
        # snapshot) -- never a row-producing call. Flag OFF => cap unchanged (6000).
        gov = self._build_governor()
        # ZERO-BUDGET QUOTA RECOVERY: the quota snapshot the governor reads is only
        # written from INSIDE acquire(), so a zero grant freezes the very input that
        # caused it -- the run then stops before acquisition, nothing rewrites the
        # snapshot, and the zero survives even a real provider quota reset. When (and
        # only when) the zero was derived from provider-quota metadata, spend ONE
        # 0-row count request to re-read the provider's quota headers and rebuild the
        # governor ONCE. Deliberately placed BEFORE run_cap/controller are derived so
        # a recovered budget flows through the normal path with no recompute. Fail
        # closed: the original decision stands unless the refresh actually succeeded.
        gov, quota_refresh = self._maybe_refresh_quota(gov)
        run_cap = int(config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN)
        budget_source = "per_run_ceiling"
        if gov.run_budget is not None and gov.run_budget < run_cap:
            run_cap, budget_source = int(gov.run_budget), "governor"
        # date_created WATERMARK (Category 2, default OFF) is a SINGLE-WINDOW run:
        # the window is pinned once and the adapter pages within it, so the top-up
        # loop must run exactly ONE slice sized to the whole run cap (Gate-E D3).
        # Multi-slice would re-open the same window and re-bill it.
        watermark_on = bool(getattr(config, "FANTASTIC_DATE_CREATED_WATERMARK_ENABLED", False))
        slice_jobs = run_cap if watermark_on else config.FANTASTIC_TOPUP_SLICE_JOBS
        controller = TopUpController(
            target_net_new=config.NET_NEW_SEND_SAFE_TARGET,
            safety_cap_jobs=run_cap,
            slice_jobs=max(1, int(slice_jobs)),
            min_quota_remaining=config.FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING,
            runtime_budget_seconds=(config.TOPUP_RUNTIME_BUDGET_SECONDS or None),
            max_iterations=(1 if watermark_on else config.TOPUP_MAX_ITERATIONS),
            budget_source=budget_source,
        )
        ledger = self._build_yield_ledger()
        # Cumulative acquisition accounting across ALL top-up slices (Gate D): the
        # summary must never report only the last slice. Three distinct series:
        # unique-kept jobs, returned/billed rows, and provider quota consumed.
        acq_cum: Dict[str, Any] = {
            "jobs_unique_kept": 0, "jobs_returned_billed": 0, "jobs_quota_consumed": 0,
            "physical_requests": 0, "cross_query_duplicates": 0, "per_source": {},
            "last_jobs_quota_remaining": None}
        acq_iters: List[Dict[str, Any]] = []
        # The top-up loop enriches once per SLICE, and the run report is assembled
        # from the accumulation. Before 2026-09-04 the final EnrichmentReport was
        # rebuilt as ``EnrichmentReport(leads=all_leads, stages=[])`` -- discarding
        # every slice's funnel -- so ``enrichment.funnel`` was ALWAYS {} on the
        # production path and jobs_reviewed / qualified_opportunities were
        # permanently unreportable, however productive the run had been.
        funnel_cum: Dict[str, Any] = {}
        loss_cum: Dict[str, Any] = {}

        # Pre-Apollo dedupe: snapshot ONCE, then keep a live function-key set so a
        # company+function we CREATE in an earlier slice is not re-enriched in a
        # later slice (no second Airtable read; delivery stays the backstop).
        snapshot = self._existing_identity_snapshot()
        live_function_keys = set(snapshot["company_function_keys"]) if snapshot else set()
        # Frozen copy of the coverage that existed AT RUN START. The shadow
        # function-aware-dedupe metric must be judged against this, not against
        # ``live_function_keys`` (which grows as this run creates rows) -- otherwise
        # a row created earlier in the same run would be scored as "avoidable".
        covered_at_start = frozenset(live_function_keys)
        account_keys = (set(snapshot["company_bare_keys"])
                        if (snapshot and config.AIRTABLE_SUPPRESS_ACCOUNT_LEVEL) else set())
        existing = snapshot["existing"] if snapshot else None

        all_leads: List[Any] = []
        lane_results: Dict[str, LaneResult] = {}
        postings_total = 0
        agg = RealDeliveryReport(mode="topup")
        last_quota: Optional[int] = None
        last_circuit = False
        last_inventory = False
        stop_reason = ""
        acquisition_error = ""  # set on a FAILED acquisition lane (not "no inventory")

        # A zero governor grant is a CLEAN, distinct stop before any acquisition
        # (Gate-E D8) -- never a failed run, never an acquisition attempt.
        if gov.run_budget is not None and gov.run_budget <= 0:
            stop_reason = "governor_zero_budget"

        while not stop_reason:
            decision = controller.decide(
                quota_remaining=last_quota, apollo_circuit_open=last_circuit,
                inventory_exhausted=last_inventory)
            if not decision.should_continue:
                stop_reason = decision.stop_reason
                break

            # Slice 1 discovers the fresh edge (head) then backfills (deep); every
            # later slice is DEEP only, so the top-of-feed head query is billed at
            # most once per run and top-up never re-runs the fresh-edge query.
            slice_mode = "head_then_deep" if controller.iterations == 0 else "deep"
            with self._FantasticSliceCap(decision.next_slice), \
                    self._FantasticAcquireMode(slice_mode):
                iter_lanes = self._acquire(plan, resume=(resume and controller.iterations == 0))
            lane_results = iter_lanes  # last slice's lanes surface in the result

            # A FAILED acquisition lane is an operational error (config/provider/parse
            # crash), NOT "inventory exhausted". Stop immediately and mark the run
            # failed so a silent zero-acquisition run can never be reported complete.
            failed_lanes = [(name, r) for name, r in iter_lanes.items() if r.status == "failed"]
            if failed_lanes:
                acquisition_error = "; ".join(
                    f"{name}: {'; '.join(r.errors) if r.errors else 'lane failed'}"
                    for name, r in failed_lanes)
                stop_reason = "acquisition_failed"
                break

            iter_postings = [j for r in iter_lanes.values() for j in r.jobs]
            kept = len(iter_postings)
            postings_total += kept

            fl = iter_lanes.get("fantastic")
            # BILLED = rows the provider RETURNED (it bills every returned row,
            # including ones we dedupe/reject), NOT the unique-kept count. Falls back
            # to kept when the lane exposes no billing counter (non-Fantastic lanes).
            billed = kept
            if fl is not None:
                q = fl.attribution.get("jobs_quota_remaining")
                last_quota = int(q) if q is not None else last_quota
                rb = int(fl.attribution.get("raw_records") or 0)
                billed = rb if rb > 0 else kept
                acq_cum["jobs_quota_consumed"] += int(fl.attribution.get("jobs_quota_consumed") or 0)
                acq_cum["cross_query_duplicates"] += int(fl.attribution.get("cross_query_duplicates") or 0)
                for src, s in (fl.attribution.get("per_source") or {}).items():
                    agg_src = acq_cum["per_source"].setdefault(src, {"jobs": 0, "returned_billed": 0, "requests": 0})
                    for k in agg_src:
                        agg_src[k] += int(s.get(k, 0) or 0)
                acq_cum["last_jobs_quota_remaining"] = last_quota
            acq_cum["jobs_unique_kept"] += kept
            acq_cum["jobs_returned_billed"] += billed
            acq_cum["physical_requests"] += sum(int(r.physical_requests or 0) for r in iter_lanes.values())
            last_inventory = (kept == 0)
            for j in iter_postings:
                j.setdefault("_acquisition_mode", slice_mode)
            ledger.record_acquired(iter_postings, mode=slice_mode)

            opportunities, dedup_stage = self._dedup(iter_postings, supp.seen_postings())
            report.add(dedup_stage)
            # Ledger: postings that exit at dedupe never become leads.
            passed_ids = {str(o.get("posting_id") or o.get("job_id")) for o in opportunities}
            for j in iter_postings:
                jid = str(j.get("posting_id") or j.get("job_id") or "")
                if jid and jid not in passed_ids:
                    ledger.mark(jid, exit_stage="dedup_previously_seen", previously_seen=True)

            # Checkpoint acquisition BEFORE enrichment and delivery run. Recording
            # only at the end of the slice would lose these counters whenever a
            # later stage crashed -- and "we acquired 6,206 jobs then died" is
            # precisely the fact the weekly report needs to keep.
            self.ledger.record(
                STAGE_ACQUISITION,
                {"jobs_captured": acq_cum["jobs_unique_kept"]},
                acquisition_entered=bool(acq_cum["physical_requests"]),
                physical_requests=int(acq_cum["physical_requests"]),
                source_counts={str(src): dict(vals)
                               for src, vals in (acq_cum.get("per_source") or {}).items()},
            )

            enr_kwargs: Dict[str, Any] = {}
            deliver_kwargs: Dict[str, Any] = {}
            if snapshot is not None:
                if config.AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION:
                    enr_kwargs["exclude_company_function_keys"] = set(live_function_keys)
                if account_keys:
                    enr_kwargs["exclude_company_keys"] = account_keys
                deliver_kwargs["existing"] = existing

            enrichment = plan.enrichment_engine.run(opportunities, **enr_kwargs)
            for st in enrichment.stages:
                report.add(st)
            all_leads.extend(enrichment.leads)
            _accumulate_counts(funnel_cum, getattr(enrichment, "funnel", {}) or {})
            _accumulate_counts(loss_cum, getattr(enrichment, "loss_census", {}) or {})

            delivery = plan.delivery_manager.deliver(
                enrichment.leads, run_id=self.ctx.run_id,
                known_delivered=supp.delivered_leads(), **deliver_kwargs)

            net_new = self._count_net_new_send_safe(enrichment.leads, delivery)
            last_circuit = bool(getattr(enrichment, "enrichment_incomplete", False)
                                and "apollo" in str(getattr(enrichment, "stop_reason", "")))
            self._ledger_mark_outcomes(ledger, enrichment.leads, delivery,
                                       covered_at_start=covered_at_start)

            supp.commit_postings(enrichment.terminal_posting_ids())
            supp.commit_delivered(getattr(delivery, "delivered_lead_keys", []) or [])
            # Gate-E D12: postings that terminally collapsed into another lead (N->1)
            # must also be committed, else they are re-billed + re-enriched every run.
            collapsed_ids = [rid for lead in enrichment.leads
                             for rid in (getattr(lead, "related_posting_ids", []) or [])
                             if rid and rid != getattr(lead, "posting_id", None)]
            if collapsed_ids:
                supp.commit_postings(collapsed_ids)
            # date_created WATERMARK commit: ONLY now -- after this window's postings
            # were processed AND persisted to the suppression store (Gate-E D4).
            if watermark_on:
                try:
                    import fantastic_jobs_adapter as _fja
                    wm = _fja.commit_watermark(success=True)
                    acq_cum["watermark_commit"] = wm
                except Exception as exc:  # noqa: BLE001 - leaves the window in-flight (replayed next run)
                    acq_cum["watermark_commit"] = {"committed": False, "error": type(exc).__name__}
            # Feed created company+function keys forward so later slices skip them.
            for lead in enrichment.leads:
                import airtable_client
                live_function_keys |= airtable_client.company_function_keys_for_job(
                    lead.contact.get("_airtable_row") or {})

            # Accumulate delivery counters (each slice reconciles => the sum does).
            # getattr defaults keep this tolerant of any delivery-report shape.
            agg.entered += int(getattr(delivery, "entered", 0) or 0)
            agg.reviewable_submitted += int(getattr(delivery, "reviewable_submitted", 0) or 0)
            agg.created += int(getattr(delivery, "created", 0) or 0)
            agg.skipped += int(getattr(delivery, "skipped", 0) or 0)
            agg.skipped_existing += int(getattr(delivery, "skipped_existing", 0) or 0)
            agg.failed += int(getattr(delivery, "failed", 0) or 0)
            agg.enrolled += int(getattr(delivery, "enrolled", 0) or 0)
            agg.final_pass += int(getattr(delivery, "final_pass", 0) or 0)
            agg.needs_check += int(getattr(delivery, "needs_check", 0) or 0)
            agg.other_reviewable += int(getattr(delivery, "other_reviewable", 0) or 0)
            agg.delivered_lead_keys.extend(getattr(delivery, "delivered_lead_keys", []) or [])

            controller.record(billed=billed, net_new_send_safe=net_new)
            acq_iters.append({"slice_index": controller.iterations, "slice_mode": slice_mode,
                              "jobs_unique_kept": kept, "jobs_returned_billed": billed,
                              "physical_requests": sum(int(r.physical_requests or 0) for r in iter_lanes.values()),
                              "net_new_send_safe": net_new, "quota_remaining": last_quota})
            # Checkpoint the compact reporting record at every slice boundary, with
            # CUMULATIVE values. A run killed mid-loop then reports what it had
            # actually achieved instead of disappearing from the week.
            self._ledger_record_slice(acq_cum, funnel_cum, all_leads, agg)

        # Record this run's ACTUAL billed credits against the monthly ledger
        # (idempotent per run_id; no-op when the governor is disabled).
        self._commit_governor(gov, billed=controller.billed)
        ledger.flush()

        # -- build the run report from the accumulation -------------------
        report.set_unit("postings", postings_total)
        report.set_unit("opportunities", len(all_leads))
        report.census([l.disposition for l in all_leads])
        report.set_unit("companies", len({l.company.get("name") for l in all_leads
                                           if l.company and l.company.get("name")}))
        report.set_unit("contacts", len([l for l in all_leads if l.contact.get("email")]))
        report.set_unit("final_pass_leads",
                        len([l for l in all_leads if l.disposition is Disposition.FINAL_PASS]))
        report.set_unit("delivered_rows", agg.created)
        report.set_unit("net_new_send_safe", controller.net_new)
        # Email-presence / verification counters (Gate D): derived from the leads'
        # actual resolved email + Apollo status, never from disposition labels.
        with_email = [l for l in all_leads if l.contact.get("email")]
        def _verified(l) -> bool:
            row = l.contact.get("_airtable_row") or {}
            st = str(row.get("apollo_email_status") or getattr(l, "email_status", "") or "").lower()
            return st == "verified"
        n_verified = sum(1 for l in with_email if _verified(l))
        emails_block = {"with_email": len(with_email), "verified": n_verified,
                        "unverified": len(with_email) - n_verified}

        # Final compact reporting record. Also covers the paths the slice loop never
        # reached -- a zero governor grant stops before the first iteration, and that
        # is a MEASURED zero capture, not an absent counter.
        self._ledger_record_slice(acq_cum, funnel_cum, all_leads, agg)
        self.ledger.record(STAGE_ENRICHMENT, {"verified_emails": n_verified})

        enrichment = EnrichmentReport(leads=all_leads, stages=[],
                                      funnel=funnel_cum, loss_census=loss_cum)
        capacity = build_capacity_report(
            raw_postings=postings_total, opportunities=len(all_leads),
            enrichment=enrichment, delivered_final_pass=agg.created,
            acquisition_requests=sum(r.physical_requests for r in lane_results.values()),
            enrichment_calls=len(all_leads) * 6,
            runtime_seconds=time.perf_counter() - started,
            quota_consumed=plan.quota_consumed, inventory_remaining=plan.inventory_remaining,
            runs_per_day=plan.runs_per_day, target=plan.target)

        all_reconcile = (report.all_reconcile() and agg.reconciles()
                         and agg.enrollment_reconciles())
        target_reached = controller.net_new >= controller.target_net_new
        if acquisition_error:
            # A failed acquisition lane overrides everything: the run is FAILED, never
            # a silent "complete" with raw_postings=0.
            status = RunStatus.FAILED
            self.ctx.finish(status, f"acquisition_failed: {acquisition_error}")
        else:
            status = RunStatus.COMPLETE if all_reconcile else RunStatus.INCOMPLETE
            stop = "" if all_reconcile else "a stage or delivery boundary failed to reconcile"
            self.ctx.finish(status, stop or f"topup:{stop_reason}")

        topup_dict = controller.to_dict()
        topup_dict["final_stop_reason"] = stop_reason
        topup_dict["acquisition_error"] = acquisition_error
        # CUMULATIVE acquisition block (additive; result["lanes"] stays the last
        # slice so the f27ccf1 failure observability is byte-for-byte preserved).
        acquisition_block = {
            "iterations": controller.iterations,
            "final_stop_reason": stop_reason,
            "acquisition_error": acquisition_error,
            "budget_source": controller.budget_source,
            "run_cap": controller.safety_cap_jobs,
            "cumulative": dict(acq_cum),
            "per_iteration": acq_iters,
        }
        result = {
            "run": self.ctx.to_dict(),
            "lanes": {lane: r.to_dict() for lane, r in lane_results.items()},
            "acquisition": acquisition_block,
            "governor": {**gov.to_dict(), "quota_refresh": quota_refresh},
            "yield_ledger": ledger.summary(),
            "emails": emails_block,
            "waterfall": report.to_dict(),
            "enrichment": enrichment.to_dict(),
            "delivery": agg.to_dict(),
            "capacity": capacity.to_dict(),
            "topup": topup_dict,
            "target_satisfied_by_final_pass_only": None,
            "budget": self.budget.to_dict(),
            "run_lock": lock.to_dict() if lock is not None else None,
            "suppression": supp.to_dict(),
            "all_reconcile": all_reconcile,
        }
        self.state.write_artifact("run_status.json", {"run_id": self.ctx.run_id,
                                                      "status": self.ctx.status.value,
                                                      "stop_reason": self.ctx.stop_reason})
        self.state.write_artifact("run_manifest.json", self.ctx.to_dict())
        self.state.write_artifact("waterfall.json", report.to_dict())
        self.state.write_artifact("delivery.json", agg.to_dict())
        self.state.write_artifact("capacity_report.json", capacity.to_dict())
        self.state.write_artifact("topup.json", topup_dict)
        self.state.write_artifact("acquisition.json", acquisition_block)
        self.state.write_artifact("orchestrator_result.json", result)
        return result

    # -- governor / ledger helpers (all fail-open; never affect run outcome) -----
    def _build_governor(self):
        from orchestrator import fantastic_governor as G
        try:
            if not bool(getattr(config, "FANTASTIC_MONTHLY_GOVERNOR_ENABLED", False)):
                return G.GovernorContext(enabled=False, decision=None, ledger=None)
            import fantastic_jobs_adapter as _fja
            snap = _fja.load_quota_snapshot()
            reset_at = G._parse_iso(snap.get("next_billing_date") or "")
            jr = snap.get("jobs_remaining")
            return G.build_context(config, run_id=self.ctx.run_id,
                                   provider_jobs_remaining=(int(jr) if jr is not None else None),
                                   provider_reset_at=reset_at)
        except Exception as exc:  # noqa: BLE001 - a governor failure must fail CONSERVATIVELY
            print(f"[governor] unavailable ({type(exc).__name__}); granting the daily minimum only")
            from orchestrator.fantastic_governor import GovernorDecision
            dmin = int(getattr(config, "FANTASTIC_DAILY_MIN_JOBS", 100) or 0)
            return G.GovernorContext(enabled=True, ledger=None, decision=GovernorDecision(
                run_budget=dmin, reason="governor_error_conservative", remaining_credits=0,
                spendable_credits=0, reserve_credits=0, base_daily_allowance=0,
                carry_forward_applied=0, days_remaining=0.0, inventory_capped=False,
                provider_authoritative=False))

    def _maybe_refresh_quota(self, gov):
        """At most ONE provider quota refresh per run, then at most ONE rebuild.

        Returns ``(governor_context, info)``. The context returned is the ORIGINAL
        one unless a refresh genuinely succeeded, so every failure mode (disabled,
        non-quota reason, transport error, non-200, missing/malformed headers,
        unwritable snapshot) preserves today's zero-budget behaviour exactly.
        Never raises: recovery must not be able to break a run.
        """
        info: Dict[str, Any] = {"attempted": False, "refreshed": False, "reason": "",
                                "requests_made": 0, "budget_before": None,
                                "budget_after": None}
        try:
            from orchestrator import fantastic_governor as G
            info["budget_before"] = gov.run_budget
            if gov.run_budget is None or gov.decision is None:
                info["reason"] = "governor_not_governing"
                return gov, info
            if gov.run_budget > 0:
                info["reason"] = "budget_positive"
                return gov, info
            # Only a zero DERIVED FROM QUOTA METADATA can be corrected by re-reading
            # quota; every other zero reason is left untouched.
            if gov.decision.reason not in G.QUOTA_METADATA_ZERO_REASONS:
                info["reason"] = f"not_quota_metadata:{gov.decision.reason}"
                return gov, info
            if not bool(getattr(config, "FANTASTIC_JOBS_ENABLED", False)):
                info["reason"] = "fantastic_disabled"
                return gov, info
            # PROVIDER-HEADERS-OFF INVARIANT. With FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS
            # disabled, build_context() forces provider_reset_at to None and passes
            # provider_jobs_remaining=None, so `remaining` comes from the LOCAL ledger
            # and BOTH snapshot fields are ignored -- a refresh cannot change this
            # run's decision (verified: identical budget/reason for snapshot 236 vs
            # 100000). Spend no request rather than buy information we may not act on,
            # and leave the ledger-authoritative policy exactly as it is.
            if not bool(getattr(config, "FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS", True)):
                info["reason"] = "provider_headers_disabled"
                return gov, info

            import fantastic_jobs_adapter as _fja
            info["attempted"] = True
            res = _fja.refresh_quota_snapshot()
            info["requests_made"] = int(res.get("requests_made", 0) or 0)
            info["reason"] = str(res.get("reason", "") or "")
            info["http_status"] = res.get("http_status")
            if not res.get("refreshed"):
                info["budget_after"] = gov.run_budget      # unchanged (fail closed)
                print(f"[governor] quota refresh did not yield provider truth "
                      f"({info['reason']}); keeping the zero-budget decision")
                return gov, info
            info["refreshed"] = True
            info["jobs_remaining"] = res.get("jobs_remaining")
            info["next_billing_date"] = res.get("next_billing_date")
            # Rebuild EXACTLY once. This method runs once per run, so no second
            # refresh and no recursion is reachable from here. A rebuild failure is
            # named distinctly (the snapshot IS repaired, but this run keeps its
            # original zero) so observability never shows a bare "ok" with no budget.
            try:
                new_gov = self._build_governor()
            except Exception as exc:  # noqa: BLE001
                info["reason"] = f"rebuild_failed:{type(exc).__name__}"
                info["budget_after"] = gov.run_budget
                return gov, info
            info["budget_after"] = new_gov.run_budget
            print(f"[governor] quota snapshot refreshed (0 job rows, 1 request): "
                  f"budget {info['budget_before']} -> {info['budget_after']}")
            return new_gov, info
        except Exception as exc:  # noqa: BLE001 - recovery must never break a run
            info["reason"] = info["reason"] or f"error:{type(exc).__name__}"
            print(f"[governor] quota refresh skipped ({type(exc).__name__}); "
                  f"keeping the zero-budget decision")
            return gov, info

    def _commit_governor(self, gov, *, billed: int) -> None:
        try:
            from orchestrator import fantastic_governor as G
            G.commit_run(gov, run_id=self.ctx.run_id, billed=int(billed))
        except Exception as exc:  # noqa: BLE001
            print(f"[governor] ledger commit skipped: {type(exc).__name__}")

    def _build_yield_ledger(self):
        from orchestrator.yield_ledger import YieldLedger
        try:
            return YieldLedger(str(getattr(config, "YIELD_LEDGER_PATH", "") or ""), self.ctx.run_id,
                               enabled=bool(getattr(config, "YIELD_LEDGER_ENABLED", False)))
        except Exception:  # noqa: BLE001
            return YieldLedger("", self.ctx.run_id, enabled=False)

    @staticmethod
    def _ledger_mark_outcomes(ledger, leads, delivery, covered_at_start=frozenset()) -> None:
        """Attribute enrichment + delivery outcomes to each lead's PRIMARY posting
        and mark its related postings as collapsed (credit counted once)."""
        try:
            import airtable_client
            created = set((getattr(delivery, "detail", {}) or {}).get("airtable", {}).get("created_lead_keys", []) or [])
            for lead in leads:
                pid = str(getattr(lead, "posting_id", "") or "")
                if not pid:
                    continue
                row = lead.contact.get("_airtable_row") or {}
                email = bool(lead.contact.get("email"))
                status = str(row.get("apollo_email_status") or getattr(lead, "email_status", "") or "").lower()
                dispo = str(getattr(lead.disposition, "value", lead.disposition) or "")
                is_created = lead.contact_key in created
                try:
                    safe = bool(airtable_client.send_safe_facts(airtable_client._job_to_fields(row))[0]) if row else False
                except Exception:  # noqa: BLE001
                    safe = False
                # SHADOW: would the (default-OFF) function-aware upstream dedupe
                # have excluded this row before Fantastic billed it? Judged against
                # the run-start coverage using the SAME key helper delivery uses,
                # so the shadow can never disagree with the real suppression.
                shadow_exclude, shadow_reason = False, ""
                try:
                    fkeys = airtable_client.company_function_keys_for_job(row)
                    hit = sorted(fkeys & set(covered_at_start))
                    if hit:
                        shadow_exclude, shadow_reason = True, hit[0]
                except Exception:  # noqa: BLE001 - shadow metric never breaks a run
                    pass
                fam = ""
                try:
                    from orchestrator.function_acquisition import family_for_role
                    fam = family_for_role(str(row.get("_matched_role")
                                              or lead.contact.get("_matched_role") or ""))
                except Exception:  # noqa: BLE001
                    pass
                ledger.mark(pid, exit_stage="enriched",
                            function_aware_would_exclude=shadow_exclude,
                            function_aware_reason=shadow_reason,
                            acquisition_function_family=fam,
                            org_id_fallback_attempted=bool(row.get("_apollo_org_id_recovered")
                                                           or lead.contact.get("_apollo_org_id_recovered")),
                            org_id_fallback_recovered=bool(row.get("_apollo_org_id_recovered")),
                            icp_outcome=("reject" if dispo == "REJECT" else "pass"),
                            hm_outcome=("found" if email else "not_found"),
                            zero_apollo_people=(str(getattr(lead.primary_reason, "value", "")) == "hiring_manager_not_found"),
                            email_outcome=("verified" if (email and status == "verified") else ("unverified" if email else "none")),
                            send_safe=safe, airtable_created=is_created,
                            net_new_send_safe=(safe and is_created),
                            role_bucket=str(row.get("_role_bucket") or lead.contact.get("_role_bucket") or ""))
                ledger.mark_collapsed(pid, list(getattr(lead, "related_posting_ids", []) or []))
        except Exception:  # noqa: BLE001 - analytics never affects the run
            pass
