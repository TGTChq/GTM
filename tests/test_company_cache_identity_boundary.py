"""A cached display decision is evidence only for its recorded identities."""
import json
from pathlib import Path

import pytest

from company_display_resolver import CompanyDisplayCache, resolve_company_display


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("incoming", [
    {"org_linkedin_slug": "acme", "employer_domain": "globex.com"},
    {"org_linkedin_slug": "globex", "employer_domain": "acme.com"},
])
def test_same_one_anchor_cannot_reuse_decision_for_different_other_anchor(tmp_path, manual, incoming):
    path = tmp_path / "cache.json"
    payload = {
        "schema": "company-display-cache/1",
        "entries": {"linkedin:acme": {
            "display_name": "ACME", "confidence": "high", "identity_safe": True,
            "identity_keys": ["linkedin:acme", "domain:acme.com"],
            "manual_override": manual,
        }},
        "aliases": {"domain:acme.com": "linkedin:acme"},
    }
    path.write_text(json.dumps(payload))
    result = resolve_company_display(organization="Acme", cache=CompanyDisplayCache(path),
                                     persist=False, **incoming)
    assert result.hold is True
    assert result.evidence["cache_hit"] is False
    assert "linkedin_slug_domain_disagreement" in result.evidence["reasons"]
    assert json.loads(path.read_text()) == payload


@pytest.mark.parametrize("manual", [False, True])
def test_cache_entry_marked_unsafe_cannot_promote(tmp_path, manual):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "schema": "company-display-cache/1",
        "entries": {"linkedin:globex": {
            "display_name": "Acme", "confidence": "high", "identity_safe": False,
            "identity_keys": ["linkedin:globex", "domain:acme.com"],
            "manual_override": manual,
        }},
        "aliases": {},
    }))
    result = resolve_company_display(organization="Acme", org_linkedin_slug="globex",
                                     employer_domain="acme.com", cache=CompanyDisplayCache(path),
                                     persist=False)
    assert result.hold is True
    assert result.evidence["cache_hit"] is False


def test_rmc_reviewed_pair_recovers_only_the_display_gate(tmp_path):
    overrides = Path(__file__).resolve().parents[1] / "company_display_overrides.json"
    cache = CompanyDisplayCache(tmp_path / "cache.json", overrides_path=overrides)
    inputs = dict(organization="Resource Management Concepts, Inc.",
                  org_linkedin_slug="resource-management-concepts-inc-", employer_domain="rmcweb.com")
    before = resolve_company_display(**inputs, cache=CompanyDisplayCache(tmp_path / "empty.json"), persist=False)
    after = resolve_company_display(**inputs, cache=cache, persist=False)
    assert before.hold is True
    assert before.evidence["reasons"] == ["linkedin_slug_domain_disagreement"]
    assert after.hold is False
    assert after.name == "Resource Management Concepts"
    assert after.evidence["manual_override"] is True
    unsafe_domain = resolve_company_display(**{**inputs, "employer_domain": "globex.com"}, cache=cache, persist=False)
    unsafe_slug = resolve_company_display(**{**inputs, "org_linkedin_slug": "globex"}, cache=cache, persist=False)
    assert unsafe_domain.hold is True
    assert unsafe_slug.hold is True
