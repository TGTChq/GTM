"""Segment credit-allocation FOUNDATIONS (Category 2; default OFF = broad).

Deliberately NOT a multi-armed bandit. It provides only:

* a historical yield LOOKUP by segment (read from the yield-ledger aggregate or a
  persisted table; never hard-coded from one run);
* a configurable segment PRIORITY list;
* a count-informed inventory HINT interface;
* a single ``allocate()`` entry point whose DEFAULT returns the whole budget to
  the one broad query (exactly today's behavior).

Future modes may split a finite daily budget toward higher-yield segments; that
activation is gated by ``config.SEGMENT_ALLOCATOR_ENABLED`` and requires evidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

BROAD_SEGMENT = "__broad__"


@dataclass(frozen=True)
class Segment:
    id: str
    yield_estimate: Optional[float] = None   # net-new send-safe / credit
    inventory_hint: Optional[int] = None     # from a 0-credit count endpoint
    priority: int = 0                        # higher first (config-driven)
    sample_credits: int = 0                  # evidence size behind the estimate


@dataclass
class Allocation:
    mode: str
    grants: Dict[str, int] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "grants": dict(self.grants), "detail": dict(self.detail)}


def load_yield_table(path: str) -> Dict[str, Dict[str, Any]]:
    """Persisted per-segment yields (e.g. produced offline from the yield ledger).
    Missing/corrupt => empty (the allocator then has no evidence and stays broad)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def allocate(budget: int, segments: List[Segment], *, enabled: bool,
             min_evidence_credits: int = 500) -> Allocation:
    """Default (``enabled=False`` or insufficient evidence): ONE broad grant equal
    to the budget -- preserves current acquisition exactly.

    Enabled mode (weighted): segments with enough evidence are weighted by
    yield_estimate, clamped by inventory hints; any unallocated remainder goes to
    the broad query so coverage never collapses to a few segments."""
    budget = max(0, int(budget))
    if not enabled or not segments:
        return Allocation(mode="broad", grants={BROAD_SEGMENT: budget})
    evidenced = [s for s in segments if (s.yield_estimate is not None
                                         and s.sample_credits >= min_evidence_credits)]
    if not evidenced:
        return Allocation(mode="broad", grants={BROAD_SEGMENT: budget},
                          detail={"reason": "insufficient_evidence"})
    ordered = sorted(evidenced, key=lambda s: (-s.priority, -(s.yield_estimate or 0.0)))
    total_w = sum(max(0.0, s.yield_estimate or 0.0) for s in ordered) or 1.0
    grants: Dict[str, int] = {}
    used = 0
    for s in ordered:
        share = int(budget * max(0.0, s.yield_estimate or 0.0) / total_w)
        if s.inventory_hint is not None:
            share = min(share, max(0, int(s.inventory_hint)))
        share = min(share, budget - used)
        if share > 0:
            grants[s.id] = share
            used += share
    if budget - used > 0:
        grants[BROAD_SEGMENT] = budget - used
    return Allocation(mode="weighted", grants=grants,
                      detail={"evidenced_segments": [s.id for s in ordered]})


def segments_from_table(table: Dict[str, Dict[str, Any]], priority: List[str]) -> List[Segment]:
    prio = {sid: len(priority) - i for i, sid in enumerate(priority or [])}
    out: List[Segment] = []
    for sid, row in (table or {}).items():
        try:
            out.append(Segment(id=str(sid), yield_estimate=float(row.get("yield")) if row.get("yield") is not None else None,
                               inventory_hint=(int(row["inventory_hint"]) if row.get("inventory_hint") is not None else None),
                               priority=int(prio.get(sid, 0)), sample_credits=int(row.get("credits", 0) or 0)))
        except (TypeError, ValueError):
            continue
    return out
