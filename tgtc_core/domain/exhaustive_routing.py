"""Exhaustive nine-campaign routing (authoritative business-scope change, 2026-09-20).

The nine campaigns are collectively exhaustive for every job that passes the
hard eligibility gates. A job is never rejected for "no campaign fit", for an
absent allowlist title, for an imperfect role pattern, for spanning several
functions, or for a classifier tie. Those are routing problems now.

Order of authority, highest first:

1. the twelve approved hard exclusions (``facts.py``) -- unchanged, and scope
   never overrides them;
2. deterministic evidence in the description (``classification.score_functions``);
3. deterministic evidence in the title (this module);
4. the semantic classifier, for unresolved ties only;
5. the fallback destination.

``OPERATIONS`` is the fallback because the instruction names it as the home for
"other legitimate corporate roles without a better functional destination". It
is a destination, never a rejection.

Evidence discipline is unchanged. A title route cites the title it matched, and
nothing else. Employer boilerplate, benefits and EEO language are still not
campaign evidence, which is why this module never reads the description.

Everything here is gated by ``TGTC_EXHAUSTIVE_NINE_CAMPAIGNS=1``. With the flag
off the module is inert and ``tgtc-core/2`` decisions stand byte-identical, so
rollback is a flag flip and nothing else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

FLAG_ENV = "TGTC_EXHAUSTIVE_NINE_CAMPAIGNS"
RULE_VERSION = "tgtc-exhaustive/1"

#: "other legitimate corporate roles without a better functional destination".
FALLBACK_FUNCTION = "operations"
FALLBACK_PHRASE = "corporate role, no more specific functional destination"


@dataclass(frozen=True)
class TitleRoute:
    function_key: str
    phrase: str
    excerpt: str
    basis: str = "title"


#: Ordered. First match wins, so every entry is placed where its specificity
#: demands: "Technical Support Engineer" is support, not engineering;
#: "Technical Recruiter" is people, not engineering; "Sales Operations" is GTM,
#: not operations; "Marketplace Operations" is ecommerce, not operations;
#: "Product Marketing" is marketing, not product.
_ROUTES: Tuple[Tuple[str, str, str], ...] = (
    # --- Customer Experience: support reads before any "engineer" pattern ---
    ("customer_support",
     r"technical support|support engineer|customer support|help\s?desk|service desk|"
     r"support specialist|support representative|support agent|support analyst",
     "customer support"),
    ("customer_success",
     r"customer success|client success|customer experience|client services|"
     r"implementation|onboarding|service delivery|customer operations|renewals|"
     r"account manager|customer care",
     "customer success"),
    # --- People & HR: reads before engineering so "Technical Recruiter" lands here ---
    ("people_hr",
     r"recruit|talent acquisition|sourcer|human resources|people operations|people ops|"
     r"hris|compensation|benefits|learning and development|employee experience|"
     r"employee relations|talent partner|head of people|chief people|people partner|"
     r"hr business partner|onboarding specialist",
     "people and HR"),
    # --- Ecommerce: reads before operations and marketing ---
    ("ecommerce",
     r"e-?commerce|marketplace|merchandis|online retail|digital storefront|shopify|"
     r"amazon seller|direct-to-consumer|catalog manage",
     "ecommerce"),
    # --- Product Marketing is marketing, not product ---
    ("marketing", r"product marketing", "product marketing"),
    # --- Product ---
    ("product",
     r"product manager|product management|product owner|product operations|product ops|"
     r"product strategy|product research|product design|product analytics|product lead|"
     r"head of product|chief product|director of product|vp of product|"
     r"user experience|user research|ux",
     "product"),
    # --- GTM Systems: reads before operations, finance and engineering ---
    ("gtm_revenue",
     r"revenue operations|revops|sales operations|sales ops|gtm operations|"
     r"go-to-market operations|deal desk|sales enablement|enablement|partnerships|"
     r"business development|account executive|sales development|crm administrator|"
     r"salesforce administrator|revenue automation|sales engineer|solutions engineer|"
     r"sales manager|sales director|head of sales|chief revenue",
     "GTM systems and revenue"),
    # --- Finance ---
    ("finance",
     r"accountant|accounting|fp&a|financial planning|controller|bookkeep|payroll|"
     r"treasury|taxation|tax manager|tax analyst|audit|accounts payable|"
     r"accounts receivable|finance|financial analyst|financial reporting",
     "finance"),
    # --- AI & Technical: reads before the generic creative/design patterns ---
    ("engineering",
     r"software engineer|engineer|engineering|developer|programmer|data scientist|"
     r"data analyst|analytics engineer|machine learning|artificial intelligence|"
     r"devops|sre|site reliability|infrastructure|platform|cloud|security|"
     r"cybersecurity|quality assurance|test automation|automation|"
     r"information technology|systems administrator|sysadmin|network|"
     r"database administrator|technical lead|chief technology|architect|"
     r"full[\s-]?stack|back[\s-]?end|front[\s-]?end",
     "AI and technical automation"),
    # --- Marketing & Creative: generic creative patterns last of the specifics ---
    ("marketing",
     r"marketing|brand|content|communications|public relations|demand generation|"
     r"growth|seo|sem|social media|creative|copywriter|designer|design|media buyer|"
     r"community manager|editor|campaign manager",
     "marketing and creative"),
    # --- Operations: the named corporate-operations roles ---
    ("operations",
     r"business operations|biz\s?ops|strategy and operations|operations manager|"
     r"operations lead|operations analyst|operations specialist|operations director|"
     r"program manager|project manager|pmo|process improvement|continuous improvement|"
     r"procurement|supply chain|logistics|legal operations|compliance|contracts|"
     r"paralegal|executive assistant|office manager|administrative|facilities|"
     r"vendor manage|head of operations|chief operating|chief of staff",
     "operations"),
)

_COMPILED: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = tuple(
    (fn, re.compile(pattern, re.I), phrase) for fn, pattern, phrase in _ROUTES
)


def exhaustive_enabled(env: Optional[Mapping[str, str]]) -> bool:
    return str((env or {}).get(FLAG_ENV, "") or "").strip() == "1"


def route_by_title(title: Optional[str]) -> Optional[TitleRoute]:
    """Deterministic functional destination for a job title, or None.

    None means "this title says nothing", not "reject": the caller falls back.
    """
    text = str(title or "").strip()
    if not text:
        return None
    for function_key, rx, phrase in _COMPILED:
        match = rx.search(text)
        if match:
            return TitleRoute(function_key=function_key, phrase=phrase, excerpt=text[:300])
    return None


def fallback_route(title: Optional[str]) -> TitleRoute:
    """The destination of last resort. Never a rejection."""
    return TitleRoute(
        function_key=FALLBACK_FUNCTION,
        phrase=FALLBACK_PHRASE,
        excerpt=str(title or "").strip()[:300],
        basis="fallback",
    )


__all__ = [
    "FLAG_ENV", "RULE_VERSION", "FALLBACK_FUNCTION", "FALLBACK_PHRASE",
    "TitleRoute", "exhaustive_enabled", "route_by_title", "fallback_route",
]
