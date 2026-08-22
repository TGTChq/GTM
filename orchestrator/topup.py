"""Adaptive net-new top-up controller (COMMIT 3).

Keeps three DISTINCT concepts apart so none is overloaded:

  A. Acquisition safety cap  -- the maximum Fantastic jobs we are willing to BILL
     in one run (config.FANTASTIC_JOBS_MAX_JOBS_PER_RUN). A hard ceiling; billing
     can never exceed it.
  B. Production yield target  -- the desired count of NET-NEW, send_safe_facts-PASS
     leads ACTUALLY CREATED in Airtable (config.NET_NEW_SEND_SAFE_TARGET). This is
     the ONLY thing that counts toward "done"; duplicates, existing company/function
     rows, cheap qualification rejects, no-contact results and non-send-safe results
     do NOT count.
  C. Hard stop boundaries  -- fantastic quota reserve, the safety cap, inventory /
     continuation exhaustion, Apollo circuit open, and a runtime budget.

The controller is pure and deterministic: it decides, before each acquisition
slice, whether to continue and how large the next slice may be (never past the
safety cap), and it records what each processed slice actually billed and yielded.
A bounded max-iteration guard makes an infinite loop impossible even if a slice
ever yields zero net-new without tripping another boundary.

This module holds NO I/O -- the pipeline feeds it observations (quota remaining,
circuit state, whether the last slice returned inventory) and the net-new count.
``NET_NEW_SEND_SAFE_TARGET <= 0`` means the feature is OFF and the pipeline runs
its normal single pass.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class TopUpDecision:
    should_continue: bool
    stop_reason: str
    next_slice: int  # jobs the next acquisition may request (0 when stopping)


class TopUpController:
    def __init__(
        self,
        *,
        target_net_new: int,
        safety_cap_jobs: int,
        slice_jobs: int,
        min_quota_remaining: int = 0,
        runtime_budget_seconds: Optional[float] = None,
        max_iterations: int = 50,
        clock: Callable[[], float] = time.monotonic,
        budget_source: str = "per_run_ceiling",
    ) -> None:
        self.target_net_new = int(target_net_new)
        self.safety_cap_jobs = max(0, int(safety_cap_jobs))
        # Which authority produced ``safety_cap_jobs``: "per_run_ceiling"
        # (FANTASTIC_JOBS_MAX_JOBS_PER_RUN, the unchanged default) or "governor"
        # (the monthly credit governor's run budget). Determines the stop label so
        # a governor stop is never reported as the generic safety cap.
        self.budget_source = str(budget_source or "per_run_ceiling")
        self.slice_jobs = max(1, int(slice_jobs))
        self.min_quota_remaining = max(0, int(min_quota_remaining))
        self.runtime_budget_seconds = runtime_budget_seconds
        self.max_iterations = max(1, int(max_iterations))
        self._clock = clock
        self._start = clock()
        # Live accumulation.
        self.net_new = 0
        self.billed = 0
        self.iterations = 0
        self.last_stop_reason = ""

    @property
    def enabled(self) -> bool:
        return self.target_net_new > 0

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self._start

    def decide(
        self,
        *,
        quota_remaining: Optional[int] = None,
        apollo_circuit_open: bool = False,
        inventory_exhausted: bool = False,
    ) -> TopUpDecision:
        """Decide whether to acquire ANOTHER slice, and how big it may be. Called
        BEFORE each acquisition. Order matters: target satisfaction wins first, then
        each hard boundary. ``next_slice`` is always clamped so cumulative billing
        can never exceed the safety cap."""
        if self.net_new >= self.target_net_new:
            return self._stop("target_reached")
        if self.iterations >= self.max_iterations:
            return self._stop("max_iterations_guard")
        if self.billed >= self.safety_cap_jobs:
            return self._stop(self._cap_reason())
        if quota_remaining is not None and quota_remaining <= self.min_quota_remaining:
            return self._stop("fantastic_quota_floor")
        if apollo_circuit_open:
            return self._stop("apollo_circuit_open")
        if (self.runtime_budget_seconds is not None
                and self.elapsed_seconds >= self.runtime_budget_seconds):
            return self._stop("runtime_budget")
        if inventory_exhausted:
            return self._stop("inventory_exhausted")
        remaining_cap = self.safety_cap_jobs - self.billed
        nxt = min(self.slice_jobs, remaining_cap)
        if nxt <= 0:
            return self._stop(self._cap_reason())
        return TopUpDecision(True, "", nxt)

    def _cap_reason(self) -> str:
        """Distinct stop label per budget authority (never overloaded)."""
        return "governor_run_budget" if self.budget_source == "governor" else "acquisition_safety_cap"

    def record(self, *, billed: int, net_new_send_safe: int) -> None:
        """Record the outcome of one processed slice."""
        self.billed += max(0, int(billed))
        self.net_new += max(0, int(net_new_send_safe))
        self.iterations += 1

    def _stop(self, reason: str) -> TopUpDecision:
        self.last_stop_reason = reason
        return TopUpDecision(False, reason, 0)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "target_net_new": self.target_net_new,
            "safety_cap_jobs": self.safety_cap_jobs,
            "budget_source": self.budget_source,
            "slice_jobs": self.slice_jobs,
            "net_new_send_safe": self.net_new,
            "jobs_billed": self.billed,
            "iterations": self.iterations,
            "stop_reason": self.last_stop_reason,
            "target_reached": self.net_new >= self.target_net_new,
        }
