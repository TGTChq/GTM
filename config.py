"""Central configuration for the TGTC job-intent outbound pipeline."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

from role_catalog import DEFAULT_SEARCH_ROLES

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def _env_json(name: str, default: Any) -> Any:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain valid JSON") from exc


# ---------- Runtime ----------
PRODUCTION = _env_bool("PRODUCTION", True)
DEBUG_API_RESPONSES = _env_bool("DEBUG_API_RESPONSES", False)
REQUEST_TIMEOUT_SECONDS = _env_int("REQUEST_TIMEOUT_SECONDS", 30)
MAX_HTTP_RETRIES = _env_int("MAX_HTTP_RETRIES", 3)

# ---------- Paths ----------
STATE_DIR = str(BASE_DIR / "data" / "state")
ARTIFACT_ROOT = Path(os.getenv("PIPELINE_ARTIFACT_ROOT", str(BASE_DIR / "data")))
OUTPUT_DIR = str(ARTIFACT_ROOT / "raw")
FILTERED_OUTPUT_DIR = str(ARTIFACT_ROOT / "filtered")
STEP3_OUTPUT_DIR = str(ARTIFACT_ROOT / "enriched")
LOG_DIR = str(ARTIFACT_ROOT / "logs")
RUN_SUMMARY_DIR = str(ARTIFACT_ROOT / "logs" / "runs")
SEEN_JOBS_FILE = str(Path(STATE_DIR) / "seen_jobs.json")
CRM_EXCLUSION_FILE = os.getenv(
    "CRM_EXCLUSION_FILE", str(BASE_DIR / "data" / "exclusions" / "crm_companies.csv")
)
STAFFING_GROUND_TRUTH_FILE = os.getenv(
    "STAFFING_GROUND_TRUTH_FILE",
    str(BASE_DIR / "data" / "validation" / "staffing_ground_truth.csv"),
)
REQUIRE_STAFFING_GROUND_TRUTH = _env_bool("REQUIRE_STAFFING_GROUND_TRUTH", False)

for directory in (
    OUTPUT_DIR,
    FILTERED_OUTPUT_DIR,
    STEP3_OUTPUT_DIR,
    LOG_DIR,
    STATE_DIR,
    RUN_SUMMARY_DIR,
):
    Path(directory).mkdir(parents=True, exist_ok=True)

# ---------- JSearch ----------
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
JSEARCH_HOST = os.getenv("JSEARCH_HOST", "jsearch.p.rapidapi.com")
JSEARCH_ENDPOINT = os.getenv("JSEARCH_ENDPOINT", "https://jsearch.p.rapidapi.com/search-v2")
DATE_POSTED = os.getenv("DATE_POSTED", "today")
COUNTRY = os.getenv("COUNTRY", "us")
NUM_PAGES = _env_int("NUM_PAGES", 3)
SEARCH_DELAY_SECONDS = _env_float("SEARCH_DELAY_SECONDS", 0.8)
# Operational controls keep the complete Brett-approved catalog active while
# bounding request-unit usage. Zero disables only the corresponding guard.
JSEARCH_MAX_QUERIES_PER_RUN = _env_int("JSEARCH_MAX_QUERIES_PER_RUN", 0)
# Guard the estimated request units before the first API call. JSearch charges
# approximately one request unit per requested page; 118 roles x 3 pages = 354.
# The 370-unit default leaves 16 units for diversified lookback queries. Set to
# 0 only for an intentional, supervised deep diagnostic.
JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN = _env_int(
    "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 370
)
JSEARCH_STOP_ON_LOW_QUOTA = _env_bool("JSEARCH_STOP_ON_LOW_QUOTA", True)
JSEARCH_MIN_REMAINING_REQUESTS = _env_int("JSEARCH_MIN_REMAINING_REQUESTS", 500)
# Ask JSearch for remote inventory directly instead of paying to retrieve mostly
# onsite jobs and discarding them later. Text bias remains configurable because
# provider flags can be imperfect, while the downstream evidence resolver still
# makes the final work-arrangement decision.
JSEARCH_REMOTE_JOBS_ONLY = _env_bool("JSEARCH_REMOTE_JOBS_ONLY", True)
JSEARCH_REMOTE_QUERY_BIAS = _env_bool("JSEARCH_REMOTE_QUERY_BIAS", True)
# JSearch /search-v2 officially exposes ``work_from_home=true`` for remote-only
# inventory. The previous ``remote_jobs_only`` name was not part of the current
# provider contract and could be ignored, allowing avoidable onsite inventory.
JSEARCH_REMOTE_FILTER_PARAMETER = os.getenv(
    "JSEARCH_REMOTE_FILTER_PARAMETER", "work_from_home"
).strip()
# Diversify the reserved lookback budget across publisher-scoped queries instead
# of repeating the same broad query. JSearch supports ``via <publisher>`` in the
# query string. The final local gates remain authoritative.
JSEARCH_LOOKBACK_QUERY_VARIANTS = _env_json(
    "JSEARCH_LOOKBACK_QUERY_VARIANTS",
    ["linkedin", "indeed", "glassdoor", "hiring"],
)
# When one-page mode is intentionally configured, use only the remaining
# request-unit budget on deeper pages for roles whose first-page results survive
# the local pre-enrichment gates. Three-page production coverage disables this
# redundant deepening path automatically. Diagnostic runs remain deterministic.
JSEARCH_ADAPTIVE_DEEPENING = _env_bool("JSEARCH_ADAPTIVE_DEEPENING", True)
JSEARCH_MAX_EXTRA_PAGES_PER_ROLE = _env_int(
    "JSEARCH_MAX_EXTRA_PAGES_PER_ROLE", 1
)
JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES = _env_int(
    "JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES", 32
)
JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE = _env_int(
    "JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE", 1
)
JSEARCH_ADAPTIVE_BUCKET_BALANCING = _env_bool(
    "JSEARCH_ADAPTIVE_BUCKET_BALANCING", True
)
# After three-page base coverage, reserve the remaining 16-unit budget for a
# diversified, wider-window pass. This improves qualified inventory without
# weakening any quality gate.
JSEARCH_ADAPTIVE_LOOKBACK = _env_bool("JSEARCH_ADAPTIVE_LOOKBACK", True)
JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED = os.getenv(
    "JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED", "week"
)
JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES = _env_int(
    "JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES", 16
)
JSEARCH_TARGET_PREFILTER_VIABLE = _env_int(
    "JSEARCH_TARGET_PREFILTER_VIABLE", 60
)
# Reject only clearly stale job-intent signals before enrichment. Unknown or
# conflicting dates remain eligible; the oldest parseable source date is used.
MAX_JOB_AGE_DAYS = _env_int("MAX_JOB_AGE_DAYS", 30)

# Quality gates restore the paid-test standard before any Apollo/Hunter spend.
# The 118-role catalog remains active, but only current full-time roles with
# explicit US hiring evidence may reach enrichment.
REQUIRE_FULL_TIME_ROLES = _env_bool("REQUIRE_FULL_TIME_ROLES", True)
REJECT_NON_ACTIVE_HIRING_SIGNALS = _env_bool(
    "REJECT_NON_ACTIVE_HIRING_SIGNALS", True
)
REQUIRE_EXPLICIT_US_REMOTE_SCOPE = _env_bool(
    "REQUIRE_EXPLICIT_US_REMOTE_SCOPE", True
)
# Treat JSearch's structured US country + remote signals as sufficient when no
# explicit foreign/global contradiction exists. This prevents generic "Remote"
# listings from being discarded merely because the body omits the phrase
# "United States", while retaining explicit non-US/global hard rejects.
ALLOW_PROVIDER_CONFIRMED_US_REMOTE = _env_bool(
    "ALLOW_PROVIDER_CONFIRMED_US_REMOTE", True
)

ROLES = _env_json("ROLES_JSON", list(DEFAULT_SEARCH_ROLES))

# Global title exclusions from Brett's Intent-Based Outbound 2.0 rules.
# Phrase matching is word-boundary based in jsearch_scraper.py.
EXCLUDED_TITLE_KEYWORDS = [
    "vp",
    "vice president",
    "director",
    "intern",
    "internship",
    "senior",
    "sr",
    "head of",
    "event marketing",
    "field marketing",
]

# ---------- Health gates ----------
MIN_JOBS_PER_RUN = _env_int("MIN_JOBS_PER_RUN", 10)
MIN_ROLES_WITH_RESULTS = _env_int("MIN_ROLES_WITH_RESULTS", 4)
MAX_ROLE_FAILURES = _env_int("MAX_ROLE_FAILURES", 3)
# The absolute threshold protects small role sets; the rate prevents the full
# 100+ role catalog from failing because of a handful of isolated query errors.
MAX_ROLE_FAILURE_RATE = _env_float("MAX_ROLE_FAILURE_RATE", 0.10)
MIN_HIRING_MANAGER_MATCH_RATE = _env_float("MIN_HIRING_MANAGER_MATCH_RATE", 0.70)
ENFORCE_HM_MATCH_RATE = _env_bool("ENFORCE_HM_MATCH_RATE", False)

# Daily production throughput controls. The pipeline stops enrichment after it
# reaches the reviewable-lead target, with an eligible-company safety cap to
# bound Apollo/Hunter usage on low-contactability days.
TARGET_REVIEWABLE_LEADS_PER_RUN = _env_int("TARGET_REVIEWABLE_LEADS_PER_RUN", 30)
MAX_ELIGIBLE_COMPANIES_PER_RUN = _env_int("MAX_ELIGIBLE_COMPANIES_PER_RUN", 90)
SEEN_JOBS_RETENTION_DAYS = _env_int("SEEN_JOBS_RETENTION_DAYS", 30)
CRM_MIN_MATCH_LENGTH = _env_int("CRM_MIN_MATCH_LENGTH", 4)

# ---------- Firmographics ----------
MIN_EMPLOYEES = _env_int("MIN_EMPLOYEES", 25)
MAX_EMPLOYEES = _env_int("MAX_EMPLOYEES", 1000)
REJECT_UNKNOWN_FIRMOGRAPHICS = _env_bool("REJECT_UNKNOWN_FIRMOGRAPHICS", False)
ENFORCE_FOUNDED_BEFORE = _env_bool("ENFORCE_FOUNDED_BEFORE", False)
FOUNDED_BEFORE_YEAR = _env_int("FOUNDED_BEFORE_YEAR", 2010)
ENABLE_BROADER_INDUSTRY_EXCLUSIONS = _env_bool(
    "ENABLE_BROADER_INDUSTRY_EXCLUSIONS", True
)

APOLLO_EXCLUDED_INDUSTRY_KEYWORDS = [
    "staffing and recruiting",
    "staffing",
    "recruiting",
    "government administration",
    "nonprofit organization management",
    "hospital & health care",
    "hospitals and health care",
    "health care",
    "healthcare",
    "mental health care",
    "mental health",
    "medical practice",
    "human resources services",
    "outsourcing/offshoring",
    "events services",
    "broadcast media",
    "newspapers",
    "book publishing",
    "chemicals",
]

# ---------- Apollo / Hunter ----------
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
APOLLO_RATE_LIMIT_DELAY = _env_float("APOLLO_RATE_LIMIT_DELAY", 0.6)
HUNTER_RATE_LIMIT_DELAY = _env_float("HUNTER_RATE_LIMIT_DELAY", 0.35)
VERIFY_WITH_HUNTER = _env_bool("VERIFY_WITH_HUNTER", True)
# Search is free, but each Apollo person match can consume credits. Try a small
# ranked set so one contact with no email does not discard an otherwise good account.
APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET = _env_int(
    "APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET", 3
)
HUNTER_MAX_FALLBACK_ATTEMPTS_PER_BUCKET = _env_int(
    "HUNTER_MAX_FALLBACK_ATTEMPTS_PER_BUCKET", 2
)
# Founders remain a legitimate fallback for genuinely small companies, but not
# for mid-market accounts where a functional leader should exist.
FOUNDER_FALLBACK_MAX_EMPLOYEES = _env_int(
    "FOUNDER_FALLBACK_MAX_EMPLOYEES", 99
)

# ---------- Airtable ----------
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "Leads")
AIRTABLE_RATE_LIMIT_DELAY = _env_float("AIRTABLE_RATE_LIMIT_DELAY", 0.25)
# Suppress a company already present anywhere in the review/outbound table, even
# when a new manager or role would otherwise generate a different Lead Key.
AIRTABLE_SUPPRESS_EXISTING_COMPANY = _env_bool(
    "AIRTABLE_SUPPRESS_EXISTING_COMPANY", True
)
AIRTABLE_STATUS_PENDING = os.getenv("AIRTABLE_STATUS_PENDING", "Pending")
AIRTABLE_STATUS_APPROVED = os.getenv("AIRTABLE_STATUS_APPROVED", "Approved")
AIRTABLE_STATUS_REJECTED = os.getenv("AIRTABLE_STATUS_REJECTED", "Rejected")
AIRTABLE_STATUS_ENROLLED = os.getenv("AIRTABLE_STATUS_ENROLLED", "Enrolled")
AIRTABLE_STATUS_ERROR = os.getenv("AIRTABLE_STATUS_ERROR", "Error")

# ---------- Instantly ----------
INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY", "")
INSTANTLY_BASE_URL = os.getenv("INSTANTLY_BASE_URL", "https://api.instantly.ai/api/v2")
INSTANTLY_CAMPAIGN_ID = os.getenv("INSTANTLY_CAMPAIGN_ID", "")
INSTANTLY_RATE_LIMIT_DELAY = _env_float("INSTANTLY_RATE_LIMIT_DELAY", 0.35)
INSTANTLY_VERIFY_ON_IMPORT = _env_bool("INSTANTLY_VERIFY_ON_IMPORT", False)

# Campaign routing. More-specific keys win over broader keys.
# Example env names:
# INSTANTLY_CAMPAIGN_MARKETING_SMALL, INSTANTLY_CAMPAIGN_MARKETING,
# INSTANTLY_CAMPAIGN_ENGINEERING_MID, INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS.
CAMPAIGN_ENV_BY_BUCKET = {
    "gtm_revenue": "INSTANTLY_CAMPAIGN_GTM",
    "engineering": "INSTANTLY_CAMPAIGN_ENGINEERING",
    "marketing": "INSTANTLY_CAMPAIGN_MARKETING",
    "customer_success": "INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS",
    "customer_support": "INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT",
    "finance": "INSTANTLY_CAMPAIGN_FINANCE",
    "operations": "INSTANTLY_CAMPAIGN_OPERATIONS",
    "people_hr": "INSTANTLY_CAMPAIGN_PEOPLE_HR",
    "product": "INSTANTLY_CAMPAIGN_PRODUCT",
    "ecommerce": "INSTANTLY_CAMPAIGN_ECOMMERCE",
}


def company_size_band(employee_count: int | None) -> str:
    if employee_count is None:
        return "unknown"
    if employee_count < 100:
        return "small"
    if employee_count < 500:
        return "mid"
    return "large"


def resolve_campaign_id(role_bucket: str, employee_count: int | None) -> str:
    band = company_size_band(employee_count).upper()
    base_env = CAMPAIGN_ENV_BY_BUCKET.get(role_bucket)
    if base_env:
        size_specific = os.getenv(f"{base_env}_{band}", "")
        if size_specific:
            return size_specific
        bucket_campaign = os.getenv(base_env, "")
        if bucket_campaign:
            return bucket_campaign
    return INSTANTLY_CAMPAIGN_ID


# Work-arrangement evidence from the 2.0 brief. JSearch's boolean remote flag
# is useful but not authoritative: live validation showed remote jobs whose
# title explicitly said "Remote" while job_is_remote was false. Strong text
# evidence therefore wins over the provider flag.
REMOTE_TITLE_LOCATION_PATTERNS = [
    r"\b100% remote\b",
    r"\bfully remote\b",
    r"\bremote[- ]first\b",
    r"\bwork from home\b",
    r"\bwfh\b",
    r"\bhome[- ]based\b",
    r"\bremote\b",
]

REMOTE_DESCRIPTION_PATTERNS = [
    r"\bthis is (?:a )?(?:full[- ]time,? |full(?:y)? |100% )?remote (?:job|position|role|opportunity)\b",
    r"\b(?:work|working) in a fully remote environment\b",
    r"\bthe (?:role|position) is fully remote\b",
    r"\bremote\s*[—–-]\s*full[- ]time\b",
    r"\b(?:job|work) location\s*:\s*(?:100% |fully )?remote\b",
    r"\blocation\s*:\s*(?:100% |fully )?remote\b",
    r"\bremote anywhere in (?:the )?united states\b",
    r"\bwork remotely from anywhere in (?:the )?united states\b",
    r"\banywhere in (?:the )?(?:u\.?s\.?|usa|united states)\b",
    r"\bopen to remote candidates\b",
    r"\bwork from home\b",
    r"\bhome[- ]based position\b",
]

# Title/location evidence is high precision. A title such as "Remote/Hybrid"
# is rejected because it still advertises an in-person operating model.
IN_PERSON_TITLE_LOCATION_PATTERNS = [
    r"\bon[- ]site\b",
    r"\bonsite\b",
    r"\bin[- ]person\b",
    r"\bhybrid\b",
    r"\boffice[- ]based\b",
]

# Description evidence must describe an actual requirement, not merely contain
# a word such as "onsite" in a product/channel context.
IN_PERSON_DESCRIPTION_PATTERNS = [
    r"\bthis is (?:an? )?(?:on[- ]site|onsite|in[- ]person|in[- ]office|hybrid) (?:job|position|role)\b",
    r"\bthis is (?:an? )?(?:on[- ]site|onsite|in[- ]office),?[^.\n]{0,40}\b(?:job|position|role)\b",
    r"\b(?:the )?(?:position|role) is (?:an? )?(?:on[- ]site|onsite|in[- ]office)\b",
    r"\b(?:must|required to|expected to) (?:work|be|report|come) (?:on[- ]site|onsite|in[- ]person|in (?:the|our) office)\b",
    r"\bwork from (?:the|our) office\b",
    r"\bmust (?:be able to )?commute\b",
    r"\bwithin commuting distance\b",
    r"\brelocation (?:is )?required\b",
    r"\b[1-5] days? (?:a|per) week in (?:the )?office\b",
    r"\bnot (?:a )?remote (?:job|position|role)\b",
    r"\bnot (?:a )?(?:traditional )?work[- ]from[- ]home role\b",
    r"\blittle to no work from home\b",
    r"\bsignificant portion or all work (?:must be|to be) performed in (?:a )?(?:scif|office|facility)\b",
    r"\bwork location\s*:\s*hybrid remote\b",
    r"\bhybrid remote in\b",
    r"\bfield[- ]based position\b",
    r"\bhybrid (?:work model|schedule|position|role)\b",
    r"\bin office (?:monday|tuesday|wednesday|thursday|friday|[1-5] days?)\b",
    # Covers reversed constructions such as "working from our Mountain View
    # office three days a week" that a provider may still label as remote.
    r"\b(?:work(?:ing)?|be|report(?:ing)?) from (?:the|our) [^.\n]{0,100}\boffice\b[^.\n]{0,80}\b(?:one|two|three|four|five|[1-5]) days? (?:a|per) week\b",
    r"\b(?:one|two|three|four|five|[1-5]) days? (?:a|per) week[^.\n]{0,80}\b(?:in|at|from) (?:the|our) [^.\n]{0,80}\boffice\b",
    r"\b(?:option|flexibility) (?:of|to) work(?:ing)? remotely for the remainder of the week\b",
    r"\b(?:required|mandatory|expected) in[- ]office (?:work|days?|attendance)\b",
    r"\b(?:monday|tuesday|wednesday|thursday|friday)(?:\s*,\s*(?:monday|tuesday|wednesday|thursday|friday))+(?:\s+and\s+(?:monday|tuesday|wednesday|thursday|friday))?[^.\n]{0,100}\b(?:office|on[- ]site|onsite)\b",
    r"\btravel (?:approximately |up to |minimum |at least )?(?:2[5-9]|[3-9]\d|100)%\b",
    r"\bfrequent travel\b",
    r"\btravel regularly to (?:client|customer) sites\b",
    r"\bestimated 1\+ day/?week\b",
]

# Explicit foreign-only eligibility overrides a noisy US country field.
FOREIGN_ONLY_ELIGIBILITY_PATTERNS = [
    r"\bremote role for (?:eu|european union|uk|canadian|australian) residents\b",
    r"\b(?:eu|european union|uk|canadian|australian) residents only\b",
    r"\bmust be (?:based|located|resident) in (?:the )?(?:eu|european union|uk|canada|australia|india|philippines|latam)\b",
    r"\bopen only to candidates (?:based|located) in (?:the )?(?:eu|european union|uk|canada|australia|india|philippines|latam)\b",
    r"\b(?:open|available) only to candidates in (?:the )?(?:eu|european union|uk|canada|australia|india|philippines|latam)\b",
    r"\bavailable only (?:to|for) (?:the )?(?:eu|european union|uk|canada|australia|india|philippines|latam)\b",
    r"\b(?:role|position|job) (?:is )?(?:fully )?remote (?:role )?based (?:with teams )?in (?:the )?(?:philippines|india|canada|australia|uk|europe|eu|latam)\b",
    r"\bfully remote role based (?:with teams )?in (?:the )?(?:philippines|india|canada|australia|uk|europe|eu|latam)\b",
]

NON_PAYING_JOB_PATTERNS = [
    r"\bunpaid\b",
    r"\bvolunteer (role|position|opportunity)\b",
    r"\bcommission[- ]only\b",
    r"\bequity[- ]only\b",
    r"\bno (financial )?compensation\b",
    r"\bwithout (financial )?compensation\b",
]

# Provider employment labels are not trusted when the title/description carries
# a stronger contradictory signal (for example, "Full-time" plus "15 hrs/wk").
NON_FULL_TIME_TITLE_PATTERNS = [
    r"\bpart[- ]time\b",
    r"\bcontractor\b",
    r"\bcontract (?:role|position|opportunity|job)\b",
    r"\btemporary (?:role|position|opportunity|job)\b",
    r"\btemp(?:orary)?[- ]to[- ]hire\b",
    r"\bfreelance(?:r)?\b",
    r"\bseasonal\b",
    r"\bper diem\b",
    r"\bfixed[- ]term\b",
    r"\b\d{1,2}[- ]month contract\b",
    r"\bcontract[- ]to[- ]hire\b",
    r"\bretainer\b",
    r"\btemporary\b",
    r"\bcontract\b",
    r"\b(?:up to|at least|approximately|minimum of)?\s*\d{1,2}\+?\s*(?:hours|hrs)(?:\s+per|/)\s*(?:week|wk)\b",
    r"\b\d{1,2}\s*[-–]\s*\d{1,2}\s*(?:hours|hrs)(?:\s+per|/)\s*(?:week|wk)\b",
]
NON_FULL_TIME_DESCRIPTION_PATTERNS = [
    r"\bthis is (?:a )?part[- ]time (?:role|position|job)\b",
    r"\b(?:seeking|hiring|looking for) (?:an? )?freelance(?:r)?\b",
    r"\bthis is (?:an? )?(?:freelance|independent contractor|project[- ]based|temporary) (?:role|position|job|engagement)\b",
    r"\b(?:independent contractor|freelance) position\b",
    r"\b(?:flexible )?project[- ]based work\b",
    r"\b(?:up to|at least|approximately|minimum of)?\s*\d{1,2}\+?\s*(?:hours|hrs)(?:\s+per|/)\s*(?:week|wk)\b",
    r"\b\d{1,2}\s*[-–]\s*\d{1,2}\s*(?:hours|hrs)(?:\s+per|/)\s*(?:week|wk)\b",
]
NON_FULL_TIME_EMPLOYMENT_TYPES = {
    "part time", "part-time", "contract", "contractor", "temporary",
    "temp", "freelance", "internship", "seasonal", "per diem",
}
NON_ACTIVE_HIRING_SIGNAL_PATTERNS = [
    r"\bfuture openings?\b",
    r"\bfuture opportunities\b",
    r"\bevergreen (?:role|position|opening)\b",
    r"\btalent pool\b",
    r"\btalent pipeline\b",
    r"\bexpression of interest\b",
    r"\bgeneral application\b",
    r"\bregister your interest\b",
]

# ---------- Filtering dictionaries ----------
STAFFING_EMPLOYER_KEYWORDS = [
    "staffing",
    "recruiting firm",
    "recruitment firm",
    "recruitment agency",
    "recruiting agency",
    "employment agency",
    "headhunt",
    "head hunt",
    "executive search",
    "talent solutions",
    "talent partners",
    "talent group",
    "placement agency",
    "placement services",
    "staffing solutions",
    "staffing services",
    "rpo",
    "recruiter",
    "recruiters",
    "recruits",
    "recruiting company",
    "search firm",
    "job placement",
]


# Job aggregators, publisher brands, and generic job-board "employers".
# These are filtered before enrichment so credits are not spent on a publisher
# that merely reposted another company's vacancy.
KNOWN_JOB_AGGREGATOR_EMPLOYERS = [
    "chatgpt jobs",
    "jobright",
    "jobright ai",
    "jobgether",
    "lensa",
    "bebee",
    "jooble",
    "talent com",
    "careerbuilder",
    "ziprecruiter",
    "adzuna",
    "jora",
    "whatjobs",
    "grabjobs",
    "jobleads",
    "remote rocketship",
    "remote jobs",
    "startup jobs",
    "tech jobs",
    "ai jobs",
    "msccn",
    "huzzle",
    "huzzle.com",
    "learn4good",
    "remoteleaf",
    "towardjobs",
    "toward jobs",
    "powertofly",
    "power to fly",
    "dice",
    "freelanceshop",
    "onlinejobs ph client",
    "freelance shop",
]

# Generic employer-name patterns are only used with corroborating evidence
# (for example, no employer website, matching publisher name, or aggregator
# language in the description). This avoids rejecting legitimate companies
# merely because the word "jobs" appears in their brand.
GENERIC_JOB_PUBLISHER_NAME_PATTERNS = [
    r"^jobs?$",
    r"^.+\s+jobs$",
    r"^jobs\s+.+$",
    r"^.+\s+careers$",
    r"^careers\s+.+$",
    r"^.+\s+job\s+board$",
    r"^.+\s+job\s+search$",
    r"^.+\s+career\s+portal$",
    r"^.+\s+job\s+portal$",
]

JOB_AGGREGATOR_DESCRIPTION_PHRASES = [
    "job board",
    "job search platform",
    "browse thousands of jobs",
    "find your next job",
    "this job was originally posted",
    "originally posted on",
    "we aggregate jobs",
    "aggregated from",
]

# Domains that identify an intermediary, ATS, or public job board rather than
# the hiring company's own website. They must never be used as a company domain.
INTERMEDIARY_JOB_DOMAINS = [
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "careerbuilder.com",
    "adzuna.com",
    "jooble.org",
    "talent.com",
    "lensa.com",
    "jobright.ai",
    "jobgether.com",
    "jora.com",
    "whatjobs.com",
    "grabjobs.co",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "workdayjobs.com",
    "icims.com",
    "smartrecruiters.com",
    "jobvite.com",
    "breezy.hr",
    "workable.com",
    "recruitee.com",
    "applytojob.com",
    "adp.com",
    "oraclecloud.com",
    "successfactors.com",
    "bamboohr.com",
    "personio.com",
    # Syndication/publisher domains observed in production. They may host a real
    # employer's listing, but they are never safe company identifiers for Apollo.
    "builtin.com",
    "builtinchicago.org",
    "builtinboston.com",
    "builtinnyc.com",
    "builtinla.com",
    "builtinaustin.com",
    "builtincolorado.com",
    "builtinseattle.com",
    "bebee.com",
    "jobleads.com",
    "salutemyjob.com",
    "trabajo.org",
    "virtualvocations.com",
    "jobilize.com",
    "simplyhired.com",
    "monster.com",
    "dice.com",
    "careerjet.com",
    "flexjobs.com",
    "remote.co",
    "wellfound.com",
    "railway.app",
    "unaux.com",
    "remotejobs.org",
    "mysmartpros.com",
    "clickclickjob.com",
    "tealhq.com",
    "learn4good.com",
    "himalayas.app",
    "dailyremote.com",
    "up2staff.com",
    "mediabistro.com",
    "recruit.net",
    "remoteleaf.com",
    "theladders.com",
    "climatetechlist.com",
    "liveblog365.com",
    "goparalegals.com",
    "dynamitejobs.com",
    "remotive.com",
]

KNOWN_OUTSOURCING_EMPLOYERS = [
    "concentrix", "teleperformance", "foundever", "sitel", "ttec",
    "alorica", "taskus", "transcom", "genpact", "wns", "conduent",
    "supportninja", "helpware", "cloudstaff", "bruntwork", "cyberbacker",
    "wing assistant", "wing assistants", "outsourced doers", "boldr",
    "remote staff", "athena", "somewhere", "agileengine",
    "anomaly squared", "cognizant", "brillio", "boldly", "cleardesk",
]

OUTSOURCING_DESCRIPTION_PATTERNS = [
    r"\bwe are (?:a|an) (?:global )?(?:business process outsourcing|bpo) (?:company|provider)\b",
    r"\bour (?:business process outsourcing|outsourcing) services\b",
    r"\bwe provide (?:virtual assistant|outsourced staffing|offshore staffing) services\b",
    r"\boutsourcing and offshoring consulting\b",
    r"\bwe (?:provide|deliver|offer) (?:outsourced )?(?:call|contact) center services\b",
    r"\bwe (?:provide|deliver|offer) outsourced (?:customer support|customer service|back[- ]office) services\b",
    r"\b(?:our|the) (?:agents?|representatives?|support teams?) (?:serve|support|are assigned to) (?:multiple |our )?clients?\b",
    r"\bmanaged (?:customer support|customer service|contact center) services for clients?\b",
]

KNOWN_STAFFING_EMPLOYERS = [
    "teksystems",
    "tek systems",
    "actalent",
    "aerotek",
    "allegis",
    "randstad",
    "robert half",
    "kelly services",
    "kforce",
    "insight global",
    "apex systems",
    "motion recruitment",
    "cybercoders",
    "hays",
    "adecco",
    "manpower",
    "spherion",
    "express employment",
    "staffmark",
    "pridestaff",
    "aquent",
    "synergisticit",
    "she recruits",
    "creative circle",
    "digital people",
    "virtual coworker",
    "icreatives",
    "jobot",
    "bcforward",
    "addison group",
    "vitamin t",
    "michael page",
    "modis",
    "experis",
    "tundra technical",
    "lensa",
    "bebee",
    "paired",
    "realynk assistants",
    "aston carter",
    "lasalle network",
    "stand 8",
    "gridiron it solutions",
    "my3tech",
    "baer group",
    "delphi-us",
    "linda werner associates",
    "clindcast",
    "digi axess",
    "bright vision technologies",
    "vava virtual assistants",
    "venraro",
    "recxchange",
    "qureos",
    "zillion technologies",
    "lancesoft",
    "gofasti",
    "my smart pros",
    "mysmartpros",
    "crossing hurdles",
    "remote talent cloud",
    "blueline search",
    "atomus partners",
    "inspyr solutions",
    "vmysmartpros",
    "jackson james",
]

VAGUE_EMPLOYER_SIGNALS = [
    "staff",
    "recruit",
    "talent",
    "placement",
    "search",
    "workforce",
    "undisclosed",
    "confidential",
]

# Strong first-person language indicating the employer itself is an intermediary.
STAFFING_DESCRIPTION_PHRASES = [
    "on behalf of our client",
    "on behalf of one of our clients",
    "we are a staffing agency",
    "we are a staffing firm",
    "we are a recruiting agency",
    "we are a recruiting firm",
    "our staffing agency",
    "our recruitment agency",
    "we place candidates",
    "we connect talent with employers",
    "direct hire placement services",
    "as an agency worker",
]

# Phrases that usually mean the direct employer is rejecting agency submissions.
STAFFING_NEGATION_PHRASES = [
    "no staffing agencies",
    "no recruitment agencies",
    "no recruiting agencies",
    "no agency submissions",
    "no third party recruiters",
    "no third-party recruiters",
    "we do not accept agency submissions",
    "we are not accepting agency submissions",
    "staffing agencies need not apply",
]

EXCLUDED_INDUSTRY_EMPLOYER_KEYWORDS = [
    "nonprofit",
    "non profit",
    "foundation",
    "charitable",
    "charity",
    "department of",
    "city of",
    "county of",
    "state of",
    "town of",
    "township of",
    "municipality of",
    "u s government",
    "federal government",
    "chemical manufacturing",
    "chemical company",
    "chemical corporation",
    "book publisher",
    "book publishing",
    "publishing house",
    "hospital",
    "health system",
    "medical center",
    "healthcare system",
    "healthcare",
    "health care",
    "health",
    "medical",
    "clinic",
    "diagnostics",
    "healthineers",
    "labcorp",
    "orthofix",
    "public radio",
    "public media",
    "arts alliance",
    "blue cross",
    "steris",
    "event planning",
    "consumer shows",
    "home shows",
    "bridal expo",
    "wedding expo",
    "event management company",
    "events company",
    "news network",
    "news outlet",
    "broadcasting company",
    "broadcast network",
    "television network",
    "radio network",
]

EXCLUDED_MEDIA_PRODUCTION_KEYWORDS = [
    "film production",
    "production studio",
    "media production company",
]

# High-confidence first-party descriptions for excluded industries. These are
# intentionally narrow so a software vendor serving nonprofits or healthcare
# clients is not excluded merely because the sector appears in the JD.
EXCLUDED_INDUSTRY_DESCRIPTION_PATTERNS = [
    r"\bwe are (?:a|an) (?:501\(c\)\(3\) |non[- ]?profit |nonprofit )?(?:organization|charity|foundation)\b",
    r"\bour (?:non[- ]?profit|nonprofit) organization\b",
    r"\b(?:a|an) national not[- ]for[- ]profit organization\b",
    r"\bmission[- ]driven (?:non[- ]?profit|ministry|religious organization)\b",
    r"\b(?:christian|faith[- ]based) ministry\b",
    r"\bregistered 501\(c\)\(3\)\b",
]

EXCLUDED_INDUSTRY_JOB_TITLE_KEYWORDS = [
    "clinical",
    "patient",
    "medical",
    "healthcare",
    "hospital",
    "diagnostics",
]

GOVERNMENT_WEBSITE_MARKERS = [".gov"]
FREELANCE_MARKETPLACE_EMPLOYERS = [
    "upwork",
    "fiverr",
    "toptal",
    "freelancer com",
    "peopleperhour",
    "mercor",
    "braintrust",
    "twine",
    "dataannotation",
    "toloka annotators",
    "the work app",
    "workada",
    "rex.zone",
    "review pays",
    "certified mobile notary",
    "the ai training company",
]
GOVERNMENT_JOB_BOARD_DOMAINS = ["governmentjobs.com", "usajobs.gov", "neogov.com"]

# ---------- Geography ----------
GENERIC_REMOTE_LOCATIONS = {
    "", "remote", "anywhere", "work from home", "united states", "usa", "us",
}
US_REMOTE_SCOPE_PATTERNS = [
    r"\bremote[ ,(/-]*(?:u\.?s\.?|usa|united states)\b",
    r"\bremote work within (?:the )?(?:u\.?s\.?|usa|united states)\b",
    r"\bremote role within (?:the )?(?:u\.?s\.?|usa|united states)\b",
    r"\bopen to candidates in (?:the )?(?:u\.?s\.?|usa|united states)\b",
    r"\b(?:continental|contiguous) (?:u\.?s\.?|usa|united states)\b",
    r"\b(?:u\.?s\.?|usa|united states)[ -]based\b",
    r"\bremote anywhere in (?:the )?united states\b",
    r"\bwork remotely from anywhere in (?:the )?united states\b",
    r"\banywhere in (?:the )?(?:u\.?s\.?|usa|united states)\b",
    r"\bmust (?:reside|live|be based|be located) in (?:the )?(?:u\.?s\.?|usa|united states)\b",
    r"\bopen to candidates (?:based|located) in (?:the )?(?:u\.?s\.?|usa|united states)\b",
    r"\b(?:u\.?s\.?|usa|united states) (?:residents|candidates) only\b",
    r"\bavailable (?:to candidates )?(?:from|in) [^.\n]{0,80}\b(?:u\.?s\.?|usa|united states)\b",
]
GLOBAL_REMOTE_PATTERNS = [
    r"\bglobal remote\b",
    r"\bremote worldwide\b",
    r"\bwork from anywhere in the world\b",
    r"\bworldwide remote\b",
]
# A generic ``Anywhere`` location plus the query's country echo is not proof of
# US eligibility. These markers catch explicit foreign locations before the
# provider country field is considered.
FOREIGN_CITY_URL_SLUGS = [
    "warsaw", "london", "toronto", "vancouver", "berlin", "paris",
    "madrid", "barcelona", "lisbon", "dublin", "amsterdam", "manila",
    "cebu", "mumbai", "bangalore", "bengaluru", "delhi", "sydney",
    "melbourne", "mexico-city", "sao-paulo", "bogota", "buenos-aires",
]
US_COUNTRY_CODES = {"us", "usa", "united states", "united states of america"}
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia", "puerto rico",
}
US_STATE_ABBREVS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv",
    "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn",
    "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc", "pr",
}
US_APPLY_LINK_SCOPE_PATTERNS = [
    r"(?:--|/)united-states(?:--|/|$)",
    r"(?:--|/)[a-z0-9-]+-(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)(?:--|/|\?|$)",
]

FOREIGN_COUNTRY_URL_SLUGS = [
    "germany", "canada", "mexico", "india", "france", "spain", "brazil",
    "philippines", "australia", "united-kingdom", "italy", "netherlands",
    "poland", "portugal", "ireland", "argentina", "colombia", "chile",
]
NON_US_LOCATION_MARKERS = [
    "canada", "mexico", "india", "united kingdom", "europe", "latam",
    "philippines", "australia", "italy", "germany", "france", "spain", "brazil",
    "malaysia", "belize", "ecuador", "western cape", "south africa",
    "poland", "apac", "emea",
]
NON_US_WEBSITE_TLDS = (
    ".it", ".de", ".fr", ".es", ".co.uk", ".ca", ".mx", ".in", ".au", ".br", ".nl", ".pl",
)
TRUSTED_US_JOB_BOARD_DOMAINS = [
    "builtinchicago.org", "builtinboston.com", "builtinnyc.com", "builtinla.com",
    "builtinaustin.com", "builtincolorado.com", "builtinseattle.com", "builtin.com",
]

# Approximate bounding box for the 50 US states + DC. Puerto Rico is handled by text fields.
US_LAT_MIN = 18.0
US_LAT_MAX = 72.0
US_LON_MIN = -179.0
US_LON_MAX = -66.0

# ---------- Derived dated path ----------
STEP2_KEPT_FILE = os.getenv(
    "STEP2_KEPT_FILE",
    str(Path(FILTERED_OUTPUT_DIR) / f"jobs_filtered_{datetime.now():%Y-%m-%d}.json"),
)
