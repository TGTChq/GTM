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
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

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


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _url_host(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        return (urlparse(raw).hostname or "").lower().lstrip("www.")
    except ValueError:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    host = host.lower().strip(".")
    domain = domain.lower().strip(".")
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def _is_intermediary_host(host: str) -> bool:
    return any(_host_matches(host, value) for value in config.INTERMEDIARY_JOB_DOMAINS)


def _is_generic_host(host: str) -> bool:
    generic_hosts = (
        "railway.app", "herokuapp.com", "web.app", "github.io",
        "wordpress.com", "notion.site", "wixsite.com",
    )
    return any(_host_matches(host, value) for value in generic_hosts)


def _candidate_urls(job: Dict) -> Iterable[Tuple[str, str]]:
    values = [
        job.get("employer_website") or "",
        job.get("job_apply_link") or "",
    ]
    for option in job.get("apply_options") or []:
        if isinstance(option, dict):
            values.append(option.get("apply_link") or "")
    for value in values:
        host = _url_host(value)
        if host:
            yield value, host


def _safe_company_hosts(job: Dict) -> list[str]:
    return [
        host
        for _value, host in _candidate_urls(job)
        if not _is_intermediary_host(host) and not _is_generic_host(host)
    ]


def _domain_core(host: str) -> str:
    labels = [label for label in host.split(".") if label]
    if len(labels) >= 2:
        return labels[-2]
    return labels[0] if labels else ""


def _name_matches_host(name: str, host: str) -> bool:
    name_compact = _compact_name(name)
    domain_compact = _compact_name(_domain_core(host))
    return bool(
        len(name_compact) >= 4
        and len(domain_compact) >= 4
        and (name_compact in domain_compact or domain_compact in name_compact)
    )


def _known_aggregator_name(value: str) -> bool:
    normalized = _normalized_name(value)
    for known in config.KNOWN_JOB_AGGREGATOR_EMPLOYERS:
        candidate = _normalized_name(known)
        if normalized == candidate or (
            len(candidate) >= 6
            and re.search(r"\b" + re.escape(candidate) + r"\b", normalized)
        ):
            return True
    return False


def _generic_publisher_name(value: str) -> bool:
    normalized = _normalized_name(value)
    return any(
        re.search(pattern, normalized, re.I)
        for pattern in config.GENERIC_JOB_PUBLISHER_NAME_PATTERNS
    )


def _clean_employer_candidate(value: str) -> Optional[str]:
    candidate = _clean_space(value).strip(" :;,.|-–—")
    if not 2 <= len(candidate) <= 80 or len(candidate.split()) > 8:
        return None
    normalized = _normalized_name(candidate)
    blocked = {
        "our client", "the client", "client", "company", "the company",
        "employer", "organization", "hiring company", "confidential",
        "undisclosed", "remote jobs", "job board", "reputed company",
    }
    if normalized in blocked or normalized.startswith("our client"):
        return None
    if _known_aggregator_name(candidate) or _generic_publisher_name(candidate):
        return None
    return candidate


def _recover_employer_from_description(job: Dict) -> Tuple[str, str]:
    """Recover a direct employer only from high-confidence description evidence.

    Recovery is attempted solely when JSearch supplied an aggregator/publisher as
    the employer. The extracted name must come from a labelled company field, a
    possessive organization phrase, or a direct self-identification. We retain
    the original value for audit and let the existing staffing/industry gates and
    Apollo organization-name compatibility checks remain authoritative.
    """
    employer = str(job.get("employer_name") or "")
    if not (_known_aggregator_name(employer) or _generic_publisher_name(employer)):
        return "", ""

    description = str(job.get("job_description") or "")[:12000]
    safe_hosts = _safe_company_hosts(job)
    patterns = (
        (
            "description_company_label",
            r"(?im)^\s*(?:company|company name)\s*:\s*"
            r"([A-Z][A-Za-z0-9&.'’() /+-]{1,70}?)(?=\s*(?:\||•|[-–—]{2,}|$))",
        ),
        (
            "description_possessive_organization",
            r"\bwithin\s+([A-Z][A-Za-z0-9&.'’() +-]{1,60}?)[’']s\s+"
            r"(?:[A-Za-z& -]{0,40}\s+)?organization\b",
        ),
        (
            "description_self_identification",
            r"(?m)(?:^|[.!?]\s+)([A-Z][A-Za-z0-9&.'’() +-]{1,60}?)\s+is\s+"
            r"(?:a|an|the)\s+(?:company|platform|business|firm|organization|provider|"
            r"technology|software|finance|healthcare|marketing|consumer|global|leading|"
            r"high-performance)\b",
        ),
        (
            "description_join_company",
            r"\bjoin\s+([A-Z][A-Za-z0-9&.'’() +-]{1,60}?)\s+(?:as|and help|to help)\b",
        ),
    )

    for source, pattern in patterns:
        for match in re.finditer(pattern, description):
            candidate = _clean_employer_candidate(match.group(1))
            if not candidate:
                continue
            occurrences = len(
                re.findall(r"\b" + re.escape(candidate) + r"\b", description, re.I)
            )
            domain_match = any(_name_matches_host(candidate, host) for host in safe_hosts)
            if source == "description_company_label" or domain_match or occurrences >= 2:
                return candidate, source
    return "", ""


def normalize_job_identity(job: Dict) -> Dict:
    """Normalize provider noise and repair only corroborated employer identity.

    Narrow ATS/Careers wrappers can be removed when a non-intermediary company
    domain corroborates the base name. When JSearch supplies a job board as the
    employer, a direct employer may be recovered from high-confidence labelled
    or self-identifying description evidence. Unresolved aggregators remain hard
    rejects, so Apollo never searches for the publisher itself.
    """
    title = _clean_space(job.get("job_title") or "")
    title = re.sub(r"(?:\s*[,|/;-]\s*){2,}$", "", title).strip(" ,|/;-")
    job["job_title"] = title

    employer = _clean_space(job.get("employer_name") or "")
    publisher = _clean_space(job.get("job_publisher") or "")
    original_employer = employer

    ats_match = re.fullmatch(r"(.+?)\s+(?:ats|applicant tracking system)", employer, re.I)
    if ats_match:
        base_name = _clean_space(ats_match.group(1))
        if publisher and _compact_name(publisher) == _compact_name(base_name):
            employer = publisher
        else:
            employer = base_name
        job["_employer_name_normalization"] = "removed_ats_wrapper"

    wrapper_match = re.fullmatch(r"(.+?)\s+careers?", employer, re.I)
    if wrapper_match and not _known_aggregator_name(employer):
        base_name = _clean_employer_candidate(wrapper_match.group(1))
        description = str(job.get("job_description") or "")[:5000]
        base_mentioned = bool(
            base_name
            and re.search(r"\b" + re.escape(base_name) + r"\b", description, re.I)
        )
        if (
            base_name
            and base_mentioned
            and any(_name_matches_host(base_name, host) for host in _safe_company_hosts(job))
        ):
            employer = base_name
            job["_employer_name_normalization"] = "removed_careers_wrapper"

    job["employer_name"] = employer
    recovered, source = _recover_employer_from_description(job)
    if recovered:
        job["_original_employer_name"] = original_employer
        job["employer_name"] = recovered
        job["_employer_name_normalization"] = source
        job["_employer_identity_repaired"] = True
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
        "co_op": r"\bco[- ]?op(?:erative education)?\b",
        "academic_work_term": r"\b(?:student|academic|semester|summer|winter) work term\b",
        "student_placement": r"\b(?:student|semester|academic) placement\b",
        "volunteer": r"\bvolunteer (?:role|position|opportunity)\b",
    }
    for reason, pattern in program_patterns.items():
        if re.search(pattern, title, re.I) or re.search(
            rf"\b(?:this is|join|apply for|seeking|hiring for)\b[^.\n]{{0,80}}{pattern}",
            description_head,
            re.I,
        ):
            return QualityAssessment(False, "excluded_restricted_role", reason)

    # Some providers strip "Co-op" from the title while retaining the academic
    # work-term language in the body. Require corroborating academic evidence so
    # ordinary mentions of collaboration or training do not become false positives.
    academic_program = re.search(
        r"\b(?:co[- ]?op|cooperative education|work term|semester placement|student placement)\b",
        description_head,
        re.I,
    )
    academic_corroboration = re.search(
        r"\b(?:currently enrolled|return(?:ing)? to (?:school|university|college)|"
        r"four[- ]month|4[- ]month|semester|academic credit|student status)\b",
        description_head,
        re.I,
    )
    if academic_program and academic_corroboration:
        return QualityAssessment(
            False, "excluded_restricted_role", "academic_coop_or_work_term"
        )

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

    federal_agency = (
        r"(?:department of veterans affairs|veterans health administration|"
        r"department of defense|department of homeland security|"
        r"department of health and human services|centers for medicare (?:and|&) medicaid services|"
        r"federal agency|federal government|government agency)"
    )
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
        "named_federal_agency_delivery": (
            rf"\b(?:support(?:ing|s|ed)?|work(?:ing|s|ed)?|deliver(?:ing|s|ed)?|"
            rf"provide(?:s|d|ing)?|project|contract|engagement|program)\b"
            rf"[^.\n]{{0,180}}\b(?:with |for |to |on behalf of )?(?:the )?{federal_agency}\b"
        ),
        "named_federal_agency_reverse_delivery": (
            rf"\b(?:the )?{federal_agency}\b[^.\n]{{0,180}}\b"
            rf"(?:project|contract|engagement|program|support(?:ing|s|ed)?|deliver(?:ing|s|ed)?)\b"
        ),
        # VA is ambiguous with Virginia, so require uppercase plus direct project/
        # contract language in the same short clause.
        "va_project_delivery": (
            r"\b(?:project|contract|engagement|program|support(?:ing|s|ed)?)\b"
            r"[^.\n]{0,80}\b(?:with |for |to )?(?:the )?(?-i:VA)\b"
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
    description = str(job.get("job_description") or "")[:7000]

    for known in config.KNOWN_OUTSOURCING_EMPLOYERS:
        normalized = re.sub(r"[^a-z0-9]+", " ", known.lower()).strip()
        if employer == normalized or re.search(r"\b" + re.escape(normalized) + r"\b", employer):
            return QualityAssessment(False, "excluded_outsourcing", f"known_outsourcing_employer:{known}")

    # A company whose own name is a call/contact center is the service provider,
    # not the direct employer signal TGTC wants to pursue. This also catches thin
    # postings whose body contains only generic hiring language.
    if re.search(r"\b(?:call|contact) center\b|\bbpo\b|\boutsourc(?:ing|ed)\b", employer, re.I):
        return QualityAssessment(
            False, "excluded_outsourcing", "outsourcing_service_model_in_employer"
        )

    for pattern in config.OUTSOURCING_DESCRIPTION_PATTERNS:
        if re.search(pattern, description, re.I):
            return QualityAssessment(False, "excluded_outsourcing", f"outsourcing_business_model:{pattern}")

    # Corroborated service-model evidence catches euphemisms without rejecting an
    # ordinary internal customer-support team merely because it mentions clients.
    service_signal = re.search(
        r"\b(?:call center|contact center|customer support outsourcing|"
        r"customer service outsourcing|outsourced customer support|"
        r"outsourced customer service|business process outsourcing|bpo|"
        r"managed customer service|outsourced support)\b",
        description,
        re.I,
    )
    client_delivery_signal = re.search(
        r"\b(?:for (?:our|multiple) clients?|client accounts?|client campaigns?|"
        r"assigned to (?:a|our) client|serve (?:our|multiple) clients?)\b",
        description,
        re.I,
    )
    if service_signal and client_delivery_signal:
        return QualityAssessment(
            False, "excluded_outsourcing", "corroborated_outsourcing_service_model"
        )
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
