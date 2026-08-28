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


#: Memberships that mean the lead IS in the campaign we intended. ONLY these may be
#: written back to Airtable as Enrolled. A 2xx response is not on this list: a lead
#: that already lived in some OTHER campaign was never delivered where we intended,
#: and marking it Enrolled retires it from the queue on a delivery that never happened.
DELIVERED_MEMBERSHIPS = (NEWLY_CREATED, ALREADY_IN_TARGET_CAMPAIGN)


@dataclass
class EnrollmentResult:
    #: True ONLY when the lead is in the target campaign -- this drives the Airtable
    #: Enrolled write. It is deliberately NOT "the API returned 2xx".
    success: bool
    status: str  # enrolled / duplicate / not_delivered / failed
    record_id: str
    email: str
    campaign_id: str
    error: str = ""
    #: Truthful outcome derived from the returned Lead (see MEMBERSHIP_CLASSES).
    membership: str = MEMBERSHIP_UNKNOWN
    lead_id: str = ""
    lead_campaign: str = ""
    created_at: str = ""
    #: The API accepted the request (2xx). Kept separate from ``success`` so logs can
    #: still report acceptance without implying delivery.
    api_accepted: bool = False
    #: Campaign ids returned by the authoritative membership lookup, when consulted.
    verified_campaigns: tuple = ()

    @property
    def net_new(self) -> bool:
        """Only a lead this request actually created counts as net-new delivered."""
        return self.membership == NEWLY_CREATED

    @property
    def delivered(self) -> bool:
        return self.membership in DELIVERED_MEMBERSHIPS


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


def verify_membership_via_search(email: str, target_campaign: str) -> tuple[str, tuple]:
    """Authoritatively resolve target-campaign membership for ONE email.

    ``GET /campaigns/search-by-contact`` is the only endpoint that truthfully answers
    "which campaigns contain this email" -- ``/leads/list`` ignores its
    ``campaign_ids`` filter (proven in production: it returns leads from other
    campaigns when filtered), so it must never be used for this.

    This is the EXCEPTIONAL path only: it runs for pre-existing/ambiguous responses,
    never for a lead this request just created, so overhead stays bounded to
    duplicates rather than becoming N+1 across the whole run.

    Fails closed -- any error or unparseable answer is ``MEMBERSHIP_UNKNOWN``.
    """
    if not email:
        return MEMBERSHIP_UNKNOWN, ()
    try:
        response = request_with_retry(
            "GET",
            f"{config.INSTANTLY_BASE_URL.rstrip('/')}/campaigns/search-by-contact",
            headers=_headers(),
            params={"search": email},
        )
        data = safe_json(response)
    except Exception as exc:
        logger.warning("Instantly membership lookup failed: %s", str(exc)[:200])
        return MEMBERSHIP_UNKNOWN, ()
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return MEMBERSHIP_UNKNOWN, ()
    campaigns = tuple(
        str(c.get("id") or "") for c in items if isinstance(c, dict) and c.get("id")
    )
    if not campaigns:
        # Authoritative: the email is in no campaign at all, so it is certainly not
        # in ours. It exists in the workspace but was not delivered.
        return ALREADY_EXISTS_WORKSPACE, ()
    if target_campaign and target_campaign in campaigns:
        return ALREADY_IN_TARGET_CAMPAIGN, campaigns
    return EXISTING_OTHER_CAMPAIGN, campaigns


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


def wave1_enrollment_overlay(record: Dict) -> tuple[str, Dict[str, str]]:
    """Outbound Wave 1 overlay for one approved record.

    Returns ``(challenger_campaign_id, custom_variables)``. Both are empty for a
    Control A record, so the enrollment payload stays byte-identical to what
    production sends today.

    Four independent conditions must ALL hold before a record is routed to the
    challenger; any one of them failing leaves the record on Control A:

    1. ``OUTBOUND_WAVE1_ENABLED`` is set;
    2. the account hashes into arm B at the configured split;
    3. every Wave 1 QA gate passes for the rendered copy;
    4. a challenger campaign id is configured for that role bucket.

    The whole overlay is wrapped: a failure inside the experiment must never be
    able to break, or silently alter, a control enrollment.
    """
    if not getattr(config, "OUTBOUND_WAVE1_ENABLED", False):
        return "", {}
    try:
        from outbound_wave1 import resolve_wave1
        from outbound_wave1.assignment import ARM_B
        from outbound_wave1.claims import load_claim_registry

        fields = record.get("fields") or {}
        resolution = resolve_wave1(
            fields,
            experiment_id=config.OUTBOUND_WAVE1_EXPERIMENT_ID,
            b_split_pct=config.OUTBOUND_WAVE1_B_SPLIT_PCT,
            salt=config.OUTBOUND_WAVE1_ASSIGNMENT_SALT,
            registry=load_claim_registry(config.OUTBOUND_WAVE1_CLAIM_REGISTRY_PATH),
            record_id=str(record.get("id") or ""),
        )
        if resolution.experiment_arm != ARM_B:
            return "", {}
        if not resolution.qa_pass:
            logger.info(
                "Wave 1 challenger withheld for %s: %s",
                resolution.record_id, ",".join(resolution.qa_reasons)[:200],
            )
            return "", {}
        challenger_campaign = config.resolve_wave1_challenger_campaign_id(
            resolution.role_bucket
        )
        if not challenger_campaign:
            return "", {}
        return challenger_campaign, resolution.to_custom_variables()
    except Exception as exc:  # noqa: BLE001 - the experiment never breaks delivery
        logger.warning("Wave 1 overlay skipped: %s", str(exc)[:200])
        return "", {}


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

    wave1_campaign, wave1_variables = wave1_enrollment_overlay(record)
    if wave1_campaign:
        campaign_id = wave1_campaign
    custom_variables.update(wave1_variables)

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
    target = str(lead["campaign"] or "")
    try:
        response = request_with_retry(
            "POST",
            f"{config.INSTANTLY_BASE_URL.rstrip('/')}/leads",
            headers=_headers(),
            # Never add the same email to the SAME campaign twice. This cannot
            # suppress a legitimate enrollment -- it only prevents a duplicate send
            # to a person already in this campaign. ``skip_if_in_workspace`` follows
            # the canonical anti-spam policy switch instead of being hard-coded:
            # production leaves ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS off, which
            # deliberately allows one person across different role-bucket campaigns.
            json_body={
                **lead,
                "skip_if_in_campaign": True,
                "skip_if_in_workspace": bool(
                    getattr(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", False)
                ),
            },
        )
        data = safe_json(response)
        debug_dump("instantly_create_lead", data, redact_keys=("email",))
        membership, lead_id, lead_campaign, created_at = classify_membership(
            data, target_campaign=target, request_started_at=started
        )
        verified: tuple = ()
        if membership != NEWLY_CREATED:
            # timestamp_created proves created-vs-existing, but NOT which campaigns an
            # existing lead belongs to. Resolve that authoritatively -- exceptional
            # path only, so the normal run costs no extra calls.
            membership, verified = verify_membership_via_search(email, target)
            logger.info(
                "Instantly returned a pre-existing lead for record=%s: %s "
                "(response_campaign=%s target=%s created=%s verified_campaigns=%d)",
                record_id, membership, lead_campaign or "-", target,
                created_at or "-", len(verified),
            )
        delivered = membership in DELIVERED_MEMBERSHIPS
        status = ("enrolled" if membership == NEWLY_CREATED
                  else "duplicate" if membership == ALREADY_IN_TARGET_CAMPAIGN
                  else "not_delivered")
        error = "" if delivered else (
            f"Not delivered to target campaign {target}: {membership}"
            + (f" (lead is in {len(verified)} other campaign(s))" if verified else "")
        )
        return EnrollmentResult(
            delivered, status, record_id, email, lead["campaign"], error,
            membership=membership, lead_id=lead_id,
            lead_campaign=lead_campaign, created_at=created_at,
            api_accepted=True, verified_campaigns=verified,
        )
    except requests.HTTPError as exc:
        response = exc.response
        text = response.text if response is not None else str(exc)
        lowered = text.lower()
        if response is not None and response.status_code in {409, 422} and any(
            marker in lowered for marker in ("already", "duplicate", "exists")
        ):
            # An explicit duplicate rejection proves the email exists but NOT that it
            # is in our target campaign -- resolve authoritatively, fail closed.
            membership, verified = verify_membership_via_search(email, str(lead["campaign"] or ""))
            delivered = membership in DELIVERED_MEMBERSHIPS
            return EnrollmentResult(
                delivered,
                "duplicate" if delivered else "not_delivered",
                record_id, email, lead["campaign"],
                "" if delivered else f"Duplicate not in target campaign: {membership}",
                membership=membership, api_accepted=True, verified_campaigns=verified)
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

    # Only target-delivered rows may be written back as Enrolled. Everything else --
    # including 2xx responses for leads that live in another campaign -- stays out of
    # the Enrolled set and is reported as a failure so it is never silently retired.
    successful_ids = [result.record_id for result in results if result.delivered]
    failed = [result for result in results if not result.delivered]
    membership = {name: 0 for name in MEMBERSHIP_CLASSES}
    for result in results:
        if result.membership:
            membership[result.membership] = membership.get(result.membership, 0) + 1
    return {
        "enrolled_record_ids": successful_ids,
        "enrolled": sum(result.status == "enrolled" for result in results),
        "duplicates": sum(result.status == "duplicate" for result in results),
        "not_delivered": sum(result.status == "not_delivered" for result in results),
        "api_accepted": sum(result.api_accepted for result in results),
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
