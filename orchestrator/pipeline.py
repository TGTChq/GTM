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
            lock.release()
            try:
                self.state.prune(keep=int(retention_keep), max_bytes=RETENTION_MAX_BYTES,
                                 protect={self.ctx.run_id})
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

            delivery = plan.delivery_manager.deliver(
                enrichment.leads, run_id=self.ctx.run_id,
                known_delivered=supp.delivered_leads(), **deliver_kwargs)
            report.set_unit("delivered_rows", delivery.created)
            report.set_unit("enrolled_contacts", delivery.enrolled)

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
        """Temporarily cap the Fantastic per-call max so one iteration bills at most
        `n` jobs; the controller separately clamps cumulative billing to the safety
        cap. Restores the original on exit."""
        def __init__(self, n: int) -> None:
            self.n = int(n)

        def __enter__(self):
            self._orig = config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN
            config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN = self.n
            return self

        def __exit__(self, *exc):
            config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN = self._orig
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

    def _run_body_topup(self, plan: OrchestratorPlan, *, resume: bool = False,
                        lock: Optional[RunLock] = None) -> Dict[str, Any]:
        from orchestrator.enrichment import Disposition, EnrichmentReport
        from orchestrator.adapters_real import RealDeliveryReport

        started = time.perf_counter()
        report = WaterfallReport()
        supp = SuppressionStore(self.state)
        controller = TopUpController(
            target_net_new=config.NET_NEW_SEND_SAFE_TARGET,
            safety_cap_jobs=config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN,
            slice_jobs=config.FANTASTIC_TOPUP_SLICE_JOBS,
            min_quota_remaining=config.FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING,
            runtime_budget_seconds=(config.TOPUP_RUNTIME_BUDGET_SECONDS or None),
            max_iterations=config.TOPUP_MAX_ITERATIONS,
        )

        # Pre-Apollo dedupe: snapshot ONCE, then keep a live function-key set so a
        # company+function we CREATE in an earlier slice is not re-enriched in a
        # later slice (no second Airtable read; delivery stays the backstop).
        snapshot = self._existing_identity_snapshot()
        live_function_keys = set(snapshot["company_function_keys"]) if snapshot else set()
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

        while True:
            decision = controller.decide(
                quota_remaining=last_quota, apollo_circuit_open=last_circuit,
                inventory_exhausted=last_inventory)
            if not decision.should_continue:
                stop_reason = decision.stop_reason
                break

            with self._FantasticSliceCap(decision.next_slice):
                iter_lanes = self._acquire(plan, resume=(resume and controller.iterations == 0))
            lane_results = iter_lanes  # last slice's lanes surface in the result
            iter_postings = [j for r in iter_lanes.values() for j in r.jobs]
            billed = len(iter_postings)
            postings_total += billed

            fl = iter_lanes.get("fantastic")
            if fl is not None:
                q = fl.attribution.get("jobs_quota_remaining")
                last_quota = int(q) if q is not None else last_quota
            last_inventory = (billed == 0)

            opportunities, dedup_stage = self._dedup(iter_postings, supp.seen_postings())
            report.add(dedup_stage)

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

            delivery = plan.delivery_manager.deliver(
                enrichment.leads, run_id=self.ctx.run_id,
                known_delivered=supp.delivered_leads(), **deliver_kwargs)

            net_new = self._count_net_new_send_safe(enrichment.leads, delivery)
            last_circuit = bool(getattr(enrichment, "enrichment_incomplete", False)
                                and "apollo" in str(getattr(enrichment, "stop_reason", "")))

            supp.commit_postings(enrichment.terminal_posting_ids())
            supp.commit_delivered(getattr(delivery, "delivered_lead_keys", []) or [])
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

        enrichment = EnrichmentReport(leads=all_leads, stages=[])
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
        status = RunStatus.COMPLETE if all_reconcile else RunStatus.INCOMPLETE
        stop = "" if all_reconcile else "a stage or delivery boundary failed to reconcile"
        self.ctx.finish(status, stop or f"topup:{stop_reason}")

        topup_dict = controller.to_dict()
        topup_dict["final_stop_reason"] = stop_reason
        result = {
            "run": self.ctx.to_dict(),
            "lanes": {lane: r.to_dict() for lane, r in lane_results.items()},
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
        self.state.write_artifact("orchestrator_result.json", result)
        return result
