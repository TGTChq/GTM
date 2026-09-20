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
from tgtc_core.domain.gates import pre_enrichment_check, title_matches
from tgtc_core.domain.facts import RULE_VERSION
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


# --- Fix round 2, IMPORTANT (independent review): "Chief Executive Officer"
# as a qualified PHRASE (not the whole title) must still be founder-tier --
# is_founder_tier's own token/exact-match check only recognizes it as an
# exact whole-string match or the bare "ceo" token, so a qualified spelling
# fell through silently. This is the exact class of defect as C1 (a "no
# behaviour change" refactor quietly dropping matches): the deleted local
# copy in contact_mapping.py matched this phrase anywhere in the title, and
# I2's delegation (fix round 1) narrowed that without a test to catch it.

def test_qualified_chief_executive_officer_phrasing_is_still_founder_tier():
    for title in ("Interim Chief Executive Officer", "Acting Chief Executive Officer",
                  "Deputy Chief Executive Officer", "Group Chief Executive Officer",
                  "Chief Executive Officer of Acme"):
        assert is_founder_tier(title), title


def test_vice_president_still_excluded_after_the_ceo_phrase_widening():
    """The widening above must not also widen the OTHER direction: every
    Vice President spelling stays excluded."""
    for title in ("Vice President of Sales", "VP, Marketing", "SVP Finance",
                  "Senior Vice President, People", "EVP Operations", "Vice President Engineering"):
        assert not is_founder_tier(title), title


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


# --- Fix round 1, C1 (CRITICAL, independent review): the over-match deny-list
# was applied globally, so ordinary modifier words in OTHER functions' real
# titles ("Field Marketing Manager", "Security Engineering Manager", "IT
# Support Manager", "Plant Controller" ...) were silently dropped -- an
# uncounted new drop class, in a task whose entire purpose is to stop dropping
# candidates. Q21's finding was operations-only; the deny-list must be scoped
# there, not applied to every function's matches. Pinned in BOTH directions:
# still blocked within operations (below), no longer blocked elsewhere (here).

def test_qualifier_words_do_not_block_matches_outside_operations():
    """Every one of these regressed (old=True, new=False) under the unscoped
    deny-list; the same qualifier words are ordinary, legitimate modifiers in
    their own function and must not be blocked there."""
    cases = [
        ("Field Marketing Manager", "marketing", ("Marketing Manager",)),
        ("Security Engineering Manager", "engineering", ("Engineering Manager",)),
        ("Network Engineering Manager", "engineering", ("Engineering Manager",)),
        ("Production Engineering Manager", "engineering", ("Engineering Manager",)),
        ("IT Support Manager", "customer_support", ("Support Manager",)),
        ("Production Support Manager", "customer_support", ("Support Manager",)),
        ("Plant Controller", "finance", ("Controller",)),
    ]
    for title, fn, targets in cases:
        assert title_matches(title, targets), (title, fn)


def test_retail_and_field_operations_managers_are_not_unrelated_domains():
    """"Retail Operations Manager" and "Field Operations Manager" are plausible
    genuine business-operations titles, not the warehouse/clinical/IT pattern
    Q21 actually measured; the deny-list must not have been widened past the
    evidence that justified it."""
    operations_targets = ("Operations Director", "Director of Operations", "Operations Manager")
    for title in ("Retail Operations Manager", "Field Operations Manager"):
        assert title_matches(title, operations_targets), title


def test_unrelated_operations_titles_still_blocked_within_operations():
    """The other direction of the same pin: the deny-list, now scoped, must
    still block the exact Q21-measured pattern inside operations."""
    operations_targets = ("Operations Director", "Director of Operations", "Operations Manager")
    for title in ("Warehouse Operations Manager", "Clinical Operations Manager", "IT Operations Manager"):
        assert not title_matches(title, operations_targets), title


# --- Task 9 ----------------------------------------------------------------

def opportunity_with_contacts(count: int) -> ContactState:
    return ContactState(existing_count=count, existing_person_keys={f"p{i}" for i in range(count)},
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


# --- Fix round 1, I4 (Luis's ruling overrides the original acceptance test):
# "Three people in the same role are NOT diversification" is a fixed business
# rule, not a preference. select_next_contact must STOP (return None) once
# every persona present among the candidates is already held, rather than
# falling back to a same-persona pick.

def test_no_unheld_persona_left_stops_instead_of_repeating_a_persona():
    candidates = [
        {"person_key": "p4", "title": "Sales Director", "persona": "functional_owner"},
        {"person_key": "p5", "title": "VP Sales", "persona": "executive_leader"},
    ]
    chosen = select_next_contact(existing_personas=["functional_owner", "executive_leader"], candidates=candidates)
    assert chosen is None


# --- Fix round 1, C3 (CRITICAL, independent review): existing_person_keys and
# a search candidate's person_key must be the SAME identity namespace
# (person_ref()'s pid:/li: format), or the "never re-select a held person"
# check is comparing two sets that essentially never intersect -- dead code in
# the live path. This tests the extraction that makes the seeding provably
# correct: _build_contact_state must derive existing_person_keys from each
# approved row's OWN apollo_person_id/linkedin_url via person_ref(), not from
# the legacy suppression-tracking keys (email:<addr> / an imported
# contact_key) used only for the existing_count quota baseline.

def test_existing_person_keys_are_derived_via_person_ref_not_suppression_keys():
    from tgtc_core.domain.identity import person_ref
    from tgtc_core.services.opportunity import _build_contact_state

    existing_contacts = {"email:someone@acme.com"}   # legacy suppression-tracking key: a different namespace
    approved_rows = [{"apollo_person_id": "p-owner", "linkedin_url": None, "title": "Marketing Director"}]
    state = _build_contact_state(existing_contacts, approved_rows, "marketing")
    assert state.existing_person_keys == {person_ref(apollo_person_id="p-owner")}
    assert state.existing_count == len(existing_contacts)
    # the derived ref must be directly comparable to what a search candidate carries
    candidate = {"person_key": person_ref(apollo_person_id="p-owner"), "title": "Marketing Director", "persona": "functional_owner"}
    assert select_next_contact(candidates=[candidate], existing_person_keys=state.existing_person_keys) is None


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


# --- Fix round 1, I5 (Luis's ruling: fix in this round): a partially filled
# opportunity requeued at buyer_search_pending forever, with no give-up rule,
# so opportunities.state = 'approved' became near-unreachable below the
# configured quota. Once this evidence epoch's own paid-attempt budget
# (max_match_attempts_per_evidence_epoch * max_contacts) is exhausted, a
# partial opportunity must finalize with the contacts it already has instead
# of waiting again.

def test_partial_opportunity_finalizes_once_the_epoch_attempt_budget_is_exhausted(conn, clock):
    """Fix round 2 note: this must exercise the BUDGET-exhaustion path
    specifically (real untried candidates remain, but the epoch's paid-attempt
    cap is spent) -- not the (higher-priority) no-further-candidates path
    fix round 2 added, which fires whenever the candidate pool is fully
    consumed regardless of budget. A SECOND candidate is left untried when
    the budget cap breaks the loop, so this stays a distinct, meaningful
    scenario from test_two_persona_function_finalizes_instead_of_waiting_forever_at_default_quota."""
    domain, org = "acme.com", "Acme"
    owner = make_person(id="p-owner", first="Owner", last="Person", title="Marketing Director",
                        org_name=org, org_domain=domain, email="owner@acme.com", email_status="verified")
    executive = make_person(id="p-exec", first="Exec", last="Person", title="VP Marketing",
                            org_name=org, org_domain=domain, email="exec@acme.com", email_status="verified")
    _, eid, opp = seed_opportunity(conn, clock, function_key="marketing", domain=domain, org_name=org)
    # max_contacts=2 -> max_attempts = 3 (max_match_attempts_per_evidence_epoch) * 2 = 6.
    # Pre-seed 5 already-spent match attempts this epoch (a prior call's own
    # paid attempts), so THIS call's one remaining attempt (approving "owner",
    # the first-ranked candidate) exactly exhausts the budget: 5 + 1 = 6 >= 6,
    # breaking the loop with "executive" still untried in `remaining`.
    with conn.cursor() as cur:
        for i in range(5):
            cur.execute(
                "INSERT INTO candidate_attempts (opportunity_id, candidate_ref, attempt_kind, outcome, reason, epoch) "
                "VALUES (%s, %s, 'match', 'not_found', 'test_seed', 1)",
                (opp, f"pid:seed-{i}"),
            )
    conn.commit()
    fake = apollo_for(domain, org, function_key="marketing", people=[owner, executive])
    out = opportunity_service(conn, fake, clock, max_contacts_per_opportunity=2).process(opp)
    assert out.outcome == "approved" and out.reason == "approved_partial_quota_epoch_exhausted"
    assert out.details["approved_total"] == 1
    state = sqlall(conn, "SELECT state FROM opportunities WHERE id = %s", (opp,))[0]["state"]
    assert state == "approved", "a partial opportunity with its epoch budget exhausted must finalize, not requeue forever"


# --- Fix round 1, I2 (IMPORTANT, independent review): campaigns.is_founder_tier
# (task 7's fix) and contact_mapping.is_small_company_executive held two
# independently-written copies of the exact same "vice-aware president" regex
# -- contact_mapping's own docstring even names the bug task 7 fixed. Ruling:
# share within tgtc_core (contact_mapping is not deployed, but both modules
# are). This proves genuine delegation, not just coincidentally-matching
# behaviour: it fails if is_small_company_executive stops calling through to
# the shared predicate.

def test_is_small_company_executive_shares_campaigns_is_founder_tier(monkeypatch):
    import tgtc_core.domain.contact_mapping as cm

    calls = []

    def spy(title):
        calls.append(title)
        return False

    monkeypatch.setattr(cm, "is_founder_tier", spy)
    cm.is_small_company_executive("Vice President of Sales")
    assert calls, "is_small_company_executive never called the shared is_founder_tier predicate"


# --- Fix round 1, I3 (IMPORTANT, independent review): tasks 7 and 8 stamped
# rule_version nowhere, though both decisions flow into GateResult.evidence,
# which IS persisted (candidate_attempts.details). Pin both changed decision
# points in ONE matcher/predicate each: the title-match rejection (task 8) and
# the founder-tier rejection (task 7).

def test_title_mismatch_rejection_carries_rule_version():
    result = pre_enrichment_check(
        person={"id": "p1", "title": "Warehouse Associate"}, employer_name="Acme",
        employer_domains={"acme.com"}, buyer_titles=("VP Marketing", "Marketing Director"), founder_allowed=False,
    )
    assert not result.passed and result.reason == "contact:function_or_authority_mismatch"
    assert result.evidence.get("rule_version") == RULE_VERSION


def test_founder_tier_rejection_carries_rule_version():
    result = pre_enrichment_check(
        person={"id": "p1", "title": "Founder", "organization": {"name": "Acme"}}, employer_name="Acme",
        employer_domains={"acme.com"}, buyer_titles=("Founder", "CEO"), founder_allowed=False,
    )
    assert not result.passed and result.reason == "contact:founder_tier_not_allowed_for_size"
    assert result.evidence.get("rule_version") == RULE_VERSION


# --- Fix round 2, CRITICAL (independent review): round 1's give-up rule
# (I5) keyed termination on the paid-attempt BUDGET (matches_done + made >=
# max_attempts). But both "no progress" paths -- select_next_contact
# returning None (I4's hard stop) and a pass finding zero candidates at all
# -- reach that check with made = 0, making ZERO paid attempts, so the
# budget never advances. 9 of 10 functions expose exactly 2 reachable
# personas (only people_hr has a third); at the SHIPPED DEFAULT quota of 3
# (config.py's max_contacts_per_opportunity), such an opportunity approves 2
# contacts and then waits at buyer_search_pending forever, since it can
# never make the paid attempt that would advance the budget. Termination
# must key on NO PROGRESS (no further role-diverse candidate selectable),
# not on spend.

def test_two_persona_function_finalizes_instead_of_waiting_forever_at_default_quota(conn, clock):
    domain, org = "acme.com", "Acme"
    owner = make_person(id="p-owner", first="Owner", last="Person", title="Marketing Director",
                        org_name=org, org_domain=domain, email="owner@acme.com", email_status="verified")
    executive = make_person(id="p-exec", first="Exec", last="Person", title="VP Marketing",
                            org_name=org, org_domain=domain, email="exec@acme.com", email_status="verified")
    # A THIRD candidate, but marketing has only two personas -- this one is
    # necessarily a repeat of whichever persona is already held (functional_owner,
    # since "Growth Director" is also a DIRECT_BUYER_TITLES entry), so it can
    # never be selected once both personas are represented.
    third = make_person(id="p-third", first="Third", last="Person", title="Growth Director",
                        org_name=org, org_domain=domain, email="third@acme.com", email_status="verified")
    _, eid, opp = seed_opportunity(conn, clock, function_key="marketing", domain=domain, org_name=org)
    fake = apollo_for(domain, org, function_key="marketing", people=[owner, executive, third])
    # The SHIPPED DEFAULT quota (config.py's Settings.max_contacts_per_opportunity),
    # not the OpportunityService constructor's own lower bare default of 1.
    out = opportunity_service(conn, fake, clock, max_contacts_per_opportunity=3).process(opp)
    assert out.outcome == "approved" and out.reason == "approved_no_further_role_diverse_candidates"
    assert out.details["approved_total"] == 2
    state = sqlall(conn, "SELECT state FROM opportunities WHERE id = %s", (opp,))[0]["state"]
    assert state == "approved", "a two-persona function must finalize once diversity is exhausted, not wait forever"
