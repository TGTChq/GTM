"""Step 5: enroll Airtable-approved leads in Instantly API v2."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

import config
from job_signal import enrollment_block_reason
from http_utils import debug_dump, request_with_retry, safe_json

logger = logging.getLogger(__name__)


#: Membership classifications for a 2xx create-lead response.
#:
#: Instantly v2 ``POST /leads`` answers 200 "The Lead" whether it created a new lead
#: or the email already existed in the workspace -- the OpenAPI contract defines no
#: 409/422 for this operation and no created-vs-existing flag. So a bare 2xx is NOT
#: evidence of a net-new delivery. The returned Lead does carry ``id``, ``campaign``
#: and ``timestamp_created``, which lets the outcome be classified from the response
#: alone -- no extra API calls, no N+1. (``/campaigns/search-by-contact`` is per-email
#: only, and ``/leads/list`` ignores ``campaign_ids``, so neither offers a bulk
#: precheck.)
NEWLY_CREATED = "instantly_newly_created"
ALREADY_IN_TARGET_CAMPAIGN = "instantly_already_in_target_campaign"
EXISTING_OTHER_CAMPAIGN = "instantly_existing_other_campaign"
ALREADY_EXISTS_WORKSPACE = "instantly_already_exists_workspace"
MEMBERSHIP_UNKNOWN = "instantly_membership_unknown"
API_ERROR = "instantly_api_error"

MEMBERSHIP_CLASSES = (
    NEWLY_CREATED, ALREADY_IN_TARGET_CAMPAIGN, EXISTING_OTHER_CAMPAIGN,
    ALREADY_EXISTS_WORKSPACE, MEMBERSHIP_UNKNOWN, API_ERROR,
)

#: Clock skew allowed between our run clock and Instantly's ``timestamp_created``.
#: A lead created by THIS request is stamped ~now; anything older pre-existed.
_CREATION_SKEW_SECONDS = 120


@dataclass
class EnrollmentResult:
    success: bool
    status: str  # enrolled / duplicate / failed
    record_id: str
    email: str
    campaign_id: str
    error: str = ""
    #: Truthful outcome derived from the returned Lead (see MEMBERSHIP_CLASSES).
    membership: str = MEMBERSHIP_UNKNOWN
    lead_id: str = ""
    lead_campaign: str = ""
    created_at: str = ""

    @property
    def net_new(self) -> bool:
        """Only a lead this request actually created counts as net-new delivered."""
        return self.membership == NEWLY_CREATED


def _parse_ts(value) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_membership(data: Dict, *, target_campaign: str,
                        request_started_at: datetime) -> tuple[str, str, str, str]:
    """Classify a create-lead response. Returns ``(membership, id, campaign, created)``.

    Fails closed: anything that cannot be positively established as created by THIS
    request is reported as pre-existing or unknown, never as net-new.
    """
    if not isinstance(data, dict):
        return MEMBERSHIP_UNKNOWN, "", "", ""
    lead_id = str(data.get("id") or "")
    lead_campaign = str(data.get("campaign") or "")
    created_raw = str(data.get("timestamp_created") or "")
    created = _parse_ts(created_raw)
    if created is None:
        return MEMBERSHIP_UNKNOWN, lead_id, lead_campaign, created_raw
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    if created >= request_started_at - timedelta(seconds=_CREATION_SKEW_SECONDS):
        return NEWLY_CREATED, lead_id, lead_campaign, created_raw
    # Pre-existed in the workspace. Say where it lives now, since a lead sitting in
    # another campaign was NOT delivered into the campaign we intended.
    if not lead_campaign:
        return ALREADY_EXISTS_WORKSPACE, lead_id, lead_campaign, created_raw
    if target_campaign and lead_campaign == target_campaign:
        return ALREADY_IN_TARGET_CAMPAIGN, lead_id, lead_campaign, created_raw
    return EXISTING_OTHER_CAMPAIGN, lead_id, lead_campaign, created_raw


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


def airtable_record_to_lead(record: Dict, *, probe: bool = True) -> Dict:
    fields = record.get("fields") or {}

    # Defense in depth: only validated actionable states may be enrolled, and
    # run_approved.py revalidates hard filters immediately before this call.
    final_decision = str(fields.get("Final Decision") or "").strip()
    validation_version = str(fields.get("Validation Version") or "").strip()
    if final_decision and final_decision not in {"FINAL_PASS", "NEEDS_CHECK", "UNVERIFIED"}:
        raise ValueError(f"Approved row is not actionable: {final_decision}")
    if final_decision and not validation_version:
        raise ValueError("Approved validated row is missing Validation Version")
    if fields.get("Outbound Hold"):
        raise ValueError("Outbound display is held for manual review")
    company_confidence = str(fields.get("Outbound Company Confidence") or "").strip().lower()
    if validation_version == str(config.VALIDATION_VERSION) and company_confidence not in {"high", "medium"}:
        raise ValueError("Outbound company display confidence is not send-safe")

    # ``probe=False`` keeps this builder zero-network (job-URL status is not
    # fetched) so the approved-sync preflight and local revalidation stay hermetic.
    signal_block = enrollment_block_reason(fields, probe_missing=probe)
    if signal_block:
        raise ValueError(signal_block)

    required = {
        "Email": fields.get("Email"),
        "Outbound Company": fields.get("Outbound Company"),
        "Outbound Role": fields.get("Outbound Role"),
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
        "open_role": fields.get("Outbound Role"),
        "open_roles": fields.get("Outbound Roles") or fields.get("Outbound Role"),
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
        "company_name": fields.get("Outbound Company"),
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
        # probe=False: Approved Sync is delivery-only. With probe=True a row whose
        # ``Job URL Status`` is blank triggered select_job_url(probe=True) -- a live
        # job-URL fetch during enrollment. Approval is the authorization boundary,
        # so delivery performs no network corroboration.
        lead = airtable_record_to_lead(record, probe=False)
    except Exception as exc:
        # Never reached Instantly -- membership is not applicable, so leave it blank
        # rather than polluting the membership histogram with un-attempted rows.
        return EnrollmentResult(False, "failed", record_id, email, "", str(exc), membership="")

    if not lead.get("email"):
        return EnrollmentResult(False, "failed", record_id, email, lead["campaign"],
                                "Missing email", membership="")

    started = datetime.now(timezone.utc)
    try:
        response = request_with_retry(
            "POST",
            f"{config.INSTANTLY_BASE_URL.rstrip('/')}/leads",
            headers=_headers(),
            json_body=lead,
        )
        data = safe_json(response)
        debug_dump("instantly_create_lead", data, redact_keys=("email",))
        membership, lead_id, lead_campaign, created_at = classify_membership(
            data, target_campaign=str(lead["campaign"] or ""), request_started_at=started
        )
        if membership != NEWLY_CREATED:
            logger.info(
                "Instantly returned a pre-existing lead for record=%s: %s "
                "(lead_campaign=%s target=%s created=%s)",
                record_id, membership, lead_campaign or "-", lead["campaign"], created_at or "-",
            )
        return EnrollmentResult(
            True, "enrolled", record_id, email, lead["campaign"],
            membership=membership, lead_id=lead_id,
            lead_campaign=lead_campaign, created_at=created_at,
        )
    except requests.HTTPError as exc:
        response = exc.response
        text = response.text if response is not None else str(exc)
        lowered = text.lower()
        if response is not None and response.status_code in {409, 422} and any(
            marker in lowered for marker in ("already", "duplicate", "exists")
        ):
            return EnrollmentResult(True, "duplicate", record_id, email, lead["campaign"],
                                    membership=ALREADY_EXISTS_WORKSPACE)
        return EnrollmentResult(False, "failed", record_id, email, lead["campaign"], text[:1000],
                                membership=API_ERROR)
    except Exception as exc:
        return EnrollmentResult(False, "failed", record_id, email, lead["campaign"], str(exc),
                                membership=API_ERROR)


def enroll_approved_leads(airtable_records: List[Dict]) -> Dict:
    results: List[EnrollmentResult] = []
    for record in airtable_records:
        results.append(enroll_record(record))
        time.sleep(config.INSTANTLY_RATE_LIMIT_DELAY)

    successful_ids = [result.record_id for result in results if result.success]
    failed = [result for result in results if not result.success]
    membership = {name: 0 for name in MEMBERSHIP_CLASSES}
    for result in results:
        if result.membership:
            membership[result.membership] = membership.get(result.membership, 0) + 1
    return {
        "enrolled_record_ids": successful_ids,
        "enrolled": sum(result.status == "enrolled" for result in results),
        "duplicates": sum(result.status == "duplicate" for result in results),
        "failed": len(failed),
        "failures": [
            {"record_id": result.record_id, "email": result.email, "error": result.error}
            for result in failed
        ],
        # Truthful delivery accounting. ``enrolled`` counts accepted API responses;
        # only ``net_new_delivered`` counts leads this run actually created.
        "membership": membership,
        "net_new_delivered": sum(result.net_new for result in results),
        "pre_existing_in_instantly": sum(
            result.membership in (ALREADY_IN_TARGET_CAMPAIGN, EXISTING_OTHER_CAMPAIGN,
                                  ALREADY_EXISTS_WORKSPACE)
            for result in results
        ),
        "pre_existing_detail": [
            {"record_id": r.record_id, "membership": r.membership,
             "lead_campaign": r.lead_campaign, "target_campaign": r.campaign_id,
             "created_at": r.created_at}
            for r in results if r.membership in (ALREADY_IN_TARGET_CAMPAIGN,
                                                 EXISTING_OTHER_CAMPAIGN,
                                                 ALREADY_EXISTS_WORKSPACE)
        ],
    }
