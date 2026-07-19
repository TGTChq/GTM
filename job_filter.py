"""Step 2: business filters, CRM exclusion, geography, and deduplication."""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

from company_identity import safe_company_domain
from domain_utils import normalize_company_domain

import config
from job_quality import assess_quality_guard, normalize_job_identity
from job_signal import classify_freshness
from pipeline_state import SeenJobsRegistry

logger = logging.getLogger(__name__)

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|s\.?r\.?l\.?)\b",
    re.I,
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r"\s+")


@dataclass
class FilterResult:
    output_path: str
    rejected_path: str
    kept_count: int
    rejected_count: int
    stats: Dict
    success: bool = True
    errors: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkArrangementEvidence:
    status: str
    reason: str


@dataclass(frozen=True)
class GeographyEvidence:
    eligible: bool
    reason: str
    display_location: str
    scope: str


@dataclass(frozen=True)
class EmploymentEvidence:
    eligible: bool
    reason: str
    classification: str


@dataclass(frozen=True)
class PreEnrichmentAssessment:
    """Local, zero-credit eligibility signal used by Step 1 and Step 2."""

    eligible: bool
    stat_name: str
    reason: str
    work_arrangement: WorkArrangementEvidence
    geography: GeographyEvidence
    employment: EmploymentEvidence
    employer_domain: str
    employer_domain_source: str


def normalize_text(value: str) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii").lower().strip()
    text = _NON_ALNUM.sub(" ", text)
    text = _COMPANY_SUFFIXES.sub(" ", text)
    return _MULTI_SPACE.sub(" ", text).strip()


def normalize_compact(value: str) -> str:
    return normalize_text(value).replace(" ", "")


def normalize_title(title: str) -> str:
    return normalize_text(title)


def dedup_key(job: Dict) -> Tuple[str, str]:
    domain = get_safe_employer_domain(job)[0]
    company = domain or normalize_text(job.get("employer_name", ""))
    return company, normalize_title(job.get("job_title", ""))


def extract_domain(value: str) -> str:
    """Return a validated, normalized root company domain.

    Noisy values such as ``investor.capitalone.com`` are reduced to
    ``capitalone.com``. Bare company names such as ``google`` or
    ``the mitre`` are rejected rather than being treated as domains.
    """
    return normalize_company_domain(value)


def _contains_keyword(text: str, keyword: str) -> bool:
    return re.search(r"\b" + re.escape(normalize_text(keyword)) + r"\b", text) is not None


def _domain_is_intermediary(domain: str) -> bool:
    normalized = extract_domain(domain)
    return bool(normalized and not safe_company_domain(
        normalized, config.INTERMEDIARY_JOB_DOMAINS
    ))


def get_safe_employer_domain(job: Dict) -> Tuple[str, str]:
    """Return a company-owned domain and the evidence source used.

    Publisher, aggregator, and ATS domains are deliberately ignored. When the
    provider gives no safe domain, Step 3 resolves the company by employer name
    and validates Apollo's returned organization name before using its domain.
    """
    candidates: List[Tuple[str, str]] = [
        (job.get("employer_website") or "", "employer_website"),
    ]
    for option in job.get("apply_options") or []:
        if isinstance(option, dict):
            candidates.append((option.get("apply_link") or "", "apply_option"))
    candidates.append((job.get("job_apply_link") or "", "job_apply_link"))

    for value, source in candidates:
        domain = safe_company_domain(value, config.INTERMEDIARY_JOB_DOMAINS)
        if domain:
            return domain, source
    return "", "employer_name_resolution_required"


def is_job_aggregator_or_publisher(job: Dict) -> Tuple[bool, str]:
    """Reject job boards/publishers masquerading as the employer.

    JSearch occasionally returns a publisher brand in ``employer_name`` rather
    than the actual hiring company. Known aggregators are rejected directly.
    Generic names such as ``Something Jobs`` require corroborating evidence so
    legitimate companies are not removed on a single weak keyword.
    """
    employer_norm = normalize_text(job.get("employer_name", "") or "")
    publisher_norm = normalize_text(job.get("job_publisher", "") or "")
    employer_domain = extract_domain(job.get("employer_website") or "")
    apply_domain = extract_domain(job.get("job_apply_link") or "")
    description = normalize_text((job.get("job_description") or "")[:4000])

    for known in config.KNOWN_JOB_AGGREGATOR_EMPLOYERS:
        known_norm = normalize_text(known)
        if employer_norm == known_norm or (
            len(known_norm) >= 6 and re.search(r"\b" + re.escape(known_norm) + r"\b", employer_norm)
        ):
            return True, f"known_job_aggregator:{known}"

    generic_name = any(
        re.search(pattern, employer_norm, re.I)
        for pattern in config.GENERIC_JOB_PUBLISHER_NAME_PATTERNS
    )
    if not generic_name:
        return False, ""

    corroborating_signals: List[str] = []
    if not employer_domain:
        corroborating_signals.append("missing_employer_domain")
    if publisher_norm and publisher_norm == employer_norm:
        corroborating_signals.append("publisher_matches_employer")
    if apply_domain and _domain_is_intermediary(apply_domain):
        corroborating_signals.append("intermediary_apply_domain")
    if any(normalize_text(phrase) in description for phrase in config.JOB_AGGREGATOR_DESCRIPTION_PHRASES):
        corroborating_signals.append("aggregator_description")

    if corroborating_signals:
        return True, "generic_job_publisher:" + ",".join(corroborating_signals)
    return False, ""


def is_stale_job(job: Dict) -> Tuple[bool, str]:
    """Reject only clearly stale job-intent signals before enrichment.

    JSearch can occasionally surface an old syndicated listing inside a
    recent-date query. We use the oldest parseable posting signal in the
    payload. Missing/unparseable dates are retained rather than rejected.
    """
    _freshness, age_days, reason = classify_freshness(job)
    if reason == "explicit_expiration_is_in_the_past":
        return True, "expired_job_posting"
    if age_days is not None and age_days >= config.MAX_JOB_AGE_DAYS:
        return True, f"stale_job:{age_days}days"
    return False, ""


def is_staffing_company(job: Dict) -> Tuple[bool, str]:
    employer = job.get("employer_name", "") or ""
    employer_norm = normalize_text(employer)

    for marketplace in config.FREELANCE_MARKETPLACE_EMPLOYERS:
        marketplace_norm = normalize_text(marketplace)
        if employer_norm == marketplace_norm or _contains_keyword(employer_norm, marketplace_norm):
            return True, f"freelance_marketplace:{marketplace}"

    for known in config.KNOWN_STAFFING_EMPLOYERS:
        known_norm = normalize_text(known)
        if employer_norm == known_norm or _contains_keyword(employer_norm, known_norm):
            return True, f"known_staffing_employer:{known}"

    for keyword in config.STAFFING_EMPLOYER_KEYWORDS:
        if _contains_keyword(employer_norm, keyword):
            return True, f"staffing_keyword_in_employer:{keyword}"

    description = normalize_text(job.get("job_description") or "")
    if any(normalize_text(phrase) in description for phrase in config.STAFFING_NEGATION_PHRASES):
        return False, ""

    employer_is_vague = any(
        normalize_text(signal) in employer_norm for signal in config.VAGUE_EMPLOYER_SIGNALS
    )
    for phrase in config.STAFFING_DESCRIPTION_PHRASES:
        normalized_phrase = normalize_text(phrase)
        if normalized_phrase in description:
            # Strong first-person phrases are enough; generic signals require a vague employer.
            is_first_person = normalized_phrase.startswith(("we are", "our ", "we place", "we connect"))
            is_explicit_intermediary = normalized_phrase in {"as an agency worker"}
            if (
                is_first_person
                or is_explicit_intermediary
                or employer_is_vague
                or normalized_phrase.startswith("on behalf")
            ):
                return True, f"staffing_phrase_in_description:{phrase}"

    return False, ""


def is_excluded_industry(job: Dict) -> Tuple[bool, str]:
    if not config.ENABLE_BROADER_INDUSTRY_EXCLUSIONS:
        return False, ""

    employer_norm = normalize_text(job.get("employer_name", "") or "")
    title_norm = normalize_text(job.get("job_title", "") or "")
    website = (job.get("employer_website") or "").lower()
    apply_domain = extract_domain(job.get("job_apply_link") or "")

    for keyword in config.EXCLUDED_INDUSTRY_JOB_TITLE_KEYWORDS:
        if _contains_keyword(title_norm, keyword):
            return True, f"excluded_industry_job_title:{keyword}"

    for keyword in config.EXCLUDED_INDUSTRY_EMPLOYER_KEYWORDS:
        if _contains_keyword(employer_norm, keyword):
            return True, f"excluded_industry_employer:{keyword}"

    if any(marker in website for marker in config.GOVERNMENT_WEBSITE_MARKERS):
        return True, "excluded_industry_government_website"
    if any(domain in apply_domain for domain in config.GOVERNMENT_JOB_BOARD_DOMAINS):
        return True, f"excluded_industry_government_board:{apply_domain}"

    for keyword in config.EXCLUDED_MEDIA_PRODUCTION_KEYWORDS:
        if _contains_keyword(employer_norm, keyword):
            return True, f"excluded_industry_media_production:{keyword}"

    description = (job.get("job_description") or "")[:5000]
    for pattern in config.EXCLUDED_INDUSTRY_DESCRIPTION_PATTERNS:
        if re.search(pattern, description, re.I):
            return True, f"excluded_industry_description:{pattern}"

    # High-confidence nonprofit/religious organization signal. A .org domain
    # alone is not enough; it must be corroborated by mission/nonprofit language.
    website_domain = extract_domain(website)
    mission_signal = re.search(
        r"\b(?:501\(c\)\(3\)|non[- ]?profit|not[- ]for[- ]profit|faith[- ]based|"
        r"religious organization|statement of faith|ministry|mission alignment|"
        r"mission[- ]driven organization)\b",
        description,
        re.I,
    )
    if website_domain.endswith(".org") and mission_signal:
        return True, "excluded_industry_mission_driven_org"

    return False, ""


def _location_contains_marker(location: str, marker: str) -> bool:
    return re.search(r"\b" + re.escape(marker) + r"\b", location, re.I) is not None


def _coordinates_are_us(job: Dict) -> bool:
    try:
        lat = float(job.get("job_latitude"))
        lon = float(job.get("job_longitude"))
    except (TypeError, ValueError):
        return False
    return config.US_LAT_MIN <= lat <= config.US_LAT_MAX and config.US_LON_MIN <= lon <= config.US_LON_MAX


def _raw_us_state_abbreviations(text: str) -> List[str]:
    """Return explicit uppercase US state abbreviations from source text.

    Matching is case-sensitive so ordinary words such as ``in`` and ``or`` do
    not become Indiana or Oregon.
    """
    if not text:
        return []
    abbreviations = "|".join(sorted((value.upper() for value in config.US_STATE_ABBREVS), key=len, reverse=True))
    return list(dict.fromkeys(re.findall(rf"(?<![A-Za-z])({abbreviations})(?![A-Za-z])", text)))


def _extract_title_city_state(title: str) -> str:
    """Extract only an explicit ``City, ST`` phrase from a title.

    Bare tokens are deliberately ignored: ``REMOTE OK`` is not Oklahoma and
    ``PR Account Executive`` is not Puerto Rico.
    """
    if not title:
        return ""
    abbreviations = "|".join(sorted((value.upper() for value in config.US_STATE_ABBREVS), key=len, reverse=True))
    city = r"[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,4}"
    patterns = (
        rf"\bin\s+({city},\s*(?:{abbreviations}))(?:,\s*(?:U\.?S\.?A?\.?|United States))?\b",
        rf"(?:^|[-–—|(/])\s*({city},\s*(?:{abbreviations}))(?:,\s*(?:U\.?S\.?A?\.?|United States))?(?=$|[)\]|/| -])",
    )
    for pattern in patterns:
        match = re.search(pattern, title)
        if not match:
            continue
        value = match.group(1).strip()
        city_value = normalize_text(value.split(",", 1)[0])
        if city_value not in config.GENERIC_REMOTE_LOCATIONS:
            return value
    return ""


def _extract_display_location(job: Dict, explicit_us: bool) -> str:
    raw_location = str(job.get("job_location") or "").strip()
    normalized_location = normalize_text(raw_location)
    if normalized_location not in config.GENERIC_REMOTE_LOCATIONS:
        return raw_location

    city = str(job.get("job_city") or "").strip()
    state = str(job.get("job_state") or "").strip()
    if city and state:
        return f"{city}, {state}"
    if state:
        return state

    title = str(job.get("job_title") or "")
    city_state = _extract_title_city_state(title)
    if city_state:
        return city_state

    # Never infer a state from a bare two-letter token in the title. Tokens
    # such as "REMOTE OK" and "PR Account Executive" are semantic text, not
    # Oklahoma/Puerto Rico. Structured city/state fields or an explicit
    # "in City, ST" phrase are required.
    return "Remote, United States" if explicit_us else (raw_location or "Remote")


def assess_us_eligibility(job: Dict) -> GeographyEvidence:
    """Require independent US evidence for generic remote/Anywhere listings.

    JSearch's ``country=us`` query can echo ``job_country=US`` even when a
    syndicated listing is global or explicitly foreign. Therefore the country
    field is accepted only with a non-generic location or separate US evidence.
    """
    title = str(job.get("job_title") or "")
    location = str(job.get("job_location") or "")
    description = str(job.get("job_description") or "")[:5000]
    city = str(job.get("job_city") or "")
    state_raw = str(job.get("job_state") or "").strip()
    country = normalize_text(job.get("job_country") or "")
    apply_link = unquote(str(job.get("job_apply_link") or "")).lower()
    source_text = "\n".join([title, location, city, state_raw, description])
    normalized_location_source = normalize_text("\n".join([title, location, city, state_raw]))
    normalized_location = normalize_text(location)

    for pattern in config.FOREIGN_ONLY_ELIGIBILITY_PATTERNS:
        if re.search(pattern, source_text, re.I):
            return GeographyEvidence(False, f"foreign_only_eligibility:{pattern}", location or "Remote", "foreign")
    for pattern in config.GLOBAL_REMOTE_PATTERNS:
        if re.search(pattern, source_text, re.I):
            return GeographyEvidence(False, f"global_remote_scope:{pattern}", location or "Remote", "global")

    url_tokens = re.sub(r"[^a-z0-9]+", "-", apply_link)
    for marker in [*config.FOREIGN_COUNTRY_URL_SLUGS, *config.FOREIGN_CITY_URL_SLUGS]:
        token = marker.lower().strip("-")
        if token and re.search(rf"(?:^|-)({re.escape(token)})(?:-|$)", url_tokens):
            return GeographyEvidence(False, f"non_us_apply_link_marker:{marker}", location or "Remote", "foreign")

    explicit_us_url_pattern = next(
        (pattern for pattern in config.US_APPLY_LINK_SCOPE_PATTERNS if re.search(pattern, apply_link, re.I)),
        None,
    )

    for marker in config.NON_US_LOCATION_MARKERS:
        normalized_marker = normalize_text(marker)
        if normalized_marker and _location_contains_marker(normalized_location_source, normalized_marker):
            return GeographyEvidence(False, f"non_us_location:{marker}", location or "Remote", "foreign")

    website = (job.get("employer_website") or "").lower()
    if any(website.endswith(tld) or f"{tld}/" in website for tld in config.NON_US_WEBSITE_TLDS):
        return GeographyEvidence(False, "non_us_website_tld", location or "Remote", "foreign")

    explicit_us_pattern = next(
        (pattern for pattern in config.US_REMOTE_SCOPE_PATTERNS if re.search(pattern, source_text, re.I)),
        None,
    )
    # A delimiter-bounded US/USA suffix in a title (for example "AI Engineer - US")
    # is explicit scope. Matching remains case-sensitive for US/USA so the pronoun
    # "us" cannot become geographic evidence.
    title_has_explicit_us = bool(
        re.search(r"(?:^|[-–—|(/])\s*(?:U\.?S\.?A?\.?)\s*(?=$|[)\]|/| -])", title)
        or re.search(
            r"(?:^|[-–—|(/])\s*United States\s*(?=$|[)\]|/| -])",
            title,
            re.I,
        )
    )
    # A structured job_location of "United States" is independent geographic
    # evidence; a query-echoed job_country=US by itself is not.
    location_is_explicit_us = normalized_location in config.US_COUNTRY_CODES
    explicit_us = bool(
        explicit_us_pattern is not None
        or title_has_explicit_us
        or location_is_explicit_us
        or explicit_us_url_pattern is not None
    )

    state_norm = normalize_text(state_raw)
    state_is_us = bool(
        state_norm
        and (state_norm in config.US_STATE_ABBREVS or state_norm in config.US_STATE_NAMES)
    )
    # ``Georgia`` alone is ambiguous between the US state and the country.
    if state_norm == "georgia":
        state_is_us = False

    state_tokens = _raw_us_state_abbreviations("\n".join([location, state_raw]))
    title_city_state = _extract_title_city_state(title)
    location_has_us_state_name = any(
        state_name != "georgia" and _location_contains_marker(normalize_text(location), state_name)
        for state_name in config.US_STATE_NAMES
    )
    explicit_state = bool(
        state_is_us or state_tokens or title_city_state or location_has_us_state_name
    )

    if explicit_us or explicit_state:
        display = _extract_display_location(job, explicit_us=True)
        if (
            explicit_us_url_pattern
            and not explicit_us_pattern
            and not title_has_explicit_us
            and not location_is_explicit_us
        ):
            reason = f"explicit_us_apply_link:{explicit_us_url_pattern}"
        elif title_has_explicit_us and not explicit_us_pattern:
            reason = "explicit_us_title"
        elif location_is_explicit_us and not explicit_us_pattern:
            reason = "explicit_us_location"
        elif (
            explicit_us_pattern
            and explicit_us_pattern.startswith(r"\bavailable")
            and country in config.US_COUNTRY_CODES
        ):
            reason = "country_field"
        elif explicit_us_pattern:
            reason = f"explicit_us_scope:{explicit_us_pattern}"
        else:
            reason = "explicit_us_state"
        return GeographyEvidence(True, reason, display, "us_explicit")

    generic_remote = normalized_location in config.GENERIC_REMOTE_LOCATIONS
    if country and country not in config.US_COUNTRY_CODES:
        return GeographyEvidence(False, f"non_us_country:{country}", location or "Remote", "foreign")

    if config.REQUIRE_EXPLICIT_US_REMOTE_SCOPE:
        reason = (
            "ambiguous_remote_location_without_us_evidence"
            if generic_remote
            else "specific_location_without_us_corroboration"
        )
        return GeographyEvidence(False, reason, location or "Remote", "ambiguous")

    if country in config.US_COUNTRY_CODES:
        return GeographyEvidence(True, "country_field", _extract_display_location(job, explicit_us=False), "us_weak")
    return GeographyEvidence(False, "missing_us_signals", location or "Remote", "ambiguous")


def is_us_job(job: Dict) -> Tuple[bool, str]:
    evidence = assess_us_eligibility(job)
    return evidence.eligible, evidence.reason



def classify_work_arrangement(job: Dict) -> WorkArrangementEvidence:
    """Resolve work arrangement from high-confidence text before provider flags.

    JSearch sometimes marks an explicitly remote title as ``job_is_remote=False``.
    Conversely, a true flag can coexist with a hybrid or onsite title. Title and
    location requirements therefore have the highest precedence, followed by
    precise requirement language in the description, explicit remote language,
    and finally the provider flag.
    """
    title_location = "\n".join([
        job.get("job_title") or "",
        job.get("job_location") or "",
    ])
    description = (job.get("job_description") or "")[:6000]

    for pattern in config.IN_PERSON_TITLE_LOCATION_PATTERNS:
        if re.search(pattern, title_location, re.I):
            return WorkArrangementEvidence(
                "in_person", f"in_person_title_or_location:{pattern}"
            )

    for pattern in config.IN_PERSON_DESCRIPTION_PATTERNS:
        if re.search(pattern, description, re.I):
            return WorkArrangementEvidence(
                "in_person", f"required_in_person_description:{pattern}"
            )

    for pattern in config.REMOTE_TITLE_LOCATION_PATTERNS:
        if re.search(pattern, title_location, re.I):
            return WorkArrangementEvidence(
                "remote", f"remote_title_or_location:{pattern}"
            )

    for pattern in config.REMOTE_DESCRIPTION_PATTERNS:
        if re.search(pattern, description, re.I):
            return WorkArrangementEvidence(
                "remote", f"remote_description:{pattern}"
            )

    remote_flag = job.get("job_is_remote")
    if remote_flag is True:
        return WorkArrangementEvidence("remote", "jsearch_remote_true")
    if remote_flag is False:
        return WorkArrangementEvidence(
            "in_person", "jsearch_remote_false_without_remote_evidence"
        )
    return WorkArrangementEvidence("unknown", "missing_work_arrangement_evidence")


def is_explicitly_in_person(job: Dict) -> Tuple[bool, str]:
    evidence = classify_work_arrangement(job)
    if evidence.status == "in_person":
        return True, evidence.reason
    return False, ""



def is_obvious_role_mismatch(job: Dict) -> Tuple[bool, str]:
    """Reject high-confidence catalog leakage that should never reach enrichment."""
    matched_role = job.get("_matched_role") or ""
    if matched_role not in {"Automation Specialist", "AI Automation Engineer"}:
        return False, ""
    text = "\n".join([
        job.get("job_title") or "",
        (job.get("job_description") or "")[:6000],
    ])
    patterns = [
        r"\b(?:industrial|manufacturing) automation\b",
        r"\b(?:plc|scada|controls engineer|instrumentation)\b",
    ]
    for pattern in patterns:
        if re.search(pattern, text, re.I):
            return True, f"obvious_role_mismatch:{pattern}"
    return False, ""

def is_non_paying_role(job: Dict) -> Tuple[bool, str]:
    """Reject postings that explicitly state the role is unpaid/equity-only."""
    title = job.get("job_title") or ""
    description = (job.get("job_description") or "")[:5000]
    text = f"{title}\n{description}"
    for pattern in config.NON_PAYING_JOB_PATTERNS:
        if re.search(pattern, text, re.I):
            return True, f"non_paying_role:{pattern}"
    return False, ""


def assess_employment_quality(job: Dict) -> EmploymentEvidence:
    title = str(job.get("job_title") or "")
    description = str(job.get("job_description") or "")[:5000]
    employment_type_raw = str(job.get("job_employment_type") or "")
    employment_type = normalize_text(employment_type_raw)

    if config.REJECT_NON_ACTIVE_HIRING_SIGNALS:
        for pattern in config.NON_ACTIVE_HIRING_SIGNAL_PATTERNS:
            if re.search(pattern, f"{title}\n{description}", re.I):
                return EmploymentEvidence(False, f"non_active_hiring_signal:{pattern}", "non_active")

    for pattern in config.NON_FULL_TIME_TITLE_PATTERNS:
        if re.search(pattern, title, re.I):
            return EmploymentEvidence(False, f"non_full_time_title:{pattern}", "non_full_time")
    for pattern in config.NON_FULL_TIME_DESCRIPTION_PATTERNS:
        if re.search(pattern, description, re.I):
            return EmploymentEvidence(False, f"non_full_time_description:{pattern}", "non_full_time")
    if any(value in employment_type for value in config.NON_FULL_TIME_EMPLOYMENT_TYPES):
        return EmploymentEvidence(False, f"non_full_time_employment_type:{employment_type_raw}", "non_full_time")

    full_time = employment_type in {"full time", "fulltime"} or bool(
        re.search(r"\bfull[- ]time\b", title, re.I)
    )
    if config.REQUIRE_FULL_TIME_ROLES and employment_type_raw and not full_time:
        return EmploymentEvidence(False, f"unsupported_employment_type:{employment_type_raw}", "unknown")
    if full_time:
        return EmploymentEvidence(True, "explicit_full_time", "full_time")
    # Some JSearch sources omit employment type entirely. Keep the job when no
    # contrary part-time/contract signal exists, but preserve the uncertainty.
    return EmploymentEvidence(True, "employment_type_missing_no_negative_evidence", "unknown")


def is_non_full_time_role(job: Dict) -> Tuple[bool, str]:
    assessment = assess_employment_quality(job)
    return (not assessment.eligible and assessment.classification != "non_active", assessment.reason)


def is_non_active_hiring_signal(job: Dict) -> Tuple[bool, str]:
    assessment = assess_employment_quality(job)
    return (assessment.classification == "non_active", assessment.reason)

def _detect_company_column(headers: List[str]) -> Optional[str]:
    preferred = [
        "company", "company name", "employer", "employer name", "account",
        "account name", "organization", "client", "name",
    ]
    lowered = {header.lower().strip(): header for header in headers}
    for name in preferred:
        if name in lowered:
            return lowered[name]
    return headers[0] if len(headers) == 1 else None


def load_crm_companies(path: str) -> Tuple[Set[str], Set[str]]:
    source = Path(path)
    if not source.exists():
        if config.PRODUCTION:
            raise FileNotFoundError(f"CRM exclusion file required in production: {path}")
        return set(), set()

    raw_names: List[str] = []
    with source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        if config.PRODUCTION:
            raise ValueError(f"CRM file is empty: {path}")
        return set(), set()

    header_candidate = [cell.strip() for cell in rows[0]]
    company_column = _detect_company_column(header_candidate)
    if company_column and company_column.lower().strip() in {
        "company", "company name", "employer", "employer name", "account",
        "account name", "organization", "client", "name",
    }:
        index = header_candidate.index(company_column)
        data_rows = rows[1:]
    elif len(header_candidate) == 1:
        index = 0
        data_rows = rows
    else:
        raise ValueError(
            f"Could not identify a company column in CRM file headers: {header_candidate}"
        )

    for row in data_rows:
        if len(row) > index and row[index].strip():
            raw_names.append(row[index].strip())

    normalized = {
        normalize_text(name) for name in raw_names
        if len(normalize_text(name)) >= config.CRM_MIN_MATCH_LENGTH
    }
    compact = {
        normalize_compact(name) for name in raw_names
        if len(normalize_compact(name)) >= config.CRM_MIN_MATCH_LENGTH
    }
    if config.PRODUCTION and not normalized:
        raise ValueError(f"CRM file loaded zero companies: {path}")
    return normalized, compact


def is_in_crm(job: Dict, crm_normalized: Set[str], crm_compact: Set[str]) -> Tuple[bool, str]:
    employer_norm = normalize_text(job.get("employer_name", "") or "")
    employer_compact = normalize_compact(employer_norm)
    domain_compact = normalize_compact(get_safe_employer_domain(job)[0])

    for candidate in (employer_norm, employer_compact, domain_compact):
        if candidate and (candidate in crm_normalized or candidate in crm_compact):
            return True, "exact_company_match"

    min_len = max(config.CRM_MIN_MATCH_LENGTH, 5)
    for crm_name in crm_compact:
        for candidate in (employer_compact, domain_compact):
            if len(crm_name) < min_len or len(candidate) < min_len:
                continue
            if crm_name in candidate or candidate in crm_name:
                ratio = min(len(crm_name), len(candidate)) / max(len(crm_name), len(candidate))
                if ratio >= 0.88:
                    return True, f"fuzzy_company_match:{crm_name}"
    return False, ""


def assess_pre_enrichment_viability(job: Dict) -> PreEnrichmentAssessment:
    """Apply every zero-credit gate shared by scraping and final filtering.

    CRM suppression, in-run deduplication, and persistent seen-state are excluded
    because they require run-level context. Everything else is intentionally the
    same logic used by Step 2, so adaptive JSearch deepening rewards jobs that can
    actually reach enrichment instead of merely matching a title.
    """
    normalize_job_identity(job)
    quality = assess_quality_guard(job)
    arrangement = classify_work_arrangement(job)
    geography = assess_us_eligibility(job)
    employment = assess_employment_quality(job)
    employer_domain, employer_domain_source = get_safe_employer_domain(job)

    if not quality.eligible:
        return PreEnrichmentAssessment(
            eligible=False,
            stat_name=quality.stat_name,
            reason=quality.reason,
            work_arrangement=arrangement,
            geography=geography,
            employment=employment,
            employer_domain=employer_domain,
            employer_domain_source=employer_domain_source,
        )

    checks = [
        ("excluded_aggregator", is_job_aggregator_or_publisher),
        ("excluded_stale", is_stale_job),
        ("excluded_staffing", is_staffing_company),
        ("excluded_industry", is_excluded_industry),
        ("excluded_role_mismatch", is_obvious_role_mismatch),
        (
            "excluded_in_person",
            lambda _job: (
                arrangement.status == "in_person",
                arrangement.reason if arrangement.status == "in_person" else "",
            ),
        ),
        ("excluded_non_paying", is_non_paying_role),
        (
            "excluded_non_active",
            lambda _job: (
                employment.classification == "non_active",
                employment.reason if employment.classification == "non_active" else "",
            ),
        ),
        (
            "excluded_non_full_time",
            lambda _job: (
                not employment.eligible and employment.classification != "non_active",
                employment.reason if not employment.eligible else "",
            ),
        ),
        (
            "excluded_non_us",
            lambda _job: (not geography.eligible, geography.reason),
        ),
    ]
    for stat_name, check in checks:
        matched, reason = check(job)
        if matched:
            return PreEnrichmentAssessment(
                eligible=False,
                stat_name=stat_name,
                reason=reason,
                work_arrangement=arrangement,
                geography=geography,
                employment=employment,
                employer_domain=employer_domain,
                employer_domain_source=employer_domain_source,
            )

    return PreEnrichmentAssessment(
        eligible=True,
        stat_name="",
        reason="",
        work_arrangement=arrangement,
        geography=geography,
        employment=employment,
        employer_domain=employer_domain,
        employer_domain_source=employer_domain_source,
    )


def find_latest_raw_file() -> str:
    candidates = sorted(Path(config.OUTPUT_DIR).glob("jobs_*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No raw scrape files in {config.OUTPUT_DIR}")
    return str(candidates[0])


def run_filter(
    input_path: Optional[str] = None,
    registry: Optional[SeenJobsRegistry] = None,
    output_dir: Optional[str] = None,
) -> FilterResult:
    input_path = input_path or find_latest_raw_file()
    registry = registry or SeenJobsRegistry()
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    jobs = payload.get("jobs", [])
    crm_normalized, crm_compact = load_crm_companies(config.CRM_EXCLUSION_FILE)

    kept: List[Dict] = []
    rejected: List[Dict] = []
    seen_dedup_keys: Set[Tuple[str, str]] = set()
    stats = {
        "input_total": len(jobs),
        "kept": 0,
        "excluded_posting_integrity": 0,
        "excluded_restricted_role": 0,
        "excluded_outsourcing": 0,
        "excluded_contextual_mismatch": 0,
        "excluded_aggregator": 0,
        "excluded_stale": 0,
        "excluded_staffing": 0,
        "excluded_industry": 0,
        "excluded_role_mismatch": 0,
        "excluded_in_person": 0,
        "excluded_non_paying": 0,
        "excluded_non_active": 0,
        "excluded_non_full_time": 0,
        "excluded_non_us": 0,
        "excluded_crm": 0,
        "excluded_duplicate": 0,
        "excluded_previously_seen": 0,
    }

    for job in jobs:
        normalize_job_identity(job)
        assessment = assess_pre_enrichment_viability(job)
        candidate = {
            **job,
            "_work_arrangement": assessment.work_arrangement.status,
            "_work_arrangement_reason": assessment.work_arrangement.reason,
            "_us_eligibility_reason": assessment.geography.reason,
            "_remote_scope": assessment.geography.scope,
            "_normalized_location": assessment.geography.display_location,
            "_employment_quality": assessment.employment.classification,
            "_employment_quality_reason": assessment.employment.reason,
            "_employer_domain_input": assessment.employer_domain,
            "_employer_domain_source": assessment.employer_domain_source,
        }
        if not assessment.eligible:
            stats[assessment.stat_name] += 1
            rejected.append({**candidate, "_filter_reason": assessment.reason})
            continue

        in_crm, crm_reason = is_in_crm(candidate, crm_normalized, crm_compact)
        if in_crm:
            stats["excluded_crm"] += 1
            rejected.append({**candidate, "_filter_reason": crm_reason})
            continue

        key = dedup_key(candidate)
        if not all(key):
            rejected.append({**candidate, "_filter_reason": "missing_company_or_title"})
            stats["excluded_duplicate"] += 1
            continue
        if key in seen_dedup_keys:
            stats["excluded_duplicate"] += 1
            rejected.append({**candidate, "_filter_reason": "duplicate_company_title_in_run"})
            continue
        if registry.has_dedup_key(key):
            stats["excluded_previously_seen"] += 1
            rejected.append({**candidate, "_filter_reason": "previously_seen_company_title"})
            continue

        seen_dedup_keys.add(key)
        kept.append(candidate)
        stats["kept"] += 1

    stamp = datetime.now().strftime("%Y-%m-%d")
    destination = Path(output_dir or config.FILTERED_OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)
    output_path = str(destination / f"jobs_filtered_{stamp}.json")
    rejected_path = str(destination / f"jobs_rejected_{stamp}.json")
    Path(output_path).write_text(
        json.dumps({
            "filter_date": datetime.now().isoformat(),
            "source_file": input_path,
            "total_jobs": len(kept),
            "stats": stats,
            "jobs": kept,
        }, indent=2),
        encoding="utf-8",
    )
    Path(rejected_path).write_text(
        json.dumps({
            "filter_date": datetime.now().isoformat(),
            "source_file": input_path,
            "total_rejected": len(rejected),
            "stats": stats,
            "jobs": rejected,
        }, indent=2),
        encoding="utf-8",
    )

    errors: List[str] = []
    if config.PRODUCTION and jobs and not kept:
        errors.append("Filter kept zero jobs from a non-empty scrape")

    return FilterResult(
        output_path=output_path,
        rejected_path=rejected_path,
        kept_count=len(kept),
        rejected_count=len(rejected),
        stats=stats,
        success=not errors,
        errors=errors,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        result = run_filter()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        sys.exit(1)
    if config.PRODUCTION and not result.success:
        sys.exit(1)
