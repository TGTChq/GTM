"""Outbound Wave 1: deterministic Challenger-B messaging policy.

Control A is the live Instantly campaign copy. It is READ-ONLY here: nothing in
this package renders, rewrites or normalises it. A record assigned to arm A is
returned with empty rendered copy so the existing Instantly sequence keeps
running exactly as it does today.

Challenger B is not a static template. It is a policy resolved from the
opportunity record that already exists in Airtable:

    record -> account assignment -> signal -> scope -> proof -> offer -> render -> QA

Every stage fails closed. An unsupported claim degrades to a safer claim rather
than being guessed at, and a render that still cannot be supported is failed by
the QA gates so it is never enrolled.
"""

from __future__ import annotations

from .assignment import (
    ARM_A,
    ARM_B,
    ARM_NONE,
    account_assignment,
    company_assignment_key,
)
from .campaigns import (
    CAMPAIGN_BY_BUCKET,
    CAMPAIGNS,
    WAVE1_CAMPAIGN_NAMES,
    CampaignPolicy,
    campaign_for_bucket,
)
from .claims import ClaimRegistry, load_claim_registry
from .measurement import (
    STRATA,
    RandomizationRow,
    analyze,
    build_frame,
    randomization_row,
)
from .resolver import (
    COPY_VERSION,
    Wave1Resolution,
    resolve_batch,
    resolve_challenger,
    resolve_wave1,
    resolve_wave1_pair,
)
from .timing import SEQUENCE_OFFSET_DAYS, sequence_schedule

__all__ = [
    "ARM_A",
    "ARM_B",
    "ARM_NONE",
    "CAMPAIGNS",
    "CAMPAIGN_BY_BUCKET",
    "COPY_VERSION",
    "SEQUENCE_OFFSET_DAYS",
    "WAVE1_CAMPAIGN_NAMES",
    "STRATA",
    "CampaignPolicy",
    "ClaimRegistry",
    "RandomizationRow",
    "Wave1Resolution",
    "account_assignment",
    "analyze",
    "build_frame",
    "campaign_for_bucket",
    "company_assignment_key",
    "load_claim_registry",
    "randomization_row",
    "resolve_batch",
    "resolve_challenger",
    "resolve_wave1",
    "resolve_wave1_pair",
    "sequence_schedule",
]
