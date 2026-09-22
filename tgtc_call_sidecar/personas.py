"""Decision-maker personas, read from the core's own buyer-title policy (read-only import).

The sidecar never changes the core's titles; it uses the same tables so a call
persona means the same thing as an email buyer.
"""
from __future__ import annotations

from typing import List, Optional

from tgtc_core.domain.title_norm import title_matches
from tgtc_core.policy.campaigns import (
    DIRECT_BUYER_TITLES, EXECUTIVE_BUYER_TITLES, TALENT_PEOPLE_BUYER_TITLES, is_founder_tier,
)

FUNCTIONAL = "functional_owner"
TALENT = "talent_people"
EXECUTIVE = "executive_founder"
#: CALL_FIRST priority: direct functional owner, then Talent/People, then executive fallback.
PRIORITY = {FUNCTIONAL: 0, TALENT: 1, EXECUTIVE: 2}


def _functional_titles(function_key: str) -> List[str]:
    direct = list(DIRECT_BUYER_TITLES.get(function_key, ()))
    execs = [t for t in EXECUTIVE_BUYER_TITLES.get(function_key, ()) if not is_founder_tier(t)]
    return direct + execs


def _talent_titles(function_key: str) -> List[str]:
    return [t for t in TALENT_PEOPLE_BUYER_TITLES.get(function_key, ()) if not is_founder_tier(t)]


def _founder_titles(function_key: str) -> List[str]:
    return [t for t in EXECUTIVE_BUYER_TITLES.get(function_key, ()) if is_founder_tier(t)]


def classify(title: str, function_key: str, employee_count: Optional[int], *,
             founder_max_employees: int = 99) -> Optional[str]:
    """The persona of a title for this campaign, or None when it is not a decision-maker."""
    title = str(title or "")
    if not title.strip():
        return None
    if title_matches(title, _functional_titles(function_key)):
        return FUNCTIONAL
    if title_matches(title, _talent_titles(function_key)):
        return TALENT
    small = employee_count is not None and employee_count <= founder_max_employees
    if small and (is_founder_tier(title) or title_matches(title, _founder_titles(function_key))):
        return EXECUTIVE
    return None


def search_titles(function_key: str, persona: str) -> List[str]:
    return {FUNCTIONAL: _functional_titles, TALENT: _talent_titles, EXECUTIVE: _founder_titles}[persona](function_key)
