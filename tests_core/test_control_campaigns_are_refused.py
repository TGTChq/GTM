"""Under the exhaustive scope a retired Control campaign is never a destination.

Eighteen campaigns exist in the workspace: nine previous (Control) and nine
current (Challenger). `approval.py` has always accepted a Control id outright,
as a fallback for a workspace with no configuration. That fallback is now a
hazard: production holds 86 pending outbox rows whose stored campaign_id is a
CONTROL id, and draining them would enrol 86 real people into retired
campaigns.

With the flag on, only a CONFIGURED campaign id is a valid destination.
"""
from __future__ import annotations

from tgtc_core.domain.exhaustive_routing import FLAG_ENV
from tgtc_core.policy.campaigns import (
    KNOWN_CHALLENGER_CAMPAIGN_IDS, KNOWN_CONTROL_CAMPAIGN_IDS, campaign_id_allowed,
)

CONTROL_OPERATIONS = "4effab2f-9073-46a9-b7ae-986ccc8f49c6"
CHALLENGER_OPERATIONS = "69def27c-7799-41a2-9ba8-205e54ab071b"
CONFIGURED = (CHALLENGER_OPERATIONS,)


def test_a_configured_challenger_id_is_allowed_in_both_modes():
    assert campaign_id_allowed(CHALLENGER_OPERATIONS, CONFIGURED, env={}) is True
    assert campaign_id_allowed(CHALLENGER_OPERATIONS, CONFIGURED, env={FLAG_ENV: "1"}) is True


def test_an_unconfigured_control_id_is_still_accepted_with_the_flag_off():
    """Unchanged legacy behaviour, so the rollback path is byte-identical."""
    assert campaign_id_allowed(CONTROL_OPERATIONS, CONFIGURED, env={}) is True


def test_an_unconfigured_control_id_is_refused_with_the_flag_on():
    assert campaign_id_allowed(CONTROL_OPERATIONS, CONFIGURED, env={FLAG_ENV: "1"}) is False


def test_every_control_id_is_refused_when_only_challengers_are_configured():
    for cid in KNOWN_CONTROL_CAMPAIGN_IDS:
        assert campaign_id_allowed(cid, tuple(KNOWN_CHALLENGER_CAMPAIGN_IDS), env={FLAG_ENV: "1"}) is False, cid


def test_every_challenger_id_is_allowed_when_configured():
    configured = tuple(KNOWN_CHALLENGER_CAMPAIGN_IDS)
    for cid in KNOWN_CHALLENGER_CAMPAIGN_IDS:
        assert campaign_id_allowed(cid, configured, env={FLAG_ENV: "1"}) is True, cid


def test_an_unknown_id_is_refused_in_both_modes():
    assert campaign_id_allowed("not-a-campaign", CONFIGURED, env={}) is False
    assert campaign_id_allowed("not-a-campaign", CONFIGURED, env={FLAG_ENV: "1"}) is False


def test_a_control_id_that_is_explicitly_configured_is_still_allowed():
    """The flag refuses the implicit fallback, not a deliberate configuration."""
    assert campaign_id_allowed(CONTROL_OPERATIONS, (CONTROL_OPERATIONS,), env={FLAG_ENV: "1"}) is True
