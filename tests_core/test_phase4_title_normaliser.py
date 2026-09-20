"""Phase 4 audit (2026-09-20), change 1: the shared title normaliser.

Measured defect (`phase5_apollo_2a/STAGE2A_REPORT.md` §3, `state/dropscan.jsonl`,
0 credits): over the frozen 100-unit frame the deployed selector dropped 70 people
with ``contact:function_or_authority_mismatch``. The titles are ordinary spellings
of titles the buyer hierarchies ALREADY list -- "Director Human Resources" (no
"of"), "Chief Technology Officer" (spelled out), "AVP Finance Policy Controls",
"Director of Financial Reporting", "SVP of Customer Experience".

Phase 2 task 8 collapsed the seniority wording (VP/SVP/EVP, Human Resources -> HR)
and bridged a leading "<Seniority>, <Function>" comma shape. What it cannot bridge
is WORD ORDER and inserted words, because it is still a contiguous-phrase search:
"Director of Human Resources" is a phrase, "Director Human Resources" is not a
substring of it, and neither is a substring of the other.

The rule this change adds is a TOKEN-SET rule, and it is deliberately
**additive**: a title that matched before still matches, with two named and
counted exceptions pinned below (the operations over-match guard restated
symmetrically, and titles that say the person is not an employee in this role
now). So it essentially cannot cost precision by dropping someone -- only by
admitting someone. The admissions are guarded (below) and counted.
"""
from __future__ import annotations

import pytest

from tgtc_core.domain.gates import pre_enrichment_check, title_matches
from tgtc_core.domain.title_norm import (
    canonical_title, content_tokens, is_department_label, non_decision_maker_title,
    not_an_employee_in_role,
)
from tgtc_core.policy.campaigns import buyer_titles, is_founder_tier


# --- canonicalisation: abbreviations and equivalent titles ------------------

@pytest.mark.parametrize("raw,expected", [
    # C-suite, spelled out -> the abbreviation the buyer lists are written in.
    ("Chief Executive Officer", "ceo"),
    ("Chief Technology Officer", "cto"),
    ("Chief Financial Officer", "cfo"),
    ("Chief Operating Officer", "coo"),
    ("Chief Operations Officer", "coo"),
    ("Chief Product Officer", "cpo"),
    ("Chief People Officer", "chro"),
    ("Chief Human Resources Officer", "chro"),
    # Vice-president family. "Assistant/Associate VP" is director-equivalent
    # (the same reading ``contact_mapping.seniority`` already uses for AVP),
    # so the word "assistant" is CONSUMED and must not then trip the
    # non-decision-maker guard below.
    ("Senior Vice President, Finance", "vp finance"),
    ("Executive Vice President Human Resources", "vp hr"),
    ("Sector Vice President of Engineering", "vp of engineering"),
    ("SVP HR", "vp hr"),
    ("EVP Operations", "vp operations"),
    ("AVP Financial Systems", "director finance systems"),
    ("Assistant Vice President Human Resources", "director hr"),
    # Ordinary abbreviations seen in the measured corpus.
    ("Marketing Mgr", "marketing manager"),
    ("Sr. Director, Finance", "senior director finance"),
    ("RevOps Manager", "revenue operations manager"),
    ("E-Commerce Director", "ecommerce director"),
    ("Cofounder", "co founder"),
    # Morphological variant of the same function word.
    ("Director of Financial Reporting", "director of finance reporting"),
])
def test_canonical_title_collapses_equivalent_spellings(raw, expected):
    assert canonical_title(raw) == expected


def test_content_tokens_drop_connectives_only():
    assert content_tokens("Director of Human Resources") == ("director", "hr")
    assert content_tokens("VP, Finance and Accounting") == ("vp", "finance", "accounting")


# --- the token-set rule: word order and inserted words ---------------------

@pytest.mark.parametrize("title,targets", [
    # Every one of these is a real dropped title from state/dropscan.jsonl.
    ("Director Human Resources", ("HR Director", "Director of Human Resources")),
    ("Managing Director Human Resources", ("HR Director",)),
    ("Director Test Engineering", ("Director Engineering",)),
    ("Director Accounting and HR", ("Accounting Director",)),
    ("Director Finance Analytics", ("Finance Director",)),
    ("AVP Finance Policy Controls", ("Director of Finance",)),
    ("SVP of Customer Experience", ("VP Customer Experience",)),
    ("Vice President of AI Integration", ("VP AI",)),
    ("Head Product Quality", ("Head of Product",)),
    ("Group Head of Financial Planning Analysis", ("Head of Finance",)),
    ("VP Financial Reporting", ("VP Finance",)),
])
def test_word_order_and_inserted_words_no_longer_lose_a_buyer(title, targets):
    assert title_matches(title, targets), title


def test_spelled_out_c_suite_matches_the_abbreviated_buyer_title():
    assert title_matches("Chief Executive Officer", ("CEO",))
    assert title_matches("Chief Technology Officer", ("CTO",))
    assert title_matches("Chief Operations Officer", ("COO",))


# --- the guards on the new path --------------------------------------------

def test_a_non_decision_maker_qualifier_does_not_reach_a_buyer_title_by_token():
    """The token rule ignores word order, so without this guard "Assistant
    Manager Human Resources" would reach "HR Manager" by token subset. The
    phrase rule never could, so this guard exists only to keep the NEW path
    from admitting people the old one correctly refused."""
    assert not title_matches("Assistant Manager Human Resources", ("HR Manager",))
    assert non_decision_maker_title("Assistant Manager Human Resources")


def test_an_assistant_keeps_the_phrase_match_it_already_had():
    """The counterpart pin: "assistant" guards the NEW path only. Dropping
    "Assistant Director of Marketing" from the phrase path would be a
    narrowing this change is not chartered to make, and it is not made."""
    assert title_matches("Assistant Director of Marketing", ("Director of Marketing",))
    assert title_matches("Assistant Controller", ("Controller",))


@pytest.mark.parametrize("title,targets", [
    ("Former Director of Finance", ("Finance Director", "Director of Finance")),
    ("Fractional Head of Marketing", ("Head of Marketing",)),
    ("Founder, Fractional CFO", ("CFO",)),
    ("Retired VP of Engineering", ("VP Engineering",)),
    ("Board Member", ("CEO", "Founder")),
])
def test_a_title_saying_the_person_is_not_an_employee_here_now_never_matches(title, targets):
    """The ONE narrowing on the phrase path, and the audit's "current versus
    former employer" item applied to the only evidence a free search carries.
    Counted, not asserted: 2 of 513 accepted candidates in the frozen frame."""
    assert not_an_employee_in_role(title), title
    assert not title_matches(title, targets), title


def test_the_not_currently_in_role_reason_is_distinct_at_the_search_gate():
    person = {"id": "x", "title": "Former Director of Finance",
              "organization": {"name": "Acme", "primary_domain": "acme.com"}}
    result = pre_enrichment_check(person=person, employer_name="Acme", employer_domains={"acme.com"},
                                  buyer_titles=("Finance Director", "Director of Finance"), founder_allowed=False)
    assert not result.passed
    assert result.reason == "contact:title_not_current_employee_in_role"


def test_an_avp_is_director_equivalent_and_not_blocked_as_an_assistant():
    """The AVP equivalence consumes "assistant": an Assistant Vice President is
    a real director-level buyer, not an assistant to somebody."""
    assert not non_decision_maker_title("Assistant Vice President Human Resources")
    assert title_matches("Assistant Vice President Human Resources", ("Director of Human Resources",))


def test_the_operations_over_match_guard_survives_the_token_rule():
    """Q21's measured over-match (warehouse/clinical/IT "Operations Manager")
    is scoped to the operations-ambiguous phrases and must hold on the token
    path too -- the token path has no notion of "the word immediately before",
    so it needs the guard restated over the token set, not skipped."""
    operations_targets = ("Operations Director", "Director of Operations", "Operations Manager")
    for title in ("Warehouse Operations Manager", "Clinical Operations Manager", "IT Operations Manager",
                  "Operations Manager, Warehouse", "Manager of Clinical Operations"):
        assert not title_matches(title, operations_targets), title
    for title in ("Retail Operations Manager", "Field Operations Manager", "Business Operations Manager",
                  "Operations Manager"):
        assert title_matches(title, operations_targets), title


def test_a_bare_department_name_is_not_a_job_title():
    """A person whose Apollo title is the department itself ("Marketing")
    carries no authority evidence at all. It was dropped before as a function
    mismatch; it is still dropped, but the reason now says which of the two it
    is, so the loss accounting stops conflating them."""
    assert is_department_label("Marketing")
    assert is_department_label("Human Resources")
    assert is_department_label("Customer Success")
    assert not is_department_label("Marketing Manager")
    assert not is_department_label("Head of Marketing")
    assert not title_matches("Marketing", ("Marketing Manager", "Marketing Director"))


def test_an_individual_contributor_title_is_not_a_department_label():
    """"No role token" alone would call every IC title a department name, which
    is both wrong and a DIFFERENT loss -- the person has a job, it is simply not
    a buying job. A department label is built only out of department words."""
    for title in ("Warehouse Associate", "Staff Accountant", "Software Engineer", "Account Executive"):
        assert not is_department_label(title), title


def test_the_department_label_reason_is_distinct_at_the_search_gate():
    person = {"id": "x", "title": "Marketing", "organization": {"name": "Acme", "primary_domain": "acme.com"}}
    result = pre_enrichment_check(person=person, employer_name="Acme", employer_domains={"acme.com"},
                                  buyer_titles=("Marketing Manager", "Marketing Director"), founder_allowed=False)
    assert not result.passed
    assert result.reason == "contact:title_is_department_not_role"


# --- the property that makes this change safe to ship ----------------------

STRICTLY_ADDITIVE_CASES = [
    ("Vice President, Revenue Operations", ("VP Revenue Operations",)),
    ("SVP Engineering", ("VP Engineering",)),
    ("Director, Customer Success", ("Customer Success Director",)),
    ("Human Resources Manager", ("HR Manager",)),
    ("Head of People Operations", ("Head of People",)),
    ("Plant Controller", ("Controller",)),
    ("Field Marketing Manager", ("Marketing Manager",)),
    ("Security Engineering Manager", ("Engineering Manager",)),
]


@pytest.mark.parametrize("title,targets", STRICTLY_ADDITIVE_CASES)
def test_every_title_that_matched_before_still_matches(title, targets):
    assert title_matches(title, targets), title


def test_every_buyer_title_still_matches_its_own_list_for_all_nine_campaigns():
    """The two modules must keep agreeing end to end, now across every function
    key rather than the four the phase-2 test sampled."""
    from tgtc_core.policy.campaigns import FUNCTION_KEYS

    for fn in FUNCTION_KEYS:
        titles = buyer_titles(fn, founder_allowed=True)
        for t in titles:
            assert title_matches(t, titles), (fn, t)


def test_the_normaliser_does_not_reopen_the_vice_president_founder_collision():
    """Phase 2 task 7 fixed "Vice President of Sales" being founder-tier because
    of the bare "president" token. The normaliser rewrites exactly that phrase,
    so the collision has to be pinned against the CANONICAL form as well."""
    for title in ("Vice President of Sales", "SVP Finance", "EVP Operations",
                  "Assistant Vice President Human Resources", "Sector Vice President of Engineering"):
        assert not is_founder_tier(title), title
        assert not is_founder_tier(canonical_title(title)), title
    for title in ("President", "President & CEO", "Chief Executive Officer", "Cofounder", "Owner"):
        assert is_founder_tier(title), title
        assert is_founder_tier(canonical_title(title)), title
