"""Real adapter composition -- wires the repository's existing domain logic and
service clients into the orchestrator. No logic is re-implemented.

Acquisition  : free_job_sources.build_adapters (free feeds),
               retrieval_measurement.drivers.run_jsearch_lane (real JSearch
               scraper via a transport seam), LaneManager.run_ats (real ATS
               boundary + fetch_board_jobs).
Enrichment   : qualification_pipeline.run_precontact_qualification (JobGate +
               RoleGate) then hiring_manager.run_hiring_manager_identification
               (company identity, Apollo, Hunter, contact gates, FINAL_PASS).
Delivery     : airtable_client.push_leads and instantly_client.enroll_approved_leads,
               behind separate explicit flags, disabled by default.

Offline hermeticity: every service client (apollo_client, hunter_client,
airtable_client, instantly_client) routes HTTP through the single seam
``request_with_retry``. ``seam_fake`` patches that name in each module for the
duration of a block, so offline modes and tests drive the REAL adapter code with
fixture responses and make zero network calls. The seam is restored on exit.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import config
from orchestrator.enrichment import EnrichmentReport, Lead
from orchestrator.lanes import LaneManager, LaneResult
from orchestrator.reasons import Disposition, ReasonCode, StageOutcome
from orchestrator.waterfall import StageResult, reconcile_stage

#: Client modules whose ``request_with_retry`` seam is faked offline.
SEAM_MODULES = ("apollo_client", "hunter_client", "airtable_client", "instantly_client")


@contextlib.contextmanager
def _output_dirs_under(qualification_dir: Path, enrichment_dir: Path):
    """Temporarily point the legacy qualification/enrichment output dirs under the
    run root, restoring the previous values in ``finally`` so no legacy config is
    permanently changed and no state leaks between runs or tests."""
    import config
    names = ("FILTERED_OUTPUT_DIR", "STEP3_OUTPUT_DIR")
    saved = {n: getattr(config, n, None) for n in names}
    config.FILTERED_OUTPUT_DIR = str(qualification_dir)
    config.STEP3_OUTPUT_DIR = str(enrichment_dir)
    try:
        yield
    finally:
        for n, v in saved.items():
            if v is None:
                if hasattr(config, n):
                    delattr(config, n)
            else:
                setattr(config, n, v)


class FakeResponse:
    """Minimal requests.Response stand-in for the faked seam."""

    def __init__(self, payload: Any, status: int = 200, url: str = "") -> None:
        self._payload = payload
        self.status_code = status
        self.url = url
        self.headers: Dict[str, str] = {}

        class _Req:
            method = "POST"

        self.request = _Req()

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def json(self) -> Any:
        return self._payload


@contextlib.contextmanager
def seam_fake(handler: Callable[..., Any], modules: Sequence[str] = SEAM_MODULES):
    """Patch ``request_with_retry`` in each named client module to ``handler``.

    ``handler(method, url, **kwargs) -> FakeResponse``. Restores the originals on
    exit. This is the single structural guarantee that offline real-adapter runs
    make no network call.
    """
    import importlib

    originals: Dict[str, Any] = {}
    mods: Dict[str, Any] = {}
    for name in modules:
        try:
            mod = importlib.import_module(name)
        except Exception:  # pragma: no cover - module optional in some checkouts
            continue
        if hasattr(mod, "request_with_retry"):
            originals[name] = mod.request_with_retry
            mods[name] = mod
            mod.request_with_retry = handler
    try:
        yield
    finally:
        for name, mod in mods.items():
            mod.request_with_retry = originals[name]


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


def real_free_feeds_runner(sources: Sequence[str], fetcher):
    """Lane runner using the REAL free-feed adapters through the budgeted seam."""
    from free_job_sources import build_adapters

    def runner(manager: LaneManager) -> LaneResult:
        def producer(seam):
            # ``seam`` already wraps the injected fetcher and counts every request
            # against the budget; the real adapters fetch straight through it.
            jobs: List[Dict[str, Any]] = []
            for adapter in build_adapters(list(sources)):
                jobs.extend(adapter.fetch(seam).jobs)
            return jobs

        return manager.run_simple("free_feeds", producer, fetcher, source="free_feeds")

    return runner


def real_external_batch_runner(csv_path: str):
    """Lane runner for an already-acquired external Fantastic batch (Apify CSV).

    The rows were bought outside this pipeline, so the lane issues ZERO provider
    requests and consumes ZERO Fantastic job credits. Normalization goes through
    ``fantastic_jobs_adapter.map_record`` and the same
    ``multi_source_acquisition._classify`` step the paid Fantastic lane uses, so
    every downstream gate sees an identical canonical record.
    """
    from external_batch_adapter import normalize_batch

    def runner(manager: LaneManager) -> LaneResult:
        errors: List[str] = []
        jobs: List[Dict[str, Any]] = []
        stats: Dict[str, Any] = {}
        status = "complete"
        try:
            jobs, stats = normalize_batch(csv_path)
            if not jobs:
                status = "failed"
                errors.append(f"external batch produced no canonical jobs: {csv_path}")
        except Exception as exc:  # noqa: BLE001 - a lane never erases another
            status = "failed"
            errors.append(f"{type(exc).__name__}: {exc}")
        return LaneResult(
            lane="external_batch", status=status, jobs=jobs, errors=errors,
            # No HTTP was performed: the batch is a local file.
            physical_requests=0,
            attribution={
                "source": "external_apify_fantastic",
                "batch_file": Path(csv_path).name,
                "records": len(jobs),
                "raw_rows": int(stats.get("raw_rows", 0) or 0),
                "role_classified": int(stats.get("classified", 0) or 0),
                "row_errors": int(stats.get("row_errors", 0) or 0),
                "rejected_by_reason": dict(stats.get("rejected_by_reason") or {}),
                # Explicit, auditable proof that this lane bought nothing.
                "requests": 0,
                "jobs_quota_consumed": 0,
                "raw_records": int(stats.get("raw_rows", 0) or 0),
            },
        )

    return runner


def real_fantastic_runner():
    """Lane runner for the Fantastic.jobs Direct API acquisition source.

    Fantastic owns its HTTP transport, pagination and provider quota (the
    ``x-api-*-remaining`` headers plus the ``FANTASTIC_JOBS_*`` reserves), so it
    does not draw on the RequestBudget seam that the ats/jsearch/free lanes use.
    A source-level failure is contained: the lane returns ``failed``/``partial``
    and never crashes the run or erases another lane. When
    ``FANTASTIC_JOBS_ENABLED`` is falsey the adapter issues no request and the
    lane completes with zero jobs.
    """
    from fantastic_jobs_adapter import run_fantastic_jobs_acquisition

    def runner(manager: LaneManager) -> LaneResult:
        errors: List[str] = []
        jobs: List[Dict[str, Any]] = []
        status = "complete"
        metadata: Dict[str, Any] = {}
        classified = 0
        raw_records = 0
        try:
            result = run_fantastic_jobs_acquisition()
            jobs = list(result.jobs)
            errors = list(result.errors)
            metadata = dict(result.metadata or {})
            raw_records = int(getattr(result, "raw_records", 0) or 0)
            if not result.success:
                status = "partial" if jobs else "failed"
            # Classify raw Fantastic postings into the target-role portfolio so the
            # RoleGate (a match-VERIFIER that reads job["_matched_role"]) has a
            # target to assess. Fantastic jobs arrive without a target role, so
            # without this every posting is UNVERIFIED_ROLE_CLASSIFICATION and
            # non-target titles (e.g. "Kitchen Manager") are never rejected. This
            # mirrors the multi_source path's classify-then-verify step.
            from multi_source_acquisition import _classify
            for job in jobs:
                try:
                    _classify(job)
                    classified += 1
                except Exception:  # noqa: BLE001 - classification never aborts a lane
                    continue
        except Exception as exc:  # noqa: BLE001 - a lane never erases another
            status = "failed"
            errors.append(f"{type(exc).__name__}: {exc}")
        reqs = int(metadata.get("requests_attempted", 0) or 0)
        return LaneResult(
            lane="fantastic", status=status, jobs=jobs, errors=errors,
            physical_requests=reqs,
            attribution={
                "source": "fantastic_jobs",
                "records": len(jobs),
                "role_classified": classified,
                "requests": reqs,
                "stop_reason": metadata.get("stop_reason", ""),
                "jobs_quota_remaining": metadata.get("jobs_quota_remaining"),
                "requests_quota_remaining": metadata.get("requests_quota_remaining"),
                # BILLING-ACCURATE counters (Gate A/D): the provider bills every RETURNED
                # row, not the unique-kept ``records``. The top-up controller, the
                # monthly governor ledger and the yield ledger must consume these.
                "jobs_quota_consumed": int(metadata.get("jobs_quota_consumed", 0) or 0),
                "raw_records": raw_records or int(metadata.get("raw_records", 0) or 0),
                "cross_query_duplicates": int(metadata.get("cross_query_duplicates", 0) or 0),
                # The SAME posting reaching us from a second source. Forwarded so
                # the weekly report can separate provider duplication from work we
                # had already done -- they are different problems with different
                # fixes, and both used to arrive as one undifferentiated "dedupe".
                "cross_source_duplicates": int(metadata.get("cross_source_duplicates", 0) or 0),
                # The SHALLOW block (jobs / returned_billed / requests). Kept as-is
                # for every existing consumer.
                "per_source": dict(metadata.get("per_source") or {}),
                # ...and the RICH one the adapter has always built beside it and
                # nothing ever forwarded: unique-kept, intra/cross-source
                # duplicates, schema rejects, provider-side filtering, per-source
                # stop reason. Without it, "Wellfound returned 160 rows" could not
                # be turned into "and how many of them were new, and why the rest
                # were not" anywhere downstream.
                "source_attribution": {
                    k: (dict(v) if isinstance(v, dict) else v)
                    for k, v in (metadata.get("source_attribution") or {}).items()
                },
                "provider_filters": dict(metadata.get("provider_filters") or {}),
                "next_billing_date": metadata.get("next_billing_date"),
                "watermark": dict(metadata.get("watermark") or {}),
            },
        )

    return runner


def _live_jsearch_request(method: str, url: str, **kwargs: Any):
    """The real JSearch transport inner: routes to http_utils.request_with_retry,
    preserving its retry/quota behaviour. Imported lazily so offline never loads it."""
    from http_utils import request_with_retry
    return request_with_retry(method, url, **kwargs)


def build_jsearch_transport(*, live: bool, budget=None, recorded=None):
    """Compose the JSearch transport.

    * offline (``live=False``): a replay transport over ``recorded`` payloads --
      it never calls the network, so offline modes stay zero-network;
    * live (``live=True``): the real ``request_with_retry`` transport, wrapped so
      each physical request reserves against the RequestBudget under the
      ``jsearch`` lane (preserving the budget seam and the reserved capacity),
      while RapidAPI quota checks and query planning stay entirely inside the
      existing scraper.
    """
    from retrieval_measurement.instrument import JSearchTransport
    if not live:
        return JSearchTransport(recorded=dict(recorded or {}))
    inner = _live_jsearch_request
    if budget is not None:
        def budgeted_inner(method: str, url: str, **kwargs: Any):
            previous = (budget.lane, budget.source)
            budget.lane, budget.source = "jsearch", "jsearch"
            try:
                budget.reserve(url)  # counts against jsearch lane / reservation
            finally:
                budget.lane, budget.source = previous
            return _live_jsearch_request(method, url, **kwargs)
        inner = budgeted_inner
    return JSearchTransport(inner=inner)


def real_jsearch_runner(*, output_dir: str, max_queries: Optional[int], registry,
                        live: bool, recorded=None):
    """Lane runner using the REAL JSearch scraper.

    ``live`` selects the real transport; offline uses replay. A RapidAPI
    preflight/quota failure is contained (the lane returns ``failed``), never
    crashing the run or erasing another lane.
    """
    from retrieval_measurement.drivers import run_jsearch_lane

    def runner(manager: LaneManager) -> LaneResult:
        transport = build_jsearch_transport(live=live, budget=manager.budget, recorded=recorded)
        errors: List[str] = []
        jobs: List[Dict[str, Any]] = []
        status = "complete"
        try:
            outputs = run_jsearch_lane(transport, output_dir=output_dir,
                                       max_queries=max_queries, registry=registry)
            for out in outputs:
                jobs.extend(out.result.jobs)
                errors.extend(out.result.errors)
            if errors:
                status = "partial"
        except Exception as exc:  # noqa: BLE001 - quota/preflight stops cleanly
            status = "failed"
            errors.append(f"{type(exc).__name__}: {exc}")
        reqs = len(getattr(transport, "requests", []))
        return LaneResult(lane="jsearch", status=status, jobs=jobs, errors=errors,
                          physical_requests=reqs,
                          attribution={"jsearch_transport": "live" if live else "replay",
                                       "records": len(jobs), "requests": reqs})

    return runner


def real_ats_runner(boards, fetcher, *, checkpoint_dir, scheduler_config, detail_budgets=None):
    def runner(manager: LaneManager) -> LaneResult:
        return manager.run_ats(boards, fetcher, checkpoint_dir=checkpoint_dir,
                               scheduler_config=scheduler_config,
                               detail_budgets=detail_budgets or {})
    return runner


# --------------------------------------------------------------------------
# Enrichment: real qualification gates + real hiring-manager (Apollo/Hunter)
# --------------------------------------------------------------------------

#: hiring_manager._final_state -> our Disposition. The vocabularies already match.
_FINAL_STATE = {
    "FINAL_PASS": Disposition.FINAL_PASS,
    "NEEDS_CHECK": Disposition.NEEDS_CHECK,
    "UNVERIFIED": Disposition.UNVERIFIED,
    "REJECT": Disposition.REJECT,
    "REROUTE": Disposition.REROUTE,
}


class RealEnrichmentStage:
    """Runs the repository's real pre-contact gates and hiring-manager pipeline.

    Produces an ``EnrichmentReport`` so the orchestrator consumes it exactly as
    it consumes the fake engine. Quality rules are the repository's own; nothing
    here weakens a gate.
    """

    def __init__(self, *, target_final_pass: Optional[int] = None, workdir: Optional[str] = None):
        self.target_final_pass = target_final_pass
        self.workdir = Path(workdir or tempfile.mkdtemp())
        self._run_budgets_initialized = False
        #: CollapseResult from the last run when company collapse is enabled.
        self.collapse = None

    def run(self, opportunities: List[Dict[str, Any]],
            *, exclude_company_keys=None,
            exclude_company_function_keys=None) -> EnrichmentReport:
        from qualification_pipeline import run_precontact_qualification
        import hiring_manager

        # Stable, input-addressed batches preserve evidence and resume identity.
        # Previously every batch overwrote postings and Step 3 outputs, and the
        # company-only checkpoint could return another opening's contact.
        encoded_rows = sorted(json.dumps(row, sort_keys=True, default=str) for row in opportunities)
        batch_key = hashlib.sha256(json.dumps(encoded_rows).encode()).hexdigest()
        qual_dir = self.workdir / "qualification" / batch_key
        enr_dir = self.workdir / "enrichment" / batch_key
        qual_dir.mkdir(parents=True, exist_ok=True)
        enr_dir.mkdir(parents=True, exist_ok=True)
        raw = qual_dir / "postings.json"
        self._write_postings(raw, opportunities)
        # Keep the historical reader/recovery path as the distinct input union,
        # never merely the last batch. This records inputs, not new acquisitions.
        census_path = self.workdir / "postings.json"
        previous = json.loads(census_path.read_text(encoding="utf-8"))["jobs"] if census_path.exists() else []
        from retrieval_measurement.accounting import posting_identity
        by_identity = {}
        for row in previous + opportunities:
            strength, key = posting_identity(row)
            identity = (strength, key) if key and strength != "none" else (
                "payload", json.dumps(row, sort_keys=True, default=str))
            by_identity[identity] = row
        self._write_postings(census_path, list(by_identity.values()))

        # Keep every real-module write UNDER the run root. The legacy config
        # output dirs are overridden only for the duration of this block and
        # restored in the finally, so no legacy configuration is changed and no
        # write lands outside orchestrator_v2.
        with _output_dirs_under(qual_dir, enr_dir):
            # 1) Real JobGate + RoleGate (offline: no source fetch).
            qual = run_precontact_qualification(str(raw), output_dir=str(qual_dir),
                                                fetch_sources=False)
            qual_path = qual.output_path
            # 1b) Optional company-level opportunity collapse. Runs BETWEEN the
            #     gates and the first Apollo call, so only postings that already
            #     qualified compete to represent their employer, and an employer
            #     can consume at most one person enrichment. Default OFF.
            self.collapse = None
            if config.COMPANY_OPPORTUNITY_COLLAPSE_ENABLED:
                qual_path = self._collapse_qualified(
                    qual_path, qual_dir,
                    suppressed_function_keys=exclude_company_function_keys)
            # 2) Real company identity + Apollo + Hunter + contact gates + FINAL_PASS.
            #    No company-wide exclusion is invented; posting-level dedup upstream
            #    already prevents re-processing the same opportunity.
            step3 = hiring_manager.run_hiring_manager_identification(
                qual_path, target_final_pass_leads=self.target_final_pass,
                exclude_company_keys=exclude_company_keys,
                exclude_company_function_keys=exclude_company_function_keys,
                reset_run_budgets=not self._run_budgets_initialized)
            self._run_budgets_initialized = True

        leads = self._load_leads(step3.output_path)
        report = self._to_report(qual, step3, leads)
        if self.collapse is not None:
            report.funnel["company_collapse"] = dict(self.collapse.metrics)
        return report

    @staticmethod
    def _write_postings(path: Path, rows: List[Dict[str, Any]]) -> None:
        # Failure propagates before paid work starts; never erase older custody.
        temp = path.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({"jobs": rows}, stream, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    def _collapse_qualified(self, qual_path: str, qual_dir: Path,
                            *, suppressed_function_keys=None) -> str:
        """Rewrite the qualified-jobs file down to one opportunity per employer.

        Writes a sibling file so the pre-collapse qualification output stays on
        disk as evidence. Any failure returns the ORIGINAL path unchanged: a
        collapse defect must never silently drop qualified inventory.
        """
        from company_opportunity_collapse import collapse_company_opportunities
        try:
            from airtable_client import company_function_keys_for_job
            payload = json.loads(Path(qual_path).read_text(encoding="utf-8"))
            jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
            result = collapse_company_opportunities(
                jobs,
                suppressed_function_keys=suppressed_function_keys,
                function_keys_for_job=company_function_keys_for_job)
        except Exception as exc:  # noqa: BLE001 - never drop inventory on a defect
            print(f"[company-collapse] skipped ({type(exc).__name__}: {exc}); "
                  f"proceeding with all {qual_path} opportunities")
            return qual_path
        self.collapse = result
        collapsed_path = qual_dir / "jobs_company_collapsed.json"
        collapsed_path.write_text(json.dumps(
            {**{k: v for k, v in payload.items() if k != "jobs"},
             "jobs": result.representatives,
             "company_collapse": result.metrics}, indent=2), encoding="utf-8")
        (qual_dir / "jobs_company_collapse_withheld.json").write_text(json.dumps({
            "company_collapse": result.metrics,
            "withheld": [{"job_id": j.get("job_id"), "employer_name": j.get("employer_name"),
                          "job_title": j.get("job_title"), "reason": reason}
                         for j, reason in result.withheld],
        }, indent=2), encoding="utf-8")
        print(f"[company-collapse] {result.metrics['input_postings']} qualified postings -> "
              f"{result.metrics['company_opportunities_after_collapse']} company opportunities "
              f"({result.metrics['jobs_suppressed_by_company_collapse']} siblings suppressed, "
              f"{result.metrics['identity_unresolved_withheld']} withheld unresolved)")
        return str(collapsed_path)

    @staticmethod
    def _load_leads(path: str) -> List[Dict[str, Any]]:
        p = Path(path)
        if not p.is_file():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data.get("leads") or data.get("jobs") or []
        return [r for r in rows if isinstance(r, dict)]

    #: Persisted _final_state -> orchestrator Disposition (vocabularies match).
    _STATE_DISPOSITION = {
        "FINAL_PASS": Disposition.FINAL_PASS,
        "NEEDS_CHECK": Disposition.NEEDS_CHECK,
        "UNVERIFIED": Disposition.UNVERIFIED,
        "REROUTE": Disposition.REROUTE,
        "REJECT": Disposition.REJECT,
    }
    #: Default reason per disposition when the persisted row carries none our enum
    #: recognises (the real pipeline's reason vocabulary is broader).
    _DEFAULT_REASON = {
        Disposition.FINAL_PASS: ReasonCode.OK,
        Disposition.NEEDS_CHECK: ReasonCode.HIRING_MANAGER_NOT_FOUND,
        Disposition.UNVERIFIED: ReasonCode.EMAIL_UNVERIFIED,
        Disposition.REROUTE: ReasonCode.COMPANY_UNRESOLVED,
        Disposition.REJECT: ReasonCode.NOT_ICP,
    }
    #: Disposition -> stage outcome for the reconciled hiring_manager boundary.
    _STAGE_OUTCOME = {
        Disposition.FINAL_PASS: StageOutcome.PASSED,
        Disposition.NEEDS_CHECK: StageOutcome.DEFERRED,
        Disposition.UNVERIFIED: StageOutcome.DEFERRED,
        Disposition.REROUTE: StageOutcome.REJECTED,
        Disposition.REJECT: StageOutcome.REJECTED,
    }

    @classmethod
    def _reason_for(cls, row: Dict[str, Any], disp: Disposition) -> ReasonCode:
        raw = str(row.get("_final_primary_reason") or row.get("_step3_reason") or "").strip()
        try:
            return ReasonCode(raw)
        except Exception:  # noqa: BLE001 - broad real reason vocabulary -> our default
            return cls._DEFAULT_REASON[disp]

    @staticmethod
    def _related_postings(row: Dict[str, Any], primary: str) -> List[str]:
        out: List[str] = []
        for pid in (row.get("related_job_ids") or []):
            s = str(pid or "")
            if s and s != primary:
                out.append(s)
        return out

    def _real_lead(self, row: Dict[str, Any], disp: Disposition) -> Lead:
        """Reconstruct a genuine Lead from a persisted row, preserving the ENTIRE
        row under contact['_airtable_row'] so review-staging writes the real
        NEEDS_CHECK/UNVERIFIED contact (Defect A), not an empty placeholder."""
        pid = str(row.get("job_id") or row.get("canonical_job_id") or row.get("lead_key") or "")
        # The orchestration wrapper carries canonical identity only.  Outbound
        # display fields remain inside the full row and are never overwritten by
        # Apollo/raw-name reconstruction here.
        company_name = (row.get("canonical_company_name")
                        or row.get("canonical_employer_name")
                        or row.get("employer_name") or "")
        return Lead(
            posting_id=pid,
            company={"name": company_name},
            contact={
                "email": row.get("hiring_manager_email", "") or "",
                "name": row.get("hiring_manager_name", "") or "",
                "_airtable_row": row,   # full real row -> RealDelivery._rows forwards it
            },
            disposition=disp,
            primary_reason=self._reason_for(row, disp),
            email_status=str(row.get("apollo_email_status") or row.get("hunter_email_status") or ""),
            contact_key=str(row.get("lead_key") or pid or ""),
            related_posting_ids=self._related_postings(row, pid),
        )

    def _to_report(self, qual, step3, lead_rows) -> EnrichmentReport:
        # Authoritative per-state counts from Step3Result (the census must match).
        authoritative = {
            Disposition.FINAL_PASS: int(step3.final_pass_leads),
            Disposition.NEEDS_CHECK: int(step3.needs_check_leads),
            Disposition.UNVERIFIED: int(step3.unverified_leads),
            Disposition.REROUTE: int(step3.reroute_leads),
            Disposition.REJECT: int(step3.rejected_leads),
        }
        # Build REAL Leads from the persisted rows for EVERY state, preserving all
        # data. Group by disposition so we can reconcile to the authoritative count.
        by_disp: Dict[Disposition, List[Lead]] = {d: [] for d in authoritative}
        for row in lead_rows:
            disp = self._STATE_DISPOSITION.get(str(row.get("_final_state") or ""))
            if disp is None:
                continue
            by_disp[disp].append(self._real_lead(row, disp))

        leads: List[Lead] = []
        for disp, target in authoritative.items():
            real = by_disp[disp]
            # Prefer real rows; trim if somehow over-count.
            leads.extend(real[:target])
            # Defensive pad ONLY if the persisted rows are sparser than the
            # authoritative count, so the disposition census stays exact. Padding
            # rows carry no contact so they can never be mistaken for deliverable.
            for i in range(max(0, target - len(real))):
                leads.append(Lead(f"{disp.value}-pad-{i}", {"name": ""}, {},
                                  disp, self._DEFAULT_REASON[disp], contact_key=""))

        dispo = [(self._STAGE_OUTCOME[l.disposition], l.primary_reason, None) for l in leads]
        stage = reconcile_stage("hiring_manager", "lead", dispo)
        # Cross-stage funnel for operator observability (Defect G). Every value is
        # already computed by the real qualification + hiring-manager stages; we
        # only surface it so the operator can read the funnel from Railway logs.
        qual_reasons = {k[len("reason__"):]: v
                        for k, v in (getattr(qual, "stats", {}) or {}).items()
                        if str(k).startswith("reason__")}
        # "Qualified opportunities" as a stakeholder means it: job/role policy
        # passed AND the company/account ICP decision passed AND the opportunity
        # was actually able to enter contact discovery. Emitted by
        # hiring_manager at the people-search decision point.
        #
        # ``target_role_eligible`` is deliberately NOT this number. It is the
        # pre-contact role/source gate, which passed 92.6% of postings on the
        # 2026-09-04 control run -- reporting it as "qualified" would show a
        # stage that appears to do nothing while the real ICP decision, which
        # rejected 606 opportunities that day, stayed invisible.
        step3_stats = getattr(step3, "stats", {}) or {}
        entered = step3_stats.get("contact_discovery_entered")
        funnel = {
            "qualification_input": int(getattr(qual, "input_jobs", 0) or 0),
            "target_role_eligible": int(getattr(qual, "contact_eligible_jobs", 0) or 0),
            "qual_rejected": int(getattr(qual, "rejected_jobs", 0) or 0),
            "qual_needs_check": int(getattr(qual, "needs_check_jobs", 0) or 0),
            "qual_unverified": int(getattr(qual, "unverified_jobs", 0) or 0),
            "companies_considered": int(getattr(step3, "companies_considered", 0) or 0),
            "icp_eligible_companies": int(getattr(step3, "eligible_companies", 0) or 0),
            "icp_rejected_companies": int(getattr(step3, "company_criteria_excluded_companies", 0) or 0),
            "hiring_managers_found": int(getattr(step3, "hiring_manager_found", 0) or 0),
            "hiring_managers_not_found": int(getattr(step3, "hiring_manager_not_found", 0) or 0),
            "contactable_hiring_managers": int(getattr(step3, "contactable_hiring_managers", 0) or 0),
            "final_pass": authoritative[Disposition.FINAL_PASS],
            "needs_check": authoritative[Disposition.NEEDS_CHECK],
            "unverified": authoritative[Disposition.UNVERIFIED],
            "reroute": authoritative[Disposition.REROUTE],
            "rejected": authoritative[Disposition.REJECT],
            "qual_reason_counts": qual_reasons,
            # Non-PII HM + multi-function observability, surfaced to the operator
            # run summary so coverage-by-bucket and multi-function handling are
            # readable straight from Railway logs (see hm_observability.py).
            "hm_observability": dict(getattr(step3, "hm_observability", {}) or {}),
        }
        # Absent means unmeasured, never zero: a Step3Result that does not carry
        # the counter (a stub, or a pre-2026-09-05 run) must not be reported as
        # "0 qualified opportunities".
        if entered is not None:
            funnel["contact_discovery_entered"] = int(entered)
        return EnrichmentReport(
            leads=leads,
            stages=[stage],
            loss_census={
                "hiring_manager_not_found": int(step3.hiring_manager_not_found),
                "reroute": authoritative[Disposition.REROUTE],
                "rejected": authoritative[Disposition.REJECT],
                "needs_check": authoritative[Disposition.NEEDS_CHECK],
                "unverified": authoritative[Disposition.UNVERIFIED],
            },
            funnel=funnel,
            stop_reason=str(getattr(step3, "stop_reason", "") or ""),
            enrichment_incomplete=(
                str(getattr(step3, "stop_reason", "") or "") in _INCOMPLETE_ENRICHMENT_STOP_REASONS
                or bool((getattr(step3, "stats", {}) or {}).get("apollo_circuit_open"))
            ),
        )


# --------------------------------------------------------------------------
# Delivery: real Airtable + Instantly, behind explicit flags, default disabled
# --------------------------------------------------------------------------


#: Dispositions written to Airtable for manual review -- the repository's own
#: AIRTABLE_REVIEW_STATES ({FINAL_PASS, UNVERIFIED, NEEDS_CHECK}); REJECT and
#: REROUTE are terminal and never written.
_REVIEWABLE = (Disposition.FINAL_PASS, Disposition.NEEDS_CHECK, Disposition.UNVERIFIED)

#: Enrichment stop reasons that mean the loop stopped BEFORE every eligible
#: company was processed -- a provider-capacity (Apollo) or runtime-budget stop.
#: The run must then report INCOMPLETE (never a false success) while completed
#: leads still deliver and the remaining companies stay resumable.
_INCOMPLETE_ENRICHMENT_STOP_REASONS = frozenset({
    "apollo_circuit_open", "enrichment_runtime_budget_reached",
})


@dataclass
class RealDeliveryReport:
    mode: str = "dry_no_write"
    entered: int = 0
    reviewable_submitted: int = 0
    created: int = 0
    skipped: int = 0
    skipped_existing: int = 0        # idempotency duplicates (Airtable server-side)
    skipped_already_delivered: int = 0  # local cross-run lead_key idempotency
    # MUTUALLY EXCLUSIVE breakdown of every submitted-but-not-created row (Gate D):
    #   reviewable_submitted - created - failed
    #     == skipped_existing + updated_existing + company_function_suppressed
    #        + account_suppressed + no_contact + send_safe_withheld + other
    updated_existing: int = 0
    company_function_suppressed: int = 0
    account_suppressed: int = 0
    no_contact: int = 0
    #: Withheld by AIRTABLE_WRITE_SEND_SAFE_ONLY: the row was NOT written at all,
    #: in either status. It is preserved in the run's enrichment artifacts.
    send_safe_withheld: int = 0
    person_employer_duplicate: int = 0
    other_unreconciled: int = 0
    failed: int = 0
    enrolled: int = 0
    instantly_contacts: int = 0      # MUST be 0 in review-staging
    final_pass: int = 0
    needs_check: int = 0
    other_reviewable: int = 0
    failed_rows: List[Dict[str, Any]] = field(default_factory=list)
    delivered_lead_keys: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    def reconciles(self) -> bool:
        # NOT tautological (Gate D): ``skipped`` is derived, so check the entered set
        # against INDEPENDENT counters: everything entered is either submitted, or
        # withheld before submission (already delivered / non-reviewable disposition).
        #
        # ...which the old default quietly defeated. Falling back to
        # ``entered - reviewable_submitted`` makes the comparison
        # ``entered == reviewable_submitted + entered - reviewable_submitted``, i.e.
        # true for every input -- exactly the tautology the docstring above denies.
        # The 2026-09-04 production record has no ``withheld_before_submit`` and
        # reported ``airtable_reconciles: true`` on 2,410 entered against 1,681
        # submitted, which nothing had checked.
        #
        # An absent count is not a passing check. A run that entered nothing has
        # nothing to verify and passes; anything else must have recorded the number.
        # `dry_no_write` never calls the writer, so there is no submission identity
        # to verify -- the same exemption `reviewable_reconciles` already makes.
        if self.mode == "dry_no_write":
            return True
        if not self.entered and not self.reviewable_submitted:
            return True
        withheld = self.detail.get("withheld_before_submit")
        if withheld is None:
            return False
        return self.entered == self.reviewable_submitted + int(withheld)

    def skip_breakdown(self) -> Dict[str, int]:
        """Mutually exclusive partition of submitted-but-not-created rows.
        ``person_employer_duplicate`` is reported alongside but is NOT part of the
        submitted identity: collapse losers are withheld BEFORE submission (like
        already-delivered rows), so they are reconciled against ``entered``."""
        return {
            "skipped_existing": self.skipped_existing,
            "updated_existing": self.updated_existing,
            "company_function_suppressed": self.company_function_suppressed,
            "account_suppressed": self.account_suppressed,
            "no_contact": self.no_contact,
            "send_safe_withheld": self.send_safe_withheld,
            "other": self.other_unreconciled,
        }

    def reviewable_reconciles(self) -> bool:
        """EXACT identity over mutually-exclusive counters (no double/triple count):
        submitted - created - failed == sum(skip_breakdown)."""
        if self.mode == "dry_no_write":
            return True
        return (self.reviewable_submitted - self.created - self.failed
                == sum(self.skip_breakdown().values()))

    def instantly_untouched(self) -> bool:
        return self.enrolled == 0 and self.instantly_contacts == 0

    def enrollment_reconciles(self) -> bool:
        # Enrolled contacts can never exceed auto-approved FINAL_PASS; in
        # review-staging (no auto-approve) enrollment is always zero.
        return self.enrolled <= self.final_pass

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode, "entered": self.entered,
            "reviewable_submitted": self.reviewable_submitted,
            "created": self.created, "skipped": self.skipped,
            "skipped_existing": self.skipped_existing,
            "skipped_already_delivered": self.skipped_already_delivered,
            "person_employer_duplicate": self.person_employer_duplicate,
            "skip_breakdown": self.skip_breakdown(),
            "delivered_lead_keys": len(self.delivered_lead_keys),
            "failed": self.failed,
            "final_pass": self.final_pass, "needs_check": self.needs_check,
            "other_reviewable": self.other_reviewable,
            "instantly_contacts": self.instantly_contacts, "enrolled": self.enrolled,
            "airtable_reconciles": self.reconciles(),
            "reviewable_reconciles": self.reviewable_reconciles(),
            "instantly_untouched": self.instantly_untouched(),
            "failed_rows": len(self.failed_rows),
            "detail": dict(self.detail or {}),
        }


# Deterministic bucket priority for the person-employer collapse winner (highest
# first). A person resolved for several functions is enrolled under the single
# highest-priority one; the rest are dropped (provenance kept in delivery detail).
_BUCKET_PRIORITY = ("gtm_revenue", "finance", "operations", "product", "engineering",
                    "marketing", "people_hr", "customer_success", "customer_support")


def _norm_email(value: str) -> str:
    return str(value or "").strip().lower()


def _norm_domain(value: str) -> str:
    s = str(value or "").strip().lower()
    s = s.split("://", 1)[-1].split("/", 1)[0]
    return s[4:] if s.startswith("www.") else s


def _person_employer_key(lead) -> str:
    """(normalized employer domain, normalized resolved email) -- bucket-agnostic."""
    email = _norm_email(lead.contact.get("email"))
    row = lead.contact.get("_airtable_row") or {}
    domain = _norm_domain(row.get("employer_website") or row.get("company_domain")
                          or lead.company.get("website") or "")
    if not domain:
        # lead_key is "domain|email|bucket" -- reuse its domain segment as a fallback.
        domain = _norm_domain(str(lead.contact_key or "").split("|", 1)[0])
    return f"{domain}|{email}" if (domain and email) else ""


def _bucket_rank(lead) -> int:
    row = lead.contact.get("_airtable_row") or {}
    b = str(row.get("_role_bucket") or lead.contact.get("_role_bucket") or "").strip().lower()
    try:
        return _BUCKET_PRIORITY.index(b)
    except ValueError:
        return len(_BUCKET_PRIORITY)


def _existing_person_keys(existing) -> set:
    """Person-email index over existing Airtable rows (bucket-agnostic).

    NOTE: the existing-leads snapshot deliberately fetches a NARROW field set that
    does NOT include ``Email`` -- so the index is derived primarily from the
    ``Lead Key`` (format ``domain|email|bucket``), which IS fetched. Falling back to
    an ``Email``/``Website`` field when a caller supplies a richer record keeps this
    tolerant of every snapshot shape. An unreadable snapshot yields an empty index,
    which degrades to within-run collapse only (never over-suppresses).
    """
    keys: set = set()
    if not existing:
        return keys
    rows = existing.values() if isinstance(existing, dict) else existing
    for rec in rows:
        f = rec.get("fields", rec) if isinstance(rec, dict) else {}
        if not isinstance(f, dict):
            continue
        email = _norm_email(f.get("Email") or f.get("hiring_manager_email") or f.get("email"))
        domain = _norm_domain(f.get("Website") or f.get("employer_website") or f.get("company_domain") or "")
        lk = str(f.get("lead_key") or f.get("Lead Key") or "")
        if "|" in lk:
            parts = lk.split("|")
            if not domain:
                domain = _norm_domain(parts[0])
            if not email and len(parts) >= 2 and "@" in parts[1]:
                email = _norm_email(parts[1])
        if email and domain:
            keys.add(f"{domain}|{email}")
    return keys


def _collapse_person_employer(leads, *, existing=None):
    """Enforce ONE enrollment per (employer domain, resolved email).

    Returns (kept_leads, losers) where losers is a list of (winner_key, dropped_bucket).
    Distinct emails at the same employer (distinct buyers) are all kept; the same
    person at two employers (different domain) is kept in both. Flag-gated.
    """
    if not bool(getattr(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", False)):
        return list(leads), []
    existing_people = _existing_person_keys(existing)
    best: Dict[str, Any] = {}
    losers: List[Any] = []
    order: List[str] = []
    for lead in leads:
        pk = _person_employer_key(lead)
        if not pk:
            best[f"__nokey__{id(lead)}"] = lead
            order.append(f"__nokey__{id(lead)}")
            continue
        if pk in existing_people:
            # This person is ALREADY an active Airtable row (any bucket): never
            # re-enroll them under a new function key.
            row = lead.contact.get("_airtable_row") or {}
            losers.append((pk, str(row.get("_role_bucket") or "")))
            continue
        cur = best.get(pk)
        if cur is None:
            best[pk] = lead
            order.append(pk)
        elif _bucket_rank(lead) < _bucket_rank(cur):
            lrow = cur.contact.get("_airtable_row") or {}
            losers.append((pk, str(lrow.get("_role_bucket") or "")))
            best[pk] = lead
        else:
            row = lead.contact.get("_airtable_row") or {}
            losers.append((pk, str(row.get("_role_bucket") or "")))
    return [best[k] for k in order], losers


class RealDelivery:
    """Wraps airtable_client.push_leads + instantly_client.enroll_approved_leads.

    Three intents, chosen by the flags:

    * **dry_no_write** -- ``enable_airtable_write`` off: nothing written;
    * **review_staging** -- write on, ``auto_approve`` off: the REVIEWABLE set
      (FINAL_PASS + NEEDS_CHECK + other reviewable, never REJECT) is written to
      Airtable as ``Status=Pending`` for manual review; **Instantly is never
      called**, nothing is auto-approved or marked contacted;
    * **auto_approve** -- write on, ``auto_approve`` on: FINAL_PASS only, with
      optional Instantly enrollment.

    The real ``airtable_client`` owns the field schema (``Status=Pending`` via
    ``AIRTABLE_STATUS_PENDING``), idempotency by ``lead_key``, batch + per-record
    fallback, and the created/skipped_existing/failed accounting.
    """

    def __init__(self, *, enable_airtable_write: bool, auto_approve: bool, enable_instantly: bool):
        self.enable_airtable_write = enable_airtable_write
        self.auto_approve = auto_approve
        self.enable_instantly = enable_instantly

    def _rows(self, leads: List[Lead], *, run_id: str, source: str) -> List[Dict[str, Any]]:
        rows = []
        for l in leads:
            base = dict(l.contact.get("_airtable_row") or {})
            base.update({
                "lead_key": l.contact_key,
                "_final_state": l.disposition.value,
                "run_id": run_id,
                "_acquisition_source": source or "orchestrator",
                "employer_name": l.company.get("name", ""),
                "hiring_manager_email": l.contact.get("email", ""),
                "hiring_manager_name": l.contact.get("name", ""),
                "_primary_reason": l.primary_reason.value,
            })
            rows.append(base)
        return rows

    def deliver(self, leads: List[Lead], *, run_id: str = "", source: str = "",
                known_delivered=None, existing=None) -> RealDeliveryReport:
        known_delivered = {str(k) for k in (known_delivered or set())}
        rep = RealDeliveryReport(entered=len(leads))
        by = {d: [l for l in leads if l.disposition is d] for d in Disposition}
        reviewable = [l for l in leads if l.disposition in _REVIEWABLE]
        rep.final_pass = len(by[Disposition.FINAL_PASS])
        rep.needs_check = len(by[Disposition.NEEDS_CHECK])
        rep.other_reviewable = len(reviewable) - rep.final_pass - rep.needs_check

        if not self.enable_airtable_write:
            rep.mode = "dry_no_write"
            rep.skipped = rep.entered
            rep.detail = {"would_write_reviewable": len(reviewable)}
            return rep

        # What we actually submit: FINAL_PASS only when auto-approving, else the
        # whole reviewable set (never REJECT). Cross-run local idempotency: a
        # lead_key already delivered in a prior run is not re-submitted (this is
        # in addition to Airtable's own server-side lead_key dedup).
        candidates = by[Disposition.FINAL_PASS] if self.auto_approve else reviewable
        submit = [l for l in candidates if l.contact_key not in known_delivered]
        rep.skipped_already_delivered = len(candidates) - len(submit)
        # ENROLLMENT ANTI-SPAM IDENTITY (flag-gated): one person at one employer is
        # enrolled ONCE even when several functions/jobs resolved to them. Losers are
        # DROPPED from the submit set (never routed through suppressed keys, which
        # would mark them "delivered" and permanently suppress them) -- Gate C.
        submit, dup_losers = _collapse_person_employer(submit, existing=existing)
        rep.person_employer_duplicate = len(dup_losers)
        rep.mode = "auto_approve" if self.auto_approve else "review_staging"
        rep.reviewable_submitted = len(submit)

        import airtable_client
        result = airtable_client.push_leads(
            self._rows(submit, run_id=run_id, source=source), existing=existing)
        rep.created = int(result.get("created", 0))
        rep.failed = int(result.get("failed", 0))
        rep.skipped_existing = int(result.get("skipped_existing", 0))
        # MUTUALLY EXCLUSIVE skip counters, each from its own push_leads field -- the
        # old derivation (len(persisted)-created + the same suppressed lists again)
        # double-counted existing rows and triple-counted function suppressions.
        rep.updated_existing = int(result.get("updated", 0) or 0)
        rep.company_function_suppressed = int(result.get("skipped_existing_company", 0) or 0)
        rep.account_suppressed = int(result.get("skipped_existing_account", 0) or 0)
        rep.no_contact = int(result.get("skipped_no_contact", 0) or 0)
        # Rows withheld by AIRTABLE_WRITE_SEND_SAFE_ONLY are neither created nor
        # any skip category above. They were the largest single category on
        # 2026-09-04 -- 1,681 submitted, 781 created -- and they arrived in the run
        # summary as an unnamed residual, which reads as "900 rows lost" rather
        # than "900 rows deliberately not written because they are not send-safe".
        #
        # So they are now their own counter. ``other`` keeps the residual, because
        # the reconciliation identity must stay exact even when the adapter reports
        # something neither of us named; naming the send-safe share separately just
        # means the reader no longer has to guess which it was.
        rep.send_safe_withheld = int(result.get("not_written_not_send_safe", 0) or 0)
        accounted = (rep.created + rep.failed + rep.skipped_existing + rep.updated_existing
                     + rep.company_function_suppressed + rep.account_suppressed
                     + rep.no_contact + rep.send_safe_withheld)
        rep.other_unreconciled = max(0, rep.reviewable_submitted - accounted)
        # person_employer_duplicate rows were removed BEFORE submission, so they are
        # withheld (like already-delivered), not part of the submitted identity.
        rep.skipped = rep.entered - rep.created - rep.failed      # entered reconciles
        rep.failed_rows = [{"lead_key": k} for k in (result.get("failed_lead_keys", []) or [])]
        # Delivered = created + repaired-existing this run, PLUS the ones we
        # skipped because they were already delivered. NEVER a failed row, NEVER a
        # person-employer collapse loser.
        rep.delivered_lead_keys = sorted(
            (set(result.get("persisted_lead_keys", []) or [])
             | {l.contact_key for l in candidates if l.contact_key in known_delivered})
            - set(result.get("failed_lead_keys", []) or []))
        rep.detail = {"airtable": result,
                      "other_skips": sum(rep.skip_breakdown().values()) - rep.skipped_existing,
                      "withheld_before_submit": rep.skipped_already_delivered + len(dup_losers)
                      + (rep.entered - len(candidates)),
                      "person_employer_collapsed": [
                          {"winner": w, "dropped_bucket": b} for (w, b) in dup_losers]}

        # Instantly: ONLY in auto-approve mode and only when explicitly enabled.
        # Review-staging never enrolls anyone.
        if self.auto_approve and self.enable_instantly and rep.created:
            import instantly_client
            records = [{"id": k, "fields": {}} for k in
                       list(result.get("created_lead_keys", []) or [])[:rep.created]] \
                or [{"id": f"rec-{i}", "fields": {}} for i in range(rep.created)]
            enr = instantly_client.enroll_approved_leads(records)
            rep.enrolled = int(enr.get("enrolled", 0)) + int(enr.get("duplicates", 0))
            rep.instantly_contacts = len(records)
            rep.detail["instantly"] = enr
        return rep
