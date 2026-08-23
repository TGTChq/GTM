"""Deterministic recovery of outbound-display holds that are resolver artifacts
rather than genuine identity/role ambiguity.

Two production hold rules are *stricter than their own evidence*:

1. ``company_display_resolver`` short-circuits on ``linkedin_slug_domain_disagreement``
   BEFORE it ever consults how strongly the chosen display name is corroborated.
   ``_anchors_conflict`` is a crude string test between the LinkedIn slug and the
   e-mail domain brand, and it fires on abbreviated domains (``occidentalmanagement``
   vs ``occmgmt``), brand-vs-legal-entity pairs (``biomedgps`` vs ``smarttrak``) and
   sub-4-character brands (``hextechnologies`` vs ``hex``). None of those make the
   *name* wrong. When the display name EXACTLY equals one of the two stable identity
   anchors, the name is corroborated by identity and the residual disagreement is
   about the other anchor only.

2. ``role_display_resolver`` holds on ``competing_role_heads`` /
   ``matched_role_secondary_or_competing_head`` to avoid picking the wrong head out
   of a multi-head posting title. But the catalog ``Matched Role`` is not a guess --
   it is the role the row was already qualified, bucketed and HM-searched on. Using
   the resolver's OWN anchor renderer against that role is extraction, not choice.

Both recoveries are evidence-bounded and fail closed:

* Nothing is invented. A recovered company name is a name already stored on the row;
  a recovered role must be a contiguous substring of the posting title.
* Corroboration must be EXACT. Prefix/extension/partial matches stay held.
* A cross-function or evidence-less row is never recovered.

This module is pure: no network, no Airtable, no mutation. Callers decide what to
write, and only ``HIGH`` classifications are ever applied.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import company_display_resolver as _cdr
import role_display_resolver as _rdr

RECOVERY_VERSION = "delivery-recovery/1"

# --- classification labels -------------------------------------------------
CANONICAL_EXACT_CORROBORATION = "CANONICAL_EXACT_CORROBORATION"
LINKEDIN_DOMAIN_CORROBORATION = "LINKEDIN_DOMAIN_CORROBORATION"
SAFE_SUFFIX_NORMALIZATION_CORROBORATED = "SAFE_SUFFIX_NORMALIZATION_CORROBORATED"
PARENT_SUBSIDIARY_AMBIGUOUS = "PARENT_SUBSIDIARY_AMBIGUOUS"
MULTI_BRAND_AMBIGUOUS = "MULTI_BRAND_AMBIGUOUS"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
MISSING_EVIDENCE = "MISSING_EVIDENCE"

ROLE_ANCHOR_EXTRACTION = "ROLE_ANCHOR_EXTRACTION"
ROLE_CROSS_FUNCTION_AMBIGUOUS = "ROLE_CROSS_FUNCTION_AMBIGUOUS"
ROLE_NO_CATALOG_ANCHOR = "ROLE_NO_CATALOG_ANCHOR"
ROLE_RENDER_NOT_EXTRACTIVE = "ROLE_RENDER_NOT_EXTRACTIVE"

HIGH, MEDIUM, NONE = "HIGH", "MEDIUM", "NONE"


@dataclass
class RecoveryProposal:
    """One row's recovery verdict. ``patch`` is empty unless ``confidence`` is HIGH."""

    classification: str
    confidence: str
    reason: str
    patch: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def applicable(self) -> bool:
        return self.confidence == HIGH and bool(self.patch)


_EVIDENCE_MAX = 95_000


def _dump_evidence(payload: Dict[str, Any]) -> str:
    """Serialize resolver evidence, shedding bulk rather than truncating.

    Airtable long-text fields cap out, and a truncated JSON string is unparseable --
    which would silently blind every later read of this row's evidence. Drop the
    bulky candidate list instead so the result is always valid JSON.
    """
    text = json.dumps(payload, sort_keys=True)
    if len(text) <= _EVIDENCE_MAX:
        return text
    trimmed = {k: v for k, v in payload.items() if k != "candidates"}
    trimmed["candidates_omitted"] = True
    text = json.dumps(trimmed, sort_keys=True)
    return text if len(text) <= _EVIDENCE_MAX else json.dumps(
        {k: payload.get(k) for k in ("recovery_version", "recovery_classification",
                                     "recovery_reason", "identity_keys")},
        sort_keys=True,
    )


def _load_evidence(fields: Dict[str, Any], key: str) -> Dict[str, Any]:
    raw = fields.get(key) or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _identity_anchors(evidence: Dict[str, Any]) -> tuple[str, str]:
    """Return ``(slug_anchor, domain_brand)`` from the stored resolver identity keys.

    These are the values production already normalized, so recovery compares exactly
    what the hold decision compared.
    """
    slug = domain = ""
    for key in evidence.get("identity_keys") or []:
        text = str(key)
        if text.startswith("linkedin:") and not slug:
            slug = text.split(":", 1)[1]
        elif text.startswith("domain:") and not domain:
            domain = text.split(":", 1)[1]
    return slug, _cdr._domain_brand(domain)


_AMPERSAND_RE = re.compile(r"\s*&\s*")


def _name_keys(name: str) -> set[str]:
    """Every deterministic normalization of a display name.

    ``_name_key`` drops ``&`` entirely (``EKI Environment & Water`` ->
    ``ekienvironmentwater``) while LinkedIn spells it out
    (``ekienvironmentandwater``). That is a normalization artifact, not a different
    company, so the ampersand-expanded key is compared too. Legal suffixes
    (``Inc.``, ``LLC``) are dropped as production already does.
    """
    expanded = _AMPERSAND_RE.sub(" and ", str(name or ""))
    keys = set()
    for value in (name, expanded):
        for drop_legal in (False, True):
            key = _cdr._name_key(value, drop_legal=drop_legal)
            if key:
                keys.add(key)
    return keys


def classify_company_hold(fields: Dict[str, Any]) -> RecoveryProposal:
    """Classify one company-side hold using only stored evidence."""
    evidence = _load_evidence(fields, "Outbound Company Evidence")
    reasons = [str(r) for r in (evidence.get("reasons") or [])]
    name = str(fields.get("Outbound Company") or "").strip()
    slug, domain_brand = _identity_anchors(evidence)
    base = {
        "resolver_reasons": reasons[:3],
        "slug_anchor": slug,
        "domain_anchor": domain_brand,
        "display_name": name,
    }

    if not name:
        return RecoveryProposal(MISSING_EVIDENCE, NONE, "no_stored_display_name", evidence=base)
    if not slug and not domain_brand:
        return RecoveryProposal(
            MISSING_EVIDENCE, NONE, "no_stable_linkedin_or_domain_identity", evidence=base
        )

    # A name-level ambiguity is about the NAME itself; identity corroboration of the
    # other anchor cannot repair it. These stay held.
    if "unresolved_multi_entity_or_franchise_name" in reasons:
        return RecoveryProposal(
            PARENT_SUBSIDIARY_AMBIGUOUS, NONE, "multi_entity_or_franchise_name", evidence=base
        )
    if "malformed_or_coded_company_name" in reasons:
        return RecoveryProposal(
            INSUFFICIENT_EVIDENCE, NONE, "malformed_or_coded_company_name", evidence=base
        )

    keys = _name_keys(name)
    exact_slug = bool(slug) and slug in keys
    exact_domain = bool(domain_brand) and domain_brand in keys
    base["exact_slug_match"] = exact_slug
    base["exact_domain_match"] = exact_domain

    if not (exact_slug or exact_domain):
        # Only prefix/extension/no corroboration survives -- the name is plausible but
        # not provable from stored identity. Never auto-applied.
        partial = bool(
            _cdr._anchor_match(_cdr._name_key(name), slug)
            or _cdr._anchor_match(_cdr._name_key(name), domain_brand)
        )
        return RecoveryProposal(
            MULTI_BRAND_AMBIGUOUS if partial else INSUFFICIENT_EVIDENCE,
            MEDIUM if partial else NONE,
            "name_not_exactly_corroborated_by_either_anchor",
            evidence=base,
        )

    # Exactly corroborated by a stable identity anchor. The name is a fact about the
    # entity; the slug/domain string distance is not evidence against it.
    if exact_slug and exact_domain:
        classification, reason = (
            CANONICAL_EXACT_CORROBORATION,
            "display_name_exactly_corroborated_by_both_anchors",
        )
    elif exact_domain:
        classification, reason = (
            LINKEDIN_DOMAIN_CORROBORATION,
            "display_name_exactly_matches_verified_email_domain_brand",
        )
    else:
        classification, reason = (
            CANONICAL_EXACT_CORROBORATION,
            "display_name_exactly_matches_posting_linkedin_organization",
        )
    if _cdr._name_key(name) not in {slug, domain_brand}:
        # The match needed ampersand/legal-suffix normalization -- still deterministic.
        classification = SAFE_SUFFIX_NORMALIZATION_CORROBORATED
        reason += "_after_safe_normalization"

    return RecoveryProposal(
        classification,
        HIGH,
        reason,
        patch={
            # "medium" is honest: the name is corroborated, the secondary anchor is
            # not. It clears the send-safe gate without claiming full agreement.
            "Outbound Company Confidence": "medium",
            "Outbound Company Evidence": _dump_evidence(
                {
                    **evidence,
                    "recovery_version": RECOVERY_VERSION,
                    "recovery_classification": classification,
                    "recovery_reason": reason,
                    "reasons": list(dict.fromkeys([*reasons, f"recovered:{reason}"])),
                }
            ),
        },
        evidence=base,
    )


def _is_contiguous_extraction(rendered: str, title: str) -> bool:
    """True when ``rendered`` appears verbatim in ``title`` on token boundaries."""
    if not rendered or not title:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(rendered.strip()) + r"(?![A-Za-z0-9])"
    return bool(re.search(pattern, title, flags=re.IGNORECASE))


def classify_role_hold(fields: Dict[str, Any]) -> RecoveryProposal:
    """Classify one role-side hold, recovering only pure in-title extractions."""
    evidence = _load_evidence(fields, "Outbound Role Evidence")
    rules = [str(r) for r in (evidence.get("rules") or [])]
    raw = str(evidence.get("normalized_title") or "").strip()
    if not raw:
        raw, _ = _rdr._collapse_serialized_duplicate(
            _rdr._text(fields.get("Open Role") or fields.get("Outbound Role") or "")
        )
    matched = _rdr._normalize_abbreviations(
        evidence.get("matched_role") or fields.get("Matched Role") or ""
    )
    base = {"rules": rules[-3:], "normalized_title": raw, "matched_role": matched}

    if not matched:
        return RecoveryProposal(
            ROLE_NO_CATALOG_ANCHOR, NONE, "no_catalog_matched_role_to_anchor_on", evidence=base
        )
    if "material_cross_function_disagreement" in rules:
        # The catalog role sits in a different function than the posting title. Using
        # either one would misdescribe the opening.
        return RecoveryProposal(
            ROLE_CROSS_FUNCTION_AMBIGUOUS, NONE, "matched_role_crosses_function", evidence=base
        )
    if _rdr._find_anchor(raw, matched)[0] < 0:
        return RecoveryProposal(
            ROLE_NO_CATALOG_ANCHOR, NONE, "matched_role_not_present_in_title", evidence=base
        )

    rendered, modifiers = _rdr._render_from_anchor(raw, matched)
    rendered = str(rendered or "").strip()
    base["rendered"] = rendered
    base["preserved_modifiers"] = list(modifiers or [])

    # Fail closed: the outbound role must be text lifted out of the posting title and
    # must still contain the catalog role. Anything reordered or invented stays held.
    if not _is_contiguous_extraction(rendered, raw):
        # The renderer reordered or rewrote the title. That does not make the row
        # ambiguous if the catalog role itself sits verbatim in the title -- using it
        # is extraction, not choice, and it is the role this row was already
        # qualified, bucketed and HM-searched on.
        # Scoped to match the resolver invariant: a DELIMITED title may advertise two
        # distinct roles, so naming one is unsafe there. Without a delimiter the extra
        # words are qualifiers ("Account Manager Team Lead"), and lifting the catalog
        # role out of the title is extraction, not choice.
        verbatim = ("" if _rdr._has_delimited_competing_heads(raw)
                    else _rdr.catalog_role_verbatim(raw, matched))
        if verbatim:
            rendered = verbatim
            base["rendered"] = rendered
            base["anchor_method"] = "catalog_role_verbatim"
        else:
            return RecoveryProposal(
                ROLE_RENDER_NOT_EXTRACTIVE, NONE,
                "rendered_role_not_verbatim_in_title", evidence=base,
            )
    if not _is_contiguous_extraction(matched, rendered):
        return RecoveryProposal(
            ROLE_RENDER_NOT_EXTRACTIVE, NONE, "rendered_role_dropped_the_catalog_role", evidence=base
        )

    return RecoveryProposal(
        ROLE_ANCHOR_EXTRACTION,
        HIGH,
        "rendered_verbatim_from_corroborated_catalog_role_anchor",
        patch={
            "Outbound Role": rendered,
            "Outbound Role Confidence": "medium",
            "Outbound Role Evidence": _dump_evidence(
                {
                    **evidence,
                    "recovery_version": RECOVERY_VERSION,
                    "recovery_classification": ROLE_ANCHOR_EXTRACTION,
                    "rules": list(
                        dict.fromkeys([*rules, "recovered:rendered_from_catalog_role_anchor"])
                    ),
                }
            ),
        },
        evidence=base,
    )


#: Fields recovery is ever permitted to rewrite. Anything else -- Email, Hiring
#: Manager, Job URL, Final Decision, Validated At -- must survive untouched, so a
#: re-signature can never launder a change to the validated decision itself.
RECOVERABLE_FIELDS = frozenset({
    "Outbound Company Confidence", "Outbound Company Evidence",
    "Outbound Role", "Outbound Role Confidence", "Outbound Role Evidence",
    "Outbound Hold", "Role Focus", "Focus Quality", "Focus Evidence",
})


def resign_patch(fields: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``patch`` plus a regenerated ``Validation Fingerprint``.

    The fingerprint signs the outbound-display fields, so any recovery invalidates
    it and the row would merely fail the *next* gate instead. Re-signing is a
    privileged act, so it is fenced three ways:

    * the row's CURRENT fingerprint must already verify -- an unauthentic or
      stale-version row is never re-signed into a valid one;
    * the patch may only touch :data:`RECOVERABLE_FIELDS`;
    * ``Validated At`` and ``Final Decision`` are carried through unchanged, so the
      signature still attests to the same validation event.

    Raises ``ValueError`` if any fence is violated -- callers must skip the row.
    """
    from validation_integrity import fingerprint_matches, validation_fingerprint

    if not patch:
        return {}
    illegal = sorted(set(patch) - RECOVERABLE_FIELDS)
    if illegal:
        raise ValueError(f"recovery may not modify signed fields: {illegal}")
    if not fingerprint_matches(fields):
        raise ValueError("refusing to re-sign a row whose current fingerprint is invalid")
    merged = {**fields, **patch}
    return {**patch, "Validation Fingerprint": validation_fingerprint(merged)}


def stale_hold_patch(fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Clear a hold flag that no current condition justifies.

    Returns ``None`` unless BOTH sides are independently send-safe right now, so this
    can only ever remove leftover workflow state -- never grant a hold-worthy row.
    """
    if not bool(fields.get("Outbound Hold")):
        return None
    company_conf = str(fields.get("Outbound Company Confidence") or "").strip().lower()
    role_conf = str(fields.get("Outbound Role Confidence") or "").strip().lower()
    if not str(fields.get("Outbound Company") or "").strip() or company_conf not in {"high", "medium"}:
        return None
    if not str(fields.get("Outbound Role") or "").strip():
        return None
    if role_conf and role_conf not in {"high", "medium"}:
        return None
    return {"Outbound Hold": False}
