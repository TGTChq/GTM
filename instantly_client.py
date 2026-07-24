"""Step 5: enroll Airtable-approved leads in Instantly API v2."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

import config
from job_signal import enrollment_block_reason
from http_utils import debug_dump, request_with_retry, safe_json

logger = logging.getLogger(__name__)


@dataclass
class EnrollmentResult:
    success: bool
    status: str  # enrolled / duplicate / failed
    record_id: str
    email: str
    campaign_id: str
    error: str = ""


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {config.INSTANTLY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def validate_preflight() -> None:
    if not config.INSTANTLY_API_KEY:
        raise ValueError("INSTANTLY_API_KEY is missing from .env")


def _flat(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def airtable_record_to_lead(record: Dict) -> Dict:
    fields = record.get("fields") or {}

    # Defense in depth: only validated actionable states may be enrolled, and
    # run_approved.py revalidates hard filters immediately before this call.
    final_decision = str(fields.get("Final Decision") or "").strip()
    validation_version = str(fields.get("Validation Version") or "").strip()
    if final_decision and final_decision not in {"FINAL_PASS", "NEEDS_CHECK", "UNVERIFIED"}:
        raise ValueError(f"Approved row is not actionable: {final_decision}")
    if final_decision and not validation_version:
        raise ValueError("Approved validated row is missing Validation Version")

    signal_block = enrollment_block_reason(fields)
    if signal_block:
        raise ValueError(signal_block)

    required = {
        "Email": fields.get("Email"),
        "Company": fields.get("Company"),
        "Open Role": fields.get("Open Role"),
        "Role Focus": fields.get("Role Focus"),
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise ValueError(
            "Missing required approved-lead fields: " + ", ".join(missing)
        )

    campaign_id = fields.get("Campaign ID") or config.resolve_campaign_id(
        fields.get("Role Bucket", ""), fields.get("Employees")
    )
    if not campaign_id:
        raise ValueError(
            f"No Instantly campaign configured for bucket={fields.get('Role Bucket')!r}, "
            f"size={fields.get('Size Band')!r}"
        )

    custom_variables = {
        "open_role": fields.get("Open Role"),
        "open_roles": fields.get("Open Roles"),
        "role_focus": fields.get("Role Focus"),
        "matched_role": fields.get("Matched Role"),
        "role_bucket": fields.get("Role Bucket"),
        "company_size": fields.get("Employees"),
        "company_size_band": fields.get("Size Band"),
        "job_posted_at": fields.get("Posted At"),
        "job_source": fields.get("Job Source"),
        "job_url": fields.get("Job URL"),
        "job_freshness": fields.get("Job Freshness"),
        "job_age_days": fields.get("Job Age Days"),
        "job_url_status": fields.get("Job URL Status"),
        "job_url_source": fields.get("Job URL Source"),
        "relevance": fields.get("Relevance"),
    }
    custom_variables = {
        key: _flat(value) for key, value in custom_variables.items() if value not in (None, "")
    }

    return {
        "campaign": campaign_id,
        "email": fields.get("Email", ""),
        "first_name": (fields.get("Hiring Manager", "").split(" ", 1)[0] or "").strip(),
        "last_name": (
            fields.get("Hiring Manager", "").split(" ", 1)[1].strip()
            if " " in fields.get("Hiring Manager", "").strip()
            else ""
        ),
        "company_name": fields.get("Company"),
        "job_title": fields.get("HM Title"),
        "website": fields.get("Website"),
        "skip_if_in_workspace": True,
        "skip_if_in_campaign": True,
        "verify_leads_on_import": config.INSTANTLY_VERIFY_ON_IMPORT,
        "custom_variables": custom_variables,
    }


def enroll_record(record: Dict) -> EnrollmentResult:
    validate_preflight()
    record_id = record.get("id", "")
    fields = record.get("fields") or {}
    email = fields.get("Email", "")
    try:
        lead = airtable_record_to_lead(record)
    except Exception as exc:
        return EnrollmentResult(False, "failed", record_id, email, "", str(exc))

    if not lead.get("email"):
        return EnrollmentResult(False, "failed", record_id, email, lead["campaign"], "Missing email")

    try:
        response = request_with_retry(
            "POST",
            f"{config.INSTANTLY_BASE_URL.rstrip('/')}/leads",
            headers=_headers(),
            json_body=lead,
        )
        data = safe_json(response)
        debug_dump("instantly_create_lead", data, redact_keys=("email",))
        return EnrollmentResult(True, "enrolled", record_id, email, lead["campaign"])
    except requests.HTTPError as exc:
        response = exc.response
        text = response.text if response is not None else str(exc)
        lowered = text.lower()
        if response is not None and response.status_code in {409, 422} and any(
            marker in lowered for marker in ("already", "duplicate", "exists")
        ):
            return EnrollmentResult(True, "duplicate", record_id, email, lead["campaign"])
        return EnrollmentResult(False, "failed", record_id, email, lead["campaign"], text[:1000])
    except Exception as exc:
        return EnrollmentResult(False, "failed", record_id, email, lead["campaign"], str(exc))


def enroll_approved_leads(airtable_records: List[Dict]) -> Dict:
    results: List[EnrollmentResult] = []
    for record in airtable_records:
        results.append(enroll_record(record))
        time.sleep(config.INSTANTLY_RATE_LIMIT_DELAY)

    successful_ids = [result.record_id for result in results if result.success]
    failed = [result for result in results if not result.success]
    return {
        "enrolled_record_ids": successful_ids,
        "enrolled": sum(result.status == "enrolled" for result in results),
        "duplicates": sum(result.status == "duplicate" for result in results),
        "failed": len(failed),
        "failures": [
            {"record_id": result.record_id, "email": result.email, "error": result.error}
            for result in failed
        ],
    }
