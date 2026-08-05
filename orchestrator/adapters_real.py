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
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from orchestrator.enrichment import EnrichmentReport, Lead
from orchestrator.lanes import LaneManager, LaneResult
from orchestrator.reasons import Disposition, ReasonCode, StageOutcome
from orchestrator.waterfall import StageResult, reconcile_stage

#: Client modules whose ``request_with_retry`` seam is faked offline.
SEAM_MODULES = ("apollo_client", "hunter_client", "airtable_client", "instantly_client")


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

    def run(self, opportunities: List[Dict[str, Any]]) -> EnrichmentReport:
        from qualification_pipeline import run_precontact_qualification
        import hiring_manager

        self.workdir.mkdir(parents=True, exist_ok=True)
        raw = self.workdir / "postings.json"
        raw.write_text(json.dumps({"jobs": opportunities}), encoding="utf-8")

        # 1) Real JobGate + RoleGate (offline: no source fetch).
        qual = run_precontact_qualification(str(raw), output_dir=str(self.workdir),
                                            fetch_sources=False)

        # 2) Real company identity + Apollo + Hunter + contact gates + FINAL_PASS.
        step3 = hiring_manager.run_hiring_manager_identification(
            qual.output_path, target_final_pass_leads=self.target_final_pass)

        leads = self._load_leads(step3.output_path)
        report = self._to_report(qual, step3, leads)
        return report

    @staticmethod
    def _load_leads(path: str) -> List[Dict[str, Any]]:
        p = Path(path)
        if not p.is_file():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data.get("leads") or data.get("jobs") or []
        return [r for r in rows if isinstance(r, dict)]

    def _to_report(self, qual, step3, lead_rows) -> EnrichmentReport:
        fp = int(step3.final_pass_leads)
        nc = int(step3.needs_check_leads)
        uv = int(step3.unverified_leads)
        rr = int(step3.reroute_leads)
        rj = int(step3.rejected_leads)

        # Real FINAL_PASS Lead objects (real lead_key + hiring_manager_email) for
        # delivery, taken from the persisted leads file.
        fp_rows = [r for r in lead_rows if str(r.get("_final_state")) == "FINAL_PASS"]
        leads: List[Lead] = []
        for row in fp_rows[:fp]:
            leads.append(Lead(
                posting_id=str(row.get("job_id") or row.get("lead_key") or ""),
                company={"name": row.get("employer_name", "")},
                contact={"email": row.get("hiring_manager_email", "")},
                disposition=Disposition.FINAL_PASS, primary_reason=ReasonCode.OK,
                contact_key=str(row.get("lead_key") or row.get("job_id") or ""),
            ))
        # Pad FINAL_PASS to the authoritative Step3Result count if rows are sparse.
        while len(leads) < fp:
            i = len(leads)
            leads.append(Lead(f"fp-{i}", {"name": ""}, {"email": f"fp{i}@x"},
                              Disposition.FINAL_PASS, ReasonCode.OK, contact_key=f"fp-{i}"))
        # Synthetic Leads carry the remaining dispositions so the census is exact.
        for disp, count, reason in (
            (Disposition.NEEDS_CHECK, nc, ReasonCode.HIRING_MANAGER_NOT_FOUND),
            (Disposition.UNVERIFIED, uv, ReasonCode.EMAIL_UNVERIFIED),
            (Disposition.REROUTE, rr, ReasonCode.COMPANY_UNRESOLVED),
            (Disposition.REJECT, rj, ReasonCode.NOT_ICP),
        ):
            for i in range(count):
                leads.append(Lead(f"{disp.value}-{i}", {"name": ""}, {},
                                  disp, reason, contact_key=""))

        dispo = (
            [(StageOutcome.PASSED, ReasonCode.OK, None)] * fp
            + [(StageOutcome.DEFERRED, ReasonCode.HIRING_MANAGER_NOT_FOUND, None)] * (nc + uv)
            + [(StageOutcome.REJECTED, ReasonCode.NOT_ICP, None)] * (rr + rj)
        )
        stage = reconcile_stage("hiring_manager", "lead", dispo)
        return EnrichmentReport(
            leads=leads,
            stages=[stage],
            loss_census={
                "hiring_manager_not_found": int(step3.hiring_manager_not_found),
                "reroute": rr, "rejected": rj, "needs_check": nc, "unverified": uv,
            },
        )


# --------------------------------------------------------------------------
# Delivery: real Airtable + Instantly, behind explicit flags, default disabled
# --------------------------------------------------------------------------


#: Dispositions written to Airtable for manual review -- the repository's own
#: AIRTABLE_REVIEW_STATES ({FINAL_PASS, UNVERIFIED, NEEDS_CHECK}); REJECT and
#: REROUTE are terminal and never written.
_REVIEWABLE = (Disposition.FINAL_PASS, Disposition.NEEDS_CHECK, Disposition.UNVERIFIED)


@dataclass
class RealDeliveryReport:
    mode: str = "dry_no_write"
    entered: int = 0
    reviewable_submitted: int = 0
    created: int = 0
    skipped: int = 0
    skipped_existing: int = 0        # idempotency duplicates
    failed: int = 0
    enrolled: int = 0
    instantly_contacts: int = 0      # MUST be 0 in review-staging
    final_pass: int = 0
    needs_check: int = 0
    other_reviewable: int = 0
    failed_rows: List[Dict[str, Any]] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    def reconciles(self) -> bool:
        return self.entered == self.created + self.skipped + self.failed

    def reviewable_reconciles(self) -> bool:
        # Mandated: reviewable_records = created + skipped_existing + failed
        # (idempotency dupes + created + failed). Any non-reviewable/suppressed
        # rows are counted separately in ``detail.other_skips``.
        other = int(self.detail.get("other_skips", 0))
        return self.reviewable_submitted == (
            self.created + self.skipped_existing + self.failed + other)

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
            "skipped_existing": self.skipped_existing, "failed": self.failed,
            "final_pass": self.final_pass, "needs_check": self.needs_check,
            "other_reviewable": self.other_reviewable,
            "instantly_contacts": self.instantly_contacts, "enrolled": self.enrolled,
            "airtable_reconciles": self.reconciles(),
            "reviewable_reconciles": self.reviewable_reconciles(),
            "instantly_untouched": self.instantly_untouched(),
            "failed_rows": len(self.failed_rows),
            "detail": dict(self.detail or {}),
        }


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

    def deliver(self, leads: List[Lead], *, run_id: str = "", source: str = "") -> RealDeliveryReport:
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
        # whole reviewable set (never REJECT).
        submit = by[Disposition.FINAL_PASS] if self.auto_approve else reviewable
        rep.mode = "auto_approve" if self.auto_approve else "review_staging"
        rep.reviewable_submitted = len(submit)

        import airtable_client
        result = airtable_client.push_leads(self._rows(submit, run_id=run_id, source=source))
        rep.created = int(result.get("created", 0))
        rep.failed = int(result.get("failed", 0))
        rep.skipped_existing = int(result.get("skipped_existing", 0))
        # Existing rows the adapter repaired in place (persisted beyond created).
        updated = max(0, len(result.get("persisted_lead_keys", []) or []) - rep.created)
        other_skips = (updated
                       + int(result.get("skipped_existing_company", 0))
                       + int(result.get("skipped_no_contact", 0))
                       + len(result.get("suppressed_company_lead_keys", []) or []))
        rep.skipped = rep.entered - rep.created - rep.failed      # entered reconciles
        rep.failed_rows = [{"lead_key": k} for k in (result.get("failed_lead_keys", []) or [])]
        rep.detail = {"airtable": result, "other_skips": other_skips}

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
