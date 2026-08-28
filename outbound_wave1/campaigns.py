"""The nine live Wave 1 campaigns and their frozen messaging policy.

The campaign split is fixed by the live Instantly workspace (nine campaigns).
``role_bucket`` is the pipeline's existing classification, so this module maps
the ten production buckets onto those nine campaigns -- ``customer_success`` and
``customer_support`` share the CUSTOMER EXPERIENCE campaign, which is exactly
how ``config.CAMPAIGN_ENV_BY_BUCKET`` already routes them.

Nothing here is copy: it is the policy each campaign resolves under. The copy
itself lives in ``render.py`` so the policy stays inspectable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# --- signal types -----------------------------------------------------------
SIGNAL_MULTI_OPENING = "multi_opening"
SIGNAL_MULTI_OPENING_CROSS_BUCKET = "multi_opening_cross_bucket"
SIGNAL_ROLE_FOCUS_MATCH = "role_focus_match"
SIGNAL_SCOPE_COMBINATION = "scope_combination"
SIGNAL_JOB_AGE = "job_age"
SIGNAL_ACTIVE_REQ = "active_req"

# --- tiers ------------------------------------------------------------------
TIER_1 = "T1"
TIER_2 = "T2"
TIER_3 = "T3"

# --- proof types ------------------------------------------------------------
PROOF_TESTING_MECHANICS = "testing_mechanics"
PROOF_ECONOMICS = "economics"
PROOF_REMOTE_READINESS = "remote_readiness"

# --- offers -----------------------------------------------------------------
OFFER_TESTING_OVERVIEW = "testing_overview"
OFFER_ROLE_ECONOMICS = "role_economics"
OFFER_REMOTE_READINESS_OVERVIEW = "remote_readiness_overview"

#: Offer classes. ``process_explainer`` describes how TGTC works and needs no
#: external published source. ``published_economics`` quotes a number TGTC has
#: published for one exact role and therefore always needs a claim source.
OFFER_CLASS_PROCESS = "process_explainer"
OFFER_CLASS_PUBLISHED = "published_economics"

#: The only offer nouns Wave 1 may use. The noun is chosen once for E1 and is
#: reused verbatim in E2/E3/E4 -- it must never change mid-thread.
OFFER_NOUNS: Dict[str, str] = {
    OFFER_TESTING_OVERVIEW: "how we test for this role",
    OFFER_ROLE_ECONOMICS: "the numbers for this role",
    OFFER_REMOTE_READINESS_OVERVIEW: "how we assess remote readiness",
}
#: Permitted vocabulary (a QA gate asserts membership). "how the assessment
#: works" is reserved for a campaign that later configures a named assessment.
VALID_OFFER_NOUNS = frozenset(set(OFFER_NOUNS.values()) | {"how the assessment works"})

OFFER_CLASS_BY_TYPE: Dict[str, str] = {
    OFFER_TESTING_OVERVIEW: OFFER_CLASS_PROCESS,
    OFFER_REMOTE_READINESS_OVERVIEW: OFFER_CLASS_PROCESS,
    OFFER_ROLE_ECONOMICS: OFFER_CLASS_PUBLISHED,
}


@dataclass(frozen=True)
class CampaignPolicy:
    """Frozen Wave 1 policy for one live campaign."""

    key: str
    name: str
    buckets: Tuple[str, ...]
    #: Instantly campaign env var(s) that already route this bucket in production.
    env_keys: Tuple[str, ...]
    #: T1 signal this campaign attempts before degrading to T2/T3.
    t1_signal: str
    #: Proof used when the campaign's preferred proof is available.
    preferred_proof: str
    #: Proof used when the preferred proof cannot be supported.
    fallback_proof: str
    #: Offer paired with the preferred proof.
    preferred_offer: str
    #: Offer used when degrading.
    fallback_offer: str
    #: Minimum usable Focus Evidence items the T1 copy needs. 0 = not evidence-led.
    t1_min_evidence: int = 0
    #: Facets used to derive ``scope_combination`` (campaign-specific, deterministic).
    scope_facets: Tuple[Tuple[str, Tuple[str, ...]], ...] = field(default_factory=tuple)
    #: Set when the campaign covers two audiences that must never be conflated.
    audience_by_bucket: Dict[str, str] = field(default_factory=dict)
    #: Claim id (in the claim registry) that licenses the campaign's strongest
    #: proof wording. Unconfigured -> the safe wording is used instead.
    strong_claim_id: str = ""


def _facets(*pairs: Tuple[str, Tuple[str, ...]]) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    return tuple(pairs)


PRODUCT = CampaignPolicy(
    key="product",
    name="PRODUCT",
    buckets=("product",),
    env_keys=("INSTANTLY_CAMPAIGN_PRODUCT",),
    t1_signal=SIGNAL_MULTI_OPENING,
    preferred_proof=PROOF_TESTING_MECHANICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_TESTING_OVERVIEW,
    fallback_offer=OFFER_TESTING_OVERVIEW,
)

OPERATIONS = CampaignPolicy(
    key="operations",
    name="OPERATIONS",
    buckets=("operations",),
    env_keys=("INSTANTLY_CAMPAIGN_OPERATIONS",),
    t1_signal=SIGNAL_ROLE_FOCUS_MATCH,
    # Wave 1 is FIXED to testing_overview here. Operations must not bifurcate
    # into an economics offer, so preferred and fallback are deliberately equal.
    preferred_proof=PROOF_TESTING_MECHANICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_TESTING_OVERVIEW,
    fallback_offer=OFFER_TESTING_OVERVIEW,
    t1_min_evidence=2,
)

FINANCE = CampaignPolicy(
    key="finance",
    name="FINANCE",
    buckets=("finance",),
    env_keys=("INSTANTLY_CAMPAIGN_FINANCE",),
    t1_signal=SIGNAL_SCOPE_COMBINATION,
    preferred_proof=PROOF_ECONOMICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_ROLE_ECONOMICS,
    fallback_offer=OFFER_TESTING_OVERVIEW,
    t1_min_evidence=2,
    scope_facets=_facets(
        ("transactional accounting", (
            "accounts payable", "accounts receivable", "invoice", "invoicing", "billing",
            "collections", "payroll", "bookkeeping", "reconciliation", "reconciliations",
            "month-end", "month end", "close", "expense",
        )),
        ("reporting and analysis", (
            "reporting", "financial reporting", "analysis", "analytics", "variance",
            "forecast", "forecasting", "budget", "budgeting", "modeling", "modelling",
            "fp&a", "planning",
        )),
        ("controls and compliance", (
            "compliance", "controls", "audit", "gaap", "tax", "policy", "sox",
        )),
        ("systems and process", (
            "erp", "netsuite", "quickbooks", "systems", "automation",
            "process improvement", "workflow",
        )),
    ),
)

PEOPLE_HR = CampaignPolicy(
    key="people_hr",
    name="PEOPLE & HR",
    buckets=("people_hr",),
    env_keys=("INSTANTLY_CAMPAIGN_PEOPLE_HR",),
    t1_signal=SIGNAL_MULTI_OPENING_CROSS_BUCKET,
    preferred_proof=PROOF_REMOTE_READINESS,
    fallback_proof=PROOF_REMOTE_READINESS,
    preferred_offer=OFFER_REMOTE_READINESS_OVERVIEW,
    fallback_offer=OFFER_REMOTE_READINESS_OVERVIEW,
    strong_claim_id="remote_readiness_1000_hires",
)

ECOMMERCE = CampaignPolicy(
    key="ecommerce",
    name="ECOMMERCE",
    buckets=("ecommerce",),
    env_keys=("INSTANTLY_CAMPAIGN_ECOMMERCE",),
    t1_signal=SIGNAL_MULTI_OPENING,
    preferred_proof=PROOF_TESTING_MECHANICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_TESTING_OVERVIEW,
    fallback_offer=OFFER_TESTING_OVERVIEW,
)

CUSTOMER_EXPERIENCE = CampaignPolicy(
    key="customer_experience",
    name="CUSTOMER EXPERIENCE",
    buckets=("customer_success", "customer_support"),
    env_keys=("INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS", "INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT"),
    t1_signal=SIGNAL_MULTI_OPENING,
    preferred_proof=PROOF_ECONOMICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_ROLE_ECONOMICS,
    fallback_offer=OFFER_TESTING_OVERVIEW,
    # Support and Success are different jobs. Rendering "support roles" at a
    # Customer Success record is a hard QA failure, so the audience noun is
    # carried explicitly rather than inferred at render time.
    audience_by_bucket={
        "customer_success": "customer success",
        "customer_support": "customer support",
    },
)

MARKETING_CREATIVE = CampaignPolicy(
    key="marketing_creative",
    name="MARKETING & CREATIVE",
    buckets=("marketing",),
    env_keys=("INSTANTLY_CAMPAIGN_MARKETING",),
    t1_signal=SIGNAL_SCOPE_COMBINATION,
    preferred_proof=PROOF_ECONOMICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_ROLE_ECONOMICS,
    fallback_offer=OFFER_TESTING_OVERVIEW,
    t1_min_evidence=2,
    scope_facets=_facets(
        ("paid media", (
            "paid media", "paid acquisition", "paid social", "ppc", "google ads",
            "performance marketing", "media buying", "sem", "demand generation",
        )),
        ("lifecycle", (
            "lifecycle", "email marketing", "crm marketing", "retention", "nurture",
            "marketing automation", "campaign automation", "customer marketing",
        )),
        ("content", (
            "content", "copywriting", "editorial", "blog", "seo", "social media",
            "community",
        )),
        ("creative production", (
            "design", "creative", "brand", "video", "motion", "asset production",
            "graphic", "post-production",
        )),
        ("analytics", (
            "analytics", "reporting", "attribution", "measurement", "dashboards",
        )),
    ),
)

GTM_SYSTEMS = CampaignPolicy(
    key="gtm_systems",
    name="GTM SYSTEMS & REVENUE AUTOMATION",
    buckets=("gtm_revenue",),
    env_keys=("INSTANTLY_CAMPAIGN_GTM",),
    t1_signal=SIGNAL_SCOPE_COMBINATION,
    preferred_proof=PROOF_TESTING_MECHANICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_TESTING_OVERVIEW,
    fallback_offer=OFFER_TESTING_OVERVIEW,
    t1_min_evidence=2,
    strong_claim_id="gtm_combination_directly_tested",
    scope_facets=_facets(
        ("CRM administration", (
            "crm", "salesforce", "hubspot", "crm automation", "crm administration",
            "system administration",
        )),
        ("data and enrichment", (
            "enrichment", "data hygiene", "data orchestration", "data quality",
            "lead enrichment", "clay", "apollo", "data operations",
        )),
        ("routing and lifecycle automation", (
            "routing", "lead routing", "lifecycle automation", "workflow automation",
            "territory", "round-robin", "process automation",
        )),
        ("revenue reporting", (
            "revenue operations", "revenue reporting", "forecasting", "pipeline",
            "attribution", "dashboards", "analytics", "reporting",
        )),
        ("outbound systems", (
            "outbound infrastructure", "sequencing", "outreach", "deliverability",
            "prospecting", "lead generation", "outbound",
        )),
    ),
)

AI_TECHNICAL = CampaignPolicy(
    key="ai_technical",
    name="AI & TECHNICAL AUTOMATION",
    buckets=("engineering",),
    env_keys=("INSTANTLY_CAMPAIGN_ENGINEERING",),
    t1_signal=SIGNAL_ROLE_FOCUS_MATCH,
    preferred_proof=PROOF_TESTING_MECHANICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_TESTING_OVERVIEW,
    fallback_offer=OFFER_TESTING_OVERVIEW,
    t1_min_evidence=2,
    strong_claim_id="ai_live_llm_evaluation",
)

CAMPAIGNS: Tuple[CampaignPolicy, ...] = (
    PRODUCT,
    OPERATIONS,
    FINANCE,
    PEOPLE_HR,
    ECOMMERCE,
    CUSTOMER_EXPERIENCE,
    MARKETING_CREATIVE,
    GTM_SYSTEMS,
    AI_TECHNICAL,
)

#: Live campaign names, in the order the Wave 1 split is stated.
WAVE1_CAMPAIGN_NAMES: Tuple[str, ...] = tuple(policy.name for policy in CAMPAIGNS)

CAMPAIGN_BY_KEY: Dict[str, CampaignPolicy] = {p.key: p for p in CAMPAIGNS}

CAMPAIGN_BY_BUCKET: Dict[str, CampaignPolicy] = {
    bucket: policy for policy in CAMPAIGNS for bucket in policy.buckets
}


def campaign_for_bucket(role_bucket: str) -> Optional[CampaignPolicy]:
    """Return the Wave 1 campaign for a production role bucket, or ``None``.

    An unknown bucket is not silently routed anywhere: the caller treats it as
    out of Wave 1 scope.
    """
    return CAMPAIGN_BY_BUCKET.get(str(role_bucket or "").strip().lower())
