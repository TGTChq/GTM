"""What one acquisition source COST, and whether that cost is known.

Three states, and conflating the last two is how a free lane came to look like a
billing failure:

* **billed** -- a paid provider reported how many rows it charged for. A number.
* **known zero** -- a lane that spends no provider credit by construction. Also a
  number, and it is ``0``: scraping an employer's own careers page or reading a
  public free feed has no per-row charge to report, so the absence of a billing
  total is the expected shape rather than missing evidence.
* **unknown** -- a PAID provider that returned no billing total, or a source whose
  economics this module cannot classify. Only here is ``None`` correct.

The run-level total is therefore a sum whenever every source is in one of the first
two states, and ``None`` only when a genuinely paid source failed to report. The
previous rule -- "no billing total, so the run total is unknown" -- would have
blanked the whole figure the moment the 145 free ATS boards were enabled, which is
a planned change and would have looked like a regression in cost reporting.

Classification is by the ``_acquisition_source`` label the adapters already stamp on
every posting. No company, employer or provider account is consulted.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

#: Labels stamped by the paid Fantastic adapter. A per-source suffix (``::<id>``)
#: is appended for filtered sub-queries, so this is a PREFIX test.
PAID_SOURCE_PREFIXES: Tuple[str, ...] = ("fantastic_jobs",)

#: Lanes that spend no provider credit per row. Direct ATS boards are fetched from
#: each employer's own public board; the free feeds are public endpoints. Both are
#: rate-limited rather than metered, so their per-row cost is zero as a fact about
#: how they work, not as an assumption about a bill we did not see.
FREE_SOURCE_PREFIXES: Tuple[str, ...] = ("ats_",)
FREE_SOURCE_LABELS = frozenset({
    "himalayas", "jobicy", "remotive", "remoteok", "weworkremotely",
})

BILLED = "source_returned_billed"
KNOWN_ZERO = "known_zero_cost_free_lane"
PAID_BILLING_ABSENT = "paid_source_billing_absent"
UNCLASSIFIED = "source_cost_unclassified"


def is_paid_source(label: Any) -> bool:
    text = str(label or "").strip().lower()
    return any(text.startswith(prefix) for prefix in PAID_SOURCE_PREFIXES)


def is_free_source(label: Any) -> bool:
    text = str(label or "").strip().lower()
    return text in FREE_SOURCE_LABELS or any(
        text.startswith(prefix) for prefix in FREE_SOURCE_PREFIXES)


def source_cost(label: Any, counts: Mapping[str, Any]) -> Dict[str, Any]:
    """``{"credits": int|None, "basis": str}`` for one source's per-run counters."""
    if is_paid_source(label):
        if "returned_billed" in counts:
            return {"credits": int(counts.get("returned_billed") or 0), "basis": BILLED}
        return {"credits": None, "basis": PAID_BILLING_ABSENT}
    if is_free_source(label):
        return {"credits": 0, "basis": KNOWN_ZERO}
    # An unrecognised label is NOT assumed free. A source whose economics nobody has
    # stated is exactly the case where inventing a zero would understate cost.
    return {"credits": None, "basis": UNCLASSIFIED}


def costs_by_source(per_source: Optional[Mapping[str, Mapping[str, Any]]]
                    ) -> Dict[str, Dict[str, Any]]:
    return {str(label): source_cost(label, counts or {})
            for label, counts in (per_source or {}).items()}


def billed_by_source(per_source: Optional[Mapping[str, Mapping[str, Any]]]
                     ) -> Dict[str, Optional[int]]:
    """The credits map the yield ledger consumes: ``None`` means genuinely unknown."""
    return {label: cost["credits"] for label, cost in costs_by_source(per_source).items()}


def run_total(costs: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Sum the run's cost, or say which sources make it unknowable.

    A single unknown source makes the TOTAL unknown -- that part is unchanged and
    right, because a sum missing a term is not a smaller sum. What changed is which
    sources produce one.
    """
    unknown = sorted(label for label, cost in costs.items() if cost.get("credits") is None)
    if unknown:
        return {"credits": None, "basis": "incomplete",
                "unknown_cost_sources": unknown,
                "known_zero_cost_sources": sorted(
                    label for label, cost in costs.items() if cost.get("basis") == KNOWN_ZERO)}
    return {"credits": sum(int(cost.get("credits") or 0) for cost in costs.values()),
            "basis": BILLED if any(cost.get("basis") == BILLED for cost in costs.values())
                     else KNOWN_ZERO,
            "unknown_cost_sources": [],
            "known_zero_cost_sources": sorted(
                label for label, cost in costs.items() if cost.get("basis") == KNOWN_ZERO)}
