"""Exact classification of physical outbound HTTP attempts.

Six mutually exclusive classes, reconciling to the global physical count:

    physical = initial_listing + listing_retries + listing_redirects
             + initial_detail  + detail_retries  + detail_redirects

Why context propagation rather than inference
---------------------------------------------
The previous instrumentation derived detail requests as
``physical - seam``, which cannot distinguish a detail call from a retry of a
listing call -- they look identical from above. And the transport layer, which
is the only place that knows an attempt is a retry, has no idea whether the
call it is retrying was a listing or a detail request.

So the two facts are carried from the two places that actually know them:

* **role** (listing vs detail) is annotated by the caller -- the ATS provider
  branch that is about to fetch a job's detail page says so;
* **phase** (initial vs retry vs redirect) is annotated by the transport loops
  that already count them: ``http_utils.request_with_retry`` knows its attempt
  number, ``free_job_sources.default_fetcher`` knows its redirect hop.

Neither annotation reimplements transport, retry or redirect logic. Each is a
single cheap call inside a loop that already exists.

Disabled by default
-------------------
With no ``Trace`` installed, every helper here is an early-return on a
``ContextVar.get()``. No hook, no patch, no wrapper: ``request_with_retry`` and
``default_fetcher`` execute exactly the statements they executed before, with
identical timeouts, headers, backoff, exceptions, redirects and return values.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

ROLES = ("listing", "detail")
PHASES = ("initial", "retry", "redirect")

#: What a redirect hop was born from. Metadata only -- ``origin_attempt`` never
#: adds to any of the six physical counters; it distinguishes a redirect that
#: happened during a retry from one during an initial request.
ORIGINS = ("initial", "retry")

#: The active trace, or None. Everything is a no-op while this is None.
_TRACE: ContextVar[Optional["Trace"]] = ContextVar("rm_trace", default=None)
_ROLE: ContextVar[str] = ContextVar("rm_role", default="listing")
_PHASE: ContextVar[str] = ContextVar("rm_phase", default="initial")
_ORIGIN: ContextVar[str] = ContextVar("rm_origin", default="initial")


def enabled() -> bool:
    return _TRACE.get() is not None


@dataclass
class Trace:
    """Counters for one run. Shared across lanes; safe under threads."""

    initial_listing: int = 0
    listing_retries: int = 0
    listing_redirects: int = 0
    initial_detail: int = 0
    detail_retries: int = 0
    detail_redirects: int = 0
    #: {origin_attempt -> count} over redirect hops only. A pure annotation:
    #: sum(redirect_origins.values()) == listing_redirects + detail_redirects.
    #: It never contributes to ``total``.
    redirect_origins: Dict[str, int] = field(default_factory=dict)
    by_board: Dict[str, Dict[str, int]] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    _FIELDS = {
        ("listing", "initial"): "initial_listing",
        ("listing", "retry"): "listing_retries",
        ("listing", "redirect"): "listing_redirects",
        ("detail", "initial"): "initial_detail",
        ("detail", "retry"): "detail_retries",
        ("detail", "redirect"): "detail_redirects",
    }

    def record(self, role: str, phase: str, *, board: str = "", origin: str = "initial") -> str:
        name = self._FIELDS.get((role, phase))
        if name is None:
            raise ValueError(f"unknown attempt class: role={role!r} phase={phase!r}")
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)
            if board:
                bucket = self.by_board.setdefault(board, {})
                bucket[name] = bucket.get(name, 0) + 1
            if phase == "redirect":
                # Metadata: which attempt this redirect descended from. Counted
                # once, as a redirect -- never also as a retry.
                origin_key = origin if origin in ORIGINS else "initial"
                self.redirect_origins[origin_key] = (
                    self.redirect_origins.get(origin_key, 0) + 1
                )
        return name

    @property
    def total(self) -> int:
        return (
            self.initial_listing + self.listing_retries + self.listing_redirects
            + self.initial_detail + self.detail_retries + self.detail_redirects
        )

    def reconciles(self, physical_requests: int) -> bool:
        return self.total == int(physical_requests)

    def origins_reconcile(self) -> bool:
        """Origin metadata must sum to exactly the redirect count, no more."""
        return sum(self.redirect_origins.values()) == (
            self.listing_redirects + self.detail_redirects
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "initial_listing": self.initial_listing,
            "listing_retries": self.listing_retries,
            "listing_redirects": self.listing_redirects,
            "initial_detail": self.initial_detail,
            "detail_retries": self.detail_retries,
            "detail_redirects": self.detail_redirects,
            "total_physical_attempts": self.total,
            "redirect_origins": dict(sorted(self.redirect_origins.items())),
            "by_board": {k: dict(v) for k, v in sorted(self.by_board.items())},
        }


@contextmanager
def install(trace: Optional[Trace] = None) -> Iterator[Trace]:
    """Activate tracing for the duration of the block."""
    active = trace or Trace()
    token = _TRACE.set(active)
    try:
        yield active
    finally:
        _TRACE.reset(token)


@contextmanager
def role(kind: str) -> Iterator[None]:
    """Annotate the calls inside this block as listing or detail work."""
    if kind not in ROLES:
        raise ValueError(f"unknown request role {kind!r}")
    token = _ROLE.set(kind)
    try:
        yield
    finally:
        _ROLE.reset(token)


def detail():
    """Sugar for the provider branches that fetch a single posting."""
    return role("detail")


def mark_attempt(phase: str) -> None:
    """Declare the phase of the NEXT physical request on this context.

    Called from inside the transport loops. Returns immediately when tracing is
    off, which is the default, so the production path is untouched.

    A redirect remembers what it descended from: if the current phase is already
    ``retry``, the redirect's ``origin_attempt`` is ``retry`` and it is counted
    once, as a redirect -- never also as a retry request.
    """
    if _TRACE.get() is None:
        return
    if phase not in PHASES:
        return
    if phase == "redirect":
        previous = _PHASE.get()
        _ORIGIN.set(previous if previous in ORIGINS else "initial")
    _PHASE.set(phase)


def reset() -> None:
    """Clear any pending phase/origin annotation on this context.

    Called at board boundaries so an attempt that was marked but never sent
    (e.g. a fetch that raised between ``mark_retry`` and the request) cannot
    leak its phase onto the next board's first request. A no-op's cost when
    tracing is off is two ``ContextVar.set`` calls.
    """
    _PHASE.set("initial")
    _ORIGIN.set("initial")


def mark_retry() -> None:
    mark_attempt("retry")


def mark_redirect() -> None:
    mark_attempt("redirect")


def mark_initial() -> None:
    mark_attempt("initial")


def classify(board: str = "") -> Optional[str]:
    """Record one physical attempt against the active trace.

    Called by ``RequestBudget`` at the single point every physical request
    passes through, so the classification and the budget see exactly the same
    events and cannot drift apart.
    """
    trace = _TRACE.get()
    if trace is None:
        return None
    phase = _PHASE.get()
    origin = _ORIGIN.get() if phase == "redirect" else "initial"
    name = trace.record(_ROLE.get(), phase, board=board, origin=origin)
    # Each attempt declares its own phase; the next one is an initial request
    # again unless a transport loop says otherwise.
    _PHASE.set("initial")
    _ORIGIN.set("initial")
    return name
