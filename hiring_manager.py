"""Step 3: enrich companies and identify one decision-maker per company/bucket."""

from __future__ import annotations

import json
import logging
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
import hunter_client as hunter
from company_identity import (
    company_names_compatible,
    domains_equivalent,
    email_matches_company,
    is_intermediary_domain,
    safe_company_domain,
)
from job_filter import extract_domain, get_safe_employer_domain, normalize_text
from job_signal import annotate_job
from role_focus import extract_role_focus
from role_mapping import (
    get_bucket_name_for_job,
    get_hiring_manager_bucket_for_job,
    get_target_titles_for_jobs,
)

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
    company_criteria_excluded_companies: int = 0
    target_eligible_companies: Optional[int] = None
    target_reviewable_leads: Optional[int] = None
    reviewable_leads: int = 0
    reviewable_target_reached: bool = True
    max_eligible_companies: Optional[int] = None
    eligible_company_limit_reached: bool = False
    target_reached: bool = True
    stop_reason: str = "candidate_pool_exhausted"
    processed_company_keys: List[str] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)
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


def _is_intermediary_domain(domain: str) -> bool:
    return is_intermediary_domain(domain, config.INTERMEDIARY_JOB_DOMAINS)


def _domain_from_apply_link(job: Dict) -> str:
    """Recover a company domain only from safe, direct application URLs."""
    apply_link = (job.get("job_apply_link") or "").strip()
    if not apply_link:
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

    if (
        config.ENFORCE_FOUNDED_BEFORE
        and org.founded_year is not None
        and org.founded_year >= config.FOUNDED_BEFORE_YEAR
    ):
        return False, f"founded_too_recent:{org.founded_year}", False

    if config.ENFORCE_FOUNDED_BEFORE and org.founded_year is None:
        return True, "unknown_founded_year", True

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


def process_company(company_jobs: List[Dict]) -> Tuple[List[Dict], Dict]:
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
                person.first_name
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


def _is_reviewable_lead(lead: Dict) -> bool:
    """Mirror Airtable's reviewable-lead gate without making an API call."""
    return bool(
        lead.get("_step3_status") == "found"
        and lead.get("hiring_manager_confidence") in {"high", "medium", "low"}
        and lead.get("hiring_manager_email")
        and lead.get("lead_key")
    )


def _count_unique_reviewable_leads(leads: List[Dict]) -> int:
    return len(
        {
            str(lead.get("lead_key"))
            for lead in leads
            if _is_reviewable_lead(lead)
        }
    )


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


def run_hiring_manager_identification(
    input_path: Optional[str] = None,
    *,
    target_eligible_companies: Optional[int] = None,
    target_reviewable_leads: Optional[int] = None,
    max_eligible_companies: Optional[int] = None,
    exclude_company_keys: Optional[set[str]] = None,
    output_suffix: Optional[str] = None,
) -> Step3Result:
    """Run Step 3 with optional controlled-test and production stop conditions.

    ``target_eligible_companies`` is used by the controlled test runner, where
    ``--companies 10`` means ten eligible companies after firmographic checks.

    ``target_reviewable_leads`` is the daily production goal. A reviewable lead
    is a unique company/bucket contact that passes the same gate used by the
    Airtable writer: usable email, confidence, and lead key.

    ``max_eligible_companies`` is a production safety cap. It bounds enrichment
    usage on days when contactability is low. The target is a goal, not a hard
    promise: the available filtered market may produce fewer leads, and a final
    company with multiple buckets can make the count slightly exceed the target.
    """
    validate_preflight()
    for name, value in (
        ("target_eligible_companies", target_eligible_companies),
        ("target_reviewable_leads", target_reviewable_leads),
        ("max_eligible_companies", max_eligible_companies),
    ):
        if value is not None and value < 1:
            raise ValueError(f"{name} must be at least 1")

    input_path = input_path or config.STEP2_KEPT_FILE
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    jobs = payload.get("jobs", [])

    jobs_by_company: Dict[str, List[Dict]] = defaultdict(list)
    excluded_company_keys = {str(value) for value in (exclude_company_keys or set()) if value}
    skipped_existing_company_keys: set[str] = set()
    skipped_existing_job_rows: List[Dict] = []
    skipped_existing_jobs = 0
    for job in jobs:
        company_key = company_key_for_job(job)
        if company_key in excluded_company_keys:
            skipped_existing_company_keys.add(company_key)
            skipped_existing_job_rows.append(job)
            skipped_existing_jobs += 1
            continue
        jobs_by_company[company_key].append(job)

    all_leads: List[Dict] = []
    # Same-run company duplicates are intentionally consumed even though they do
    # not trigger another Apollo search; otherwise their new job IDs would recur
    # on the next day and waste top-up budget again.
    processed_jobs: List[Dict] = list(skipped_existing_job_rows)
    total_stats = defaultdict(int)
    total_stats["topup_skipped_previously_considered_companies"] = len(
        skipped_existing_company_keys
    )
    total_stats["topup_skipped_previously_considered_jobs"] = skipped_existing_jobs
    companies_considered = 0
    eligible_companies = 0
    excluded_companies = 0
    company_items = list(jobs_by_company.items())
    company_items.sort(key=_company_priority, reverse=True)
    total_candidate_companies = len(company_items)
    stop_reason = "candidate_pool_exhausted"
    processed_company_keys: List[str] = []

    for index, (company_key, company_jobs) in enumerate(company_items, 1):
        logger.info("[%d/%d] Enriching %s", index, total_candidate_companies, company_key)
        leads, stats = process_company(company_jobs)
        companies_considered += 1
        processed_company_keys.append(company_key)
        processed_jobs.extend(company_jobs)
        all_leads.extend(leads)
        for key, value in stats.items():
            total_stats[key] += value

        company_is_excluded = bool(leads) and all(
            lead.get("_step3_status") == "excluded" for lead in leads
        )
        if company_is_excluded:
            excluded_companies += 1
        else:
            eligible_companies += 1

        reviewable_leads = _count_unique_reviewable_leads(all_leads)

        if (
            target_reviewable_leads is not None
            and reviewable_leads >= target_reviewable_leads
        ):
            stop_reason = "reviewable_lead_target_reached"
            logger.info(
                "Reached daily target of %d reviewable leads after considering %d "
                "companies (%d eligible)",
                target_reviewable_leads,
                companies_considered,
                eligible_companies,
            )
            break

        if (
            target_eligible_companies is not None
            and eligible_companies >= target_eligible_companies
        ):
            stop_reason = "eligible_company_target_reached"
            logger.info(
                "Reached controlled-test target of %d eligible companies after "
                "considering %d companies",
                target_eligible_companies,
                companies_considered,
            )
            break

        if (
            max_eligible_companies is not None
            and eligible_companies >= max_eligible_companies
        ):
            stop_reason = "eligible_company_safety_cap_reached"
            logger.warning(
                "Reached safety cap of %d eligible companies with %d reviewable "
                "leads after considering %d companies",
                max_eligible_companies,
                reviewable_leads,
                companies_considered,
            )
            break

    excluded_buckets = sum(1 for lead in all_leads if lead.get("_step3_status") == "excluded")
    eligible_leads = [lead for lead in all_leads if lead.get("_step3_status") != "excluded"]
    eligible_buckets = len(eligible_leads)

    # "Hiring manager identified" and "usable email found" are separate
    # success metrics. A person can be correctly identified even when Apollo and
    # Hunter cannot reveal a deliverable email.
    identified = sum(1 for lead in eligible_leads if lead.get("hiring_manager_name"))
    not_identified = eligible_buckets - identified
    contactable = sum(1 for lead in eligible_leads if lead.get("_step3_status") == "found")
    uncontactable = eligible_buckets - contactable
    match_rate = identified / eligible_buckets if eligible_buckets else 0.0
    contactable_rate = contactable / eligible_buckets if eligible_buckets else 0.0
    reviewable_leads = _count_unique_reviewable_leads(all_leads)

    reviewable_target_reached = (
        target_reviewable_leads is None
        or reviewable_leads >= target_reviewable_leads
    )
    eligible_target_reached = (
        target_eligible_companies is None
        or eligible_companies >= target_eligible_companies
    )
    eligible_company_limit_reached = (
        max_eligible_companies is not None
        and eligible_companies >= max_eligible_companies
    )
    target_reached = (
        reviewable_target_reached
        if target_reviewable_leads is not None
        else eligible_target_reached
    )

    suffix = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(output_suffix or "").strip())
    suffix_part = f"_{suffix}" if suffix else ""
    output_path = str(
        Path(config.STEP3_OUTPUT_DIR)
        / f"jobs_enriched_{datetime.now():%Y-%m-%d}{suffix_part}.json"
    )
    Path(output_path).write_text(
        json.dumps(
            {
                "run_date": datetime.now().isoformat(),
                "source_file": input_path,
                "source_total_jobs": len(jobs),
                "total_input_jobs": len(processed_jobs),
                "total_output_leads": len(all_leads),
                "companies_considered": companies_considered,
                "eligible_companies": eligible_companies,
                "company_criteria_excluded_companies": excluded_companies,
                "target_eligible_companies": target_eligible_companies,
                "target_reviewable_leads": target_reviewable_leads,
                "reviewable_leads": reviewable_leads,
                "reviewable_target_reached": reviewable_target_reached,
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
                # Only these processed jobs should enter cross-day seen-state.
                # Jobs left unprocessed because a daily target/cap was reached
                # remain eligible for a later run.
                "processed_job_refs": [_job_state_ref(job) for job in processed_jobs],
                "processed_company_keys": processed_company_keys,
                # Backward-compatible aliases.
                "hiring_manager_found": identified,
                "hiring_manager_not_found": not_identified,
                "match_rate": round(match_rate, 4),
                "stats": dict(total_stats),
                "jobs": all_leads,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    errors: List[str] = []
    if config.ENFORCE_HM_MATCH_RATE and eligible_buckets and match_rate < config.MIN_HIRING_MANAGER_MATCH_RATE:
        errors.append(
            f"Hiring-manager match rate {match_rate:.1%} is below "
            f"{config.MIN_HIRING_MANAGER_MATCH_RATE:.1%}"
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
        company_criteria_excluded_companies=excluded_companies,
        target_eligible_companies=target_eligible_companies,
        target_reviewable_leads=target_reviewable_leads,
        reviewable_leads=reviewable_leads,
        reviewable_target_reached=reviewable_target_reached,
        max_eligible_companies=max_eligible_companies,
        eligible_company_limit_reached=eligible_company_limit_reached,
        target_reached=target_reached,
        stop_reason=stop_reason,
        processed_company_keys=processed_company_keys,
        stats=dict(total_stats),
        success=not errors,
        errors=errors,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_hiring_manager_identification()
