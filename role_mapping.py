"""Map TGTC target roles to campaign functions and likely hiring managers.

Intent-Based Outbound 2.0 separates two concepts that used to be conflated:

- function bucket: how roles are grouped for one account-level campaign/shortlist;
- hiring-manager bucket: which leadership hierarchy Apollo should search.

For example, Data Analyst belongs to the broader Engineering/Data/IT campaign
function but should prioritize Data leadership rather than a generic VP Eng.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List

from role_catalog import (
    ROLE_DEFINITIONS,
    get_function_bucket,
    get_hiring_manager_bucket,
)


# Backward-compatible public mapping used by tests and reporting.
ROLE_TO_BUCKET: Dict[str, str] = {
    role: definition.function_bucket for role, definition in ROLE_DEFINITIONS.items()
}
ROLE_TO_HIRING_MANAGER_BUCKET: Dict[str, str] = {
    role: definition.hiring_manager_bucket for role, definition in ROLE_DEFINITIONS.items()
}


BUCKET_TITLES: Dict[str, List[str]] = {
    "gtm_revenue": [
        "Head of Revenue Operations",
        "Head of RevOps",
        "VP Revenue Operations",
        "VP of Revenue Operations",
        "Chief Revenue Officer",
        "CRO",
        "Head of GTM",
        "VP Sales",
        "VP of Sales",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "engineering": [
        "CTO",
        "Chief Technology Officer",
        "VP Engineering",
        "VP of Engineering",
        "Head of Engineering",
        "Head of AI",
        "VP AI",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "data": [
        "Chief Data Officer",
        "VP Data",
        "VP of Data",
        "Head of Data",
        "Head of Analytics",
        "VP Analytics",
        "CTO",
        "VP Engineering",
        "VP of Engineering",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "it": [
        "CIO",
        "Chief Information Officer",
        "VP Information Technology",
        "VP of Information Technology",
        "VP IT",
        "Head of IT",
        "Head of Information Technology",
        "CTO",
        "Chief Technology Officer",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "marketing": [
        "CMO",
        "Chief Marketing Officer",
        "VP Marketing",
        "VP of Marketing",
        "Head of Marketing",
        "Head of Growth",
        "VP Growth",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "customer_success": [
        "Chief Customer Officer",
        "VP Customer Success",
        "VP of Customer Success",
        "Head of Customer Success",
        "Head of Customer Experience",
        "VP Customer Experience",
        "COO",
        "Chief Operating Officer",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "customer_support": [
        "VP Customer Support",
        "VP of Customer Support",
        "Head of Customer Support",
        "Head of Support",
        "Head of Customer Experience",
        "VP Customer Experience",
        "Chief Customer Officer",
        "COO",
        "Chief Operating Officer",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "finance": [
        "CFO",
        "Chief Financial Officer",
        "VP Finance",
        "VP of Finance",
        "Head of Finance",
        "Controller",
        "Corporate Controller",
        "COO",
        "Chief Operating Officer",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "operations": [
        "COO",
        "Chief Operating Officer",
        "VP Operations",
        "VP of Operations",
        "Head of Operations",
        "Chief of Staff",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "people_hr": [
        "CHRO",
        "Chief Human Resources Officer",
        "Chief People Officer",
        "VP People",
        "VP of People",
        "VP Human Resources",
        "VP of Human Resources",
        "Head of People",
        "Head of Talent Acquisition",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "product": [
        "Chief Product Officer",
        "CPO",
        "VP Product",
        "VP of Product",
        "Head of Product",
        "Head of Design",
        "VP Design",
        "CTO",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "ecommerce": [
        "VP Ecommerce",
        "VP of Ecommerce",
        "Head of Ecommerce",
        "Head of E-commerce",
        "CMO",
        "Chief Marketing Officer",
        "VP Marketing",
        "COO",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
    "partnerships": [
        "VP Partnerships",
        "VP of Partnerships",
        "Head of Partnerships",
        "Chief Business Officer",
        "VP Business Development",
        "Head of Business Development",
        "Chief Revenue Officer",
        "CRO",
        "Founder",
        "Co-Founder",
        "CEO",
    ],
}


_GTM_SYSTEMS_TITLES = [
    "Head of GTM Systems",
    "Head of Revenue Systems",
    "VP Revenue Systems",
    "VP of Revenue Systems",
    "Head of Business Systems",
]
_GTM_SALES_OPS_TITLES = [
    "Head of Sales Operations",
    "VP Sales Operations",
    "VP of Sales Operations",
]
_GTM_MARKETING_OPS_TITLES = [
    "Head of Marketing Operations",
    "VP Marketing Operations",
    "VP of Marketing Operations",
]
_GTM_OPERATIONS_FALLBACK_TITLES = ["COO", "Chief Operating Officer"]

_GTM_SYSTEMS_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\bgtm systems?\b",
        r"\brevenue systems?\b",
        r"\bbusiness systems?\b",
        r"\bcrm (architecture|systems?|infrastructure)\b",
        r"\b(salesforce|hubspot)\b",
        r"\bsystems integration\b",
    )
]
_GTM_SALES_OPS_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\bsales operations\b",
        r"\bsales ops\b",
        r"\blead routing\b",
        r"\bterritory\b",
        r"\bsales process\b",
    )
]
_GTM_MARKETING_OPS_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\bmarketing operations\b",
        r"\bmarketing ops\b",
        r"\bmarketing automation\b",
        r"\b(marketo|pardot|hubspot marketing)\b",
        r"\battribution\b",
    )
]

_AUTOMATION_GTM_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\bgtm\b",
        r"\bgo[- ]to[- ]market\b",
        r"\brevops\b",
        r"\brevenue operations\b",
        r"\bcrm automation\b",
        r"\blead routing\b",
        r"\blead enrichment\b",
        r"\boutbound (systems?|automation|infrastructure)\b",
        r"\b(salesforce|hubspot|clay|apollo|outreach|salesloft|instantly)\b",
        r"\bsales (operations|systems|automation)\b",
    )
]
_AUTOMATION_TECH_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\b(ai|llm) agents?\b",
        r"\bagentic\b",
        r"\blarge language models?\b",
        r"\bgenerative ai\b",
        r"\bpython\b",
        r"\bbackend\b",
        r"\bsoftware engineering\b",
        r"\bproduction[- ]grade\b",
        r"\bcloud (infrastructure|architecture)\b",
        r"\b(machine learning|ml)\b",
    )
]


def _dedupe_titles(titles: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for title in titles:
        key = title.lower().strip()
        if key not in seen:
            seen.add(key)
            result.append(title)
    return result


def _promote_founders(titles: List[str], employee_count: int | None) -> List[str]:
    if employee_count is None or employee_count >= 75:
        return titles
    founder_titles = ["Founder", "Co-Founder", "CEO"]
    return founder_titles + [title for title in titles if title not in founder_titles]


def get_bucket_name(matched_role: str) -> str:
    """Return the campaign/function bucket for a canonical role."""
    return get_function_bucket(matched_role)


def get_hiring_manager_bucket_name(matched_role: str) -> str:
    """Return the buyer-title hierarchy bucket for a canonical role."""
    return get_hiring_manager_bucket(matched_role)


def _automation_is_gtm(job: Dict) -> bool:
    text = f"{job.get('job_title') or ''}\n{job.get('job_description') or ''}"[:16000]
    gtm_score = sum(bool(pattern.search(text)) for pattern in _AUTOMATION_GTM_PATTERNS)
    technical_score = sum(bool(pattern.search(text)) for pattern in _AUTOMATION_TECH_PATTERNS)
    return gtm_score >= 1 and gtm_score > technical_score


def get_bucket_name_for_job(job: Dict) -> str:
    """Return function bucket, with context-aware Automation Specialist routing."""
    matched_role = job.get("_matched_role", "")
    if matched_role == "Automation Specialist" and _automation_is_gtm(job):
        return "gtm_revenue"
    return get_bucket_name(matched_role)


def get_hiring_manager_bucket_for_job(job: Dict) -> str:
    """Return likely buyer hierarchy, with context-aware automation routing."""
    matched_role = job.get("_matched_role", "")
    if matched_role == "Automation Specialist" and _automation_is_gtm(job):
        return "gtm_revenue"
    return get_hiring_manager_bucket_name(matched_role)


def get_target_titles(
    matched_role: str,
    employee_count: int | None = None,
    bucket_override: str | None = None,
) -> List[str]:
    """Return ordered hiring-manager titles for one canonical role."""
    bucket = bucket_override or get_hiring_manager_bucket_name(matched_role)
    titles = list(BUCKET_TITLES.get(bucket, BUCKET_TITLES["gtm_revenue"]))
    return _promote_founders(titles, employee_count)


def _contextual_gtm_titles(job: Dict) -> List[str]:
    text = f"{job.get('job_title') or ''}\n{job.get('job_description') or ''}"[:16000]
    contextual: List[str] = []
    if any(pattern.search(text) for pattern in _GTM_SYSTEMS_PATTERNS):
        contextual.extend(_GTM_SYSTEMS_TITLES)
    if any(pattern.search(text) for pattern in _GTM_SALES_OPS_PATTERNS):
        contextual.extend(_GTM_SALES_OPS_TITLES)
    if any(pattern.search(text) for pattern in _GTM_MARKETING_OPS_PATTERNS):
        contextual.extend(_GTM_MARKETING_OPS_TITLES)
    return contextual


def get_target_titles_for_job(
    job: Dict,
    employee_count: int | None = None,
    bucket_override: str | None = None,
) -> List[str]:
    """Return context-aware hiring-manager titles for a specific posting."""
    matched_role = job.get("_matched_role", "")
    hm_bucket = bucket_override or get_hiring_manager_bucket_for_job(job)
    base = get_target_titles(matched_role, employee_count, bucket_override=hm_bucket)
    if hm_bucket != "gtm_revenue":
        return base

    combined = _dedupe_titles(
        _contextual_gtm_titles(job) + base + _GTM_OPERATIONS_FALLBACK_TITLES
    )
    return _promote_founders(combined, employee_count)


def get_target_titles_for_jobs(
    jobs: Iterable[Dict], employee_count: int | None = None
) -> List[str]:
    """Build one ordered buyer search across all openings in a function bucket.

    This prevents a company with, for example, both Data and Engineering roles
    from losing a relevant leader merely because one posting was selected as the
    primary outbound signal.
    """
    combined: List[str] = []
    for job in jobs:
        combined.extend(get_target_titles_for_job(job, employee_count))
    return _promote_founders(_dedupe_titles(combined), employee_count)
