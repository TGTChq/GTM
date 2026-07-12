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
from job_filter import extract_domain, normalize_text
from job_signal import annotate_job
from role_focus import extract_role_focus
from role_mapping import get_bucket_name_for_job, get_target_titles_for_job

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
    target_reached: bool = True
    stats: Dict = field(default_factory=dict)
    success: bool = True
    errors: List[str] = field(default_factory=list)


def validate_preflight() -> None:
    if not config.APOLLO_API_KEY:
        raise ValueError("APOLLO_API_KEY is missing from .env")
    if config.VERIFY_WITH_HUNTER and not config.HUNTER_API_KEY:
        logger.warning("HUNTER_API_KEY is missing; Hunter verification/fallback is disabled")


def _is_intermediary_domain(domain: str) -> bool:
    domain = (domain or "").lower().strip(".")
    return any(
        domain == blocked or domain.endswith("." + blocked)
        for blocked in config.INTERMEDIARY_JOB_DOMAINS
    )


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
    return extract_domain(job.get("employer_website") or "") or _domain_from_apply_link(job)


def passes_company_criteria(org: apollo.OrgEnrichment) -> Tuple[bool, str, bool]:
    """Return (eligible, reason, needs_manual_review)."""
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


def pick_best_candidate(people: List[Dict], target_titles: List[str]) -> Optional[Dict]:
    if not people:
        return None

    ranked = sorted(
        people,
        key=lambda person: (
            _title_priority(person.get("title") or "", target_titles)[0],
            -_title_priority(person.get("title") or "", target_titles)[1],
            not bool(person.get("linkedin_url")),
        ),
    )
    best = ranked[0]
    _, quality = _title_priority(best.get("title") or "", target_titles)
    return best if quality > 0 else None


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
            "company_domain": org.domain or extract_domain(primary.get("employer_website") or ""),
            "hiring_manager_confidence": "none",
        }
    )
    return lead


def process_company(company_jobs: List[Dict]) -> Tuple[List[Dict], Dict]:
    stats = defaultdict(int)
    first = company_jobs[0]
    raw_website = first.get("employer_website") or ""
    input_domain = _best_input_domain(first)
    company_name = first.get("employer_name") or ""
    # Never pass a bare company name or a noisy subdomain as Apollo's website.
    # Apollo can still resolve by company name when no valid domain is available.
    enrichment_website = f"https://{input_domain}" if input_domain else ""

    org = apollo.enrich_organization(
        domain=input_domain, name=company_name, website=enrichment_website
    )
    time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
    search_domain = org.domain or input_domain

    eligible, company_reason, company_needs_review = passes_company_criteria(org)

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
            continue

        target_titles = get_target_titles_for_job(
            primary, org.employee_count, bucket_override=bucket
        )
        people = apollo.search_people_at_company(search_domain, target_titles)
        time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
        candidate = pick_best_candidate(people, target_titles)

        if not candidate:
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
            continue

        person = apollo.match_person(candidate)
        time.sleep(config.APOLLO_RATE_LIMIT_DELAY)
        hunter_result: Optional[hunter.HunterResult] = None

        if person.email and config.VERIFY_WITH_HUNTER and config.HUNTER_API_KEY:
            hunter_result = hunter.verify_email(person.email)
            time.sleep(config.HUNTER_RATE_LIMIT_DELAY)
        elif (
            not person.email
            and person.first_name
            and person.last_name
            and config.HUNTER_API_KEY
        ):
            hunter_result = hunter.find_email(person.first_name, person.last_name, search_domain)
            time.sleep(config.HUNTER_RATE_LIMIT_DELAY)
            if hunter_result.found and hunter_result.email:
                person.email = hunter_result.email
                person.email_found = True
                person.email_source = "hunter"

        confidence = _email_confidence(person, hunter_result)
        found = confidence not in {"none", "invalid"}
        role_focus = extract_role_focus(
            primary, primary.get("_matched_role", "")
        )

        lead = dict(primary)
        lead.update(
            {
                "_role_bucket": bucket,
                "_step3_status": "found" if found else "not_found",
                "_step3_reason": "email_invalid" if confidence == "invalid" else ("contact_found" if found else "no_usable_email"),
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
                "hiring_manager_email": person.email,
                "hiring_manager_email_source": person.email_source,
                "apollo_email_status": person.email_status,
                "hunter_email_status": hunter_result.status if hunter_result else None,
                "hiring_manager_confidence": confidence,
                "campaign_id": config.resolve_campaign_id(bucket, org.employee_count),
            }
        )
        if person.email:
            lead["lead_key"] = _lead_key(search_domain, person.email, bucket)
        # Freshness and URL quality are evaluated only for contactable leads,
        # because those are the records that enter the Airtable review queue.
        # Older/aggregator-sourced jobs are kept, but clearly flagged.
        lead = annotate_job(lead, probe_url=found)
        leads.append(lead)
        stats[f"bucket_{bucket}_{'found' if found else 'not_found'}"] += 1

    return leads, dict(stats)


def run_hiring_manager_identification(
    input_path: Optional[str] = None,
    *,
    max_eligible_companies: Optional[int] = None,
) -> Step3Result:
    """Run Step 3, optionally stopping after N eligible companies.

    ``max_eligible_companies`` is used by the controlled test runner.  Companies
    rejected by firmographic criteria do not count toward the target, so
    ``--companies 10`` now means ten eligible companies whenever the filtered
    candidate pool is large enough.
    """
    validate_preflight()
    if max_eligible_companies is not None and max_eligible_companies < 1:
        raise ValueError("max_eligible_companies must be at least 1")

    input_path = input_path or config.STEP2_KEPT_FILE
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    jobs = payload.get("jobs", [])

    jobs_by_company: Dict[str, List[Dict]] = defaultdict(list)
    for job in jobs:
        domain = _best_input_domain(job)
        company_key = domain or normalize_text(job.get("employer_name") or "unknown")
        jobs_by_company[company_key].append(job)

    all_leads: List[Dict] = []
    processed_jobs: List[Dict] = []
    total_stats = defaultdict(int)
    companies_considered = 0
    eligible_companies = 0
    excluded_companies = 0
    total_candidate_companies = len(jobs_by_company)

    for index, (company_key, company_jobs) in enumerate(jobs_by_company.items(), 1):
        logger.info("[%d/%d] Enriching %s", index, total_candidate_companies, company_key)
        leads, stats = process_company(company_jobs)
        companies_considered += 1
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

        if (
            max_eligible_companies is not None
            and eligible_companies >= max_eligible_companies
        ):
            logger.info(
                "Reached target of %d eligible companies after considering %d companies",
                max_eligible_companies,
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
    target_reached = (
        max_eligible_companies is None
        or eligible_companies >= max_eligible_companies
    )

    output_path = str(Path(config.STEP3_OUTPUT_DIR) / f"jobs_enriched_{datetime.now():%Y-%m-%d}.json")
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
                "target_eligible_companies": max_eligible_companies,
                "target_reached": target_reached,
                "company_criteria_excluded": excluded_buckets,
                "eligible_company_buckets": eligible_buckets,
                "hiring_manager_identified": identified,
                "hiring_manager_not_identified": not_identified,
                "hiring_manager_identification_rate": round(match_rate, 4),
                "contactable_hiring_managers": contactable,
                "uncontactable_hiring_managers": uncontactable,
                "contactable_rate": round(contactable_rate, 4),
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
        target_eligible_companies=max_eligible_companies,
        target_reached=target_reached,
        stats=dict(total_stats),
        success=not errors,
        errors=errors,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_hiring_manager_identification()
