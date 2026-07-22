"""Step 4: idempotent Airtable review queue client."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, Iterable, List, Set
from urllib.parse import quote

import config
from company_identity import normalize_company_name
from domain_utils import normalize_company_domain
from job_filter import normalize_text
from job_signal import annotate_job
from validation_integrity import validation_fingerprint, utc_now_iso
from http_utils import request_with_retry, safe_json

logger = logging.getLogger(__name__)
AIRTABLE_API_BASE = "https://api.airtable.com/v0"


REQUIRED_FIELDS = [
    "Lead Key",
    "Company",
    "Website",
    "Open Role",
    "Open Roles",
    "Role Focus",
    "Focus Quality",
    "Focus Evidence",
    "Matched Role",
    "Role Bucket",
    "Job URL",
    "Job Source",
    "Posted At",
    "Job Freshness",
    "Job Age Days",
    "Job URL Status",
    "Job URL Source",
    "Job Signal Notes",
    "Location",
    "Employment Type",
    "Relevance",
    "Relevance Score",
    "Relevance Reason",
    "Hiring Manager",
    "HM Title",
    "LinkedIn",
    "Apollo Person ID",
    "Email",
    "Email Source",
    "Apollo Email Status",
    "Hunter Email Status",
    "Confidence",
    "Employees",
    "Size Band",
    "Founded",
    "Industry",
    "Campaign ID",
    "Job ID",
    "Final Decision",
    "Decision Reason",
    "Secondary Reasons",
    "Official Source",
    "Evidence Status",
    "Firmographics Status",
    "Contact Alignment",
    "Email Validation",
    "Validation Version",
    "Validated At",
    "Validation Fingerprint",
    "Evidence Bundle",
    "Status",
    "Error",
]


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {config.AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    table = quote(config.AIRTABLE_TABLE_NAME, safe="")
    return f"{AIRTABLE_API_BASE}/{config.AIRTABLE_BASE_ID}/{table}"


def validate_preflight() -> None:
    missing = [
        name for name, value in (
            ("AIRTABLE_TOKEN", config.AIRTABLE_TOKEN),
            ("AIRTABLE_BASE_ID", config.AIRTABLE_BASE_ID),
            ("AIRTABLE_TABLE_NAME", config.AIRTABLE_TABLE_NAME),
        ) if not value
    ]
    if missing:
        raise ValueError(f"Missing Airtable configuration: {', '.join(missing)}")


def _clean_fields(fields: Dict) -> Dict:
    return {key: value for key, value in fields.items() if value not in (None, "", [])}


def _gate_state(job: Dict, gate: str) -> str:
    decision = (job.get("_gate_decisions") or {}).get(gate) or {}
    return str(decision.get("state") or job.get(f"_{gate}_gate_state") or "")


def _evidence_bundle_text(job: Dict) -> str:
    payload = {
        "final_state": job.get("_final_state"),
        "primary_reason": job.get("_final_primary_reason"),
        "secondary_reasons": job.get("_final_secondary_reasons") or [],
        "gate_decisions": job.get("_gate_decisions") or {},
        "validation_version": job.get("_validation_version") or config.VALIDATION_VERSION,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:95000]


def _job_to_fields(job: Dict) -> Dict:
    related_roles = job.get("related_open_roles") or []
    relevance_reasons = job.get("_role_relevance_reasons") or []
    strict_state = str(job.get("_final_state") or "")
    relevance = job.get("_airtable_relevance") if strict_state else job.get("_role_relevance_status")
    official_source = (
        job.get("canonical_source_url")
        or job.get("official_source_url")
        or job.get("official_job_url")
        or job.get("job_url_selected")
        or job.get("job_apply_link")
        or job.get("job_google_link")
    )
    official_source_type = (
        job.get("canonical_source_type")
        or job.get("official_job_source_type")
        or job.get("job_url_source")
    )
    source_state = str(job.get("official_job_status") or "")
    canonical_active_status = job.get("canonical_active_status")
    if not canonical_active_status and source_state:
        canonical_active_status = {
            "ACTIVE_VERIFIED": "verified",
            "INACTIVE_VERIFIED": "broken",
            "SOURCE_TEMPORARILY_UNAVAILABLE": "unverified_review",
            "SOURCE_UNRESOLVED": "unverified_review",
        }.get(source_state, source_state.lower())
    fields = {
        "Lead Key": job.get("lead_key"),
        "Company": job.get("canonical_company_name") or job.get("canonical_employer_name") or job.get("employer_name"),
        "Website": (
            f"https://{job.get('company_domain')}"
            if job.get("company_domain")
            else job.get("employer_website")
        ),
        "Open Role": job.get("canonical_job_title") or job.get("job_title"),
        "Open Roles": " | ".join(related_roles),
        "Role Focus": job.get("role_focus"),
        "Focus Quality": job.get("role_focus_quality"),
        "Focus Evidence": " | ".join(job.get("role_focus_evidence") or []),
        "Matched Role": job.get("_matched_role"),
        "Role Bucket": job.get("_role_bucket"),
        "Job URL": official_source,
        "Job Source": official_source_type or job.get("job_publisher"),
        "Posted At": job.get("canonical_published_at") or job.get("job_posted_at_datetime_utc") or job.get("job_posted_at_timestamp"),
        "Job Freshness": job.get("job_freshness"),
        "Job Age Days": job.get("job_age_days"),
        "Job URL Status": canonical_active_status or job.get("job_url_status"),
        "Job URL Source": official_source_type,
        "Job Signal Notes": " | ".join(
            part for part in (
                job.get("job_signal_notes"),
                f"us_evidence={job.get('_us_eligibility_reason')}" if job.get("_us_eligibility_reason") else "",
                f"employment={job.get('_employment_quality_reason')}" if job.get("_employment_quality_reason") else "",
            ) if part
        ),
        "Location": job.get("canonical_location") or job.get("_normalized_location") or job.get("job_location"),
        "Employment Type": job.get("canonical_employment_type") or job.get("job_employment_type"),
        "Relevance": relevance,
        "Relevance Score": job.get("_role_relevance_score"),
        "Relevance Reason": " | ".join(relevance_reasons),
        "Hiring Manager": job.get("hiring_manager_name"),
        "HM Title": job.get("hiring_manager_title"),
        "LinkedIn": job.get("hiring_manager_linkedin"),
        "Apollo Person ID": job.get("hiring_manager_person_id"),
        "Email": job.get("hiring_manager_email"),
        "Email Source": job.get("hiring_manager_email_source"),
        "Apollo Email Status": job.get("apollo_email_status"),
        "Hunter Email Status": job.get("hunter_email_status"),
        "Confidence": job.get("hiring_manager_confidence"),
        "Employees": job.get("company_employee_count"),
        "Size Band": config.company_size_band(job.get("company_employee_count")),
        "Founded": job.get("company_founded_year"),
        "Industry": job.get("company_industry"),
        "Campaign ID": job.get("campaign_id"),
        "Job ID": job.get("canonical_job_id") or job.get("job_id"),
        "Final Decision": strict_state,
        "Decision Reason": job.get("_final_primary_reason"),
        "Secondary Reasons": " | ".join(job.get("_final_secondary_reasons") or []),
        "Official Source": official_source,
        "Evidence Status": _gate_state(job, "job"),
        "Firmographics Status": _gate_state(job, "account"),
        "Contact Alignment": _gate_state(job, "contact"),
        "Email Validation": _gate_state(job, "email"),
        "Validation Version": job.get("_validation_version") or (config.VALIDATION_VERSION if strict_state else None),
        "Validated At": job.get("_validation_timestamp") or (utc_now_iso() if strict_state else None),
        "Evidence Bundle": _evidence_bundle_text(job) if strict_state else None,
        "Status": config.AIRTABLE_STATUS_PENDING,
    }
    cleaned = _clean_fields(fields)
    if strict_state:
        cleaned["Validation Fingerprint"] = validation_fingerprint(cleaned)
    return cleaned


def _get_existing_leads() -> Dict[str, Dict]:
    """Return existing Airtable records keyed by Lead Key.

    Role-focus fields are included so a rerun can repair records created by an
    older pipeline version without overwriting reviewer edits.
    """
    records_by_key: Dict[str, Dict] = {}
    offset = None
    while True:
        params: List[tuple[str, str | int]] = [
            ("pageSize", 100),
            ("fields[]", "Lead Key"),
            ("fields[]", "Company"),
            ("fields[]", "Website"),
            ("fields[]", "Role Bucket"),
            ("fields[]", "Status"),
            ("fields[]", "Role Focus"),
            ("fields[]", "Focus Quality"),
            ("fields[]", "Focus Evidence"),
            ("fields[]", "Open Role"),
            ("fields[]", "Matched Role"),
            ("fields[]", "Job URL"),
            ("fields[]", "Posted At"),
            ("fields[]", "Job Freshness"),
            ("fields[]", "Job Age Days"),
            ("fields[]", "Job URL Status"),
            ("fields[]", "Job URL Source"),
            ("fields[]", "Job Signal Notes"),
        ]
        if offset:
            params.append(("offset", offset))
        response = request_with_retry("GET", _base_url(), headers=_headers(), params=params)
        data = safe_json(response)
        for record in data.get("records", []):
            fields = record.get("fields") or {}
            key = fields.get("Lead Key")
            if key:
                records_by_key[str(key)] = {
                    "id": record.get("id"),
                    "fields": fields,
                }
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)
    return records_by_key


def _company_identity_keys_from_fields(fields: Dict) -> Set[str]:
    keys: Set[str] = set()
    domain = normalize_company_domain(fields.get("Website"))
    if domain:
        keys.add(f"domain:{domain}")
    company = normalize_company_name(fields.get("Company"))
    if company:
        keys.add(f"name:{company}")
    return keys


def _company_identity_keys_from_job(job: Dict) -> Set[str]:
    fields = {
        "Website": (
            f"https://{job.get('company_domain')}"
            if job.get("company_domain")
            else job.get("employer_website")
        ),
        "Company": job.get("employer_name"),
    }
    return _company_identity_keys_from_fields(fields)


def _active_existing_company_keys(existing: Dict[str, Dict]) -> Set[str]:
    keys: Set[str] = set()
    for record in existing.values():
        fields = record.get("fields") or {}
        status = str(fields.get("Status") or "").strip().lower()
        # Rejected and Error records may re-enter when a later job is genuinely
        # qualified. Pending, Approved, Enrolled, and blank legacy states remain
        # suppressed to avoid uncoordinated multi-campaign contact.
        retryable_statuses = {
            str(config.AIRTABLE_STATUS_ERROR).strip().lower(),
            str(config.AIRTABLE_STATUS_REJECTED).strip().lower(),
        }
        if status in retryable_statuses:
            continue
        keys.update(_company_identity_keys_from_fields(fields))
    return keys


def get_active_existing_company_keys_for_pipeline() -> Set[str]:
    """Return company keys in the same format used by hiring_manager.

    The daily target is a target of *new* Airtable accounts.  Suppressing a
    company only after reaching 30 upstream can leave fewer than 30 created
    rows, so active Airtable accounts are excluded before Apollo and before the
    FINAL_PASS counter is evaluated.
    """
    validate_preflight()
    existing = _get_existing_leads()
    result: Set[str] = set()
    retryable_statuses = {
        str(config.AIRTABLE_STATUS_ERROR).strip().lower(),
        str(config.AIRTABLE_STATUS_REJECTED).strip().lower(),
    }
    for record in existing.values():
        fields = record.get("fields") or {}
        status = str(fields.get("Status") or "").strip().lower()
        if status in retryable_statuses:
            continue
        domain = normalize_company_domain(fields.get("Website"))
        if domain:
            result.add(domain)
        company = normalize_text(fields.get("Company") or "")
        if company:
            result.add(company)
    return result


def _ensure_job_signal(job: Dict) -> Dict:
    required = ("job_freshness", "job_url_status", "job_url_selected")
    if all(job.get(key) not in (None, "") for key in required):
        return dict(job)
    return annotate_job(job, probe_url=True)


def push_leads(jobs: List[Dict], batch_size: int = 10) -> Dict:
    validate_preflight()
    strict_mode = any(job.get("_final_state") for job in jobs)
    if strict_mode:
        reviewable = [
            dict(job) for job in jobs
            if job.get("_final_state") in {"FINAL_PASS", "NEEDS_CHECK"}
            and job.get("_airtable_relevance") in {"accept", "review"}
            and job.get("hiring_manager_email")
            and job.get("lead_key")
        ]
    else:
        # Legacy compatibility is retained only for rollback-mode fixtures.
        reviewable = [
            _ensure_job_signal(job) for job in jobs
            if job.get("_step3_status") == "found"
            and job.get("hiring_manager_confidence") in {"high", "medium", "low"}
            and job.get("hiring_manager_email")
            and job.get("lead_key")
        ]

    # One contact/company/bucket record only, even if upstream data is duplicated.
    unique_by_key = {job["lead_key"]: job for job in reviewable}
    existing = _get_existing_leads()

    existing_keys = [key for key in unique_by_key if key in existing]
    existing_company_keys = (
        _active_existing_company_keys(existing)
        if config.AIRTABLE_SUPPRESS_EXISTING_COMPANY
        else set()
    )
    suppressed_company_keys: List[str] = []
    to_create: List[Dict] = []
    for key, job in unique_by_key.items():
        if key in existing:
            continue
        company_keys = _company_identity_keys_from_job(job)
        if company_keys and company_keys & existing_company_keys:
            suppressed_company_keys.append(key)
            continue
        to_create.append(job)

    # Repair blank generated fields only. Never overwrite reviewer-edited values
    # or reset Status on an existing record.
    to_update: List[Dict] = []
    for key in existing_keys:
        record = existing[key]
        existing_fields = record.get("fields") or {}
        job = unique_by_key[key]
        patch_fields: Dict = {}

        generated_focus = job.get("role_focus")
        if not existing_fields.get("Role Focus") and generated_focus:
            patch_fields.update({
                "Role Focus": generated_focus,
                "Focus Quality": job.get("role_focus_quality"),
                "Focus Evidence": " | ".join(job.get("role_focus_evidence") or []),
            })

        if not existing_fields.get("Job Freshness"):
            patch_fields.update({
                "Job Freshness": job.get("job_freshness"),
                "Job Age Days": job.get("job_age_days"),
                "Job Signal Notes": job.get("job_signal_notes"),
            })

        if not existing_fields.get("Job URL Status"):
            canonical_fields = _job_to_fields(job)
            patch_fields.update({
                "Job URL": canonical_fields.get("Job URL") or existing_fields.get("Job URL"),
                "Job URL Status": canonical_fields.get("Job URL Status"),
                "Job URL Source": canonical_fields.get("Job URL Source"),
                "Official Source": canonical_fields.get("Official Source"),
                "Job Signal Notes": job.get("job_signal_notes"),
            })

        if patch_fields:
            to_update.append({
                "id": record.get("id"),
                "lead_key": key,
                "fields": _clean_fields(patch_fields),
                "updated_role_focus": bool(
                    not existing_fields.get("Role Focus") and generated_focus
                ),
                "updated_job_signal": bool(
                    not existing_fields.get("Job Freshness")
                    or not existing_fields.get("Job URL Status")
                ),
            })

    created = 0
    updated_missing_role_focus = 0
    updated_missing_job_signals = 0
    failed = 0
    failed_lead_keys: List[str] = []
    effective_batch_size = min(batch_size, 10)

    for index in range(0, len(to_create), effective_batch_size):
        batch = to_create[index:index + effective_batch_size]
        body = {
            "records": [{"fields": _job_to_fields(job)} for job in batch],
            "typecast": True,
        }
        try:
            response = request_with_retry("POST", _base_url(), headers=_headers(), json_body=body)
            data = safe_json(response)
            created += len(data.get("records", []))
            if len(data.get("records", [])) != len(batch):
                raise ValueError("Airtable returned fewer records than submitted")
        except Exception as exc:
            logger.error("Airtable batch create failed: %s", exc)
            failed += len(batch)
            failed_lead_keys.extend(job["lead_key"] for job in batch)
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)

    for index in range(0, len(to_update), effective_batch_size):
        batch = to_update[index:index + effective_batch_size]
        body = {
            "records": [{"id": item["id"], "fields": item["fields"]} for item in batch],
            "typecast": True,
        }
        try:
            response = request_with_retry("PATCH", _base_url(), headers=_headers(), json_body=body)
            data = safe_json(response)
            updated_count = len(data.get("records", []))
            if updated_count != len(batch):
                raise ValueError("Airtable returned fewer updated records than submitted")
            updated_missing_role_focus += sum(1 for item in batch if item["updated_role_focus"])
            updated_missing_job_signals += sum(1 for item in batch if item["updated_job_signal"])
        except Exception as exc:
            logger.error("Airtable generated-field repair failed: %s", exc)
            failed += len(batch)
            failed_lead_keys.extend(item["lead_key"] for item in batch)
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)

    skipped_existing = len(existing_keys) - len(to_update)
    skipped_existing_company = len(suppressed_company_keys)
    skipped_no_contact = len(jobs) - len(reviewable)
    signal_review_required = sum(
        bool(job.get("job_signal_review_required")) for job in unique_by_key.values()
    )
    return {
        "created": created,
        "updated_missing_role_focus": updated_missing_role_focus,
        "updated_missing_job_signals": updated_missing_job_signals,
        "skipped_existing": skipped_existing,
        "skipped_existing_company": skipped_existing_company,
        "suppressed_company_lead_keys": suppressed_company_keys,
        "failed": failed,
        "failed_lead_keys": failed_lead_keys,
        "skipped_no_contact": skipped_no_contact,
        "reviewable": len(unique_by_key),
        "final_pass": sum(job.get("_final_state") == "FINAL_PASS" for job in unique_by_key.values()),
        "needs_check": sum(job.get("_final_state") == "NEEDS_CHECK" for job in unique_by_key.values()),
        "strict_mode": strict_mode,
        "job_signal_review_required": signal_review_required,
    }


def repair_missing_role_focus(batch_size: int = 10) -> Dict:
    """Backfill only blank Role Focus fields on existing Airtable records."""
    from role_focus import extract_role_focus

    validate_preflight()
    existing = _get_existing_leads()
    repairs: List[Dict] = []
    skipped_no_mapping = 0

    for lead_key, record in existing.items():
        fields = record.get("fields") or {}
        if fields.get("Role Focus"):
            continue
        matched_role = fields.get("Matched Role") or ""
        result = extract_role_focus(
            {"job_title": fields.get("Open Role") or "", "job_description": ""},
            matched_role,
        )
        if not result.text:
            skipped_no_mapping += 1
            continue
        repairs.append({
            "id": record.get("id"),
            "lead_key": lead_key,
            "fields": {
                "Role Focus": result.text,
                "Focus Quality": result.quality,
                "Focus Evidence": " | ".join(result.evidence),
            },
        })

    updated = 0
    failed = 0
    failed_lead_keys: List[str] = []
    effective_batch_size = min(batch_size, 10)
    for index in range(0, len(repairs), effective_batch_size):
        batch = repairs[index:index + effective_batch_size]
        body = {
            "records": [{"id": item["id"], "fields": item["fields"]} for item in batch],
            "typecast": True,
        }
        try:
            response = request_with_retry("PATCH", _base_url(), headers=_headers(), json_body=body)
            data = safe_json(response)
            updated += len(data.get("records", []))
            if len(data.get("records", [])) != len(batch):
                raise ValueError("Airtable returned fewer repaired records than submitted")
        except Exception as exc:
            logger.error("Airtable Role Focus backfill failed: %s", exc)
            failed += len(batch)
            failed_lead_keys.extend(item["lead_key"] for item in batch)
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)

    return {
        "missing_role_focus_found": len(repairs),
        "updated": updated,
        "skipped_no_mapping": skipped_no_mapping,
        "failed": failed,
        "failed_lead_keys": failed_lead_keys,
    }


def repair_missing_job_signals(batch_size: int = 10) -> Dict:
    """Backfill blank freshness/URL-quality fields on existing Airtable rows.

    Reviewer-edited values are preserved. The Job URL itself is replaced only
    when Job URL Status is blank and a better candidate is available.
    """
    validate_preflight()
    existing = _get_existing_leads()
    repairs: List[Dict] = []

    for lead_key, record in existing.items():
        fields = record.get("fields") or {}
        needs_freshness = not fields.get("Job Freshness")
        needs_url = not fields.get("Job URL Status")
        if not needs_freshness and not needs_url:
            continue

        pseudo_job = {
            "job_posted_at_datetime_utc": fields.get("Posted At"),
            "job_apply_link": fields.get("Job URL"),
            "employer_website": fields.get("Website"),
        }
        assessed = annotate_job(pseudo_job, probe_url=True)
        patch_fields: Dict = {}
        if needs_freshness:
            patch_fields.update({
                "Job Freshness": assessed.get("job_freshness"),
                "Job Age Days": assessed.get("job_age_days"),
                "Job Signal Notes": assessed.get("job_signal_notes"),
            })
        if needs_url:
            patch_fields.update({
                "Job URL": assessed.get("job_url_selected") or fields.get("Job URL"),
                "Job URL Status": assessed.get("job_url_status"),
                "Job URL Source": assessed.get("job_url_source"),
                "Job Signal Notes": assessed.get("job_signal_notes"),
            })

        repairs.append({
            "id": record.get("id"),
            "lead_key": lead_key,
            "fields": _clean_fields(patch_fields),
        })

    updated = 0
    failed = 0
    failed_lead_keys: List[str] = []
    effective_batch_size = min(batch_size, 10)
    for index in range(0, len(repairs), effective_batch_size):
        batch = repairs[index:index + effective_batch_size]
        body = {
            "records": [{"id": item["id"], "fields": item["fields"]} for item in batch],
            "typecast": True,
        }
        try:
            response = request_with_retry("PATCH", _base_url(), headers=_headers(), json_body=body)
            data = safe_json(response)
            updated += len(data.get("records", []))
            if len(data.get("records", [])) != len(batch):
                raise ValueError("Airtable returned fewer repaired records than submitted")
        except Exception as exc:
            logger.error("Airtable job-signal backfill failed: %s", exc)
            failed += len(batch)
            failed_lead_keys.extend(item["lead_key"] for item in batch)
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)

    return {
        "missing_job_signals_found": len(repairs),
        "updated": updated,
        "failed": failed,
        "failed_lead_keys": failed_lead_keys,
    }


def get_approved_leads() -> List[Dict]:
    validate_preflight()
    records: List[Dict] = []
    offset = None
    while True:
        params: List[tuple[str, str | int]] = [
            ("filterByFormula", f"{{Status}} = '{config.AIRTABLE_STATUS_APPROVED}'"),
            ("pageSize", 100),
        ]
        if offset:
            params.append(("offset", offset))
        response = request_with_retry("GET", _base_url(), headers=_headers(), params=params)
        data = safe_json(response)
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)

    if config.FINAL_PASS_PIPELINE_ENABLED:
        safe_records = [
            record for record in records
            if str((record.get("fields") or {}).get("Final Decision") or "").strip() == "FINAL_PASS"
            and str((record.get("fields") or {}).get("Validation Version") or "").strip()
        ]
        skipped = len(records) - len(safe_records)
        if skipped:
            logger.error(
                "Blocked %d Approved Airtable row(s) without a validated FINAL_PASS decision",
                skipped,
            )
        return safe_records
    return records


def mark_status(record_ids: Iterable[str], status: str, error: str = "") -> None:
    ids = list(record_ids)
    for index in range(0, len(ids), 10):
        batch = ids[index:index + 10]
        records = []
        for record_id in batch:
            fields = {"Status": status}
            if error:
                fields["Error"] = error[:1000]
            elif status == config.AIRTABLE_STATUS_ENROLLED:
                fields["Error"] = ""
            records.append({"id": record_id, "fields": fields})
        request_with_retry(
            "PATCH",
            _base_url(),
            headers=_headers(),
            json_body={"records": records, "typecast": True},
        )
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)


def mark_enrolled(record_ids: Iterable[str]) -> None:
    mark_status(record_ids, config.AIRTABLE_STATUS_ENROLLED)


def mark_error(record_ids: Iterable[str], error: str) -> None:
    mark_status(record_ids, config.AIRTABLE_STATUS_ERROR, error=error)
