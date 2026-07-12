"""Maps target roles to the most likely functional hiring managers.

Automation Specialist is routed dynamically because the same title can describe
very different jobs:
- GTM/revenue automation -> CRO / RevOps leadership
- technical/AI automation -> CTO / Engineering leadership
"""

from __future__ import annotations

import re
from typing import Dict, List

ROLE_TO_BUCKET: Dict[str, str] = {
    "GTM Engineer": "gtm_revenue",
    "AI Engineer": "engineering",
    "Automation Specialist": "engineering",  # default; job-aware routing may override
    "Graphic Designer": "marketing",
    "Video Editor": "marketing",
    "Performance Marketing Manager": "marketing",
    "Customer Success Manager": "customer_success",
    "Customer Support": "customer_support",
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
        "COO",
        "Chief Operating Officer",
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

_GTM_OPERATIONS_FALLBACK_TITLES = [
    "COO",
    "Chief Operating Officer",
]

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


def _dedupe_titles(titles: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for title in titles:
        key = title.lower().strip()
        if key not in seen:
            seen.add(key)
            result.append(title)
    return result


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


def get_bucket_name(matched_role: str) -> str:
    """Static bucket lookup; use get_bucket_name_for_job when a JD is available."""
    return ROLE_TO_BUCKET.get(matched_role, "gtm_revenue")


def get_bucket_name_for_job(job: Dict) -> str:
    """Route an Automation Specialist based on explicit JD signals."""
    matched_role = job.get("_matched_role", "")
    if matched_role != "Automation Specialist":
        return get_bucket_name(matched_role)

    text = f"{job.get('job_title') or ''}\n{job.get('job_description') or ''}"[:16000]
    gtm_score = sum(bool(pattern.search(text)) for pattern in _AUTOMATION_GTM_PATTERNS)
    technical_score = sum(bool(pattern.search(text)) for pattern in _AUTOMATION_TECH_PATTERNS)

    # A single explicit GTM signal is enough when no stronger technical signal
    # exists; otherwise technical/engineering remains the conservative default.
    if gtm_score >= 1 and gtm_score > technical_score:
        return "gtm_revenue"
    return "engineering"


def get_target_titles(
    matched_role: str,
    employee_count: int | None = None,
    bucket_override: str | None = None,
) -> List[str]:
    """Return ordered titles; founder titles are promoted for small companies."""
    bucket = bucket_override or get_bucket_name(matched_role)
    titles = list(BUCKET_TITLES[bucket])
    if employee_count is not None and employee_count < 75:
        founder_titles = ["Founder", "Co-Founder", "CEO"]
        titles = founder_titles + [title for title in titles if title not in founder_titles]
    return titles

def get_target_titles_for_job(
    job: Dict,
    employee_count: int | None = None,
    bucket_override: str | None = None,
) -> List[str]:
    """Return context-aware hiring-manager titles for a specific posting."""
    matched_role = job.get("_matched_role", "")
    bucket = bucket_override or get_bucket_name_for_job(job)
    base = get_target_titles(matched_role, employee_count, bucket_override=bucket)
    if bucket != "gtm_revenue":
        return base

    text = f"{job.get('job_title') or ''}\n{job.get('job_description') or ''}"[:16000]
    contextual: List[str] = []
    if any(pattern.search(text) for pattern in _GTM_SYSTEMS_PATTERNS):
        contextual.extend(_GTM_SYSTEMS_TITLES)
    if any(pattern.search(text) for pattern in _GTM_SALES_OPS_PATTERNS):
        contextual.extend(_GTM_SALES_OPS_TITLES)
    if any(pattern.search(text) for pattern in _GTM_MARKETING_OPS_PATTERNS):
        contextual.extend(_GTM_MARKETING_OPS_TITLES)

    # Operations leadership is a last-resort functional owner, not the first
    # choice. It is appended after Brett's preferred CRO/RevOps/GTM titles.
    combined = _dedupe_titles(contextual + base + _GTM_OPERATIONS_FALLBACK_TITLES)
    if employee_count is not None and employee_count < 75:
        founder_titles = ["Founder", "Co-Founder", "CEO"]
        combined = founder_titles + [title for title in combined if title not in founder_titles]
    return combined

