"""Step 3: enrich companies and identify one decision-maker per company/bucket."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import apollo_client as apollo
import config
import domain_resolution
import hunter_client as hunter
from account_gate import AccountGate
from contact_gate import ContactGate
from decision_engine import annotate_final_decision
from decision_types import GateDecision, GateState, gate_decision_from_dict
from email_gate import EmailGate
from evidence_types import EvidenceBundle
from reason_codes import ReasonCode
from reroute_state import RerouteRegistry
from company_identity import (
    canonical_candidate_key,
    company_names_compatible,
    domains_equivalent,
    email_matches_company,
    is_intermediary_domain,
    safe_company_domain,
)
from company_display_resolver import resolve_company_display
from job_filter import (
    extract_domain,
    get_safe_employer_domain,
    is_in_crm,
    load_crm_companies,
    normalize_text,
)
from job_signal import annotate_job
from role_focus import extract_role_focus
from role_display_resolver import resolve_role_display
from role_mapping import (
    founder_allowed_for_employee_count,
    get_bucket_name_for_job,
    get_broadened_target_titles_for_jobs,
    get_hiring_manager_bucket_for_job,
    get_target_titles_for_jobs,
    is_founder_tier_title,
)
from review_policy import is_airtable_reviewable

logger = logging.getLogger(__name__)


@dataclass
class Step3Result:
    output_path: str
    total_input_jobs: int
    total_output_leads: int
    company_criteria_excluded: int
    hiring_manager_found: int
    hiring_manager_not_found: int
    match_rate: float
    contactable_hiring_managers: int
    uncontactable_hiring_managers: int
    contactable_rate: float
    companies_considered: int = 0
    eligible_companies: int = 0
    icp_pass_companies: int = 0
    lead_capable_companies: int = 0
    company_criteria_excluded_companies: int = 0
    target_eligible_companies: Optional[int] = None
    target_reviewable_leads: Optional[int] = None
    reviewable_leads: int = 0
    reviewable_target_reached: bool = True
    final_pass_target: Optional[int] = None
    final_pass_leads: int = 0
    needs_check_leads: int = 0
    reroute_leads: int = 0
    unverified_leads: int = 0
    rejected_leads: int = 0
    final_pass_target_reached: bool = False
    max_eligible_companies: Optional[int] = None
    eligible_company_limit_reached: bool = False
    target_reached: bool = True
    stop_reason: str = "candidate_pool_exhausted"
    processed_company_keys: List[str] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)
    #: Non-PII hiring-manager + multi-function observability summaries, surfaced
    #: to the operator run summary (see hm_observability.write_run_artifacts).
    hm_observability: Dict = field(default_factory=dict)
    success: bool = True
    errors: List[str] = field(default_factory=list)


def validate_preflight() -> None:
    if not config.APOLLO_API_KEY:
        raise ValueError("APOLLO_API_KEY is missing from .env")
    if config.APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET < 1:
        raise ValueError("APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET must be at least 1")
    if config.HUNTER_MAX_FALLBACK_ATTEMPTS_PER_BUCKET < 0:
        raise ValueError("HUNTER_MAX_FALLBACK_ATTEMPTS_PER_BUCKET cannot be negative")
    if config.VERIFY_WITH_HUNTER and not config.HUNTER_API_KEY:
        logger.warning("HUNTER_API_KEY is missing; Hunter verification/fallback is disabled")


_CRM_EXCLUSION_CACHE: Optional[Tuple[set, set]] = None


def _crm_exclusion_sets() -> Tuple[set, set]:
    """Load the company-level CRM / active-pipeline exclusion list once per run.

    Delegates to the single canonical loader (``job_filter.load_crm_companies``)
    so the orchestrator path applies exactly the same company normalization and
    the same production safety policy as the Step 2 path: in PRODUCTION a
    missing, unreadable, empty, or zero-company exclusion file raises instead of
    silently disabling the exclusion. There is deliberately no second CRM
    matching implementation.
    """
    global _CRM_EXCLUSION_CACHE
    if _CRM_EXCLUSION_CACHE is None:
        _CRM_EXCLUSION_CACHE = load_crm_companies(config.CRM_EXCLUSION_FILE)
    return _CRM_EXCLUSION_CACHE


def reset_crm_exclusion_cache() -> None:
    """Drop the cached CRM sets so a changed file or config path is re-read."""
    global _CRM_EXCLUSION_CACHE
    _CRM_EXCLUSION_CACHE = None


def _is_intermediary_domain(domain: str) -> bool:
    return is_intermediary_domain(domain, config.INTERMEDIARY_JOB_DOMAINS)


def _domain_from_apply_link(job: Dict) -> str:
    """Recover a company domain only from safe, direct application URLs."""
    apply_link = (job.get("job_apply_link") or "").strip()
    direct_flag = job.get("job_apply_is_direct")
    if not apply_link or direct_flag is False:
        return ""
    if direct_flag is not True:
        try:
            host = urlparse(apply_link).netloc.lower().split(":", 1)[0]
        except Exception:
            return ""
        if not host.startswith(("careers.", "jobs.", "apply.")):
            return ""

    candidates = [apply_link]
    try:
        parsed = urlparse(apply_link)
        query = parse_qs(parsed.query)
        for key in ("url", "redirect", "redirect_url", "target", "u"):
            candidates.extend(unquote(value) for value in query.get(key, []))
    except Exception:
        pass

    for candidate in candidates:
        domain = extract_domain(candidate)
        if domain and not _is_intermediary_domain(domain):
            for prefix in ("jobs.", "careers.", "apply."):
                if domain.startswith(prefix) and domain.count(".") >= 2:
                    domain = domain[len(prefix):]
                    break
            return domain
    return ""


def _best_input_domain(job: Dict) -> str:
    annotated = safe_company_domain(
        job.get("_employer_domain_input") or "",
        config.INTERMEDIARY_JOB_DOMAINS,
    )
    if annotated:
        return annotated
    return get_safe_employer_domain(job)[0] or _domain_from_apply_link(job)


def company_key_for_job(job: Dict) -> str:
    """Return the stable domain-or-name key used for company-level enrichment."""
    return _best_input_domain(job) or normalize_text(job.get("employer_name") or "unknown")


def _name_matches_blocklist(name: str, values: List[str]) -> Optional[str]:
    normalized = normalize_text(name or "")
    if not normalized:
        return None
    for value in values:
        candidate = normalize_text(value)
        if normalized == candidate or re.search(r"\b" + re.escape(candidate) + r"\b", normalized):
            return value
    return None


def _reason_family(reason: str) -> str:
    """Collapse detailed company decisions into stable observable families."""
    value = str(reason or "unknown").strip().lower()
    value = value.split(":", 1)[0]
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value or "unknown"


def passes_company_criteria(
    org: apollo.OrgEnrichment, company_name: str = ""
) -> Tuple[bool, str, bool]:
    """Return (eligible, reason, needs_manual_review)."""
    resolved_name = org.name or company_name
    if blocked := _name_matches_blocklist(
        resolved_name, [*config.KNOWN_STAFFING_EMPLOYERS, *config.KNOWN_JOB_AGGREGATOR_EMPLOYERS]
    ):
        return False, f"excluded_intermediary_company:{blocked}", False

    if not org.found:
        if config.REJECT_UNKNOWN_FIRMOGRAPHICS:
            return False, "rejected_no_org_data", False
        return True, "unknown_org_data", True

    industry = normalize_text(org.industry or "")
    if industry and any(normalize_text(keyword) in industry for keyword in config.APOLLO_EXCLUDED_INDUSTRY_KEYWORDS):
        return False, f"excluded_apollo_industry:{org.industry}", False

    if org.employee_count is None:
        if config.REJECT_UNKNOWN_FIRMOGRAPHICS:
            return False, "rejected_unknown_employee_count", False
        return True, "unknown_employee_count", True

    if org.employee_count < config.MIN_EMPLOYEES:
        return False, f"too_small:{org.employee_count}", False
    if org.employee_count > config.MAX_EMPLOYEES:
        return False, f"too_large:{org.employee_count}", False

    # Founding year is intentionally NEUTRAL for qualification in the definitive
    # simplified ICP: it never rejects and never changes the qualification state
    # (known-new, known-old and unknown are all treated identically). It is still
    # enriched and persisted elsewhere for display.
    return True, "passes", False


def _title_priority(title: str, target_titles: List[str]) -> Tuple[int, int]:
    normalized = normalize_text(title)
    if not normalized:
        return len(target_titles) + 100, 0
    for index, target in enumerate(target_titles):
        target_norm = normalize_text(target)
        if normalized == target_norm:
            return index, 3
        if re.search(r"\b" + re.escape(target_norm) + r"\b", normalized):
            return index, 2
        if target_norm in normalized or normalized in target_norm:
            return index, 1
    return len(target_titles) + 10, 0


def rank_candidates(people: List[Dict], target_titles: List[str]) -> List[Dict]:
    """Return only title-matched candidates in deterministic priority order."""
    ranked = sorted(
        people or [],
        key=lambda person: (
            _title_priority(person.get("title") or "", target_titles)[0],
            -_title_priority(person.get("title") or "", target_titles)[1],
            not bool(person.get("linkedin_url")),
            str(person.get("id") or person.get("person_id") or ""),
        ),
    )
    return [
        person
        for person in ranked
        if _title_priority(person.get("title") or "", target_titles)[1] > 0
    ]


def pick_best_candidate(people: List[Dict], target_titles: List[str]) -> Optional[Dict]:
    ranked = rank_candidates(people, target_titles)
    return ranked[0] if ranked else None


def _hm_second_pass(bucket_jobs, org, search_domain, target_titles,
                    ranked_candidates, people, stats):
    """Quality-preserving HM recovery (config.HM_SECOND_PASS_TITLE_BROADENING). When
    the first people search yields no title-matched candidate, run at most ONE more
    search broadened WITHIN THE SAME function to adjacent senior decision-makers
    (SVP/EVP/generalist leaders). Returns possibly-updated (titles, ranked, people).
    The recovered candidates still flow through the identical downstream gates."""
    if ranked_candidates or not config.HM_SECOND_PASS_TITLE_BROADENING:
        return target_titles, ranked_candidates, people
    broadened = get_broadened_target_titles_for_jobs(bucket_jobs, org.employee_count)
    if not broadened or broadened == target_titles:
        return target_titles, ranked_candidates, people
    stats["hm_second_pass_attempts"] += 1
    try:
        people2 = apollo.search_people_at_company(search_domain, broadened)
    except apollo.GLOBAL_FATAL_ERRORS:
        raise
    except Exception:  # noqa: BLE001 - a second-pass failure never worsens the miss
        return target_titles, ranked_candidates, people
    time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
    ranked2 = rank_candidates(people2, broadened)
    if ranked2:
        stats["hm_second_pass_recovered"] += 1
        return broadened, ranked2, people2
    return target_titles, ranked_candidates, people


def _organization_domains(org: apollo.OrgEnrichment) -> set[str]:
    values = [org.domain]
    raw = org.raw or {}
    values.extend([
        raw.get("primary_domain"),
        raw.get("domain"),
        raw.get("website_url"),
    ])
    return {
        domain
        for value in values
        if (domain := safe_company_domain(value, config.INTERMEDIARY_JOB_DOMAINS))
    }


def _person_belongs_to_company(
    person: apollo.PersonMatch,
    company_domains: set[str],
    company_name: str,
) -> bool:
    person_domain = safe_company_domain(
        person.organization_domain or "", config.INTERMEDIARY_JOB_DOMAINS
    )
    if person_domain:
        return any(domains_equivalent(person_domain, domain) for domain in company_domains)
    if person.organization_name:
        return company_names_compatible(company_name, person.organization_name)
    # Apollo may omit current-organization identity from a person match. The
    # original people search was domain-constrained, and a usable email must
    # still pass strict company-domain validation before the lead is reviewable.
    return True


def _selection_tier(title: str | None) -> str:
    normalized = normalize_text(title or "")
    if re.search(
        r"\b(?:founder|co founder|ceo|chief executive officer|owner|president)\b",
        normalized,
    ):
        return "founder_fallback"
    if any(token in normalized for token in ("manager", "director", "head")):
        return "direct_functional_leader"
    return "functional_executive"


def _email_confidence(
    person: apollo.PersonMatch,
    hunter_result: Optional[hunter.HunterResult],
) -> str:
    if not person.email:
        return "none"

    hunter_status = (hunter_result.status if hunter_result else "") or ""
    hunter_status = hunter_status.lower()
    apollo_status = (person.email_status or "").lower()

    if hunter_status in {"invalid", "disposable"}:
        return "invalid"
    if hunter_status == "valid":
        return "high" if apollo_status == "verified" else "medium"
    if hunter_status in {"accept_all", "webmail", "risky"}:
        return "medium" if apollo_status == "verified" else "low"
    if apollo_status == "verified":
        return "medium"
    return "low"


def _primary_job(jobs: List[Dict]) -> Dict:
    def sort_key(job: Dict) -> Tuple[int, str]:
        score = int(job.get("_role_relevance_score") or 0)
        posted = str(job.get("job_posted_at_datetime_utc") or job.get("job_posted_at_timestamp") or "")
        return score, posted

    return max(jobs, key=sort_key)


def _lead_key(domain: str, email: str, bucket: str) -> str:
    return f"{domain.lower()}|{email.lower()}|{bucket}"


def _candidate_identity_key(candidate: Dict) -> str:
    """Candidate-attempt tracking key, extended beyond a bare Apollo person
    ID (identity-key audit, FINAL_30_PLUS_SYSTEM_SPEC.md section 18): a
    candidate with a LinkedIn URL or email but no provider ID was previously
    never tracked as "attempted" at all (every attempted_id_reasons/
    attempted_ids write site guarded on a non-empty raw id), so it could be
    re-selected and re-attempted every reroute round indefinitely.

    The provider-person-ID tier is deliberately kept as the bare, unprefixed
    ID -- not canonical_candidate_key()'s "pid:{id}" form -- to preserve
    RerouteRegistry's existing persisted key shape exactly (it has always
    been keyed by raw provider ID); only the new LinkedIn/email fallback
    tiers use canonical_candidate_key()'s prefixed form, since those cases
    were never tracked at all before and so have no legacy shape to break.
    A truly unresolved candidate (no id, no LinkedIn, no email) still yields
    "" here rather than a one-off random sentinel, since a value that can
    never again match on a future lookup is not worth persisting --
    preserves the existing not-tracked behavior for that narrow case exactly.
    """
    provider_id = str(candidate.get("id") or candidate.get("person_id") or "").strip()
    if provider_id:
        return provider_id
    key = canonical_candidate_key(
        linkedin_url=candidate.get("linkedin_url"),
        email=candidate.get("email"),
    )
    return "" if key.startswith("unresolved:") else key


def _build_no_contact_lead(
    primary: Dict,
    bucket_jobs: List[Dict],
    bucket: str,
    org: apollo.OrgEnrichment,
    company_reason: str,
    company_needs_review: bool,
    status: str,
    reason: str,
) -> Dict:
    role_focus = extract_role_focus(primary, primary.get("_matched_role", ""))
    lead = dict(primary)
    lead.update(
        {
            "_role_bucket": bucket,
            "_hiring_manager_buckets": sorted({
                get_hiring_manager_bucket_for_job(job) for job in bucket_jobs
            }),
            "_step3_status": status,
            "_step3_reason": reason,
            "_company_criteria_reason": company_reason,
            "_company_needs_review": company_needs_review,
            "related_open_roles": sorted({j.get("job_title", "") for j in bucket_jobs if j.get("job_title")}),
            "related_job_ids": [j.get("job_id") for j in bucket_jobs if j.get("job_id")],
            "role_focus": role_focus.text,
            "role_focus_quality": role_focus.quality,
            "role_focus_evidence": role_focus.evidence,
            "company_employee_count": org.employee_count,
            "company_founded_year": org.founded_year,
            "company_industry": org.industry,
            "company_domain": safe_company_domain(
                org.domain or _best_input_domain(primary),
                config.INTERMEDIARY_JOB_DOMAINS,
            ),
            "hiring_manager_confidence": "none",
        }
    )
    return lead


def _process_company_legacy(company_jobs: List[Dict]) -> Tuple[List[Dict], Dict]:
    stats = defaultdict(int)
    first = company_jobs[0]
    input_domain = _best_input_domain(first)
    company_name = first.get("employer_name") or ""
    # Never pass a bare company name or a noisy subdomain as Apollo's website.
    # Apollo can still resolve by company name when no valid domain is available.
    enrichment_website = f"https://{input_domain}" if input_domain else ""

    org = apollo.enrich_organization(
        domain=input_domain, name=company_name, website=enrichment_website
    )
    time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
    search_domain = safe_company_domain(
        org.domain or input_domain, config.INTERMEDIARY_JOB_DOMAINS
    )
    if input_domain:
        stats["company_domain_from_first_party_signal"] += 1
    elif search_domain:
        stats["company_domain_resolved_by_name"] += 1
    else:
        stats["company_domain_unresolved"] += 1

    eligible, company_reason, company_needs_review = passes_company_criteria(org, company_name)
    reason_family = _reason_family(company_reason)
    stats[f"company_criteria_reason__{reason_family}"] += 1
    if company_needs_review:
        stats[f"company_manual_review_reason__{reason_family}"] += 1

    jobs_by_bucket: Dict[str, List[Dict]] = defaultdict(list)
    for job in company_jobs:
        jobs_by_bucket[get_bucket_name_for_job(job)].append(job)

    leads: List[Dict] = []
    if not eligible:
        for bucket, bucket_jobs in jobs_by_bucket.items():
            primary = _primary_job(bucket_jobs)
            leads.append(
                _build_no_contact_lead(
                    primary,
                    bucket_jobs,
                    bucket,
                    org,
                    company_reason,
                    company_needs_review,
                    "excluded",
                    company_reason,
                )
            )
            stats["company_criteria_excluded"] += 1
        return leads, dict(stats)

    for bucket, bucket_jobs in jobs_by_bucket.items():
        primary = _primary_job(bucket_jobs)
        if not search_domain:
            leads.append(
                _build_no_contact_lead(
                    primary,
                    bucket_jobs,
                    bucket,
                    org,
                    company_reason,
                    company_needs_review,
                    "not_found",
                    "missing_company_domain",
                )
            )
            stats[f"bucket_{bucket}_not_found"] += 1
            stats["missing_company_domain_buckets"] += 1
            continue

        target_titles = get_target_titles_for_jobs(bucket_jobs, org.employee_count)
        people = apollo.search_people_at_company(search_domain, target_titles)
        time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
        ranked_candidates = rank_candidates(people, target_titles)

        target_titles, ranked_candidates, people = _hm_second_pass(
            bucket_jobs, org, search_domain, target_titles, ranked_candidates, people, stats)

        if not ranked_candidates:
            leads.append(
                _build_no_contact_lead(
                    primary,
                    bucket_jobs,
                    bucket,
                    org,
                    company_reason,
                    company_needs_review,
                    "not_found",
                    "no_matching_hiring_manager",
                )
            )
            stats[f"bucket_{bucket}_not_found"] += 1
            stats["no_matching_hiring_manager"] += 1
            continue

        company_domains = _organization_domains(org)
        company_domains.add(search_domain)
        max_person_attempts = config.APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET
        max_hunter_attempts = config.HUNTER_MAX_FALLBACK_ATTEMPTS_PER_BUCKET
        hunter_attempts = 0
        selected_person: Optional[apollo.PersonMatch] = None
        selected_hunter: Optional[hunter.HunterResult] = None
        selected_confidence = "none"
        best_identified: Optional[apollo.PersonMatch] = None
        terminal_reason = "no_usable_email"

        for candidate in ranked_candidates[:max_person_attempts]:
            candidate_tier = _selection_tier(candidate.get("title"))
            if (
                candidate_tier == "founder_fallback"
                and (
                    org.employee_count is None
                    or org.employee_count > config.FOUNDER_FALLBACK_MAX_EMPLOYEES
                )
            ):
                stats["candidate_founder_fallback_disallowed"] += 1
                terminal_reason = "founder_fallback_disallowed_for_company_size"
                continue

            stats["person_match_attempts"] += 1
            person = apollo.match_person(candidate)
            time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
            if not _person_belongs_to_company(person, company_domains, company_name):
                stats["candidate_organization_domain_mismatch"] += 1
                terminal_reason = "candidate_organization_domain_mismatch"
                continue
            if person.person_found and best_identified is None:
                best_identified = person

            hunter_result: Optional[hunter.HunterResult] = None
            allowed_domains = set(company_domains)
            if person.organization_domain:
                allowed_domains.add(person.organization_domain)

            if person.email:
                if not email_matches_company(person.email, allowed_domains):
                    stats["candidate_email_domain_mismatch"] += 1
                    terminal_reason = "candidate_email_domain_mismatch"
                    person.email = None
                    person.email_found = False
                    continue
                if config.VERIFY_WITH_HUNTER and config.HUNTER_API_KEY:
                    hunter_result = hunter.verify_email(person.email)
                    time.sleep(config.HUNTER_RATE_LIMIT_DELAY)
            elif (
                config.VERIFY_WITH_HUNTER
                and person.first_name
                and person.last_name
                and config.HUNTER_API_KEY
                and hunter_attempts < max_hunter_attempts
            ):
                hunter_attempts += 1
                stats["hunter_fallback_attempts"] += 1
                hunter_result = hunter.find_email(
                    person.first_name, person.last_name, search_domain
                )
                time.sleep(config.HUNTER_RATE_LIMIT_DELAY)
                if hunter_result.found and hunter_result.email:
                    if email_matches_company(hunter_result.email, allowed_domains):
                        person.email = hunter_result.email
                        person.email_found = True
                        person.email_source = "hunter"
                    else:
                        stats["candidate_email_domain_mismatch"] += 1
                        terminal_reason = "candidate_email_domain_mismatch"
                        continue

            confidence = _email_confidence(person, hunter_result)
            if confidence == "invalid":
                stats["candidate_email_invalid"] += 1
                terminal_reason = "email_invalid"
                continue
            if confidence == "none":
                stats["candidate_no_usable_email"] += 1
                terminal_reason = "no_usable_email"
                continue

            selected_person = person
            selected_hunter = hunter_result
            selected_confidence = confidence
            terminal_reason = "contact_found"
            break

        person = selected_person or best_identified or apollo.PersonMatch(person_found=False)
        hunter_result = selected_hunter
        found = selected_person is not None
        confidence = selected_confidence if found else "none"
        role_focus = extract_role_focus(
            primary, primary.get("_matched_role", "")
        )

        lead = dict(primary)
        lead.update(
            {
                "_role_bucket": bucket,
                "_hiring_manager_buckets": sorted({
                    get_hiring_manager_bucket_for_job(job) for job in bucket_jobs
                }),
                "_step3_status": "found" if found else "not_found",
                "_step3_reason": terminal_reason,
                "_company_criteria_reason": company_reason,
                "_company_needs_review": company_needs_review,
                "related_open_roles": sorted({j.get("job_title", "") for j in bucket_jobs if j.get("job_title")}),
                "related_job_ids": [j.get("job_id") for j in bucket_jobs if j.get("job_id")],
                "role_focus": role_focus.text,
                "role_focus_quality": role_focus.quality,
                "role_focus_evidence": role_focus.evidence,
                "company_domain": search_domain,
                "company_employee_count": org.employee_count,
                "company_founded_year": org.founded_year,
                "company_industry": org.industry,
                "hiring_manager_name": " ".join(
                    part for part in (person.first_name, person.last_name) if part
                ) or None,
                "hiring_manager_first_name": person.first_name,
                "hiring_manager_last_name": person.last_name,
                "hiring_manager_title": person.title,
                "hiring_manager_linkedin": person.linkedin_url,
                "hiring_manager_email": person.email if found else None,
                "hiring_manager_email_source": person.email_source if found else None,
                "apollo_email_status": person.email_status if found else None,
                "hunter_email_status": hunter_result.status if hunter_result else None,
                "hiring_manager_confidence": confidence,
                "hiring_manager_selection_tier": _selection_tier(person.title) if person.person_found else None,
                "campaign_id": config.resolve_campaign_id(bucket, org.employee_count),
            }
        )
        if found and person.email:
            lead["lead_key"] = _lead_key(search_domain, person.email, bucket)
        # Freshness and URL quality are evaluated only for contactable leads,
        # because those are the records that enter the Airtable review queue.
        lead = annotate_job(lead, probe_url=found)
        leads.append(lead)
        stats[f"bucket_{bucket}_{'found' if found else 'not_found'}"] += 1
        if found:
            stats[f"selection_tier_{_selection_tier(person.title)}"] += 1

    return leads, dict(stats)



def _strict_gate_from_job(job: Dict, field: str, gate: str) -> GateDecision:
    payload = job.get(field)
    if isinstance(payload, dict):
        return gate_decision_from_dict(payload, gate=gate)
    state = job.get(f"_{gate}_gate_state") or GateState.UNVERIFIED.value
    reason = job.get(f"_{gate}_gate_reason") or f"UNVERIFIED_{gate.upper()}_GATE"
    return GateDecision(gate, state, reason, retryable=False, next_action="discard_and_replace")


def _strict_base_lead(
    primary: Dict,
    bucket_jobs: List[Dict],
    bucket: str,
    org: apollo.OrgEnrichment,
    account_decision: GateDecision,
) -> Dict:
    role_focus = extract_role_focus(primary, primary.get("_matched_role", ""))
    role_display = resolve_role_display({**primary, "role_focus": role_focus.text})
    related_role_results = [
        resolve_role_display({
            **job,
            "role_focus": extract_role_focus(job, job.get("_matched_role", "")).text,
        })
        for job in bucket_jobs
    ]
    related_role_displays = {
        result.name
        for result in related_role_results
        if result.name and not result.hold
    }
    related_role_holds = [
        result.evidence.get("raw_title") or result.name
        for result in related_role_results
        if result.hold
    ]
    role_evidence = dict(role_display.evidence)
    if related_role_holds:
        role_evidence["related_ambiguous_roles"] = related_role_holds
    canonical_company_name = account_decision.metadata.get("canonical_company_name")
    canonical_domain = (
        account_decision.metadata.get("canonical_domain")
        or org.domain
        or primary.get("employer_website")
    )
    company_display = resolve_company_display(
        organization=primary.get("organization") or primary.get("employer_name"),
        org_linkedin_name=primary.get("org_linkedin_name"),
        canonical_company_name=canonical_company_name,
        org_linkedin_slug=primary.get("org_linkedin_slug"),
        org_linkedin_website=primary.get("org_linkedin_website"),
        employer_domain=canonical_domain,
        canonical_identity_verified=account_decision.state_value == GateState.PASS.value,
    )
    lead = dict(primary)
    lead.update(
        {
            "_role_bucket": bucket,
            "_hiring_manager_buckets": sorted({
                get_hiring_manager_bucket_for_job(job) for job in bucket_jobs
            }),
            "_company_criteria_reason": (
                account_decision.primary_reason.value
                if hasattr(account_decision.primary_reason, "value")
                else str(account_decision.primary_reason)
            ),
            "_company_needs_review": False,
            "_account_gate_state": account_decision.state_value,
            "_account_gate_reason": (
                account_decision.primary_reason.value
                if hasattr(account_decision.primary_reason, "value")
                else str(account_decision.primary_reason)
            ),
            "_account_gate_decision": account_decision.to_dict(),
            "related_open_roles": sorted({
                j.get("canonical_job_title") or j.get("job_title", "")
                for j in bucket_jobs
                if j.get("canonical_job_title") or j.get("job_title")
            }),
            "related_outbound_roles": sorted(related_role_displays),
            "outbound_role_name": role_display.name,
            "outbound_role_confidence": role_display.confidence,
            "_outbound_role_hold": bool(role_display.hold or related_role_holds),
            "_outbound_role_evidence": role_evidence,
            "_outbound_role_resolver_version": role_display.resolver_version,
            "related_job_ids": [j.get("job_id") for j in bucket_jobs if j.get("job_id")],
            "role_focus": role_focus.text,
            "role_focus_quality": role_focus.quality,
            "role_focus_evidence": role_focus.evidence,
            "company_domain": account_decision.metadata.get("canonical_domain"),
            "company_employee_count": org.employee_count,
            "company_founded_year": org.founded_year,
            "company_industry": org.industry,
            "company_business_model": account_decision.metadata.get("business_model"),
            "canonical_company_name": canonical_company_name,
            "outbound_company_name": company_display.name,
            "outbound_company_confidence": company_display.confidence,
            "outbound_company_identity_key": company_display.identity_key,
            "_outbound_company_identity_safe": company_display.identity_safe,
            "_outbound_company_hold": company_display.hold,
            "_outbound_company_evidence": company_display.evidence,
            "_outbound_display_resolver_version": company_display.resolver_version,
            "campaign_id": config.resolve_campaign_id(bucket, org.employee_count),
        }
    )
    return lead


def _outbound_display_gate(lead: Dict) -> GateDecision:
    if lead.get("_outbound_company_hold"):
        return GateDecision(
            "display",
            GateState.NEEDS_CHECK,
            ReasonCode.NEEDS_CHECK_OUTBOUND_COMPANY_IDENTITY,
            retryable=False,
            next_action="resolve_company_display_alias_before_approval",
            metadata={
                "confidence": lead.get("outbound_company_confidence") or "low",
                "identity_key": lead.get("outbound_company_identity_key") or "",
                "identity_safe": bool(lead.get("_outbound_company_identity_safe")),
                "hold": True,
            },
        )
    if lead.get("_outbound_role_hold"):
        return GateDecision(
            "display",
            GateState.NEEDS_CHECK,
            ReasonCode.NEEDS_CHECK_OUTBOUND_ROLE_AMBIGUOUS,
            retryable=False,
            next_action="resolve_outbound_role_before_approval",
            metadata={
                "confidence": lead.get("outbound_role_confidence") or "low",
                "hold": True,
                "role_evidence": lead.get("_outbound_role_evidence") or {},
            },
        )
    return GateDecision(
        "display",
        GateState.PASS,
        "OUTBOUND_DISPLAY_PASS",
        metadata={
            "confidence": lead.get("outbound_company_confidence") or "",
            "identity_key": lead.get("outbound_company_identity_key") or "",
            "identity_safe": bool(lead.get("_outbound_company_identity_safe")),
            "role_confidence": lead.get("outbound_role_confidence") or "",
            "hold": False,
        },
    )


def _process_company_strict(company_jobs: List[Dict]) -> Tuple[List[Dict], Dict]:
    """Run Account, Contact, Email and final gates for prequalified jobs."""
    stats = defaultdict(int)
    first = company_jobs[0]
    input_domain = _best_input_domain(first)
    company_name = str(first.get("canonical_employer_name") or first.get("employer_name") or "")
    enrichment_website = f"https://{input_domain}" if input_domain else ""
    org = apollo.enrich_organization(
        domain=input_domain, name=company_name, website=enrichment_website
    )
    time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
    founder_allowed = founder_allowed_for_employee_count(org.employee_count)

    account_decision = AccountGate().evaluate(
        org=org,
        input_company_name=company_name,
        input_domain=input_domain,
        jobs=company_jobs,
    )
    stats[f"account_{account_decision.state_value.lower()}"] += 1
    stats[f"account_reason__{_reason_family(str(account_decision.primary_reason))}"] += 1

    jobs_by_bucket: Dict[str, List[Dict]] = defaultdict(list)
    for job in company_jobs:
        jobs_by_bucket[get_bucket_name_for_job(job)].append(job)

    leads: List[Dict] = []
    # Company-level "at least one bucket had X" flags for the row-2 instrumentation
    # (66-eligible-to-19-attempts reconciliation). Set inside the bucket loop below.
    company_had_people_search_call = False
    company_had_person_returned = False
    company_had_title_match = False
    company_had_untried_candidate = False
    company_had_person_match_attempt = False
    for bucket, bucket_jobs in jobs_by_bucket.items():
        primary = _primary_job(bucket_jobs)
        job_decision = _strict_gate_from_job(primary, "_job_gate_decision", "job")
        role_decision = _strict_gate_from_job(primary, "_role_gate_decision", "role")
        lead = _strict_base_lead(primary, bucket_jobs, bucket, org, account_decision)
        display_decision = _outbound_display_gate(lead)

        search_domain = str(
            account_decision.metadata.get("canonical_domain")
            or input_domain
            or org.domain
            or ""
        )
        # Additive, deterministic domain recovery + explicit classification. This
        # NEVER overrides an already-resolved domain and never accepts an
        # intermediary host; when the domain is empty it tries two safe evidence
        # steps (direct apply_options host, curated employer alias) that can unlock
        # a live Apollo search, and it always classifies WHY a company is
        # unresolved so staffing/aggregator posters are not conflated with genuine
        # resolver failures (the real cause of this run's 353 no_search_domain).
        domain_res = domain_resolution.recover_search_domain(
            search_domain, primary, company_name)
        if not search_domain and domain_res.resolved_domain:
            search_domain = domain_res.resolved_domain
            stats[f"domain_recovered__{domain_res.resolution_method}"] += 1
        if account_decision.state_value == GateState.REJECT.value or not search_domain:
            final = annotate_final_decision(
                lead,
                {
                    "job": job_decision,
                    "role": role_decision,
                    "account": account_decision,
                    "display": display_decision,
                },
            )
            final["_step3_status"] = (
                "excluded" if final.get("_final_state") == "REJECT" else "unverified"
            )
            final["_step3_reason"] = final.get("_final_primary_reason")
            final["hiring_manager_confidence"] = "none"
            final["_row2_diagnostic"] = {
                "company_key": company_key_for_job(first),
                "domain": search_domain or input_domain or "",
                "bucket": bucket,
                "people_search_call": False,
                "apollo_search_error": False,
                "people_returned": None,
                "title_matched_candidates": None,
                "untried_candidates": None,
                "person_match_attempts": 0,
                "terminal_reason": (
                    "account_reject"
                    if account_decision.state_value == GateState.REJECT.value
                    else "no_search_domain"
                ),
                # Explicit domain-resolution evidence so a no_search_domain failure
                # is classified (staffing/aggregator vs known-employer-acronym vs
                # no-evidence) rather than opaque.
                "domain_classification": domain_res.classification,
                "domain_unresolved_reason": domain_res.unresolved_reason,
                "domain_resolution_method": domain_res.resolution_method,
            }
            leads.append(final)
            stats[f"final_{str(final.get('_final_state')).lower()}"] += 1
            if not search_domain:
                stats[f"domain_unresolved__{domain_res.unresolved_reason or 'unknown'}"] += 1
            continue

        target_titles = get_target_titles_for_jobs(bucket_jobs, org.employee_count)
        # Denylist-checked, not just search_domain trusted as already-safe by
        # construction (identity-key audit, FINAL_30_PLUS_SYSTEM_SPEC.md
        # section 19) -- deliberately kept as a bare domain (no "domain:"
        # prefix) to preserve RerouteRegistry's existing persisted key shape
        # exactly; canonical_company_key()'s prefixed format is used by
        # recovery_inventory.py's stores, which already persist that shape.
        safe_search_domain = safe_company_domain(search_domain, config.INTERMEDIARY_JOB_DOMAINS)
        account_bucket_key = f"{safe_search_domain}|{bucket}"
        reroute_registry = RerouteRegistry()
        attempted_before = reroute_registry.attempted_ids(account_bucket_key)

        company_had_people_search_call = True
        stats["row2_people_search_calls_total"] += 1
        apollo_error = False
        try:
            people = apollo.search_people_at_company(search_domain, target_titles)
        except apollo.GLOBAL_FATAL_ERRORS:
            # A whole-account Apollo outage must propagate to open the run-level
            # circuit, not be masked as an empty-people result for this one bucket.
            raise
        except Exception as exc:
            apollo_error = True
            people = []
            logger.warning(
                "Apollo people search error for %s|%s: %s", search_domain, bucket, exc
            )
        time.sleep(config.APOLLO_RATE_LIMIT_DELAY)

        if apollo_error:
            # An exception/failed response is not the same as a confirmed empty
            # result and must not be counted under bucket_zero_apollo_people.
            stats["bucket_apollo_search_error"] += 1
            title_ranked_candidates: List[Dict] = []
            ranked_candidates: List[Dict] = []
        else:
            stats["row2_apollo_people_returned_total"] += len(people)
            if people:
                stats["row2_buckets_with_apollo_person"] += 1
                company_had_person_returned = True
            title_ranked_candidates = rank_candidates(people, target_titles)
            # Quality-preserving second pass: broaden WITHIN the same function to
            # adjacent senior decision-makers when the first title-filtered search
            # matched nobody. Apollo filters person_titles server-side
            # (include_similar_titles=false), so a broadened set can also recover a
            # subset of zero-people buckets. Recovered candidates pass every
            # downstream gate unchanged.
            if not title_ranked_candidates and config.HM_SECOND_PASS_TITLE_BROADENING:
                broadened = get_broadened_target_titles_for_jobs(bucket_jobs, org.employee_count)
                if broadened and broadened != target_titles:
                    stats["hm_second_pass_attempts"] += 1
                    try:
                        people2 = apollo.search_people_at_company(search_domain, broadened)
                    except apollo.GLOBAL_FATAL_ERRORS:
                        raise
                    except Exception:  # noqa: BLE001 - never worsens the miss
                        people2 = None
                    if people2:
                        time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
                        t2 = rank_candidates(people2, broadened)
                        if t2:
                            stats["hm_second_pass_recovered"] += 1
                            target_titles = broadened
                            people = people2
                            title_ranked_candidates = t2
            stats["row2_title_matched_candidates_total"] += len(title_ranked_candidates)
            if title_ranked_candidates:
                stats["row2_buckets_with_title_match"] += 1
                company_had_title_match = True
            ranked_candidates = [
                item for item in title_ranked_candidates
                if _candidate_identity_key(item) not in attempted_before
            ]
            if not founder_allowed:
                # Defensive safety net: target_titles already excludes
                # founder-tier query titles when founder_allowed is False
                # (role_mapping._founders_last), but Apollo's own similar-title
                # expansion can still surface one. Drop it here too, before it
                # can consume an attempt slot that ContactGate would reject
                # deterministically anyway (ROOT_CAUSE_TABLE_STRUCTURAL.md row 2).
                before_founder_filter = len(ranked_candidates)
                ranked_candidates = [
                    item for item in ranked_candidates
                    if not is_founder_tier_title(str(item.get("title") or ""))
                ]
                if len(ranked_candidates) < before_founder_filter:
                    stats["bucket_founder_tier_prefiltered"] += 1
            stats["row2_untried_candidates_total"] += len(ranked_candidates)
            if ranked_candidates:
                stats["row2_buckets_with_untried_candidate"] += 1
                company_had_untried_candidate = True

        if apollo_error:
            zero_attempt_reason = "apollo_search_error"
        elif not ranked_candidates:
            # Distinguishes why this bucket contributes zero person_match_attempts:
            # no people returned by Apollo at all, people returned but none ranked
            # against target titles, or candidates existed but were all already
            # attempted in a prior run (RerouteRegistry). Instrumentation only —
            # does not change which candidates are searched or attempted.
            if not people:
                stats["bucket_zero_apollo_people"] += 1
                zero_attempt_reason = "zero_apollo_people"
            elif not title_ranked_candidates:
                stats["bucket_no_title_match"] += 1
                zero_attempt_reason = "no_title_match"
            else:
                stats["bucket_all_candidates_previously_attempted"] += 1
                zero_attempt_reason = "all_candidates_previously_attempted"
        else:
            zero_attempt_reason = None

        company_domains = _organization_domains(org)
        company_domains.add(search_domain)
        selected_person: Optional[apollo.PersonMatch] = None
        selected_hunter: Optional[hunter.HunterResult] = None
        selected_contact_decision: Optional[GateDecision] = None
        selected_email_decision: Optional[GateDecision] = None
        last_contact_decision: Optional[GateDecision] = None
        last_email_decision: Optional[GateDecision] = None
        attempted_ids: List[str] = []
        attempted_id_reasons: Dict[str, str] = {}
        hunter_attempts = 0
        max_attempts = min(
            max(1, config.CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET),
            max(1, len(ranked_candidates)),
        )

        for candidate in ranked_candidates[:max_attempts]:
            candidate_id = _candidate_identity_key(candidate)
            if candidate_id:
                attempted_ids.append(candidate_id)
            stats["person_match_attempts"] += 1
            try:
                person = apollo.match_person(candidate)
            except apollo.GLOBAL_FATAL_ERRORS:
                # A whole-account Apollo outage must propagate to open the
                # run-level circuit, not be masked as a per-candidate match error.
                raise
            except Exception as exc:
                # A transient/provider error must not abort the whole run (the
                # same defect class already fixed for org enrichment and people
                # search -- TECHNICAL_DESIGN.md D10). Count it distinctly and
                # move on to the next candidate rather than crashing.
                stats["candidate_match_error"] += 1
                logger.warning(
                    "Apollo match_person error for %s (candidate %s): %s",
                    search_domain, candidate_id, exc,
                )
                if candidate_id:
                    attempted_id_reasons[candidate_id] = "APOLLO_MATCH_ERROR"
                continue
            time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
            contact_decision = ContactGate().evaluate(
                person=person,
                target_titles=target_titles,
                company_domains=company_domains,
                company_name=company_name,
                intent_market="us_market",
                founder_allowed=founder_allowed,
            )
            last_contact_decision = contact_decision
            if contact_decision.state_value == GateState.NEEDS_CHECK.value:
                # Real terminal state this candidate can reach (e.g. current
                # employment unverified) that was previously invisible in the
                # stats dict, since only hard failures were counted
                # (TECHNICAL_DESIGN.md D12).
                stats[f"contact_needs_check_reason__{_reason_family(str(contact_decision.primary_reason))}"] += 1
            if contact_decision.state_value not in {
                GateState.PASS.value,
                GateState.NEEDS_CHECK.value,
            }:
                stats[f"contact_reason__{_reason_family(str(contact_decision.primary_reason))}"] += 1
                if candidate_id:
                    attempted_id_reasons[candidate_id] = str(contact_decision.primary_reason or "")
                continue

            allowed_domains = set(company_domains)
            if person.organization_domain:
                allowed_domains.add(person.organization_domain)
            hunter_result: Optional[hunter.HunterResult] = None
            if person.email and config.VERIFY_WITH_HUNTER and config.HUNTER_API_KEY:
                try:
                    hunter_result = hunter.verify_email(person.email)
                except Exception as exc:
                    stats["email_verify_error"] += 1
                    logger.warning("Hunter verify_email error for %s: %s", person.email, exc)
                    hunter_result = None
                time.sleep(config.HUNTER_RATE_LIMIT_DELAY)
            elif (
                config.VERIFY_WITH_HUNTER
                and not person.email
                and person.first_name
                and person.last_name
                and config.HUNTER_API_KEY
                and hunter_attempts < config.HUNTER_MAX_FALLBACK_ATTEMPTS_PER_BUCKET
            ):
                hunter_attempts += 1
                stats["hunter_fallback_attempts"] += 1
                try:
                    hunter_result = hunter.find_email(
                        person.first_name, person.last_name, search_domain
                    )
                except Exception as exc:
                    stats["email_verify_error"] += 1
                    logger.warning(
                        "Hunter find_email error for %s %s: %s",
                        person.first_name, person.last_name, exc,
                    )
                    hunter_result = None
                time.sleep(config.HUNTER_RATE_LIMIT_DELAY)
                if hunter_result and hunter_result.found and hunter_result.email:
                    person.email = hunter_result.email
                    person.email_found = True
                    person.email_source = "hunter"

            email_decision = EmailGate().evaluate(
                person=person,
                hunter_result=hunter_result,
                company_domains=allowed_domains,
            )
            last_email_decision = email_decision
            if email_decision.state_value == GateState.NEEDS_CHECK.value:
                stats[f"email_needs_check_reason__{_reason_family(str(email_decision.primary_reason))}"] += 1
            if email_decision.state_value not in {
                GateState.PASS.value,
                GateState.NEEDS_CHECK.value,
            }:
                stats[f"email_reason__{_reason_family(str(email_decision.primary_reason))}"] += 1
                if candidate_id:
                    attempted_id_reasons[candidate_id] = str(email_decision.primary_reason or "")
                continue

            selected_person = person
            selected_hunter = hunter_result
            selected_contact_decision = contact_decision
            selected_email_decision = email_decision
            break

        if attempted_ids:
            company_had_person_match_attempt = True

        if selected_person and selected_contact_decision and selected_email_decision:
            person = selected_person
            lead.update(
                {
                    "_step3_status": "found",
                    "_step3_reason": "strict_contact_and_email_pass",
                    "hiring_manager_name": " ".join(
                        part for part in (person.first_name, person.last_name) if part
                    ) or None,
                    "hiring_manager_first_name": person.first_name,
                    "hiring_manager_last_name": person.last_name,
                    "hiring_manager_title": person.title,
                    "hiring_manager_linkedin": person.linkedin_url,
                    "hiring_manager_person_id": person.person_id,
                    "hiring_manager_email": person.email,
                    "hiring_manager_email_source": person.email_source,
                    "apollo_email_status": person.email_status,
                    "hunter_email_status": selected_hunter.status if selected_hunter else None,
                    "hiring_manager_confidence": (
                        "verified"
                        if all(
                            decision.state_value == GateState.PASS.value
                            for decision in (
                                job_decision,
                                role_decision,
                                account_decision,
                                display_decision,
                                selected_contact_decision,
                                selected_email_decision,
                            )
                        )
                        else "review"
                    ),
                    "hiring_manager_selection_tier": _selection_tier(person.title),
                }
            )
            lead["lead_key"] = _lead_key(search_domain, str(person.email), bucket)
            final = annotate_final_decision(
                lead,
                {
                    "job": job_decision,
                    "role": role_decision,
                    "account": account_decision,
                    "display": display_decision,
                    "contact": selected_contact_decision,
                    "email": selected_email_decision,
                },
            )
            reroute_registry.clear(account_bucket_key)
        else:
            if ranked_candidates and last_contact_decision and last_contact_decision.state_value == GateState.REROUTE.value:
                contact_decision = last_contact_decision
                gates = {
                    "job": job_decision,
                    "role": role_decision,
                    "account": account_decision,
                    "display": display_decision,
                    "contact": contact_decision,
                }
            elif last_contact_decision and last_contact_decision.state_value == GateState.PASS.value and last_email_decision:
                gates = {
                    "job": job_decision,
                    "role": role_decision,
                    "account": account_decision,
                    "display": display_decision,
                    "contact": last_contact_decision,
                    "email": last_email_decision,
                }
            else:
                contact_decision = GateDecision(
                    "contact", GateState.UNVERIFIED,
                    ReasonCode.UNVERIFIED_NO_VALID_CONTACT,
                    retryable=True, next_action="retry_contact_reroute_then_replace",
                )
                gates = {
                    "job": job_decision,
                    "role": role_decision,
                    "account": account_decision,
                    "display": display_decision,
                    "contact": contact_decision,
                }
            final = annotate_final_decision(lead, gates)
            final["_step3_status"] = (
                "reroute" if final.get("_final_state") == "REROUTE" else "unverified"
            )
            final["_step3_reason"] = final.get("_final_primary_reason")
            final["hiring_manager_confidence"] = "none"
            if attempted_ids:
                # Each candidate's own contact/email failure reason drives its
                # own TTL (ROOT_CAUSE_TABLE_STRUCTURAL.md row 8 / TECHNICAL_DESIGN.md
                # D9) -- a candidate never reached (e.g. loop broke early) or
                # whose specific reason wasn't captured falls back to the
                # bucket's overall final reason, matching the prior behavior.
                fallback_reason = str(final.get("_final_primary_reason") or "")
                reroute_registry.record_many(
                    account_bucket_key,
                    {
                        candidate_id: attempted_id_reasons.get(candidate_id, fallback_reason)
                        for candidate_id in attempted_ids
                    },
                )

        final = annotate_job(final, probe_url=False)
        final["_row2_diagnostic"] = {
            "company_key": company_key_for_job(first),
            "domain": search_domain,
            "bucket": bucket,
            "people_search_call": True,
            "apollo_search_error": apollo_error,
            "people_returned": None if apollo_error else len(people),
            "title_matched_candidates": None if apollo_error else len(title_ranked_candidates),
            "untried_candidates": None if apollo_error else len(ranked_candidates),
            "person_match_attempts": len(attempted_ids),
            "terminal_reason": "attempted" if attempted_ids else zero_attempt_reason,
        }
        leads.append(final)
        stats[f"bucket_{bucket}_{final.get('_step3_status')}"] += 1
        stats[f"final_{str(final.get('_final_state')).lower()}"] += 1

    if company_had_people_search_call:
        stats["row2_companies_with_people_search_call"] += 1
    if company_had_person_returned:
        stats["row2_companies_with_person_returned"] += 1
    if company_had_title_match:
        stats["row2_companies_with_title_match"] += 1
    if company_had_untried_candidate:
        stats["row2_companies_with_untried_candidate"] += 1
    if company_had_person_match_attempt:
        stats["row2_companies_with_person_match_attempt"] += 1

    return leads, dict(stats)


def process_company(company_jobs: List[Dict]) -> Tuple[List[Dict], Dict]:
    strict = bool(
        config.FINAL_PASS_PIPELINE_ENABLED
        and company_jobs
        and all(job.get("_job_gate_state") for job in company_jobs)
    )
    if strict:
        return _process_company_strict(company_jobs)
    return _process_company_legacy(company_jobs)


def _is_final_pass_lead(lead: Dict) -> bool:
    return bool(
        lead.get("_final_state") == "FINAL_PASS"
        and lead.get("hiring_manager_email")
        and lead.get("lead_key")
    )


def _is_reviewable_lead(lead: Dict) -> bool:
    """Return Airtable-surface candidates, with legacy-fixture compatibility."""
    if lead.get("_final_state"):
        return is_airtable_reviewable(lead)
    return bool(
        lead.get("_step3_status") == "found"
            and lead.get("hiring_manager_confidence") in {"high", "medium", "low", "review", "verified"}
        and lead.get("hiring_manager_email")
        and lead.get("lead_key")
    )


def _count_unique_reviewable_leads(leads: List[Dict]) -> int:
    return len({str(lead.get("lead_key")) for lead in leads if _is_reviewable_lead(lead)})


def _count_unique_final_pass_leads(leads: List[Dict]) -> int:
    return len({str(lead.get("lead_key")) for lead in leads if _is_final_pass_lead(lead)})


def _final_state_counts(leads: List[Dict]) -> Dict[str, int]:
    counts = {name: 0 for name in ("FINAL_PASS", "NEEDS_CHECK", "REROUTE", "UNVERIFIED", "REJECT")}
    for lead in leads:
        state = str(lead.get("_final_state") or "")
        if state in counts:
            counts[state] += 1
    return counts


def _company_priority(item: Tuple[str, List[Dict]]) -> Tuple[int, int, int]:
    """Prioritize safer, stronger account signals before the Apollo safety cap."""
    _company_key, company_jobs = item
    has_first_party_domain = int(any(_best_input_domain(job) for job in company_jobs))
    max_relevance = max(
        (int(job.get("_role_relevance_score") or 0) for job in company_jobs),
        default=0,
    )
    multiple_openings = len({job.get("job_id") for job in company_jobs if job.get("job_id")})
    return has_first_party_domain, max_relevance, multiple_openings


def _job_state_ref(job: Dict) -> Dict:
    """Keep only the fields SeenJobsRegistry needs for cross-day dedupe."""
    return {
        "job_id": job.get("job_id"),
        "employer_name": job.get("employer_name"),
        "employer_website": job.get("employer_website"),
        "job_title": job.get("job_title"),
    }


_ENRICHMENT_PROGRESS_SCHEMA = "hiring-manager-enrichment-progress/1"
_ENRICHMENT_PROGRESS_FILE = "enrichment_progress.json"


class _EnrichmentProgress:
    """Per-company enrichment checkpoint written beside the Step 3 output.

    Only SUCCESSFULLY enriched companies are recorded. A resumed run reuses them
    instead of re-calling Apollo (no re-consumed credits), while any company that
    failed -- record-level or a whole-account Apollo outage -- is deliberately not
    recorded, so it stays recoverable on the next run.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.companies: Dict[str, Dict] = {}

    @classmethod
    def load(cls, directory: str) -> "_EnrichmentProgress":
        path = Path(directory) / _ENRICHMENT_PROGRESS_FILE
        progress = cls(path)
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("schema") == _ENRICHMENT_PROGRESS_SCHEMA:
                    companies = data.get("companies")
                    if isinstance(companies, dict):
                        progress.companies = companies
        except Exception:  # noqa: BLE001 - a corrupt checkpoint just starts fresh
            logger.warning(
                "Could not read enrichment progress checkpoint at %s; starting fresh.",
                path,
            )
            progress.companies = {}
        return progress

    def get(self, company_key: str) -> Optional[Tuple[List[Dict], Dict]]:
        entry = self.companies.get(company_key)
        if not isinstance(entry, dict):
            return None
        leads = entry.get("leads")
        stats = entry.get("stats")
        if isinstance(leads, list) and isinstance(stats, dict):
            return leads, stats
        return None

    def record(self, company_key: str, leads: List[Dict], stats: Dict) -> None:
        self.companies[company_key] = {"leads": leads, "stats": dict(stats)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"schema": _ENRICHMENT_PROGRESS_SCHEMA, "companies": self.companies},
                    default=str,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)  # atomic
        except Exception:  # noqa: BLE001 - checkpointing is never fatal
            logger.warning(
                "Could not persist enrichment progress checkpoint at %s.",
                self.path,
                exc_info=True,
            )


def _apollo_circuit_reason(exc: BaseException) -> str:
    if isinstance(exc, apollo.ApolloCreditsExhaustedError):
        return "apollo_credit_exhausted"
    if isinstance(exc, apollo.ApolloAuthorizationError):
        return "apollo_authorization"
    if isinstance(exc, apollo.ApolloRateLimited):
        return "apollo_rate_limited"
    return "apollo_unavailable"


def _degraded_company_leads(
    company_jobs: List[Dict], *, reason: str
) -> Tuple[List[Dict], Dict]:
    """Emit an honest UNVERIFIED lead for a company Apollo could not enrich.

    Never FINAL_PASS (there is no verified contact), never fabricates firmographics
    or an email. The rest of the batch continues; this company stays reviewable as
    UNVERIFIED and recoverable on a later run.
    """
    first = company_jobs[0]
    input_domain = _best_input_domain(first)
    company_name = str(
        first.get("canonical_employer_name") or first.get("employer_name") or ""
    )
    lead = dict(first)
    lead.update(
        {
            "_role_bucket": get_bucket_name_for_job(first),
            "company_domain": input_domain or None,
            "canonical_company_name": company_name or None,
            "_final_state": "UNVERIFIED",
            "_final_primary_reason": reason,
            "_step3_status": "unverified",
            "_step3_reason": reason,
            "hiring_manager_name": None,
            "hiring_manager_email": None,
            "hiring_manager_confidence": "none",
            "_company_needs_review": True,
            "_apollo_enrichment_failed": True,
        }
    )
    stats: Dict[str, int] = {
        "final_unverified": 1,
        "apollo_degraded_companies": 1,
        f"apollo_degraded_reason__{reason}": 1,
    }
    return [lead], stats


def run_hiring_manager_identification(
    input_path: Optional[str] = None,
    *,
    target_eligible_companies: Optional[int] = None,
    target_reviewable_leads: Optional[int] = None,
    target_final_pass_leads: Optional[int] = None,
    max_eligible_companies: Optional[int] = None,
    exclude_company_keys: Optional[set[str]] = None,
    exclude_company_function_keys: Optional[set[str]] = None,
    output_suffix: Optional[str] = None,
) -> Step3Result:
    """Enrich prequalified accounts under the applicable daily target.

    The strict target is a minimum SLA. Production continues through every
    eligible company unless an explicit safety cap is configured. Legacy
    reviewable behavior remains available for controlled rollback tests.
    """
    validate_preflight()
    for name, value in (
        ("target_eligible_companies", target_eligible_companies),
        ("target_reviewable_leads", target_reviewable_leads),
        ("target_final_pass_leads", target_final_pass_leads),
        ("max_eligible_companies", max_eligible_companies),
    ):
        if value is not None and value < 1:
            raise ValueError(f"{name} must be at least 1")

    input_path = input_path or config.STEP2_KEPT_FILE
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    jobs = payload.get("jobs", [])
    strict_input = bool(
        config.FINAL_PASS_PIPELINE_ENABLED
        and any(job.get("_job_gate_state") for job in jobs)
    )
    if strict_input and target_final_pass_leads is None:
        target_final_pass_leads = (
            target_reviewable_leads
            if target_reviewable_leads is not None
            else config.get_final_pass_target()
        )

    jobs_by_company: Dict[str, List[Dict]] = defaultdict(list)
    excluded_company_keys = {str(value) for value in (exclude_company_keys or set()) if value}
    # FUNCTION-level pre-Apollo exclusion: prefixed ``domain:x|bucket:y`` /
    # ``name:x|bucket:y`` keys of Airtable's already-active company+function leads.
    # A match removes ONLY the same company+function candidate (its own bucket),
    # never the whole company -- so a DIFFERENT function of the same company still
    # proceeds. Keys are derived by airtable_client's own helper, so this skip and
    # the delivery-side suppression share one derivation and cannot disagree.
    excluded_company_function_keys = {
        str(value) for value in (exclude_company_function_keys or set()) if value
    }
    _af_keys = None
    if excluded_company_function_keys:
        import airtable_client as _af
        _af_keys = _af.company_function_keys_for_job
    skipped_existing_company_keys: set[str] = set()
    skipped_existing_function_keys: set[str] = set()
    skipped_existing_function_jobs = 0
    skipped_existing_job_rows: List[Dict] = []
    skipped_existing_jobs = 0
    # Company-level CRM / active-pipeline exclusion. It runs HERE -- during
    # company grouping, before the first Apollo call and therefore before any
    # Hunter call and any Airtable delivery -- because this is the only point in
    # the orchestrator path (RealEnrichmentStage -> run_precontact_qualification
    # -> here) where companies exist as companies. Step 2's run_filter applies
    # the same exclusion, but the orchestrator path never calls run_filter, so
    # without this the CRM list was inert for every orchestrator run.
    crm_normalized, crm_compact = _crm_exclusion_sets()
    crm_excluded_company_keys: set[str] = set()
    crm_excluded_reasons: Dict[str, str] = {}
    crm_excluded_jobs = 0
    for job in jobs:
        company_key = company_key_for_job(job)
        if company_key in excluded_company_keys:
            skipped_existing_company_keys.add(company_key)
            skipped_existing_job_rows.append(job)
            skipped_existing_jobs += 1
            continue
        # Function-level pre-Apollo skip: only this company+function candidate is
        # removed (multi-function preserved). Fail-open: a job with no resolvable
        # function key (no bucket / no domain+name) yields an empty set and is
        # never suppressed here -- the delivery backstop still applies.
        if _af_keys is not None:
            job_function_keys = _af_keys(job)
            if job_function_keys and (job_function_keys & excluded_company_function_keys):
                skipped_existing_function_keys |= job_function_keys
                skipped_existing_job_rows.append(job)
                skipped_existing_function_jobs += 1
                continue
        # The decision is ACCOUNT-WIDE, never function-specific: one CRM match
        # removes every posting and therefore every function bucket of that
        # company, including buckets already grouped from earlier postings.
        if company_key in crm_excluded_company_keys:
            crm_excluded_jobs += 1
            continue
        in_crm, crm_reason = is_in_crm(job, crm_normalized, crm_compact)
        if in_crm:
            already_grouped = jobs_by_company.pop(company_key, [])
            crm_excluded_company_keys.add(company_key)
            crm_excluded_reasons[company_key] = crm_reason
            crm_excluded_jobs += len(already_grouped) + 1
            continue
        jobs_by_company[company_key].append(job)
    if crm_excluded_company_keys:
        logger.info(
            "CRM exclusion removed %d company(ies) / %d job(s) before enrichment: %s",
            len(crm_excluded_company_keys), crm_excluded_jobs,
            ", ".join(sorted(crm_excluded_company_keys)[:20]),
        )

    all_leads: List[Dict] = []
    processed_jobs: List[Dict] = list(skipped_existing_job_rows)
    total_stats = defaultdict(int)
    total_stats["topup_skipped_previously_considered_companies"] = len(skipped_existing_company_keys)
    total_stats["topup_skipped_previously_considered_jobs"] = skipped_existing_jobs
    total_stats["preapollo_skipped_existing_function_keys"] = len(skipped_existing_function_keys)
    total_stats["preapollo_skipped_existing_function_jobs"] = skipped_existing_function_jobs
    total_stats["crm_excluded_companies"] = len(crm_excluded_company_keys)
    total_stats["crm_excluded_jobs"] = crm_excluded_jobs
    if skipped_existing_function_jobs:
        logger.info(
            "Pre-Apollo existing company+function skip removed %d job(s) across %d "
            "function key(s) before any Apollo call",
            skipped_existing_function_jobs, len(skipped_existing_function_keys),
        )
    companies_considered = 0
    eligible_companies = 0
    excluded_companies = 0
    company_items = list(jobs_by_company.items())
    company_items.sort(key=_company_priority, reverse=True)
    total_candidate_companies = len(company_items)
    stop_reason = "candidate_pool_exhausted"
    processed_company_keys: List[str] = sorted(skipped_existing_company_keys)

    # Per-company enrichment checkpoint: a resumed run reuses already-enriched
    # companies instead of re-calling Apollo. The Apollo circuit stays closed until
    # a whole-account failure (credit/auth/rate-limit) trips it, after which no
    # further Apollo calls are made and completed work is preserved.
    progress = _EnrichmentProgress.load(config.STEP3_OUTPUT_DIR)

    # Optional soft wall-clock budget (0 = unlimited). A fail-safe for large daily
    # runs: on expiry the loop stops taking NEW companies while every enriched
    # company is already checkpointed, so a later run resumes without re-consuming
    # Apollo or duplicating Airtable rows. Never truncates silently.
    enrichment_budget_seconds = max(
        0, int(getattr(config, "ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS", 0) or 0)
    )
    enrichment_started = time.monotonic()

    for index, (company_key, company_jobs) in enumerate(company_items, 1):
        if (
            enrichment_budget_seconds
            and time.monotonic() - enrichment_started >= enrichment_budget_seconds
        ):
            stop_reason = "enrichment_runtime_budget_reached"
            total_stats["enrichment_runtime_budget_reached"] = 1
            logger.warning(
                "Enrichment runtime budget of %ds reached after %d/%d companies; "
                "preserving checkpointed work and stopping (resumable).",
                enrichment_budget_seconds, companies_considered, total_candidate_companies,
            )
            break
        cached = progress.get(company_key)
        if cached is not None:
            leads, stats = cached
            total_stats["enrichment_resume_reused_companies"] += 1
            logger.info(
                "[%d/%d] Reusing checkpointed enrichment for %s",
                index, total_candidate_companies, company_key,
            )
        else:
            logger.info("[%d/%d] Enriching %s", index, total_candidate_companies, company_key)
            try:
                leads, stats = process_company(company_jobs)
            except apollo.GLOBAL_FATAL_ERRORS as exc:
                # Apollo is unusable for the WHOLE run. Open the circuit, preserve
                # every company already completed, and stop cleanly with a
                # reconciled stop reason -- never crash the pipeline. The company
                # that tripped the circuit is left unprocessed (not checkpointed)
                # so a later run retries it once Apollo recovers.
                circuit_reason = _apollo_circuit_reason(exc)
                logger.error(
                    "Apollo unavailable for the whole run (%s) after %d companies; "
                    "opening the Apollo circuit and preserving completed work: %s",
                    circuit_reason, companies_considered, exc,
                )
                total_stats["apollo_circuit_open"] = 1
                total_stats[f"apollo_circuit_reason__{circuit_reason}"] = 1
                stop_reason = "apollo_circuit_open"
                break
            except Exception as exc:  # noqa: BLE001 - one company never aborts the run
                # Record-level failure for THIS company only: mark UNVERIFIED and
                # continue with the rest of the batch. Not checkpointed, so it
                # stays recoverable on a later run.
                logger.warning(
                    "Apollo enrichment failed for %s: %s; marking UNVERIFIED and continuing.",
                    company_key, exc,
                )
                total_stats["apollo_company_record_failures"] += 1
                leads, stats = _degraded_company_leads(
                    company_jobs, reason="apollo_record_error"
                )
            else:
                progress.record(company_key, leads, stats)
        companies_considered += 1
        processed_company_keys.append(company_key)
        processed_jobs.extend(company_jobs)
        all_leads.extend(leads)
        for key, value in stats.items():
            total_stats[key] += value

        if strict_input:
            company_account_pass = any(
                lead.get("_account_gate_state") in {
                    GateState.PASS.value,
                    GateState.NEEDS_CHECK.value,
                    GateState.UNVERIFIED.value,
                }
                for lead in leads
            )
            if company_account_pass:
                eligible_companies += 1
            else:
                excluded_companies += 1
        else:
            company_is_excluded = bool(leads) and all(
                lead.get("_step3_status") == "excluded" for lead in leads
            )
            if company_is_excluded:
                excluded_companies += 1
            else:
                eligible_companies += 1

        reviewable_leads = _count_unique_reviewable_leads(all_leads)
        final_pass_leads = _count_unique_final_pass_leads(all_leads)

        if (
            strict_input
            and target_final_pass_leads is not None
            # Reviewable rows must never satisfy a FINAL_PASS target. This
            # compared reviewable_leads against target_final_pass_leads, which
            # is the defect that let production report
            # final_pass_target_reached at FINAL_PASS=15 against a target of 30
            # because the review surface had reached 30. The counterpart in
            # final_pass_topup.py:356-362 was already hardened for the same
            # incident; this site was not.
            and final_pass_leads >= target_final_pass_leads
            and not config.CONTINUE_AFTER_FINAL_PASS_TARGET
        ):
            stop_reason = "final_pass_target_reached"
            logger.info(
                "Reached daily target of %d FINAL_PASS leads after considering %d companies "
                "(%d reviewable rows, reported separately)",
                target_final_pass_leads,
                companies_considered,
                reviewable_leads,
            )
            break
        if (
            not strict_input
            and target_reviewable_leads is not None
            and reviewable_leads >= target_reviewable_leads
        ):
            stop_reason = "reviewable_lead_target_reached"
            logger.info(
                "Reached legacy target of %d reviewable leads after considering %d companies",
                target_reviewable_leads,
                companies_considered,
            )
            break
        if target_eligible_companies is not None and eligible_companies >= target_eligible_companies:
            stop_reason = "eligible_company_target_reached"
            break
        if max_eligible_companies is not None and eligible_companies >= max_eligible_companies:
            stop_reason = "eligible_company_safety_cap_reached"
            logger.warning(
                "Reached safety cap of %d eligible companies with %d FINAL_PASS and %d review rows",
                max_eligible_companies,
                final_pass_leads,
                reviewable_leads,
            )
            break

    excluded_buckets = sum(1 for lead in all_leads if lead.get("_step3_status") == "excluded")
    eligible_leads = [lead for lead in all_leads if lead.get("_step3_status") != "excluded"]
    eligible_buckets = len(eligible_leads)
    identified = sum(1 for lead in eligible_leads if lead.get("hiring_manager_name"))
    not_identified = eligible_buckets - identified
    contactable = sum(1 for lead in eligible_leads if lead.get("_step3_status") == "found")
    uncontactable = eligible_buckets - contactable
    match_rate = identified / eligible_buckets if eligible_buckets else 0.0
    contactable_rate = contactable / eligible_buckets if eligible_buckets else 0.0
    reviewable_leads = _count_unique_reviewable_leads(all_leads)
    final_pass_leads = _count_unique_final_pass_leads(all_leads)
    state_counts = _final_state_counts(all_leads)

    review_target = target_final_pass_leads if strict_input else target_reviewable_leads
    reviewable_target_reached = (
        review_target is None or reviewable_leads >= review_target
    )
    final_pass_target_reached = (
        target_final_pass_leads is not None
        and final_pass_leads >= target_final_pass_leads
    )
    eligible_target_reached = (
        target_eligible_companies is None or eligible_companies >= target_eligible_companies
    )
    eligible_company_limit_reached = (
        max_eligible_companies is not None and eligible_companies >= max_eligible_companies
    )
    if strict_input:
        target_reached = reviewable_target_reached
    elif target_reviewable_leads is not None:
        target_reached = reviewable_target_reached
    else:
        target_reached = eligible_target_reached

    suffix = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(output_suffix or "").strip())
    suffix_part = f"_{suffix}" if suffix else ""
    output_path = str(
        Path(config.STEP3_OUTPUT_DIR)
        / f"jobs_enriched_{datetime.now():%Y-%m-%d}{suffix_part}.json"
    )
    output_payload = {
        "run_date": datetime.now().isoformat(),
        "validation_version": config.VALIDATION_VERSION,
        "strict_final_pass_mode": strict_input,
        "source_file": input_path,
        "source_total_jobs": len(jobs),
        "total_input_jobs": len(processed_jobs),
        "total_output_leads": len(all_leads),
        "companies_considered": companies_considered,
        # Company-level CRM / active-pipeline exclusions applied before any
        # Apollo/Hunter call. Surfaced per company so a run is auditable.
        "crm_excluded_companies": len(crm_excluded_company_keys),
        "crm_excluded_jobs": crm_excluded_jobs,
        "crm_excluded_company_reasons": dict(sorted(crm_excluded_reasons.items())),
        "eligible_companies": eligible_companies,
        # "eligible_companies" means "not hard-rejected by firmographics/
        # industry/business-model" -- it does NOT mean the company could
        # actually reach a people search. icp_pass_companies is the same
        # number under an honest name; lead_capable_companies is the number
        # that were both ICP-eligible AND had a resolvable search domain
        # (ROOT_CAUSE_TABLE_STRUCTURAL.md row 6 / TECHNICAL_DESIGN.md D8).
        "icp_pass_companies": eligible_companies,
        "lead_capable_companies": total_stats.get("row2_companies_with_people_search_call", 0),
        "company_criteria_excluded_companies": excluded_companies,
        "target_eligible_companies": target_eligible_companies,
        "target_reviewable_leads": target_reviewable_leads,
        "reviewable_leads": reviewable_leads,
        "reviewable_target_reached": reviewable_target_reached,
        "final_pass_target": target_final_pass_leads,
        "final_pass_leads": final_pass_leads,
        "final_pass_target_reached": final_pass_target_reached,
        "needs_check_leads": state_counts["NEEDS_CHECK"],
        "reroute_leads": state_counts["REROUTE"],
        "unverified_leads": state_counts["UNVERIFIED"],
        "rejected_leads": state_counts["REJECT"],
        "final_state_counts": state_counts,
        "max_eligible_companies": max_eligible_companies,
        "eligible_company_limit_reached": eligible_company_limit_reached,
        "stop_reason": stop_reason,
        "target_reached": target_reached,
        "company_criteria_excluded": excluded_buckets,
        "eligible_company_buckets": eligible_buckets,
        "hiring_manager_identified": identified,
        "hiring_manager_not_identified": not_identified,
        "hiring_manager_identification_rate": round(match_rate, 4),
        "contactable_hiring_managers": contactable,
        "uncontactable_hiring_managers": uncontactable,
        "contactable_rate": round(contactable_rate, 4),
        "processed_job_refs": [_job_state_ref(job) for job in processed_jobs],
        "processed_company_keys": processed_company_keys,
        "hiring_manager_found": identified,
        "hiring_manager_not_found": not_identified,
        "match_rate": round(match_rate, 4),
        "stats": dict(total_stats),
        "jobs": all_leads,
    }
    Path(output_path).write_text(json.dumps(output_payload, indent=2), encoding="utf-8")

    # Permanent per-run HM + multi-function observability (non-PII). Emitting
    # these here means the failure population, multi-function handling, and
    # coverage-by-bucket are always reconstructable from artifacts + logs,
    # without ever mounting the volume by hand. Never fatal to the run.
    hm_observability: Dict[str, Any] = {}
    try:
        import hm_observability as _hm_obs
        _obs = _hm_obs.write_run_artifacts(all_leads, config.STEP3_OUTPUT_DIR)
        hm_observability = {
            "hiring_manager": _obs["hiring_manager"],
            "multi_function": _obs["multi_function"],
            "domain_resolution": _obs["domain_resolution"],
        }
        for _line in _hm_obs.stdout_summary(_obs["hiring_manager"], _obs["multi_function"],
                                            _obs["domain_resolution"]):
            logger.info(_line)
    except Exception as exc:  # noqa: BLE001 - observability must never break a run
        logger.warning("hm_observability artifact emission failed (non-fatal): %s", exc)

    errors: List[str] = []
    if config.ENFORCE_HM_MATCH_RATE and eligible_buckets and match_rate < config.MIN_HIRING_MANAGER_MATCH_RATE:
        errors.append(
            f"Hiring-manager match rate {match_rate:.1%} is below {config.MIN_HIRING_MANAGER_MATCH_RATE:.1%}"
        )

    return Step3Result(
        output_path=output_path,
        total_input_jobs=len(processed_jobs),
        total_output_leads=len(all_leads),
        company_criteria_excluded=excluded_buckets,
        hiring_manager_found=identified,
        hiring_manager_not_found=not_identified,
        match_rate=match_rate,
        contactable_hiring_managers=contactable,
        uncontactable_hiring_managers=uncontactable,
        contactable_rate=contactable_rate,
        companies_considered=companies_considered,
        eligible_companies=eligible_companies,
        icp_pass_companies=eligible_companies,
        lead_capable_companies=total_stats.get("row2_companies_with_people_search_call", 0),
        company_criteria_excluded_companies=excluded_companies,
        target_eligible_companies=target_eligible_companies,
        target_reviewable_leads=target_reviewable_leads,
        reviewable_leads=reviewable_leads,
        reviewable_target_reached=reviewable_target_reached,
        final_pass_target=target_final_pass_leads,
        final_pass_leads=final_pass_leads,
        needs_check_leads=state_counts["NEEDS_CHECK"],
        reroute_leads=state_counts["REROUTE"],
        unverified_leads=state_counts["UNVERIFIED"],
        rejected_leads=state_counts["REJECT"],
        final_pass_target_reached=final_pass_target_reached,
        max_eligible_companies=max_eligible_companies,
        eligible_company_limit_reached=eligible_company_limit_reached,
        target_reached=target_reached,
        stop_reason=stop_reason,
        processed_company_keys=processed_company_keys,
        stats=dict(total_stats),
        hm_observability=hm_observability,
        success=not errors,
        errors=errors,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_hiring_manager_identification()
