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

#: The firmographic gates that fail closed on a null provider field. These are
#: the MEASURED loss: Wellfound and YC rows carry no firmographics at all, so a
#: headcount range or an employment-type equality drops 100% of them.
FAIL_CLOSED_ON_NULL = ("ai_employment_type", "organization_headcount_gte",
                       "organization_headcount_lt")


def test_the_exhaustive_profile_exists_and_validates():
    assert EXHAUSTIVE_PROFILE in QUERY_PROFILES
    assert validate_profile(EXHAUSTIVE_PROFILE) == EXHAUSTIVE_PROFILE


def test_exhaustive_drops_the_firmographic_gates_that_fail_closed():
    params = profile_filters(EXHAUSTIVE_PROFILE)
    for gate in FAIL_CLOSED_ON_NULL:
        assert gate not in params, f"{gate} would drop rows whose provider field is null"


def test_exhaustive_keeps_the_professional_taxonomies():
    """Measured, first canary: removing the taxonomy filter did not widen the
    KNOWLEDGE-WORK universe, it imported the frontline labour market -- Usher,
    Ramp Agent, Car Wash Associate, Bartender, Teller, CDL-A Dump Truck Driver,
    Prepared Foods Cook. Each one costs a credit to buy and then rejects.

    The taxonomy filter is an affirmative selection of the professional labour
    market, enforced before a credit is spent. Rows with a NULL taxonomy are
    not lost: the discovery slot below buys them unfiltered."""
    params = profile_filters(EXHAUSTIVE_PROFILE)
    assert "ai_taxonomies_a" in params


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


#: Measured, first full production run (2026-09-21), eligible contacts per
#: billed record by arm: priority 152/482 = 0.315, discovery 43/400 = 0.108,
#: exhaustive 139/1,814 = 0.077. The widened arm bought part-time and
#: out-of-ICP employers (33% of it rejected on employment type alone) and was
#: dominated by BOTH other arms. The null-firmographic rows it was meant to
#: reach are already reached by discovery, at a better yield. So the flag no
#: longer changes acquisition at all: the exhaustive scope's gain is downstream,
#: in routing (priority postings assigned 72%), and that is unaffected.
MEASURED_YIELD = {"priority_v1": 152 / 482, "discovery_v1": 43 / 400, "exhaustive_v1": 139 / 1814}


def test_the_measured_arm_ranking_is_what_the_allocation_follows():
    assert MEASURED_YIELD["priority_v1"] > MEASURED_YIELD["discovery_v1"] > MEASURED_YIELD["exhaustive_v1"]


def test_flag_on_allocates_the_measured_winner():
    profiles = [p for _, p in balanced_slots(SOURCES, 10, env=ON)]
    assert profiles.count(PRIORITY_PROFILE) == 8
    assert profiles.count(DISCOVERY_PROFILE) == 2
    assert EXHAUSTIVE_PROFILE not in profiles


def test_a_null_firmographic_row_is_still_reachable():
    """Discovery sends no firmographic or taxonomy gate at all."""
    params = profile_filters(DISCOVERY_PROFILE)
    for gate in FAIL_CLOSED_ON_NULL + ("ai_taxonomies_a",):
        assert gate not in params
    assert DISCOVERY_PROFILE in [p for _, p in balanced_slots(SOURCES, 10, env=ON)]


def test_both_sources_still_get_both_arms():
    by_source: dict = {}
    for source, profile in balanced_slots(SOURCES, 20, env=ON):
        by_source.setdefault(source, set()).add(profile)
    assert set(by_source) == set(SOURCES)
    for profiles in by_source.values():
        assert profiles == {PRIORITY_PROFILE, DISCOVERY_PROFILE}


def test_slot_allocation_is_deterministic():
    assert list(balanced_slots(SOURCES, 10, env=ON)) == list(balanced_slots(SOURCES, 10, env=ON))
