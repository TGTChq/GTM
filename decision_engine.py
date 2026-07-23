"""Conjunctive final decision engine."""

from __future__ import annotations

from typing import Dict, Iterable

from validation_integrity import utc_now_iso

from decision_types import FinalDecision, FinalState, GateDecision, GateState, stable_unique
from reason_codes import ReasonCode


_STATE_PRECEDENCE = {
    GateState.REJECT.value: 5,
    GateState.UNVERIFIED.value: 4,
    GateState.REROUTE.value: 3,
    GateState.NEEDS_CHECK.value: 2,
    GateState.PASS.value: 1,
}


def decide(gates: Dict[str, GateDecision]) -> FinalDecision:
    """Combine gate decisions without averaging or score compensation."""
    if not gates:
        return FinalDecision(
            state=FinalState.UNVERIFIED,
            primary_reason="UNVERIFIED_NO_GATE_DECISIONS",
            next_action="discard_and_replace",
        )

    ordered = sorted(
        gates.values(),
        key=lambda item: _STATE_PRECEDENCE.get(item.state_value, 99),
        reverse=True,
    )
    dominant = ordered[0]
    secondaries = stable_unique(
        reason
        for item in ordered
        for reason in [item.primary_reason, *item.secondary_reasons]
        if item is not dominant or reason != dominant.primary_reason
    )

    if dominant.state_value == GateState.REJECT.value:
        return FinalDecision(
            FinalState.REJECT,
            dominant.primary_reason,
            secondaries,
            gates,
            "discard_and_replace",
        )
    if dominant.state_value == GateState.UNVERIFIED.value:
        return FinalDecision(
            FinalState.UNVERIFIED,
            dominant.primary_reason,
            secondaries,
            gates,
            "retry_bounded_fallbacks_then_replace" if dominant.retryable else "discard_and_replace",
        )
    if dominant.state_value == GateState.REROUTE.value:
        return FinalDecision(
            FinalState.REROUTE,
            dominant.primary_reason,
            secondaries,
            gates,
            "reroute_contact",
        )
    if dominant.state_value == GateState.NEEDS_CHECK.value:
        # NEEDS_CHECK is valid only when every other gate passed.  The precedence
        # ordering above already ensures a reject/unknown/reroute would dominate.
        return FinalDecision(
            FinalState.NEEDS_CHECK,
            dominant.primary_reason,
            secondaries,
            gates,
            "write_review_and_continue_topup",
        )

    if all(item.state_value == GateState.PASS.value for item in gates.values()):
        return FinalDecision(
            FinalState.FINAL_PASS,
            ReasonCode.FINAL_PASS,
            secondaries,
            gates,
            "write_accept",
        )

    return FinalDecision(
        FinalState.UNVERIFIED,
        "UNVERIFIED_UNRECOGNIZED_GATE_STATE",
        secondaries,
        gates,
        "discard_and_replace",
    )


def annotate_final_decision(lead: dict, gates: Dict[str, GateDecision]) -> dict:
    final = decide(gates)
    result = dict(lead)
    payload = final.to_dict()
    retryable = any(bool(item.retryable) for item in gates.values())
    operational_state = (
        "READY"
        if payload["state"] == FinalState.FINAL_PASS.value
        else "RETRY"
        if retryable or payload["state"] == FinalState.REROUTE.value
        else "REJECTED"
    )
    result.update(
        {
            "_final_state": payload["state"],
            "_final_primary_reason": payload["primary_reason"],
            "_final_secondary_reasons": payload["secondary_reasons"],
            "_final_next_action": payload["next_action"],
            "_counts_toward_target": payload["counts_toward_target"],
            "_airtable_relevance": payload["airtable_relevance"],
            "_validation_version": payload["validation_version"],
            "_validation_timestamp": lead.get("_validation_timestamp") or utc_now_iso(),
            "_gate_decisions": payload["gate_decisions"],
            "_operational_state": operational_state,
        }
    )
    return result
