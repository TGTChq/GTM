"""The nine routes: the new registry agrees with the legacy one and env resolution matches."""

from __future__ import annotations

import importlib

import pytest

from tgtc_core.policy import campaigns as new


def test_nine_campaigns_ten_functions():
    assert len(new.CAMPAIGNS) == 9
    assert len(new.FUNCTION_KEYS) == 10
    assert new.CAMPAIGN_BY_FUNCTION["customer_success"] is new.CAMPAIGN_BY_FUNCTION["customer_support"]
    assert set(new.CAMPAIGN_ENV_BY_FUNCTION) == set(new.FUNCTION_KEYS)
    assert len(new.KNOWN_CONTROL_CAMPAIGN_IDS) == 9


def test_registry_matches_legacy_wave1_campaigns():
    """Drift guard: read the legacy module read-only and compare names/functions/env names."""
    legacy = importlib.import_module("outbound_wave1.campaigns")
    legacy_by_name = {p.name: p for p in legacy.CAMPAIGNS}
    assert set(legacy_by_name) == {c.name for c in new.CAMPAIGNS}
    for c in new.CAMPAIGNS:
        lp = legacy_by_name[c.name]
        assert tuple(lp.buckets) == c.functions, c.name
        assert set(lp.env_keys) == {new.CAMPAIGN_ENV_BY_FUNCTION[f] for f in c.functions}, c.name


def test_env_map_matches_legacy_config_map():
    legacy_config = importlib.import_module("config")
    assert dict(legacy_config.CAMPAIGN_ENV_BY_BUCKET) == new.CAMPAIGN_ENV_BY_FUNCTION


@pytest.mark.parametrize("function_key", new.FUNCTION_KEYS)
def test_every_function_resolves_a_campaign_id_from_env(function_key):
    env = {name: f"id-{name}" for name in new.CAMPAIGN_ENV_BY_FUNCTION.values()}
    assert new.resolve_campaign_id(function_key, 120, env) == f"id-{new.CAMPAIGN_ENV_BY_FUNCTION[function_key]}"


def test_size_band_override_and_global_fallback():
    env = {"INSTANTLY_CAMPAIGN_MARKETING": "base", "INSTANTLY_CAMPAIGN_MARKETING_SMALL": "small", "INSTANTLY_CAMPAIGN_ID": "global"}
    assert new.resolve_campaign_id("marketing", 50, env) == "small"
    assert new.resolve_campaign_id("marketing", 500, env) == "base"
    assert new.resolve_campaign_id("product", 500, env) == "global"
    assert new.resolve_campaign_id("product", 500, {}) == ""


def test_buyer_titles_founders_last_and_size_gated():
    with_founders = new.buyer_titles("finance", founder_allowed=True)
    without = new.buyer_titles("finance", founder_allowed=False)
    assert with_founders[0] == "Finance Director"          # direct managers first
    assert with_founders[-1] in ("CEO", "Co-Founder", "Founder")
    assert not any(new.is_founder_tier(t) for t in without)
    assert "Controller" in without
