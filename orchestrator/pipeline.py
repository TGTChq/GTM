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
    STAGE_FINAL,
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_INCOMPLETE,
    RunLedger,
    backfill_from_artifacts,
    prune_ledger,
    reason_census_from_parts,
)
from orchestrator.runcontrol import RunContext, RunStatus
from orchestrator.runlock import RunLock, RunLockHeld
from orchestrator.state import StateManager
from orchestrator.suppression import SuppressionStore
from orchestrator import pending_work
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
    # Brett's "qualified opportunities" = job/role policy passed AND company ICP
    # passed AND the opportunity entered contact discovery. Deliberately NOT
    # target_role_eligible, which is the pre-contact role gate and passed 92.6%
    # of postings on the 2026-09-04 control run. There is no fallback to the
    # looser counter: reporting the wrong stage under the right name is worse
    # than reporting nothing.
    ("qualified_opportunities", "contact_discovery_entered"),
    ("role_qualified_postings", "target_role_eligible"),
    ("companies_considered", "companies_considered"),
)

def _opportunity_key(job) -> str:
    """company x function -- the unit approvals are capped by, and NOT a posting.

    The same functions the Airtable suppression rule keys on, so a cohort counted
    here and a cohort counted by `run_maintenance.measure_identities` agree.
    """
    try:
        from airtable_client import _company_identity_keys_from_job
        from role_mapping import get_bucket_name_for_job
    except Exception:  # noqa: BLE001 - identity is observability, never a run blocker
        return ""
    try:
        keys = _company_identity_keys_from_job(job)
        if not keys:
            return ""
        return f"{sorted(keys)[0]}|{get_bucket_name_for_job(job) or 'unbucketed'}"
    except Exception:  # noqa: BLE001
        return ""


def _account_recovery_cohort(cohort, leads, delivery) -> None:
    """Follow the recovered cohort through ONE slice's enrichment and delivery.

    Attribution is by POSTING IDENTITY, which is the only key shared by the pending
    store, the enrichment leads and the delivery record. A lead belongs to the cohort
    if its own posting is in it OR any posting collapsed into it is -- a recovered
    posting folded into a lead alongside fresh work is still recovered work, and
    dropping it would understate the cohort every time the collapse fired.

    Delivered lead keys are collected rather than counted so the cohort can be
    reconciled LATER against Airtable and the Approved Sync that enrols it, which
    runs in a different service on a different schedule. A count cannot be joined to
    anything; a key can.
    """
    ids = cohort.get("posting_ids") or set()
    if not ids:
        return
    delivered = set(getattr(delivery, "delivered_lead_keys", []) or [])
    for lead in leads or []:
        own = str(getattr(lead, "posting_id", "") or "")
        related = [str(r) for r in (getattr(lead, "related_posting_ids", []) or [])]
        if own not in ids and not any(r in ids for r in related):
            continue
        cohort["leads"] += 1
        # ATTEMPTED means the stage produced an outcome for this opportunity. It is
        # the only honest denominator for a conversion rate: work the run never
        # reached has no outcome and must not sit in the bottom of a fraction.
        opp = _opportunity_key(getattr(lead, "company", None) or {})
        if not opp:
            opp = str(getattr(lead, "posting_id", "") or "")
        if opp:
            cohort["attempted_opportunity_keys"].add(opp)
        contact_key = str(getattr(lead, "contact_key", "") or "")
        # A CONTACT IS AN ADDRESS, not a key. `_build_no_contact_lead` still carries a
        # `contact_key`, so counting the key reported `with_contact 26` on a
        # calibration that found TWO contacts -- and printed `opp->contact 1.0`, a
        # 100% conversion, on a run whose own funnel said `contacts_found 2`.
        contact = getattr(lead, "contact", None) or {}
        email = str((contact.get("email") if isinstance(contact, dict) else "") or "").strip()
        if email:
            cohort["with_contact"] += 1
        disposition = str(getattr(getattr(lead, "disposition", None), "value", "") or "")
        if disposition == "FINAL_PASS":
            cohort["final_pass"] += 1
        elif disposition == "NEEDS_CHECK":
            cohort["needs_check"] += 1
        elif disposition == "REJECT":
            cohort["rejected"] += 1
        else:
            cohort["other"] += 1
        if contact_key and contact_key in delivered:
            cohort["delivered_lead_keys"].append(contact_key)


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


#: Additive per-source acquisition counters. Every one of these is a count of
#: PROVIDER ROWS at a named decision point, so summing them across top-up slices
#: is meaningful. Anything not listed here (``stop_reason``, offsets, ``drained``)
#: describes a state rather than a quantity and is carried by last-writer.
_SOURCE_ADDITIVE_KEYS = (
    "jobs",                    # unique-kept rows attributed to this source
    "returned_billed",         # rows the provider returned AND billed
    "unique_kept",             # rows that passed the provider-response schema
    "requests",                # successful physical requests
    "duplicates",              # same provider id seen again within this source
    "cross_source_duplicates",  # same posting already claimed by another source
    "schema_rejected",
    "source_filtered_out",
    # Filled from the pipeline's own dedupe decision point (see ``_dedup``).
    "postings_in",
    "net_new",
    "canonical_duplicates_in_run",
    "historical_previously_seen",
    "missing_identity",
)

#: Carried, not summed: the LAST slice's value wins.
_SOURCE_STATE_KEYS = ("stop_reason", "error_code", "offset_from", "offset_to", "drained")


def _lane_billing_totals(lane_results: Any, kept_fallback: int) -> tuple:
    """(rows the provider returned, rows the provider billed) across all lanes.

    A lane that exposes no billing counter (the free/ATS lanes) contributes its
    kept-row count, which is the best available truth for it. Never reports fewer
    rows than were kept -- that would claim we got rows we did not pay for.
    """
    returned = 0
    billed = 0
    exposed = False
    for r in (lane_results or {}).values():
        attr = getattr(r, "attribution", None) or {}
        raw = int(attr.get("raw_records") or 0)
        consumed = int(attr.get("jobs_quota_consumed") or 0)
        if raw or consumed:
            exposed = True
            returned += raw or len(getattr(r, "jobs", []) or [])
            billed += consumed or raw
        else:
            n = len(getattr(r, "jobs", []) or [])
            returned += n
            billed += n
    if not exposed:
        return max(int(kept_fallback), returned), max(int(kept_fallback), billed)
    return returned, billed


def _merge_source_counts(into: Dict[str, Any], lane_results: Any,
                         dedup_attr: Any = None) -> Dict[str, Any]:
    """Fold one slice's per-source acquisition attribution into a cumulative map.

    Prefers the provider's RICH ``source_attribution.per_source`` (returned/billed,
    unique-kept, duplicates, cross-source duplicates, schema rejects, provider-side
    filtering, stop reason and window cursor) over the shallow three-field
    ``per_source`` block that used to be the only thing to reach the ledger. Both
    are produced by the same adapter; only the shallow one was ever forwarded, so
    per-source novelty and drain state were invisible downstream.
    """
    for r in (lane_results or {}).values():
        attr = getattr(r, "attribution", None) or {}
        shallow = attr.get("per_source") or {}
        rich = ((attr.get("source_attribution") or {}).get("per_source")) or {}
        cursors = ((attr.get("watermark") or {}).get("window_cursors")) or {}
        drained = ((attr.get("watermark") or {}).get("drained_sources")) or {}
        for label in set(shallow) | set(rich):
            incoming = dict(rich.get(label) or {})
            incoming.update({k: v for k, v in (shallow.get(label) or {}).items()
                             if k not in incoming or k == "jobs"})
            cur = cursors.get(label) or {}
            if cur:
                incoming.setdefault("offset_from", cur.get("offset_from"))
                incoming["offset_to"] = cur.get("offset_to", incoming.get("offset_to"))
            if label in drained:
                incoming["drained"] = bool(drained.get(label))
            bucket = into.setdefault(str(label), {})
            for key in _SOURCE_ADDITIVE_KEYS:
                if key in incoming:
                    bucket[key] = int(bucket.get(key, 0) or 0) + int(incoming.get(key) or 0)
            for key in _SOURCE_STATE_KEYS:
                if incoming.get(key) is not None:
                    # The FIRST requested offset of the run is the resume point, so
                    # a later slice must not overwrite it; the ending offset is the
                    # latest one. Everything else is last-writer-wins.
                    if key == "offset_from" and "offset_from" in bucket:
                        continue
                    bucket[key] = incoming[key]
    for label, counts in ((dedup_attr or {}).get("per_source") or {}).items():
        bucket = into.setdefault(str(label), {})
        for key, value in counts.items():
            if key in _SOURCE_ADDITIVE_KEYS:
                bucket[key] = int(bucket.get(key, 0) or 0) + int(value or 0)
    for bucket in into.values():
        returned = int(bucket.get("returned_billed") or 0)
        if returned:
            bucket["novelty_pct"] = round(100.0 * int(bucket.get("net_new") or 0) / returned, 2)
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
        """Split a slice of postings into net-new opportunities and its losses.

        Returns ``(opportunities, stage, attribution)``. ``attribution`` records the
        fate of every posting AT THE DECISION POINT, so the three losses stay
        mutually exclusive and never have to be reconstructed by subtraction. The
        difference matters commercially: a posting rejected as ``duplicate_in_run``
        is provider duplication we paid for twice inside one run, while
        ``previously_seen`` is inventory a PREVIOUS run already processed. Both used
        to be summed into ``historical_duplicates``, which made an acquisition
        overlap problem and a re-billing problem indistinguishable.

        ``per_source`` carries the same partition per ``_acquisition_source`` label,
        so novelty (net-new / returned) is answerable for ATS, LinkedIn, Wellfound
        and Y Combinator separately rather than only for the run as a whole.
        """
        dispo = []
        opportunities: List[Dict[str, Any]] = []
        run_keys: set = set()
        per_source: Dict[str, Dict[str, int]] = {}
        #: (job_id, exit_stage) for every posting that did NOT become an
        #: opportunity, so the yield ledger can attribute the credit we spent on it
        #: to the right exit instead of labelling every dedupe loss "previously seen".
        exits: List[tuple] = []
        totals = {"postings_in": 0, "net_new": 0, "canonical_duplicates_in_run": 0,
                  "historical_previously_seen": 0, "missing_identity": 0}

        def _bump(job: Dict[str, Any], field_name: str) -> None:
            totals["postings_in"] += 1
            totals[field_name] += 1
            label = str(job.get("_acquisition_source") or "unattributed")
            bucket = per_source.setdefault(label, {
                "postings_in": 0, "net_new": 0, "canonical_duplicates_in_run": 0,
                "historical_previously_seen": 0, "missing_identity": 0})
            bucket["postings_in"] += 1
            bucket[field_name] += 1

        for job in postings:
            job_id = str(job.get("posting_id") or job.get("job_id") or "")
            strength, key = posting_identity(job)
            if not key or strength == "none":
                _bump(job, "missing_identity")
                if job_id:
                    exits.append((job_id, "missing_identity"))
                dispo.append((StageOutcome.ERRORED, ReasonCode.MISSING_JOB_ID, None))
                continue
            if key in run_keys:
                _bump(job, "canonical_duplicates_in_run")
                if job_id:
                    exits.append((job_id, "dedup_in_run"))
                dispo.append((StageOutcome.REJECTED, ReasonCode.DUPLICATE_IN_RUN, None))
                continue
            if seen is not None and key in seen:
                _bump(job, "historical_previously_seen")
                if job_id:
                    exits.append((job_id, "dedup_previously_seen"))
                dispo.append((StageOutcome.REJECTED, ReasonCode.PREVIOUSLY_SEEN, None))
                continue
            run_keys.add(key)
            opp = dict(job)
            opp.setdefault("posting_id", key)
            opportunities.append(opp)
            _bump(job, "net_new")
            dispo.append((StageOutcome.PASSED, ReasonCode.OK, None))
        stage = reconcile_stage("acquisition_dedup", "posting", dispo)
        attribution = dict(totals)
        attribution["per_source"] = per_source
        attribution["exits"] = exits
        # The identity this whole split exists to make checkable. It holds by
        # construction (each posting takes exactly one branch above); asserting it
        # here means a future edit that breaks it is caught by the run itself.
        attribution["reconciles"] = (
            totals["postings_in"] == totals["net_new"]
            + totals["canonical_duplicates_in_run"]
            + totals["historical_previously_seen"]
            + totals["missing_identity"])
        return opportunities, stage, attribution

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
                import fantastic_jobs_adapter as _fja_custody
                _fja_custody.set_custody_hook(None)
            except Exception:  # noqa: BLE001
                pass
            # Lift any run that predates the ledger into it BEFORE retention
            # deletes its evidence. Without this the last runs written by the
            # previous build would vanish from the very report the ledger exists
            # to fix. Idempotent, so it is a no-op once every run has an entry.
            backfilled = False
            try:
                backfill_from_artifacts(self.state.root)
                backfilled = True
            except Exception:  # noqa: BLE001
                pass
            # PRUNE ONLY IF THE LIFT SUCCEEDED. Both steps were separately
            # fail-open, which reads as caution and is not: a backfill that raised
            # left runs with no compact record, and the very next statement deleted
            # the artifacts that were the only remaining copy. Retention is a
            # disk-space guarantee, and disk space is the one thing that is not
            # urgent -- the artifacts are already bounded, one extra run's worth
            # costs nothing, and the next run retries the lift. Losing the evidence
            # is not recoverable at any later time.
            # Lift opportunity lists left by runs that predate custody INTO custody,
            # for exactly the same reason the ledger backfill runs here: the file is
            # under run_artifacts and the next statement may delete it. A run whose
            # postings were never finished has its only copy in
            # run_artifacts/<run_id>/enrichment/postings.json.
            protect_runs = {self.ctx.run_id}
            if bool(getattr(config, "PENDING_WORK_ENABLED", True)):
                store = self.state.store_path("pending_work")
                try:
                    self.result_pending_adoption = pending_work.adopt_from_artifacts(
                        self.state.root, store,
                        limit=int(getattr(config, "PENDING_WORK_RESUME_MAX_PER_RUN", 0) or 0) or None,
                        exclude_keys=SuppressionStore(self.state).seen_postings() or set(),
                    )
                except Exception:  # noqa: BLE001 - recovery never masks the outcome
                    pass
                # Age out custody WITHOUT calling it done: expire() archives the
                # payloads and audits the outcome as unresolved. A retention policy
                # must never read as a completed disposition.
                try:
                    pending_work.expire(
                        store,
                        max_age_days=int(getattr(config, "PENDING_WORK_MAX_AGE_DAYS", 0) or 0),
                        run_id=self.ctx.run_id)
                except Exception:  # noqa: BLE001
                    pass
                # A run that still owes work keeps its artifacts: they are the
                # evidence any recovery or reconciliation has to be checked against.
                try:
                    protect_runs |= pending_work.pending_run_ids(store)
                except Exception:  # noqa: BLE001
                    pass
            if backfilled:
                try:
                    self.state.prune(keep=int(retention_keep), max_bytes=RETENTION_MAX_BYTES,
                                     protect=protect_runs)
                except Exception:  # noqa: BLE001
                    pass
            else:
                # print, not logging: this module has no logger, and reaching for
                # one is how run_orchestrator.py:461 shipped a NameError into a live
                # acquisition path (#39).
                print("[retention] skipped: backfill_from_artifacts failed, so "
                      "pruning could delete run evidence that has no ledger entry")
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
        opportunities, dedup_stage, dedup_attr = self._dedup(postings, seen_postings)
        report.add(dedup_stage)
        report.set_unit("opportunities", len(opportunities))
        acquisition_requests = sum(int(r.physical_requests or 0) for r in lane_results.values())
        # BILLING-ACCURATE: the provider bills every row it RETURNED, including the
        # ones its own schema/source filter then dropped, so ``len(postings)`` (the
        # unique-KEPT lane output) understates what we paid for. Prefer the lane's
        # billing counters and fall back to kept only for lanes that expose none.
        provider_returned, provider_billed = _lane_billing_totals(lane_results, len(postings))
        self.ledger.record(
            STAGE_ACQUISITION,
            {
                # Net-new is the stakeholder population; provider duplication is
                # acquisition efficiency, recorded separately.
                "jobs_captured": len(opportunities),
                "net_new_jobs_captured": len(opportunities),
                "provider_jobs_returned": provider_returned,
                "provider_jobs_billed": provider_billed,
                # Decision-point counters: mutually exclusive, never subtracted.
                "historical_duplicates": dedup_attr["historical_previously_seen"],
                "historical_previously_seen_duplicates": dedup_attr["historical_previously_seen"],
                "canonical_duplicates_in_run": dedup_attr["canonical_duplicates_in_run"],
                "postings_missing_identity": dedup_attr["missing_identity"],
                "unique_opportunities": len(opportunities),
            },
            acquisition_entered=bool(acquisition_requests),
            physical_requests=acquisition_requests,
            source_counts=_merge_source_counts({}, lane_results, dedup_attr),
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
            self.ledger.record(
                STAGE_FINAL,
                loss_reasons=reason_census_from_parts(
                    report.to_dict(), getattr(enrichment, "loss_census", {}) or {},
                    delivery.to_dict() if hasattr(delivery, "to_dict") else {},
                    qual_reasons=(getattr(enrichment, "funnel", {}) or {}).get(
                        "qual_reason_counts")),
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

    def _ledger_record_acquisition(self, acq_cum: Dict[str, Any],
                                   unique_opportunities: int) -> None:
        """Persist the CUMULATIVE acquisition counters, and only those.

        Every caller records the same stakeholder definition of captured work --
        ``jobs_captured = net_new_jobs_captured``. The mid-slice checkpoint used to
        write ``jobs_unique_kept`` here and rely on a later snapshot to correct it,
        so a run killed in between left the ledger permanently reporting provider
        volume as delivered work.
        """
        requests = int(acq_cum.get("physical_requests") or 0)
        self.ledger.record(
            STAGE_ACQUISITION,
            {
                # What the stakeholder funnel counts as captured: postings that are
                # NEW work. Provider duplication stays visible below as acquisition
                # efficiency, where it belongs.
                "jobs_captured": acq_cum.get("net_new_jobs_captured"),
                "net_new_jobs_captured": acq_cum.get("net_new_jobs_captured"),
                "provider_jobs_returned": acq_cum.get("jobs_returned_billed"),
                "provider_jobs_billed": acq_cum.get("jobs_quota_consumed"),
                # Three mutually exclusive dedupe exits, each counted where the
                # decision is taken. ``historical_duplicates`` is retained as the
                # historical name of the previously-seen count so an existing report
                # keeps reading the same thing -- only more narrowly, and correctly.
                "historical_duplicates": acq_cum.get("historical_previously_seen_duplicates"),
                "historical_previously_seen_duplicates":
                    acq_cum.get("historical_previously_seen_duplicates"),
                "canonical_duplicates_in_run": acq_cum.get("canonical_duplicates_in_run"),
                "postings_missing_identity": acq_cum.get("postings_missing_identity"),
                "cross_query_duplicates": acq_cum.get("cross_query_duplicates"),
                "cross_source_duplicates": acq_cum.get("cross_source_duplicates"),
                "unique_opportunities": unique_opportunities,
            },
            acquisition_entered=bool(requests),
            physical_requests=requests,
            source_counts={str(src): dict(vals)
                           for src, vals in (acq_cum.get("per_source") or {}).items()},
            acquisition_reconciles=bool(acq_cum.get("dedupe_reconciles", True)),
        )

    def _ledger_record_slice(self, acq_cum: Dict[str, Any], funnel_cum: Dict[str, Any],
                             leads: Sequence[Any], agg: Any) -> None:
        """Persist CUMULATIVE top-up counters to the compact reporting ledger.

        Called at every slice boundary so an interrupted run still reports the work
        it completed. Values mirror the waterfall the run report is built from, so
        the ledger and the heavy artifacts can never disagree.
        """
        self._ledger_record_acquisition(acq_cum, len(leads))
        self.ledger.record(STAGE_ENRICHMENT, _enrichment_ledger_metrics(funnel_cum, leads))
        self.ledger.record(
            STAGE_DELIVERY,
            {"sent_to_airtable": getattr(agg, "created", None),
             "airtable_candidates": getattr(agg, "reviewable_submitted", None),
             "airtable_suppressed": getattr(agg, "skipped_existing", None),
             "airtable_write_failures": getattr(agg, "failed", None),
             "sent_to_instantly": (getattr(agg, "enrolled", None)
                                   if self.ctx.policy.allow_instantly_enrollment else None)},
            delivery_skip_breakdown=(agg.skip_breakdown()
                                     if hasattr(agg, "skip_breakdown") else None),
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
            "physical_requests": 0, "cross_query_duplicates": 0,
            "cross_source_duplicates": 0, "per_source": {},
            # NET-NEW is the population that actually becomes this week's work.
            # A posting the provider returns for the second time is acquisition
            # inefficiency, NOT a review-stage loss, and conflating the two made
            # Brett's funnel read as a 61% "review" drop that no one had lost.
            #
            # These three are MUTUALLY EXCLUSIVE and are counted where the decision
            # is made, never derived by subtraction. ``historical_duplicates`` used
            # to be ``kept - len(opportunities)``, which silently absorbed in-run
            # canonical duplicates and identity-less rows as well: three different
            # problems reported as one number.
            "historical_duplicates": 0,
            "historical_previously_seen_duplicates": 0,
            "canonical_duplicates_in_run": 0,
            "postings_missing_identity": 0,
            "net_new_jobs_captured": 0,
            "dedupe_reconciles": True,
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
        #: The recovered-work cohort, followed through every stage of THIS run. Kept
        #: as posting identities because that is the one key enrichment, delivery and
        #: a later Airtable/Instantly reconciliation all share.
        #: THREE DIFFERENT UNITS, kept apart. Custody stores POSTINGS, approvals are
        #: capped per company x function OPPORTUNITY, and the hiring-manager stage
        #: emits LEADS. The first version called the posting count
        #: `opportunities_resumed`, which is the same conflation that produced every
        #: bad capacity number this week.
        #: Everything THIS run has already handed back, so a batch is never handed
        #: back to itself. Custody deliberately keeps non-terminal work, so without
        #: this the loop re-adopts the same rows on every iteration.
        run_adopted_keys: set = set()
        recovery_cohort: Dict[str, Any] = {
            "postings_resumed": 0, "opportunities_resumed": 0,
            "leads": 0, "with_contact": 0,
            "final_pass": 0, "needs_check": 0, "rejected": 0, "other": 0,
            "delivered_lead_keys": [], "posting_ids": set(),
            "opportunity_keys": set(), "attempted_opportunity_keys": set()}
        stop_reason = ""
        acquisition_error = ""  # set on a FAILED acquisition lane (not "no inventory")

        pending_on = bool(getattr(config, "PENDING_WORK_ENABLED", True))
        pending_dir = self.state.store_path("pending_work") if pending_on else None

        # A zero governor grant is a CLEAN, distinct stop before any acquisition
        # (Gate-E D8) -- never a failed run, never an acquisition attempt.
        #
        # ...but it is a stop for ACQUISITION, and this guard ended the whole run.
        # The 2026-09-06 calibration was a recovery run with 3,595 paid-for postings
        # owed by custody, and it exited here with `acquisition_entered: false` before
        # adopting a single row -- because the FANTASTIC daily allowance was spent.
        # A credit ceiling for the source the run is deliberately not using must not
        # decide whether queued work gets done.
        if gov.run_budget is not None and gov.run_budget <= 0:
            owed_at_start = 0
            if pending_on:
                try:
                    owed_at_start = sum(
                        int(r.get("postings") or 0)
                        for r in (pending_work.summary(pending_dir).get("runs") or [])
                        if str(r.get("run_id")) != self.ctx.run_id)
                except Exception:  # noqa: BLE001 - never a run blocker
                    owed_at_start = 0
            if owed_at_start <= 0:
                stop_reason = "governor_zero_budget"
        if pending_on:
            # Custody must be durable BEFORE the acquisition cursor is. The adapter
            # persists per-source offsets at the end of acquisition -- before the
            # pipeline sees a single posting -- and replays them forward, so a run
            # that died in between advanced past rows nothing had kept. The hook
            # takes custody of the RAW acquired rows (a superset: dedupe has not run
            # yet); the rows dedupe then proves were not new work are released
            # immediately afterwards with a `deduped` outcome.
            import fantastic_jobs_adapter as _fja_custody

            def _hold(rows, _run_id=self.ctx.run_id, _dir=pending_dir):
                return bool(pending_work.record(_dir, _run_id, rows).get("ok"))

            _fja_custody.set_custody_hook(_hold)
        while not stop_reason:
            # `pending_owed` is what tells the controller an acquisition budget must
            # not end a run that still has queued work to do.
            owed_now = 0
            if pending_on:
                try:
                    owed_now = max(0, sum(
                        int(r.get("postings") or 0)
                        for r in (pending_work.summary(pending_dir).get("runs") or [])
                        if str(r.get("run_id")) != self.ctx.run_id) - len(run_adopted_keys))
                except Exception:  # noqa: BLE001
                    owed_now = 0
            decision = controller.decide(
                quota_remaining=last_quota, apollo_circuit_open=last_circuit,
                inventory_exhausted=last_inventory, pending_owed=owed_now)
            if not decision.should_continue:
                stop_reason = decision.stop_reason
                break

            # Slice 1 discovers the fresh edge (head) then backfills (deep); every
            # later slice is DEEP only, so the top-of-feed head query is billed at
            # most once per run and top-up never re-runs the fresh-edge query.
            slice_mode = "head_then_deep" if controller.iterations == 0 else "deep"
            if decision.next_slice <= 0:
                # A ZERO SLICE IS A DELIBERATE INSTRUCTION, not an empty result:
                # the acquisition budget is spent and the queue still owes work, so
                # this iteration drains the queue and contacts no provider. Calling
                # the lanes with a cap of zero would still open a session and could
                # still bill -- the opposite of what the decision means.
                iter_lanes = {}
            else:
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
                acq_cum["cross_source_duplicates"] += int(
                    fl.attribution.get("cross_source_duplicates") or 0)
                acq_cum["last_jobs_quota_remaining"] = last_quota
            # RICH per-source attribution (returned/billed, unique-kept, duplicates,
            # schema rejects, provider filtering, stop reason, window cursor and
            # drain state) for EVERY lane, not just Fantastic's shallow three fields.
            _merge_source_counts(acq_cum["per_source"], iter_lanes)
            acq_cum["jobs_unique_kept"] += kept
            acq_cum["jobs_returned_billed"] += billed
            acq_cum["physical_requests"] += sum(int(r.physical_requests or 0) for r in iter_lanes.values())
            # INVENTORY IS NOT EXHAUSTED WHILE CUSTODY STILL OWES WORK. `kept == 0`
            # means ACQUISITION found nothing new -- which is permanently true on a
            # recovery run, where acquisition is deliberately off. Reading that as
            # "inventory exhausted" stopped the loop after one batch and left 3,595
            # paid-for postings sitting in the store.
            pending_owed = 0
            if pending_on:
                try:
                    # THIS RUN'S OWN entries are not adoptable -- `load` excludes them
                    # by `exclude_run_id`, because they are the work in flight right
                    # now, not a debt from an earlier run. Counting them made
                    # "inventory exhausted" unreachable and the loop ran until the
                    # iteration guard.
                    owed = sum(int(r.get("postings") or 0)
                               for r in (pending_work.summary(pending_dir).get("runs") or [])
                               if str(r.get("run_id")) != self.ctx.run_id)
                    pending_owed = max(0, owed - len(run_adopted_keys))
                except Exception:  # noqa: BLE001 - observability, never a run blocker
                    pending_owed = 0
            last_inventory = (kept == 0 and pending_owed <= 0)
            for j in iter_postings:
                j.setdefault("_acquisition_mode", slice_mode)
            ledger.record_acquired(iter_postings, mode=slice_mode)

            opportunities, dedup_stage, dedup_attr = self._dedup(
                iter_postings, supp.seen_postings())
            report.add(dedup_stage)
            # Postings this run bought that a PREVIOUS run had already processed to
            # completion. They are not reviewable work and must never be counted as
            # a funnel loss; they are the cost of the acquisition overlap. Counted
            # at the decision point, so they can no longer absorb the two OTHER
            # things that exit here: in-run canonical duplicates (the same posting
            # bought twice inside one run) and rows with no usable identity.
            acq_cum["historical_previously_seen_duplicates"] += dedup_attr["historical_previously_seen"]
            acq_cum["historical_duplicates"] = acq_cum["historical_previously_seen_duplicates"]
            acq_cum["canonical_duplicates_in_run"] += dedup_attr["canonical_duplicates_in_run"]
            acq_cum["postings_missing_identity"] += dedup_attr["missing_identity"]
            acq_cum["net_new_jobs_captured"] += len(opportunities)
            acq_cum["dedupe_reconciles"] = bool(
                acq_cum["dedupe_reconciles"] and dedup_attr["reconciles"])
            _merge_source_counts(acq_cum["per_source"], {}, dedup_attr)
            # Ledger: postings that exit at dedupe never become leads. The two exits
            # are recorded distinctly -- ``previously_seen`` is inventory a prior run
            # already worked, ``dedup_in_run`` is a row this run paid for twice.
            for jid, exit_stage in dedup_attr["exits"]:
                ledger.mark(jid, exit_stage=exit_stage,
                            previously_seen=(exit_stage == "dedup_previously_seen"))
            # The custody hook held the RAW rows, because it had to run before the
            # cursor advanced and dedupe had not happened yet. Now that dedupe has
            # ruled, retire the ones that were never new work -- with an explicit
            # `deduped` outcome, so custody shrinks to genuine debt and the audit
            # still shows why each row left.
            if pending_on and dedup_attr["exits"]:
                pending_work.release(
                    pending_dir, [jid for jid, _stage in dedup_attr["exits"]],
                    outcome=pending_work.OUTCOME_DEDUPED, run_id=self.ctx.run_id)

            # Checkpoint acquisition BEFORE enrichment and delivery run. Recording
            # only at the end of the slice would lose these counters whenever a
            # later stage crashed -- and "we acquired 6,206 jobs then died" is
            # precisely the fact the weekly report needs to keep.
            #
            # It records the SAME stakeholder definition the final snapshot uses.
            # It previously wrote ``jobs_captured = jobs_unique_kept``, so a run
            # killed between here and the slice-end snapshot left the ledger
            # permanently reporting provider volume as captured work -- the exact
            # conflation the net-new semantics exist to remove.
            self._ledger_record_acquisition(acq_cum, len(all_leads))

            # CUSTODY of paid-for work, taken BEFORE enrichment can fail.
            #
            # "Not suppressed" is not the same as "will be retried". When Apollo
            # refused on 2026-09-06 nothing was blacklisted and the watermark stayed
            # in flight -- and the 226 postings were still lost, because the window
            # offsets had already advanced past them and no later run reads a run's
            # enrichment workdir. So the deduped opportunities are persisted here,
            # in a store prune cannot reach, and released only when they finish.
            if pending_on:
                acq_cum["pending_work_recorded"] = pending_work.record(
                    pending_dir, self.ctx.run_id, opportunities)
                # Adopt what earlier runs are still owed, once per run. This happens
                # AFTER net_new_jobs_captured is accumulated, so re-entered work is
                # never counted as newly captured -- it was bought and counted once,
                # by the run that acquired it.
                # ONE BATCH PER ITERATION, not one per run. `PENDING_WORK_RESUME_MAX_PER_RUN`
                # bounds a BATCH -- it protects memory and keeps a failure small -- and
                # it used to bound the day, because adoption ran once and then never
                # again however much work custody still owed. A backlog of 3,595
                # postings would have taken two days to drain at 2,000 a run, for no
                # reason but a one-shot guard.
                #
                # `run_adopted_keys` is what stops a batch being handed back to
                # itself: work that did not reach a terminal disposition stays in
                # custody by design, so without it the next iteration would load the
                # same rows for ever.
                if True:
                    resumed, resume_info = pending_work.load(
                        pending_dir,
                        exclude_run_id=self.ctx.run_id,
                        limit=int(getattr(config, "PENDING_WORK_RESUME_MAX_PER_RUN", 0) or 0) or None,
                        exclude_keys=([k for k in (posting_identity(o)[1] for o in opportunities) if k]
                                      + sorted(run_adopted_keys)),
                    )
                    # Anything a previous run already finished is in suppression;
                    # re-entering it would redo settled work.
                    already = supp.seen_postings()
                    resumed = [j for j in resumed
                               if posting_identity(j)[1] not in (already or set())]
                    for j in resumed:
                        key = posting_identity(j)[1]
                        if key:
                            run_adopted_keys.add(key)
                    if resumed:
                        for j in resumed:
                            j.setdefault("_resumed_from_pending", True)
                        opportunities = list(opportunities) + resumed
                        resume_info["adopted"] = len(resumed)
                        # COHORT ATTRIBUTION. Recovered work has to be followable to
                        # the end -- through enrichment, approval and the Approved
                        # Sync that runs in a different service -- or "we reprocessed
                        # the backlog" is a claim with no measurement behind it. The
                        # posting identity is the only key that survives every stage,
                        # so the cohort is a set of them and every later counter is
                        # an intersection with it.
                        for j in resumed:
                            key = posting_identity(j)[1]
                            if key:
                                recovery_cohort["posting_ids"].add(key)
                            jid = str(j.get("job_id") or j.get("posting_id") or "")
                            if jid:
                                recovery_cohort["posting_ids"].add(jid)
                            opp = _opportunity_key(j)
                            if opp:
                                recovery_cohort["opportunity_keys"].add(opp)
                        # POSTINGS, named as such. The opportunity count is the
                        # DISTINCT company x function set, which is smaller and is
                        # the unit approvals are actually capped by.
                        recovery_cohort["postings_resumed"] += len(resumed)
                        recovery_cohort["opportunities_resumed"] = len(
                            recovery_cohort["opportunity_keys"])
                    # ACCUMULATED across iterations. Overwriting reported the LAST
                    # batch, and the last batch of a drained queue adopts nothing --
                    # so a run that handed back 3,595 postings reported 0.
                    prior = acq_cum.get("pending_work_resumed") or {}
                    resume_info["adopted"] = (int(prior.get("adopted") or 0)
                                              + int(resume_info.get("adopted") or 0))
                    resume_info["batches"] = int(prior.get("batches") or 0) + 1
                    acq_cum["pending_work_resumed"] = resume_info

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

            _account_recovery_cohort(recovery_cohort, enrichment.leads, delivery)

            net_new = self._count_net_new_send_safe(enrichment.leads, delivery)
            last_circuit = bool(getattr(enrichment, "enrichment_incomplete", False)
                                and "apollo" in str(getattr(enrichment, "stop_reason", "")))
            self._ledger_mark_outcomes(ledger, enrichment.leads, delivery,
                                       covered_at_start=covered_at_start)

            terminal_ids = enrichment.terminal_posting_ids()
            supp.commit_postings(terminal_ids)
            supp.commit_delivered(getattr(delivery, "delivered_lead_keys", []) or [])
            # Custody ends where suppression begins, on the IDENTICAL id set: a
            # posting stops being owed work exactly when it is genuinely finished,
            # never on a deferred outcome.
            if pending_on:
                acq_cum["pending_work_released"] = pending_work.release(
                    pending_dir, terminal_ids,
                    outcome=pending_work.OUTCOME_TERMINAL, run_id=self.ctx.run_id)
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
            # EVERY additive delivery counter, not a hand-picked subset. The skip
            # breakdown fields were missing here, so the aggregate reported
            # "1,681 submitted, 781 created" with every loss reason at zero: 900
            # rows with no explanation, and ``reviewable_reconciles()`` silently
            # false. Per-slice reports already carried the reasons; only the sum
            # threw them away.
            for _field in ("entered", "reviewable_submitted", "created", "skipped",
                           "skipped_existing", "skipped_already_delivered",
                           "updated_existing", "company_function_suppressed",
                           "account_suppressed", "no_contact",
                           "send_safe_withheld",
                           "person_employer_duplicate", "other_unreconciled",
                           "failed", "enrolled", "instantly_contacts",
                           "final_pass", "needs_check", "other_reviewable"):
                setattr(agg, _field,
                        int(getattr(agg, _field, 0) or 0)
                        + int(getattr(delivery, _field, 0) or 0))
            agg.delivered_lead_keys.extend(getattr(delivery, "delivered_lead_keys", []) or [])
            agg.failed_rows.extend(getattr(delivery, "failed_rows", []) or [])
            # ``withheld_before_submit`` is what ``reconciles()`` checks the entered
            # set against, so the aggregate needs the sum of the slices' values --
            # otherwise the fallback (entered - submitted) is used and the check
            # becomes tautological.
            _slice_detail = dict(getattr(delivery, "detail", {}) or {})
            if "withheld_before_submit" in _slice_detail:
                agg.detail["withheld_before_submit"] = (
                    int(agg.detail.get("withheld_before_submit", 0) or 0)
                    + int(_slice_detail.get("withheld_before_submit") or 0))

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
        self.ledger.record(
            STAGE_FINAL,
            loss_reasons=reason_census_from_parts(
                report.to_dict(), loss_cum, agg.to_dict(),
                qual_reasons=funnel_cum.get("qual_reason_counts")),
        )

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
        # RECOVERED-WORK COHORT. Serialisable form: the identity SET becomes a count
        # (it can run to thousands and the ids are already in the pending store), and
        # the delivered lead keys are kept in full because they are the join a later
        # Airtable / Approved Sync reconciliation needs. A count cannot be joined.
        _sets = ("posting_ids", "opportunity_keys", "attempted_opportunity_keys")
        recovery_block = {k: v for k, v in recovery_cohort.items() if k not in _sets}
        recovery_block["cohort_postings"] = len(recovery_cohort.get("posting_ids") or ())
        recovery_block["delivered"] = len(recovery_block.get("delivered_lead_keys") or [])
        # ATTEMPTED vs UNATTEMPTED, kept apart. A conversion rate divides by the
        # opportunities the stage actually produced an outcome for; the remainder is
        # reported beside it as work with NO reconciled outcome -- which is what it
        # is. Calling it "never attempted" would assert execution evidence this run
        # may not carry.
        attempted = len(recovery_cohort.get("attempted_opportunity_keys") or ())
        resumed_opps = int(recovery_block.get("opportunities_resumed") or 0)
        recovery_block["opportunities_attempted"] = attempted
        recovery_block["opportunities_without_reconciled_outcome"] = max(
            0, resumed_opps - attempted)
        recovery_block["opportunity_to_contact_rate"] = (
            round(recovery_block["with_contact"] / attempted, 4) if attempted else None)
        recovery_block["rate_denominator"] = (
            "opportunities_attempted" if attempted else "")
        acquisition_block = {
            "iterations": controller.iterations,
            "recovery_cohort": recovery_block,
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
            "pending_work": (pending_work.summary(pending_dir) if pending_on else
                             {"pending_postings": 0, "pending_runs": 0, "runs": []}),
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
