"""Acquisition lanes -- the composition the validation proved missing.

Each lane is independently selectable and independently isolated: a lane that
raises records its own failure and returns; it can never erase another lane's
results (they are collected as they complete, never accumulated-then-committed).

The ATS lane is first-class and assembles the validated components into a single
loop:

* ``ats_schedule.select_boards``     -- deterministic board selection;
* ``RequestBudget``                  -- board < provider < lane < global scopes,
                                        plus reserved non-ATS capacity;
* ``AtsBoardSession``                -- per-board checkpoint the instant a board
                                        finishes, so a later board cannot retract
                                        completed work;
* ``request_trace.Trace``            -- physical-request classification; because
                                        every physical request is reserved through
                                        the budget, and ``budget.reserve`` classifies
                                        into the installed trace, ``trace.total`` equals
                                        the physical count by construction;
* ``ats_board_registry.fetch_board_jobs`` -- the untouched provider adapters.

The seam is a single ``_BudgetSeam`` wrapper: one physical request == one
``budget.reserve``. That keeps offline and live accounting identical and
deterministic; live redirect/retry phase refinement is populated by the existing
``http_utils`` / ``default_fetcher`` transport hooks when a trace is installed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ats_board_registry import fetch_board_jobs
from free_job_sources import FetchPayload

from retrieval_measurement import request_trace
from retrieval_measurement.ats_checkpoint import AtsBoardSession
from retrieval_measurement.instrument import RequestBudget, RequestCeilingReached
from retrieval_measurement.request_trace import Trace
from retrieval_measurement.schema import RequestRecord
from retrieval_measurement import ats_schedule


Fetcher = Callable[..., FetchPayload]


@dataclass
class LaneResult:
    lane: str
    status: str                       # complete | partial | failed | skipped
    jobs: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    physical_requests: int = 0
    attribution: Dict[str, Any] = field(default_factory=dict)
    accounting: Dict[str, Any] = field(default_factory=dict)
    request_ledger: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lane": self.lane,
            "status": self.status,
            "jobs": len(self.jobs),
            "errors": list(self.errors),
            "physical_requests": self.physical_requests,
            "attribution": dict(self.attribution),
            "accounting": dict(self.accounting),
            "notes": list(self.notes),
        }


class _BudgetSeam:
    """One physical request == one budget reservation, recorded and traced.

    ``reserve`` consults board/provider/lane/global scopes (innermost first) and
    raises ``RequestCeilingReached`` before the wire when a scope is exhausted.
    """

    def __init__(self, inner: Fetcher, budget: RequestBudget, *, source: str = "") -> None:
        self.inner = inner
        self.budget = budget
        self.source = source
        self.requests: List[RequestRecord] = []
        self._seq = 0

    def __call__(self, url: str, **kwargs: Any) -> FetchPayload:
        self.budget.reserve(url)  # may raise before the request is issued
        started = time.perf_counter()
        payload = self.inner(url, **kwargs)
        self._seq += 1
        text = getattr(payload, "text", "") or ""
        self.requests.append(RequestRecord(
            sequence=self._seq,
            source=self.source or self.budget.source,
            url=str(getattr(payload, "url", url) or url),
            method=str(kwargs.get("method", "GET")),
            param_keys=sorted(str(k) for k in dict(kwargs.get("params") or {})),
            board_key=self.budget.board,
            status_code=getattr(payload, "status_code", None),
            response_bytes=len(text.encode("utf-8", "ignore")),
            duration_seconds=round(time.perf_counter() - started, 4),
            error=str(getattr(payload, "error", "") or ""),
        ))
        return payload


class LaneManager:
    """Runs the selected lanes in isolation and returns a result per lane."""

    def __init__(self, *, budget: RequestBudget) -> None:
        self.budget = budget

    # -- ATS ---------------------------------------------------------------

    def run_ats(
        self,
        boards: Sequence[Mapping[str, Any]],
        fetcher: Fetcher,
        *,
        checkpoint_dir: str | Path,
        scheduler_config: Any,
        position: int = 0,
        carried_overdue: Sequence[str] = (),
        detail_budgets: Optional[Mapping[str, int]] = None,
    ) -> LaneResult:
        detail_budgets = dict(detail_budgets or {})
        trace = Trace()
        session = AtsBoardSession(checkpoint_dir=checkpoint_dir, budget=self.budget, trace=trace)

        decision = ats_schedule.select_boards(
            list(boards), config=scheduler_config, position=position, carried_overdue=carried_overdue
        )
        selected = decision.selected
        session.plan(selected, decision=decision, scheduler_config=scheduler_config)

        seam = _BudgetSeam(fetcher, self.budget)
        errors: List[str] = []
        run_stopped = False

        with request_trace.install(trace), self.budget.context(lane="ats"):
            for board in selected:
                provider = str(board.get("provider") or "unknown")
                identifier = str(board.get("identifier") or "")
                key = f"{provider}:{identifier}"
                block = self.budget.would_block(lane="ats", source=f"ats_{provider}", board=key)
                if block is not None:
                    session.skip_for_budget(board, block["scope"], exhausted_scope=block["scope"])
                    if block["scope"] in ("run", "lane", "lane_reservation"):
                        run_stopped = True
                    continue
                with session.board(board) as state:
                    seam.source = f"ats_{provider}"
                    jobs: List[Dict[str, Any]] = []
                    error = ""
                    stop_reason = ""
                    with request_trace.role("listing"):
                        try:
                            jobs, error = fetch_board_jobs(
                                dict(board),
                                seam,
                                greenhouse_detail_budget=detail_budgets.get("greenhouse"),
                                workday_detail_budget=detail_budgets.get("workday"),
                                smartrecruiters_detail_budget=detail_budgets.get("smartrecruiters"),
                            )
                        except RequestCeilingReached as exc:
                            scope = (self.budget.blocked_next_request or {}).get("scope", "run")
                            stop_reason = f"budget_exhausted:{scope}"
                            error = stop_reason
                            if scope in ("run", "lane", "lane_reservation"):
                                run_stopped = True
                        except Exception as exc:  # noqa: BLE001 - one board never costs the rest
                            error = f"{type(exc).__name__}: {exc}"
                    session.record(state, jobs=jobs, error=error, budget_stop=stop_reason,
                                   pages=len(jobs))
                    if error:
                        errors.append(f"{key}:{error}")
                if run_stopped:
                    # Remaining boards are pre-skipped so they are recorded, not lost.
                    for remaining in selected[selected.index(board) + 1:]:
                        session.skip_for_budget(remaining, "run", exhausted_scope="run")
                    break

        acct = session.accounting()
        status = "complete"
        if not session.results:
            status = "skipped"
        elif acct.get("boards_failed") and not acct.get("boards_completed"):
            status = "failed"
        elif acct.get("lane_status") == "partial":
            status = "partial"
        return LaneResult(
            lane="ats",
            status=status,
            jobs=session.jobs(),
            errors=errors,
            physical_requests=int(acct.get("physical_requests", 0)),
            attribution={
                "request_trace": trace.to_dict(),
                "trace_total_equals_physical": trace.reconciles(int(acct.get("physical_requests", 0))),
                "seam_requests": len(seam.requests),
            },
            accounting={**acct, "reconciliation_identities": session.full_reconciliation(),
                        "reconciled": session.reconciles(),
                        "session": session.to_dict()},
            request_ledger=[r.to_dict() for r in seam.requests],
        )

    # -- generic simple lane (jsearch / free feeds) ------------------------

    def run_simple(
        self,
        lane: str,
        producer: Callable[[Fetcher], List[Dict[str, Any]]],
        fetcher: Fetcher,
        *,
        source: str,
    ) -> LaneResult:
        """A lane that pulls records through the budgeted seam via a producer.

        ``producer`` receives the seam and returns canonical records. Used for
        the JSearch and free-feed lanes, which run under the reserved capacity
        rather than the ATS lane budget.
        """
        seam = _BudgetSeam(fetcher, self.budget, source=source)
        errors: List[str] = []
        status = "complete"
        jobs: List[Dict[str, Any]] = []
        with self.budget.context(lane=lane, source=source):
            try:
                jobs = list(producer(seam))
            except RequestCeilingReached as exc:
                status = "partial"
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                errors.append(f"{type(exc).__name__}: {exc}")
        return LaneResult(
            lane=lane,
            status=status,
            jobs=jobs,
            errors=errors,
            physical_requests=len(seam.requests),
            attribution={"seam_requests": len(seam.requests)},
            request_ledger=[r.to_dict() for r in seam.requests],
        )
