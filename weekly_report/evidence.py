"""The typed metric contract shared by the weekly report and the future dashboard.

A number without provenance is not reportable. Every metric therefore carries:

* ``value``     -- the number, or ``None`` when it could not be reconstructed;
* ``status``    -- ``measured`` / ``partial`` / ``unavailable`` / ``not_applicable``;
* ``source``    -- the system the number came from (``run_artifacts``, ``instantly``…);
* ``evidence``  -- the exact artifact field(s) read, e.g.
  ``waterfall.json:unit_totals.postings``;
* ``attribution`` -- the timestamp field that placed the number inside the window;
* ``contributing_run_ids`` / ``runs_missing_field`` -- which runs supplied it, and
  which ones were silent.

``partial`` exists because "some runs reported this and some did not" is a
materially different claim from "the total was N". A missing field is never
treated as a zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STATUS_MEASURED = "measured"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_APPLICABLE = "not_applicable"

#: Ordered worst-to-best, used when a derived metric inherits its inputs' status.
_STATUS_RANK = {
    STATUS_UNAVAILABLE: 0,
    STATUS_NOT_APPLICABLE: 1,
    STATUS_PARTIAL: 2,
    STATUS_MEASURED: 3,
}


def weakest(*statuses: str) -> str:
    """The least-confident of the given statuses."""
    if not statuses:
        return STATUS_UNAVAILABLE
    return min(statuses, key=lambda s: _STATUS_RANK.get(s, 0))


@dataclass
class Metric:
    """One reported number and everything needed to defend it."""

    key: str
    label: str
    unit: str
    value: Optional[float] = None
    status: str = STATUS_UNAVAILABLE
    source: str = ""
    definition: str = ""
    evidence: List[str] = field(default_factory=list)
    attribution: str = ""
    contributing_run_ids: List[str] = field(default_factory=list)
    runs_missing_field: List[str] = field(default_factory=list)
    reason: str = ""
    notes: List[str] = field(default_factory=list)
    #: The population this number counts, as a machine key. ``unit`` is a display
    #: word ("posting", "contact") and two metrics can share one while counting
    #: different things; this is the identity a subtraction has to match on.
    #: Empty means "not declared", which is treated as incomparable.
    counted_unit: str = ""
    #: WHERE the population came from. Two counters can share a unit and still be
    #: different cohorts -- this week's contacts and this week's Instantly
    #: enrollments are both people, but the enrollments come from an Approved
    #: backlog accumulated over previous weeks, so their difference is not a loss.
    cohort: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None and self.status in (STATUS_MEASURED, STATUS_PARTIAL)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "value": self.value,
            "status": self.status,
            "source": self.source,
            "definition": self.definition,
            "evidence": list(self.evidence),
            "attribution": self.attribution,
            "contributing_run_ids": list(self.contributing_run_ids),
            # Carried into the JSON so a dashboard cannot repeat the subtraction
            # this report refuses to make.
            "counted_unit": self.counted_unit,
            "cohort": self.cohort,
        }
        if self.runs_missing_field:
            payload["runs_missing_field"] = list(self.runs_missing_field)
        if self.reason:
            payload["reason"] = self.reason
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


def unavailable(
    key: str,
    label: str,
    unit: str,
    *,
    reason: str,
    definition: str = "",
    source: str = "",
    evidence: Optional[List[str]] = None,
    attribution: str = "",
) -> Metric:
    """A metric that could not be reconstructed. Explicitly not zero."""
    return Metric(
        key=key,
        label=label,
        unit=unit,
        value=None,
        status=STATUS_UNAVAILABLE,
        source=source,
        definition=definition,
        evidence=list(evidence or []),
        attribution=attribution,
        reason=reason,
    )


@dataclass
class Gap:
    """A metric or evidence source the report could not stand behind."""

    metric: str
    reason: str
    impact: str
    remedy: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "reason": self.reason,
            "impact": self.impact,
            "remedy": self.remedy,
        }


def dig(payload: Any, path: str) -> Any:
    """Read a dotted path out of nested mappings. Returns ``None`` if absent.

    Deliberately strict about type: a value that is present but not a number is
    surfaced as-is so the caller can reject it, rather than being coerced.
    """
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        if part not in current:
            return None
        current = current[part]
    return current


def as_count(value: Any) -> Optional[int]:
    """Coerce an artifact value to a non-negative count, or ``None``.

    Booleans are rejected: ``True`` is not the number one in this domain.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
