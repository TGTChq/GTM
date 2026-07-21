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

    required = {
        "RAPIDAPI_KEY": config.RAPIDAPI_KEY,
        "APOLLO_API_KEY": config.APOLLO_API_KEY,
        "AIRTABLE_TOKEN": config.AIRTABLE_TOKEN,
        "AIRTABLE_BASE_ID": config.AIRTABLE_BASE_ID,
        "INSTANTLY_API_KEY": config.INSTANTLY_API_KEY,
    }
    for name, value in required.items():
        if not value:
            errors.append(f"Missing {name}")

    if config.VERIFY_WITH_HUNTER and not config.HUNTER_API_KEY:
        warnings.append("VERIFY_WITH_HUNTER=1 but HUNTER_API_KEY is missing")

    if config.JSEARCH_MAX_QUERIES_PER_RUN < 0:
        errors.append("JSEARCH_MAX_QUERIES_PER_RUN cannot be negative")
    elif config.JSEARCH_MAX_QUERIES_PER_RUN:
        warnings.append(
            "JSEARCH_MAX_QUERIES_PER_RUN is active and will truncate the complete "
            f"role catalog to {config.JSEARCH_MAX_QUERIES_PER_RUN} queries per run"
        )
    if config.NUM_PAGES < 1:
        errors.append("NUM_PAGES must be at least 1")
    if config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN < 0:
        errors.append("JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN cannot be negative")
    scheduled_queries = (
        min(len(config.ROLES), config.JSEARCH_MAX_QUERIES_PER_RUN)
        if config.JSEARCH_MAX_QUERIES_PER_RUN
        else len(config.ROLES)
    )
    estimated_units = scheduled_queries * max(1, config.NUM_PAGES)
    if (
        config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN > 0
        and estimated_units > config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN
    ):
        errors.append(
            "Estimated JSearch usage exceeds the configured per-run budget: "
            f"{scheduled_queries} queries x {config.NUM_PAGES} pages = "
            f"{estimated_units} units > "
            f"{config.JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN}. Set NUM_PAGES=1 "
            "for the daily full catalog or intentionally raise the budget."
        )
    if config.JSEARCH_MIN_REMAINING_REQUESTS < 0:
        errors.append("JSEARCH_MIN_REMAINING_REQUESTS cannot be negative")
    if config.JSEARCH_MAX_EXTRA_PAGES_PER_ROLE < 0:
        errors.append("JSEARCH_MAX_EXTRA_PAGES_PER_ROLE cannot be negative")
    if config.JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES < 0:
        errors.append("JSEARCH_ADAPTIVE_MAX_EXTRA_QUERIES cannot be negative")
    if config.JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE < 0:
        errors.append("JSEARCH_ADAPTIVE_MIN_PREFILTER_VIABLE cannot be negative")
    if not config.JSEARCH_REMOTE_JOBS_ONLY:
        warnings.append(
            "JSEARCH_REMOTE_JOBS_ONLY=0 can substantially reduce reviewable lead volume "
            "because onsite jobs consume the same request budget"
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
    if config.MAX_ELIGIBLE_COMPANIES_PER_RUN < final_target:
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
        if config.FINAL_PASS_MICROBATCH_QUERY_UNITS < 1:
            errors.append("FINAL_PASS_MICROBATCH_QUERY_UNITS must be at least 1")
        if config.FINAL_PASS_MAX_TOPUP_ITERATIONS < 1:
            errors.append("FINAL_PASS_MAX_TOPUP_ITERATIONS must be at least 1")
        if config.FINAL_PASS_MAX_RUNTIME_SECONDS < 60:
            errors.append("FINAL_PASS_MAX_RUNTIME_SECONDS must be at least 60")
        if config.CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET < 1:
            errors.append("CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET must be at least 1")
    if not 0 <= config.MAX_ROLE_FAILURE_RATE <= 1:
        errors.append("MAX_ROLE_FAILURE_RATE must be between 0 and 1")
    if config.JSEARCH_STOP_ON_LOW_QUOTA and config.JSEARCH_MIN_REMAINING_REQUESTS <= 0:
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
        response = request_with_retry(
            "GET",
            airtable_client._base_url(),  # intentional setup diagnostic
            headers=airtable_client._headers(),
            params={"pageSize": 1},
        )
        data = safe_json(response)
        results["airtable"] = {"ok": True, "records_returned": len(data.get("records", []))}
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
