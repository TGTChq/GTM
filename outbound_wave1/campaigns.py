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
#: Every proof below is a VERIFIED fact about the TGTC offer. Nothing here may
#: assert a capability that is not on that list -- a bucket that cannot be
#: differentiated with a verified fact keeps a shared proof rather than gaining
#: an invented one.
PROOF_TESTING_MECHANICS = "testing_mechanics"
PROOF_ECONOMICS = "economics"
PROOF_REMOTE_READINESS = "remote_readiness"
#: Placed people join the client's team full-time without going onto the
#: client's official headcount.
PROOF_HEADCOUNT_MODEL = "headcount_model"
#: TGTC carries payroll, taxes, benefits, compliance and HR administration.
PROOF_EMPLOYMENT_ADMIN = "employment_admin"

# --- offers -----------------------------------------------------------------
OFFER_TESTING_OVERVIEW = "testing_overview"
OFFER_ROLE_ECONOMICS = "role_economics"
OFFER_REMOTE_READINESS_OVERVIEW = "remote_readiness_overview"
OFFER_HEADCOUNT_OVERVIEW = "headcount_overview"
OFFER_EMPLOYMENT_ADMIN_OVERVIEW = "employment_admin_overview"

#: Offer classes. ``process_explainer`` describes how TGTC works and needs no
#: external published source. ``published_economics`` quotes a number TGTC has
#: published for one exact role and therefore always needs a claim source.
OFFER_CLASS_PROCESS = "process_explainer"
OFFER_CLASS_PUBLISHED = "published_economics"

#: Default offer noun per offer type. A campaign may override it (see
#: ``CampaignPolicy.offer_nouns``) because the same offer is named differently in
#: different campaigns -- Product says "how our testing works" where Operations
#: says "how we test for this role".
#:
#: The noun is resolved ONCE for E1 and then appears verbatim in E1, E2, E3 and
#: E4. It is the literal rendered text, not a semantic label: E1 asks
#: "Want me to send <noun>?" so the reader sees the same words every time.
DEFAULT_OFFER_NOUNS: Dict[str, str] = {
    OFFER_TESTING_OVERVIEW: "how we test for this role",
    OFFER_ROLE_ECONOMICS: "the numbers for this role",
    OFFER_REMOTE_READINESS_OVERVIEW: "how we assess remote readiness",
    OFFER_HEADCOUNT_OVERVIEW: "how the headcount side works",
    OFFER_EMPLOYMENT_ADMIN_OVERVIEW: "what we carry on the employment side",
}
#: Backwards-compatible alias for the default map.
OFFER_NOUNS = DEFAULT_OFFER_NOUNS

#: Permitted vocabulary. A QA gate asserts the resolved noun is one of these.
VALID_OFFER_NOUNS = frozenset({
    "how we test for this role",
    "how our testing works",
    "how the assessment works",
    "how we assess remote readiness",
    "the numbers for this role",
    "how we test for a scope like this",
    "what the testing covers",
    "how we test the combination",
    "how the headcount side works",
    "how an embedded hire works",
    "what we carry on the employment side",
})

OFFER_CLASS_BY_TYPE: Dict[str, str] = {
    OFFER_TESTING_OVERVIEW: OFFER_CLASS_PROCESS,
    OFFER_REMOTE_READINESS_OVERVIEW: OFFER_CLASS_PROCESS,
    OFFER_HEADCOUNT_OVERVIEW: OFFER_CLASS_PROCESS,
    OFFER_EMPLOYMENT_ADMIN_OVERVIEW: OFFER_CLASS_PROCESS,
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
    #: Per-campaign visible offer noun, keyed by offer type. Falls back to
    #: ``DEFAULT_OFFER_NOUNS``.
    offer_nouns: Dict[str, str] = field(default_factory=dict)
    #: Offer noun for records whose T1 signal did NOT fire. Set only where the
    #: campaign's own noun names something the degraded email never describes.
    degraded_offer_nouns: Dict[str, str] = field(default_factory=dict)
    #: Per-campaign phrasing of a proof line, keyed by proof type. The CLAIM is
    #: identical -- only the sentence that carries it changes, so it follows this
    #: campaign's own argument instead of reading as one template reused nine
    #: times. An unset proof type falls back to the shared wording.
    proof_texts: Dict[str, str] = field(default_factory=dict)

    def offer_noun(self, offer_type: str) -> str:
        """The exact words this campaign uses for ``offer_type``."""
        return self.offer_nouns.get(
            offer_type,
            DEFAULT_OFFER_NOUNS.get(offer_type, DEFAULT_OFFER_NOUNS[OFFER_TESTING_OVERVIEW]),
        )

    def degraded_offer_noun(self, offer_type: str) -> str:
        """Tier-safe noun for ``offer_type``, or "" to keep the campaign's own."""
        return self.degraded_offer_nouns.get(offer_type, "")

    def proof_text(self, proof_type: str) -> str:
        """This campaign's phrasing of ``proof_type``, or "" for the shared one."""
        return self.proof_texts.get(proof_type, "")


def _facets(*pairs: Tuple[str, Tuple[str, ...]]) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    return tuple(pairs)


#: PRODUCT sells the HEADCOUNT MODEL. A product leader with more than one req
#: open is rarely short of candidates -- they are short of approvals, and the
#: second and third role wait on the next planning cycle. The verified fact that
#: answers that is the one about official headcount, not the one about testing.
PRODUCT = CampaignPolicy(
    key="product",
    name="PRODUCT",
    buckets=("product",),
    env_keys=("INSTANTLY_CAMPAIGN_PRODUCT",),
    t1_signal=SIGNAL_MULTI_OPENING,
    preferred_proof=PROOF_HEADCOUNT_MODEL,
    fallback_proof=PROOF_HEADCOUNT_MODEL,
    preferred_offer=OFFER_HEADCOUNT_OVERVIEW,
    fallback_offer=OFFER_HEADCOUNT_OVERVIEW,
)

OPERATIONS = CampaignPolicy(
    key="operations",
    name="OPERATIONS",
    buckets=("operations",),
    env_keys=("INSTANTLY_CAMPAIGN_OPERATIONS",),
    t1_signal=SIGNAL_ROLE_FOCUS_MATCH,
    # Wave 1 is FIXED to testing_overview here. Operations must not bifurcate
    # into an economics offer, so preferred and fallback are deliberately equal.
    # The ARGUMENT is ops-specific: an ops title is a container for whatever the
    # company happened to pile into it, so a title match is close to meaningless
    # and the scope is the only thing worth evaluating against.
    preferred_proof=PROOF_TESTING_MECHANICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_TESTING_OVERVIEW,
    fallback_offer=OFFER_TESTING_OVERVIEW,
    t1_min_evidence=2,
    offer_nouns={OFFER_TESTING_OVERVIEW: "how we test for a scope like this"},
    # Behind a degraded signal no scope is described, so "a scope like this"
    # points at nothing.
    degraded_offer_nouns={OFFER_TESTING_OVERVIEW: "how we test for this role"},
    proof_texts={
        PROOF_TESTING_MECHANICS: (
            "We test candidates on the actual work before they get to you."
        )
    },
)

FINANCE = CampaignPolicy(
    key="finance",
    name="FINANCE",
    buckets=("finance",),
    env_keys=("INSTANTLY_CAMPAIGN_FINANCE",),
    # FINANCE degrades to the EMPLOYMENT ADMINISTRATION fact, not to testing. A
    # controller's objection to a hire is rarely "can they do the work" -- it is
    # who carries the payroll, tax, benefits and compliance load behind the
    # person. Economics stays the preferred proof for the day a published number
    # exists; until then the fallback is the strongest verified CFO argument.
    t1_signal=SIGNAL_SCOPE_COMBINATION,
    preferred_proof=PROOF_ECONOMICS,
    fallback_proof=PROOF_EMPLOYMENT_ADMIN,
    preferred_offer=OFFER_ROLE_ECONOMICS,
    fallback_offer=OFFER_EMPLOYMENT_ADMIN_OVERVIEW,
    t1_min_evidence=2,
    scope_facets=_facets(
        ("day-to-day accounting", (
            "accounts payable", "accounts receivable", "invoice", "invoicing", "billing",
            "collections", "payroll", "bookkeeping", "reconciliation", "reconciliations",
            "month-end", "month end", "close", "expense",
        )),
        ("reporting", (
            "reporting", "financial reporting", "analysis", "analytics", "variance",
            "forecast", "forecasting", "budget", "budgeting", "modeling", "modelling",
            "fp&a", "planning",
        )),
        ("compliance", (
            "compliance", "controls", "audit", "gaap", "tax", "policy", "sox",
        )),
        ("systems", (
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

#: ECOMMERCE is the one bucket that could NOT be given a distinct verified
#: angle. Every ecommerce-specific idea worth selling (seasonal/peak capacity,
#: platform-specific proof) needs a capability that is not on the verified list,
#: and inventing one is exactly what the policy forbids. It therefore keeps the
#: shared testing proof with an ecommerce-specific reason, and is flagged as
#: strategically closest to OPERATIONS. It is excluded from Pilot 1 anyway --
#: its live Control campaign is `completed`.
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
    offer_nouns={OFFER_TESTING_OVERVIEW: "how our testing works"},
    proof_texts={
        PROOF_TESTING_MECHANICS: (
            "We test candidates on the actual work instead of going off the "
            "title."
        )
    },
)

CUSTOMER_EXPERIENCE = CampaignPolicy(
    key="customer_experience",
    name="CUSTOMER EXPERIENCE",
    buckets=("customer_success", "customer_support"),
    env_keys=("INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS", "INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT"),
    # CX sells TESTING, on a reason no other campaign can use: people in these
    # roles are professionally good at conversations, which makes the interview
    # the least diagnostic signal the buyer has. Economics is dropped here rather
    # than kept as an unreachable preferred -- this argument is the stronger one
    # even if a published number appears later.
    t1_signal=SIGNAL_MULTI_OPENING,
    preferred_proof=PROOF_TESTING_MECHANICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_TESTING_OVERVIEW,
    fallback_offer=OFFER_TESTING_OVERVIEW,
    offer_nouns={OFFER_TESTING_OVERVIEW: "what the testing covers"},
    proof_texts={
        PROOF_TESTING_MECHANICS: (
            "We test people on the work itself before you meet them."
        )
    },
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
    # MARKETING sells the HEADCOUNT MODEL, on a different reason from PRODUCT's.
    # A marketing req that spans paid, lifecycle and content usually exists
    # because one line was approved and it has to cover several jobs -- so the
    # useful fact is that the rest of the scope does not need another line.
    t1_signal=SIGNAL_SCOPE_COMBINATION,
    preferred_proof=PROOF_HEADCOUNT_MODEL,
    fallback_proof=PROOF_HEADCOUNT_MODEL,
    preferred_offer=OFFER_HEADCOUNT_OVERVIEW,
    fallback_offer=OFFER_HEADCOUNT_OVERVIEW,
    t1_min_evidence=2,
    offer_nouns={OFFER_HEADCOUNT_OVERVIEW: "how an embedded hire works"},
    # No proof override. The friction now carries the "a second hire needs no
    # extra slot" idea, so a proof repeating it would say the same thing twice in
    # consecutive sentences. The shared headcount sentence is the right close.
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
    # GTM sells TESTING on the INTERSECTION. Each part of a RevOps scope is
    # common on its own; it is the combination that is rare, and a CV lists the
    # parts without ever evidencing the join.
    t1_signal=SIGNAL_SCOPE_COMBINATION,
    preferred_proof=PROOF_TESTING_MECHANICS,
    fallback_proof=PROOF_TESTING_MECHANICS,
    preferred_offer=OFFER_TESTING_OVERVIEW,
    fallback_offer=OFFER_TESTING_OVERVIEW,
    t1_min_evidence=2,
    strong_claim_id="gtm_combination_directly_tested",
    offer_nouns={OFFER_TESTING_OVERVIEW: "how we test the combination"},
    # Behind a degraded signal no combination is described.
    degraded_offer_nouns={OFFER_TESTING_OVERVIEW: "how we test for this role"},
    proof_texts={
        PROOF_TESTING_MECHANICS: (
            "We test for the combination, not just the individual pieces."
        )
    },
    scope_facets=_facets(
        ("CRM administration", (
            "crm", "salesforce", "hubspot", "crm automation", "crm administration",
            "system administration",
        )),
        ("data hygiene", (
            "enrichment", "data hygiene", "data orchestration", "data quality",
            "lead enrichment", "clay", "apollo", "data operations",
        )),
        ("routing", (
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
    offer_nouns={OFFER_TESTING_OVERVIEW: "how the assessment works"},
    # The ARGUMENT here is the strongest one available and needs no new claim:
    # the tooling is too new for anyone to have a long track record in it, so
    # years-of-experience on a CV carries almost no signal in this category.
    proof_texts={
        PROOF_TESTING_MECHANICS: (
            "We test candidates on the actual work instead."
        )
    },
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
