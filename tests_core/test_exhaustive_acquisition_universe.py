"""Widening the acquisition universe under the exhaustive nine-campaign scope.

The objective is not "buy more rows". It is to stop losing GOOD rows to
provider-side filters that fail closed on a missing field, while keeping every
affirmative exclusion that makes a row worthless.

Measured context this comes from (funnel audit, 2026-09-19/20): of 5,285 unique
jobs, 1,088 were hard rejects and 3,399 -- 64% -- ended UNDECIDED. Only 757
qualified. The undecided majority is the universe this change recovers, and the
exhaustive routing recovers it downstream. This file covers the upstream half:
the PRIORITY profile sends three POSITIVE provider filters
(``ai_employment_type``, ``organization_headcount_*``, ``ai_taxonomies_a``), and
a row whose provider field is null fails all three. Wellfound and YC rows carry
no firmographics at all, so those gates drop 100% of them.

What must NOT be relaxed: the agency exclusion and the excluded-industry list
(staffing, government administration, non-profit, hospitals, outsourcing ...).
Those are affirmative exclusions of things we have decided we never want, and
they are also the cheapest possible place to enforce them -- before a credit is
spent.
"""
from __future__ import annotations

from tgtc_core.domain.acquisition_query import (
    EXCLUDED_LINKEDIN_INDUSTRIES, EXHAUSTIVE_PROFILE, PRIORITY_PROFILE, DISCOVERY_PROFILE,
    QUERY_PROFILES, balanced_slots, policy_filters, profile_filters, validate_profile,
)
from tgtc_core.domain.exhaustive_routing import FLAG_ENV

ON = {FLAG_ENV: "1"}

#: The three positive gates that fail closed on a null provider field.
FAIL_CLOSED_ON_NULL = ("ai_employment_type", "organization_headcount_gte",
                       "organization_headcount_lt", "ai_taxonomies_a")


def test_the_exhaustive_profile_exists_and_validates():
    assert EXHAUSTIVE_PROFILE in QUERY_PROFILES
    assert validate_profile(EXHAUSTIVE_PROFILE) == EXHAUSTIVE_PROFILE


def test_exhaustive_sends_no_filter_that_fails_closed_on_a_missing_field():
    params = profile_filters(EXHAUSTIVE_PROFILE)
    for gate in FAIL_CLOSED_ON_NULL:
        assert gate not in params, f"{gate} would drop rows whose provider field is null"


def test_priority_still_sends_them_so_the_two_arms_stay_comparable():
    params = profile_filters(PRIORITY_PROFILE)
    for gate in FAIL_CLOSED_ON_NULL:
        assert gate in params


def test_exhaustive_keeps_every_affirmative_employer_exclusion():
    """Staffing, government, non-profit and the rest are still excluded before a
    credit is spent. Widening the universe never means buying those."""
    params = profile_filters(EXHAUSTIVE_PROFILE)
    assert params["organization_agency"] == "exclude"
    excluded = params["exclude_organization_industry"]
    for label in ("Staffing and Recruiting", "Government Administration",
                  "Non-profit Organization Management", "Outsourcing/Offshoring"):
        assert label in excluded


def test_exhaustive_excludes_exactly_the_same_industries_as_policy():
    assert profile_filters(EXHAUSTIVE_PROFILE)["exclude_organization_industry"] == \
        policy_filters()["exclude_organization_industry"]
    assert set(EXCLUDED_LINKEDIN_INDUSTRIES)


def test_exhaustive_is_a_strict_superset_of_priority_in_reach():
    """Every row PRIORITY can return, EXHAUSTIVE can also return: it only ever
    removes constraints, never adds one."""
    priority = profile_filters(PRIORITY_PROFILE)
    exhaustive = profile_filters(EXHAUSTIVE_PROFILE)
    assert set(exhaustive) <= set(priority)
    for key, value in exhaustive.items():
        assert priority[key] == value


# --- slot allocation: the run itself measures which arm yields more ----------


SOURCES = ("active-jb", "active-ats")


def test_flag_off_slot_allocation_is_unchanged():
    slots = list(balanced_slots(SOURCES, 10, env={}))
    assert {p for _, p in slots} == {PRIORITY_PROFILE, DISCOVERY_PROFILE}
    assert EXHAUSTIVE_PROFILE not in {p for _, p in slots}


def test_flag_on_gives_the_widened_profile_four_slots_in_five():
    slots = list(balanced_slots(SOURCES, 10, env=ON))
    profiles = [p for _, p in slots]
    assert profiles.count(EXHAUSTIVE_PROFILE) == 8
    assert profiles.count(PRIORITY_PROFILE) == 2


def test_a_narrow_control_arm_always_survives():
    """Without a narrow arm the run cannot say whether widening helped."""
    for pages in (10, 20, 50, 100):
        profiles = [p for _, p in balanced_slots(SOURCES, pages, env=ON)]
        assert PRIORITY_PROFILE in profiles, pages
        assert EXHAUSTIVE_PROFILE in profiles, pages


def test_both_sources_still_get_both_arms():
    by_source: dict = {}
    for source, profile in balanced_slots(SOURCES, 20, env=ON):
        by_source.setdefault(source, set()).add(profile)
    assert set(by_source) == set(SOURCES)
    for source, profiles in by_source.items():
        assert profiles == {EXHAUSTIVE_PROFILE, PRIORITY_PROFILE}, source


def test_slot_allocation_is_deterministic():
    assert list(balanced_slots(SOURCES, 10, env=ON)) == list(balanced_slots(SOURCES, 10, env=ON))
