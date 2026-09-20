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

Task 9 -- contact depth never exceeds 1: the first approval finalizes the
opportunity (a terminal state) even when the configured maximum
(``TGTC_MAX_CONTACTS_PER_OPPORTUNITY``, default 3) is not yet met, and
ranking has no persona-diversity concept. See tgtc_core/services/opportunity.py.
"""
from __future__ import annotations

import pytest

from tgtc_core.policy.campaigns import is_founder_tier
from tgtc_core.domain.gates import title_matches
from tgtc_core.services.opportunity import ContactState, contact_persona, select_next_contact
from tgtc_core.testing.fakes import make_person
from tests_core.helpers import opportunity_service, sqlall
from tests_core.seed import apollo_for, seed_opportunity


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


# --- Task 9 ----------------------------------------------------------------

def opportunity_with_contacts(count: int) -> ContactState:
    return ContactState(existing_person_keys={f"p{i}" for i in range(count)},
                        existing_personas=["functional_owner"] * count)


def test_opportunity_wants_more_contacts_until_the_target_is_met():
    opp = opportunity_with_contacts(count=1)
    assert opp.wants_more_contacts(max_contacts=3) is True


def test_opportunity_stops_at_the_configured_maximum():
    opp = opportunity_with_contacts(count=3)
    assert opp.wants_more_contacts(max_contacts=3) is False


def test_the_next_contact_is_a_different_persona():
    chosen = select_next_contact(existing_personas=["functional_owner"],
                                 candidates=[{"person_key": "p2", "title": "Marketing Manager", "persona": "functional_owner"},
                                             {"person_key": "p3", "title": "VP Marketing", "persona": "executive_leader"}])
    assert chosen["persona"] == "executive_leader"


def test_a_person_already_held_is_never_selected_again():
    assert select_next_contact(existing_person_keys={"p1"},
                               candidates=[{"person_key": "p1", "title": "VP Marketing", "persona": "executive_leader"}]) is None


def test_three_candidates_in_the_same_persona_are_not_diversification():
    """"Three people in the same role are NOT diversification" (Authority): with no
    persona held yet, the first pick is unconstrained, but once a persona is held,
    a same-persona candidate is skipped in favour of a not-yet-held persona."""
    candidates = [
        {"person_key": "p1", "title": "Sales Director", "persona": "functional_owner"},
        {"person_key": "p2", "title": "Director of Sales", "persona": "functional_owner"},
        {"person_key": "p3", "title": "VP Sales", "persona": "executive_leader"},
    ]
    first = select_next_contact(candidates=candidates)
    assert first["person_key"] == "p1"
    second = select_next_contact(existing_person_keys={"p1"}, existing_personas=["functional_owner"], candidates=candidates)
    assert second["person_key"] == "p3" and second["persona"] == "executive_leader"


def test_wants_more_contacts_never_exceeds_the_hard_cap_of_three():
    """Never raise the configured maximum in code (brief constraint), even if a
    caller passes a larger number."""
    opp = opportunity_with_contacts(count=3)
    assert opp.wants_more_contacts(max_contacts=10) is False


@pytest.mark.parametrize("function_key,title,expected", [
    ("gtm_revenue", "Sales Director", "functional_owner"),
    ("gtm_revenue", "VP Sales", "executive_leader"),
    ("people_hr", "HR Director", "functional_owner"),
    ("people_hr", "Head of Talent Acquisition", "ta_people_leader"),
])
def test_contact_persona_classifies_the_three_tiers(function_key, title, expected):
    assert contact_persona(title, function_key) == expected


# --- Task 9, end to end: proves the fix is reachable by the live approval flow,
# not just the pure functions above. SIMULATED Apollo only (tgtc_core.testing.fakes)
# -- no paid provider call occurs anywhere in this test.

def test_two_role_diverse_contacts_are_approved_in_one_pass(conn, clock):
    """Reproduces the measured defect directly: before this batch, the FIRST
    approval finalized the opportunity (terminal 'approved' state) even with a
    second qualifying buyer sitting right there in the same search result and
    quota room to spare. With the fix and max_contacts=2, both are approved in
    one process() call, and they are NOT "three people in the same role" --
    the functional owner and the executive leader, not two of the same tier."""
    domain, org = "acme.com", "Acme"
    owner = make_person(id="p-owner", first="Owner", last="Person", title="Marketing Director",
                        org_name=org, org_domain=domain, email="owner@acme.com", email_status="verified")
    executive = make_person(id="p-exec", first="Exec", last="Person", title="VP Marketing",
                            org_name=org, org_domain=domain, email="exec@acme.com", email_status="verified")
    _, eid, opp = seed_opportunity(conn, clock, function_key="marketing", domain=domain, org_name=org)
    fake = apollo_for(domain, org, function_key="marketing", people=[owner, executive])
    out = opportunity_service(conn, fake, clock, max_contacts_per_opportunity=2).process(opp)
    assert out.outcome == "approved"
    assert out.details["approvals_created"] == 2 and out.details["approved_total"] == 2
    rows = sqlall(conn, "SELECT p.title FROM approvals a JOIN people p ON p.id = a.person_id "
                        "WHERE a.opportunity_id = %s", (opp,))
    approved_titles = {r["title"] for r in rows}
    assert approved_titles == {"Marketing Director", "VP Marketing"}
    personas = {contact_persona(t, "marketing") for t in approved_titles}
    assert personas == {"functional_owner", "executive_leader"}, "not diversified: both contacts in the same persona"


def test_a_single_available_buyer_does_not_finalize_below_quota(conn, clock):
    """The core of the measured defect: with only ONE qualifying buyer available
    and a quota of 3, the opportunity must NOT reach the terminal 'approved'
    state (it stays open, waiting for more) -- so a later purchase can still
    follow the first approval, unlike the 86/86 production opportunities that
    could never reopen once finalized."""
    domain, org = "acme.com", "Acme"
    owner = make_person(id="p-owner", first="Owner", last="Person", title="Marketing Director",
                        org_name=org, org_domain=domain, email="owner@acme.com", email_status="verified")
    _, eid, opp = seed_opportunity(conn, clock, function_key="marketing", domain=domain, org_name=org)
    fake = apollo_for(domain, org, function_key="marketing", people=[owner])
    out = opportunity_service(conn, fake, clock, max_contacts_per_opportunity=3).process(opp)
    assert out.outcome == "wait" and out.reason.startswith("buyer_search_pending:")
    assert sqlall(conn, "SELECT count(*) AS n FROM approvals")[0]["n"] == 1
    state = sqlall(conn, "SELECT state FROM opportunities WHERE id = %s", (opp,))[0]["state"]
    assert state == "open", "opportunity finalized below its configured contact quota"
