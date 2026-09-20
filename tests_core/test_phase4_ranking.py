"""Phase 4 audit (2026-09-20), change 3: deterministic ranking before enrichment.

"A paid call is only ever spent on the best-ranked candidate" needs the
best-ranked candidate to be well defined. It was not.

Three defects in ``OpportunityService._rank``:

1. **It was not a total order.** The key was the index of the first matching
   buyer title and nothing else. ``sorted`` is stable, so every tie fell back to
   the order Apollo happened to return the people in -- an undocumented,
   provider-side ordering. Two runs over the same employer could buy two
   different people, and nothing in the artefacts would say why.
2. **A founder could outrank the functional owner.** The founder demotion
   (``+5``) sat in the branch for candidates matching NO buyer title -- which
   ``pre_enrichment_check`` has already rejected, so the branch is unreachable
   from the live path. A candidate titled "Founder & CTO" matches "CTO", takes
   the CTO's index, and is bought ahead of an actual Director of Engineering.
3. **Employer evidence was not a signal at all.** A candidate whose Apollo
   organization domain IS the employer's domain and one matched only by a
   compatible company NAME ranked identically, although the first is strictly
   stronger evidence -- and ``email:domain_not_employer`` was the single
   largest post-enrichment loss in Stage 2a (9 of 101 paid calls).

Not fixed here, and stated rather than assumed: Apollo's free people-search
returns NEITHER ``departments`` NOR ``seniority``. Both are empty on all 512
candidates Stage 2a saved (`phase5_apollo_2a/state/search.jsonl`), so neither
can inform a pre-enrichment ranking. The title is the only function evidence a
free search carries.
"""
from __future__ import annotations

import pytest

from tgtc_core.domain.contact_ranking import candidate_rank_key, rank_candidates
from tgtc_core.policy.campaigns import buyer_titles
from tgtc_core.services.opportunity import contact_persona


ENGINEERING = buyer_titles("engineering", founder_allowed=True)


def person(pid, title, *, org_domain="acme.com", org_name="Acme"):
    return {"id": pid, "_ref": f"pid:{pid}", "title": title,
            "organization": {"name": org_name, "primary_domain": org_domain}}


def ranked(people, titles=ENGINEERING, employer_domains=frozenset({"acme.com"}), employer_name="Acme"):
    return [p["id"] for p in rank_candidates(people, buyer_titles=titles,
                                             employer_domains=set(employer_domains), employer_name=employer_name)]


# --- 1. total order ---------------------------------------------------------

def test_ranking_is_a_total_order_over_candidates():
    """No two distinct candidates may compare equal: if they do, the winner is
    whatever order the provider returned them in."""
    people = [person("a", "Engineering Manager"), person("b", "Engineering Manager"),
              person("c", "Director of Engineering"), person("d", "VP Engineering")]
    keys = [candidate_rank_key(p, buyer_titles=ENGINEERING, employer_domains={"acme.com"}, employer_name="Acme")
            for p in people]
    assert len(set(keys)) == len(keys)


def test_the_same_candidates_in_any_provider_order_rank_identically():
    people = [person("a", "Engineering Manager"), person("b", "Director of Engineering"),
              person("c", "VP Engineering"), person("d", "Engineering Manager")]
    forward = ranked(people)
    assert ranked(list(reversed(people))) == forward
    assert ranked([people[2], people[0], people[3], people[1]]) == forward


# --- 2. a founder never outranks the named functional owner -----------------

def test_a_founder_who_also_holds_a_buyer_title_does_not_outrank_that_buyer():
    """"Founder & CTO" matches "CTO" and used to inherit the CTO's rank. A
    founder is the FALLBACK buyer -- the person policy allows when the company
    is too small to have the real one -- not the preferred one."""
    order = ranked([person("founder-cto", "Founder & CTO"), person("dir", "Director of Engineering")])
    assert order == ["dir", "founder-cto"]


def test_a_founder_is_still_ranked_when_no_one_else_qualifies():
    assert ranked([person("f", "Co-Founder & CEO")]) == ["f"]


# --- 3. employer evidence breaks ties, and only ties ------------------------

def test_exact_employer_domain_wins_a_tie_against_a_name_only_match():
    order = ranked([person("name-only", "Engineering Manager", org_domain="acme-holdings.example"),
                    person("exact", "Engineering Manager")])
    assert order == ["exact", "name-only"]


def test_employer_evidence_does_not_override_the_buyer_list_order():
    """Evidence breaks ties; it does not re-order the functions. The buyer
    list's own order is policy, and a weaker-evidence functional owner is still
    bought before a stronger-evidence executive."""
    order = ranked([person("exec", "VP Engineering"),
                    person("owner", "Engineering Manager", org_domain="acme-holdings.example")])
    assert order == ["owner", "exec"]


# --- the ordering agrees with the persona policy ---------------------------

def test_the_first_ranked_candidate_is_the_functional_owner_persona():
    people = [person("ta", "Head of Talent Acquisition"), person("exec", "VP Engineering"),
              person("owner", "Engineering Manager")]
    first = rank_candidates(people, buyer_titles=ENGINEERING, employer_domains={"acme.com"},
                            employer_name="Acme")[0]
    assert contact_persona(str(first["title"]), "engineering") == "functional_owner"


@pytest.mark.parametrize("function_key", ["engineering", "finance", "people_hr", "marketing", "ecommerce"])
def test_the_talent_persona_ranks_behind_the_campaigns_own_owners(function_key):
    titles = buyer_titles(function_key, founder_allowed=False)
    people = [person("ta", "Head of Talent Acquisition"),
              person("owner", titles[0]),
              person("exec", next(t for t in titles if contact_persona(t, function_key) == "executive_leader"))]
    order = ranked(people, titles=titles)
    assert order.index("ta") == 2, (function_key, order)


# --- reachable by the live pipeline ----------------------------------------

def test_the_service_buys_the_deterministically_best_candidate(conn, clock):
    """End to end with SIMULATED Apollo and a quota of 1: the one paid call goes
    to the functional owner, whatever order the provider lists the people in."""
    from tgtc_core.testing.fakes import make_person
    from tests_core.helpers import opportunity_service, sqlall
    from tests_core.seed import apollo_for, seed_opportunity

    domain, org = "acme.com", "Acme"
    people = [
        make_person(id="p-founder", first="F", last="Ounder", title="Founder & CTO",
                    org_name=org, org_domain=domain, email="f@acme.com", email_status="verified"),
        make_person(id="p-exec", first="E", last="Xec", title="VP Engineering",
                    org_name=org, org_domain=domain, email="e@acme.com", email_status="verified"),
        make_person(id="p-owner", first="O", last="Wner", title="Engineering Manager",
                    org_name=org, org_domain=domain, email="o@acme.com", email_status="verified"),
    ]
    _, _, opp = seed_opportunity(conn, clock, function_key="engineering", domain=domain, org_name=org)
    fake = apollo_for(domain, org, function_key="engineering", people=people, headcount=60)
    opportunity_service(conn, fake, clock, max_contacts_per_opportunity=1).process(opp)
    titles = [r["title"] for r in sqlall(
        conn, "SELECT p.title FROM approvals a JOIN people p ON p.id = a.person_id WHERE a.opportunity_id = %s", (opp,))]
    assert titles == ["Engineering Manager"], titles
