"""Phase 4 audit (2026-09-20), change 2: three genuinely distinct personas for
all nine campaigns.

Measured defect. ``opportunity.contact_persona`` recognised a third persona
(``ta_people_leader``) for ONE function key, ``people_hr``, from a hard-coded
two-title tuple, and for none of the other nine. ``opportunity.py``'s own
termination rule says so out loud: "9 of 10 functions expose exactly 2 reachable
personas (only people_hr has a third)". So depth 3 was structurally unreachable
outside people_hr no matter what Apollo returned, and the shipped default quota
of 3 could never be met.

Authority (SKILL.md, fixed definitions): "Target 2-3 role-diverse contacts per
company x campaign: the functional owner, the executive leader, and a TA/People
leader when appropriate. Equivalent people do not diversify."

This is a POLICY DATA change plus a persona classifier that reads it, not a new
gate. The third persona's titles enter ``buyer_titles()``, which is both what
the free search SENDS and what the contact gates ACCEPT -- so it changes the
Apollo query, and its effect on candidate availability can only be measured by a
live free search, never offline.
"""
from __future__ import annotations

import pytest

from tgtc_core.domain.gates import title_matches
from tgtc_core.policy.campaigns import (
    CAMPAIGNS, DIRECT_BUYER_TITLES, EXECUTIVE_BUYER_TITLES, FUNCTION_KEYS,
    TALENT_PEOPLE_BUYER_TITLES, buyer_titles, is_founder_tier,
)
from tgtc_core.services.opportunity import (
    CONTACT_PERSONAS, PERSONA_EXECUTIVE_LEADER, PERSONA_FUNCTIONAL_OWNER, PERSONA_TA_PEOPLE_LEADER,
    contact_persona,
)


def _non_founder(titles):
    return tuple(t for t in titles if not is_founder_tier(t))


# --- every campaign has all three personas ---------------------------------

@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_every_function_key_has_all_three_personas(function_key):
    assert DIRECT_BUYER_TITLES.get(function_key), function_key
    assert _non_founder(EXECUTIVE_BUYER_TITLES.get(function_key, ())), function_key
    assert TALENT_PEOPLE_BUYER_TITLES.get(function_key), function_key


def test_all_nine_campaigns_are_covered_not_just_the_function_keys():
    """The unit of measurement is the CAMPAIGN, and customer_experience carries
    two function keys. Every campaign must reach depth 3 through every one of
    its function keys, or the campaign has a hole a per-function test misses."""
    for campaign in CAMPAIGNS:
        for fn in campaign.functions:
            assert TALENT_PEOPLE_BUYER_TITLES.get(fn), (campaign.key, fn)


@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_the_three_personas_are_disjoint_within_a_function(function_key):
    """"Three variants of one title is not three personas." A title that sits in
    two of a function's own persona lists makes the persona of a candidate
    ambiguous and lets the same role be counted as diversification."""
    direct = {t.lower() for t in DIRECT_BUYER_TITLES.get(function_key, ())}
    execs = {t.lower() for t in _non_founder(EXECUTIVE_BUYER_TITLES.get(function_key, ()))}
    talent = {t.lower() for t in TALENT_PEOPLE_BUYER_TITLES.get(function_key, ())}
    assert not direct & execs, (function_key, direct & execs)
    assert not direct & talent, (function_key, direct & talent)
    assert not execs & talent, (function_key, execs & talent)


# --- the classifier agrees with the data, for every campaign ---------------

@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_contact_persona_round_trips_every_listed_title(function_key):
    """Every title the pipeline SEARCHES for must classify back to the persona
    it was listed under, or the persona a candidate is credited with is not the
    persona that found them."""
    for title in DIRECT_BUYER_TITLES[function_key]:
        assert contact_persona(title, function_key) == PERSONA_FUNCTIONAL_OWNER, (function_key, title)
    for title in _non_founder(EXECUTIVE_BUYER_TITLES[function_key]):
        assert contact_persona(title, function_key) == PERSONA_EXECUTIVE_LEADER, (function_key, title)
    for title in TALENT_PEOPLE_BUYER_TITLES[function_key]:
        assert contact_persona(title, function_key) == PERSONA_TA_PEOPLE_LEADER, (function_key, title)


@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_all_three_personas_are_reachable_for_every_function(function_key):
    reached = {contact_persona(t, function_key)
               for group in (DIRECT_BUYER_TITLES[function_key],
                             _non_founder(EXECUTIVE_BUYER_TITLES[function_key]),
                             TALENT_PEOPLE_BUYER_TITLES[function_key])
               for t in group}
    assert reached == set(CONTACT_PERSONAS), (function_key, reached)


def test_people_hr_keeps_talent_acquisition_distinct_from_its_own_people_executives():
    """people_hr is the one function where the TA persona sits INSIDE the same
    buyer hierarchy. Its own executives must stay executives -- collapsing
    "Chief People Officer" into the TA persona would take people_hr from three
    personas back to two."""
    assert contact_persona("Head of Talent Acquisition", "people_hr") == PERSONA_TA_PEOPLE_LEADER
    assert contact_persona("Chief People Officer", "people_hr") == PERSONA_EXECUTIVE_LEADER
    assert contact_persona("Head of People", "people_hr") == PERSONA_EXECUTIVE_LEADER
    assert contact_persona("HR Director", "people_hr") == PERSONA_FUNCTIONAL_OWNER


def test_three_variants_of_one_title_are_still_one_persona():
    """The measured defect restated as a test: widening the matcher must not
    turn near-synonyms into diversity."""
    for title in ("Finance Director", "Director of Finance", "Director Finance Analytics"):
        assert contact_persona(title, "finance") == PERSONA_FUNCTIONAL_OWNER, title


# --- the searched list ------------------------------------------------------

@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_buyer_titles_searches_all_three_personas(function_key):
    titles = buyer_titles(function_key, founder_allowed=False)
    for group in (DIRECT_BUYER_TITLES[function_key],
                  _non_founder(EXECUTIVE_BUYER_TITLES[function_key]),
                  TALENT_PEOPLE_BUYER_TITLES[function_key]):
        for t in group:
            assert any(x.lower() == t.lower() for x in titles), (function_key, t)


@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_the_talent_persona_is_searched_after_the_functional_and_executive_owners(function_key):
    """Ranking spends the paid call on the best-ranked candidate, and ranking's
    base order is this list's order. The function's own owners must come first:
    a TA leader is the THIRD contact, not the first."""
    titles = [t.lower() for t in buyer_titles(function_key, founder_allowed=False)]
    talent_positions = [titles.index(t.lower()) for t in TALENT_PEOPLE_BUYER_TITLES[function_key]]
    own_positions = [titles.index(t.lower())
                     for group in (DIRECT_BUYER_TITLES[function_key],
                                   _non_founder(EXECUTIVE_BUYER_TITLES[function_key]))
                     for t in group]
    assert min(talent_positions) > max(own_positions), function_key


@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_founder_titles_still_come_last_and_only_when_allowed(function_key):
    with_founders = buyer_titles(function_key, founder_allowed=True)
    without = buyer_titles(function_key, founder_allowed=False)
    assert not any(is_founder_tier(t) for t in without), function_key
    founder_positions = [i for i, t in enumerate(with_founders) if is_founder_tier(t)]
    if founder_positions:
        assert min(founder_positions) == len(with_founders) - len(founder_positions), function_key


@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_no_talent_title_is_dropped_by_the_shared_matcher(function_key):
    """The new titles have to survive the same predicate that will judge the
    people they find -- a persona title the matcher itself rejects is a query
    that can never produce an accepted contact."""
    titles = buyer_titles(function_key, founder_allowed=False)
    for t in TALENT_PEOPLE_BUYER_TITLES[function_key]:
        assert title_matches(t, titles), (function_key, t)


# --- reachable by the live pipeline, not only by the pure functions --------

def test_three_role_diverse_contacts_are_approved_for_a_non_people_hr_campaign(conn, clock):
    """The end-to-end proof that this is not a test-only change. Before it, a
    marketing opportunity with three qualifying buyers in one search result
    approved TWO and finalized with `approved_no_further_role_diverse_candidates`,
    because marketing had no third persona to select -- whatever the quota said.

    SIMULATED Apollo (`tgtc_core.testing.fakes`); no paid provider call occurs.
    """
    from tgtc_core.testing.fakes import make_person
    from tests_core.helpers import opportunity_service, sqlall
    from tests_core.seed import apollo_for, seed_opportunity

    domain, org = "acme.com", "Acme"
    people = [
        make_person(id="p-owner", first="Owner", last="Person", title="Marketing Director",
                    org_name=org, org_domain=domain, email="owner@acme.com", email_status="verified"),
        make_person(id="p-exec", first="Exec", last="Person", title="VP Marketing",
                    org_name=org, org_domain=domain, email="exec@acme.com", email_status="verified"),
        make_person(id="p-ta", first="Ta", last="Person", title="Head of Talent Acquisition",
                    org_name=org, org_domain=domain, email="ta@acme.com", email_status="verified"),
    ]
    _, _, opp = seed_opportunity(conn, clock, function_key="marketing", domain=domain, org_name=org)
    fake = apollo_for(domain, org, function_key="marketing", people=people)
    out = opportunity_service(conn, fake, clock, max_contacts_per_opportunity=3).process(opp)
    assert out.outcome == "approved"
    assert out.details["approvals_created"] == 3 and out.details["approved_total"] == 3
    titles = {r["title"] for r in sqlall(
        conn, "SELECT p.title FROM approvals a JOIN people p ON p.id = a.person_id WHERE a.opportunity_id = %s", (opp,))}
    assert {contact_persona(t, "marketing") for t in titles} == set(CONTACT_PERSONAS)


def test_the_talent_persona_is_bought_last_not_first(conn, clock):
    """Ranking must not spend the first credit on the third persona. With a
    quota of 1 and all three personas available, the contact bought is the
    functional owner."""
    from tgtc_core.testing.fakes import make_person
    from tests_core.helpers import opportunity_service, sqlall
    from tests_core.seed import apollo_for, seed_opportunity

    domain, org = "acme.com", "Acme"
    people = [
        make_person(id="p-ta", first="Ta", last="Person", title="Head of Talent Acquisition",
                    org_name=org, org_domain=domain, email="ta@acme.com", email_status="verified"),
        make_person(id="p-exec", first="Exec", last="Person", title="VP Marketing",
                    org_name=org, org_domain=domain, email="exec@acme.com", email_status="verified"),
        make_person(id="p-owner", first="Owner", last="Person", title="Marketing Director",
                    org_name=org, org_domain=domain, email="owner@acme.com", email_status="verified"),
    ]
    _, _, opp = seed_opportunity(conn, clock, function_key="marketing", domain=domain, org_name=org)
    fake = apollo_for(domain, org, function_key="marketing", people=people)
    opportunity_service(conn, fake, clock, max_contacts_per_opportunity=1).process(opp)
    titles = [r["title"] for r in sqlall(
        conn, "SELECT p.title FROM approvals a JOIN people p ON p.id = a.person_id WHERE a.opportunity_id = %s", (opp,))]
    assert titles == ["Marketing Director"], titles
