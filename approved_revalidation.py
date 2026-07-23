"""Fail-closed revalidation immediately before Instantly enrollment."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

import config
import apollo_client as apollo
import hunter_client as hunter
from account_gate import AccountGate
from contact_gate import ContactGate
from decision_types import GateState
from domain_utils import normalize_company_domain
from email_gate import EmailGate
from job_source_resolver import JobSourceResolver
from validation_integrity import fingerprint_matches


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def revalidate_approved_record(record: Dict) -> Tuple[bool, str]:
    fields = record.get("fields") or {}
    if not fingerprint_matches(fields):
        return False, "Validation fingerprint mismatch; critical Airtable fields changed"
    validated_at = _parse(str(fields.get("Validated At") or ""))
    if not validated_at:
        return False, "Validated At is missing or invalid"
    if validated_at < datetime.now(timezone.utc) - timedelta(
        hours=max(1, config.APPROVED_REVALIDATION_MAX_AGE_HOURS)
    ):
        return False, "Validation is stale; rerun qualification before enrollment"

    company = str(fields.get("Company") or "")
    website = str(fields.get("Website") or "")
    domain = normalize_company_domain(website)
    role = str(fields.get("Open Role") or "")
    job_url = str(fields.get("Job URL") or fields.get("Official Source") or "")
    job = {
        "job_id": fields.get("Job ID"),
        "job_title": role,
        "employer_name": company,
        "employer_website": website,
        "official_job_url": job_url,
        "job_apply_link": job_url,
        "job_location": fields.get("Location"),
        "job_employment_type": fields.get("Employment Type"),
    }

    if config.APPROVED_REVALIDATE_JOB_SOURCE:
        source = JobSourceResolver().resolve(job, fetch=True)
        trusted_active = bool(
            source.state == "ACTIVE_VERIFIED" and (source.official or source.corroborated)
        ) or bool(
            source.state == "ACTIVE_CORROBORATED" and source.corroborated
        )
        if not trusted_active:
            return False, f"Job source revalidation failed: {source.state}"

    org = apollo.enrich_organization(domain=domain, name=company, website=website)
    account = AccountGate().evaluate(
        org=org,
        input_company_name=company,
        input_domain=domain,
        jobs=[job],
        fetch_company=True,
    )
    if account.state_value != GateState.PASS.value:
        return False, f"Account revalidation failed: {account.primary_reason}"

    person_id = str(fields.get("Apollo Person ID") or "")
    if not person_id:
        return False, "Apollo Person ID is missing; current employment cannot be revalidated"
    person = apollo.match_person({"id": person_id})
    stored_email = str(fields.get("Email") or "").strip().lower()
    current_email = str(person.email or "").strip().lower()
    if current_email and current_email != stored_email:
        return False, "Apollo now returns a different email for the selected contact"

    contact = ContactGate().evaluate(
        person=person,
        target_titles=[str(fields.get("HM Title") or "")],
        company_domains={domain},
        company_name=company,
        intent_market="us_market",
        founder_allowed=True,
    )
    if contact.state_value != GateState.PASS.value:
        return False, f"Contact revalidation failed: {contact.primary_reason}"

    hunter_result = None
    if config.VERIFY_WITH_HUNTER and config.HUNTER_API_KEY:
        hunter_result = hunter.verify_email(stored_email)
    if not current_email:
        if hunter_result is None:
            return False, "Current Apollo record no longer exposes the approved email"
        person = replace(
            person,
            email=stored_email,
            email_found=True,
            email_status=None,
            email_source="airtable_revalidation",
        )
    email = EmailGate().evaluate(
        person=person,
        hunter_result=hunter_result,
        company_domains={domain},
    )
    if email.state_value != GateState.PASS.value:
        return False, f"Email revalidation failed: {email.primary_reason}"
    return True, "approved_record_revalidated"
