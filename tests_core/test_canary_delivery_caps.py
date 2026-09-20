"""Code-enforced canary caps on the Instantly write portion.

The bound is at most ten eligible net-new contacts per Challenger campaign and
ninety in total. It lives in code, not in a start command or a human's
attention, so a deploy cannot exceed it by accident.

Fewer than ten is acceptable: the cap is a ceiling, never a quota to fill.
"""
from __future__ import annotations

from tgtc_core.services.delivery import CanaryCaps

A = "8bfa0769-4b9a-4346-8e93-17ac8b726dce"
B = "7b319c7a-cc55-4e08-8a47-7058c345d8ae"


def test_disabled_by_default():
    caps = CanaryCaps.from_env({})
    assert caps.enabled is False
    for _ in range(500):
        assert caps.exceeded(A) is None
        caps.record(A)


def test_per_campaign_ceiling():
    caps = CanaryCaps.from_env({"TGTC_CANARY_MAX_PER_CAMPAIGN": "10", "TGTC_CANARY_MAX_TOTAL": "90"})
    assert caps.enabled is True
    for _ in range(10):
        assert caps.exceeded(A) is None
        caps.record(A)
    assert caps.exceeded(A) == "canary_cap_campaign"


def test_one_campaign_filling_up_does_not_block_another():
    caps = CanaryCaps.from_env({"TGTC_CANARY_MAX_PER_CAMPAIGN": "10", "TGTC_CANARY_MAX_TOTAL": "90"})
    for _ in range(10):
        caps.record(A)
    assert caps.exceeded(A) == "canary_cap_campaign"
    assert caps.exceeded(B) is None


def test_total_ceiling_stops_everything():
    caps = CanaryCaps.from_env({"TGTC_CANARY_MAX_PER_CAMPAIGN": "90", "TGTC_CANARY_MAX_TOTAL": "5"})
    for _ in range(5):
        assert caps.exceeded(A) is None
        caps.record(A)
    assert caps.exceeded(A) == "canary_cap_total"
    assert caps.exceeded(B) == "canary_cap_total"


def test_nine_campaigns_at_ten_each_is_exactly_ninety():
    caps = CanaryCaps.from_env({"TGTC_CANARY_MAX_PER_CAMPAIGN": "10", "TGTC_CANARY_MAX_TOTAL": "90"})
    written = 0
    for campaign in (f"campaign-{i}" for i in range(9)):
        while caps.exceeded(campaign) is None:
            caps.record(campaign)
            written += 1
    assert written == 90
    assert caps.exceeded("campaign-0") == "canary_cap_total"


def test_a_tenth_campaign_cannot_push_past_the_total():
    caps = CanaryCaps.from_env({"TGTC_CANARY_MAX_PER_CAMPAIGN": "10", "TGTC_CANARY_MAX_TOTAL": "90"})
    for i in range(9):
        for _ in range(10):
            caps.record(f"campaign-{i}")
    assert caps.exceeded("campaign-extra") == "canary_cap_total"


def test_only_one_of_the_two_limits_set_still_enables_the_cap():
    assert CanaryCaps.from_env({"TGTC_CANARY_MAX_TOTAL": "5"}).enabled is True
    assert CanaryCaps.from_env({"TGTC_CANARY_MAX_PER_CAMPAIGN": "3"}).enabled is True


def test_a_zero_or_negative_limit_blocks_every_write():
    caps = CanaryCaps.from_env({"TGTC_CANARY_MAX_TOTAL": "0"})
    assert caps.enabled is True
    assert caps.exceeded(A) == "canary_cap_total"
