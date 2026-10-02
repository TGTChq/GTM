"""Campaign compatibility from responsibilities, not titles.

Order of decision (PRODUCT_CONTRACT §6):

1. Deterministic facts -> a hard incompatibility closes the posting.
2. Deterministic function evidence -> a dominant function with enough independent
   evidence and no strong competitor decides without any model call.
3. Otherwise the semantic port is consulted; its answer is re-validated here (schema,
   grounding of every excerpt, hard exclusions). The model never approves.
4. An unsupported semantic exclusion is ignored. It cannot suppress independently
   grounded positive routing evidence from the same answer.
5. Port unavailable or positive answer ungrounded -> ``insufficient_evidence``
   (reopenable).

The title is NOT an input to function scoring. It is carried as data and offered to
the semantic port as optional context only.
"""

from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..policy.campaigns import (
    CAMPAIGN_BY_FUNCTION, FUNCTION_KEYS, POLICY_VERSION, POLICY_VERSION_EXHAUSTIVE,
)
from .exhaustive_routing import (
    RULE_VERSION as EXHAUSTIVE_RULE_VERSION,
    TitleRoute, exhaustive_enabled, fallback_route, physical_title_reason, route_by_title,
)
from .facts import RULE_VERSION, JobFacts, extract_job_facts, sentences
from .inference import (
    UNAVAILABLE_ANSWER, UNAVAILABLE_CONFIG, UNAVAILABLE_TRANSIENT,
    InferencePort, InferenceRequest, InferenceResponse, grounded,
)

METHOD_DETERMINISTIC = "deterministic"
METHOD_SEMANTIC = "semantic"
METHOD_UNAVAILABLE = "unavailable"

# Deterministic decision thresholds. A dominant function needs a real body of
# evidence, not one lucky phrase.
MIN_DOMINANT_SCORE = 6
MIN_DISTINCT_SIGNALS = 2
MIN_MARGIN = 3

#: Phase 3 audit (2026-09-20). Per-CAMPAIGN minimum deterministic corroboration for a
#: SEMANTIC function assignment: the model may not put a posting into one of these
#: campaigns unless the posting's own wording matches at least this many distinct signals
#: in that campaign's lexicon. A function absent from this table is not subject to the
#: requirement at all -- the nine campaigns are measured separately and enrolled
#: separately, which is the point.
#:
#: ``gtm_revenue`` was already enrolled at 1, hard-coded, by the scoped re-review
#: (IMPORTANT 2). This turns that single hard-coded case into the table it always was, so
#: the five campaigns added here SHARE its predicate instead of each copying it.
#:
#: Measured on the CALIBRATION stratum only (the holdout is not read), replaying the 327
#: answers already bought in phase 4 through ``score_functions`` --
#: ``qualification_recovery/route_evidence.py``, 109 assignments over 70 items. Requiring
#: >= 1 distinct deterministic hit for the assigned function would withhold:
#:
#:     function          withheld right / wrong   that function's precision
#:     customer_success        0 / 3               0.200 -> 0.500
#:     engineering             0 / 2               0.444 -> 0.571
#:     people_hr               0 / 2               0.286 -> 0.400
#:     ecommerce               0 / 2               0.200 -> 0.333
#:     finance                 0 / 1               0.429 -> 0.500
#:     --- deliberately NOT enrolled: ---
#:     operations              4 / 8               costs 4 labelled-right assignments
#:     product                 1 / 2               costs 1
#:     customer_support        1 / 1               costs 1, gains nothing
#:     marketing               0 / 0               no measured effect either way
#:
#: Only the five that withhold ZERO labelled-right assignments are enrolled. ``marketing``
#: is left out on purpose: a change with no measured effect is not evidence, and the brief
#: is explicit that this shape must not be generalised blind. For the route as a whole the
#: requirement WOULD cost right assignments, which is exactly why it is per campaign.
#:
#: Withholding is never a rejection. The function is dropped, any other function survives,
#: and a posting left with none closes as ``insufficient_evidence:*`` -- which
#: ``classification_service``'s reopen query re-enters when a new model version appears.
SEMANTIC_MIN_DETERMINISTIC_HITS: Dict[str, int] = {
    "gtm_revenue": 1,
    "customer_success": 1,
    "engineering": 1,
    "people_hr": 1,
    "ecommerce": 1,
    "finance": 1,
}


@dataclass(frozen=True)
class Signal:
    pattern: str
    weight: int
    phrase: str  # canonical responsibility phrase this evidence licenses


def _s(pattern: str, weight: int, phrase: str) -> Signal:
    return Signal(pattern, weight, phrase)


#: Responsibility lexicon. Multi-word, function-specific phrases carry weight 3;
#: shared words carry 1 so they can corroborate but never decide alone.
LEXICON: Dict[str, Tuple[Signal, ...]] = {
    "customer_success": (
        _s(r"\bcustomer onboarding\b|\bonboard(?:ing)? (?:new )?(?:customers|clients|accounts)\b", 3, "customer onboarding"),
        _s(r"\b(?:drive|improve|increase|own) (?:product )?adoption\b|\bproduct adoption\b", 3, "product adoption"),
        _s(r"\brenewals?\b|\bretention\b|\bchurn\b", 2, "renewals and retention"),
        _s(r"\b(?:upsell|expansion|cross-sell) (?:opportunit|revenue|motion)", 2, "account expansion"),
        _s(r"\bbook of (?:business|accounts)\b|\bportfolio of (?:customers|accounts|clients)\b", 3, "managing a portfolio of accounts"),
        _s(r"\bquarterly business reviews?\b|\bqbrs?\b|\bhealth scores?\b", 3, "customer health reviews"),
        _s(r"\bimplementation(?:s)? (?:for|of|with) (?:new )?(?:customers|clients)\b|\bcustomer implementation\b", 3, "customer implementation"),
        _s(r"\btrusted advisor\b|\bcustomer advocate\b|\bvoice of the customer\b", 2, "customer advocacy"),
        _s(r"\bcustomer success\b", 2, "customer success"),
    ),
    "customer_support": (
        _s(r"\b(?:support|help ?desk) tickets?\b|\bticket (?:queue|volume|resolution)\b", 3, "ticket resolution"),
        _s(r"\brespond(?:ing)? to (?:customer|user) (?:inquiries|questions|issues|requests)\b", 3, "responding to customer inquiries"),
        _s(r"\btroubleshoot(?:ing)?\b[^.]{0,60}\b(?:customer|user|product|technical) (?:issues|problems)\b", 3, "technical troubleshooting"),
        _s(r"\b(?:live )?chat,? (?:email,? )?(?:and|or) phone\b|\bomni-?channel support\b|\bvia (?:email|chat|phone)\b", 2, "multichannel support"),
        _s(r"\bslas?\b[^.]{0,40}\b(?:response|resolution)\b|\bfirst response time\b|\bcsat\b", 2, "service levels and CSAT"),
        _s(r"\bescalat(?:e|ing|ion)\b[^.]{0,60}\b(?:issues|tickets|cases)\b", 2, "issue escalation"),
        _s(r"\bknowledge base\b|\bhelp cent(?:er|re) articles?\b|\bmacros\b", 2, "knowledge base maintenance"),
        _s(r"\bzendesk\b|\bintercom\b|\bfreshdesk\b|\bgorgias\b|\bkustomer\b", 2, "support tooling"),
        _s(r"\bcustomer (?:support|service|care)\b", 2, "customer support"),
    ),
    "engineering": (
        _s(r"\b(?:design|build|ship|develop|maintain)\b[^.]{0,60}\b(?:apis?|microservices|backend services|web applications|features)\b", 3, "building and maintaining services"),
        _s(r"\b(?:python|typescript|javascript|golang|\bgo\b|java|rust|c\+\+|c#|ruby|kotlin|swift)\b", 2, "software development"),
        _s(r"\b(?:react|node\.?js|django|rails|spring|next\.?js|vue|angular|flask|fastapi)\b", 2, "application development"),
        _s(r"\b(?:ci/cd|continuous integration|deployment pipelines?|kubernetes|docker|terraform|infrastructure as code)\b", 3, "deployment and infrastructure automation"),
        _s(r"\b(?:aws|gcp|azure|cloud infrastructure)\b", 1, "cloud infrastructure"),
        _s(r"\b(?:unit|integration|automated) tests?\b|\btest automation\b|\bcode reviews?\b", 2, "testing and code quality"),
        _s(r"\b(?:llm|large language model|machine learning|ml models?|rag|retrieval-augmented|prompt engineering|fine-?tun(?:e|ing)|ai agents?|agentic)\b", 3, "AI and LLM systems"),
        _s(r"\b(?:data pipelines?|etl|elt|data warehouse|dbt|airflow|snowflake|bigquery|databricks)\b", 3, "data pipelines and warehousing"),
        _s(r"\b(?:sql queries|dashboards? in (?:looker|tableau|power bi)|business intelligence)\b", 2, "analytics and business intelligence"),
        _s(r"\bautomat(?:e|ing|ion) (?:of )?(?:manual |internal |business )?(?:workflows|processes|tasks)\b[^.]{0,40}\b(?:zapier|make|n8n|scripts?|python|code|apis?)\b", 3, "workflow automation"),
        _s(r"\bsystem (?:design|architecture)\b|\bscalab(?:le|ility)\b|\bproduction (?:reliability|incidents?|on-?call)\b", 2, "system design and reliability"),
        _s(r"\bqa\b|\bquality assurance\b|\bsoftware quality\b", 2, "software quality assurance"),
    ),
    "finance": (
        _s(r"\baccounts? (?:payable|receivable)\b|\bap/ar\b", 3, "accounts payable and receivable"),
        _s(r"\bmonth[- ]end close\b|\bclose process\b|\bgeneral ledger\b|\bjournal entries\b", 3, "month-end close and general ledger"),
        _s(r"\breconciliations?\b", 2, "account reconciliations"),
        _s(r"\bfinancial (?:statements|reporting|reports)\b|\bgaap\b|\bifrs\b", 3, "financial reporting"),
        _s(r"\b(?:budget(?:ing)?|forecast(?:ing)?|fp&a|variance analysis|financial model(?:ing|s)?)\b", 2, "budgeting and forecasting"),
        _s(r"\bbookkeeping\b|\bbookkeeper\b", 3, "bookkeeping"),
        _s(r"\binvoic(?:e|ing)\b|\bbilling\b|\bcollections\b", 2, "invoicing and collections"),
        _s(r"\b(?:netsuite|quickbooks|xero|sage intacct|bill\.com|expensify)\b", 2, "finance systems"),
        _s(r"\bpayroll\b", 1, "payroll processing"),
        _s(r"\b(?:tax|audit|sox|internal controls)\b", 1, "tax, audit and controls"),
        _s(r"\bcpa\b|\baccountant\b|\baccounting\b", 2, "accounting"),
    ),
    "people_hr": (
        _s(r"\bemployee (?:onboarding|offboarding|relations|lifecycle|records|engagement)\b", 3, "employee lifecycle administration"),
        _s(r"\bbenefits? (?:administration|enrollment)\b|\bopen enrollment\b", 3, "benefits administration"),
        _s(r"\bhris\b|\b(?:workday|bamboohr|rippling|gusto|adp|paylocity|hibob)\b", 2, "HRIS administration"),
        _s(r"\brecruit(?:ing|ment)\b|\btalent acquisition\b|\bsourc(?:e|ing) candidates\b|\bcandidate (?:pipeline|experience)\b|\bfull[- ]cycle recruiting\b", 3, "recruiting and talent acquisition"),
        _s(r"\bcompliance with (?:labor|employment) laws?\b|\bfmla\b|\beeo\b|\bi-9\b|\blabor law\b", 3, "employment compliance"),
        _s(r"\bperformance (?:management|review) (?:cycles?|process)\b|\bcompensation (?:reviews?|planning|bands)\b", 2, "performance and compensation cycles"),
        _s(r"\bpeople (?:operations|ops|team)\b|\bhuman resources\b|\bhr (?:policies|processes|operations)\b", 2, "people operations"),
        _s(r"\bemployee handbook\b|\bhr policies\b|\bworkplace culture\b", 2, "HR policy and culture"),
    ),
    "marketing": (
        _s(r"\b(?:paid media|paid social|paid search|ppc|google ads|meta ads|performance marketing|media buying)\b", 3, "paid media"),
        _s(r"\b(?:email marketing|lifecycle marketing|marketing automation|nurture (?:campaigns|flows)|drip campaigns)\b", 3, "lifecycle and email marketing"),
        _s(r"\b(?:content (?:marketing|calendar|strategy)|blog posts?|copywriting|editorial calendar|seo)\b", 3, "content and SEO"),
        _s(r"\bsocial media (?:channels|accounts|content|strategy|calendar)\b|\bcommunity management\b", 3, "social media"),
        _s(r"\b(?:brand (?:guidelines|identity|voice)|creative (?:assets|direction)|graphic design|video (?:editing|production)|figma|adobe|canva)\b", 3, "brand and creative production"),
        _s(r"\b(?:hubspot|marketo|klaviyo|mailchimp|braze|pardot)\b", 2, "marketing platforms"),
        _s(r"\b(?:campaign performance|marketing analytics|attribution|conversion rates?|a/b test(?:s|ing)?|landing pages?)\b", 2, "campaign analytics and optimization"),
        _s(r"\b(?:demand generation|lead generation campaigns|webinars?|events? marketing|product marketing|go-to-market launch)\b", 2, "demand generation"),
        _s(r"\bmarketing\b", 1, "marketing"),
    ),
    "operations": (
        _s(r"\b(?:business|internal|operational) (?:processes|workflows)\b[^.]{0,60}\b(?:improve|streamline|document|optimi[sz]e|standardi[sz]e)\b", 3, "process improvement"),
        _s(r"\b(?:sops?|standard operating procedures?)\b|\bprocess documentation\b", 3, "standard operating procedures"),
        _s(r"\b(?:vendor|supplier) (?:management|relationships|onboarding)\b|\bprocurement\b", 2, "vendor management"),
        _s(r"\bcross[- ]functional (?:coordination|projects|initiatives)\b|\bproject (?:tracking|coordination|timelines)\b", 2, "cross-functional coordination"),
        _s(r"\b(?:executive|administrative) (?:support|assistant)\b|\bcalendar management\b|\bscheduling (?:meetings|travel)\b|\bexpense reports?\b", 3, "executive and administrative support"),
        _s(r"\b(?:kpis?|operational metrics|operating (?:cadence|rhythm)|okrs?)\b", 2, "operational metrics"),
        _s(r"\b(?:notion|asana|monday\.com|clickup|airtable|smartsheet)\b", 1, "operations tooling"),
        _s(r"\bbusiness operations\b|\boperations (?:team|function)\b|\bday-to-day operations\b", 2, "business operations"),
        _s(r"\b(?:order|fulfillment|inventory) (?:management|tracking|processing)\b", 1, "order and inventory administration"),
    ),
    "product": (
        _s(r"\bproduct (?:roadmap|requirements|specs?|specifications|strategy|discovery|backlog)\b|\bprds?\b", 3, "product roadmap and requirements"),
        _s(r"\buser (?:research|interviews|stories|feedback|testing)\b|\busability (?:testing|studies)\b", 3, "user research"),
        _s(r"\b(?:wireframes?|prototypes?|mockups?|design systems?|ux|ui design|user flows|figma)\b", 3, "UX and product design"),
        _s(r"\b(?:prioriti[sz]e|prioriti[sz]ation of) (?:features|the backlog|initiatives)\b|\bfeature prioriti[sz]ation\b", 3, "feature prioritization"),
        _s(r"\bwork(?:ing)? (?:closely )?with (?:engineering|engineers|design|designers) (?:teams? )?to\b", 2, "partnering with engineering and design"),
        _s(r"\b(?:product analytics|amplitude|mixpanel|pendo|product metrics|activation|funnel)\b", 2, "product analytics"),
        _s(r"\bproduct (?:manager|management|owner|operations|ops)\b|\bagile\b|\bscrum\b|\bsprint planning\b", 2, "product management"),
    ),
    "gtm_revenue": (
        _s(r"\b(?:salesforce|hubspot) (?:administration|admin|configuration|workflows|automation)\b|\bcrm (?:administration|hygiene|automation|architecture|workflows)\b", 3, "CRM administration and automation"),
        _s(r"\blead (?:routing|scoring|enrichment|assignment)\b|\bround[- ]robin\b|\bterritor(?:y|ies)\b", 3, "lead routing and enrichment"),
        _s(r"\b(?:outbound (?:sequences|infrastructure|tooling)|sequencing|outreach\.io|salesloft|apollo\.io|clay\b|deliverability)\b", 3, "outbound systems"),
        _s(r"\b(?:pipeline|revenue|sales) (?:reporting|forecasting|dashboards|analytics)\b|\bforecast accuracy\b", 3, "revenue reporting and forecasting"),
        _s(r"\b(?:revenue operations|revops|sales operations|sales ops|marketing operations|gtm (?:systems|operations|engineering))\b", 3, "revenue operations"),
        _s(r"\b(?:quota|commission|comp plans?|sales compensation)\b", 2, "quota and compensation operations"),
        _s(r"\b(?:sales (?:development|prospecting)|prospect(?:ing)? (?:into|new) accounts|cold (?:calls|outreach|email)|book(?:ing)? (?:meetings|demos))\b", 2, "sales development and prospecting"),
        _s(r"\b(?:account executive|closing deals|sales cycle|demos? to prospects|quota attainment)\b", 2, "sales execution"),
        _s(r"\b(?:partnerships?|channel partners?|business development)\b", 1, "partnerships and business development"),
    ),
    "ecommerce": (
        _s(r"\b(?:shopify|bigcommerce|woocommerce|magento|salesforce commerce cloud)\b", 3, "ecommerce platform operations"),
        _s(r"\b(?:amazon (?:seller central|marketplace|listings|fba|ppc)|marketplace listings|walmart marketplace)\b", 3, "marketplace management"),
        _s(r"\bproduct (?:listings|catalog|pages|pdp|detail pages|feeds?)\b|\bmerchandising\b", 3, "catalog and merchandising"),
        _s(r"\b(?:conversion rate optimi[sz]ation|cro\b|site (?:merchandising|navigation|search)|checkout (?:flow|experience))\b", 3, "conversion optimization"),
        _s(r"\b(?:online store|e-?commerce (?:store|site|operations|business|sales))\b|\bdtc\b|\bdirect[- ]to[- ]consumer\b", 2, "online store operations"),
        _s(r"\b(?:promotions|discount codes|inventory (?:levels|sync)|order (?:fulfillment|management))\b", 1, "promotions and order management"),
    ),
}

assert set(LEXICON) == set(FUNCTION_KEYS), "lexicon must cover every function key"

#: Phase 2 audit task 6 (2026-09-19): the approved exclusion -- quota-carrying sales
#: is excluded from GTM Systems unless RevOps, Sales Ops or GTM-systems work is
#: primary -- was never implemented. The holdout measured 8.7% precision (2/23): 19
#: of 21 misses were plain quota-carrying sales postings (Account Executive, SDR)
#: that reach ``gtm_revenue`` dominance purely on the lexicon's own selling-verb
#: signals ("quota", "sales cycle", "prospecting", "closing deals" ...).
#:
#: These two sets categorize the SAME canonical phrases ``score_functions`` already
#: matched for ``gtm_revenue`` -- no second detection pass, so the "is this selling
#: or ops" view cannot drift from the lexicon that produced the evidence (the
#: established pattern: see ``exclusion_evidence.py``'s reuse of ``facts.py``).
#: "partnerships and business development" is deliberately in neither set: it is
#: not clearly one or the other and stays neutral.
GTM_OPS_PHRASES = frozenset({
    "CRM administration and automation", "lead routing and enrichment", "outbound systems",
    "revenue reporting and forecasting", "revenue operations",
})
GTM_SELLING_PHRASES = frozenset({
    "quota and compensation operations", "sales development and prospecting", "sales execution",
})


def _gtm_revenue_scope(hits: Sequence[Tuple[Signal, str]]) -> Tuple[int, int]:
    """(selling score, ops score) from the phrases already matched for gtm_revenue."""
    selling = sum(sig.weight for sig, _ in hits if sig.phrase in GTM_SELLING_PHRASES)
    ops = sum(sig.weight for sig, _ in hits if sig.phrase in GTM_OPS_PHRASES)
    return selling, ops


def quota_carrying_sales_exclusion(hits: Sequence[Tuple[Signal, str]]) -> Optional[Dict[str, object]]:
    """THE GTM Systems scope predicate: the exclusion payload when the posting's
    own ``gtm_revenue`` evidence is selling rather than RevOps/Sales Ops/GTM
    systems, else ``None``.

    Final whole-branch review, C1 (CRITICAL, 2026-09-20): this used to be an
    inline ``if`` on the DETERMINISTIC path only, so ``_apply_semantic`` -- the
    path tasks 1-2 measurably move rows onto, and which is live in production
    (``runner.py``'s configured inference port) -- assigned ``gtm_revenue``
    from a model answer with no scope check at all. Both paths now call THIS
    function on the SAME ``score_functions`` evidence (the lexicon hits that
    produced the scores), so the "selling or ops" view cannot drift between
    them and the exclusion cannot exist on one path only. Reviewers on this
    branch have twice rejected a copied predicate.
    """
    selling, ops = _gtm_revenue_scope(hits)
    if selling and selling >= ops:
        return {"code": "quota_carrying_sales", "rule_version": RULE_VERSION,
                "selling_score": selling, "ops_score": ops}
    return None


@dataclass
class Responsibility:
    phrase: str
    excerpt: str


@dataclass
class ClassificationResult:
    compatible_functions: List[str] = field(default_factory=list)
    campaign_keys: List[str] = field(default_factory=list)
    excluded: bool = False
    exclusion_reason: str = ""
    responsibilities: List[Responsibility] = field(default_factory=list)
    method: str = METHOD_UNAVAILABLE
    scores: Dict[str, int] = field(default_factory=dict)
    facts: Dict[str, object] = field(default_factory=dict)
    contradictions: List[str] = field(default_factory=list)
    policy_version: str = POLICY_VERSION
    model_version: str = ""
    notes: List[str] = field(default_factory=list)
    unavailable_kind: str = ""
    unavailable_reason: str = ""
    #: Fix round 1, I3 (IMPORTANT, independent review): a changed decision
    #: path's rule_version belongs at the top level, queryable directly
    #: (``result_json->>'rule_version'``), the same way ``facts.py``'s
    #: ``Fact``/``Exclusion.rule_version`` are -- not buried inside an ad-hoc
    #: dict under ``facts`` where an auditor querying exclusions by rule
    #: version would not see it. Set only on a changed decision path (empty
    #: otherwise); ``ClassificationResult`` has no per-field Fact/Exclusion
    #: structure of its own to stamp instead.
    rule_version: str = ""

    @property
    def primary_function(self) -> str:
        return self.compatible_functions[0] if self.compatible_functions else ""

    @property
    def decided(self) -> bool:
        return self.excluded or bool(self.compatible_functions)

    def to_dict(self) -> Dict[str, object]:
        return {
            "compatible_functions": list(self.compatible_functions),
            "campaign_keys": list(self.campaign_keys),
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "responsibilities": [{"phrase": r.phrase, "excerpt": r.excerpt[:300]} for r in self.responsibilities],
            "method": self.method,
            "scores": dict(self.scores),
            "facts": self.facts,
            "contradictions": list(self.contradictions),
            "policy_version": self.policy_version,
            "model_version": self.model_version,
            "notes": list(self.notes),
            "unavailable_kind": self.unavailable_kind,
            "unavailable_reason": self.unavailable_reason,
            "rule_version": self.rule_version,
        }


def score_functions(description: str) -> Tuple[Dict[str, int], Dict[str, List[Tuple[Signal, str]]]]:
    """Distinct-signal scoring over sentences. A signal counts once per function."""
    sents = sentences(description)
    scores: Dict[str, int] = {}
    hits: Dict[str, List[Tuple[Signal, str]]] = {}
    for fn, signals in LEXICON.items():
        seen: set = set()
        for sig in signals:
            rx = re.compile(sig.pattern, re.I)
            for s in sents:
                if rx.search(s):
                    if sig.phrase not in seen:
                        seen.add(sig.phrase)
                        hits.setdefault(fn, []).append((sig, s[:300]))
                        scores[fn] = scores.get(fn, 0) + sig.weight
                    break
    return scores, hits


def _dominant(scores: Dict[str, int], hits: Dict[str, List[Tuple[Signal, str]]]) -> Optional[str]:
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    top_fn, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if top < MIN_DOMINANT_SCORE or len(hits.get(top_fn, [])) < MIN_DISTINCT_SIGNALS:
        return None
    if top - second < MIN_MARGIN:
        return None
    return top_fn


def _responsibilities_from_hits(hits: List[Tuple[Signal, str]]) -> List[Responsibility]:
    ordered = sorted(hits, key=lambda h: -h[0].weight)
    out: List[Responsibility] = []
    for sig, excerpt in ordered:
        if all(r.phrase != sig.phrase for r in out):
            out.append(Responsibility(sig.phrase, excerpt))
        if len(out) == 3:
            break
    return out


def classify_posting(
    *,
    description: Optional[str],
    title: Optional[str] = None,
    employment_type: Optional[str] = None,
    ai_employment_type: Optional[str] = None,
    location_type: Optional[str] = None,
    countries: Sequence[str] = (),
    location_text: Optional[str] = None,
    employer_name: Optional[str] = None,
    agency_flag: Optional[bool] = None,
    org_industry: Optional[str] = None,
    content_hash: str = "",
    inference: Optional[InferencePort] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ClassificationResult:
    exhaustive = exhaustive_enabled(env)
    facts = extract_job_facts(
        title=title, description=description, employment_type=employment_type,
        ai_employment_type=ai_employment_type, location_type=location_type, countries=countries,
        location_text=location_text, employer_name=employer_name, agency_flag=agency_flag,
        org_industry=org_industry,
    )
    result = ClassificationResult(facts=facts.to_dict())
    if exhaustive:
        result.policy_version = POLICY_VERSION_EXHAUSTIVE

    # 1) hard incompatibility: no model call, ever. Campaign scope NEVER
    # overrides an eligibility gate, so this runs before any routing.
    waived = _licence_waiver(facts, title) if exhaustive else None
    if facts.excluded and waived:
        # Audit 2026-09-21: of 95 licence exclusions, two were "Corporate Tax
        # Manager" / "Family Office Tax Manager" -- a CPA is a professional
        # credential for remotely executable finance work, not a physical or
        # jurisdiction-bound duty. The insurance agents and clinical roles in
        # the same bucket stay excluded: the waiver needs the licence to be the
        # SOLE exclusion AND the title to route to Finance.
        result.facts["exclusion_waived"] = waived
        result.notes.append("exhaustive_scope:licence_is_a_finance_credential")
    elif facts.excluded:
        result.excluded = True
        result.exclusion_reason = facts.exclusions[0].reason
        result.method = METHOD_DETERMINISTIC
        return result

    # 1b) affirmative title evidence that the duties are physical. Runs before
    # any routing, because scope never overrides an eligibility gate: without
    # it the OPERATIONS fallback dresses a Residential Plumber up as a
    # corporate operations role (measured, first production canary 2026-09-20).
    if exhaustive:
        physical = physical_title_reason(title)
        if physical:
            result.excluded = True
            result.exclusion_reason = physical
            result.method = METHOD_DETERMINISTIC
            result.rule_version = EXHAUSTIVE_RULE_VERSION
            return result

    desc = str(description or "")
    if len(desc.strip()) < 120:
        # Too little description to score. Under the exhaustive scope a usable
        # title is still deterministic evidence, so the job routes instead of
        # being dropped for content it never had.
        if exhaustive:
            route = route_by_title(title)
            if route is not None:
                return _apply_route(result, route)
        result.method = METHOD_DETERMINISTIC
        result.notes.append("insufficient_evidence:description_too_short")
        return result

    # 2) deterministic function evidence
    scores, hits = score_functions(desc)
    result.scores = dict(scores)
    dominant = _dominant(scores, hits)
    if dominant == "gtm_revenue":
        role_exclusion = quota_carrying_sales_exclusion(hits[dominant])
        if role_exclusion is not None and exhaustive:
            # Business-scope change 2026-09-20. Quota-carrying sales is not one
            # of the twelve approved hard exclusions, and GTM Systems now
            # explicitly covers "sales, business development, partnerships".
            # The Phase 2 exclusion therefore becomes a routing decision. It is
            # recorded, not silently dropped: the old verdict stays queryable.
            result.facts["role_exclusion_waived"] = role_exclusion
            result.notes.append("exhaustive_scope:quota_carrying_sales_routed")
            result.compatible_functions = [dominant]
            result.campaign_keys = [CAMPAIGN_BY_FUNCTION[dominant].key]
            result.responsibilities = _responsibilities_from_hits(hits[dominant])
            result.method = METHOD_DETERMINISTIC
            result.rule_version = EXHAUSTIVE_RULE_VERSION
            return result
        if role_exclusion is not None:
            result.method = METHOD_DETERMINISTIC
            result.excluded = True
            result.exclusion_reason = "role:quota_carrying_sales"
            result.rule_version = RULE_VERSION
            result.facts["role_exclusion"] = role_exclusion
            return result
    if dominant:
        result.compatible_functions = [dominant]
        result.campaign_keys = [CAMPAIGN_BY_FUNCTION[dominant].key]
        result.responsibilities = _responsibilities_from_hits(hits[dominant])
        result.method = METHOD_DETERMINISTIC
        return result

    # 3) deterministic title evidence, BEFORE the model: the instruction is
    # "deterministic matching first, the classifier only for unresolved ties".
    # Description dominance already had its chance above and outranks this.
    if exhaustive:
        route = route_by_title(title)
        if route is not None:
            return _apply_route(result, route)

    # 4) semantic port
    if inference is None:
        if exhaustive:
            return _apply_route(result, fallback_route(title))
        result.method = METHOD_UNAVAILABLE
        result.unavailable_kind = UNAVAILABLE_CONFIG
        result.unavailable_reason = "no_inference_configured"
        result.notes.append("insufficient_evidence:semantic_port_unavailable")
        return result
    request = InferenceRequest(
        content_hash=content_hash, description=desc, title=title or "",
        structured={"employment_type": employment_type, "location_type": location_type,
                    "countries": list(countries), "org_industry": org_industry},
        function_keys=list(FUNCTION_KEYS),
        deterministic_scores=dict(scores),
    )
    response: InferenceResponse = inference.classify(request)
    result.model_version = response.model_version
    if not response.available and exhaustive:
        # A provider outage is not evidence against the job, and under the
        # exhaustive scope it is not a reason to leave it unrouted either.
        return _apply_route(result, fallback_route(title))
    if not response.available:
        result.method = METHOD_UNAVAILABLE
        result.unavailable_kind = (response.unavailable_kind if response.unavailable_kind in
                                   (UNAVAILABLE_CONFIG, UNAVAILABLE_TRANSIENT, UNAVAILABLE_ANSWER)
                                   else UNAVAILABLE_TRANSIENT)
        result.unavailable_reason = response.unavailable_reason or "inference_unavailable"
        result.notes.append(f"insufficient_evidence:{response.unavailable_reason or 'inference_unavailable'}")
        return result
    result.method = METHOD_SEMANTIC
    decided = _apply_semantic(result, response, desc, facts, hits, exhaustive=exhaustive)
    if exhaustive and not decided.excluded and not decided.compatible_functions:
        # The model declined to place it. That is a tie, not a rejection.
        return _apply_route(decided, fallback_route(title))
    return decided



def _licence_waiver(facts: JobFacts, title: Optional[str]) -> Optional[Dict[str, object]]:
    """A licence that is only a professional credential for finance work."""
    if not facts.exclusions or any(e.reason != "deliverability:professional_license" for e in facts.exclusions):
        return None
    route = route_by_title(title)
    if route is None or route.function_key != "finance":
        return None
    first = facts.exclusions[0]
    return {"reason": first.reason, "excerpt": str(getattr(first, "excerpt", "") or "")[:200],
            "basis": "professional_credential_for_finance_work", "rule_version": EXHAUSTIVE_RULE_VERSION}

def _apply_route(result: ClassificationResult, route: TitleRoute) -> ClassificationResult:
    """Stamp a deterministic routing decision onto a result.

    The cited evidence is the title the route matched and nothing else, so a
    posting whose description is pure benefits/EEO boilerplate can never have
    that boilerplate recorded as its campaign evidence. ``approval.py`` refuses
    on ``no_responsibility_evidence``, so this record is what lets a
    title-routed job reach approval at all.
    """
    result.compatible_functions = [route.function_key]
    result.campaign_keys = [CAMPAIGN_BY_FUNCTION[route.function_key].key]
    result.responsibilities = [Responsibility(route.phrase, route.excerpt)]
    result.method = METHOD_DETERMINISTIC
    result.rule_version = EXHAUSTIVE_RULE_VERSION
    result.policy_version = POLICY_VERSION_EXHAUSTIVE
    result.notes.append(f"exhaustive_scope:{route.basis}")
    return result


def _apply_semantic(result: ClassificationResult, response: InferenceResponse, desc: str, facts: JobFacts,
                    hits: Dict[str, List[Tuple[Signal, str]]], exhaustive: bool = False) -> ClassificationResult:
    """Model output is data. Re-validate everything before it can influence a decision.

    ``hits`` is ``score_functions``' own evidence for this description (the
    deterministic pass that already ran before the port was consulted). It is a
    REQUIRED argument, with no default: C1 (final whole-branch review) was
    exactly a scope check that existed on one path and not the other, and a
    default would let a caller silently skip it again."""
    from .exclusion_evidence import corroborates

    claimed_exclusion = (response.seniority == "director_plus" or response.people_management is True
                         or response.incompatible_reasons or response.exclusion_evidence)
    if claimed_exclusion:
        if not math.isfinite(response.confidence) or not 0.8 <= response.confidence <= 1:
            result.notes.append("ignored_semantic_exclusion:low_confidence")
        else:
            for evidence in response.exclusion_evidence:
                code, excerpt = evidence.get("code", ""), evidence.get("excerpt", "")
                if grounded(excerpt, desc) and corroborates(code, excerpt, desc):
                    result.excluded = True
                    result.exclusion_reason = f"semantic_evidence:{code}"
                    result.facts["semantic_exclusion"] = {"code": code, "excerpt": excerpt, "confidence": response.confidence}
                    return result
            result.notes.append("ignored_semantic_exclusion:ungrounded_or_unsupported")
    functions = [f for f in response.compatible_functions if f in FUNCTION_KEYS]
    if not functions or not math.isfinite(response.confidence) or not 0.6 <= response.confidence <= 1:
        result.notes.append("insufficient_evidence:semantic_low_confidence_or_no_function")
        return result
    grounded_resps: List[Responsibility] = []
    for item in response.responsibilities[:3]:
        if grounded(item.excerpt, desc) and 0 < len(item.phrase) <= 60 and not re.search(r"https?://|@|\{\{|<", item.phrase):
            grounded_resps.append(Responsibility(item.phrase.strip(), item.excerpt.strip()))
    if not grounded_resps:
        result.notes.append("insufficient_evidence:semantic_responsibilities_ungrounded")
        return result
    # C1 (CRITICAL, final whole-branch review): the SAME scope predicate the
    # deterministic path applies. The approved exclusion is about the GTM
    # Systems campaign, and a job counts once under its primary, so only
    # gtm_revenue is dropped from the assignment; a posting left with no
    # function at all is excluded exactly as the deterministic path excludes
    # it (there, gtm_revenue being dominant means it was the only function).
    # Scoped re-review, IMPORTANT 2 (2026-09-20), generalised per campaign by the phase-3
    # audit: the scope predicate is EVIDENCE-conditional -- on an empty hit list it sees
    # selling=0 and excludes nothing. Only the description is scored, so a quota-carrying
    # posting whose wording matches no gtm_revenue signal ("own a book of new logos ...
    # measured on the number you bring in") reached GTM Systems on the model's word alone,
    # and the semantic path is exactly the population where that happens.
    #
    # A model assignment with ZERO deterministic evidence for the campaign it names is an
    # unqualified guess; "unknown is never approved", so the assignment is WITHHELD and the
    # posting goes to review (no function, reopened by classification_service when a new
    # model version appears), never rejected and never approved. The lexicon is
    # deliberately NOT widened to paper over this.
    #
    # Which campaigns this applies to is a MEASURED table, not a blanket rule
    # (SEMANTIC_MIN_DETERMINISTIC_HITS). One loop, one predicate: gtm_revenue keeps
    # precisely the behaviour it had, and the five campaigns enrolled beside it share this
    # code rather than each growing a carve-out of their own.
    withheld: List[Dict[str, object]] = []
    for function in list(functions):
        minimum = SEMANTIC_MIN_DETERMINISTIC_HITS.get(function)
        if minimum is None or len(hits.get(function, ())) >= minimum:
            continue
        functions = [f for f in functions if f != function]
        withheld.append({"function": function, "code": "no_deterministic_function_evidence",
                         "rule_version": RULE_VERSION})
        result.notes.append(f"insufficient_evidence:{function}_unsupported_by_deterministic_evidence")
    if withheld:
        result.rule_version = RULE_VERSION
        result.facts["withheld_functions"] = withheld
        if not functions:
            return result
    if "gtm_revenue" in functions:
        gtm_hits = hits.get("gtm_revenue", ())
        role_exclusion = quota_carrying_sales_exclusion(gtm_hits)
        if role_exclusion is not None and exhaustive:
            # The exhaustive scope routes quota-carrying sales to GTM Systems on
            # BOTH paths. Audit 2026-09-21: 14 sales postings were still excluded
            # here because the flag had only reached the deterministic path.
            result.facts["role_exclusion_waived"] = role_exclusion
            result.notes.append("exhaustive_scope:quota_carrying_sales_routed")
            role_exclusion = None
        if role_exclusion is not None:
            functions = [f for f in functions if f != "gtm_revenue"]
            result.rule_version = RULE_VERSION
            result.facts["role_exclusion"] = role_exclusion
            if not functions:
                result.excluded = True
                result.exclusion_reason = "role:quota_carrying_sales"
                return result
            result.notes.append("dropped_function:gtm_revenue:quota_carrying_sales")
    result.compatible_functions = functions[:2]
    result.campaign_keys = list(dict.fromkeys(CAMPAIGN_BY_FUNCTION[f].key for f in result.compatible_functions))
    result.responsibilities = grounded_resps
    return result
