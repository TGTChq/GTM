"""Retrieval Measurement Harness (Milestone 1 candidate).

Answers, for one measured acquisition run:

  * how many postings each source *returned* (gross),
  * how many are unique (two independent uniqueness definitions),
  * how many were already seen (against an optional read-only snapshot),
  * how many would actually enter the funnel (incremental new),
  * and, only where a provider publishes one, what share of that provider's
    own declared inventory we retrieved.

It deliberately does NOT estimate total US market inventory. No such number is
derivable from the data available, and inventing one would be worse than
reporting the gap honestly.

Safety posture
--------------
The harness is measurement-only. It never enriches, never contacts a delivery
service, and never writes outside its own artifact root. ``DeliveryImportGuard``
enforces the first two of those structurally rather than by convention.
"""

from __future__ import annotations

import sys
from types import TracebackType
from typing import Iterable, Optional, Sequence, Type

SCHEMA_VERSION = "1.0.0"
HARNESS_VERSION = "1.0.0-m1-candidate"

#: Modules that reach a paid or outbound service. A measurement run that
#: imports any of these has escaped its remit.
FORBIDDEN_MODULES: frozenset[str] = frozenset({
    "apollo_client",
    "hunter_client",
    "airtable_client",
    "instantly_client",
    "hiring_manager",
    "qualification_pipeline",
    "run_daily",
    "run_approved",
})


class DeliveryImportBlocked(ImportError):
    """Raised when a measurement run tries to import an outbound-service module."""


class _BlockingFinder:
    def __init__(self, blocked: Iterable[str]) -> None:
        self._blocked = frozenset(blocked)

    def find_module(self, fullname: str, path: Optional[Sequence[str]] = None):  # pragma: no cover - legacy API
        self.find_spec(fullname, path)
        return None

    def find_spec(self, fullname: str, path: Optional[Sequence[str]] = None, target=None):
        root = fullname.split(".", 1)[0]
        if root in self._blocked:
            raise DeliveryImportBlocked(
                f"retrieval_measurement blocked import of {fullname!r}: the measurement "
                "harness must never reach an enrichment or delivery service."
            )
        return None


class DeliveryImportGuard:
    """Block imports of outbound-service modules for the duration of a run.

    Modules already imported before the guard is entered are reported rather
    than silently tolerated -- an already-imported delivery module means the
    process was not clean to begin with.
    """

    def __init__(self, blocked: Iterable[str] = FORBIDDEN_MODULES) -> None:
        self.blocked = frozenset(blocked)
        self._finder: Optional[_BlockingFinder] = None

    def preexisting(self) -> list[str]:
        return sorted(name for name in self.blocked if name in sys.modules)

    def __enter__(self) -> "DeliveryImportGuard":
        self._finder = _BlockingFinder(self.blocked)
        sys.meta_path.insert(0, self._finder)
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if self._finder is not None and self._finder in sys.meta_path:
            sys.meta_path.remove(self._finder)
        self._finder = None


__all__ = [
    "SCHEMA_VERSION",
    "HARNESS_VERSION",
    "FORBIDDEN_MODULES",
    "DeliveryImportBlocked",
    "DeliveryImportGuard",
]
