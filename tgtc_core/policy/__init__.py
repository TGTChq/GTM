"""Versioned policy: campaigns, eligibility rules, buyer hierarchies."""

from .campaigns import (  # noqa: F401
    CAMPAIGNS, CAMPAIGN_BY_FUNCTION, CAMPAIGN_BY_KEY, CAMPAIGN_ENV_BY_FUNCTION,
    FUNCTION_KEYS, KNOWN_CONTROL_CAMPAIGN_IDS, POLICY_VERSION, Campaign,
    buyer_titles, campaign_env_names, campaign_for_function, campaign_route_configured,
    is_founder_tier, resolve_campaign_id, size_band,
)
from .requirements import RULES, describe, excluded_industry, rule  # noqa: F401
