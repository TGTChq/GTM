"""Phase 2 audit (2026-09-19): contact-side defects, tasks 7-9.

Task 7 -- ``is_founder_tier`` treats bare "president" as a founder token, so
"Vice President of Sales" is founder-tier. Luis's decision (2026-09-19): a
C-level job opening is not the same thing as a founder contact; "President"
alone stays founder-tier, "Vice President" in any spelling does not.
"""
from __future__ import annotations

from tgtc_core.policy.campaigns import is_founder_tier


# --- Task 7 --------------------------------------------------------------

def test_vice_president_titles_are_not_founder_tier():
    for title in ("Vice President of Sales", "VP, Marketing", "SVP Finance",
                  "Senior Vice President, People", "EVP Operations", "Vice President Engineering"):
        assert not is_founder_tier(title), title


def test_real_founder_titles_are_still_founder_tier():
    for title in ("Founder", "Co-Founder", "Cofounder", "CEO", "Chief Executive Officer",
                  "President", "President & CEO", "Owner"):
        assert is_founder_tier(title), title
