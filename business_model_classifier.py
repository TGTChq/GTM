"""Evidence-backed company business-model classifier.

Patterns describe the company's own offering, not incidental customer mentions.
The classifier returns UNKNOWN rather than guessing when evidence is too thin.
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
    "media_broadcasting_news": [r"\bbroadcast(?:ing| media) company\b", r"\binternet news (?:company|publisher)\b", r"\bmedia production studio\b"],
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


def _items(category: str, excerpts: List[str], source_url: str) -> List[EvidenceItem]:
    return [
        EvidenceItem(
            "business_model", category, EvidenceStatus.VERIFIED_OFFICIAL,
            "company_website", source_url, excerpt, 0.97,
        )
        for excerpt in excerpts[:4]
    ]


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
    combined = f"{official}\n{apollo}\n{job_text}"[:250_000]

    for category, patterns, reason in (
        ("staffing_recruiting", STAFFING_PATTERNS, "REJECT_STAFFING"),
        ("outsourcing_staff_augmentation", OUTSOURCING_PATTERNS, "REJECT_OUTSOURCING"),
        ("healthcare_vertical_software", HEALTHCARE_VERTICAL_SOFTWARE_PATTERNS, "REJECT_HEALTHCARE"),
        ("healthcare_delivery", HEALTHCARE_CORE_PATTERNS, "REJECT_HEALTHCARE"),
        ("government_delivery", GOVERNMENT_PATTERNS, "REJECT_GOVERNMENT"),
    ):
        excerpts = _find(patterns, combined)
        if excerpts:
            return BusinessModelResult("EXCLUDED", category, reason, _items(category, excerpts, source_url))

    for category, patterns in EXCLUDED_INDUSTRY_PATTERNS.items():
        excerpts = _find(patterns, combined)
        if excerpts:
            return BusinessModelResult(
                "EXCLUDED", category, "REJECT_EXCLUDED_INDUSTRY",
                _items(category, excerpts, source_url),
            )

    # Fail closed: arbitrary first-party page text is not a verified business
    # model. Require a clause that describes what the company itself builds,
    # sells, operates or provides. Navigation/careers boilerplate alone remains
    # UNKNOWN and is replaced by the top-up loop.
    allowed_excerpts = _find(ALLOWED_MODEL_PATTERNS, official)
    if allowed_excerpts:
        return BusinessModelResult(
            "ALLOWED", "commercial_product_or_service", "",
            _items("commercial_product_or_service", allowed_excerpts, source_url),
        )

    industry_norm = apollo_industry.strip().lower()
    if len(apollo_description.strip()) >= 120 and industry_norm not in GENERIC_INDUSTRIES:
        item = EvidenceItem(
            "business_model", "commercial_product_or_service",
            EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo",
            excerpt=apollo_description[:700], confidence=0.82,
        )
        return BusinessModelResult("ALLOWED", "commercial_product_or_service", "", [item])

    return BusinessModelResult("UNKNOWN", "unknown", "UNVERIFIED_BUSINESS_MODEL", [])
