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
    "name,slug,domain,brand",
    [
        ("Clark", "clarkaudit", "getclark.com", "clark"),
        ("Blueground", "bluegroundco", "theblueground.com", "blueground"),
        ("Carpe", "carpe1", "mycarpe.com", "carpe"),
    ],
)
def test_real_branded_domain_pairs_clear_at_medium(tmp_path, name, slug, domain, brand):
    result = _resolve(tmp_path, name, slug, domain)
    assert result.hold is False
    assert result.identity_safe is True
    assert result.confidence == "medium"
    assert result.evidence["identity_conflict"] is True
    assert result.evidence["bridging_brand"] == brand
    assert "identity_conflict_bridged_by_shared_brand" in result.evidence["reasons"]


def test_a_bridged_conflict_is_never_promoted_to_high(tmp_path):
    """Corroboration proves the NAME, not that the anchors are one legal entity."""
    exact = _resolve(tmp_path, "Clark", "clark", "getclark.com")
    assert exact.confidence == "medium"
    assert exact.evidence["bridging_brand"] == "clark"


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
    length and the full name is in neither anchor, so the conflict still holds it."""
    result = _resolve(tmp_path, "AGS - American Gaming Systems",
                      "americangamingsystems", "playags.com")
    assert result.hold is True
    assert result.evidence["bridging_brand"] == ""
    assert "linkedin_slug_domain_disagreement" in result.evidence["reasons"]


def test_multi_entity_name_still_holds_once_a_conflict_is_bridged(tmp_path):
    """The ambiguity gate sits AFTER the conflict gate, so a bridged conflict must
    not let a franchise/multi-entity name through."""
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
