"""Functional decision-maker mapping for job-level opportunities (``tgtc-contact/1``).

Pure, deterministic functions (no provider calls, no LLM). Not wired into the runtime: the Apollo contact pilot
of 2026-09-19 replays it over already-paid records. Every rule below cites the calibration mapping (``cal#N`` in
``contact_pilot_20260919/qa_sheet.txt``) or the explicit instruction it implements.

Gates that are NOT loosened here: Apollo ``verified`` is the only email authority; the employer organization must
match by domain or LinkedIn page; current employment and a LinkedIn identity are required; foreign-territory
contacts are refused. Alternate email domains are accepted only on saved same-organization evidence, otherwise
quarantined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .gates import FOREIGN_TERRITORY, GLOBAL_SCOPE, US_TERRITORY
from .identity import (
    _GENERIC_NAME_TOKENS, email_domain, is_intermediary_host, is_placeholder_company_name, linkedin_slug,
    normalize_company_domain, normalize_company_name,
)

CONTACT_MAPPER_VERSION = "tgtc-contact/1"
SMALL_COMPANY_MAX = 99      # policy founder_fallback_max_employees

GROUPS = ("ai_engineering_automation", "gtm_revops_salesops", "marketing_creative", "customer_success_support")


def normalize_title(title: Optional[str]) -> str:
    t = str(title or "").lower().replace("&", " and ")
    t = re.sub(r"[/\-,|().:;+]", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", "", t)
    t = re.sub(r"\bsr\b", "senior", t)
    return " ".join(t.split())


def _any(phrases: Iterable[str], text: str) -> bool:
    """Complete phrases on token boundaries: 'data' never matches 'database', 'it' never matches 'digital'."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", text) for p in phrases)


# --- seniority (instruction: VP = Vice President; SVP/EVP; Head; appropriate Director-level owners) ------------
LEVEL_RANK = {"c_level": 6, "evp": 5, "svp": 4, "vp": 3, "head": 3, "director": 2}
_C_LEVEL = re.compile(r"\bchief(?: [a-z0-9]+){1,6} officer\b|\b(?:cto|cio|cmo|cro|coo|ceo|caio|cdo|cpto)\b")
_EVP = re.compile(r"\b(?:evp|executive vice president)\b")
_SVP = re.compile(r"\b(?:svp|senior vice president)\b")
_SUB_VP = re.compile(r"\b(?:avp|assistant vice president|associate vice president)\b")   # director-equivalent
_VP = re.compile(r"\b(?:vp|vice president)\b")
_HEAD = re.compile(r"\bhead\b")
_DIRECTOR = re.compile(r"\bdirector\b")


def seniority(title: Optional[str]) -> Optional[str]:
    t = normalize_title(title)
    if _C_LEVEL.search(t):
        return "c_level"
    if _EVP.search(t):
        return "evp"
    if _SVP.search(t):
        return "svp"
    if _SUB_VP.search(t):
        return "director"
    if _VP.search(t):
        return "vp"
    if _HEAD.search(t):
        return "head"
    if _DIRECTOR.search(t):
        return "director"
    return None


def is_small_company_executive(title: Optional[str]) -> bool:
    """Founder/CEO/COO/owner/president. 'Vice President …' is NEVER founder tier (instruction; production's
    ``is_founder_tier('Vice President of Sales')`` returns True)."""
    t = normalize_title(title)
    if re.search(r"\b(?:founder|cofounder|co founder|ceo|chief executive officer|owner|coo|chief operating officer)\b", t):
        return True
    return bool(re.search(r"(?<!vice )\bpresident\b", t))


# --- functional phrases (complete phrases; four mappings kept separate) ------------------------------------------
ENGINEERING = ("engineering", "software", "technology", "technologies", "tech", "information technology", "it",
               "information systems", "information officer", "ai", "artificial intelligence", "machine learning",
               "ml", "data", "data science", "analytics", "automation", "platform", "infrastructure", "cloud",
               "devops", "r and d", "research and development", "architecture", "cto", "cio", "cdo", "caio", "cpto")
IT = ("information technology", "it", "it operations", "information systems", "information officer",
      "infrastructure", "technology", "enterprise applications", "cio", "cto")
GTM = ("sales", "revenue", "revops", "rev ops", "revenue operations", "sales operations", "sales ops", "gtm",
       "go to market", "commercial", "business systems", "cro")
MARKETING = ("marketing", "growth", "demand generation", "demand gen", "brand", "brands", "creative", "cmo")
CS_LEADERSHIP = ("customer success", "client success", "customer experience", "client experience", "cx",
                 "chief customer", "chief experience", "account management", "client services", "client service",
                 "customer operations", "member experience")
SUPPORT = ("customer support", "support", "customer service", "customer services", "customer care",
           "technical support", "member services")
FAMILY_PHRASES: Dict[str, Tuple[str, ...]] = {
    "engineering": ENGINEERING, "it": IT, "gtm": GTM, "sales": GTM, "marketing": MARKETING,
    "cs_leadership": CS_LEADERSHIP, "support": SUPPORT,
}
# Phrase-level negatives, per family (cal#16 energy infrastructure, cal#17 market data, cal#26 sales engineering,
# cal#18 GTM engineering).
FAMILY_NEGATIVES: Dict[str, Tuple[str, ...]] = {
    "engineering": ("sales engineering", "go to market engineering", "gtm engineering", "market data", "energy"),
    "it": ("energy",),
    "support": ("it support", "desktop support", "sales support", "business support", "engineering support"),
}
# A title that names another business function is not an owner in any of the four mappings (cal#11 "Brand &
# Commercial Finance", cal#47 "Purchasing Support Operations").
OTHER_FUNCTION = ("finance", "financial", "accounting", "controller", "legal", "counsel", "human resources", "hr",
                  "people", "talent", "recruiting", "recruitment", "procurement", "purchasing", "supply chain",
                  "facilities", "real estate", "compliance", "investor relations", "tax", "treasury", "payroll")
EXCLUDED_TOKENS = ("assistant", "intern", "advisor", "adviser", "board member", "board of", "investor", "former",
                   "retired", "consultant", "contractor", "fractional", "student", "volunteer", "recruiter")
REGIONAL_SCOPE = ("regional", "region", "area", "territory", "district", "zone")   # cal#38 Regional Sales Director
ASIA = r"\basia\b"                                                                     # cal#40 "…, Asia"


# --- opening routing (instruction: account management, sales and IT helpdesk are not automatically CS) --------
_IT_OPENING = ("it support", "it helpdesk", "it help desk", "help desk", "helpdesk", "service desk", "desktop support",
               "applications support", "application support", "it specialist", "it technician", "it analyst", "noc",
               "network operations", "system administrator", "systems administrator")          # cal#47 (job#170)
_AM_OPENING = ("account manager", "account management", "account executive", "key account", "client partner",
               "partnerships manager", "partnership manager", "partner manager")   # cal#21 cal#28 cal#32/33 cal#44
_SALES_OPENING = ("sales", "business development", "inside sales")
_OUT_OF_MAPPING_MARKETING = ("product designer", "product design", "ux designer", "ui designer", "ux", "user experience")  # cal#1


def opening_route(offer_group: str, job_title: Optional[str]) -> Tuple[str, Tuple[str, ...]]:
    """-> (route name, owner families in priority order). An empty tuple means no owner inside the four mappings."""
    t = normalize_title(job_title)
    if offer_group == "ai_engineering_automation":
        return "engineering", ("engineering",)
    if offer_group == "gtm_revops_salesops":
        return "gtm", ("gtm",)
    if offer_group == "marketing_creative":
        if _any(_OUT_OF_MAPPING_MARKETING, t):
            return "product_design_outside_mapping", ()
        return "marketing", ("marketing",)
    if offer_group == "customer_success_support":
        if _any(_IT_OPENING, t):
            return "it_helpdesk", ("it",)
        if _any(_AM_OPENING, t):
            # Account management belongs to CS or sales leadership, never to technical support (cal#32).
            return "account_management", ("cs_leadership", "sales")
        if _any(_SALES_OPENING, t):
            return "sales", ("sales",)
        return "customer_success", ("cs_leadership", "support")
    raise ValueError(f"unknown offer group {offer_group!r}")


def small_executive_allowed(route: str) -> bool:
    return route not in ("marketing", "product_design_outside_mapping")   # not in the marketing mapping


@dataclass
class RoleDecision:
    accepted: bool
    reason: str
    tier: Optional[str] = None           # function_executive | functional_director | small_company_executive
    level: Optional[str] = None
    family: Optional[str] = None
    rank: int = 0                        # higher = more senior


def is_small_company(linkedin_headcount: Optional[int], apollo_employees: Optional[int]) -> bool:
    """Both known sizes must be small; at least one must be known. A LinkedIn division page under a large Apollo
    organization is not a small company (cal#33: Shurtape Industrial 28 on LinkedIn, 850 in Apollo)."""
    sizes = [s for s in (linkedin_headcount, apollo_employees) if isinstance(s, int)]
    return bool(sizes) and all(s <= SMALL_COMPANY_MAX for s in sizes)


def territory_foreign(title: Optional[str], headline: Optional[str]) -> List[str]:
    scope = f"{title or ''} | {headline or ''}"
    foreign = [k for k, p in FOREIGN_TERRITORY.items() if re.search(p, scope, re.I)]
    if re.search(ASIA, scope, re.I):
        foreign.append("ASIA")
    if foreign and not re.search(US_TERRITORY, scope, re.I) and not re.search(GLOBAL_SCOPE, scope, re.I):
        return foreign
    return []


def map_contact(*, contact_title: Optional[str], offer_group: str, job_title: Optional[str],
                linkedin_headcount: Optional[int], apollo_employees: Optional[int]) -> RoleDecision:
    """Is this person a decision-maker for THIS opening? Role only: organization, employment, territory and email
    are separate gates."""
    route, families = opening_route(offer_group, job_title)
    if not families:
        return RoleDecision(False, f"role:{route}")
    t = normalize_title(contact_title)
    if not t:
        return RoleDecision(False, "role:no_title")
    if _any(EXCLUDED_TOKENS, t):
        return RoleDecision(False, "role:excluded_title_token")
    if _any(OTHER_FUNCTION, t):
        return RoleDecision(False, "role:other_business_function")
    level = seniority(t)
    hit = None
    for fam in families:
        if _any(FAMILY_PHRASES[fam], t) and not _any(FAMILY_NEGATIVES.get(fam, ()), t):
            hit = fam
            break
    small = is_small_company(linkedin_headcount, apollo_employees)
    opening_rank = LEVEL_RANK.get(seniority(job_title) or "", 0)
    if hit and level in ("c_level", "evp", "svp", "vp", "head"):
        dec = RoleDecision(True, "role:function_executive", "function_executive", level, hit, LEVEL_RANK[level])
    elif hit and level == "director":
        if _any(REGIONAL_SCOPE, t):
            return RoleDecision(False, "role:regional_director_not_functional_owner")
        dec = RoleDecision(True, "role:functional_director", "functional_director", level, hit, LEVEL_RANK[level])
    elif is_small_company_executive(t) and small_executive_allowed(route):
        if not small:
            return RoleDecision(False, "role:executive_not_allowed_for_size")
        dec = RoleDecision(True, "role:small_company_executive", "small_company_executive", "small_exec", None, 7)
    elif level:
        return RoleDecision(False, "role:senior_other_function")
    elif hit:
        return RoleDecision(False, "role:function_below_director")
    else:
        return RoleDecision(False, "role:not_decision_maker")
    # Peer guard: an opening at Director level or above needs a MORE senior owner (cal#34, cal#9).
    if opening_rank >= LEVEL_RANK["director"] and dec.rank <= opening_rank:
        return RoleDecision(False, "role:peer_or_junior_of_opening", dec.tier, dec.level, dec.family, dec.rank)
    return dec


# --- true hiring company ----------------------------------------------------------------------------------------
VC_INDUSTRY = "Venture Capital and Private Equity Principals"


def _distinctive_tokens(name: Optional[str]) -> List[str]:
    return [t for t in normalize_company_name(name).split() if t not in _GENERIC_NAME_TOKENS and len(t) >= 3]


def hiring_company_problem(*, employer_name: Optional[str], employer_domain: Optional[str], industry: Optional[str],
                           recruitment_agency: Optional[bool], description: Optional[str]) -> Optional[str]:
    """None when the listed employer is the true hiring company; otherwise the reason it is not."""
    if not employer_name or is_placeholder_company_name(employer_name):
        return "org_unresolvable:placeholder_employer"                       # "Stealth Startup", "Confidential Company"
    if recruitment_agency is True:
        return "not_true_hiring_company:staffing_or_recruiting"
    if employer_domain and is_intermediary_host(employer_domain):
        return "not_true_hiring_company:job_board_or_ats_host"
    if industry == VC_INDUSTRY:
        text = normalize_title(description)
        tokens = _distinctive_tokens(employer_name)
        if not tokens or not any(re.search(r"\b" + re.escape(t) + r"\b", text) for t in tokens):
            return "not_true_hiring_company:vc_attributed_portfolio_job"      # SoGal->Lovevery, Newfund->Aircall
    return None


# --- organization and email ------------------------------------------------------------------------------------
def email_alignment(*, email: Optional[str], job_domain: str, job_slug: str, apollo_org: Dict,
                    company_evidence_domains: Iterable[str]) -> Tuple[str, str]:
    """-> ('exact' | 'corroborated_alternate' | 'quarantine' | 'none', basis).

    Exact employer-domain equality is the default. An alternate domain is accepted only when the Apollo
    organization is proven to be the employer (same LinkedIn page, or same primary domain) AND saved evidence ties
    the domain to it: it is one of the employer's own recorded domains, or it spells the shared LinkedIn company
    page (cal#22 baltosoftware.com <-> 'baltosoftware'; cal#43 coval.dev <-> 'covaldev'). Initialisms (cal#34
    jpms.com) and parent-company domains (cal#36 redventures.com) are quarantined.
    """
    ed = email_domain(email)
    if not ed:
        return "none", ""
    jd = normalize_company_domain(job_domain)
    if ed == jd:
        return "exact", "employer_domain"
    org_slug = linkedin_slug(apollo_org.get("linkedin_url") or "")
    org_domain = normalize_company_domain(apollo_org.get("primary_domain") or apollo_org.get("website_url") or "")
    same_org = bool((job_slug and org_slug == job_slug) or (jd and org_domain == jd))
    if not same_org:
        return "quarantine", "apollo_org_not_proven_employer"
    recorded = {normalize_company_domain(d) for d in company_evidence_domains if d}
    recorded |= {normalize_company_domain(apollo_org.get(k) or "") for k in ("primary_domain", "website_url")}
    recorded.discard("")
    if ed in recorded:
        return "corroborated_alternate", "recorded_company_domain"
    label, _, tld = ed.partition(".")
    if job_slug and org_slug == job_slug and job_slug in (label, label + tld.replace(".", "")):
        return "corroborated_alternate", "linkedin_page_spells_domain"
    return "quarantine", "alternate_domain_unproven"


# --- reuse ------------------------------------------------------------------------------------------------------
def reuse_unit(company_key: str, offer_group: str) -> Tuple[str, str]:
    """Contacts are discovered and enriched ONCE per company x offer group and linked to every relevant opening."""
    return (company_key, offer_group)


@dataclass
class EnrichmentLedger:
    """A person id is enriched at most once, whatever the number of openings it serves."""
    bought: Set[str] = field(default_factory=set)
    reused: int = 0

    def need_purchase(self, person_ref: str) -> bool:
        if person_ref in self.bought:
            self.reused += 1
            return False
        self.bought.add(person_ref)
        return True
