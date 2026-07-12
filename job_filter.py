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

from domain_utils import normalize_company_domain

import config
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
    domain = extract_domain(job.get("employer_website") or "")
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
    normalized = (domain or "").lower().strip(".")
    return any(
        normalized == blocked or normalized.endswith("." + blocked)
        for blocked in config.INTERMEDIARY_JOB_DOMAINS
    )


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
        if normalize_text(marketplace) in employer_norm:
            return True, f"freelance_marketplace:{marketplace}"

    for known in config.KNOWN_STAFFING_EMPLOYERS:
        if normalize_text(known) in employer_norm:
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
            if is_first_person or employer_is_vague or normalized_phrase.startswith("on behalf"):
                return True, f"staffing_phrase_in_description:{phrase}"

    return False, ""


def is_excluded_industry(job: Dict) -> Tuple[bool, str]:
    if not config.ENABLE_BROADER_INDUSTRY_EXCLUSIONS:
        return False, ""

    employer_norm = normalize_text(job.get("employer_name", "") or "")
    website = (job.get("employer_website") or "").lower()
    apply_domain = extract_domain(job.get("job_apply_link") or "")

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


def is_us_job(job: Dict) -> Tuple[bool, str]:
    apply_link = (job.get("job_apply_link") or "").lower()
    for country_slug in config.FOREIGN_COUNTRY_URL_SLUGS:
        if re.search(r"--" + re.escape(country_slug) + r"--", apply_link, re.I):
            return False, f"non_us_apply_link_slug:{country_slug}"

    country = normalize_text(job.get("job_country") or "")
    if country:
        if country in config.US_COUNTRY_CODES:
            return True, "country_field"
        return False, f"non_us_country:{country}"

    location = normalize_text(job.get("job_location") or "")
    for marker in config.NON_US_LOCATION_MARKERS:
        if _location_contains_marker(location, normalize_text(marker)):
            return False, f"non_us_location:{marker}"

    website = (job.get("employer_website") or "").lower()
    if any(website.endswith(tld) or f"{tld}/" in website for tld in config.NON_US_WEBSITE_TLDS):
        return False, "non_us_website_tld"

    state = normalize_text(job.get("job_state") or "")
    if state in config.US_STATE_ABBREVS or state in config.US_STATE_NAMES:
        return True, "state_field"

    # Last comma-delimited token often contains a state abbreviation + ZIP.
    raw_location = (job.get("job_location") or "").lower()
    state_part = raw_location.split(",")[-1].strip() if "," in raw_location else ""
    state_token = state_part.split()[0] if state_part else ""
    if state_token in config.US_STATE_ABBREVS:
        return True, "location_state"

    if re.search(r"/us/", apply_link) or "gl=us" in apply_link:
        return True, "apply_link_us"
    if any(domain in apply_link for domain in config.TRUSTED_US_JOB_BOARD_DOMAINS):
        return True, "trusted_us_job_board"

    # Coordinates alone are not conclusive: Canadian locations overlap a broad
    # US bounding box. Keep them as metadata, not as an independent country signal.

    if location in {"remote", "anywhere", "work from home", "united states"}:
        description = normalize_text((job.get("job_description") or "")[:1500])
        if "united states" in description or re.search(r"\busa\b", description):
            return True, "remote_us_description"
        return False, "ambiguous_remote_location"

    return False, "missing_us_signals"


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
    domain_compact = normalize_compact(extract_domain(job.get("employer_website") or ""))

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


def find_latest_raw_file() -> str:
    candidates = sorted(Path(config.OUTPUT_DIR).glob("jobs_*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No raw scrape files in {config.OUTPUT_DIR}")
    return str(candidates[0])


def run_filter(
    input_path: Optional[str] = None,
    registry: Optional[SeenJobsRegistry] = None,
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
        "excluded_aggregator": 0,
        "excluded_stale": 0,
        "excluded_staffing": 0,
        "excluded_industry": 0,
        "excluded_non_us": 0,
        "excluded_crm": 0,
        "excluded_duplicate": 0,
        "excluded_previously_seen": 0,
    }

    checks = [
        ("excluded_aggregator", is_job_aggregator_or_publisher),
        ("excluded_stale", is_stale_job),
        ("excluded_staffing", is_staffing_company),
        ("excluded_industry", is_excluded_industry),
        ("excluded_non_us", lambda job: (lambda ok, reason: (not ok, reason))(*is_us_job(job))),
        ("excluded_crm", lambda job: is_in_crm(job, crm_normalized, crm_compact)),
    ]

    for job in jobs:
        rejected_reason = ""
        rejected_stat = ""
        for stat_name, check in checks:
            matched, reason = check(job)
            if matched:
                rejected_stat = stat_name
                rejected_reason = reason
                break
        if rejected_reason:
            stats[rejected_stat] += 1
            rejected.append({**job, "_filter_reason": rejected_reason})
            continue

        key = dedup_key(job)
        if not all(key):
            rejected.append({**job, "_filter_reason": "missing_company_or_title"})
            stats["excluded_duplicate"] += 1
            continue
        if key in seen_dedup_keys:
            stats["excluded_duplicate"] += 1
            rejected.append({**job, "_filter_reason": "duplicate_company_title_in_run"})
            continue
        if registry.has_dedup_key(key):
            stats["excluded_previously_seen"] += 1
            rejected.append({**job, "_filter_reason": "previously_seen_company_title"})
            continue

        seen_dedup_keys.add(key)
        kept.append(job)
        stats["kept"] += 1

    stamp = datetime.now().strftime("%Y-%m-%d")
    output_path = str(Path(config.FILTERED_OUTPUT_DIR) / f"jobs_filtered_{stamp}.json")
    rejected_path = str(Path(config.FILTERED_OUTPUT_DIR) / f"jobs_rejected_{stamp}.json")
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
