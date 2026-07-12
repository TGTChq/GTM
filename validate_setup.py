"""Validate local configuration and optionally perform read-only live checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

import requests

import airtable_client
import config
from http_utils import request_with_retry, safe_json


def _configured_campaign_ids() -> List[str]:
    values = {config.INSTANTLY_CAMPAIGN_ID}
    for base_env in config.CAMPAIGN_ENV_BY_BUCKET.values():
        values.add(__import__("os").getenv(base_env, ""))
        for band in ("SMALL", "MID", "LARGE", "UNKNOWN"):
            values.add(__import__("os").getenv(f"{base_env}_{band}", ""))
    return sorted(value for value in values if value)


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
    if not _configured_campaign_ids():
        errors.append("No Instantly campaign ID is configured")

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
