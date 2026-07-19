"""High-precision, zero-credit quality guards for job-intent discovery.

The rules in this module target failure *families*, not individual postings:
posting integrity, non-standard work programs, restricted/government work,
outsourcing intermediaries, and context collisions between similarly named roles.
All checks run before Apollo/Hunter so bad inventory cannot consume credits.
"""

from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass
from typing import Dict

import config


@dataclass(frozen=True)
class QualityAssessment:
    eligible: bool
    stat_name: str = ""
    reason: str = ""


def _text(job: Dict, limit: int = 12000) -> str:
    return "\n".join(
        [
            str(job.get("job_title") or ""),
            str(job.get("employer_name") or ""),
            str(job.get("job_description") or "")[:limit],
        ]
    )


def _clean_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_job_identity(job: Dict) -> Dict:
    """Normalize provider noise without inventing an employer or location.

    A publisher can safely repair an employer label only for the narrow ATS
    wrapper pattern (for example ``Travelopia Group ATS`` -> ``Travelopia
    Group``). Publisher/board domains remain blocked by the existing company
    identity guard.
    """
    title = _clean_space(job.get("job_title") or "")
    title = re.sub(r"(?:\s*[,|/;-]\s*){2,}$", "", title).strip(" ,|/;-")
    job["job_title"] = title

    employer = _clean_space(job.get("employer_name") or "")
    publisher = _clean_space(job.get("job_publisher") or "")
    ats_match = re.fullmatch(r"(.+?)\s+(?:ats|applicant tracking system)", employer, re.I)
    if ats_match:
        base_name = _clean_space(ats_match.group(1))
        if publisher and re.sub(r"[^a-z0-9]+", "", publisher.lower()) == re.sub(
            r"[^a-z0-9]+", "", base_name.lower()
        ):
            employer = publisher
        else:
            employer = base_name
        job["_employer_name_normalization"] = "removed_ats_wrapper"
    job["employer_name"] = employer
    return job


def assess_posting_integrity(job: Dict) -> QualityAssessment:
    title = str(job.get("job_title") or "")
    employer = str(job.get("employer_name") or "")
    description = str(job.get("job_description") or "")[:16000]

    generic_employer = re.fullmatch(
        r"\s*(?:confidential|undisclosed|anonymous|client of .+|stealth(?: startup)?|hiring company)\s*",
        employer,
        re.I,
    )
    if generic_employer and not job.get("employer_website"):
        return QualityAssessment(False, "excluded_posting_integrity", "unresolvable_generic_employer")

    # Multi-job roundup pages are not a single active employer-role signal.
    company_labels = len(re.findall(r"\bcompany\s*:\s*\S", description, re.I))
    location_labels = len(re.findall(r"\blocation\s*:\s*\S", description, re.I))
    title_labels = len(re.findall(r"\b(?:job )?title\s*:\s*\S", description, re.I))
    if max(company_labels, location_labels, title_labels) >= 3:
        return QualityAssessment(False, "excluded_posting_integrity", "multi_job_roundup_page")

    if re.search(r"\b(?:multiple|several) (?:open roles|positions|job openings)\b", title, re.I):
        return QualityAssessment(False, "excluded_posting_integrity", "multi_role_posting")

    if len(title) < 4 or re.fullmatch(r"[\W_]+", title):
        return QualityAssessment(False, "excluded_posting_integrity", "malformed_job_title")

    deadline = re.search(
        r"\bapplication deadline\s*:\s*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*,?\s*"
        r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\b",
        description,
        re.I,
    )
    if deadline:
        try:
            parsed = datetime.strptime(
                f"{deadline.group(1)} {deadline.group(2)} {deadline.group(3)}",
                "%B %d %Y",
            )
        except ValueError:
            try:
                parsed = datetime.strptime(
                    f"{deadline.group(1)} {deadline.group(2)} {deadline.group(3)}",
                    "%b %d %Y",
                )
            except ValueError:
                parsed = None
        if parsed and parsed.date() < datetime.now().date():
            return QualityAssessment(False, "excluded_posting_integrity", "embedded_application_deadline_expired")

    return QualityAssessment(True)


def assess_restricted_work(job: Dict) -> QualityAssessment:
    text = _text(job)

    title = str(job.get("job_title") or "")
    description_head = str(job.get("job_description") or "")[:2500]
    program_patterns = {
        "skillbridge_or_transition_program": r"\b(?:skillbridge|military spouse fellowship|career transition program)\b",
        "externship": r"\bextern(?:ship)?\b",
        "apprenticeship": r"\bapprentice(?:ship)?\b",
        "fellowship": r"\bfellow(?:ship)?\b",
        "returnship": r"\breturnship\b",
        "residency_program": r"\b(?:career|professional|graduate) residency\b",
        "internship": r"\bintern(?:ship)?\b",
        "volunteer": r"\bvolunteer (?:role|position|opportunity)\b",
    }
    for reason, pattern in program_patterns.items():
        if re.search(pattern, title, re.I) or re.search(
            rf"\b(?:this is|join|apply for|seeking|hiring for)\b[^.\n]{{0,80}}{pattern}",
            description_head,
            re.I,
        ):
            return QualityAssessment(False, "excluded_restricted_role", reason)

    matched_role = str(job.get("_matched_role") or "").strip()
    if matched_role:
        role_pattern = r"\s+".join(re.escape(token) for token in matched_role.split())
        if re.search(
            rf"\b(?:the|role\s*:|position\s*:)\s+senior\s+{role_pattern}\b",
            description_head,
            re.I,
        ):
            return QualityAssessment(False, "excluded_restricted_role", "hidden_senior_role")

    clearance_patterns = {
        "top_secret_clearance": r"\b(?:top secret|ts/sci|ts sci)\b",
        "security_clearance_required": r"\b(?:active |current )?(?:secret|security) clearance (?:is )?(?:required|needed|mandatory)\b",
        "public_trust_required": r"\bpublic trust(?: clearance)?\b",
        "polygraph_required": r"\b(?:ci|full scope|counterintelligence) polygraph\b",
        "cleared_role": r"\bwith security clearance\b",
    }
    for reason, pattern in clearance_patterns.items():
        if re.search(pattern, text, re.I):
            return QualityAssessment(False, "excluded_restricted_role", reason)

    government_service_patterns = {
        "role_supports_federal_agency": (
            r"\b(?:role|position|team|business unit|engineer|recruiter|portfolio|initiative)\b"
            r"[^.\n]{0,180}\b(?:supports?|supporting|serves?|for) (?:a |the )?"
            r"(?:federal (?:agency|government|client|programs?|environment)|"
            r"federal government (?:programs?|agencies|healthcare technology programs?))\b"
        ),
        "direct_federal_delivery": (
            r"\b(?:support(?:ing|s|ed)?|deliver(?:ing|s|ed)?|build(?:ing|s|t)?|provide(?:s|d|ing)?)\b"
            r"[^.\n]{0,180}\b(?:for |to )?(?:a |the )?"
            r"(?:federal agency|federal government|federal government programs?|government agencies)\b"
        ),
        "dhs_or_federal_clearance": r"\b(?:dhs|dod|federal) clearance\b",
        "public_sector_government_role": r"\b(?:pubsec|public sector)\b[^.\n]{0,80}\b(?:gov|government|state & local|federal)\b",
        "federal_contract_delivery": r"\b(?:deliver|build|provide|support)\b[^.\n]{0,100}\b(?:federal agencies|government agencies|public-sector service delivery)\b",
        "government_technology_services": r"\btransform(?:ing)? (?:their|the) experience of government\b",
    }
    for reason, pattern in government_service_patterns.items():
        if re.search(pattern, text, re.I):
            return QualityAssessment(False, "excluded_restricted_role", reason)

    return QualityAssessment(True)


def assess_outsourcing_intermediary(job: Dict) -> QualityAssessment:
    employer = re.sub(r"[^a-z0-9]+", " ", str(job.get("employer_name") or "").lower()).strip()
    description = str(job.get("job_description") or "")[:5000]

    for known in config.KNOWN_OUTSOURCING_EMPLOYERS:
        normalized = re.sub(r"[^a-z0-9]+", " ", known.lower()).strip()
        if employer == normalized or re.search(r"\b" + re.escape(normalized) + r"\b", employer):
            return QualityAssessment(False, "excluded_outsourcing", f"known_outsourcing_employer:{known}")

    for pattern in config.OUTSOURCING_DESCRIPTION_PATTERNS:
        if re.search(pattern, description, re.I):
            return QualityAssessment(False, "excluded_outsourcing", f"outsourcing_business_model:{pattern}")
    return QualityAssessment(True)


def assess_contextual_role_fit(job: Dict) -> QualityAssessment:
    matched_role = str(job.get("_matched_role") or "")
    title = str(job.get("job_title") or "")
    description = str(job.get("job_description") or "")[:9000]
    text = f"{title}\n{description}"

    if matched_role == "Account Executive":
        pr_title = re.search(
            r"\b(?:pr|public relations|media relations|communications?)\s+account executive\b|"
            r"\baccount executive\s*[-–—,:/]\s*(?:pr|public relations|media relations)\b",
            title,
            re.I,
        )
        sales_evidence = re.search(
            r"\b(?:quota|pipeline|prospecting|new business|close deals|sales cycle|revenue target|book meetings)\b",
            text,
            re.I,
        )
        if pr_title and not sales_evidence:
            return QualityAssessment(False, "excluded_contextual_mismatch", "public_relations_account_executive_not_sales")

    if matched_role == "Product Support Specialist":
        operations_context = re.search(
            r"\b(?:inventory optimization|inventory control|warehouse|supply chain|merchandising|catalog maintenance|product data)\b",
            text,
            re.I,
        )
        customer_support_context = re.search(
            r"\b(?:customer|client|user|ticket|troubleshoot|technical support|support queue|product education)\b",
            text,
            re.I,
        )
        if operations_context and not customer_support_context:
            return QualityAssessment(False, "excluded_contextual_mismatch", "inventory_or_catalog_role_not_product_support")

    if matched_role in {"Revenue Operations Analyst", "Sales Operations Analyst"}:
        finance_only = re.search(r"\b(?:billing|accounts receivable|collections|invoice processing)\b", title, re.I)
        revops_evidence = re.search(r"\b(?:crm|salesforce|pipeline|forecast|sales operations|revenue operations|gtm)\b", text, re.I)
        if finance_only and not revops_evidence:
            return QualityAssessment(False, "excluded_contextual_mismatch", "finance_operations_not_revops")

    if matched_role == "Video Editor":
        video_evidence = re.search(
            r"\b(?:video|footage|premiere pro|after effects|davinci|final cut|post-production|motion graphics|audio sync)\b",
            description,
            re.I,
        )
        graphic_only = re.search(
            r"\b(?:logos?|brand identit|photoshop|illustrator|indesign|still designs?|typography)\b",
            description,
            re.I,
        )
        if graphic_only and not video_evidence:
            return QualityAssessment(False, "excluded_contextual_mismatch", "graphic_design_role_mislabeled_as_video_editor")

    return QualityAssessment(True)


def assess_quality_guard(job: Dict) -> QualityAssessment:
    normalize_job_identity(job)
    for check in (
        assess_posting_integrity,
        assess_restricted_work,
        assess_outsourcing_intermediary,
        assess_contextual_role_fit,
    ):
        result = check(job)
        if not result.eligible:
            return result
    return QualityAssessment(True)
