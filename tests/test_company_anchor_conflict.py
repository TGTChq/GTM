"""The slug/domain anchor disagreement and when a shared brand may bridge it.

`_anchors_conflict` is unchanged: it still reports, truthfully, that the LinkedIn
slug and the domain brand differ as strings. What changed is that a disagreement
only HOLDS the row when the chosen display name is not the brand both anchors are
built from.

Every "should clear" case below is a real pair from the production Approved
backlog. Every "should stay held" case is either a real unsafe pair from the same
backlog or a class the diagnosis identified as needing protection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from company_display_resolver import (
    CompanyDisplayCache,
    _anchors_conflict,
    _bridging_brand,
    _domain_brand,
    _MIN_BRIDGING_BRAND,
    resolve_company_display,
)


def _cache(tmp_path):
    return CompanyDisplayCache(Path(tmp_path) / "cache.json")


def _resolve(tmp_path, name, slug, domain, **kwargs):
    return resolve_company_display(
        organization=name,
        canonical_company_name=name,
        org_linkedin_slug=slug,
        employer_domain=domain,
        cache=_cache(tmp_path),
        persist=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The string test itself is untouched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "slug,domain,conflicts",
    [
        ("clarkaudit", "getclark", True),
        ("acme", "acme", False),
        ("acmecorp", "acme", False),      # shared prefix, >= 4 chars
        ("globex", "acme", True),
        ("", "acme", False),              # a missing anchor is not a conflict
        ("acme", "", False),
    ],
)
def test_anchors_conflict_is_unchanged(slug, domain, conflicts):
    assert _anchors_conflict(slug, domain) is conflicts


# ---------------------------------------------------------------------------
# Real same-company pairs from the production backlog -- should clear
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,slug,domain,brand,route",
    [
        # ``get``/``my`` are registrar vanity prefixes, so the domain reads as the
        # brand -- but the SLUG still extends it (``clarkaudit``), which is the same
        # relation as ``apple`` inside ``applebank``. Corroboration decides.
        ("Clark", "clarkaudit", "getclark.com", "clark", "prefix"),
        ("Carpe", "carpe1", "mycarpe.com", "carpe", "prefix"),
        # ``the`` is NOT a vanity prefix here: stripping it would read
        # ``theresa.com`` as the company "Resa". So this stays a real conflict, and
        # the name sitting inside both anchors is a candidate signal, not evidence.
        ("Blueground", "bluegroundco", "theblueground.com", "blueground", "conflict"),
    ],
)
def test_real_branded_domain_pairs_clear_at_medium(tmp_path, name, slug, domain, brand, route):
    """These pairs clear at medium -- WHEN the organization corroborates them.

    They used to clear on shape alone: the published name sat inside both anchors,
    or opened the longer one, and that was taken as proof of one organization. It is
    not. ``Apple`` opens ``applebank`` exactly the way ``Clark`` opens
    ``clarkaudit``, so any rule that clears one clears the other.

    What separates them is the organization's own declared website. With it these
    clear, at medium and never high, because the agreement still rests on a naming
    convention. Without it they hold -- asserted in the companion test below.
    """
    attested = _resolve(tmp_path, name, slug, domain,
                        org_linkedin_website=f"https://{domain}")
    assert attested.hold is False
    assert attested.identity_safe is True
    assert attested.confidence == "medium"
    assert attested.evidence["identifier_agreement"]["kind"] == route
    assert attested.evidence["conflict_resolution"]["basis"] == (
        "linkedin_organization_profile_website")
    if route == "conflict":
        # The shared brand is still recorded -- as a candidate signal that settled
        # nothing, so a reader can see what was considered and rejected.
        assert attested.evidence["bridging_brand"] == brand


@pytest.mark.parametrize("name,slug,domain,brand,route", [
    ("Clark", "clarkaudit", "getclark.com", "clark", "prefix"),
    ("Carpe", "carpe1", "mycarpe.com", "carpe", "prefix"),
    ("Blueground", "bluegroundco", "theblueground.com", "blueground", "conflict"),
])
def test_the_same_pairs_hold_when_nothing_corroborates_them(tmp_path, name, slug,
                                                            domain, brand, route):
    """The other half of the contract, and the reason the homonym is now caught."""
    bare = _resolve(tmp_path, name, slug, domain)
    assert bare.hold is True
    assert bare.evidence["conflict_resolution"]["resolved"] is False
    assert bare.evidence["missing_evidence"]["need"]


def test_a_convention_only_agreement_is_never_promoted_to_high(tmp_path):
    """Corroboration proves the NAME, not that the anchors are one legal entity.

    ``clark`` and ``getclark.com`` line up only because we strip a prefix a
    registrar era made conventional. That is very probably right and it is not a
    fact the domain states, so it must not buy the confidence reserved for an
    identifier that spells the name outright. Here the slug is exactly ``clark``, so
    the identifiers AGREE and nothing needs corroborating -- only the cap is at issue.
    """
    exact = _resolve(tmp_path, "Clark", "clark", "getclark.com")
    assert exact.hold is False
    assert exact.confidence == "medium"
    assert "identifiers_agree_only_after_vanity_prefix" in exact.evidence["reasons"]
    assert exact.evidence["identifier_agreement"]["convention_only"] is True


def test_a_brand_tld_is_not_a_disagreement(tmp_path):
    """``kai.security`` and the slug ``kaisecurity`` are the same string.

    Splitting the domain on its first dot threw away half the brand and reported a
    conflict against an identifier that matched exactly. Reading the whole domain
    is a literal fact about it, not a convention, so this one DOES reach high.
    """
    result = _resolve(tmp_path, "Kai", "kaisecurity", "kai.security")
    assert result.hold is False
    assert result.confidence == "high"
    agreement = result.evidence["identifier_agreement"]
    assert agreement["conflict"] is False
    assert agreement["convention_only"] is False
    assert agreement["agreed_on"]["domain_form"] == "domain_full_label_key"


# ---------------------------------------------------------------------------
# Real unsafe pairs -- must stay held
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,name,slug,domain",
    [
        ("unrelated_company", "Acme", "globex", "acme.com"),
        ("government_portal", "Massachusetts Department of Developmental Services",
         "ddsmass", "mass.gov"),
        ("parent_company", "Diamond Jo Casino & Hotel", "diamondjoworth", "boydgaming.com"),
        ("state_portal", "DC Department of Human Resources", "dchumanresources", "dc.gov"),
        ("parent_gaming_group", "Blue Chip Casino Hotel Spa", "diamondjoworth", "boydgaming.com"),
    ],
)
def test_known_unsafe_pairs_stay_held(tmp_path, label, name, slug, domain):
    result = _resolve(tmp_path, name, slug, domain)
    assert result.hold is True, label
    assert result.identity_safe is False, label
    assert "linkedin_slug_domain_disagreement" in result.evidence["reasons"], label
    assert result.evidence["bridging_brand"] == "", label


def test_acronym_collision_does_not_bridge(tmp_path):
    """A short token shared by two unrelated companies is coincidence."""
    result = _resolve(tmp_path, "Hex", "hextechnologies", "hexagon.com")
    assert result.hold is True
    assert result.evidence["bridging_brand"] == ""


def test_bridging_brand_has_a_minimum_length():
    chosen = {"cleaned": "ABC", "identity_matches": {"linkedin": "prefix", "domain": ""}}
    assert _MIN_BRIDGING_BRAND == 4
    assert _bridging_brand(chosen, "abcholdings", "abcgroup") == ""


def test_real_multi_entity_name_with_a_conflict_stays_held(tmp_path):
    """AGS is a real backlog row. The brand ("ags") is below the minimum bridging
    length and the full name is in neither anchor, so it is still held.

    The REASON reported changed and the outcome did not. Name-quality checks now run
    before the identity relation, because "this label names several entities" is a
    more specific and more actionable diagnosis than "the identifiers disagree", and
    this row is both. Either way the row is held and nothing is sent.
    """
    result = _resolve(tmp_path, "AGS - American Gaming Systems",
                      "americangamingsystems", "playags.com")
    assert result.hold is True
    assert result.evidence["bridging_brand"] == ""
    assert result.evidence["identifier_agreement"]["contradictory"] is True
    assert "unresolved_multi_entity_or_franchise_name" in result.evidence["reasons"]


def test_multi_entity_name_still_holds_even_when_the_identifiers_are_bridged(tmp_path):
    """A franchise/multi-entity label must never pass, whatever the identifiers do."""
    result = _resolve(tmp_path, "Blue / Ground", "bluegroundco", "theblueground.com")
    assert result.evidence["bridging_brand"] == "blueground"  # the bridge does apply
    assert result.hold is True                                # and the row is still held
    assert "unresolved_multi_entity_or_franchise_name" in result.evidence["reasons"]


def test_a_name_in_only_one_anchor_does_not_bridge(tmp_path):
    """Containment must hold on BOTH sides -- one side is the old, unsafe shape."""
    result = _resolve(tmp_path, "ABB Optical Group",
                      "abbconciseopticalgroup", "abboptical.com")
    assert result.hold is True
    assert result.evidence["bridging_brand"] == ""


# ---------------------------------------------------------------------------
# The bridge predicate in isolation
# ---------------------------------------------------------------------------

def test_bridging_requires_existing_corroboration():
    """Containment alone must not bridge; the corroboration bar still applies."""
    uncorroborated = {"cleaned": "Clark", "identity_matches": {"linkedin": "", "domain": ""}}
    assert _bridging_brand(uncorroborated, "clarkaudit", "getclark") == ""
    corroborated = {"cleaned": "Clark", "identity_matches": {"linkedin": "prefix", "domain": ""}}
    assert _bridging_brand(corroborated, "clarkaudit", "getclark") == "clark"


def test_bridging_is_fail_closed_on_missing_inputs():
    chosen = {"cleaned": "Clark", "identity_matches": {"linkedin": "prefix"}}
    assert _bridging_brand(None, "clarkaudit", "getclark") == ""
    assert _bridging_brand(chosen, "", "getclark") == ""
    assert _bridging_brand(chosen, "clarkaudit", "") == ""


def test_bridging_handles_ampersand_and_legal_suffixes():
    chosen = {
        "cleaned": "Smith & Jones Inc",
        "identity_matches": {"linkedin": "prefix", "domain": ""},
    }
    assert _bridging_brand(chosen, "smithandjonesgroup", "getsmithandjones") == "smithandjones"


def test_bridging_prefers_the_longest_qualifying_brand():
    chosen = {
        "cleaned": "Blueground Ltd",
        "identity_matches": {"linkedin": "prefix", "domain": ""},
    }
    assert _bridging_brand(chosen, "bluegroundltdco", "thebluegroundltd") == "bluegroundltd"


# ---------------------------------------------------------------------------
# No behaviour change where the anchors already agree
# ---------------------------------------------------------------------------

def test_agreeing_anchors_are_unaffected(tmp_path):
    result = _resolve(tmp_path, "Acme", "acme", "acme.com")
    assert result.hold is False
    assert result.confidence == "high"
    assert result.evidence["identity_conflict"] is False
    assert result.evidence["bridging_brand"] == ""
    assert "identity_conflict_bridged_by_shared_brand" not in result.evidence["reasons"]


def test_uncorroborated_name_without_a_conflict_is_unchanged(tmp_path):
    result = _resolve(tmp_path, "Totally Different Name", "acme", "acme.com")
    assert result.hold is True
    assert "selected_name_not_corroborated_by_identity" in result.evidence["reasons"]


def test_missing_identity_is_unchanged(tmp_path):
    result = _resolve(tmp_path, "Acme", "", "")
    assert result.hold is True
    assert "no_stable_linkedin_or_domain_identity" in result.evidence["reasons"]


def test_domain_brand_extraction_is_unchanged():
    assert _domain_brand("getclark.com") == "getclark"
    assert _domain_brand("mass.gov") == "mass"
    assert _domain_brand("") == ""


def test_evidence_always_records_the_bridge_decision(tmp_path):
    """Auditability: every result says whether a conflict existed and what bridged it."""
    for name, slug, domain in (
        ("Clark", "clarkaudit", "getclark.com"),
        ("Acme", "globex", "acme.com"),
        ("Acme", "acme", "acme.com"),
    ):
        evidence = _resolve(tmp_path, name, slug, domain).evidence
        assert "identity_conflict" in evidence
        assert "bridging_brand" in evidence
