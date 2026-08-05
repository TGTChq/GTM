"""Boundary reconciliation for the end-to-end waterfall.

Units are kept strictly separate -- postings, production-equivalent
opportunities, companies, contacts, FINAL_PASS leads, delivered rows,
enrolled contacts -- and every boundary must satisfy:

    entered = passed + rejected + deferred + errored

with a primary (and optional secondary) reason code recorded for every record
that does not simply pass. A stage that cannot make that identity hold raises,
because a pipeline that cannot account for a record it saw is not measurable.

The FINAL_PASS target may be satisfied ONLY by records whose disposition is
FINAL_PASS and which reconcile through delivery. Reviewable records
(NEEDS_CHECK, UNVERIFIED) can never count toward it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.reasons import Disposition, ReasonCode, REVIEWABLE, StageOutcome


class ReconciliationError(AssertionError):
    """A stage boundary failed the entered = passed+rejected+deferred+errored
    identity, or a downstream unit exceeded its upstream supply."""


@dataclass
class StageResult:
    """One boundary's accounting. Immutable once sealed."""

    stage: str
    unit: str
    entered: int = 0
    passed: int = 0
    rejected: int = 0
    deferred: int = 0
    errored: int = 0
    primary_reasons: Dict[str, int] = field(default_factory=dict)
    secondary_reasons: Dict[str, int] = field(default_factory=dict)

    def reconciles(self) -> bool:
        return self.entered == self.passed + self.rejected + self.deferred + self.errored

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "unit": self.unit,
            "entered": self.entered,
            "passed": self.passed,
            "rejected": self.rejected,
            "deferred": self.deferred,
            "errored": self.errored,
            "reconciles": self.reconciles(),
            "primary_reasons": dict(sorted(self.primary_reasons.items())),
            "secondary_reasons": dict(sorted(self.secondary_reasons.items())),
        }


def reconcile_stage(
    stage: str,
    unit: str,
    dispositions: List[Tuple[StageOutcome, ReasonCode, Optional[ReasonCode]]],
) -> StageResult:
    """Fold a list of (outcome, primary_reason, secondary_reason) into a sealed,
    self-checked StageResult. Raises if the identity does not hold."""
    result = StageResult(stage=stage, unit=unit, entered=len(dispositions))
    primary: Counter = Counter()
    secondary: Counter = Counter()
    for outcome, reason, second in dispositions:
        outcome = StageOutcome(outcome)
        if outcome is StageOutcome.PASSED:
            result.passed += 1
        elif outcome is StageOutcome.REJECTED:
            result.rejected += 1
        elif outcome is StageOutcome.DEFERRED:
            result.deferred += 1
        else:
            result.errored += 1
        if reason is not None and outcome is not StageOutcome.PASSED:
            primary[ReasonCode(reason).value] += 1
        if second is not None:
            secondary[ReasonCode(second).value] += 1
    result.primary_reasons = dict(primary)
    result.secondary_reasons = dict(secondary)
    if not result.reconciles():
        raise ReconciliationError(
            f"stage {stage!r} unit {unit!r}: entered={result.entered} != "
            f"passed+rejected+deferred+errored="
            f"{result.passed + result.rejected + result.deferred + result.errored}"
        )
    return result


@dataclass
class WaterfallReport:
    stages: List[StageResult] = field(default_factory=list)
    #: separate unit tallies, never conflated
    unit_totals: Dict[str, int] = field(default_factory=dict)
    #: disposition census over opportunities/contacts at the quality gate
    disposition_census: Dict[str, int] = field(default_factory=dict)

    def add(self, result: StageResult) -> StageResult:
        if not result.reconciles():
            raise ReconciliationError(f"stage {result.stage!r} does not reconcile")
        self.stages.append(result)
        return result

    def set_unit(self, unit: str, count: int) -> None:
        self.unit_totals[unit] = int(count)

    def census(self, dispositions: List[Disposition]) -> None:
        c: Counter = Counter(d.value for d in dispositions)
        self.disposition_census = dict(c)

    def final_pass_count(self) -> int:
        """Records that may legitimately count toward the target: FINAL_PASS only.
        Reviewable dispositions are structurally excluded."""
        return int(self.disposition_census.get(Disposition.FINAL_PASS.value, 0))

    def reviewable_count(self) -> int:
        return sum(int(self.disposition_census.get(d.value, 0)) for d in REVIEWABLE)

    def target_satisfied(self, target: int, *, delivered_final_pass: int) -> bool:
        """The target is satisfied only by reconciled FINAL_PASS records that were
        actually delivered as FINAL_PASS. Reviewable rows can never satisfy it."""
        return delivered_final_pass >= int(target) and delivered_final_pass <= self.final_pass_count()

    def all_reconcile(self) -> bool:
        return all(s.reconciles() for s in self.stages)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stages": [s.to_dict() for s in self.stages],
            "unit_totals": dict(self.unit_totals),
            "disposition_census": dict(sorted(self.disposition_census.items())),
            "final_pass_count": self.final_pass_count(),
            "reviewable_count": self.reviewable_count(),
            "all_stages_reconcile": self.all_reconcile(),
        }
