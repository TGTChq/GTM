"""Central configuration for the TGTC job-intent outbound pipeline."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

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
OUTPUT_DIR = str(BASE_DIR / "data" / "raw")
FILTERED_OUTPUT_DIR = str(BASE_DIR / "data" / "filtered")
STEP3_OUTPUT_DIR = str(BASE_DIR / "data" / "enriched")
LOG_DIR = str(BASE_DIR / "logs")
STATE_DIR = str(BASE_DIR / "data" / "state")
RUN_SUMMARY_DIR = str(BASE_DIR / "logs" / "runs")
SEEN_JOBS_FILE = str(BASE_DIR / "data" / "state" / "seen_jobs.json")
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
# Reject only clearly stale job-intent signals before enrichment. Unknown or
# conflicting dates remain eligible; the oldest parseable source date is used.
MAX_JOB_AGE_DAYS = _env_int("MAX_JOB_AGE_DAYS", 30)

ROLES = _env_json(
    "ROLES_JSON",
    [
        "GTM Engineer",
        "AI Engineer",
        "Automation Specialist",
        "Graphic Designer",
        "Video Editor",
        "Performance Marketing Manager",
        "Customer Support",
        "Customer Success Manager",
    ],
)

EXCLUDED_TITLE_KEYWORDS = [
    "vp",
    "vice president",
    "director",
    "intern",
    "internship",
]

# ---------- Health gates ----------
MIN_JOBS_PER_RUN = _env_int("MIN_JOBS_PER_RUN", 10)
MIN_ROLES_WITH_RESULTS = _env_int("MIN_ROLES_WITH_RESULTS", 4)
MAX_ROLE_FAILURES = _env_int("MAX_ROLE_FAILURES", 3)
MIN_HIRING_MANAGER_MATCH_RATE = _env_float("MIN_HIRING_MANAGER_MATCH_RATE", 0.70)
ENFORCE_HM_MATCH_RATE = _env_bool("ENFORCE_HM_MATCH_RATE", False)

# Daily production throughput controls. The pipeline stops enrichment after it
# reaches the reviewable-lead target, with an eligible-company safety cap to
# bound Apollo/Hunter usage on low-contactability days.
TARGET_REVIEWABLE_LEADS_PER_RUN = _env_int("TARGET_REVIEWABLE_LEADS_PER_RUN", 30)
MAX_ELIGIBLE_COMPANIES_PER_RUN = _env_int("MAX_ELIGIBLE_COMPANIES_PER_RUN", 60)
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

# ---------- Airtable ----------
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "Leads")
AIRTABLE_RATE_LIMIT_DELAY = _env_float("AIRTABLE_RATE_LIMIT_DELAY", 0.25)
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
    "event planning",
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

GOVERNMENT_WEBSITE_MARKERS = [".gov"]
FREELANCE_MARKETPLACE_EMPLOYERS = [
    "upwork",
    "fiverr",
    "freelancer com",
    "peopleperhour",
]
GOVERNMENT_JOB_BOARD_DOMAINS = ["governmentjobs.com", "usajobs.gov", "neogov.com"]

# ---------- Geography ----------
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
FOREIGN_COUNTRY_URL_SLUGS = [
    "germany", "canada", "mexico", "india", "france", "spain", "brazil",
    "philippines", "australia", "united-kingdom", "italy", "netherlands",
    "poland", "portugal", "ireland", "argentina", "colombia", "chile",
]
NON_US_LOCATION_MARKERS = [
    "canada", "mexico", "india", "united kingdom", "europe", "latam",
    "philippines", "australia", "italy", "germany", "france", "spain", "brazil",
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
