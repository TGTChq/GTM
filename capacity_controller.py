"""Pre-contact capacity controller (Phase 13 section 5).

A real decision-and-accounting component that drives acquisition/recovery
toward a *searchable-company* target BEFORE the expensive contact-enrichment
stage, rather than only reporting after the fact. It:

- counts unique **canonical** ICP searchable companies, so the same company
  seen across sources / cycles / recovery lanes is never double-counted;
- computes the deficit against a daily target and a preferred headroom target;
- selects the next quality-safe strategy from an ordered ladder, skipping
  strategies that are disabled, exhausted, or out of budget;
- stops only when the target/headroom is met, every enabled strategy is
  exhausted, or a real budget/runtime/provider guard is hit -- always with an
  explicit stop reason.

Default OFF: ``config.CAPACITY_CONTROLLER_ENABLED`` gates activation so
deployment does not silently change acquisition behavior. When disabled, the
controller is pure observability; when enabled, ``run_until_target`` drives the
supplied strategy runners.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

# Ordered quality-safe strategy ladder (Phase 13 section 5).
STRATEGY_LADDER: List[str] = [
    "upstream_inventory",
    "base_multi_source",
    "direct_ats",
    "public_feeds",
    "domain_recovery",
    "jsearch_base_expansion",
    "jsearch_topup",
    "adzuna_expansion",
    "age_recovery_15_30",
    "age_recovery_31_60",
    "age_recovery_61_90",
    "remaining_pagination",
]


@dataclass
class CapacityController:
    target: int = 250
    headroom_target: int = 300
    enabled: bool = False
    # canonical company keys already counted as searchable this run
    _searchable: Set[str] = field(default_factory=set)
    _domain_resolved: Set[str] = field(default_factory=set)
    _icp_companies: Set[str] = field(default_factory=set)
    _exhausted: Set[str] = field(default_factory=set)
    _providers_unavailable: Set[str] = field(default_factory=set)
    stop_reason: str = ""

    # ---- accounting (canonical, no double counting) ----
    def register_searchable(self, canonical_keys: Iterable[str]) -> int:
        before = len(self._searchable)
        for key in canonical_keys:
            k = str(key or "").strip()
            if k:
                self._searchable.add(k)
        return len(self._searchable) - before

    def register_domain_resolved(self, canonical_keys: Iterable[str]) -> None:
        for key in canonical_keys:
            k = str(key or "").strip()
            if k:
                self._domain_resolved.add(k)

    def register_icp_company(self, canonical_keys: Iterable[str]) -> None:
        for key in canonical_keys:
            k = str(key or "").strip()
            if k:
                self._icp_companies.add(k)

    def mark_exhausted(self, strategy: str) -> None:
        self._exhausted.add(strategy)

    def mark_provider_unavailable(self, provider: str) -> None:
        self._providers_unavailable.add(provider)

    # ---- computed state ----
    @property
    def searchable_count(self) -> int:
        return len(self._searchable)

    @property
    def deficit(self) -> int:
        return max(0, self.target - self.searchable_count)

    @property
    def headroom_deficit(self) -> int:
        return max(0, self.headroom_target - self.searchable_count)

    def target_met(self) -> bool:
        return self.searchable_count >= self.target

    def headroom_met(self) -> bool:
        return self.searchable_count >= self.headroom_target

    def remaining_strategies(self, available: Optional[Iterable[str]] = None) -> List[str]:
        avail = set(available) if available is not None else set(STRATEGY_LADDER)
        return [s for s in STRATEGY_LADDER if s in avail and s not in self._exhausted]

    def next_strategy(
        self,
        *,
        available_strategies: Optional[Iterable[str]] = None,
        budget_ok: Optional[Callable[[str], bool]] = None,
    ) -> Optional[str]:
        """Return the next quality-safe strategy to run, or None (with
        ``stop_reason`` set) when the controller should stop."""
        if self.headroom_met():
            self.stop_reason = "headroom_target_met"
            return None
        if self.target_met() and available_strategies is None:
            # target met and no explicit further strategies to try
            self.stop_reason = "capacity_target_met"
            return None
        for strat in self.remaining_strategies(available_strategies):
            if budget_ok is not None and not budget_ok(strat):
                continue
            return strat
        self.stop_reason = (
            "capacity_target_met" if self.target_met() else "all_strategies_exhausted"
        )
        return None

    def state(self, *, remaining_budgets: Optional[Dict[str, object]] = None,
              recovery_inventory_available: Optional[int] = None) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "searchable_company_target": self.target,
            "searchable_company_headroom_target": self.headroom_target,
            "searchable_companies_available": self.searchable_count,
            "searchable_company_deficit": self.deficit,
            "searchable_company_headroom_deficit": self.headroom_deficit,
            "unique_canonical_icp_companies": len(self._icp_companies),
            "domain_resolved_companies": len(self._domain_resolved),
            "remaining_acquisition_strategies": self.remaining_strategies(),
            "remaining_query_page_budgets": dict(remaining_budgets or {}),
            "remaining_recovery_inventory": recovery_inventory_available,
            "source_exhausted": sorted(self._exhausted),
            "providers_unavailable": sorted(self._providers_unavailable),
            "target_met": self.target_met(),
            "headroom_met": self.headroom_met(),
            "stop_reason": self.stop_reason,
        }

    # ---- orchestration (offline-testable via injected runners) ----
    def run_until_target(
        self,
        strategy_runners: Dict[str, Callable[[], Iterable[str]]],
        *,
        budget_ok: Optional[Callable[[str], bool]] = None,
        max_cycles: int = 24,
        guard: Optional[Callable[[], Optional[str]]] = None,
    ) -> Dict[str, object]:
        """Drive strategies until the headroom target is met, strategies are
        exhausted, or a guard fires. Each runner returns the canonical company
        keys it newly made searchable. Runners are injected so this is fully
        exercisable offline with mocks and makes no live call itself.
        """
        if not self.enabled:
            self.stop_reason = "controller_disabled"
            return self.state()
        cycles = 0
        while cycles < max_cycles:
            cycles += 1
            if guard is not None:
                g = guard()
                if g:
                    self.stop_reason = g
                    break
            strat = self.next_strategy(
                available_strategies=list(strategy_runners.keys()), budget_ok=budget_ok,
            )
            if strat is None:
                break
            runner = strategy_runners.get(strat)
            if runner is None:
                self.mark_exhausted(strat)
                continue
            added = self.register_searchable(runner() or [])
            if added <= 0:
                # This strategy produced no new canonical searchable company;
                # it is exhausted for this run.
                self.mark_exhausted(strat)
        else:
            self.stop_reason = self.stop_reason or "max_cycles_reached"
        state = self.state()
        state["cycles_run"] = cycles
        return state


def build_from_config(config_module) -> CapacityController:
    return CapacityController(
        target=int(getattr(config_module, "SEARCHABLE_COMPANY_DAILY_TARGET", 250)),
        headroom_target=int(getattr(config_module, "SEARCHABLE_COMPANY_HEADROOM_TARGET", 300)),
        enabled=bool(getattr(config_module, "CAPACITY_CONTROLLER_ENABLED", False)),
    )
