"""Per-board checkpointing and accounting for the REAL ATS acquisition path.

Why this exists
---------------
``multi_source_acquisition.run_multi_source_acquisition`` accumulates ATS
postings in a local ``ats_jobs`` list and only merges them into ``all_jobs``
after the whole board loop finishes. ``fetch_board_jobs`` returning an *error*
is handled; ``fetch_board_jobs`` *raising* is not, and the exception unwinds the
entire function, taking every completed board's postings with it.

That is not hypothetical. Run ``20260805T015708Z-1ad3ef58`` spent 968 requests
across 30 boards and recorded nothing for exactly this reason.

``AtsBoardSession`` is a passive observer that the real loop hands each board's
outcome to. It persists that board immediately, so work already paid for is
durable before the next board is attempted. It does not fetch anything, does not
wrap the transport, and does not duplicate a single line of ``fetch_board_jobs``.

Default-off
-----------
The acquisition function takes ``ats_session=None``. With no session, every hook
is skipped, no directory is created, no file is written, and the loop behaves
exactly as it did before.

Attribution honesty
-------------------
With a ``Trace`` installed, retry-versus-redirect attribution is now exact,
including a redirect that descends from a retry (counted once, as a redirect,
with ``origin_attempt=retry`` -- see ``request_trace``). Without a trace the
ambiguous fields stay ``UNRESOLVED`` rather than guessed: an estimate written
into a field named ``listing_retries`` would be indistinguishable from a
measurement later.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from retrieval_measurement.identity import utc_stamp
from retrieval_measurement.schema import BoardResult

#: Marker for a field whose exact value needs Phase 1B-2B's classification
#: precedence. Never a number, so it can never be mistaken for one.
UNRESOLVED = "UNRESOLVED_PENDING_1B2B"

#: Board outcomes. ``partial`` means the board returned records AND reported a
#: problem -- it is neither a clean success nor a total loss.
OUTCOMES = ("completed", "partial", "failed", "skipped_by_budget")


class AtsBoardSession:
    """Observes the real ATS board loop; persists and reconciles."""

    def __init__(
        self,
        *,
        checkpoint_dir: Optional[str | Path] = None,
        budget: Optional[Any] = None,
        trace: Optional[Any] = None,
        continue_on_board_error: bool = True,
    ) -> None:
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self.budget = budget
        self.trace = trace
        self.continue_on_board_error = bool(continue_on_board_error)
        self.results: List[BoardResult] = []
        self.jobs_by_board: Dict[str, List[Dict[str, Any]]] = {}
        self.boards_planned = 0
        self.skipped_by_budget: List[str] = []
        #: Scheduler provenance, populated by ``plan`` when a decision is given.
        #: All default to a scheduler-free run so existing callers are unchanged.
        self._scheduler_skipped = 0
        self._scheduler_selected_overdue = 0
        self._scheduler_selected_retry = 0
        self._scheduler_selected_normal = 0
        self._scheduler_carried_forward: List[str] = []
        self._scheduler_config: Dict[str, Any] = {}
        self._scheduler_mode = ""
        if self.checkpoint_dir is not None:
            self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    # -- lifecycle ---------------------------------------------------------

    def plan(
        self,
        boards: Sequence[Mapping[str, Any]],
        *,
        available: Optional[int] = None,
        decision: Optional[Any] = None,
        scheduler_config: Optional[Any] = None,
    ) -> None:
        """Record the selected boards and, when scheduling ran, its provenance.

        ``available`` is the count the scheduler chose from (the full eligible
        registry); ``decision`` is a ``ScheduleDecision``; ``scheduler_config``
        is a ``SchedulerConfig``. With none of them supplied the run is treated
        as scheduler-free: nothing was skipped by a scheduler, and
        ``boards_available`` collapses to the number selected.
        """
        selected = len(list(boards))
        self.boards_planned = selected
        if available is not None:
            self._scheduler_skipped = max(0, int(available) - selected)
        if decision is not None:
            self._scheduler_skipped = max(
                0, int(getattr(decision, "available", selected)) - selected
            )
            self._scheduler_selected_overdue = int(getattr(decision, "selected_overdue", 0))
            self._scheduler_selected_retry = int(getattr(decision, "selected_retry", 0))
            self._scheduler_selected_normal = int(getattr(decision, "selected_normal", 0))
            self._scheduler_carried_forward = list(getattr(decision, "carried_forward", []))
            self._scheduler_mode = str(getattr(decision, "mode", ""))
        if scheduler_config is not None:
            self._scheduler_config = (
                scheduler_config.to_dict()
                if hasattr(scheduler_config, "to_dict")
                else dict(scheduler_config)
            )
            self._scheduler_mode = self._scheduler_config.get("mode", self._scheduler_mode)

    @contextmanager
    def board(self, board: Mapping[str, Any]) -> Iterator[Dict[str, Any]]:
        """Scope one board: timestamps, budget context, request counting."""
        provider = str(board.get("provider") or "unknown")
        identifier = str(board.get("identifier") or "")
        key = f"{provider}:{identifier}"
        state: Dict[str, Any] = {
            "provider": provider,
            "identifier": identifier,
            "company_name": str(board.get("company_name") or ""),
            "key": key,
            "selection_reason": str(board.get("scheduler_reason") or ""),
            "started_at": utc_stamp(),
            "requests_before": getattr(self.budget, "count", 0) if self.budget else 0,
            "trace_before": self._trace_snapshot(),
        }
        # Clear any pending phase annotation so a previous board that raised
        # between marking a retry/redirect and sending it cannot leak its phase
        # onto this board's first request. No-op when tracing is off.
        self._reset_trace_phase()
        try:
            if self.budget is not None:
                with self.budget.context(lane="ats", source=f"ats_{provider}", board=key):
                    yield state
            else:
                yield state
        finally:
            self._reset_trace_phase()

    def _reset_trace_phase(self) -> None:
        try:
            from retrieval_measurement.request_trace import reset

            reset()
        except Exception:  # pragma: no cover - instrumentation is never fatal
            pass

    def _trace_snapshot(self) -> Dict[str, int]:
        if self.trace is None:
            return {}
        data = self.trace.to_dict()
        return {k: v for k, v in data.items() if isinstance(v, int)}

    # -- recording ---------------------------------------------------------

    def record(
        self,
        state: Mapping[str, Any],
        *,
        jobs: Optional[Sequence[Mapping[str, Any]]] = None,
        error: str = "",
        outcome: str = "",
        budget_stop: str = "",
        pages: int = 0,
        raw_records: Optional[int] = None,
        truncation: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> BoardResult:
        """Persist one attempted board. Called once per board, immediately."""
        jobs = list(jobs or [])
        physical = (
            getattr(self.budget, "count", 0) - int(state.get("requests_before", 0))
            if self.budget is not None
            else 0
        )
        after = self._trace_snapshot()
        before = dict(state.get("trace_before") or {})
        delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}

        if not outcome:
            if error and jobs:
                outcome = "partial"
            elif error:
                outcome = "failed"
            else:
                outcome = "completed"
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown board outcome {outcome!r}")

        result = BoardResult(
            provider=str(state["provider"]),
            identifier=str(state["identifier"]),
            company_name=str(state.get("company_name") or ""),
            started_at=str(state.get("started_at") or ""),
            completed_at=utc_stamp(),
            physical_requests=physical,
            pages=int(pages),
            listing_records=len(jobs),
            canonical_records=len(jobs),
            stop_reason=budget_stop or outcome,
            error=error,
            truncation=[dict(item) for item in (truncation or [])],
            attempted=True,
            selection_reason=str(state.get("selection_reason") or ""),
        )
        # Exact where the transport reports it; explicitly unresolved otherwise.
        result.detail_records = int(delta.get("initial_detail", 0))
        result.redirects = UNRESOLVED if self.trace is None else int(
            delta.get("listing_redirects", 0) + delta.get("detail_redirects", 0)
        )
        result.retries = UNRESOLVED if self.trace is None else int(
            delta.get("listing_retries", 0) + delta.get("detail_retries", 0)
        )
        if jobs:
            from retrieval_measurement.accounting import dual_uniqueness

            uniqueness = dual_uniqueness([dict(job) for job in jobs])
            result.unique_posting_identity = uniqueness.unique_posting_identity
            result.unique_production_equivalent = uniqueness.unique_production_equivalent
        if raw_records is not None:
            result.listing_records = int(raw_records)

        self._persist(result, jobs, trace_delta=delta)
        self.results.append(result)
        self.jobs_by_board[result.key] = [dict(job) for job in jobs]
        return result

    def skip_for_budget(
        self,
        board: Mapping[str, Any],
        scope: str,
        *,
        exhausted_scope: str = "",
        stop_reason: str = "",
    ) -> BoardResult:
        """Persist a board that was selected but never entered the transport.

        ``attempted`` is False and ``skipped_by_budget`` is True, so the board is
        durably recorded yet sits outside ``boards_attempted``. Zero requests
        were made; ``exhausted_scope`` names the budget that had no room. This is
        the pre-emptive skip: it is called BEFORE any request, so the board's
        physical request count is genuinely zero.
        """
        provider = str(board.get("provider") or "unknown")
        identifier = str(board.get("identifier") or "")
        exhausted = exhausted_scope or scope
        result = BoardResult(
            provider=provider,
            identifier=identifier,
            company_name=str(board.get("company_name") or ""),
            started_at=utc_stamp(),
            completed_at=utc_stamp(),
            physical_requests=0,
            listing_records=0,
            canonical_records=0,
            stop_reason=stop_reason or f"budget_exhausted:{scope}",
            error=f"skipped: {scope} budget exhausted before any request",
            attempted=False,
            skipped_by_budget=True,
            exhausted_scope=exhausted,
            selection_reason=str(board.get("scheduler_reason") or ""),
        )
        self.skipped_by_budget.append(result.key)
        self._persist(result, [], trace_delta={})
        self.results.append(result)
        return result

    def _persist(
        self,
        result: BoardResult,
        jobs: Sequence[Mapping[str, Any]],
        *,
        trace_delta: Mapping[str, int],
    ) -> None:
        if self.checkpoint_dir is None:
            result.checkpoint_path = ""
            return
        # Real Workday/Cornerstone identifiers are ``tenant|site`` and other
        # providers may carry slashes; neither is a legal Windows filename
        # character. Map every filesystem-reserved character to ``_`` so a board
        # can always be checkpointed on any platform.
        safe = f"{result.provider}__{result.identifier or 'unknown'}"
        for ch in '<>:"/\\|?*':
            safe = safe.replace(ch, "_")
        target = self.checkpoint_dir / f"{safe}.json"
        payload = {
            "board": result.to_dict(),
            "request_trace_delta": dict(trace_delta),
            "attribution_status": "exact" if self.trace is not None else UNRESOLVED,
            "jobs": [dict(job) for job in jobs],
        }
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(temp, target)      # atomic: a crash leaves the previous file
        result.checkpoint_path = str(target)

    # -- accounting --------------------------------------------------------

    def jobs(self) -> List[Dict[str, Any]]:
        """Every posting from every board that produced one, failures included."""
        out: List[Dict[str, Any]] = []
        for result in self.results:
            out.extend(self.jobs_by_board.get(result.key, []))
        return out

    def accounting(self) -> Dict[str, Any]:
        skipped = [r for r in self.results if r.skipped_by_budget]
        attempted = [r for r in self.results if not r.skipped_by_budget]
        completed = [r for r in attempted if not r.error]
        partial = [r for r in attempted if r.error and r.canonical_records]
        failed = [r for r in attempted if r.error and not r.canonical_records]
        jobs = self.jobs()

        # Derived so the Phase 1B-2B identities hold by construction and reconcile
        # to the persisted board records:
        #   boards_selected  == boards_skipped_by_budget + boards_attempted
        #   boards_available == boards_selected + boards_skipped_by_scheduler
        boards_selected = len(attempted) + len(skipped)
        boards_available = boards_selected + self._scheduler_skipped

        counters = {
            "boards_planned": self.boards_planned,
            "boards_available": boards_available,
            "boards_selected": boards_selected,
            "boards_skipped_by_scheduler": self._scheduler_skipped,
            "boards_attempted": len(attempted),
            "boards_completed": len(completed),
            "boards_partial": len(partial),
            "boards_failed": len(failed),
            "boards_skipped_by_budget": len(skipped),
            "boards_selected_overdue": self._scheduler_selected_overdue,
            "boards_selected_retry": self._scheduler_selected_retry,
            "boards_selected_normal": self._scheduler_selected_normal,
            "boards_carried_forward": len(self._scheduler_carried_forward),
            "physical_requests": sum(r.physical_requests for r in self.results),
            "raw_records": sum(r.listing_records for r in self.results),
            "normalized_records": sum(r.canonical_records for r in self.results),
            "records_retained_after_partial_failure": sum(
                r.canonical_records for r in partial + failed
            ) + sum(r.canonical_records for r in completed),
            "lane_status": "partial" if (partial or failed or skipped) else "complete",
        }
        if self._scheduler_config:
            counters["scheduler"] = dict(self._scheduler_config)
        if self._scheduler_mode:
            counters["scheduler_mode"] = self._scheduler_mode
        trace_categories = self._trace_snapshot()
        if trace_categories:
            counters["physical_request_categories"] = trace_categories
        if jobs:
            from retrieval_measurement.accounting import dual_uniqueness

            uniqueness = dual_uniqueness(jobs)
            counters["unique_posting_identity"] = uniqueness.unique_posting_identity
            counters["unique_production_equivalent"] = uniqueness.unique_production_equivalent
        else:
            counters["unique_posting_identity"] = 0
            counters["unique_production_equivalent"] = 0
        return counters

    def reconciles(self) -> bool:
        """True when every aggregate ties to the persisted board records."""
        c = self.accounting()
        boards_ok = c["boards_attempted"] == (
            c["boards_completed"] + c["boards_partial"] + c["boards_failed"]
        )
        selected_ok = c["boards_selected"] == (
            c["boards_skipped_by_budget"] + c["boards_attempted"]
        )
        available_ok = c["boards_available"] == (
            c["boards_selected"] + c["boards_skipped_by_scheduler"]
        )
        # The genuine tie to persisted records: every selected board -- attempted
        # or pre-skipped -- has exactly one BoardResult.
        records_present_ok = len(self.results) == c["boards_selected"]
        records_ok = c["normalized_records"] == sum(
            len(v) for v in self.jobs_by_board.values()
        )
        return boards_ok and selected_ok and available_ok and records_present_ok and records_ok

    def full_reconciliation(self) -> Dict[str, bool]:
        """Each Phase 1B-2B identity, individually, for explicit assertions."""
        c = self.accounting()
        return {
            "available_equals_selected_plus_scheduler_skips": (
                c["boards_available"]
                == c["boards_selected"] + c["boards_skipped_by_scheduler"]
            ),
            "selected_equals_budget_skips_plus_attempted": (
                c["boards_selected"]
                == c["boards_skipped_by_budget"] + c["boards_attempted"]
            ),
            "attempted_equals_completed_partial_failed": (
                c["boards_attempted"]
                == c["boards_completed"] + c["boards_partial"] + c["boards_failed"]
            ),
            "records_tie_to_persisted_boards": (
                len(self.results) == c["boards_selected"]
            ),
            "normalized_records_tie_to_jobs": (
                c["normalized_records"]
                == sum(len(v) for v in self.jobs_by_board.values())
            ),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accounting": self.accounting(),
            "reconciled": self.reconciles(),
            "reconciliation_identities": self.full_reconciliation(),
            "attribution_status": "exact" if self.trace is not None else UNRESOLVED,
            "checkpoint_dir": str(self.checkpoint_dir) if self.checkpoint_dir else "",
            "carried_forward": list(self._scheduler_carried_forward),
            "boards": [r.to_dict() for r in self.results],
            "skipped_by_budget": list(self.skipped_by_budget),
        }
