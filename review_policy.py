"""Operational review policy for volume-first Airtable delivery.

Hard rejects remain terminal. Missing or incomplete verification is surfaced to
human review instead of silently removing an otherwise actionable lead.
"""

from __future__ import annotations

from typing import Dict, Iterable


AIRTABLE_REVIEW_STATES = {"FINAL_PASS", "NEEDS_CHECK", "UNVERIFIED"}
TERMINAL_STATES = {"REJECT", "REROUTE"}


def gate_states(lead: Dict) -> Iterable[str]:
    decisions = lead.get("_gate_decisions") or {}
    if isinstance(decisions, dict):
        for payload in decisions.values():
            if isinstance(payload, dict):
                state = str(payload.get("state") or "").strip().upper()
                if state:
                    yield state
    for gate in ("job", "role", "account", "contact", "email"):
        state = str(lead.get(f"_{gate}_gate_state") or "").strip().upper()
        if state:
            yield state


def has_hard_reject(lead: Dict) -> bool:
    """Return True for any terminal gate outcome, including reroute failures."""
    final_state = str(lead.get("_final_state") or "").strip().upper()
    if final_state in TERMINAL_STATES:
        return True
    return any(state in TERMINAL_STATES for state in gate_states(lead))


def has_usable_contact(lead: Dict) -> bool:
    email = str(lead.get("hiring_manager_email") or "").strip()
    lead_key = str(lead.get("lead_key") or "").strip()
    person = str(
        lead.get("hiring_manager_name")
        or lead.get("hiring_manager_person_id")
        or lead.get("hiring_manager_linkedin")
        or ""
    ).strip()
    return bool(email and lead_key and person)


def is_airtable_reviewable(lead: Dict) -> bool:
    """Allow actionable uncertainty; block only terminal/hard failures."""
    state = str(lead.get("_final_state") or "").strip().upper()
    if state not in AIRTABLE_REVIEW_STATES:
        return False
    if has_hard_reject(lead):
        return False
    return has_usable_contact(lead)


def review_reasons(lead: Dict) -> list[str]:
    reasons: list[str] = []
    for key in ("_final_primary_reason",):
        value = str(lead.get(key) or "").strip()
        if value and value not in {"FINAL_PASS", ""}:
            reasons.append(value)
    for value in lead.get("_final_secondary_reasons") or []:
        text = str(value or "").strip()
        if text and text not in reasons and text != "FINAL_PASS":
            reasons.append(text)
    return reasons
