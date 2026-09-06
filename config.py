"""Central configuration for the TGTC job-intent outbound pipeline."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

from role_catalog import DEFAULT_ACQUISITION_ROLES

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

# ---------- Acquisition mode ----------
# The production default combines every available zero-registration public
# source with JSearch when an existing RapidAPI key has usable quota. A failure
# in one acquisition source is isolated and cannot stop the remaining sources.
ACQUISITION_MODE = os.getenv("ACQUISITION_MODE", "multi_source").strip().lower()
MULTI_SOURCE_JSEARCH_ENABLED = _env_bool("MULTI_SOURCE_JSEARCH_ENABLED", True)
MULTI_SOURCE_JSEARCH_OPTIONAL = _env_bool("MULTI_SOURCE_JSEARCH_OPTIONAL", True)
# Independent recovery switch for multi-source deployments. This prevents an
# older Railway FINAL_PASS_TOPUP_ENABLED=0 value from silently disabling the
# optional JSearch deficit recovery introduced in v1.4.
MULTI_SOURCE_JSEARCH_TOPUP_ENABLED = _env_bool(
    "MULTI_SOURCE_JSEARCH_TOPUP_ENABLED", True
)
FREE_JOB_SOURCES = _env_json(
    "FREE_JOB_SOURCES_JSON",
    ["himalayas", "jobicy", "weworkremotely", "remotive", "remoteok"],
)
FREE_SOURCE_REQUEST_TIMEOUT_SECONDS = _env_int(
    "FREE_SOURCE_REQUEST_TIMEOUT_SECONDS", 20
)
FREE_SOURCE_MAX_RESPONSE_CHARS = _env_int(
    "FREE_SOURCE_MAX_RESPONSE_CHARS", 8_000_000
)
FREE_SOURCE_MAX_RECORDS_PER_SOURCE = _env_int(
    "FREE_SOURCE_MAX_RECORDS_PER_SOURCE", 1000
)
FREE_SOURCE_MIN_SUCCESSFUL_SOURCES = _env_int(
    "FREE_SOURCE_MIN_SUCCESSFUL_SOURCES", 2
)
# Jobicy's public v2 API has no documented offset/page parameter -- unlike
# Himalayas, a single `count` request is genuinely the whole retrieval
# mechanism available, not a code-side pagination gap. Raise the requested
# count above the previous hardcoded 50 so a single call captures more of
# what's available, and the fetch marks pagination_supported=False in its
# metadata so this remains an observable limitation, not a silent one.
JOBICY_REQUEST_COUNT = _env_int("JOBICY_REQUEST_COUNT", 200)
HIMALAYAS_PAGE_SIZE = _env_int("HIMALAYAS_PAGE_SIZE", 20)
HIMALAYAS_MAX_PAGES = _env_int("HIMALAYAS_MAX_PAGES", 25)
HIMALAYAS_COMPANY_PROFILE_MAX_REQUESTS = _env_int(
    "HIMALAYAS_COMPANY_PROFILE_MAX_REQUESTS", 30
)
# Stop hammering a public profile surface when access is consistently blocked.
# This is a per-run circuit breaker; it does not disable the source permanently.
HIMALAYAS_COMPANY_PROFILE_MAX_CONSECUTIVE_FAILURES = _env_int(
    "HIMALAYAS_COMPANY_PROFILE_MAX_CONSECUTIVE_FAILURES", 3
)
FREE_SOURCE_LANDING_DISCOVERY_ENABLED = _env_bool(
    "FREE_SOURCE_LANDING_DISCOVERY_ENABLED", True
)
FREE_SOURCE_LANDING_DISCOVERY_MAX_REQUESTS = _env_int(
    "FREE_SOURCE_LANDING_DISCOVERY_MAX_REQUESTS", 40
)
ATS_DIRECT_ACQUISITION_ENABLED = _env_bool(
    "ATS_DIRECT_ACQUISITION_ENABLED", True
)
ATS_BOARD_REFRESH_INTERVAL_HOURS = _env_int(
    "ATS_BOARD_REFRESH_INTERVAL_HOURS", 20
)
ATS_MAX_BOARDS_PER_RUN = _env_int("ATS_MAX_BOARDS_PER_RUN", 150)
ATS_MAX_JOBS_PER_BOARD = _env_int("ATS_MAX_JOBS_PER_BOARD", 250)
ATS_GREENHOUSE_DETAIL_MAX_REQUESTS_PER_BOARD = _env_int(
    "ATS_GREENHOUSE_DETAIL_MAX_REQUESTS_PER_BOARD", 25
)
ATS_GREENHOUSE_DETAIL_MAX_REQUESTS_PER_RUN = _env_int(
    "ATS_GREENHOUSE_DETAIL_MAX_REQUESTS_PER_RUN", 100
)
ATS_WORKDAY_MAX_PAGES_PER_BOARD = _env_int(
    "ATS_WORKDAY_MAX_PAGES_PER_BOARD", 5
)
ATS_WORKDAY_DETAIL_MAX_REQUESTS_PER_BOARD = _env_int(
    "ATS_WORKDAY_DETAIL_MAX_REQUESTS_PER_BOARD", 25
)
ATS_WORKDAY_DETAIL_MAX_REQUESTS_PER_RUN = _env_int(
    "ATS_WORKDAY_DETAIL_MAX_REQUESTS_PER_RUN", 100
)
# Cornerstone OnDemand (csod.com) tenant-based direct acquisition. See the
# UNVERIFIED-OFFLINE notes in ats_board_registry.py's fetch_board_jobs() --
# the real public API shape has not been confirmed against a live tenant.
ATS_CORNERSTONE_MAX_PAGES_PER_BOARD = _env_int(
    "ATS_CORNERSTONE_MAX_PAGES_PER_BOARD", 5
)
ATS_SMARTRECRUITERS_MAX_PAGES_PER_BOARD = _env_int(
    "ATS_SMARTRECRUITERS_MAX_PAGES_PER_BOARD", 3
)
ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_BOARD = _env_int(
    "ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_BOARD", 25
)
ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_RUN = _env_int(
    "ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_RUN", 100
)
ATS_SHADOW_FORCE_REFRESH_MAX_BOARDS = _env_int(
    "ATS_SHADOW_FORCE_REFRESH_MAX_BOARDS", 25
)
ATS_REGISTRY_AUTO_SEED_HISTORY = _env_bool(
    "ATS_REGISTRY_AUTO_SEED_HISTORY", True
)
ATS_REGISTRY_HISTORY_FILE_LIMIT = _env_int(
    "ATS_REGISTRY_HISTORY_FILE_LIMIT", 80
)
ATS_REGISTRY_MAX_HISTORY_FILE_BYTES = _env_int(
    "ATS_REGISTRY_MAX_HISTORY_FILE_BYTES", 25_000_000
)

# ---------- Deterministic ATS scheduler (Phase 1B-2B) ----------
# Off by default. ``legacy_interval`` reproduces the existing
# ``due_entries``-driven selection exactly; ``deterministic_partition`` enables
# slot-based, overdue-bounded, retry-bounded scheduling. None of these values
# changes behaviour while the mode is ``legacy_interval`` -- they are read only
# on the partitioned path. No Railway variable is renamed or repurposed.
ATS_SCHEDULER_MODE = os.getenv("ATS_SCHEDULER_MODE", "legacy_interval").strip().lower()
# Runs to cover the whole registry once, when position advances each run. With
# ~145 boards, cycle_length=3 attempts ~48 boards/run (+ any overdue) for full
# coverage every 3 runs (~1.5 days at 2 runs/day) at ~650 physical requests --
# ~54% of the 1200 ATS lane budget, leaving headroom for overdue/provider skew.
# Lower it further (2 => ~73/run, ~81% budget) only if faster coverage is worth
# the tighter margin. Requires the slot to actually rotate: run_orchestrator's
# ATS lane advances SchedulerConfig.position via the persisted scheduler state.
ATS_SCHEDULER_CYCLE_LENGTH = _env_int("ATS_SCHEDULER_CYCLE_LENGTH", 3)
# Which slot this run covers. A cron writes the run index here; ``position %
# cycle_length`` is the covered slot, so the sequence is stable and explicit.
ATS_SCHEDULER_POSITION = _env_int("ATS_SCHEDULER_POSITION", 0)
# Hard ceiling on boards attempted in one partitioned run. 0 means "no cap
# beyond ATS_MAX_BOARDS_PER_RUN".
ATS_SCHEDULER_BOARD_CAP = _env_int("ATS_SCHEDULER_BOARD_CAP", 0)
# A board older than this is overdue and bypasses its slot. 0 disables the
# freshness backstop (pure partitioning).
ATS_SCHEDULER_MAX_AGE_HOURS = _env_int("ATS_SCHEDULER_MAX_AGE_HOURS", 168)
# A board that has failed at most this many times is retried in the next run
# rather than waiting a whole cycle. Beyond it, the board falls back to its
# normal slot so a permanently broken board cannot monopolise the run.
ATS_SCHEDULER_MAX_RETRY_ATTEMPTS = _env_int("ATS_SCHEDULER_MAX_RETRY_ATTEMPTS", 2)
# Most overdue boards one run may take. Prevents the "whole registry overdue at
# once" thundering herd. 0 means unbounded (overdue is not quota-limited).
# When set it MUST be < the effective board cap so normal-slot work is never
# fully displaced.
ATS_SCHEDULER_OVERDUE_CAP = _env_int("ATS_SCHEDULER_OVERDUE_CAP", 0)
# Where the minimal scheduler state (carried-forward overdue keys) is persisted.
# Empty means no state file is written or read; the scheduler is then stateless
# and every run is a pure function of registry + config + position.
ATS_SCHEDULER_STATE_PATH = os.getenv("ATS_SCHEDULER_STATE_PATH", "").strip()

# ---------- Paths ----------
STATE_DIR = str(BASE_DIR / "data" / "state")
ARTIFACT_ROOT = Path(os.getenv("PIPELINE_ARTIFACT_ROOT", str(BASE_DIR / "data")))
OUTPUT_DIR = str(ARTIFACT_ROOT / "raw")
FILTERED_OUTPUT_DIR = str(ARTIFACT_ROOT / "filtered")
STEP3_OUTPUT_DIR = str(ARTIFACT_ROOT / "enriched")
LOG_DIR = str(ARTIFACT_ROOT / "logs")
RUN_SUMMARY_DIR = str(ARTIFACT_ROOT / "logs" / "runs")
EVIDENCE_OUTPUT_DIR = str(ARTIFACT_ROOT / "evidence")
SOURCE_CACHE_DIR = str(Path(STATE_DIR) / "source_cache")
ORGANIZATION_CACHE_DIR = str(Path(STATE_DIR) / "organization_cache")
REROUTE_STATE_FILE = str(Path(STATE_DIR) / "reroute_state.json")
RECOVERABLE_JOBS_FILE = str(Path(STATE_DIR) / "recoverable_jobs.json")
FINAL_PASS_INVENTORY_FILE = str(Path(STATE_DIR) / "final_pass_inventory.json")
OUTBOUND_COMPANY_CACHE_PATH = os.getenv(
    "OUTBOUND_COMPANY_CACHE_PATH",
    str(Path(STATE_DIR) / "company_display_cache.json"),
)
OUTBOUND_COMPANY_OVERRIDES_PATH = os.getenv(
    "OUTBOUND_COMPANY_OVERRIDES_PATH",
    str(BASE_DIR / "company_display_overrides.json"),
)
PIPELINE_CHECKPOINT_FILE = str(Path(STATE_DIR) / "pipeline_checkpoint.json")
PIPELINE_LOCK_FILE = str(Path(STATE_DIR) / "pipeline.lock")
SEEN_JOBS_FILE = str(Path(STATE_DIR) / "seen_jobs.json")
ATS_BOARD_REGISTRY_FILE = os.getenv(
    "ATS_BOARD_REGISTRY_FILE", str(Path(STATE_DIR) / "ats_board_registry.json")
)
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
    EVIDENCE_OUTPUT_DIR,
    SOURCE_CACHE_DIR,
    ORGANIZATION_CACHE_DIR,
):
    Path(directory).mkdir(parents=True, exist_ok=True)

# ---------- Final-pass architecture ----------
FINAL_PASS_PIPELINE_ENABLED = _env_bool("FINAL_PASS_PIPELINE_ENABLED", True)
VALIDATION_VERSION = "tgtc-ready-v1.4.7-role-display-2"
VALIDATION_SIGNING_KEY = os.getenv("VALIDATION_SIGNING_KEY", "")
# Source and company-site retrieval is bounded and cached.  Disabling fetches is
# intended only for deterministic offline replay; it does not relax any gate.
JOB_SOURCE_FETCH_ENABLED = _env_bool("JOB_SOURCE_FETCH_ENABLED", True)
JOB_SOURCE_MAX_CANDIDATES = _env_int("JOB_SOURCE_MAX_CANDIDATES", 8)
# Resolve supplied company/ATS job URLs before guessing careers paths. This is
# both faster and safer because the direct URL carries stronger provenance.
JOB_SOURCE_DIRECT_FIRST_ENABLED = _env_bool("JOB_SOURCE_DIRECT_FIRST_ENABLED", True)
# Generic company discovery is a bounded fallback, not a prerequisite for every
# posting. A small budget prevents inaccessible career sites from serially
# blocking the entire daily run.
JOB_SOURCE_DISCOVERY_MAX_PAGES = _env_int("JOB_SOURCE_DISCOVERY_MAX_PAGES", 4)
JOB_SOURCE_DISCOVERY_MAX_BOARD_PAGES = _env_int(
    "JOB_SOURCE_DISCOVERY_MAX_BOARD_PAGES", 2
)
JOB_SOURCE_DISCOVERY_BUDGET_SECONDS = _env_int(
    "JOB_SOURCE_DISCOVERY_BUDGET_SECONDS", 18
)
JOB_SOURCE_DISCOVERY_TIMEOUT_SECONDS = _env_int(
    "JOB_SOURCE_DISCOVERY_TIMEOUT_SECONDS", 5
)
JOB_SOURCE_MAX_REDIRECTS = _env_int("JOB_SOURCE_MAX_REDIRECTS", 5)
JOB_SOURCE_ATTEMPTS_PER_URL = _env_int("JOB_SOURCE_ATTEMPTS_PER_URL", 1)
JOB_SOURCE_TIMEOUT_SECONDS = _env_int("JOB_SOURCE_TIMEOUT_SECONDS", 8)
# When a recent direct company/ATS posting cannot be fetched because the site
# blocks bots or times out, retain recall only under a closed evidence contract:
# direct identity, recent timestamp, substantial description, and prefilter-
# corroborated full-time/remote/US facts. Approved enrollment still revalidates.
JOB_SOURCE_FRESH_DIRECT_FALLBACK_ENABLED = _env_bool(
    "JOB_SOURCE_FRESH_DIRECT_FALLBACK_ENABLED", True
)
JOB_SOURCE_FRESH_DIRECT_MAX_AGE_DAYS = _env_int(
    "JOB_SOURCE_FRESH_DIRECT_MAX_AGE_DAYS", 30
)
JOB_SOURCE_FRESH_DIRECT_MIN_DESCRIPTION_CHARS = _env_int(
    "JOB_SOURCE_FRESH_DIRECT_MIN_DESCRIPTION_CHARS", 700
)
# Aggregators and professional job networks are discovery evidence, not employer
# identity. A fresh provider record may enter human review only under this closed
# contract. It never bypasses Account/Contact/Email gates and must be revalidated
# against a trusted live source before Instantly enrollment.
JOB_SOURCE_PROVIDER_STRUCTURED_REVIEW_ENABLED = _env_bool(
    "JOB_SOURCE_PROVIDER_STRUCTURED_REVIEW_ENABLED", True
)
JOB_SOURCE_PROVIDER_STRUCTURED_MAX_AGE_DAYS = _env_int(
    "JOB_SOURCE_PROVIDER_STRUCTURED_MAX_AGE_DAYS", 30
)
JOB_SOURCE_PROVIDER_STRUCTURED_MIN_DESCRIPTION_CHARS = _env_int(
    "JOB_SOURCE_PROVIDER_STRUCTURED_MIN_DESCRIPTION_CHARS", 700
)
# Fantastic Direct API is a TRUSTED structured provider: it returns signed
# employment/US/title/employer/posted-date fields, so provider trust -- not the
# long-form-description length the aggregator path uses as a "substantial record"
# proxy -- is the basis for ACTIVE_PROVIDER_STRUCTURED review eligibility. The
# resolver relaxes ONLY the description-length bar, and ONLY for genuine Fantastic
# Direct API records (the adapter stamps `_fantastic_internal_id`); every other
# provider (JSearch, free feeds, adzuna, scraped LinkedIn, unknown) keeps the full
# 700-char bar. This never grants OFFICIAL_SOURCE and stays fail-closed at
# approved enrollment. Set the flag to 0 to fall back to the strict bar.
JOB_SOURCE_FANTASTIC_PROVIDER_STRUCTURED_ENABLED = _env_bool(
    "JOB_SOURCE_FANTASTIC_PROVIDER_STRUCTURED_ENABLED", True
)
# Default 0: the Fantastic Direct API structurally OMITS long-form descriptions
# (only AI-derived/structured fields are returned), so the record's trust basis is
# its signed provider fields (title, employer, FULL_TIME, US, posted date, apply
# URL) -- not description length. Requiring a description would re-block exactly
# the records this fix targets. Set > 0 only if a floor is later desired.
JOB_SOURCE_FANTASTIC_PROVIDER_STRUCTURED_MIN_DESCRIPTION_CHARS = _env_int(
    "JOB_SOURCE_FANTASTIC_PROVIDER_STRUCTURED_MIN_DESCRIPTION_CHARS", 0
)
JOB_SOURCE_CACHE_TTL_HOURS = _env_int("JOB_SOURCE_CACHE_TTL_HOURS", 24)
JOB_SOURCE_MAX_ACTIVE_AGE_DAYS = _env_int("JOB_SOURCE_MAX_ACTIVE_AGE_DAYS", 45)
COMPANY_SOURCE_FETCH_ENABLED = _env_bool("COMPANY_SOURCE_FETCH_ENABLED", True)
COMPANY_SOURCE_MAX_PAGES = _env_int("COMPANY_SOURCE_MAX_PAGES", 3)
COMPANY_SOURCE_TIMEOUT_SECONDS = _env_int("COMPANY_SOURCE_TIMEOUT_SECONDS", 10)
COMPANY_SOURCE_CACHE_TTL_HOURS = _env_int("COMPANY_SOURCE_CACHE_TTL_HOURS", 168)
FINAL_PASS_MICROBATCH_QUERY_UNITS = _env_int("FINAL_PASS_MICROBATCH_QUERY_UNITS", 6)
FINAL_PASS_MAX_TOPUP_ITERATIONS = _env_int("FINAL_PASS_MAX_TOPUP_ITERATIONS", 2)
# Multi-source recovery has its own limit so a stale Railway value from older
# JSearch-only deployments cannot stop deficit recovery after two batches. Zero
# means keep searching until the FINAL_PASS minimum, request budget, runtime,
# inventory exhaustion, or downstream-yield circuit breaker ends the loop.
MULTI_SOURCE_FINAL_PASS_MAX_TOPUP_ITERATIONS = _env_int(
    "MULTI_SOURCE_FINAL_PASS_MAX_TOPUP_ITERATIONS", 0
)
FINAL_PASS_MAX_RUNTIME_SECONDS = _env_int("FINAL_PASS_MAX_RUNTIME_SECONDS", 1800)
# Soft wall-clock budget for the run_orchestrator enrichment loop (0 = unlimited).
# A fail-safe valve for large daily runs (e.g. the 3,500-job Fantastic target): on
# expiry the loop stops taking NEW companies, every already-enriched company stays
# checkpointed (no re-consumed Apollo, no duplicate Airtable rows via lead_key
# idempotency), and the run ends INCOMPLETE with stop_reason
# "enrichment_runtime_budget_reached" so a later run resumes. Never truncates
# silently: default 0 processes every eligible company.
ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS = _env_int("ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS", 0)
FINAL_PASS_MAX_EMPTY_QUERY_CYCLES = _env_int(
    "FINAL_PASS_MAX_EMPTY_QUERY_CYCLES", 2
)
DRIFT_AUDIT_SAMPLE_SIZE = _env_int("DRIFT_AUDIT_SAMPLE_SIZE", 10)

# ---------- READY inventory and source corroboration ----------
# FINAL_PASS remains the persisted compatibility label; operationally it means READY.
READY_INVENTORY_TARGET = _env_int("READY_INVENTORY_TARGET", 30)
# Zero means deliver every READY lead found in the run. The commercial target
# remains a minimum success threshold, never a maximum or processing cap.
READY_DAILY_DELIVERY_LIMIT = _env_int("READY_DAILY_DELIVERY_LIMIT", 0)
CONTINUE_AFTER_FINAL_PASS_TARGET = _env_bool(
    "CONTINUE_AFTER_FINAL_PASS_TARGET", True
)
# Pre-contact capacity controller (Phase 13 section 5). Default OFF so
# deployment does not silently change acquisition behavior until explicitly
# activated. Targets are in *unique canonical searchable companies* per day.
CAPACITY_CONTROLLER_ENABLED = _env_bool("CAPACITY_CONTROLLER_ENABLED", False)
SEARCHABLE_COMPANY_DAILY_TARGET = _env_int("SEARCHABLE_COMPANY_DAILY_TARGET", 250)
SEARCHABLE_COMPANY_HEADROOM_TARGET = _env_int("SEARCHABLE_COMPANY_HEADROOM_TARGET", 300)
READY_INVENTORY_TTL_DAYS = _env_int("READY_INVENTORY_TTL_DAYS", 2)
PIPELINE_LOCK_STALE_HOURS = _env_int("PIPELINE_LOCK_STALE_HOURS", 6)
JOB_SOURCE_MIN_INDEPENDENT_PUBLISHERS = _env_int(
    "JOB_SOURCE_MIN_INDEPENDENT_PUBLISHERS", 2
)
JOB_SOURCE_ALLOW_CORROBORATED = _env_bool("JOB_SOURCE_ALLOW_CORROBORATED", True)
# Explicit aliases cover observed rebrands/legacy ATS tenants without weakening
# identity checks globally. Override or extend through JSON env variables.
COMPANY_NAME_ALIASES = _env_json(
    "COMPANY_NAME_ALIASES_JSON",
    {"magnitude software": "insightsoftware"},
)
COMPANY_DOMAIN_ALIASES = _env_json(
    "COMPANY_DOMAIN_ALIASES_JSON",
    {"magnitudesoftware.com": "insightsoftware.com"},
)
TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES = _env_int(
    "TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES", 2
)
MULTI_SOURCE_TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES = _env_int(
    "MULTI_SOURCE_TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES", 4
)
ROLE_ALLOW_SENIOR_IC = _env_bool("ROLE_ALLOW_SENIOR_IC", True)

# ---------- JSearch ----------
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
JSEARCH_HOST = os.getenv("JSEARCH_HOST", "jsearch.p.rapidapi.com")
JSEARCH_ENDPOINT = os.getenv("JSEARCH_ENDPOINT", "https://jsearch.p.rapidapi.com/search-v2")
DATE_POSTED = os.getenv("DATE_POSTED", "month")
COUNTRY = os.getenv("COUNTRY", "us")
NUM_PAGES = _env_int("NUM_PAGES", 1)
SEARCH_DELAY_SECONDS = _env_float("SEARCH_DELAY_SECONDS", 0.8)
# Acquisition uses representative role families; returned rows are classified
# locally against the complete Brett-approved catalog. Zero disables only the
# corresponding request guard.
JSEARCH_MAX_QUERIES_PER_RUN = _env_int("JSEARCH_MAX_QUERIES_PER_RUN", 0)
# Guard estimated request units before the first API call. READY v1 uses 50
# strategically complete acquisition roles at one page each, leaving bounded room for
# adaptive deepening without returning to the 118-query fan-out.
JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN = _env_int(
    "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 150
)
# Independent from JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN, which gates only the
# base pass's own request-unit guard (jsearch_scraper.py). Before this setting
# existed, final_pass_topup.py drew its per-microbatch budget from whatever
# was left of that SAME 150-unit pool after the base pass ran -- an adaptive
# base pass that deepens or looks back heavily could starve top-up down to
# nearly nothing, with no way to give top-up its own allowance without also
# inflating the base pass's ceiling. 0 means unlimited (paced by
# FINAL_PASS_MICROBATCH_QUERY_UNITS per round, bounded by the run's other
# circuit breakers -- iteration limit, runtime, zero-downstream-yield --
# exactly like every other "0 = unlimited" JSearch setting in this file).
JSEARCH_TOPUP_UNIT_BUDGET = _env_int("JSEARCH_TOPUP_UNIT_BUDGET", 150)
JSEARCH_STOP_ON_LOW_QUOTA = _env_bool("JSEARCH_STOP_ON_LOW_QUOTA", True)
JSEARCH_MIN_REMAINING_REQUESTS = _env_int("JSEARCH_MIN_REMAINING_REQUESTS", 500)
# Onsite and hybrid postings are valid intent signals when the role itself can
# be delivered remotely. Do not bias acquisition toward remote-only inventory.
JSEARCH_REMOTE_JOBS_ONLY = _env_bool("JSEARCH_REMOTE_JOBS_ONLY", False)
JSEARCH_REMOTE_QUERY_BIAS = _env_bool("JSEARCH_REMOTE_QUERY_BIAS", False)
# Keep the provider's supported remote-filter parameter available for explicit
# diagnostics, but leave it disabled in production: onsite and hybrid postings
# are valid intent signals when the underlying role is remotely deliverable.
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
JSEARCH_TOPUP_DATE_WINDOWS = _env_json(
    "JSEARCH_TOPUP_DATE_WINDOWS",
    ["week", "month"],
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
# JSearch may discover the complete 0-30-day candidate inventory; the local
# primary/recovery age gates remain authoritative at 0-14 and 15-30 days.
JSEARCH_ADAPTIVE_LOOKBACK = _env_bool("JSEARCH_ADAPTIVE_LOOKBACK", True)
JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED = os.getenv(
    "JSEARCH_ADAPTIVE_LOOKBACK_DATE_POSTED", "month"
)
JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES = _env_int(
    "JSEARCH_ADAPTIVE_LOOKBACK_MAX_QUERIES", 16
)
JSEARCH_TARGET_PREFILTER_VIABLE = _env_int(
    "JSEARCH_TARGET_PREFILTER_VIABLE", 60
)
# Legacy closed-loop inventory controls remain for rollback compatibility. In
# strict mode the final-pass loop uses small bounded micro-batches, validates
# every candidate fully and counts only FINAL_PASS.
JSEARCH_REVIEWABLE_TOPUP_ENABLED = _env_bool(
    "JSEARCH_REVIEWABLE_TOPUP_ENABLED", True
)
# Legacy strict-mode switch retained for JSearch-only deployments. Multi-source
# mode uses MULTI_SOURCE_JSEARCH_TOPUP_ENABLED so stale Railway values cannot
# silently disable deficit recovery.
FINAL_PASS_TOPUP_ENABLED = _env_bool(
    "FINAL_PASS_TOPUP_ENABLED", JSEARCH_REVIEWABLE_TOPUP_ENABLED
)
JSEARCH_TOPUP_INITIAL_PAGES = _env_int("JSEARCH_TOPUP_INITIAL_PAGES", 1)
JSEARCH_TOPUP_MAX_ROUNDS = _env_int("JSEARCH_TOPUP_MAX_ROUNDS", 3)
JSEARCH_TOPUP_MAX_UNITS_PER_ROUND = _env_int(
    "JSEARCH_TOPUP_MAX_UNITS_PER_ROUND", 84
)
JSEARCH_TOPUP_PAGES_PER_QUERY = _env_int(
    "JSEARCH_TOPUP_PAGES_PER_QUERY", 3
)
JSEARCH_TOPUP_MAX_PAGE = _env_int("JSEARCH_TOPUP_MAX_PAGE", 4)
JSEARCH_TOPUP_PREFILTER_MULTIPLIER = _env_float(
    "JSEARCH_TOPUP_PREFILTER_MULTIPLIER", 4.0
)
JSEARCH_TOPUP_MIN_PREFILTER_TARGET = _env_int(
    "JSEARCH_TOPUP_MIN_PREFILTER_TARGET", 20
)
# Age is staged rather than globally loosened. The normal pass accepts jobs up
# to 14 days old. If fewer than the minimum FINAL_PASS target survive, the same
# acquired inventory is reprocessed for active jobs aged 15-30 days.
PRIMARY_MAX_JOB_AGE_DAYS = _env_int("PRIMARY_MAX_JOB_AGE_DAYS", 14)
RECOVERY_MIN_JOB_AGE_DAYS = _env_int("RECOVERY_MIN_JOB_AGE_DAYS", 15)
RECOVERY_MAX_JOB_AGE_DAYS = _env_int("RECOVERY_MAX_JOB_AGE_DAYS", 30)
AGE_RECOVERY_ENABLED = _env_bool("AGE_RECOVERY_ENABLED", True)
# Compatibility alias used by older tests and helper scripts.
# Compatibility alias. PRIMARY_MAX_JOB_AGE_DAYS is authoritative; an older
# Railway value must not block a production run or silently change the window.
MAX_JOB_AGE_DAYS = PRIMARY_MAX_JOB_AGE_DAYS

# ---------- Tiered freshness beyond the 0-30 day window ----------
# 31-60 and 61-90 day postings are not admitted merely for being younger than
# 90 days -- they require corroborating current-active evidence (see
# freshness_policy.py). This is a distinct, additive tier above the existing
# 0-14 (primary) / 15-30 (recovery) window; it is inactive unless
# EXTENDED_AGE_RECOVERY_ENABLED explicitly turns on the acquisition pass that
# reprocesses inventory in this window.
RECOVERY_EXTENDED_MIN_JOB_AGE_DAYS = _env_int("RECOVERY_EXTENDED_MIN_JOB_AGE_DAYS", 31)
RECOVERY_EXTENDED_MAX_JOB_AGE_DAYS = _env_int("RECOVERY_EXTENDED_MAX_JOB_AGE_DAYS", 60)
RECOVERY_DEEP_MIN_JOB_AGE_DAYS = _env_int("RECOVERY_DEEP_MIN_JOB_AGE_DAYS", 61)
RECOVERY_DEEP_MAX_JOB_AGE_DAYS = _env_int("RECOVERY_DEEP_MAX_JOB_AGE_DAYS", 90)
# Default OFF (Phase 13 §2): the 31-90 day extended pass is a capacity-
# expansion lane that must not silently change production behavior until
# explicitly activated. With it off, only the base 0-14 and recovery 15-30
# lanes run, exactly as before this patch. The report and this default now
# agree.
EXTENDED_AGE_RECOVERY_ENABLED = _env_bool("EXTENDED_AGE_RECOVERY_ENABLED", False)
# Title keywords used as one conservative, evidence-based signal that a role is
# difficult to fill (required, in addition to confirmed-active evidence, for a
# 61-90 day posting to be admitted -- spec-mandated stricter bar for that tier).
DIFFICULT_TO_FILL_TITLE_KEYWORDS = [
    "senior",
    "sr.",
    "staff",
    "principal",
    "lead",
    "specialist",
    "architect",
]

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
# Controlled recall recovery. These policies move trustworthy but incomplete
# records into the existing human-review/revalidation lane; they never bypass
# Account, Contact, Email, CRM, approval, or pre-send source revalidation.
ALLOW_ACTIVE_GREENHOUSE_UNKNOWN_AGE_REVIEW = _env_bool(
    "ALLOW_ACTIVE_GREENHOUSE_UNKNOWN_AGE_REVIEW", True
)
ALLOW_GLOBAL_REMOTE_US_INCLUSIVE_REVIEW = _env_bool(
    "ALLOW_GLOBAL_REMOTE_US_INCLUSIVE_REVIEW", True
)
ALLOW_STRUCTURED_IDENTITY_CONFLICT_REVIEW = _env_bool(
    "ALLOW_STRUCTURED_IDENTITY_CONFLICT_REVIEW", True
)

ROLES = _env_json("ROLES_JSON", list(DEFAULT_ACQUISITION_ROLES))

# Global title exclusions from Brett's Intent-Based Outbound 2.0 rules.
# Phrase matching is word-boundary based in jsearch_scraper.py.
EXCLUDED_TITLE_KEYWORDS = [
    "vp",
    "vice president",
    "director",
    "intern",
    "internship",
    # NOTE: "head of" is intentionally NOT a job-title exclusion. It is not one of
    # the documented TGTC job exclusions (Intern/Director/VP/Senior/Event
    # Marketing/Field Marketing/in-person/non-paying), and excluding it wrongly
    # dropped high-recall individual-contributor postings whose title merely
    # contains "Head". (Hiring-manager titles like "Head of Growth" are a separate
    # concern handled in role_mapping, not here.)
    "event marketing",
    "field marketing",
]

# ---------- Adzuna (official) ----------
# Kept disabled by default in every committed/default configuration
# (FINAL_30_PLUS_SYSTEM_SPEC.md section 11). adzuna_client.py reads these same
# variable names directly and does not import this module, so either surface
# can be used to configure it; these constants exist so the orchestrator
# (multi_source_acquisition.py) has one place, consistent with every other
# source, to read Adzuna's enabled/credential state without importing secrets
# through a second path.
ADZUNA_ENABLED = _env_bool("ADZUNA_ENABLED", False)
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
ADZUNA_COUNTRY = os.getenv("ADZUNA_COUNTRY", "us")
ADZUNA_RESULTS_PER_PAGE = _env_int("ADZUNA_RESULTS_PER_PAGE", 50)
ADZUNA_MAX_PAGES_PER_QUERY = _env_int("ADZUNA_MAX_PAGES_PER_QUERY", 3)
ADZUNA_MAX_REQUESTS_PER_RUN = _env_int("ADZUNA_MAX_REQUESTS_PER_RUN", 40)
ADZUNA_MAX_DAYS_OLD = _env_int("ADZUNA_MAX_DAYS_OLD", 30)
ADZUNA_TIMEOUT_SECONDS = _env_int("ADZUNA_TIMEOUT_SECONDS", 20)
# Role-catalog-derived query portfolio (Phase 13 section 4). Default OFF: when
# disabled, Adzuna (if enabled at all) uses only the small fallback query list,
# so turning ADZUNA_ENABLED on does not silently trigger a large query fan-out
# until the portfolio is explicitly activated too.
ADZUNA_QUERY_PORTFOLIO_ENABLED = _env_bool("ADZUNA_QUERY_PORTFOLIO_ENABLED", False)
ADZUNA_MAX_QUERIES_PER_RUN = _env_int("ADZUNA_MAX_QUERIES_PER_RUN", 12)
ADZUNA_PORTFOLIO_MAX_PAGES_PER_QUERY = _env_int("ADZUNA_PORTFOLIO_MAX_PAGES_PER_QUERY", 2)
ADZUNA_PORTFOLIO_FRESHNESS_WINDOWS = _env_json("ADZUNA_PORTFOLIO_FRESHNESS_WINDOWS", [30])
ADZUNA_PORTFOLIO_REMOTE_VARIANTS = _env_json("ADZUNA_PORTFOLIO_REMOTE_VARIANTS", ["", "remote"])
ADZUNA_MARGINAL_MIN_NEW_COMPANIES = _env_int("ADZUNA_MARGINAL_MIN_NEW_COMPANIES", 1)

# ---------- Fantastic.jobs (additive, disabled by default) ----------
# Additive acquisition source. When FANTASTIC_JOBS_ENABLED is falsey the source
# is never attempted and production behaviour is byte-for-byte the baseline.
FANTASTIC_JOBS_ENABLED = _env_bool("FANTASTIC_JOBS_ENABLED", False)
FANTASTIC_JOBS_API_KEY = os.getenv("FANTASTIC_JOBS_API_KEY", "")  # never logged/dumped
FANTASTIC_JOBS_BASE_URL = os.getenv("FANTASTIC_JOBS_BASE_URL", "https://data.fantastic.jobs")
FANTASTIC_JOBS_TIME_FRAME = os.getenv("FANTASTIC_JOBS_TIME_FRAME", "24h")
FANTASTIC_JOBS_REQUEST_TIMEOUT_SECONDS = _env_int("FANTASTIC_JOBS_REQUEST_TIMEOUT_SECONDS", 30)
FANTASTIC_JOBS_MAX_RETRIES = _env_int("FANTASTIC_JOBS_MAX_RETRIES", 2)
FANTASTIC_JOBS_MAX_JOBS_PER_RUN = _env_int("FANTASTIC_JOBS_MAX_JOBS_PER_RUN", 400)
FANTASTIC_JOBS_ATS_LIMIT = _env_int("FANTASTIC_JOBS_ATS_LIMIT", 300)
FANTASTIC_JOBS_WELLFOUND_LIMIT = _env_int("FANTASTIC_JOBS_WELLFOUND_LIMIT", 50)
FANTASTIC_JOBS_YCOMBINATOR_LIMIT = _env_int("FANTASTIC_JOBS_YCOMBINATOR_LIMIT", 40)
FANTASTIC_JOBS_LINKEDIN_LIMIT = _env_int("FANTASTIC_JOBS_LINKEDIN_LIMIT", 10)
FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING = _env_int("FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING", 90)
FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING = _env_int("FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING", 20)
FANTASTIC_JOBS_FAIL_OPEN = _env_bool("FANTASTIC_JOBS_FAIL_OPEN", True)
# --- Adaptive net-new top-up (COMMIT 3) -----------------------------------
# Three DISTINCT concepts (see orchestrator/topup.py); do NOT conflate with the
# --target CLI arg, which stays the FINAL_PASS reporting/SLA semantic.
#   B) Production yield target: desired NET-NEW + send_safe_facts-PASS + actually-
#      created-in-Airtable leads. 0 (default) = OFF => the pipeline runs its normal
#      single pass. When > 0, the orchestrator keeps acquiring fresh continuation
#      slices to REPLACE duplicates / existing rows / cheap rejects / non-send-safe
#      results until the target is met or a hard boundary is reached.
NET_NEW_SEND_SAFE_TARGET = _env_int("NET_NEW_SEND_SAFE_TARGET", 0)
#   A) Acquisition safety cap = FANTASTIC_JOBS_MAX_JOBS_PER_RUN (above): the hard
#      ceiling on Fantastic jobs BILLED across all top-up slices in one run.
#   Per-iteration slice size (each slice bills at most this many jobs; cumulative
#   billing is still clamped to the safety cap).
FANTASTIC_TOPUP_SLICE_JOBS = _env_int("FANTASTIC_TOPUP_SLICE_JOBS", 500)
#   Per-iteration RUNTIME slice budget (0 = no slice cap). The top-up loop sets this
#   for one acquisition iteration so the adapter bills at most this many jobs THAT
#   iteration. It is deliberately DECOUPLED from FANTASTIC_JOBS_MAX_JOBS_PER_RUN (the
#   global, config-validated safety ceiling): a slice of 500 with LINKEDIN_LIMIT=6000
#   is valid because validation checks the ceiling (6000<=6000) while the adapter
#   clamps this iteration's acquisition to the slice. Cumulative billing across slices
#   stays bounded by the ceiling via the TopUpController. Never persisted; runtime-only.
FANTASTIC_JOBS_RUN_SLICE_CAP = _env_int("FANTASTIC_JOBS_RUN_SLICE_CAP", 0)
#   C) Runtime-budget and iteration-guard boundaries (never-infinite-loop).
TOPUP_RUNTIME_BUDGET_SECONDS = _env_int("TOPUP_RUNTIME_BUDGET_SECONDS", 0)  # 0 = no runtime cap
TOPUP_MAX_ITERATIONS = _env_int("TOPUP_MAX_ITERATIONS", 40)
FANTASTIC_JOBS_DESCRIPTION_FORMAT = os.getenv("FANTASTIC_JOBS_DESCRIPTION_FORMAT", "text").strip().lower()
# LinkedIn-scope ICP filters pushed INTO the Direct API request so out-of-scope
# inventory is never billed or paged. Only parameters confirmed against the live
# /v1/active-jb contract are sent: location, organization_headcount_gte,
# ai_employment_type, organization_agency. The API has no headcount-maximum
# parameter, so the upper bound (config.MAX_EMPLOYEES) stays a downstream gate.
# Downstream TGTC gates remain the authoritative US/headcount/employment/staffing
# filter (defense-in-depth); these only reduce wasted credits/pages.
FANTASTIC_JOBS_LOCATION = os.getenv("FANTASTIC_JOBS_LOCATION", "United States").strip()
FANTASTIC_JOBS_HEADCOUNT_MIN = _env_int("FANTASTIC_JOBS_HEADCOUNT_MIN", 25)
FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE = os.getenv("FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE", "FULL_TIME").strip()
FANTASTIC_JOBS_EXCLUDE_AGENCY = _env_bool("FANTASTIC_JOBS_EXCLUDE_AGENCY", True)
# Per-segment pagination ceiling (pages of up to 100 jobs). Configurable so the
# architecture is not permanently capped: at 100 jobs/page the default 50 pages =
# 5,000 jobs/segment. The 3,500 jobs/day production target needs 35 pages, well
# within the default; raise this only alongside a larger MAX_JOBS_PER_RUN and plan.
FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT = _env_int("FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT", 50)
# Cross-run continuation cursor (default off preserves the exact baseline). The
# Direct API feed is date_posted DESC with no stable `order`/id-keyset, and the
# ONLY proven date filter is the `date_posted_lt` upper bound (there is NO
# lower-bound/`date_posted_gte` parameter). Continuation therefore tracks TWO edges
# of the single stream:
#   * DEEP/backfill edge (cursor_date): each run resumes strictly OLDER than the
#     oldest job already acquired (date_posted_lt = cursor_date + 1s), so deep
#     batches never re-fetch/re-bill the prefix; the boundary second is re-included
#     and deduped by stable IDs so no job is skipped.
#   * FRESH edge (high_water): newly-posted jobs sit at the TOP of the DESC feed, so
#     each run FIRST pages from the top and stops client-side once it crosses below
#     the prior high_water (see FANTASTIC_JOBS_ACQUIRE_MODE). This needs no new API
#     parameter and, being independent of the deep cursor, a completed historical
#     crawl can never starve daily discovery of new arrivals.
# State persists on the volume so a run is resumable and both resume points are
# explicit. The head pass advances high_water (+ its boundary IDs); the deep pass
# advances cursor_date (+ its boundary IDs); an empty run rewrites neither.
# Title-targeted acquisition. The broad LinkedIn feed is ~4% target-role, so a
# live smoke proved broad retrieval wastes credits. Instead query the Direct API
# `title` (substring) param per target-role FAMILY -- variants collapse into one
# term ("Account Executive" also returns "Enterprise/Technical Account Executive")
# to minimise cross-query overlap. Title targeting is recall-first; the downstream
# classify-then-verify RoleGate remains the mandatory precision filter.
# Upstream headcount MAX (organization_headcount_lt, confirmed supported live). The
# closed benchmark's actor filtered headcount 25-999; the Direct API omitting it
# let 41% of companies (>1000) reach and fail the ICP gate, halving retention and
# wasting ~45% of credits. 0 disables. Downstream MAX_EMPLOYEES stays as defense.
FANTASTIC_JOBS_HEADCOUNT_MAX = _env_int("FANTASTIC_JOBS_HEADCOUNT_MAX", 1000)
# title_advanced: ONE Boolean OR-expression over the whole role catalog (the exact
# benchmark approach). One query returns the union counting each job ONCE -> zero
# cross-query billing overlap and 118/118 coverage, unlike per-family `title=`
# queries. Preferred when enabled; blank expression is built from role_catalog.
FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED = _env_bool("FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED", True)
FANTASTIC_JOBS_TITLE_ADVANCED_EXPRESSION = os.getenv("FANTASTIC_JOBS_TITLE_ADVANCED_EXPRESSION", "").strip()
FANTASTIC_JOBS_TITLE_TARGETING_ENABLED = _env_bool("FANTASTIC_JOBS_TITLE_TARGETING_ENABLED", False)
DEFAULT_FANTASTIC_TITLE_FAMILIES = (
    # GTM / Sales
    "Account Executive", "Account Manager", "Business Development Representative",
    "Sales Development Representative", "Inside Sales", "Sales Operations",
    "Revenue Operations", "Sales Enablement", "Partnerships Manager", "Lead Generation",
    # Customer Success / Support
    "Customer Success", "Customer Support", "Customer Experience",
    "Implementation Specialist", "Technical Support", "Community Manager",
    # Engineering / Data
    "Software Engineer", "Frontend Developer", "Backend Developer", "Full Stack Developer",
    "Cloud Engineer", "DevOps Engineer", "QA Engineer", "Data Analyst", "Data Engineer",
    "Data Scientist", "Business Intelligence", "Systems Administrator", "Database Administrator",
    # Finance
    "Accountant", "Bookkeeper", "Payroll Specialist", "Financial Analyst", "FP&A",
    "Billing Specialist", "Collections Specialist", "Accounts Payable", "Accounts Receivable",
    # Marketing / Creative
    "Content Marketing", "Digital Marketing", "Email Marketing", "Performance Marketing",
    "Growth Marketing", "Product Marketing", "Marketing Automation", "Brand Manager",
    "Marketing Coordinator", "Marketing Analyst", "SEO Specialist", "Paid Media",
    "Social Media Manager", "Copywriter", "Content Writer", "Graphic Designer",
    "UX/UI Designer", "Web Designer", "Motion Designer", "Video Editor", "Video Producer",
    # People / HR
    "Recruiter", "Talent Acquisition", "People Operations", "HR Generalist", "HR Analyst",
    "HR Administrator", "Benefits Administrator", "Compensation Analyst", "Learning & Development",
    "Recruiting Coordinator",
    # Operations
    "Operations Analyst", "Business Operations", "Executive Assistant",
    "Administrative Assistant", "Virtual Assistant", "Project Coordinator", "Data Entry",
    # Product
    "Product Manager", "Product Analyst", "Product Designer", "Technical Writer",
    # AI
    "AI Engineer", "Machine Learning Engineer", "Prompt Engineer", "AI Operations",
    "Automation Specialist", "Conversational AI", "GTM Engineer", "Data Labeling", "Chatbot",
    # Ecommerce
    "E-commerce Manager", "Amazon Marketplace", "Shopify", "Catalog Specialist", "Listings Specialist",
    # Coverage completion (the 18 roles the collapsed set missed -> 118/118)
    "Customer Onboarding", "Customer Retention", "Customer Operations", "QA Analyst",
    "AP Specialist", "AR Specialist", "PPC", "Lifecycle Marketing", "CRM Marketing",
    "HR Operations", "Deal Desk", "CRM Administrator", "AI Automation", "AI Content",
    "Data Annotator", "Podcast", "Product Support",
)
FANTASTIC_JOBS_TITLE_FAMILIES = _env_json(
    "FANTASTIC_JOBS_TITLE_FAMILIES_JSON", list(DEFAULT_FANTASTIC_TITLE_FAMILIES)
)
FANTASTIC_JOBS_CONTINUATION_ENABLED = _env_bool("FANTASTIC_JOBS_CONTINUATION_ENABLED", False)
FANTASTIC_JOBS_CONTINUATION_STATE_PATH = os.getenv(
    "FANTASTIC_JOBS_CONTINUATION_STATE_PATH",
    str(Path(STATE_DIR) / "fantastic_continuation.json"),
)
# Acquisition phase for the single-stream continuation cursor:
#   "head_then_deep" (default) -- discover jobs newer than the prior high_water
#      (fresh edge, top of the DESC feed, client-side stop; no new API param), then
#      continue the backward date_posted_lt crawl to fill the remaining cap.
#   "head" -- fresh edge only.  "deep" -- backward backfill only.
# The pipeline sets this per phase so the top-up loop bills the head query at most
# once per run (slice 1 = head_then_deep, later slices = deep). Ignored when
# continuation is disabled (a single plain current-window fetch).
FANTASTIC_JOBS_ACQUIRE_MODE = os.getenv("FANTASTIC_JOBS_ACQUIRE_MODE", "head_then_deep").strip().lower()

# ---------- Yield-optimization architecture (2026-08) ----------
# Category 1 (SAFE TO ENABLE at the next cutover) vs Category 2 (IMPLEMENTED, TESTED,
# DEFAULT OFF until post-reset validation). A code deploy must NEVER activate a
# Category-2 behavior: every one defaults OFF below.
#
# --- Monthly Fantastic credit governor (P0; Category 1) ----------------------
# The 20,000 Jobs/month plan is a HARD binding constraint. The governor is the
# spending AUTHORITY for a run; NET_NEW_SEND_SAFE_TARGET is only an aspiration that
# may stop a run early, never raise its budget. See orchestrator/fantastic_governor.py.
FANTASTIC_MONTHLY_GOVERNOR_ENABLED = _env_bool("FANTASTIC_MONTHLY_GOVERNOR_ENABLED", False)
FANTASTIC_MONTHLY_JOBS_LIMIT = _env_int("FANTASTIC_MONTHLY_JOBS_LIMIT", 20000)
FANTASTIC_MONTHLY_RESERVE_PCT = float(os.getenv("FANTASTIC_MONTHLY_RESERVE_PCT", "0.10") or 0.10)
FANTASTIC_DAILY_MIN_JOBS = _env_int("FANTASTIC_DAILY_MIN_JOBS", 100)      # conservative floor
FANTASTIC_DAILY_MAX_JOBS = _env_int("FANTASTIC_DAILY_MAX_JOBS", 0)        # 0 = no daily ceiling
FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS = _env_bool("FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS", True)
FANTASTIC_GOVERNOR_USE_COUNT_HINT = _env_bool("FANTASTIC_GOVERNOR_USE_COUNT_HINT", False)
FANTASTIC_GOVERNOR_CARRY_CAP_DAYS = float(os.getenv("FANTASTIC_GOVERNOR_CARRY_CAP_DAYS", "3") or 3.0)
# AUTO-ARM: when the governor flag is switched on MID-CYCLE, arming immediately
# would grant 0 (remaining already below the reserve) and strand an expiring
# remainder. With auto-arm the already-running cycle keeps legacy drain behaviour
# and the governor takes control automatically at the next billing-cycle rollover
# -- no later variable change, no human action at reset. Missing/corrupt arm state
# fails ARMED (bounded spend is the conservative outcome).
FANTASTIC_GOVERNOR_AUTO_ARM = _env_bool("FANTASTIC_GOVERNOR_AUTO_ARM", True)
# Explicit reset date fallback (ISO) when the provider doesn't expose one; the
# provider header x-api-next-billing-date wins when present.
FANTASTIC_BILLING_RESET_AT = os.getenv("FANTASTIC_BILLING_RESET_AT", "").strip()
FANTASTIC_GOVERNOR_LEDGER_PATH = os.getenv(
    "FANTASTIC_GOVERNOR_LEDGER_PATH", str(Path(STATE_DIR) / "fantastic_governor_ledger.json"))
# Last-known provider quota snapshot (written by every acquisition; read by the
# governor at the start of the next run so it never needs a row-producing call).
FANTASTIC_QUOTA_SNAPSHOT_PATH = os.getenv(
    "FANTASTIC_QUOTA_SNAPSHOT_PATH", str(Path(STATE_DIR) / "fantastic_quota_snapshot.json"))

# --- Server-side industry exclusion (PROVEN param: exclude_organization_industry) -
# JSON list of EXACT Fantastic/LinkedIn taxonomy labels. Never translated from Apollo
# keywords. Only "Hospitals and Health Care" is proven (live count probe: honored;
# historical: 82 billed jobs -> 0 FINAL_PASS). Additional labels stay unconfigured
# until proven. The downstream ICP gate remains intact (defense-in-depth).
# VALIDATED SET (per-label historical evidence, 1,696-posting corpus): each label
# produced 0 FINAL_PASS AND its records were rejected downstream for a STRUCTURAL
# POLICY reason (REJECT_HEALTHCARE / REJECT_GOVERNMENT / REJECT_EXCLUDED_INDUSTRY),
# so excluding it upstream saves the credit without removing convertible inventory.
# Labels whose zero-yield came from UNVERIFIED/NEEDS_CHECK (an enrichment-coverage
# miss, NOT a policy reject) are deliberately EXCLUDED from this set.
# Sent as ONE comma-joined value (schema: array, style=form, explode=false).
FANTASTIC_EXCLUDED_ORG_INDUSTRIES = _env_json(
    "FANTASTIC_EXCLUDED_ORG_INDUSTRIES_JSON", [
        "Hospitals and Health Care",                     # n=82, REJECT_HEALTHCARE
        "Home Health Care Services",                     # n=23, 'health care' policy
        "Government Administration",                     # n=21, REJECT_GOVERNMENT
        "Non-profit Organizations",                      # n=18, REJECT_EXCLUDED_INDUSTRY
        "Mental Health Care",                            # n=11, REJECT_HEALTHCARE
        "Medical Practices",                             # n=10, REJECT_HEALTHCARE
        "Public Relations and Communications Services",  # n=10, REJECT_EXCLUDED_INDUSTRY
    ])
FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED = _env_bool(
    "FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED", False)

# --- Title query: scoped tsquery negation (PROVEN operator: "& !term") -----------
# Scoped per role family so a contaminant is excluded only where it is junk:
#   {"Community Manager": ["apartment","leasing","property management","hoa"], ...}
# Only contexts with PROVEN zero FINAL_PASS collision are configured by default.
FANTASTIC_TITLE_SCOPED_EXCLUSIONS_ENABLED = _env_bool(
    "FANTASTIC_TITLE_SCOPED_EXCLUSIONS_ENABLED", False)
FANTASTIC_TITLE_SCOPED_EXCLUSIONS = _env_json(
    "FANTASTIC_TITLE_SCOPED_EXCLUSIONS_JSON",
    {"Community Manager": ["apartment", "leasing", "property management", "hoa", "homeowners"],
     "Automation Specialist": ["building automation", "hvac"],
     "Systems Administrator": ["clearance", "polygraph"],
     "Technical Writer": ["clearance"],
     "Data Scientist": ["clearance"]})
# Global negations apply to EVERY clause; only tokens proven collision-free across all
# FINAL_PASS titles belong here ("clearance" = 55 billed rows, 0 FINAL_PASS).
FANTASTIC_TITLE_GLOBAL_EXCLUSIONS = _env_json(
    "FANTASTIC_TITLE_GLOBAL_EXCLUSIONS_JSON", ["clearance"])
FANTASTIC_TITLE_GLOBAL_EXCLUSIONS_ENABLED = _env_bool(
    "FANTASTIC_TITLE_GLOBAL_EXCLUSIONS_ENABLED", False)
# Alias/recall fixes for titles whose "/" or compound form collapses into an
# unintended token-AND phrase. Always applied (pure recall, zero-cost).
FANTASTIC_TITLE_ALIASES_ENABLED = _env_bool("FANTASTIC_TITLE_ALIASES_ENABLED", False)  # Gate-E D1
FANTASTIC_TITLE_ALIASES = _env_json("FANTASTIC_TITLE_ALIASES_JSON", {
    "UX/UI Designer": ["ux designer", "ui designer", "ux ui designer", "ui ux designer"],
    "Frontend Developer": ["frontend developer", "front end developer", "front-end developer"],
})

# --- date_created watermark acquisition (Category 2; DEFAULT OFF) ----------------
# PROVEN on the production API (2026-08-22 probe): date_created_gte/lt both honored.
# Safe form ONLY: upper = now - lag; lower = prev_watermark - overlap; advance the
# watermark to `upper` only after the interval is fully processed+persisted.
# Lag/overlap are conservative placeholders pending commit-lag validation.
FANTASTIC_DATE_CREATED_WATERMARK_ENABLED = _env_bool("FANTASTIC_DATE_CREATED_WATERMARK_ENABLED", False)

#: How many CONSECUTIVE all-duplicate pages a source will pay to page past inside
#: one run, when it has a DURABLE cursor to make progress with.
#:
#: A full page of already-seen rows does NOT prove the query is exhausted, with or
#: without a cursor: it says the rows at THIS offset are ones we hold, and nothing
#: about what lies deeper. That is why ``no_new_ids`` is not in ``_DRAINED_STOPS``
#: and can never advance the watermark.
#:
#: What changes with a durable cursor is what stopping COSTS. Without one, the
#: offset dies with the run, so paging on would spend on rows the next run must
#: re-buy from zero regardless -- stopping is a budget decision. With one, the
#: offset survives, so paging on is the only mechanism that ever reaches the tail,
#: and stopping throws it away.
#:
#: Measured 2026-09-05: the first canonical-window run after the cursor shipped
#: found a window whose first 3,000 rows per source a previous (pre-cursor) run had
#: already bought, re-paged from 0, hit a duplicate page immediately and stopped.
#: 200 rows billed, 0 net-new, cursor advanced by 100. At one page per run that is
#: ~30 runs and ~6,000 credits to return to where the earlier run had already
#: reached, with the window stale and nothing acquired throughout.
#:
#: 40 pages is 4,000 rows -- enough to cross a full per-source cap in one run. The
#: run cap still bounds total spend; this only bounds the pathological window whose
#: whole length is overlap, so one run cannot spend its entire budget on nothing.
FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES = _env_int(
    "FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES", 40)

#: How far ABOVE the ``time_frame`` horizon a window's lower bound is held.
#:
#: The feed intersects ``time_frame`` with ``date_created`` -- proven 2026-09-05 by
#: two count requests (zero Jobs credits): a window lying entirely below the frame
#: returned 0 rows with ``time_frame=7d`` and 24,784 without it. So the horizon
#: ``now - time_frame`` is a hard floor on every window, and it advances with the
#: clock. Two attempts to query WITHOUT it failed -- a 45s read timeout on the full
#: window, then HTTP 504 at 240s on a 5.95-day slice carrying the production title
#: expression. That establishes those two request shapes are not servable. It does
#: NOT establish that no partition is: a materially narrower slice, or the same
#: range without the 4,222-character ``title_advanced``, was never tried, and either
#: might answer. Treat "we must send time_frame" as the current operating
#: constraint, not as a proven property of the API.
#:
#: DEFAULT 0, and that is deliberate.
#:
#: This was introduced at 30 minutes to "keep a window from being clamped to an edge
#: it then slides off while it is still paging". It cannot do that. The horizon moves
#: with the clock for as long as a run lasts, and runs last hours -- 20260904T130130Z
#: ran 13:01:30 -> 16:21:24, three hours and twenty minutes. A 30-minute margin does
#: not cover that, and no fixed margin can: the only margin that survives a run is
#: one wider than the run, which concedes more inventory than it protects.
#:
#: What it DID do is raise every window's floor 30 minutes above the provider's real
#: floor, discarding half an hour of reachable inventory on every window, for a
#: purpose that was never demonstrated. So the default is 0: the clamp targets the
#: provider's actual floor and concedes nothing beyond it.
#:
#: The knob remains because the floor is the provider's, not ours, and if it is ever
#: shown to be approximate a small margin is the right response. Raising it should
#: cite the measurement that justified it. ``frame_floor`` and ``frame_horizon`` are
#: reported separately so the provider's floor and our margin never get confused.
#: The four frames the provider documents for active-jb / active-ats. Kept here so
#: configuration is validated against them at import; the adapter re-exports the same
#: tuple as SUPPORTED_TIME_FRAMES.
_SUPPORTED_TIME_FRAMES = ("1h", "24h", "7d", "6m")


# ---------- functional (task-based) discovery ----------
# WHY A SEPARATE SEGMENT, not an addition to the existing query.
#
# The provider ANDs the `_advanced` parameters together at the query level. So
# sending `description_advanced` ALONGSIDE `title_advanced` can only NARROW the
# existing result set -- it cannot reach a single job the title filter already
# excluded. Title synonyms have the same ceiling: they widen the list of titles, and
# a job whose title is nothing like ours stays invisible however many aliases we add.
#
# Reaching work under an unfamiliar title therefore needs its own request, carrying
# `description_advanced` and NO `title_advanced`, with every other ICP filter intact.
# That is what this segment is. Downstream RoleGate, ICP, firmographic and send-safe
# decisions stay authoritative over everything it returns -- the segment widens what
# is CONSIDERED, never what is approved.
#
# Two-key gate, the same shape Wellfound and Y Combinator use: an explicit flag AND a
# non-zero limit, so a code deploy can never start paying for it. Default OFF and
# UNEVALUATED: it is implemented and reachable, and its incremental yield has not
# been measured. `description_advanced` is also rejected on `time_frame=6m` (HTTP
# 400), so this segment is only valid on 1h/24h/7d.
# EVALUATED 2026-09-05, AND THE THREE CONCLUSIONS ARE DIFFERENT CLAIMS.
#
# 1. CLASSIFICATION/INTEGRATION BEHAVIOUR: TESTED.
#    A functional row has no matched title family, so RoleGate had no target to
#    verify against and every row it returned was UNVERIFIED by construction. That
#    was an integration gap in this path -- not evidence about the postings -- and it
#    is fixed: functional rows now run the same `_classify` step the external-batch
#    path uses, which assigns a catalog role and a relevance assessment. A weak fit
#    is rejected there and stays reviewable. Nothing is inferred from task keywords.
#
# 2. OVERLAP WITH THE EXISTING QUERY: MEASURED, on 4,231 cached postings with real
#    descriptions, no request and no spend:
#
#      functional expression matched a description       418
#        of which the title query already reaches        416   (99.5%)
#        reachable ONLY by the functional query            2
#
#    Both unique hits were "Lifecycle Marketing Manager", and both are ROLE-RELEVANT
#    once classified (status `review`, 1-2 points) -- reviewable, not rejected and
#    not approved-quality. An earlier note here called them unusable; that was an
#    artifact of testing for a verbatim catalog title instead of running the
#    classifier, and it was wrong. Worth recording: the catalog defines "Performance
#    Marketing Manager" AND "Performance Marketing Specialist" but only "Lifecycle
#    Marketing Specialist", so the Manager variant of that one family is a catalog
#    gap rather than a deliberate exclusion -- no seniority rule excludes it.
#
# 3. LIVE INCREMENTAL ACQUISITION YIELD: UNMEASURED, and this corpus cannot measure
#    it. It came from an acquisition that itself used title targeting -- 4,125 of
#    4,231 titles match the deployed expression -- so postings outside the title
#    filter are largely absent BY CONSTRUCTION. The measured 2 is a FLOOR on a
#    title-selected sample, not an estimate of unseen inventory.
#
# Kept OFF pending evidence that does not exist yet. What the overlap does establish
# is a cost: a segment run beside the title query would spend most of its grant
# re-buying rows the title query returns anyway, and those are billed on arrival.
FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED = _env_bool(
    "FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED", False)
FANTASTIC_JOBS_FUNCTIONAL_LIMIT = _env_int("FANTASTIC_JOBS_FUNCTIONAL_LIMIT", 0)
#: Boolean expression over the job DESCRIPTION, in the provider's syntax
#: (`&` AND, `|` OR, `!` NOT, `'...'` phrase, `(...)` grouping). Empty means the
#: segment cannot run: a description query with no expression is an unrestricted
#: description search, which is exactly what must not be issued.
FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED = os.getenv(
    "FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED", "")

FANTASTIC_TIME_FRAME_MARGIN_MINUTES = _env_int("FANTASTIC_TIME_FRAME_MARGIN_MINUTES", 0)

FANTASTIC_DATE_CREATED_LAG_MINUTES = _env_int("FANTASTIC_DATE_CREATED_LAG_MINUTES", 180)
FANTASTIC_DATE_CREATED_OVERLAP_MINUTES = _env_int("FANTASTIC_DATE_CREATED_OVERLAP_MINUTES", 60)
FANTASTIC_WATERMARK_STATE_PATH = os.getenv(
    "FANTASTIC_WATERMARK_STATE_PATH", str(Path(STATE_DIR) / "fantastic_watermark.json"))
# Visibility-lag SELF-AUDIT: how many closed date_created windows to retain for
# later re-counting. Each run re-checks ONE with the count endpoint (0 Jobs
# credits) to measure whether rows appear AFTER a window was declared complete --
# replacing the assumed 180-minute lag with observed evidence over time.
FANTASTIC_WATERMARK_AUDIT_ENABLED = _env_bool("FANTASTIC_WATERMARK_AUDIT_ENABLED", True)
FANTASTIC_WATERMARK_AUDIT_KEEP = _env_int("FANTASTIC_WATERMARK_AUDIT_KEEP", 12)

# --- active-ats source (Category 2; DEFAULT OFF; FANTASTIC_JOBS_ATS_LIMIT stays 0 in prod)
# Complementary, non-overlapping with active-jb(exclude_ats_duplicate=true).
FANTASTIC_ATS_SOURCE_ENABLED = _env_bool("FANTASTIC_ATS_SOURCE_ENABLED", False)
# Run-local ATS circuit breaker: if the ATS segment errors, or >this fraction of a
# meaningful sample fails schema mapping, ATS is abandoned FOR THAT RUN and active-jb
# continues. Never mutates configuration.
FANTASTIC_ATS_MAX_SCHEMA_REJECT_RATE = float(
    os.getenv("FANTASTIC_ATS_MAX_SCHEMA_REJECT_RATE", "0.5") or 0.5)
FANTASTIC_ATS_APPLY_TITLE_ADVANCED = _env_bool("FANTASTIC_ATS_APPLY_TITLE_ADVANCED", True)
FANTASTIC_ATS_APPLY_ICP_FILTERS = _env_bool("FANTASTIC_ATS_APPLY_ICP_FILTERS", True)

# --- Multi-source union (Category 2; Wellfound/YC DEFAULT OFF) -----------------
# Wellfound and Y Combinator were previously reachable ONLY from the final `else`
# of a mutually-exclusive if/elif/else, so with title_advanced active (production)
# their segments were dead code regardless of their limits. They are now
# INDEPENDENT segments, gated by an explicit source flag AND a non-zero limit --
# exactly the two-key pattern the active-ats source already uses, so a code deploy
# can never activate them.
FANTASTIC_WELLFOUND_SOURCE_ENABLED = _env_bool("FANTASTIC_WELLFOUND_SOURCE_ENABLED", False)
FANTASTIC_YCOMBINATOR_SOURCE_ENABLED = _env_bool("FANTASTIC_YCOMBINATOR_SOURCE_ENABLED", False)
# How ONE governor run_cap is divided across the enabled source segments:
#   "fair_share" -- every enabled segment is first reserved an equal
#       floor (run_cap // n, capped at its own limit); whatever a segment does not
#       spend cascades to later segments. Source mix then depends on configured
#       limits and real inventory, never on dispatch ORDER.
#   "sequential" (DEFAULT) -- each segment may draw the whole remaining budget in
#       order. Retained for comparison/rollback only.
# Both enforce the SAME hard invariant: sum(billed) <= run_cap.
#
# DEFAULT IS "sequential" ON PURPOSE. With ATS=6000, LinkedIn=6000 and a grant of
# ~3164, sequential lets ATS consume the ENTIRE run and leave LinkedIn zero, so
# fair_share would materially change the live ATS/LinkedIn MIX. We have not yet
# measured which source yields better qualified leads per credit, so switching the
# mix is an unvalidated product change and must not ride along with a credit-safety
# fix. The credit OVERSPEND bug is fixed independently of this policy, so keeping
# sequential is safe -- it is exactly today's behaviour.
# Experiment arms set fair_share explicitly (level field for comparison), and
# enabling a 3rd/4th source in production should set it explicitly too.
FANTASTIC_SOURCE_ALLOCATION = os.getenv("FANTASTIC_SOURCE_ALLOCATION", "sequential").strip().lower()
# ROUND-BASED RECLAMATION (fair_share only). Round 1 gives every enabled source its
# equal floor; a later round re-shares whatever the sparse sources did not consume
# among the sources that can still spend it. Without this, a source that returns
# nothing still holds its reservation until AFTER the productive sources have run,
# so enabling two sparse sources stranded HALF the run budget (measured: 1582 of
# 3164). Bounded and deterministic: the loop also stops as soon as a round bills
# nothing, so this is a ceiling, not a target.
FANTASTIC_SOURCE_MAX_ALLOCATION_ROUNDS = _env_int("FANTASTIC_SOURCE_MAX_ALLOCATION_ROUNDS", 4)

# --- Source-aware server-side firmographics ------------------------------------
# PROVEN LIVE 2026-09-04 with 0-credit /v1/active-jb-count probes: Fantastic
# populates NO organization headcount and NO industry on Wellfound or Y Combinator
# rows. `organization_headcount_gte` and `exclude_organization_industry` are a >=
# and a NOT-IN predicate, so each DROPS NULLS -- and each independently took both
# sources from real inventory to exactly zero:
#     wellfound US 542 -> headcount_gte=1 -> 0 ;  industry exclusions -> 0
#     ycombinator  182 -> headcount_gte=1 -> 0 ;  industry exclusions -> 0
#     linkedin  717915 -> headcount_gte=1 -> 701230 (compatible)
# gte=1 -- not 25 -- proves the FIELD IS ABSENT rather than the companies small.
# For these sources the firmographic predicates are omitted and ICP is enforced
# downstream by account_gate on APOLLO facts, which is already authoritative for
# PASS (a missing employee count or industry yields NEEDS_CHECK, never PASS).
# Filters PROVEN compatible are still sent: source token, US location, window,
# ai_employment_type (542->502, 182->174) and title targeting (542->159, 182->41).
FANTASTIC_FIRMOGRAPHIC_INCOMPATIBLE_SOURCES = _env_json(
    "FANTASTIC_FIRMOGRAPHIC_INCOMPATIBLE_SOURCES_JSON", ["wellfound", "ycombinator"])

# --- First-enablement bootstrap ------------------------------------------------
# A source enabled for the first time has never inspected inventory OLDER than the
# canonical window that the already-live sources have advanced past. Without a
# backfill its historical inventory would be invisible forever, which defeats the
# point of a recall expansion.
# The bootstrap is deliberately BOUNDED to the configured acquisition lookback
# (FANTASTIC_JOBS_TIME_FRAME) -- never unlimited history -- has its OWN progress
# state, spends the SAME run/provider budget, and can neither advance nor rewind
# the production canonical watermark. Steady-state participation starts
# immediately and independently, so no forward gap can open while it backfills.
FANTASTIC_SOURCE_BOOTSTRAP_ENABLED = _env_bool("FANTASTIC_SOURCE_BOOTSTRAP_ENABLED", True)
# GUARANTEED PROGRESS. Running the backfill purely on leftover budget starves it
# forever whenever the steady-state sources can fill the whole run_cap -- the
# historical window would then never be inspected. So while ANY bootstrap is
# pending, a reserve is withheld from steady state and released to bootstrap.
# The size is DERIVED from the allocator's existing fair-share semantics rather
# than an invented percentage: bootstrap is treated as N additional claimants in
# the split, i.e. reserve = run_cap * shares / (enabled_sources + shares).
# 1 = bootstrap gets one source's worth of the run. 0 disables the reserve
# (leftover-only, and therefore starvable).
FANTASTIC_BOOTSTRAP_RESERVE_SHARES = _env_int("FANTASTIC_BOOTSTRAP_RESERVE_SHARES", 1)

# --- JB-vs-ATS source experiment (Category 2; explicit opt-in only) --------------
SOURCE_EXPERIMENT_ENABLED = _env_bool("SOURCE_EXPERIMENT_ENABLED", False)
SOURCE_EXPERIMENT_MIN_PER_ARM = _env_int("SOURCE_EXPERIMENT_MIN_PER_ARM", 100)
SOURCE_EXPERIMENT_MAX_BUDGET = _env_int("SOURCE_EXPERIMENT_MAX_BUDGET", 600)
SOURCE_EXPERIMENT_CONFIDENCE = float(os.getenv("SOURCE_EXPERIMENT_CONFIDENCE", "0.90") or 0.90)
SOURCE_EXPERIMENT_ARTIFACT_DIR = os.getenv(
    "SOURCE_EXPERIMENT_ARTIFACT_DIR", str(Path(STATE_DIR) / "source_experiment"))

# --- Candidate title expansion (SHIPPED 2026-09-04; now carried by the catalog) --
# Adjacent IC titles that were absent from the 118-role catalog. They are now REAL
# role definitions in role_catalog.py, which is what generates the production
# title_advanced expression -- so the acquisition query and the downstream
# classifier move together and this dict is the declared set the alignment test
# checks, not a second source of truth. fantastic_jobs_adapter.
# title_expansion_alignment() DROPS any entry the catalog cannot resolve, because
# a query-only family buys postings the role gate can only mark UNVERIFIED.
#
# NOTE: adding a title here does NOT relax any seniority or people-manager rule.
# Staff/Principal/Lead variants stay rejected by job_quality.assess_restricted_work
# and Director/VP/Chief by role_gate.HARD_SENIORITY_PATTERN.
# DELIBERATELY EXCLUDED pending a role-policy ruling, because each is plausibly a
# people-manager or out-of-ICP title and G2 exists precisely to keep those out:
#   Controller, Product Manager, Technical Product Manager, Product Owner,
#   Territory Manager, Renewals Manager, Demand Generation Manager.
FANTASTIC_CANDIDATE_TITLES = _env_json("FANTASTIC_CANDIDATE_TITLES_JSON", {
    # engineering
    "Site Reliability Engineer": "engineering",
    "Platform Engineer": "engineering",
    "Security Engineer": "engineering",
    "Analytics Engineer": "engineering",
    "Data Platform Engineer": "engineering",
    "Software Development Engineer": "engineering",
    "Mobile Developer": "engineering",
    "iOS Developer": "engineering",
    "Android Developer": "engineering",
    # gtm / revenue (individual contributor forms only)
    "Sales Engineer": "gtm_revenue",
    "Solutions Consultant": "gtm_revenue",
    # customer success (IC forms only)
    "Customer Success Engineer": "customer_success",
    "Renewals Specialist": "customer_success",
})

# --- Provider-firmographic REJECT-only pre-gate (Apollo stays authoritative for PASS)
# Historical equivalence: reject-side 90/96 matched Apollo with 6 false-rejects (0.6%).
# ONLY the deterministic exact-label industry rules + an extreme headcount rule are
# implemented; default OFF until the label set is re-validated against a live cycle.
PROVIDER_FIRMOGRAPHIC_PRE_REJECT = _env_bool("PROVIDER_FIRMOGRAPHIC_PRE_REJECT", False)
PROVIDER_PRE_REJECT_INDUSTRIES = _env_json(
    "PROVIDER_PRE_REJECT_INDUSTRIES_JSON", ["Hospitals and Health Care"])
# (No headcount rule: provider `_org_headcount` is CAPPED (~999) and is not an
#  employee count, so a size pre-reject can never fire -- Gate C. Industry-only.)

# --- Apollo cross-run caches (Category 1 if tests prove correctness) ------------
APOLLO_CACHE_ENABLED = _env_bool("APOLLO_CACHE_ENABLED", False)
APOLLO_CACHE_PATH = os.getenv("APOLLO_CACHE_PATH", str(Path(STATE_DIR) / "apollo_cache.json"))
APOLLO_CACHE_ORG_TTL_DAYS = _env_int("APOLLO_CACHE_ORG_TTL_DAYS", 60)
APOLLO_CACHE_PEOPLE_POSITIVE_TTL_DAYS = _env_int("APOLLO_CACHE_PEOPLE_POSITIVE_TTL_DAYS", 45)
APOLLO_CACHE_ZERO_PEOPLE_TTL_DAYS = _env_int("APOLLO_CACHE_ZERO_PEOPLE_TTL_DAYS", 21)
APOLLO_CACHE_ZERO_TITLE_TTL_DAYS = _env_int("APOLLO_CACHE_ZERO_TITLE_TTL_DAYS", 14)
APOLLO_CACHE_PERSON_MATCH_TTL_DAYS = _env_int("APOLLO_CACHE_PERSON_MATCH_TTL_DAYS", 45)

# --- Hunter on the Fantastic/LinkedIn acquisition path ------------------------
# Historical: 0 incremental send-safe leads from Hunter on this path. Integration
# code is retained; this gate disables it ONLY for the Fantastic acquisition path.
HUNTER_ENABLED_FOR_FANTASTIC_PATH = _env_bool("HUNTER_ENABLED_FOR_FANTASTIC_PATH", True)

# --- Apollo zero-people org-id recovery (Category 1 once probed; DEFAULT OFF) ----
# ~61% of HM misses are zero_apollo_people, and 184/205 of those buckets held a
# TRUSTED Apollo organization (measured) whose organization_id we never tried --
# the people search selects the company only by q_organization_domains_list[].
# The fallback re-runs the SAME titles against organization_ids[] when the domain
# search confirms zero people. People Search is a 0-credit endpoint and the org id
# was already paid for by this company's enrich_organization call, so the fallback
# adds NO Apollo credit. Titles are never broadened; every downstream gate is
# unchanged; a domain-mismatched org is never used.
APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED = _env_bool(
    "APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED", False)

# --- Enrollment anti-spam identity (Category 1) --------------------------------
# normalized employer domain + normalized resolved email => one enrollment.
ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS = _env_bool("ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", False)

# --- North-star yield ledger (analytics only; never blocks the pipeline) --------
YIELD_LEDGER_ENABLED = _env_bool("YIELD_LEDGER_ENABLED", False)  # Gate-E D13: no new file on deploy
YIELD_LEDGER_PATH = os.getenv("YIELD_LEDGER_PATH", str(Path(STATE_DIR) / "yield_ledger.jsonl"))

# --- Function-aware UPSTREAM (pre-billing) company x function dedupe -----------
# PRE_APOLLO_EXISTING_DEDUPE saves Apollo but Fantastic has ALREADY billed the row.
# This partitions acquisition into role families and sends, per family, the LinkedIn
# slugs already actively covered FOR THAT FUNCTION as exclude_organization_slug
# (PROVEN honored: 500 slugs removed 950 rows at a 13.6KB URL). A company covered
# for GTM is excluded from the GTM query only -- its Engineering demand stays
# eligible. DEFAULT OFF: partitioning the single 118-term query into N paid family
# queries changes billing shape and must be proven by replay first.
FANTASTIC_FUNCTION_AWARE_UPSTREAM_DEDUPE_ENABLED = _env_bool(
    "FANTASTIC_FUNCTION_AWARE_UPSTREAM_DEDUPE_ENABLED", False)
FANTASTIC_SLUG_CROSSWALK_PATH = os.getenv(
    "FANTASTIC_SLUG_CROSSWALK_PATH", str(Path(STATE_DIR) / "fantastic_slug_crosswalk.json"))
FANTASTIC_SLUG_CROSSWALK_TTL_DAYS = _env_int("FANTASTIC_SLUG_CROSSWALK_TTL_DAYS", 120)
# One request per family, so the exclusion list is CAPPED rather than split across
# extra requests -- extra requests would mean extra billed rows, defeating the point.
# Truncation is safe: an un-excluded covered company is simply the status quo.
FANTASTIC_FUNCTION_DEDUPE_MAX_SLUGS_PER_FAMILY = _env_int(
    "FANTASTIC_FUNCTION_DEDUPE_MAX_SLUGS_PER_FAMILY", 250)
# Bounded exploration so suppression can never become permanent blindness: every Nth
# run acquires with NO exclusions and refreshes coverage. 0 disables exploration.
FANTASTIC_FUNCTION_DEDUPE_EXPLORATION_EVERY_N_RUNS = _env_int(
    "FANTASTIC_FUNCTION_DEDUPE_EXPLORATION_EVERY_N_RUNS", 7)
# 250 = safe operational chunk (a live probe honored 500 at 13.6KB, close to common
# ~16KB URL ceilings); never blindly emit larger URLs.
FANTASTIC_SLUG_EXCLUSION_CHUNK = _env_int("FANTASTIC_SLUG_EXCLUSION_CHUNK", 250)

# --- Functional / activity role expansion (Category 2; DEFAULT OFF) -----------
# Adjacent titles that perform the SAME work as proven high-yield activity clusters
# but are missing from the 118-term catalog. With the flag OFF the production
# expression stays byte-identical (118 terms / 2966 chars).
FANTASTIC_FUNCTIONAL_ROLE_EXPANSION_ENABLED = _env_bool(
    "FANTASTIC_FUNCTIONAL_ROLE_EXPANSION_ENABLED", False)

# --- Segment allocation foundations (Category 2; DEFAULT OFF) -----------------
SEGMENT_ALLOCATOR_ENABLED = _env_bool("SEGMENT_ALLOCATOR_ENABLED", False)
# Per-segment yields, produced offline from the yield ledger. Missing/corrupt => the
# allocator has no evidence and stays BROAD (current acquisition, unchanged).
SEGMENT_ALLOCATOR_YIELD_TABLE_PATH = os.getenv(
    "SEGMENT_ALLOCATOR_YIELD_TABLE_PATH", str(Path(STATE_DIR) / "segment_yields.json"))
# A segment needs this much billed evidence before it may be weighted at all, so a
# lucky 20-credit cell can never redirect the run.
SEGMENT_ALLOCATOR_MIN_EVIDENCE_CREDITS = _env_int(
    "SEGMENT_ALLOCATOR_MIN_EVIDENCE_CREDITS", 500)
# No single segment may take more than this share of the run -- prevents a
# whole-budget capture and keeps other families alive on a bad day.
SEGMENT_ALLOCATOR_MAX_SEGMENT_SHARE = _env_float("SEGMENT_ALLOCATOR_MAX_SEGMENT_SHARE", 0.40)
# Share always reserved for broad/unevidenced acquisition, so exploration never stops
# and a starved segment can earn evidence back.
SEGMENT_ALLOCATOR_EXPLORATION_FLOOR = _env_float("SEGMENT_ALLOCATOR_EXPLORATION_FLOOR", 0.20)
SEGMENT_YIELD_TABLE_PATH = os.getenv("SEGMENT_YIELD_TABLE_PATH", str(Path(STATE_DIR) / "segment_yield.json"))
SEGMENT_PRIORITY = _env_json("SEGMENT_PRIORITY_JSON", [])


def validate_fantastic_jobs_config() -> None:
    """Fail closed on misconfiguration BEFORE any network request.

    Never raises merely because the source is disabled; never includes the API
    key in any message.
    """
    if not FANTASTIC_JOBS_ENABLED:
        return  # disabled: any/missing key is valid, no request will be made
    if not FANTASTIC_JOBS_API_KEY:
        raise ValueError("FANTASTIC_JOBS_API_KEY is required when FANTASTIC_JOBS_ENABLED=1")
    if not str(FANTASTIC_JOBS_BASE_URL).lower().startswith("https://"):
        raise ValueError("FANTASTIC_JOBS_BASE_URL must be HTTPS")
    numerics = {
        "FANTASTIC_JOBS_REQUEST_TIMEOUT_SECONDS": FANTASTIC_JOBS_REQUEST_TIMEOUT_SECONDS,
        "FANTASTIC_JOBS_MAX_RETRIES": FANTASTIC_JOBS_MAX_RETRIES,
        "FANTASTIC_JOBS_MAX_JOBS_PER_RUN": FANTASTIC_JOBS_MAX_JOBS_PER_RUN,
        "FANTASTIC_JOBS_ATS_LIMIT": FANTASTIC_JOBS_ATS_LIMIT,
        "FANTASTIC_JOBS_WELLFOUND_LIMIT": FANTASTIC_JOBS_WELLFOUND_LIMIT,
        "FANTASTIC_JOBS_YCOMBINATOR_LIMIT": FANTASTIC_JOBS_YCOMBINATOR_LIMIT,
        "FANTASTIC_JOBS_LINKEDIN_LIMIT": FANTASTIC_JOBS_LINKEDIN_LIMIT,
        "FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING": FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING,
        "FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING": FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING,
    }
    for name, value in numerics.items():
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    segment_total = (
        FANTASTIC_JOBS_ATS_LIMIT
        + FANTASTIC_JOBS_WELLFOUND_LIMIT
        + FANTASTIC_JOBS_YCOMBINATOR_LIMIT
        + FANTASTIC_JOBS_LINKEDIN_LIMIT
    )
    if segment_total > FANTASTIC_JOBS_MAX_JOBS_PER_RUN:
        raise ValueError(
            "Fantastic.jobs segment limits "
            f"({segment_total}) exceed FANTASTIC_JOBS_MAX_JOBS_PER_RUN="
            f"{FANTASTIC_JOBS_MAX_JOBS_PER_RUN}"
        )
    if FANTASTIC_JOBS_DESCRIPTION_FORMAT not in {"text", "html"}:
        raise ValueError("FANTASTIC_JOBS_DESCRIPTION_FORMAT must be 'text' or 'html'")
    # Gate-E D5: the active-ats segment now needs BOTH the explicit source flag AND a
    # non-zero limit. Surface the contract change loudly instead of silently acquiring
    # nothing when an operator sets ATS_LIMIT>0 alone.
    if FANTASTIC_JOBS_ATS_LIMIT > 0 and not FANTASTIC_ATS_SOURCE_ENABLED:
        import warnings
        warnings.warn(
            "FANTASTIC_JOBS_ATS_LIMIT>0 but FANTASTIC_ATS_SOURCE_ENABLED=0: the active-ats "
            "segment is DISABLED (set FANTASTIC_ATS_SOURCE_ENABLED=1 to acquire from it).",
            RuntimeWarning, stacklevel=2)
    # ``time_frame`` is sent on EVERY request, watermark engine or not, so this is
    # checked unconditionally. The provider documents exactly four frames; an
    # unrecognised value reaches `_parse_time_frame_hours`, which falls back to 24
    # hours -- so a typo would silently tell the frame horizon the feed only reaches
    # back one day, clamping every window to the last 24 hours and abandoning
    # anything older as unreachable. Fail the deploy instead of shrinking the window.
    if (FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED
            and FANTASTIC_JOBS_FUNCTIONAL_LIMIT > 0):
        if not FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED.strip():
            raise ValueError(
                "FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED with a non-zero limit requires "
                "FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED; an empty expression would "
                "issue an unrestricted description search")
        if FANTASTIC_JOBS_TIME_FRAME == "6m":
            raise ValueError(
                "functional discovery cannot run on time_frame=6m: the provider "
                "rejects description search on that frame with HTTP 400")
    if FANTASTIC_JOBS_TIME_FRAME not in _SUPPORTED_TIME_FRAMES:
        raise ValueError(
            "FANTASTIC_JOBS_TIME_FRAME must be one of "
            + ", ".join(_SUPPORTED_TIME_FRAMES)
            + f" (got {FANTASTIC_JOBS_TIME_FRAME!r})")
    # `6m` IS a documented provider frame and is NOT a frame this engine can page.
    #
    # The provider assigns offset+limit to 1h/24h/7d and reserves id-cursor
    # pagination for 6m -- "pass the last id returned as the cursor", which also
    # switches result ordering from date_posted DESC to id ASC -- and warns
    # explicitly against resuming an offset run with a cursor or the reverse. This
    # adapter pages by offset and persists offsets; it implements no id cursor.
    #
    # Running 6m on offsets would therefore page a frame the provider does not
    # support paging that way, against a result set two orders of magnitude larger,
    # while our persisted offsets sit in the same state namespace as the 7d windows
    # they were measured in. Historical recovery needs a cursor mode, its own state
    # namespace and its own budget; none of those exist yet, and parsing the string
    # does not create them. Reject the combination rather than half-support it.
    if FANTASTIC_JOBS_TIME_FRAME == "6m":
        raise ValueError(
            "FANTASTIC_JOBS_TIME_FRAME=6m is not supported by this engine: the "
            "provider documents id-cursor pagination for that frame (ordering by id "
            "ascending) while this adapter pages and persists OFFSETS, and mixing the "
            "two is explicitly warned against. Historical recovery needs a cursor "
            "mode with its own state namespace and budget, which is not implemented.")
    # Watermark lag/overlap must be sane before the engine may ever run.
    if FANTASTIC_DATE_CREATED_WATERMARK_ENABLED:
        if FANTASTIC_DATE_CREATED_LAG_MINUTES < 60:
            raise ValueError("FANTASTIC_DATE_CREATED_LAG_MINUTES must be >= 60 (provider commit lag ~1h)")
        if FANTASTIC_DATE_CREATED_OVERLAP_MINUTES < 0:
            raise ValueError("FANTASTIC_DATE_CREATED_OVERLAP_MINUTES must be >= 0")
        if FANTASTIC_TIME_FRAME_MARGIN_MINUTES < 0:
            raise ValueError("FANTASTIC_TIME_FRAME_MARGIN_MINUTES must be >= 0")


# ---------- Railway startup guard (a deploy must not auto-run the pipeline) ----------
# A Railway deploy starts `python run_daily.py`. With autorun disabled the deploy
# builds and starts but the pipeline does not execute (no paid work). Default 0
# so a merge/deploy can never silently launch a run.
PIPELINE_AUTORUN_ENABLED = _env_bool("PIPELINE_AUTORUN_ENABLED", False)

# Enabled/disabled token sets for the autorun guard (strict, no string-truthiness).
_AUTORUN_ENABLED_TOKENS = frozenset({"1", "true", "yes", "on"})
_AUTORUN_DISABLED_TOKENS = frozenset({"0", "false", "no", "off"})


def autorun_is_enabled() -> bool:
    """Single source of truth for whether the pipeline may execute.

    Read at RUNTIME (never an import-time snapshot) so the current Railway
    environment value governs immediately before any side effect. Fail-safe: only
    an explicit enabled token permits execution; missing, empty, whitespace,
    disabled, or invalid values all stop safely. A non-empty string such as "0" is
    never treated as truthy. Never emits secrets or the environment.
    """
    raw = os.getenv("PIPELINE_AUTORUN_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in _AUTORUN_ENABLED_TOKENS

# ---------- Health gates ----------
MIN_JOBS_PER_RUN = _env_int("MIN_JOBS_PER_RUN", 10)
MIN_ROLES_WITH_RESULTS = _env_int("MIN_ROLES_WITH_RESULTS", 4)
MAX_ROLE_FAILURES = _env_int("MAX_ROLE_FAILURES", 3)
# The absolute threshold protects small role sets; the rate prevents the full
# 100+ role catalog from failing because of a handful of isolated query errors.
MAX_ROLE_FAILURE_RATE = _env_float("MAX_ROLE_FAILURE_RATE", 0.10)
MIN_HIRING_MANAGER_MATCH_RATE = _env_float("MIN_HIRING_MANAGER_MATCH_RATE", 0.70)
ENFORCE_HM_MATCH_RATE = _env_bool("ENFORCE_HM_MATCH_RATE", False)

# Daily production throughput controls. Strict mode stops only after the
# FINAL_PASS target, with an eligible-company safety cap to bound Apollo/Hunter
# usage on low-yield days.
# Legacy name remains readable during migration.  The dynamic helper below
# allows existing environments/tests that still set TARGET_REVIEWABLE... to
# control the new target until TARGET_FINAL_PASS... is explicitly configured.
TARGET_REVIEWABLE_LEADS_PER_RUN = _env_int("TARGET_REVIEWABLE_LEADS_PER_RUN", 30)
_TARGET_FINAL_PASS_EXPLICIT = os.getenv("TARGET_FINAL_PASS_LEADS_PER_RUN")
TARGET_FINAL_PASS_LEADS_PER_RUN = (
    int(_TARGET_FINAL_PASS_EXPLICIT)
    if _TARGET_FINAL_PASS_EXPLICIT not in (None, "")
    else TARGET_REVIEWABLE_LEADS_PER_RUN
)

def get_final_pass_target() -> int:
    if _TARGET_FINAL_PASS_EXPLICIT not in (None, ""):
        return TARGET_FINAL_PASS_LEADS_PER_RUN
    return TARGET_REVIEWABLE_LEADS_PER_RUN
# No arbitrary account cap: acquisition/query budgets and downstream gates are
# the bounded controls. Set a positive value only for a deliberate cost-limited run.
MAX_ELIGIBLE_COMPANIES_PER_RUN = _env_int("MAX_ELIGIBLE_COMPANIES_PER_RUN", 0)
SEEN_JOBS_RETENTION_DAYS = _env_int("SEEN_JOBS_RETENTION_DAYS", 30)

# -- custody of acquired-but-unfinished postings ---------------------------------
# A run that buys postings and then stops before finishing them (an Apollo billing
# rejection, a crash) used to lose that work: nothing was suppressed, but nothing
# handed it back either, and the window offsets had already advanced past it. These
# bound the store that now keeps it. ON by default -- a correctness fix that is off
# fixes nothing -- but the RESUME is bounded, because a long outage must not hand a
# single run more enrichment than its budget can serve.
# MAINTENANCE-ONLY MODE. When true, `run_orchestrator` delegates to
# `run_maintenance` and NEVER constructs a lane, an enrichment engine or a delivery
# manager -- so the scheduled container does the recovery/reporting pass instead of
# a pipeline run, on the existing schedule and with no start-command change. It is
# refused unless acquisition is already paused, so it can never mask a live run.
# ACQUISITION CURSOR SHAPE. The provider documents offset paging only for draining
# a result set in ONE pass ("keep making requests until the API returns less jobs
# than the limit"); nothing documents that an index still addresses the same row on
# a later day, and the rising 7-day frame floor guarantees it does not. Slicing the
# window by `date_created` replaces the index with a boundary that cannot move: a
# row's date_created never changes, so a drained slice stays drained and no budget
# is ever spent re-walking one. Set FANTASTIC_WINDOW_SLICING_ENABLED=0 to fall back
# to the cross-run offset cursor.
FANTASTIC_WINDOW_SLICING_ENABLED = _env_bool("FANTASTIC_WINDOW_SLICING_ENABLED", True)
FANTASTIC_WINDOW_SLICE_HOURS = _env_int("FANTASTIC_WINDOW_SLICE_HOURS", 6)
# LANE SELECTION WITHOUT A START-COMMAND CHANGE. `--lanes` is baked into the
# deployed start command; this is a comma list ADDED to it. It can only widen the
# set -- a lane the start command asked for cannot be switched off here -- so the
# deployed command stays the floor of what runs. Empty means no change at all.
# Intended use: "ats", to activate the 145 registered direct boards, which cost no
# provider credits. Resolved before the strict preflight so an added lane is held
# to the same dependency checks as a requested one.
ACQUISITION_EXTRA_LANES = os.getenv("ACQUISITION_EXTRA_LANES", "")
MAINTENANCE_ONLY = _env_bool("MAINTENANCE_ONLY", False)
# Optional steps for a maintenance pass, settable without a code change so a single
# scheduled container can be pointed at a specific job.
MAINTENANCE_CAPACITY_RUNS = os.getenv("MAINTENANCE_CAPACITY_RUNS", "")
MAINTENANCE_DROP_EMPTY_RUN = os.getenv("MAINTENANCE_DROP_EMPTY_RUN", "")
MAINTENANCE_REIMPORT_RUN = os.getenv("MAINTENANCE_REIMPORT_RUN", "")
MAINTENANCE_WINDOW_START = os.getenv("MAINTENANCE_WINDOW_START", "2026-09-04T07:00:00Z")
# Board count for the direct-ATS yield measurement, or "" to skip it. Costs no
# provider credits (each employer's own public board) and persists nothing, but it
# is opt-in because it makes ~145 outbound requests.
MAINTENANCE_ATS_BOARD_YIELD = os.getenv("MAINTENANCE_ATS_BOARD_YIELD", "")
MAINTENANCE_ATS_BOARD_SECONDS = _env_int("MAINTENANCE_ATS_BOARD_SECONDS", 1500)
PENDING_WORK_ENABLED = _env_bool("PENDING_WORK_ENABLED", True)
PENDING_WORK_RESUME_MAX_PER_RUN = _env_int("PENDING_WORK_RESUME_MAX_PER_RUN", 2000)
PENDING_WORK_MAX_AGE_DAYS = _env_int("PENDING_WORK_MAX_AGE_DAYS", 14)
# Adoption is a RECOVERY sweep, not a per-run workload, so it is bounded
# separately and far more generously than the resume limit.
PENDING_WORK_ADOPT_MAX_PER_PASS = _env_int("PENDING_WORK_ADOPT_MAX_PER_PASS", 10000)
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
    "online media",
    "internet news",
    "news media",
    "media production",
    "digital news",
    "financial news",
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
# --- Automatic alternate-contact cascade -------------------------------------
# MEASURED: re-enriching the SAME person recovers nothing (0/10). A DIFFERENT
# hiring manager at the same company recovers a verified email at rank 1 33.3%
# (14/42), rank 2 26.1% (6/23), rank 3 16.7% (1/6) -- monotonic decay, so rank 3
# is terminal. Historically the contact loop STOPPED on the first candidate that
# merely had an e-mail, even an Apollo-"extrapolated" one that can never pass
# send_safe_facts; that is how a 68-row undeliverable backlog accumulated while
# APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET=3 already allowed two more tries.
# Advancing is ONLY permitted for a PERSON-level failure (no verified e-mail).
# Employer-level failures (domain mismatch, identity mismatch, company gate) never
# advance -- a colleague reproduces them (measured: 7/23 at rank 2).
ALTERNATE_CONTACT_CASCADE_ENABLED = _env_bool("ALTERNATE_CONTACT_CASCADE_ENABLED", False)
# Run-level ceiling on EXTRA person enrichments so a pathological batch cannot
# explode Apollo usage. <= 0 disables advancing entirely.
ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN = _env_int(
    "ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN", 100)

APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET = _env_int(
    "APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET", 3
)

# --- RUN-LEVEL paid-enrichment budget -----------------------------------------
# Three attempts per BUCKET is not a run-level budget. Nothing bounded the total
# number of paid `people/match` calls a run could make, which was tolerable only
# while every paid attempt belonged to a bucket that had already found people.
#
# The org-id zero-people fallback breaks that assumption by design: it fires ONLY on
# buckets that found zero people, so every paid match it enables is ADDITIONAL. On a
# run with ~150 such buckets that is up to ~450 paid calls that no prior authorization
# covered. The 0-credit People Search is genuinely free; what it feeds is not.
#
# So the fallback gets its OWN budget, and it defaults to ZERO: the recovery runs,
# the people are found, and the paid enrichment of buckets it recovered does not
# happen until someone grants a number. Existing spend is unchanged by enabling the
# fallback, which is the property that had to hold.
#
# Buckets past the budget are DEFERRED, never discarded: they are left unprocessed
# and counted, so a later run with budget picks them up. Marking them processed would
# turn a budget stop into permanent data loss.
# --- bounded historical recovery (cursor mode, 6m) ----------------------------
# The steady-state engine pages a `date_created` window by OFFSET, which is the
# method the provider documents for 1h/24h/7d. It cannot cover an interruption
# longer than the frame: once the floor rises past a gap, those postings are
# unreachable by any windowed request.
#
# The documented way back is the other pagination mode: `time_frame=6m` with
# `cursor` set to the last `id` returned, which orders by `id` ASCENDING instead of
# `date_posted` descending. The provider warns explicitly against resuming an offset
# run with a cursor or the reverse, so this is a SEPARATE mechanism with a SEPARATE
# state file -- it never reads or writes the windowed engine's offsets, and the two
# can never be mistaken for each other.
#
# Bounded by construction: it is off by default, needs an explicit row budget, and
# persists its cursor after every page so an interrupted recovery resumes where it
# stopped instead of restarting. `description_advanced` is rejected on this frame
# (HTTP 400), so functional discovery cannot ride along.
FANTASTIC_HISTORICAL_RECOVERY_ENABLED = _env_bool(
    "FANTASTIC_HISTORICAL_RECOVERY_ENABLED", False)
#: Hard ceiling on rows this recovery may bill per run. 0 disables it outright --
#: there is no implicit budget, and an unbounded historical backfill is exactly what
#: must not be launchable by flipping one flag.
FANTASTIC_HISTORICAL_RECOVERY_MAX_ROWS_PER_RUN = _env_int(
    "FANTASTIC_HISTORICAL_RECOVERY_MAX_ROWS_PER_RUN", 0)
#: Its OWN namespace. Never the windowed engine's state.
FANTASTIC_HISTORICAL_RECOVERY_STATE_PATH = os.getenv(
    "FANTASTIC_HISTORICAL_RECOVERY_STATE_PATH",
    str(Path(STATE_DIR) / "fantastic_historical_recovery.json"))

APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN = _env_int(
    "APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN", 0)

# Overall ceiling on paid `people/match` calls per run, across every path (primary,
# alternate cascade, org-id fallback). 0 = no ceiling, which is the behaviour that
# existed before this setting and is therefore the default: this is a guard to be
# switched on deliberately, not a silent new limit on work already authorized.
APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN = _env_int(
    "APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN", 0)

# Company-level opportunity collapse (company_opportunity_collapse.py): elect ONE
# posting per trusted employer identity BEFORE paid person enrichment, so an
# employer advertising eight roles cannot become eight Apollo credits and eight
# outbound leads. Applied AFTER the JobGate/RoleGate so only qualified postings
# compete for the slot. Default OFF: the live lanes already bound spend upstream
# via the function-aware provider dedupe, and turning this on changes which
# opening represents an employer.
COMPANY_OPPORTUNITY_COLLAPSE_ENABLED = _env_bool(
    "COMPANY_OPPORTUNITY_COLLAPSE_ENABLED", False)
# People search itself is free (only match_person/enrichment consumes
# credits) -- apollo_client.search_people_at_company previously fetched only
# page 1 (25 results) with no pagination at all, silently missing any
# candidate beyond the first 25 regardless of title relevance. Paginating
# further costs nothing extra and can only improve which candidates are
# available for ranking before the (separately budgeted, credit-consuming)
# match-attempt step. FINAL_30_PLUS_SYSTEM_SPEC.md section 16.
APOLLO_PEOPLE_SEARCH_MAX_PAGES = _env_int("APOLLO_PEOPLE_SEARCH_MAX_PAGES", 4)
HUNTER_MAX_FALLBACK_ATTEMPTS_PER_BUCKET = _env_int(
    "HUNTER_MAX_FALLBACK_ATTEMPTS_PER_BUCKET", 2
)
CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET = _env_int(
    "CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET", 8
)
REROUTE_STATE_TTL_DAYS = _env_int("REROUTE_STATE_TTL_DAYS", 7)
REROUTE_TEMPORARY_TTL_HOURS = _env_int("REROUTE_TEMPORARY_TTL_HOURS", 12)
REROUTE_PERMANENT_TTL_DAYS = _env_int("REROUTE_PERMANENT_TTL_DAYS", 30)
REQUIRE_CURRENT_EMPLOYMENT_EVIDENCE = _env_bool("REQUIRE_CURRENT_EMPLOYMENT_EVIDENCE", True)
REQUIRE_CONTACT_LINKEDIN = _env_bool("REQUIRE_CONTACT_LINKEDIN", True)
REQUIRE_US_CONTACT_TERRITORY = _env_bool("REQUIRE_US_CONTACT_TERRITORY", False)
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
# Airtable review/delivery dedup is now FUNCTION-aware (company + role_bucket).
# Two independent, explicit tiers replace the old bucket-blind company suppressor:
#
#  * AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION (default True) -- the review/
#    delivery dedup. Suppresses only the SAME company+function; a DIFFERENT function
#    at a company that already has a lead (Acme+Sales when Acme+Marketing exists) is
#    PRESERVED as its own opportunity. This is the intended production behaviour.
#
#  * AIRTABLE_SUPPRESS_ACCOUNT_LEVEL (default False) -- an OPTIONAL company-wide
#    (bucket-blind) CRM/active-pipeline exclusion: when enabled, any active row for a
#    company hard-suppresses ALL functions of that company. Enable ONLY when an
#    account-level "one opportunity per company at a time" policy is intended; it is
#    checked before, and is strictly stronger than, the function-level dedup.
#
# The legacy AIRTABLE_SUPPRESS_EXISTING_COMPANY flag is DEPRECATED. It no longer
# drives push_leads (that would silently collapse distinct functions); if it is
# explicitly set it is honoured ONLY as a back-compat alias that turns the
# account-level tier on/off, and validate_setup emits a deprecation notice.
# Legacy flag: retained (default True) ONLY for the retired run_daily pre-Apollo
# exclusion path. It NO LONGER drives the production orchestrator's Airtable
# suppression -- that path uses the two explicit flags below. Deprecated.
AIRTABLE_SUPPRESS_EXISTING_COMPANY = _env_bool(
    "AIRTABLE_SUPPRESS_EXISTING_COMPANY", True
)
# Production orchestrator review/delivery dedup: function-aware by default.
AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION = _env_bool(
    "AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION", True
)
# Optional company-wide (bucket-blind) account exclusion. Decoupled from the legacy
# flag and OFF by default so distinct functions are preserved unless an operator
# deliberately opts into a one-account-at-a-time policy.
AIRTABLE_SUPPRESS_ACCOUNT_LEVEL = _env_bool("AIRTABLE_SUPPRESS_ACCOUNT_LEVEL", False)
# Pre-Apollo existing-lead dedupe. When ON, the orchestrator snapshots Airtable
# existing-lead identity ONCE at run start and suppresses candidates whose
# canonical company x role-bucket (function) already has an active lead BEFORE
# any Apollo org-enrich / people-search spend -- reusing the exact identity
# semantics and the same snapshot that delivery uses (no second Airtable read).
# Delivery-side idempotency/suppression remains the authoritative final backstop.
# Governed alongside the two AIRTABLE_SUPPRESS_* flags above: function-level
# pre-suppression follows AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION, account-
# level follows AIRTABLE_SUPPRESS_ACCOUNT_LEVEL. Default OFF -- ON for the
# Fantastic production lane via env.
PRE_APOLLO_EXISTING_DEDUPE = _env_bool("PRE_APOLLO_EXISTING_DEDUPE", False)
# Send-safe-only Airtable write policy. When ON, push_leads creates a lead row
# ONLY when send_safe_facts() passes (disposition-label-INDEPENDENT; never Final
# Decision alone) -- so Airtable holds usable, outbound-eligible leads rather than
# a review dump. Non-send-safe candidates (held / no-contact / unusable email /
# unsafe identity / unresolved role / otherwise non-actionable) are withheld from
# Airtable but preserved in run artifacts and surfaced as metrics. Approved Sync is
# untouched: it independently re-runs send_safe_facts before Instantly enrollment.
# Default OFF globally -- intended ON for the Fantastic production lane via env.
AIRTABLE_WRITE_SEND_SAFE_ONLY = _env_bool("AIRTABLE_WRITE_SEND_SAFE_ONLY", False)
# Quality-preserving hiring-manager recovery: when a company x role-bucket's normal
# people search returns no acceptable HM, perform at most ONE bounded second-pass
# search broadened WITHIN THE SAME function to adjacent legitimate decision-maker
# seniorities (Director / Senior Director level -- which _selection_tier already
# treats as a valid "direct_functional_leader"). It never crosses functions, never
# accepts junior/IC titles, and never weakens the HM-title matcher or the Apollo-
# verified-email requirement -- the recovered person still passes every downstream
# gate. Default OFF.
HM_SECOND_PASS_TITLE_BROADENING = _env_bool("HM_SECOND_PASS_TITLE_BROADENING", False)
# Employer-domain corroboration/recovery: when the normal search domain yields zero
# Apollo people (or Apollo resolved a DIFFERENT organization domain), allow ONE
# recovery HM search on that alternate domain -- but ONLY when strong structured
# evidence (matching LinkedIn org identity, or exact company-name match with a
# name-consistent first-party domain) proves it is the SAME employer / legitimate
# rebrand / first-party identity. Fail-closed on any name conflict, staffing/client
# ambiguity, or merely-speculative relationship. Canonical/source identity is
# preserved separately; recovered contacts still pass every downstream gate
# (bucket/title, Apollo-verified email, displays, holds, fingerprint, send_safe_facts,
# Airtable idempotency, Approved-Sync fail-close). Default OFF.
HM_DOMAIN_CORROBORATION_RECOVERY = _env_bool("HM_DOMAIN_CORROBORATION_RECOVERY", False)
AIRTABLE_STATUS_PENDING = os.getenv("AIRTABLE_STATUS_PENDING", "Pending")
AIRTABLE_STATUS_APPROVED = os.getenv("AIRTABLE_STATUS_APPROVED", "Approved")
AIRTABLE_STATUS_REJECTED = os.getenv("AIRTABLE_STATUS_REJECTED", "Rejected")
AIRTABLE_STATUS_ENROLLED = os.getenv("AIRTABLE_STATUS_ENROLLED", "Enrolled")
AIRTABLE_STATUS_ERROR = os.getenv("AIRTABLE_STATUS_ERROR", "Error")
# Auto-approve genuine Fantastic Direct API leads that are SEND-SAFE by FACT
# (airtable_client.send_safe_facts) straight to Status=Approved at creation, so a
# normal send-safe Fantastic lead needs zero manual approval. The disposition
# label (FINAL_PASS/NEEDS_CHECK/UNVERIFIED) is NOT the criterion; the underlying
# send-safe facts are. Unsafe/ambiguous/held leads still land Pending. Approved
# Sync independently re-checks send_safe_facts, so this never bypasses safety.
FANTASTIC_AUTO_APPROVE_SEND_SAFE = _env_bool("FANTASTIC_AUTO_APPROVE_SEND_SAFE", True)
APPROVED_REVALIDATION_MAX_AGE_HOURS = _env_int("APPROVED_REVALIDATION_MAX_AGE_HOURS", 24)
APPROVED_REVALIDATE_JOB_SOURCE = _env_bool("APPROVED_REVALIDATE_JOB_SOURCE", True)
# DEPRECATED AND NOT READ AT RUNTIME. Approved Sync is delivery-only: it makes no
# Apollo, Hunter, Fantastic or JobSourceResolver call, and `run_approved.run()`
# explicitly ignores the equivalent argument (logging a warning if it is passed).
#
# It is kept as a defined name only so an environment that still sets it -- GTM
# Approved Sync has `APPROVED_SYNC_REVALIDATE_PROVIDERS=true` today -- does not
# read as "provider revalidation is on". It is not.
#
# The behaviour it used to select caused the 2026-08-12 incident: 627 Approved
# rows were marked Error by a 24h validation-age gate that ran BEFORE any provider
# call, so nothing enrolled and nothing reached Instantly. Approved is now the
# authorization boundary -- the worker delivers, it does not re-qualify. Setting
# this true does not and must not bring that path back.
APPROVED_SYNC_REVALIDATE_PROVIDERS = _env_bool("APPROVED_SYNC_REVALIDATE_PROVIDERS", True)
SLA_REQUIRE_NET_NEW_AIRTABLE = _env_bool("SLA_REQUIRE_NET_NEW_AIRTABLE", True)
# A commercial volume miss is reported in the run summary but is not a process
# failure. Keep this disabled for Railway services to avoid restart loops.
PIPELINE_FAIL_PROCESS_ON_SLA_MISS = _env_bool(
    "PIPELINE_FAIL_PROCESS_ON_SLA_MISS", False
)
RECOVERABLE_JOB_TTL_DAYS = _env_int("RECOVERABLE_JOB_TTL_DAYS", 7)
RECOVERABLE_JOB_MAX_ATTEMPTS = _env_int("RECOVERABLE_JOB_MAX_ATTEMPTS", 5)
FINAL_PASS_INVENTORY_TTL_DAYS = _env_int("FINAL_PASS_INVENTORY_TTL_DAYS", 7)

# ---------- Instantly ----------
INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY", "")
INSTANTLY_BASE_URL = os.getenv("INSTANTLY_BASE_URL", "https://api.instantly.ai/api/v2")
INSTANTLY_CAMPAIGN_ID = os.getenv("INSTANTLY_CAMPAIGN_ID", "")
INSTANTLY_RATE_LIMIT_DELAY = _env_float("INSTANTLY_RATE_LIMIT_DELAY", 0.35)
INSTANTLY_VERIFY_ON_IMPORT = _env_bool("INSTANTLY_VERIFY_ON_IMPORT", False)

# ---------- Outbound Wave 1 A/B experiment ----------
# Control A is the live campaign copy and is never touched by this code path.
# Challenger B is resolved locally (see the ``outbound_wave1`` package) and
# delivered as Instantly custom variables.
#
# DEFAULT OFF. With OUTBOUND_WAVE1_ENABLED false the enrollment payload is
# byte-identical to today's, so deploying this code changes nothing until the
# flag is set. OUTBOUND_WAVE1_B_SPLIT_PCT is the share of ACCOUNTS (never
# contacts, never campaigns) routed to the challenger; 0 keeps everyone on
# Control A even when the feature is enabled.
OUTBOUND_WAVE1_ENABLED = _env_bool("OUTBOUND_WAVE1_ENABLED", False)
OUTBOUND_WAVE1_EXPERIMENT_ID = os.getenv(
    "OUTBOUND_WAVE1_EXPERIMENT_ID", "outbound_wave1_challenger_v1"
).strip()
OUTBOUND_WAVE1_B_SPLIT_PCT = _env_int("OUTBOUND_WAVE1_B_SPLIT_PCT", 0)
#: Optional salt for a re-randomisation. Changing it reshuffles every account,
#: so it must stay fixed for the life of an experiment.
OUTBOUND_WAVE1_ASSIGNMENT_SALT = os.getenv("OUTBOUND_WAVE1_ASSIGNMENT_SALT", "").strip()
#: Static claim / role-page registry. No runtime web lookup is ever performed.
OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH = os.getenv(
    "OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH", str(BASE_DIR / "data" / "wave1_claims.json")
)
#: Instantly campaign ids for the Challenger arm, keyed by role bucket, e.g.
#: {"finance": "<campaign-uuid>"}. A bucket with no challenger campaign stays on
#: Control A no matter what the account assignment says, so B can never be sent
#: into a control campaign.
OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS = _env_json(
    "OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON", {}
)


#: Experiment start watermark. ONLY Airtable rows created at or after this
#: instant may enter Wave 1, which is what keeps the pilot to genuinely new
#: leads: an older Approved row can belong to a person the live Control
#: campaigns already emailed, and delivering that person into a Challenger
#: campaign would both double-touch them and contaminate the comparison.
#:
#: Required when Wave 1 is enabled. Leaving it blank does not silently widen the
#: population -- ``instantly_client.wave1_enrollment_overlay`` fails closed and
#: every record stays on Control A.
OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT = os.getenv(
    "OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT", ""
).strip()


def resolve_wave1_challenger_campaign_id(role_bucket: str) -> str:
    """Challenger campaign id for a bucket, or "" when none is configured."""
    mapping = OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS
    if not isinstance(mapping, dict):
        return ""
    return str(mapping.get(str(role_bucket or "").strip().lower()) or "").strip()


def wave1_configured_challenger_buckets() -> frozenset:
    """Role buckets that actually have a Challenger campaign id configured.

    A small rollout maps only some buckets. A record in an unmapped bucket is
    delivered on Control A regardless of its hash, so the resolver suppresses it
    rather than labelling it ``B`` -- otherwise a control-delivered row would sit
    in the treatment arm of the analysis.
    """
    mapping = OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS
    if not isinstance(mapping, dict):
        return frozenset()
    return frozenset(
        str(bucket or "").strip().lower()
        for bucket, campaign in mapping.items()
        if str(campaign or "").strip() and str(bucket or "").strip()
    )

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
    r"\bin[- ]office requirement\b",
    r"\b(?:monday|tuesday|wednesday|thursday|friday)(?:\s*,\s*(?:monday|tuesday|wednesday|thursday|friday))+(?:\s+and\s+(?:monday|tuesday|wednesday|thursday|friday))?[^.\n]{0,100}\b(?:office|on[- ]site|onsite)\b",
    r"\btravel (?:approximately |up to |minimum |at least )?(?:2[5-9]|[3-9]\d|100)%\b",
    r"\bfrequent travel\b",
    r"\btravel regularly to (?:client|customer) sites\b",
    r"\bestimated 1\+ day/?week\b",
]

# Onsite/hybrid language is a commercial signal, not a rejection. These narrow
# patterns identify roles whose core duties cannot be delivered by remote talent.
INHERENT_PHYSICAL_TITLE_PATTERNS = [
    r"\bwarehouse (?:associate|worker|operator|manager|operations? analyst)\b",
    r"\b(?:delivery|truck|bus) driver\b",
    r"\bfield service (?:technician|engineer)\b",
    r"\b(?:maintenance|installation|repair) technician\b",
    r"\b(?:laboratory|lab) technician\b",
    r"\b(?:machine|forklift|equipment) operator\b",
    r"\bconstruction (?:worker|superintendent|foreman)\b",
    r"\b(?:registered nurse|licensed practical nurse|medical assistant)\b",
    r"\bretail (?:associate|store manager)\b",
    r"\bfront desk|receptionist\b",
]
INHERENT_PHYSICAL_DESCRIPTION_PATTERNS = [
    r"\b(?:operate|maintain|repair|install) (?:physical )?(?:machinery|equipment|hardware) (?:on[- ]site|at customer sites?)\b",
    r"\b(?:pick|pack|load|unload) (?:inventory|orders|shipments)\b",
    r"\bmust (?:regularly )?lift (?:at least |up to )?\d{2,3} (?:lb|lbs|pounds)\b",
    r"\bprovide (?:direct )?(?:patient|clinical) care\b",
    r"\bperform (?:laboratory|lab) (?:testing|experiments|procedures)\b",
    r"\bdrive (?:a |the )?(?:company )?(?:vehicle|truck|van)\b",
    r"\bwork (?:on|at) (?:construction|customer|client) sites?\b",
    r"\bfield[- ]based (?:position|role|job)\b",
    r"\btravel (?:approximately |about |up to )?(?:[2-9]\d|100)%? to (?:customer|client) sites?\b",
    r"\b(?:requires? )?all work (?:must be|to be|is to be) performed in (?:a |an )?SCIF\b",
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
    r"\bpart[-–—‑ ]time\b",
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
    r"\bthis is (?:a )?part[-–—‑ ]time (?:role|position|job)\b",
    r"\b(?:on|as) (?:a )?part[-–—‑ ]time basis\b",
    r"\bpart[-–—‑ ]time\s+(?:or|/)\s+full[-–—‑ ]time\b",
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
FULL_TIME_EMPLOYMENT_TYPES = {
    "full time", "fulltime", "permanent", "permanent full time",
    "regular full time", "salaried full time",
}
AMBIGUOUS_EMPLOYEE_TYPES = {"regular", "employee", "salaried"}
NON_ACTIVE_HIRING_SIGNAL_PATTERNS = [
    r"(?m)^\s*(?:future openings?|future opportunities|talent pool|talent pipeline|expression of interest|general application)\s*$",
    r"\b(?:this|the) (?:posting|role|position|application) (?:is|exists|serves)\b[^.\n]{0,120}\b(?:future openings?|future opportunities|talent pool|talent pipeline|expression of interest|general application)\b",
    r"\bwe (?:are )?(?:accepting|collecting|inviting) (?:applications|interest)\b[^.\n]{0,120}\b(?:future openings?|future opportunities|talent pool|talent pipeline)\b",
    r"\bnot (?:an|a) active (?:opening|role|position)\b",
    r"\bevergreen (?:role|position|opening)\b",
    r"\bregister your interest for future\b",
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
    "gradebuzz",
    "cosmoquick",
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
    "himalayas",
    "jobicy",
    "we work remotely",
    "remotive",
    "remote ok",
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
    "jobicy.com",
    "personio.de",
    "applytojob.com",
    "adp.com",
    "oraclecloud.com",
    "successfactors.com",
    "bamboohr.com",
    "personio.com",
    # Cornerstone OnDemand's shared domain -- the tenant subdomain (e.g.
    # worldbankgroup.csod.com) is the employer's own identity, but
    # normalize_company_domain() alone would collapse it down to just
    # csod.com (same failure mode INTERMEDIARY_JOB_DOMAINS already guards
    # against for myworkdayjobs.com); see ats_board_registry.py's dedicated
    # tenant-domain-candidate extraction for the correct recovery path.
    "csod.com",
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
    "builtinsf.com",
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
    "remoterocketship.com",
    "gradebuzz.com",
    "cosmoquick.com",
    # Provider/publisher domains observed in the July 23 production corpus.
    # These can host useful discovery records but are never employer domains.
    "simplify.jobs",
    "jobtrees.com",
    "jobmesh.io",
    "remoteok.com",
    "weworkremotely.com",
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

# Company-profile evidence is fetched only from a verified first-party profile
# page associated with the provider's company slug. Keep these patterns narrow:
# they describe the employer itself, not a software vendor merely serving the
# healthcare market.
PROVIDER_PROFILE_EXCLUDED_INDUSTRY_PATTERNS = [
    r"\b(?:digital )?health(?:care)? (?:company|platform|provider|organization)\b",
    r"\bhealth and wellness engagement platform\b",
    r"\b(?:patient|member) (?:care|health|healthcare) (?:company|platform|provider)\b",
    r"\bhelps? (?:people|patients|members) [^.\n]{0,120}\bmanage (?:their )?healthcare\b",
    r"\bcare management\b[^.\n]{0,120}\bhealth information management\b",
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
GLOBAL_REMOTE_LOCATION_MARKERS = {
    "anywhere in the world",
    "worldwide",
    "remote worldwide",
    "global",
    "global remote",
}
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
    "builtinaustin.com", "builtincolorado.com", "builtinseattle.com", "builtinsf.com", "builtin.com",
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
