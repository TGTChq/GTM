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

    def _run_body(self, plan: OrchestratorPlan, *, resume: bool = False,
                  lock: Optional[RunLock] = None) -> Dict[str, Any]:
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
            enrichment = plan.enrichment_engine.run(opportunities)
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
                known_delivered=supp.delivered_leads())
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
