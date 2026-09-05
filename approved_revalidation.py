"""LEGACY. Not on the Approved Sync delivery path, and must not be put back.

This module re-ran the full qualification pipeline -- Apollo organization
enrichment, Apollo person match, Hunter, JobSourceResolver -- immediately before
Instantly enrollment, behind a 24-hour validation-age gate.

On 2026-08-12 that gate marked 627 Approved rows ``Error`` with "Validation is
stale; rerun qualification before enrollment". Every record failed it at line 41
below, BEFORE any provider call, so nothing enrolled and nothing reached Instantly.

Approved is now the authorization boundary: ``run_approved`` DELIVERS, it does not
re-qualify. It performs one local, zero-network readiness check
(``_delivery_precheck``) and nothing else. Nothing in production imports this
module; ``config.APPROVED_SYNC_REVALIDATE_PROVIDERS`` is a deprecated name that
selects nothing, and ``run_approved.run(revalidate_providers=True)`` is ignored
with a warning.

It is kept because two test modules exercise the gate composition through it, and
because deleting the record of what the delivery path deliberately does NOT do
makes it easier to reintroduce. ``tests/test_approved_sync_delivery_only.py``
asserts the delivery path stays free of it.
"""

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
        if source.state == "INACTIVE_VERIFIED":
            return False, "Job source revalidation confirmed that the vacancy is inactive"

    org = apollo.enrich_organization(domain=domain, name=company, website=website)
    account = AccountGate().evaluate(
        org=org,
        input_company_name=company,
        input_domain=domain,
        jobs=[job],
        fetch_company=True,
    )
    if account.state_value == GateState.REJECT.value:
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
    if contact.state_value not in {GateState.PASS.value, GateState.NEEDS_CHECK.value}:
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
    if email.state_value not in {GateState.PASS.value, GateState.NEEDS_CHECK.value}:
        return False, f"Email revalidation failed: {email.primary_reason}"
    return True, "approved_record_revalidated"
