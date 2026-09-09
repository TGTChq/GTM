"""The nine live campaigns, the ten function keys and the buyer hierarchies.

Ported from ``outbound_wave1/campaigns.py``, ``config.CAMPAIGN_ENV_BY_BUCKET`` and
``role_mapping.BUCKET_DIRECT_TITLES`` / ``BUCKET_TITLES`` at ``main = 5d87851``.
``tests_core/test_campaign_registry.py`` asserts this module and the legacy one
still agree, so the two cannot drift apart silently.

Nothing here is copy. Campaign ids are resolved from the environment at runtime
(``resolve_campaign_id``); the Control ids listed below are used only to REFUSE a
payload aimed at a campaign that is neither a configured Control nor a configured
Challenger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

POLICY_VERSION = "tgtc-core/1"


@dataclass(frozen=True)
class Campaign:
    key: str
    name: str
    functions: Tuple[str, ...]
    #: Display noun used when a posting has no usable title of its own.
    function_nouns: Mapping[str, str]


PRODUCT = Campaign("product", "PRODUCT", ("product",), {"product": "product role"})
OPERATIONS = Campaign("operations", "OPERATIONS", ("operations",), {"operations": "operations role"})
FINANCE = Campaign("finance", "FINANCE", ("finance",), {"finance": "finance role"})
PEOPLE_HR = Campaign("people_hr", "PEOPLE & HR", ("people_hr",), {"people_hr": "people operations role"})
ECOMMERCE = Campaign("ecommerce", "ECOMMERCE", ("ecommerce",), {"ecommerce": "ecommerce role"})
CUSTOMER_EXPERIENCE = Campaign(
    "customer_experience", "CUSTOMER EXPERIENCE", ("customer_success", "customer_support"),
    {"customer_success": "customer success role", "customer_support": "customer support role"},
)
MARKETING_CREATIVE = Campaign("marketing_creative", "MARKETING & CREATIVE", ("marketing",), {"marketing": "marketing role"})
GTM_SYSTEMS = Campaign(
    "gtm_systems", "GTM SYSTEMS & REVENUE AUTOMATION", ("gtm_revenue",),
    {"gtm_revenue": "revenue operations role"},
)
AI_TECHNICAL = Campaign(
    "ai_technical", "AI & TECHNICAL AUTOMATION", ("engineering",),
    {"engineering": "engineering role"},
)

CAMPAIGNS: Tuple[Campaign, ...] = (
    PRODUCT, OPERATIONS, FINANCE, PEOPLE_HR, ECOMMERCE,
    CUSTOMER_EXPERIENCE, MARKETING_CREATIVE, GTM_SYSTEMS, AI_TECHNICAL,
)
CAMPAIGN_BY_KEY: Dict[str, Campaign] = {c.key: c for c in CAMPAIGNS}
CAMPAIGN_BY_FUNCTION: Dict[str, Campaign] = {f: c for c in CAMPAIGNS for f in c.functions}
FUNCTION_KEYS: Tuple[str, ...] = tuple(f for c in CAMPAIGNS for f in c.functions)

#: Environment variable that holds the Instantly campaign id for each function.
#: Same names production uses today (``config.CAMPAIGN_ENV_BY_BUCKET``).
CAMPAIGN_ENV_BY_FUNCTION: Dict[str, str] = {
    "gtm_revenue": "INSTANTLY_CAMPAIGN_GTM",
    "engineering": "INSTANTLY_CAMPAIGN_ENGINEERING",
    "marketing": "INSTANTLY_CAMPAIGN_MARKETING",
    "customer_success": "INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS",
    "customer_support": "INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT",
    "finance": "INSTANTLY_CAMPAIGN_FINANCE",
    "operations": "INSTANTLY_CAMPAIGN_OPERATIONS",
    "people_hr": "INSTANTLY_CAMPAIGN_PEOPLE_HR",
    "product": "INSTANTLY_CAMPAIGN_PRODUCT",
    "ecommerce": "INSTANTLY_CAMPAIGN_ECOMMERCE",
}

#: Live Control campaign ids (``validate_wave1_variables.CONTROL_CAMPAIGN_IDS``).
#: Not secrets. Used only as an allow-list guard.
KNOWN_CONTROL_CAMPAIGN_IDS = frozenset({
    "45ac1e03-67e7-4bdd-b372-808042104e4c",  # PRODUCT
    "4effab2f-9073-46a9-b7ae-986ccc8f49c6",  # OPERATIONS
    "1db88bbe-b2cf-4574-a5b7-1cb948151a86",  # FINANCE
    "cf01e56b-e5ad-489e-a02c-c35c93cf3b53",  # PEOPLE & HR
    "0f0f57d5-fab1-436d-b0d8-8cb43b031f03",  # ECOMMERCE
    "1747c87e-12e9-4477-bc4d-048223d39513",  # CUSTOMER EXPERIENCE
    "165c9e87-c3e7-4e9c-9ccb-a8dbf5779726",  # MARKETING & CREATIVE
    "917973f3-c282-4a84-8da4-525a7a91819b",  # GTM SYSTEMS
    "04670c6a-828b-42cd-9dad-904592a63d9b",  # AI & TECHNICAL
})


def campaign_for_function(function_key: str) -> Optional[Campaign]:
    return CAMPAIGN_BY_FUNCTION.get(str(function_key or "").strip().lower())


def resolve_campaign_id(function_key: str, employee_count: Optional[int], env: Mapping[str, str]) -> str:
    """The exact Instantly campaign id for a function, from configured env names.

    Mirrors ``config.resolve_campaign_id``: an optional size-band override
    ``<ENV>_<SMALL|MID|LARGE>`` wins, then the function's env name, then the global
    ``INSTANTLY_CAMPAIGN_ID``. An empty result means "no campaign configured" and
    the caller must refuse to approve.
    """
    base_env = CAMPAIGN_ENV_BY_FUNCTION.get(str(function_key or "").strip().lower())
    if base_env:
        band = size_band(employee_count).upper()
        specific = str(env.get(f"{base_env}_{band}", "") or "").strip()
        if specific:
            return specific
        bucket_campaign = str(env.get(base_env, "") or "").strip()
        if bucket_campaign:
            return bucket_campaign
    return str(env.get("INSTANTLY_CAMPAIGN_ID", "") or "").strip()


def size_band(employee_count: Optional[int]) -> str:
    if employee_count is None:
        return "unknown"
    if employee_count < 100:
        return "small"
    if employee_count < 500:
        return "mid"
    return "large"


# ---------------------------------------------------------------------------
# Buyer hierarchies (legacy ``role_mapping``; policy data, not control flow)
# ---------------------------------------------------------------------------

#: Direct functional managers, searched before executives.
DIRECT_BUYER_TITLES: Dict[str, Tuple[str, ...]] = {
    "gtm_revenue": ("Sales Director", "Director of Sales", "Revenue Operations Director",
                    "Director of Revenue Operations", "Revenue Operations Manager",
                    "Sales Operations Director", "Sales Operations Manager"),
    "engineering": ("Engineering Manager", "Software Engineering Manager",
                    "Director of Engineering", "Director Engineering"),
    "marketing": ("Marketing Director", "Director of Marketing", "Growth Director",
                  "Director of Growth", "Marketing Manager"),
    "customer_success": ("Customer Success Director", "Director of Customer Success",
                         "Customer Experience Director"),
    "customer_support": ("Customer Support Director", "Director of Customer Support",
                         "Support Director", "Customer Support Manager", "Support Manager"),
    "finance": ("Finance Director", "Director of Finance", "Accounting Director",
                "Director of Accounting", "Accounting Manager"),
    "operations": ("Operations Director", "Director of Operations", "Operations Manager"),
    "people_hr": ("HR Director", "Human Resources Director", "Director of Human Resources",
                  "People Operations Director", "Director of People Operations",
                  "Talent Acquisition Director", "HR Manager"),
    "product": ("Product Director", "Director of Product", "Product Design Director",
                "Director of Product Design", "Design Director"),
    "ecommerce": ("Ecommerce Director", "E-commerce Director", "Director of Ecommerce"),
}

#: Executive hierarchy per function. Founder-tier titles are listed LAST and are
#: only searched when ``founder_allowed`` (employer <= FOUNDER_FALLBACK_MAX_EMPLOYEES).
EXECUTIVE_BUYER_TITLES: Dict[str, Tuple[str, ...]] = {
    "gtm_revenue": ("Head of Revenue Operations", "Head of RevOps", "VP Revenue Operations",
                    "VP of Revenue Operations", "Chief Revenue Officer", "CRO", "Head of GTM",
                    "VP Sales", "VP of Sales", "Founder", "Co-Founder", "CEO"),
    "engineering": ("CTO", "Chief Technology Officer", "VP Engineering", "VP of Engineering",
                    "Head of Engineering", "Head of AI", "VP AI", "Founder", "Co-Founder", "CEO"),
    "marketing": ("CMO", "Chief Marketing Officer", "VP Marketing", "VP of Marketing",
                  "Head of Marketing", "Head of Growth", "VP Growth", "Founder", "Co-Founder", "CEO"),
    "customer_success": ("Chief Customer Officer", "VP Customer Success", "VP of Customer Success",
                         "Head of Customer Success", "Head of Customer Experience",
                         "VP Customer Experience", "COO", "Chief Operating Officer",
                         "Founder", "Co-Founder", "CEO"),
    "customer_support": ("VP Customer Support", "VP of Customer Support", "Head of Customer Support",
                         "Head of Support", "Head of Customer Experience", "VP Customer Experience",
                         "Chief Customer Officer", "COO", "Chief Operating Officer",
                         "Founder", "Co-Founder", "CEO"),
    "finance": ("CFO", "Chief Financial Officer", "VP Finance", "VP of Finance", "Head of Finance",
                "Controller", "Corporate Controller", "COO", "Chief Operating Officer",
                "Founder", "Co-Founder", "CEO"),
    "operations": ("COO", "Chief Operating Officer", "VP Operations", "VP of Operations",
                   "Head of Operations", "Chief of Staff", "Founder", "Co-Founder", "CEO"),
    "people_hr": ("CHRO", "Chief Human Resources Officer", "Chief People Officer", "VP People",
                  "VP of People", "VP Human Resources", "VP of Human Resources", "Head of People",
                  "Head of Talent Acquisition", "Founder", "Co-Founder", "CEO"),
    "product": ("Chief Product Officer", "CPO", "VP Product", "VP of Product", "Head of Product",
                "Head of Design", "VP Design", "CTO", "Founder", "Co-Founder", "CEO"),
    "ecommerce": ("VP Ecommerce", "VP of Ecommerce", "Head of Ecommerce", "Head of E-commerce",
                  "CMO", "Chief Marketing Officer", "VP Marketing", "COO",
                  "Founder", "Co-Founder", "CEO"),
}

FOUNDER_TIER_TITLES = frozenset({"founder", "co-founder", "cofounder", "co founder", "ceo",
                                 "chief executive officer", "owner", "president"})


def is_founder_tier(title: str) -> bool:
    lowered = " ".join(str(title or "").lower().replace("-", " ").split())
    if lowered in FOUNDER_TIER_TITLES or lowered.replace(" ", "") in {"cofounder", "ceo"}:
        return True
    return any(token in lowered.split() for token in ("founder", "ceo", "owner", "president"))


def buyer_titles(function_key: str, *, founder_allowed: bool) -> Tuple[str, ...]:
    """Ordered buyer titles: direct managers, then executives, founders last or never."""
    direct = DIRECT_BUYER_TITLES.get(function_key, ())
    execs = EXECUTIVE_BUYER_TITLES.get(function_key, ())
    ordered = list(direct) + [t for t in execs if not is_founder_tier(t)]
    if founder_allowed:
        ordered += [t for t in execs if is_founder_tier(t)]
    seen, out = set(), []
    for t in ordered:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return tuple(out)
