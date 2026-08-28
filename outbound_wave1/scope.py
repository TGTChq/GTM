"""Deterministic ``scope_combination`` derivation.

A scope combination is NOT a new inference about the posting. It is a
campaign-specific reading of two fields the pipeline already stored: the
canonical ``Role Focus`` phrases and the ``Focus Evidence`` that licensed them.

Each campaign declares its own facet vocabulary (see ``campaigns.py``). A facet
is credited only when one of its literal keywords appears in a stored focus
phrase or in a usable stored evidence item. Two or more distinct facets make a
combination; fewer is insufficient evidence and the caller degrades rather than
guessing at a combination that was never signalled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .campaigns import CampaignPolicy
from .evidence import FocusEvidence, render_evidence_list

#: A combination needs at least this many distinct facets.
MIN_FACETS = 2


@dataclass(frozen=True)
class ScopeCombination:
    facets: Tuple[str, ...]
    #: The stored phrases that credited each facet, for audit.
    support: Dict[str, str]
    sufficient: bool
    reason: str

    @property
    def rendered(self) -> str:
        return render_evidence_list(self.facets)

    def as_key(self) -> str:
        return " + ".join(self.facets)


def derive_scope_combination(
    policy: CampaignPolicy,
    focus: FocusEvidence,
) -> ScopeCombination:
    """Read a scope combination for ``policy`` out of stored focus evidence."""
    if not policy.scope_facets:
        return ScopeCombination((), {}, False, "campaign_has_no_scope_facet_vocabulary")
    if not focus.is_specific:
        return ScopeCombination((), {}, False, "focus_evidence_is_not_specific")

    # Only phrases the row genuinely supports may credit a facet: the renderable
    # focus phrases (already capped by usable evidence count) plus the usable
    # evidence strings themselves.
    haystacks: List[str] = [phrase for phrase in focus.renderable(limit=len(focus.focus_items))]
    haystacks.extend(focus.usable_items)
    if not haystacks:
        return ScopeCombination((), {}, False, "no_usable_focus_phrases")

    facets: List[str] = []
    support: Dict[str, str] = {}
    for facet_name, keywords in policy.scope_facets:
        for phrase in haystacks:
            lowered = phrase.casefold()
            hit = next((kw for kw in keywords if kw in lowered), "")
            if hit:
                facets.append(facet_name)
                support[facet_name] = phrase
                break

    if len(facets) < MIN_FACETS:
        return ScopeCombination(
            tuple(facets), support, False,
            f"only_{len(facets)}_scope_facet_matched_min_{MIN_FACETS}",
        )
    return ScopeCombination(tuple(facets), support, True, "scope_combination_derived")
