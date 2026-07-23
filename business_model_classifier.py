"""Evidence-backed exclusion classifier for company business models.

The classifier is a positive-evidence veto: first-party or Apollo evidence may
exclude a company, while absence of such evidence does not become another gate.
Job text can corroborate a decision but cannot exclude an account by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from evidence_types import EvidenceItem, EvidenceStatus


@dataclass
class BusinessModelResult:
    state: str  # ALLOWED / EXCLUDED / UNKNOWN
    category: str
    reason_code: str = ""
    evidence: List[EvidenceItem] = field(default_factory=list)


EXCLUDED_INDUSTRY_PATTERNS = {
    "nonprofit": [r"\b501\(c\)\(3\)\b", r"\bnon[- ]profit organization\b", r"\bnot[- ]for[- ]profit\b"],
    "chemical_manufacturing": [r"\bchemical manufacturing\b", r"\bmanufacturer of (?:specialty )?chemicals\b"],
    "book_publishing": [r"\bbook publish(?:er|ing)\b"],
    "events_services": [r"\bevent production company\b", r"\bevents services\b"],
    "media_broadcasting_news": [
        r"\bbroadcast(?:ing| media) company\b",
        r"\b(?:online|internet|digital|financial) news (?:company|publisher|platform|site|outlet)\b",
        r"\b(?:online|news|digital|financial) media (?:company|publisher|platform|outlet)\b",
        r"\bmedia production (?:studio|company|services)\b",
        r"\bfinancial news and (?:data|media|information)\b",
    ],
}

STAFFING_PATTERNS = [
    r"\bwe (?:recruit|place|staff|connect) (?:talent|candidates|professionals)\b",
    r"\bour (?:staffing|recruiting|recruitment|executive search) (?:services|solutions|agency)\b",
    r"\b(?:staffing|recruiting|recruitment|executive search) (?:firm|agency|company)\b",
    r"\brecruitment process outsourcing\b",
    r"\bRPO services\b",
]
OUTSOURCING_PATTERNS = [
    r"\bwe provide (?:dedicated |remote |offshore |nearshore )?(?:software )?(?:developers|development teams|engineering teams|staff augmentation)\b",
    r"\b(?:software|IT|business process) outsourcing (?:company|services|solutions)\b",
    r"\bstaff augmentation (?:services|company|solutions)\b",
    r"\boffshore development (?:center|company|services|teams)\b",
    r"\bnearshore (?:software )?development (?:company|services|teams)\b",
    r"\bdedicated external (?:developers|teams)\b",
    r"\btalent marketplace\b",
    r"\bbusiness process outsourcing\b",
]
HEALTHCARE_CORE_PATTERNS = [
    r"\bwe (?:provide|deliver|offer) (?:home |behavioral |mental )?(?:healthcare|health care|patient care|clinical care|medical care)\b",
    r"\b(?:hospital|clinic|medical practice|diagnostic laboratory|patient care|medical billing) (?:network|provider|company|services|organization)\b",
    r"\bmedical billing (?:company|services|solutions)\b",
    r"\bdiagnostic testing (?:company|services|provider)\b",
    r"\bhome health(?:care)? (?:agency|provider|services)\b",
]
HEALTHCARE_VERTICAL_SOFTWARE_PATTERNS = [
    r"\b(?:software|platform|practice management|EHR|electronic health record) (?:built|designed|created) (?:for|exclusively for) (?:healthcare providers|clinics|therapists|medical practices|home health agencies)\b",
    r"\bpractice management software for (?:therapists|clinicians|healthcare providers|medical practices)\b",
    r"\bhome health(?:care)? (?:management )?software\b",
    r"\bpatient management platform\b",
    r"\bmedical billing software\b",
]
GOVERNMENT_PATTERNS = [
    r"\bwe (?:serve|support|deliver services to|provide solutions to) (?:federal|state|local|government) agencies\b",
    r"\bgovernment contractor\b",
    r"\bGSA (?:schedule|contract|vehicle)\b",
    r"\bpublic sector consulting (?:firm|services)\b",
    r"\bfederal program delivery\b",
]


ALLOWED_MODEL_PATTERNS = [
    r"\bwe (?:build|develop|create|make|manufacture|sell|operate|provide|offer|deliver)\b[^.\n]{0,180}\b(?:software|platform|products?|services?|solutions?|technology|tools?|equipment|consumer goods|financial services|logistics|education|training|analytics|security)\b",
    r"\bour (?:software|platform|product|service|solution|technology|tools?|business)\b[^.\n]{0,180}\b(?:helps?|enables?|supports?|serves?|provides?|automates?|connects?|protects?|manages?|delivers?)\b",
    r"\b(?:SaaS|software|technology|cybersecurity|analytics|e[- ]commerce|retail|manufacturing|logistics|financial services|education) (?:company|platform|provider|business|product|solutions?)\b",
    r"\b(?:is|are) (?:an? |the )?(?:global |leading |independent )?(?:SaaS|software|technology|cybersecurity|analytics|e[- ]commerce|retail|manufacturing|logistics|financial services|education|training) (?:company|platform|provider|business)\b",
    r"\bprovides?\b[^.\n]{0,180}\b(?:software|platform|products?|services?|solutions?|technology|tools?|equipment|financial services|logistics|education|training|analytics|security)\b",
]

GENERIC_INDUSTRIES = {
    "", "information technology & services", "information technology and services",
    "computer software", "software", "internet", "research", "consulting",
    "management consulting", "human resources", "professional services",
}


def _find(patterns: List[str], text: str) -> List[str]:
    evidence: List[str] = []
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            start = max(0, match.start() - 120)
            end = min(len(text), match.end() + 180)
            evidence.append(text[start:end].strip())
    return evidence


def _items(
    category: str,
    excerpts: List[str],
    source_url: str,
    *,
    status: EvidenceStatus = EvidenceStatus.VERIFIED_OFFICIAL,
    source_type: str = "company_website",
    confidence: float = 0.97,
) -> List[EvidenceItem]:
    return [
        EvidenceItem(
            "business_model", category, status,
            source_type, source_url, excerpt, confidence,
        )
        for excerpt in excerpts[:4]
    ]


def _evidence_from_sources(
    category: str,
    patterns: List[str],
    *,
    official: str,
    apollo: str,
    job_text: str,
    source_url: str,
) -> List[EvidenceItem]:
    evidence: List[EvidenceItem] = []
    official_hits = _find(patterns, official)
    if official_hits:
        evidence.extend(_items(category, official_hits, source_url))
    apollo_hits = _find(patterns, apollo)
    if apollo_hits:
        evidence.extend(_items(
            category,
            apollo_hits,
            "",
            status=EvidenceStatus.VERIFIED_CROSS_SOURCE,
            source_type="apollo",
            confidence=0.90,
        ))
    job_hits = _find(patterns, job_text)
    if job_hits:
        evidence.extend(_items(
            category,
            job_hits,
            "",
            status=EvidenceStatus.VERIFIED_CROSS_SOURCE,
            source_type="job_provider",
            confidence=0.86,
        ))
    return evidence


def classify_business_model(
    *,
    company_text: str,
    apollo_industry: str = "",
    apollo_description: str = "",
    source_url: str = "",
    job_text: str = "",
) -> BusinessModelResult:
    official = re.sub(r"\s+", " ", str(company_text or "")).strip()
    apollo = re.sub(r"\s+", " ", f"{apollo_industry}. {apollo_description}").strip()

    for category, patterns, reason in (
        ("staffing_recruiting", STAFFING_PATTERNS, "REJECT_STAFFING"),
        ("outsourcing_staff_augmentation", OUTSOURCING_PATTERNS, "REJECT_OUTSOURCING"),
        ("healthcare_vertical_software", HEALTHCARE_VERTICAL_SOFTWARE_PATTERNS, "REJECT_HEALTHCARE"),
        ("healthcare_delivery", HEALTHCARE_CORE_PATTERNS, "REJECT_HEALTHCARE"),
        ("government_delivery", GOVERNMENT_PATTERNS, "REJECT_GOVERNMENT"),
    ):
        evidence = _evidence_from_sources(
            category,
            patterns,
            official=official,
            apollo=apollo,
            job_text=job_text,
            source_url=source_url,
        )
        decisive = [item for item in evidence if item.source_type != "job_provider"]
        if decisive:
            return BusinessModelResult("EXCLUDED", category, reason, evidence)

    for category, patterns in EXCLUDED_INDUSTRY_PATTERNS.items():
        evidence = _evidence_from_sources(
            category,
            patterns,
            official=official,
            apollo=apollo,
            job_text=job_text,
            source_url=source_url,
        )
        decisive = [item for item in evidence if item.source_type != "job_provider"]
        if decisive:
            return BusinessModelResult(
                "EXCLUDED", category, "REJECT_EXCLUDED_INDUSTRY",
                evidence,
            )

    # Positive allowed-model evidence is retained for audit/ranking, but it is
    # not required to pass the account gate.
    allowed_excerpts = _find(ALLOWED_MODEL_PATTERNS, official)
    if allowed_excerpts:
        return BusinessModelResult(
            "ALLOWED", "commercial_product_or_service", "",
            _items("commercial_product_or_service", allowed_excerpts, source_url),
        )

    # Apollo can corroborate a business model only when its description contains
    # an affirmative own-offering clause. Description length plus a non-generic
    # industry is not evidence by itself; that permissive fallback allowed
    # Benzinga to pass merely because its profile was detailed.
    apollo_allowed = _find(ALLOWED_MODEL_PATTERNS, apollo_description)
    if apollo_allowed:
        return BusinessModelResult(
            "ALLOWED",
            "commercial_product_or_service",
            "",
            _items(
                "commercial_product_or_service",
                apollo_allowed,
                "",
                status=EvidenceStatus.VERIFIED_CROSS_SOURCE,
                source_type="apollo",
                confidence=0.88,
            ),
        )

    # Absence of an exclusion signal is not evidence that the account is bad.
    # The Account Gate already requires a resolved organization, domain, employee
    # count and known industry. Business-model classification is therefore a
    # negative-evidence veto, not an additional proof-of-allowance gate.
    return BusinessModelResult("ALLOWED", "no_excluded_model_detected", "", [])
