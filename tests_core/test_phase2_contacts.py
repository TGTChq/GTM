"""Phase 2 audit (2026-09-19): contact-side defects, tasks 7-9.

Task 7 -- ``is_founder_tier`` treats bare "president" as a founder token, so
"Vice President of Sales" is founder-tier. Luis's decision (2026-09-19): a
C-level job opening is not the same thing as a founder contact; "President"
alone stays founder-tier, "Vice President" in any spelling does not.

Task 8 -- the buyer-title gate (``title_matches`` in ``gates.py``, the ONE
matcher every caller uses) is an exact-phrase substring match. It misses
common variants ("Vice President, X", "SVP X", "Director, X", "Human
Resources Manager") and over-matches unrelated "Operations Manager" titles
(warehouse/clinical/IT) via bare substring.
"""
from __future__ import annotations

from tgtc_core.policy.campaigns import is_founder_tier
from tgtc_core.domain.gates import title_matches


# --- Task 7 --------------------------------------------------------------

def test_vice_president_titles_are_not_founder_tier():
    for title in ("Vice President of Sales", "VP, Marketing", "SVP Finance",
                  "Senior Vice President, People", "EVP Operations", "Vice President Engineering"):
        assert not is_founder_tier(title), title


def test_real_founder_titles_are_still_founder_tier():
    for title in ("Founder", "Co-Founder", "Cofounder", "CEO", "Chief Executive Officer",
                  "President", "President & CEO", "Owner"):
        assert is_founder_tier(title), title


# --- Task 8 --------------------------------------------------------------

def test_common_title_variants_match_the_buyer_list():
    assert title_matches("Vice President, Revenue Operations", ("VP Revenue Operations", "VP of Revenue Operations"))
    assert title_matches("SVP Engineering", ("VP Engineering", "VP of Engineering"))
    assert title_matches("Director, Customer Success", ("Customer Success Director", "Director of Customer Success"))
    assert title_matches("Human Resources Manager", ("HR Manager",))
    assert title_matches("Head of People Operations", ("Head of People",))


def test_unrelated_operations_titles_do_not_match():
    operations_targets = ("Operations Director", "Director of Operations", "Operations Manager")
    for title in ("Warehouse Operations Manager", "Clinical Operations Manager", "IT Operations Manager"):
        assert not title_matches(title, operations_targets), title


def test_real_operations_titles_still_match():
    """Widening the variants must not widen the over-match -- a genuine business
    Operations Manager (no unrelated-domain qualifier) must still pass."""
    operations_targets = ("Operations Director", "Director of Operations", "Operations Manager")
    for title in ("Operations Manager", "Director of Operations", "Business Operations Manager"):
        assert title_matches(title, operations_targets), title


def test_buyer_titles_from_campaigns_module_still_resolve_with_the_widened_matcher():
    """The two functions must keep agreeing end to end: buyer_titles() output,
    run through the SAME matcher, still accepts the canonical titles it lists."""
    from tgtc_core.policy.campaigns import buyer_titles

    for fn in ("gtm_revenue", "engineering", "people_hr", "operations"):
        titles = buyer_titles(fn, founder_allowed=True)
        for t in titles:
            assert title_matches(t, titles), (fn, t)
