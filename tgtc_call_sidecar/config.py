"""Sidecar configuration. Off by default; every limit is explicit."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

FLAG_ENV = "TGTC_CALL_LIST_SIDECAR"

#: The nine Challenger campaigns: the ONLY campaigns a follow-up call may come from.
CHALLENGER_CAMPAIGNS = {
    "product": "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0",
    "operations": "69def27c-7799-41a2-9ba8-205e54ab071b",
    "finance": "7b319c7a-cc55-4e08-8a47-7058c345d8ae",
    "people_hr": "d2326028-e312-405b-9e16-526bd309d4dd",
    "ecommerce": "c3e81c21-db44-40f5-addc-d9945a78394b",
    "customer_experience": "269cd138-00b1-48c3-9093-16c36120a20e",
    "marketing_creative": "1feb6344-6065-49d7-9764-d125985fb9c9",
    "gtm_systems": "8f25abd5-568a-4e88-b310-9acf85161c6c",
    "ai_technical": "8bfa0769-4b9a-4346-8e93-17ac8b726dce",
}
CHALLENGER_IDS = frozenset(CHALLENGER_CAMPAIGNS.values())
#: Control campaigns: never a source, never a destination.
CONTROL_IDS = frozenset({
    "45ac1e03-67e7-4bdd-b372-808042104e4c", "4effab2f-9073-46a9-b7ae-986ccc8f49c6",
    "1db88bbe-b2cf-4574-a5b7-1cb948151a86", "cf01e56b-e5ad-489e-a02c-c35c93cf3b53",
    "0f0f57d5-fab1-436d-b0d8-8cb43b031f03", "1747c87e-12e9-4477-bc4d-048223d39513",
    "165c9e87-c3e7-4e9c-9ccb-a8dbf5779726", "917973f3-c282-4a84-8da4-525a7a91819b",
    "04670c6a-828b-42cd-9dad-904592a63d9b",
})
CAMPAIGN_LABEL = {
    "product": "Product", "operations": "Operations", "finance": "Finance", "people_hr": "People & HR",
    "ecommerce": "Ecommerce", "customer_experience": "Customer Experience",
    "marketing_creative": "Marketing & Creative", "gtm_systems": "GTM Systems & Revenue Automation",
    "ai_technical": "AI & Technical Automation",
}

EMAIL_FOLLOW_UP = "EMAIL_FOLLOW_UP"
CALL_FIRST = "CALL_FIRST"
COHORTS = (EMAIL_FOLLOW_UP, CALL_FIRST)


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(FLAG_ENV, "0") or "0").strip() == "1"


@dataclass(frozen=True)
class PilotConfig:
    target_per_cohort: int = 100
    #: Separate hard cap for this pilot's Apollo reveals; never the core's daily grant.
    apollo_credit_cap: int = 2000
    #: Worst case per reveal (Apollo docs): 1 credit demographics/email + 8 for a mobile.
    max_credits_per_reveal: int = 9
    #: Reveals awaiting a poll result at once (each reserves its worst case).
    max_in_flight: int = 8
    poll_timeout_seconds: int = 900
    #: Core policy rule founder_fallback_max_employees.
    founder_max_employees: int = 99
    #: Follow-up cohort: at most this many people per company.
    follow_up_per_company: int = 2
