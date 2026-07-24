"""Validate local configuration and optionally perform read-only live checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List

import requests

import airtable_client
import config
from http_utils import request_with_retry, safe_json
from role_catalog import canonical_role_for_search, get_function_bucket


def _configured_campaign_ids() -> List[str]:
    values = {config.INSTANTLY_CAMPAIGN_ID}
    for base_env in config.CAMPAIGN_ENV_BY_BUCKET.values():
        values.add(os.getenv(base_env, ""))
        for band in ("SMALL", "MID", "LARGE", "UNKNOWN"):
            values.add(os.getenv(f"{base_env}_{band}", ""))
    return sorted(value for value in values if value)



def _active_function_buckets() -> List[str]:
    return sorted({
        get_function_bucket(canonical_role_for_search(role)) for role in config.ROLES
    })


def _bucket_has_campaign(bucket: str) -> bool:
    if config.INSTANTLY_CAMPAIGN_ID:
        return True
    base_env = config.CAMPAIGN_ENV_BY_BUCKET.get(bucket)
    if not base_env:
        return False
    if os.getenv(base_env, ""):
        return True
    return any(os.getenv(f"{base_env}_{band}", "") for band in ("SMALL", "MID", "LARGE", "UNKNOWN"))

def static_checks() -> Dict:
    errors: List[str] = []
    warnings: List[str] = []
    acquisition_mode = str(config.ACQUISITION_MODE or "").lower()
    jsearch_mode = acquisition_mode == "jsearch"

    required = {
        "APOLLO_API_KEY": config.APOLLO_API_KEY,
        "AIRTABLE_TOKEN": config.AIRTABLE_TOKEN,
        "AIRTABLE_BASE_ID": config.AIRTABLE_BASE_ID,
        "INSTANTLY_API_KEY": config.INSTANTLY_API_KEY,
    }
    for name, value in required.items():
        if not value:
            errors.append(f"Missing {name}")
    if jsearch_mode and not config.RAPIDAPI_KEY:
        errors.append("Missing RAPIDAPI_KEY for ACQUISITION_MODE=jsearch")
    if (
        acquisition_mode == "multi_source"
        and config.MULTI_SOURCE_JSEARCH_ENABLED
        and not config.RAPIDAPI_KEY
    ):
        warnings.append("RAPIDAPI_KEY is missing; multi_source will continue without JSearch")

    allowed_modes = {"multi_source", "free_multi_source", "jsearch"}
    if acquisition_mode not in allowed_modes:
        errors.append(
            f"ACQUISITION_MODE must be one of {sorted(allowed_modes)}, got {config.ACQUISITION_MODE!r}"
        )
    if acquisition_mode in {"multi_source", "free_multi_source"}:
        allowed_sources = {"himalayas", "jobicy", "weworkremotely", "remotive", "remoteok"}
        configured_sources = [str(value).lower() for value in config.FREE_JOB_SOURCES]
        unknown_sources = sorted(set(configured_sources) - allowed_sources)
        if unknown_sources:
            errors.append(f"Unsupported FREE_JOB_SOURCES_JSON values: {unknown_sources}")
        if len(configured_sources) != len(set(configured_sources)):
            errors.append("FREE_JOB_SOURCES_JSON contains duplicates")
        if not configured_sources and not config.ATS_DIRECT_ACQUISITION_ENABLED:
            errors.append("Free acquisition requires at least one global source or ATS direct acquisition")
        if not 5 <= config.FREE_SOURCE_REQUEST_TIMEOUT_SECONDS <= 60:
            errors.append("FREE_SOURCE_REQUEST_TIMEOUT_SECONDS must be between 5 and 60")
        if config.FREE_SOURCE_MAX_RESPONSE_CHARS < 100_000:
            errors.append("FREE_SOURCE_MAX_RESPONSE_CHARS must be at least 100000")
        if config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE < 1:
            errors.append("FREE_SOURCE_MAX_RECORDS_PER_SOURCE must be positive")
        if config.FREE_SOURCE_MIN_SUCCESSFUL_SOURCES < 1:
            errors.append("FREE_SOURCE_MIN_SUCCESSFUL_SOURCES must be positive")
        if not 1 <= config.HIMALAYAS_PAGE_SIZE <= 20:
            errors.append("HIMALAYAS_PAGE_SIZE must be between 1 and 20")
        if config.HIMALAYAS_MAX_PAGES < 1:
            errors.append("HIMALAYAS_MAX_PAGES must be positive")
        if config.FREE_SOURCE_LANDING_DISCOVERY_MAX_REQUESTS < 0:
            errors.append("FREE_SOURCE_LANDING_DISCOVERY_MAX_REQUESTS cannot be negative")
        if config.HIMALAYAS_COMPANY_PROFILE_MAX_REQUESTS < 0:
            errors.append("HIMALAYAS_COMPANY_PROFILE_MAX_REQUESTS cannot be negative")
        if config.HIMALAYAS_COMPANY_PROFILE_MAX_CONSECUTIVE_FAILURES < 1:
            errors.append(
                "HIMALAYAS_COMPANY_PROFILE_MAX_CONSECUTIVE_FAILURES must be at least 1"
            )
        if config.ATS_MAX_BOARDS_PER_RUN < 1 or config.ATS_MAX_JOBS_PER_BOARD < 1:
            errors.append("ATS board and job limits must be positive")
        if config.ATS_BOARD_REFRESH_INTERVAL_HOURS < 1:
            errors.append("ATS_BOARD_REFRESH_INTERVAL_HOURS must be positive")
        if config.ATS_WORKDAY_MAX_PAGES_PER_BOARD < 1:
            errors.append("ATS_WORKDAY_MAX_PAGES_PER_BOARD must be positive")
        if config.ATS_SMARTRECRUITERS_MAX_PAGES_PER_BOARD < 1:
            errors.append("ATS_SMARTRECRUITERS_MAX_PAGES_PER_BOARD must be positive")
        for name, value in (
            (
                "ATS_WORKDAY_DETAIL_MAX_REQUESTS_PER_BOARD",
                config.ATS_WORKDAY_DETAIL_MAX_REQUESTS_PER_BOARD,
            ),
            (
                "ATS_WORKDAY_DETAIL_MAX_REQUESTS_PER_RUN",
                config.ATS_WORKDAY_DETAIL_MAX_REQUESTS_PER_RUN,
            ),
            (
                "ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_BOARD",
                config.ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_BOARD,
            ),
            (
                "ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_RUN",
                config.ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_RUN,
            ),
        ):
            if value < 0:
                errors.append(f"{name} cannot be negative")
        if (
            config.MULTI_SOURCE_JSEARCH_TOPUP_ENABLED
            and not config.RAPIDAPI_KEY
        ):
            warnings.append(
                "MULTI_SOURCE_JSEARCH_TOPUP_ENABLED has no effect until JSearch "
                "is configured and has usable quota"
            )

    signing_key = str(config.VALIDATION_SIGNING_KEY or "")
    if config.PRODUCTION and (
        not signing_key
        or signing_key == "replace-with-a-long-random-secret"
        or len(signing_key) < 32
    ):
        errors.append(
            "VALIDATION_SIGNING_KEY must be a unique secret of at least 32 characters in production"
        )

    if config.VERIFY_WITH_HUNTER and not config.HUNTER_API_KEY:
        warnings.append("VERIFY_WITH_HUNTER=1 but HUNTER_API_KEY is missing")
    if config.PRODUCTION and not config.VALIDATION_SIGNING_KEY:
        errors.append(
            "VALIDATION_SIGNING_KEY is required in production to prevent "
            "Airtable edits from bypassing approval revalidation"
        )
    elif config.VALIDATION_SIGNING_KEY and len(config.VALIDATION_SIGNING_KEY) < 32:
        warnings.append("VALIDATION_SIGNING_KEY should contain at least 32 characters")

    if jsearch_mode and config.JSEARCH_MAX_QUERIES_PER_RUN < 0:
        errors.append("JSEARCH_MAX_QUERIES_PER_RUN cannot be negative")
    elif jsearch_mode and config.JSEARCH_MAX_QUERIES_PER_RUN:
        warnings.append(
            "JSEARCH_MAX_QUERIES_PER_RUN is active and will truncate the complete "
            f"role catalog to {config.JSEARCH_MAX_QUERIES_PER_RUN} queries per run"
        )
    if jsearch_mode and config.NUM_PAGES < 1:
        errors.append("NUM_PAGES must be at least 1")
    if jsearch_mode and config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN < 0:
        errors.append("JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN cannot be negative")
    scheduled_queries = (
        min(len(config.ROLES), config.JSEARCH_MAX_QUERIES_PER_RUN)
        if config.JSEARCH_MAX_QUERIES_PER_RUN
        else len(config.ROLES)
    )
    estimated_units = scheduled_queries * max(1, config.NUM_PAGES)
    if (
        jsearch_mode
        and config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN > 0
        and estimated_units > config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN
    ):
        errors.append(
            "Estimated JSearch usage exceeds the configured per-run budget: "
            f"{scheduled_queries} queries x {config.NUM_PAGES} pages = "
            f"{estimated_units} units > "
            f"{config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN}. Set NUM_PAGES=1 "
            "for the daily full catalog or intentionally raise the budget."
        )
    if jsearch_mode and config.JSEARCH_MIN_REMAINING_REQUESTS < 0:
        errors.append("JSEARCH_MIN_REMAINING_REQUESTS cannot be negative")
    if jsearch_mode and config.JSEARCH_MAX_EXTRA_PAGES_PER_ROLE < 0:
        errors.append("JSEARCH_MAX_EXTRA_PAGES_PER_ROLE cannot be negative")
    if jsearch_mode and config.JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES < 0:
        errors.append("JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES cannot be negative")
    if jsearch_mode and config.JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE < 0:
        errors.append("JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE cannot be negative")
    if jsearch_mode and config.JSEARCH_REMOTE_JOBS_ONLY:
        warnings.append(
            "JSEARCH_REMOTE_JOBS_ONLY=1 excludes valid onsite and hybrid hiring "
            "signals from the definitive v1.4 acquisition contract"
        )
    if not config.REQUIRE_FULL_TIME_ROLES:
        warnings.append("REQUIRE_FULL_TIME_ROLES=0 allows non-full-time employment labels")
    if not config.REJECT_NON_ACTIVE_HIRING_SIGNALS:
        warnings.append("REJECT_NON_ACTIVE_HIRING_SIGNALS=0 allows evergreen/future-opening posts")
    if not config.REQUIRE_EXPLICIT_US_REMOTE_SCOPE:
        warnings.append("REQUIRE_EXPLICIT_US_REMOTE_SCOPE=0 allows generic Anywhere listings from a US query echo")
    if config.FOUNDER_FALLBACK_MAX_EMPLOYEES < 1:
        errors.append("FOUNDER_FALLBACK_MAX_EMPLOYEES must be at least 1")
    if not config.AIRTABLE_SUPPRESS_EXISTING_COMPANY:
        warnings.append("AIRTABLE_SUPPRESS_EXISTING_COMPANY=0 can create uncoordinated duplicate-account outreach")
    final_target = config.get_final_pass_target()
    if final_target < 1:
        errors.append("TARGET_FINAL_PASS_LEADS_PER_RUN must be at least 1")
    if (
        config.MAX_ELIGIBLE_COMPANIES_PER_RUN > 0
        and config.MAX_ELIGIBLE_COMPANIES_PER_RUN < final_target
    ):
        warnings.append(
            "MAX_ELIGIBLE_COMPANIES_PER_RUN is below TARGET_FINAL_PASS_LEADS_PER_RUN; "
            "the daily target cannot be reached even if every eligible company converts"
        )
    if config.FINAL_PASS_PIPELINE_ENABLED:
        if not config.JOB_SOURCE_FETCH_ENABLED:
            warnings.append(
                "JOB_SOURCE_FETCH_ENABLED=0 forces official-source candidates to abstain "
                "unless a test injects verified source snapshots"
            )
        if not config.COMPANY_SOURCE_FETCH_ENABLED:
            warnings.append(
                "COMPANY_SOURCE_FETCH_ENABLED=0 can increase UNVERIFIED business-model decisions"
            )
        if jsearch_mode and config.FINAL_PASS_MICROBATCH_QUERY_UNITS < 1:
            errors.append("FINAL_PASS_MICROBATCH_QUERY_UNITS must be at least 1")
        elif jsearch_mode and config.FINAL_PASS_MICROBATCH_QUERY_UNITS > 12:
            errors.append("FINAL_PASS_MICROBATCH_QUERY_UNITS must be <= 12 in READY v1")
        if jsearch_mode and config.FINAL_PASS_MAX_TOPUP_ITERATIONS < 0:
            errors.append("FINAL_PASS_MAX_TOPUP_ITERATIONS cannot be negative; 0 means exhaustive")
        elif jsearch_mode and config.FINAL_PASS_MAX_TOPUP_ITERATIONS > 2:
            errors.append(
                "FINAL_PASS_MAX_TOPUP_ITERATIONS must be <= 2 in READY v1; "
                "inventory is weekly and top-up is not an unbounded recovery mechanism"
            )
        if jsearch_mode and 0 < config.FINAL_PASS_MAX_RUNTIME_SECONDS < 60:
            errors.append("FINAL_PASS_MAX_RUNTIME_SECONDS must be 0 or at least 60")
        if jsearch_mode and config.FINAL_PASS_MAX_EMPTY_QUERY_CYCLES < 1:
            errors.append("FINAL_PASS_MAX_EMPTY_QUERY_CYCLES must be at least 1")
        if not 1 <= config.JOB_SOURCE_DISCOVERY_MAX_PAGES <= 8:
            errors.append("JOB_SOURCE_DISCOVERY_MAX_PAGES must be between 1 and 8")
        if not 1 <= config.JOB_SOURCE_DISCOVERY_MAX_BOARD_PAGES <= 4:
            errors.append("JOB_SOURCE_DISCOVERY_MAX_BOARD_PAGES must be between 1 and 4")
        if not 5 <= config.JOB_SOURCE_DISCOVERY_BUDGET_SECONDS <= 60:
            errors.append("JOB_SOURCE_DISCOVERY_BUDGET_SECONDS must be between 5 and 60")
        if not 1 <= config.JOB_SOURCE_DISCOVERY_TIMEOUT_SECONDS <= 15:
            errors.append("JOB_SOURCE_DISCOVERY_TIMEOUT_SECONDS must be between 1 and 15")
        if not 1 <= config.JOB_SOURCE_TIMEOUT_SECONDS <= 20:
            errors.append("JOB_SOURCE_TIMEOUT_SECONDS must be between 1 and 20")
        if config.JOB_SOURCE_ATTEMPTS_PER_URL < 1:
            errors.append("JOB_SOURCE_ATTEMPTS_PER_URL must be at least 1")
        if not (
            1
            <= config.JOB_SOURCE_FRESH_DIRECT_MAX_AGE_DAYS
            <= config.RECOVERY_MAX_JOB_AGE_DAYS
        ):
            errors.append(
                "JOB_SOURCE_FRESH_DIRECT_MAX_AGE_DAYS must be between 1 and "
                "RECOVERY_MAX_JOB_AGE_DAYS"
            )
        if config.JOB_SOURCE_FRESH_DIRECT_MIN_DESCRIPTION_CHARS < 500:
            errors.append(
                "JOB_SOURCE_FRESH_DIRECT_MIN_DESCRIPTION_CHARS must be at least 500"
            )
        if not (
            1
            <= config.JOB_SOURCE_PROVIDER_STRUCTURED_MAX_AGE_DAYS
            <= config.RECOVERY_MAX_JOB_AGE_DAYS
        ):
            errors.append(
                "JOB_SOURCE_PROVIDER_STRUCTURED_MAX_AGE_DAYS must be between 1 "
                "and RECOVERY_MAX_JOB_AGE_DAYS"
            )
        if config.JOB_SOURCE_PROVIDER_STRUCTURED_MIN_DESCRIPTION_CHARS < 500:
            errors.append(
                "JOB_SOURCE_PROVIDER_STRUCTURED_MIN_DESCRIPTION_CHARS must be at least 500"
            )
        if not config.JOB_SOURCE_DIRECT_FIRST_ENABLED:
            errors.append(
                "JOB_SOURCE_DIRECT_FIRST_ENABLED must remain enabled in READY v1.1"
            )
        if not config.JOB_SOURCE_FRESH_DIRECT_FALLBACK_ENABLED:
            errors.append(
                "JOB_SOURCE_FRESH_DIRECT_FALLBACK_ENABLED must remain enabled in READY"
            )
        if not config.JOB_SOURCE_PROVIDER_STRUCTURED_REVIEW_ENABLED:
            errors.append(
                "JOB_SOURCE_PROVIDER_STRUCTURED_REVIEW_ENABLED must remain enabled in READY v1.2"
            )
        if config.PIPELINE_FAIL_PROCESS_ON_SLA_MISS:
            warnings.append(
                "PIPELINE_FAIL_PROCESS_ON_SLA_MISS=1 can trigger Railway restart loops "
                "after a technically successful low-volume run"
            )
        if config.CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET < 1:
            errors.append("CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET must be at least 1")
        if not config.REQUIRE_CURRENT_EMPLOYMENT_EVIDENCE:
            errors.append("REQUIRE_CURRENT_EMPLOYMENT_EVIDENCE must be enabled in strict FINAL_PASS mode")
        if not config.REQUIRE_CONTACT_LINKEDIN:
            errors.append("REQUIRE_CONTACT_LINKEDIN must be enabled in strict FINAL_PASS mode")
        if jsearch_mode and str(config.DATE_POSTED).lower() != "month":
            errors.append(
                "DATE_POSTED must be month so local v1.4 gates can evaluate the "
                "0-14 and 15-30 day windows"
            )
        if jsearch_mode and config.NUM_PAGES != 1:
            errors.append("NUM_PAGES must be 1; deeper acquisition is handled by bounded top-up")
        if config.READY_DAILY_DELIVERY_LIMIT < 0:
            errors.append(
                "READY_DAILY_DELIVERY_LIMIT cannot be negative; 0 means unlimited"
            )
        if config.READY_INVENTORY_TARGET < final_target:
            errors.append(
                "READY_INVENTORY_TARGET must be >= TARGET_FINAL_PASS_LEADS_PER_RUN"
            )
        if (
            config.READY_DAILY_DELIVERY_LIMIT > 0
            and config.READY_INVENTORY_TARGET < config.READY_DAILY_DELIVERY_LIMIT
        ):
            errors.append(
                "READY_INVENTORY_TARGET must be >= a positive READY_DAILY_DELIVERY_LIMIT"
            )
        if not 1 <= config.READY_INVENTORY_TTL_DAYS <= 3:
            errors.append("READY_INVENTORY_TTL_DAYS must be between 1 and 3")
        if config.PRIMARY_MAX_JOB_AGE_DAYS != 14:
            errors.append(
                "PRIMARY_MAX_JOB_AGE_DAYS must be 14 for the definitive v1.4 contract"
            )
        if config.RECOVERY_MIN_JOB_AGE_DAYS != 15:
            errors.append(
                "RECOVERY_MIN_JOB_AGE_DAYS must be 15 so recovery does not overlap primary"
            )
        if config.RECOVERY_MAX_JOB_AGE_DAYS != 30:
            errors.append(
                "RECOVERY_MAX_JOB_AGE_DAYS must be 30 for the definitive v1.4 contract"
            )
        if config.MAX_JOB_AGE_DAYS != config.PRIMARY_MAX_JOB_AGE_DAYS:
            errors.append(
                "MAX_JOB_AGE_DAYS compatibility value must equal PRIMARY_MAX_JOB_AGE_DAYS"
            )
        if not config.AGE_RECOVERY_ENABLED:
            warnings.append(
                "AGE_RECOVERY_ENABLED=0 disables the 15-30 day deficit recovery lane"
            )
        if config.JOB_SOURCE_MIN_INDEPENDENT_PUBLISHERS < 2:
            errors.append("JOB_SOURCE_MIN_INDEPENDENT_PUBLISHERS must be at least 2")
        if config.TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES < 1:
            errors.append("TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES must be at least 1")
        if config.MULTI_SOURCE_FINAL_PASS_MAX_TOPUP_ITERATIONS < 0:
            errors.append(
                "MULTI_SOURCE_FINAL_PASS_MAX_TOPUP_ITERATIONS cannot be negative"
            )
        if config.MULTI_SOURCE_TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES < 1:
            errors.append(
                "MULTI_SOURCE_TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES must be at least 1"
            )
        if config.PIPELINE_LOCK_STALE_HOURS < 1:
            errors.append("PIPELINE_LOCK_STALE_HOURS must be at least 1")
        if jsearch_mode and len(config.ROLES) > 50:
            errors.append(
                "ROLES_JSON contains more than 50 acquisition queries; remove the legacy "
                "118-role override and use acquisition families"
            )
        if not config.APPROVED_REVALIDATE_JOB_SOURCE:
            errors.append("APPROVED_REVALIDATE_JOB_SOURCE must be enabled before Instantly enrollment")
        if not config.SLA_REQUIRE_NET_NEW_AIRTABLE:
            warnings.append("SLA_REQUIRE_NET_NEW_AIRTABLE=0 can report target success before Airtable persistence")
        if config.RECOVERABLE_JOB_TTL_DAYS < 1 or config.RECOVERABLE_JOB_MAX_ATTEMPTS < 1:
            errors.append("Recoverable job queue TTL and max attempts must both be positive")
    if not 0 <= config.MAX_ROLE_FAILURE_RATE <= 1:
        errors.append("MAX_ROLE_FAILURE_RATE must be between 0 and 1")
    if jsearch_mode and config.JSEARCH_STOP_ON_LOW_QUOTA and config.JSEARCH_MIN_REMAINING_REQUESTS <= 0:
        warnings.append(
            "JSEARCH_STOP_ON_LOW_QUOTA=1 has no effect unless "
            "JSEARCH_MIN_REMAINING_REQUESTS is greater than zero"
        )
    if not _configured_campaign_ids():
        errors.append("No Instantly campaign ID is configured")
    else:
        uncovered_buckets = [
            bucket for bucket in _active_function_buckets()
            if not _bucket_has_campaign(bucket)
        ]
        if uncovered_buckets:
            warnings.append(
                "No Instantly campaign is configured for active role buckets: "
                + ", ".join(uncovered_buckets)
                + ". Leads can enter Airtable but cannot be enrolled until routing is added."
            )

    crm = Path(config.CRM_EXCLUSION_FILE)
    if not crm.exists():
        errors.append(f"CRM exclusion file does not exist: {crm}")
    elif crm.stat().st_size < 20:
        warnings.append(f"CRM exclusion file appears empty: {crm}")

    if Path(config.STAFFING_GROUND_TRUTH_FILE).exists():
        warnings.append("Staffing ground-truth file exists and will be used by audits")
    else:
        warnings.append("Staffing 95% accuracy is not independently validated yet")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def live_checks() -> Dict:
    results: Dict[str, object] = {}

    # Apollo's documented auth health endpoint does not consume enrichment credits.
    try:
        response = request_with_retry(
            "GET",
            "https://api.apollo.io/v1/auth/health",
            headers={
                "X-Api-Key": config.APOLLO_API_KEY,
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
            },
        )
        results["apollo"] = {"ok": True, "response": safe_json(response)}
    except Exception as exc:
        results["apollo"] = {"ok": False, "error": str(exc)}

    # Read-only Airtable check.
    try:
        airtable_client.validate_preflight()
        field_params = [("pageSize", 1)] + [
            ("fields[]", field_name) for field_name in airtable_client.REQUIRED_FIELDS
        ]
        response = request_with_retry(
            "GET",
            airtable_client._base_url(),  # intentional setup diagnostic
            headers=airtable_client._headers(),
            params=field_params,
        )
        data = safe_json(response)
        results["airtable"] = {
            "ok": True,
            "records_returned": len(data.get("records", [])),
            "required_fields_checked": len(airtable_client.REQUIRED_FIELDS),
        }
    except Exception as exc:
        results["airtable"] = {"ok": False, "error": str(exc)}

    # Read-only campaign existence checks.
    campaigns = {}
    for campaign_id in _configured_campaign_ids():
        try:
            response = request_with_retry(
                "GET",
                f"{config.INSTANTLY_BASE_URL.rstrip('/')}/campaigns/{campaign_id}",
                headers={"Authorization": f"Bearer {config.INSTANTLY_API_KEY}"},
            )
            data = safe_json(response)
            campaigns[campaign_id] = {"ok": True, "name": data.get("name"), "status": data.get("status")}
        except Exception as exc:
            campaigns[campaign_id] = {"ok": False, "error": str(exc)}
    results["instantly_campaigns"] = campaigns

    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Run read-only API checks")
    args = parser.parse_args()

    result = {"static": static_checks()}
    if args.live:
        result["live"] = live_checks()
    print(json.dumps(result, indent=2, default=str))

    if not result["static"]["ok"]:
        return 1
    if args.live:
        failed_live = []
        for key, value in result["live"].items():
            if key == "instantly_campaigns":
                failed_live.extend(cid for cid, item in value.items() if not item.get("ok"))
            elif isinstance(value, dict) and not value.get("ok"):
                failed_live.append(key)
        if failed_live:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
