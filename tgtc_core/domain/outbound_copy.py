"""The frozen Challenger copy contract, checked before any lead creation.

The nine live Challenger campaigns hold no literal copy. Every step body is
exactly ``{{rendered_email_N_html}}`` plus ``{{accountSignature}}``, and step 1's
subject is ``{{rendered_subject}}`` (steps 2-4 are intentionally subject-less
same-thread follow-ups). A lead enrolled without those variables therefore
receives an empty subject and a signature-only body -- the 2026-10-02 incident.

So the copy is not decoration on the payload: it IS the email. This module
renders it from the lead's own approved facts using the frozen Wave 1 renderer,
and refuses any Challenger payload that is not complete.

``outbound_wave1`` is a pure renderer (stdlib only, plus one read of the static
claim registry), not the legacy orchestrator: no I/O, no provider calls, no
account assignment. Control campaigns keep their live static templates and are
not judged against this contract.
"""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Mapping

from ..policy.campaigns import KNOWN_CHALLENGER_CAMPAIGN_IDS

#: Every one of these must be present, non-empty, fully resolved and carry
#: visible text before a Challenger lead may be created.
REQUIRED_COPY_FIELDS = ("rendered_subject", *(f"rendered_email_{i}_html" for i in range(1, 5)))

#: The copy and provenance variables a Challenger enrolment may carry, on top of
#: ``approval._CUSTOM_VARIABLE_NAMES``. Declared here so the rule that nothing
#: undocumented reaches the provider keeps holding after copy is added.
#: Legacy A/B assignment fields are deliberately absent: see ``rendered_variables``.
CHALLENGER_COPY_VARIABLE_NAMES = (
    *REQUIRED_COPY_FIELDS,
    "rendered_email_1", "rendered_email_2", "rendered_email_3", "rendered_email_4",
    "wave1_campaign", "signal_tier", "signal_type", "friction_angle", "proof_type",
    "claim_source", "outbound_offer_type", "offer_noun", "offer_class",
    "offer_fallback_type", "copy_version", "role_page_match",
)

#: Never manufactured by the rebuilt core: routing is decided by core policy, so
#: emitting these would assert an experiment arm that was never randomised here.
_LEGACY_EXPERIMENT_FIELDS = ("experiment_id", "experiment_arm", "company_assignment_key")


class _VisibleText(HTMLParser):
    """Text a human would actually read: markup and hidden elements contribute none."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _visible(value: str) -> str:
    parser = _VisibleText()
    parser.feed(value)
    parser.close()
    return " ".join(parser.parts).strip()


def copy_block_reason(payload: Mapping[str, Any]) -> str:
    """A named refusal, or an empty string when the copy contract passes.

    Judges a payload exactly as stored, so it protects both a fresh enrolment and
    a pending outbox row signed before this contract existed.
    """
    if payload.get("campaign") not in KNOWN_CHALLENGER_CAMPAIGN_IDS:
        return ""
    variables = payload.get("custom_variables")
    if not isinstance(variables, dict):
        return "challenger_copy_missing_variables"
    for key in REQUIRED_COPY_FIELDS:
        value = variables.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"challenger_copy_missing:{key}"
        if "{{" in value or "}}" in value:
            return f"challenger_copy_unresolved:{key}"
        if not _visible(value):
            return f"challenger_copy_empty:{key}"
    return ""


def rendered_variables(lead: Mapping[str, Any], fields: dict) -> dict:
    """Render this lead's approved copy from its own signed facts.

    No new copy, no new claims, no enrichment call and no account assignment: the
    campaign was already chosen by core policy. The renderer's existing
    structural QA must pass, and the result must satisfy the same contract the
    provider and the outbox enforce -- so this function either returns complete
    copy or raises a named failure. It never returns a partial set.
    """
    from outbound_wave1.resolver import resolve_challenger

    as_of = datetime.fromisoformat(str(lead["approved_at"]).replace("Z", "+00:00"))
    resolution = resolve_challenger(fields, as_of=as_of)
    if not resolution.eligible or not resolution.qa_pass:
        reasons = resolution.qa_reasons or [resolution.ineligible_reason]
        raise ValueError("challenger_copy_qa_failed:" + ",".join(str(r) for r in reasons if r))
    variables = resolution.to_custom_variables()
    for key in _LEGACY_EXPERIMENT_FIELDS:
        variables.pop(key, None)
    undocumented = set(variables) - set(CHALLENGER_COPY_VARIABLE_NAMES)
    if undocumented:
        raise ValueError("challenger_copy_undocumented_variables:" + ",".join(sorted(undocumented)))
    reason = copy_block_reason({"campaign": lead["campaign_id"], "custom_variables": variables})
    if reason:
        raise ValueError(reason)
    return variables
