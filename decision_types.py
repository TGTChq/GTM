"""Shared gate and final-decision contracts.

The final state is deliberately conjunctive: a positive score cannot compensate
for a hard veto or a critical unknown in another gate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List

import config
from evidence_types import EvidenceBundle
from reason_codes import ReasonCode, reason_value


VALIDATION_VERSION = config.VALIDATION_VERSION


class GateState(str, Enum):
    PASS = "PASS"
    NEEDS_CHECK = "NEEDS_CHECK"
    REROUTE = "REROUTE"
    UNVERIFIED = "UNVERIFIED"
    REJECT = "REJECT"


class FinalState(str, Enum):
    FINAL_PASS = "FINAL_PASS"
    NEEDS_CHECK = "NEEDS_CHECK"
    REROUTE = "REROUTE"
    UNVERIFIED = "UNVERIFIED"
    REJECT = "REJECT"


@dataclass
class GateDecision:
    gate: str
    state: GateState | str
    primary_reason: ReasonCode | str
    secondary_reasons: List[ReasonCode | str] = field(default_factory=list)
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)
    contradictions: List[Dict[str, Any]] = field(default_factory=list)
    retryable: bool = False
    next_action: str = "continue"
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def state_value(self) -> str:
        return self.state.value if isinstance(self.state, GateState) else str(self.state)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate": self.gate,
            "state": self.state_value,
            "primary_reason": reason_value(self.primary_reason),
            "secondary_reasons": [reason_value(value) for value in self.secondary_reasons],
            "evidence": self.evidence.to_dict(),
            "contradictions": list(self.contradictions),
            "retryable": bool(self.retryable),
            "next_action": self.next_action,
            "metadata": dict(self.metadata),
            "validation_version": VALIDATION_VERSION,
        }


@dataclass
class FinalDecision:
    state: FinalState | str
    primary_reason: ReasonCode | str
    secondary_reasons: List[ReasonCode | str] = field(default_factory=list)
    gate_decisions: Dict[str, GateDecision] = field(default_factory=dict)
    next_action: str = ""
    validation_version: str = VALIDATION_VERSION

    @property
    def state_value(self) -> str:
        return self.state.value if isinstance(self.state, FinalState) else str(self.state)

    @property
    def counts_toward_target(self) -> bool:
        return self.state_value == FinalState.FINAL_PASS.value

    @property
    def airtable_relevance(self) -> str | None:
        if self.state_value == FinalState.FINAL_PASS.value:
            return "accept"
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state_value,
            "primary_reason": reason_value(self.primary_reason),
            "secondary_reasons": [reason_value(value) for value in self.secondary_reasons],
            "gate_decisions": {
                name: decision.to_dict() for name, decision in self.gate_decisions.items()
            },
            "next_action": self.next_action,
            "counts_toward_target": self.counts_toward_target,
            "airtable_relevance": self.airtable_relevance,
            "validation_version": self.validation_version,
        }


def gate_state(value: GateState | str) -> str:
    return value.value if isinstance(value, GateState) else str(value)


def stable_unique(values: Iterable[ReasonCode | str]) -> List[str]:
    seen: set[str] = set()
    output: List[str] = []
    for value in values:
        text = reason_value(value)
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def gate_decision_from_dict(payload: Dict[str, Any] | None, *, gate: str = "") -> GateDecision:
    """Rehydrate a persisted gate decision without trusting arbitrary objects."""
    payload = payload or {}
    state_text = str(payload.get("state") or GateState.UNVERIFIED.value)
    try:
        state: GateState | str = GateState(state_text)
    except ValueError:
        state = state_text
    facts_payload = ((payload.get("evidence") or {}).get("facts") or {})
    bundle = EvidenceBundle()
    # Persisted evidence is retained as metadata when exact dataclass
    # reconstruction is unnecessary for final combination.
    bundle.notes.append("rehydrated_from_persisted_gate_decision")
    return GateDecision(
        gate=str(payload.get("gate") or gate),
        state=state,
        primary_reason=str(payload.get("primary_reason") or "UNVERIFIED_MISSING_REASON"),
        secondary_reasons=[str(v) for v in payload.get("secondary_reasons") or []],
        evidence=bundle,
        contradictions=list(payload.get("contradictions") or []),
        retryable=bool(payload.get("retryable")),
        next_action=str(payload.get("next_action") or ""),
        metadata={
            **dict(payload.get("metadata") or {}),
            "persisted_evidence": facts_payload,
        },
    )
