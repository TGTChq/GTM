"""Delivery re-validates the destination at send time, not only at approval.

The stored payload carries the campaign the approval was signed for. Production
holds 86 pending rows whose stored campaign is a retired CONTROL id, and
delivery only ever checked that the campaign was ACTIVE -- which the Control
campaigns still are. Without this guard, draining the outbox enrols real people
into retired campaigns.
"""
from __future__ import annotations

from tgtc_core.domain.exhaustive_routing import FLAG_ENV
from tgtc_core.services.delivery import retired_campaign_block_reason

CONTROL_OPERATIONS = "4effab2f-9073-46a9-b7ae-986ccc8f49c6"
CHALLENGER_OPERATIONS = "69def27c-7799-41a2-9ba8-205e54ab071b"
CONFIGURED = (CHALLENGER_OPERATIONS,)
ON = {FLAG_ENV: "1"}


def test_a_configured_challenger_destination_is_not_blocked():
    assert retired_campaign_block_reason(CHALLENGER_OPERATIONS, CONFIGURED, ON) is None


def test_a_retired_control_destination_is_blocked_with_a_named_reason():
    reason = retired_campaign_block_reason(CONTROL_OPERATIONS, CONFIGURED, ON)
    assert reason == "retired_control_campaign"


def test_an_unconfigured_unknown_destination_is_blocked():
    assert retired_campaign_block_reason("not-a-campaign", CONFIGURED, ON) == "campaign_not_configured"


def test_nothing_is_blocked_with_the_flag_off():
    """Rollback path stays byte-identical."""
    assert retired_campaign_block_reason(CONTROL_OPERATIONS, CONFIGURED, {}) is None
    assert retired_campaign_block_reason("not-a-campaign", CONFIGURED, {}) is None


def test_no_configuration_at_all_blocks_rather_than_guesses():
    assert retired_campaign_block_reason(CHALLENGER_OPERATIONS, (), ON) == "campaign_not_configured"
