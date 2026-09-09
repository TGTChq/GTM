"""Deterministic approval over complete facts, and the two delivery payloads.

An Approved lead is the ONLY commercial output. Anything that cannot satisfy every
requirement returns an ``ApprovalRefusal`` with a reason; nothing here can emit a
review class.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..policy.campaigns import (CAMPAIGN_BY_FUNCTION, KNOWN_CONTROL_CAMPAIGN_IDS, POLICY_VERSION, size_band)
from ..policy.requirements import rule
from .identity import lead_key as make_lead_key

VALIDATION_VERSION = POLICY_VERSION  # deliberately != legacy config.VALIDATION_VERSION
APPROVED_STATUS = "Approved"

#: Fields covered by the fingerprint (identity + destination + contact + evidence).
SIGNED_FIELDS = (
    "lead_key", "employer_name", "employer_domain", "function_key", "campaign_key", "campaign_id",
    "first_name", "last_name", "buyer_title", "linkedin_url", "apollo_person_id", "email",
    "email_status", "open_role", "role_focus", "posting_url", "posting_id", "policy_version",
)

_CUSTOM_VARIABLE_NAMES = (
    "open_role", "open_roles", "role_focus", "matched_role", "role_bucket", "company_size",
    "company_size_band", "job_posted_at", "job_source", "job_url", "job_freshness", "job_age_days",
    "job_url_status", "job_url_source", "relevance",
)


@dataclass
class ApprovalRefusal:
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovedLead:
    lead: Dict[str, Any]
    lead_key: str
    fingerprint: str


def _clean(text: Optional[str]) -> str:
    return " ".join(str(text or "").split())


def display_role(title: Optional[str], function_key: str) -> str:
    """Conversational role noun for copy. The title is used when it is display-safe;
    otherwise the function noun. Never empty for a known function."""
    t = _clean(title)
    t = re.sub(r"\s*[\(\[\-–—|/]\s*(?:remote|hybrid|on-?site|us|usa|united states|[a-z ]*,\s*[A-Z]{2}).*$", "", t, flags=re.I)
    t = re.sub(r"\b(?:remote|hybrid|work from home|wfh)\b", "", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—|/,")
    if t and len(t) <= 60 and not re.search(r"[|/{}<>@#]|\d{4,}|\bhiring\b|\burgent\b|\$", t) and len(t.split()) <= 7:
        return t
    campaign = CAMPAIGN_BY_FUNCTION.get(function_key)
    if campaign:
        return campaign.function_nouns.get(function_key, f"{function_key.replace('_', ' ')} role")
    return ""


def role_focus_text(phrases: Sequence[str]) -> str:
    items = [p.strip().rstrip(".") for p in phrases if p and p.strip()][:3]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{items[0]}, {items[1]}, and {items[2]}"


def fingerprint(lead: Dict[str, Any], signing_key: str) -> str:
    payload = {k: lead.get(k) for k in SIGNED_FIELDS}
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    key = signing_key or "offline-test-key"
    return hmac.new(key.encode(), serialized.encode(), hashlib.sha256).hexdigest()


def build_approved_lead(
    *,
    posting: Dict[str, Any],
    classification: Dict[str, Any],
    employer: Dict[str, Any],
    person: Dict[str, Any],
    function_key: str,
    campaign_id: str,
    allowed_campaign_ids: Sequence[str],
    signing_key: str,
    now: Optional[datetime] = None,
    suppression_hits: Sequence[str] = (),
) -> ApprovedLead | ApprovalRefusal:
    """Every requirement of PRODUCT_CONTRACT §8, checked in order, with a named refusal."""
    moment = now or datetime.now(timezone.utc)
    if suppression_hits:
        return ApprovalRefusal("suppressed", {"hits": list(suppression_hits)})
    campaign = CAMPAIGN_BY_FUNCTION.get(function_key)
    if not campaign:
        return ApprovalRefusal("unknown_function", {"function_key": function_key})
    if not campaign_id:
        return ApprovalRefusal("no_campaign_configured", {"function_key": function_key})
    if campaign_id not in set(allowed_campaign_ids) and campaign_id not in KNOWN_CONTROL_CAMPAIGN_IDS:
        return ApprovalRefusal("campaign_id_not_allowed", {"campaign_id": campaign_id})
    if not signing_key:
        return ApprovalRefusal("signing_key_missing")

    # posting evidence
    if not posting.get("id"):
        return ApprovalRefusal("posting_identity_missing")
    if str(posting.get("state") or "") in {"closed", "expired"}:
        return ApprovalRefusal("posting_not_active", {"state": posting.get("state")})
    valid_through = posting.get("date_valid_through")
    if isinstance(valid_through, datetime) and valid_through < moment:
        return ApprovalRefusal("posting_expired", {"date_valid_through": _iso(valid_through)})
    if classification.get("excluded"):
        return ApprovalRefusal("posting_excluded", {"reason": classification.get("exclusion_reason")})
    if function_key not in (classification.get("compatible_functions") or []):
        return ApprovalRefusal("function_not_compatible", {"function_key": function_key})
    responsibilities = [r.get("phrase") for r in (classification.get("responsibilities") or []) if r.get("phrase")]
    if not responsibilities:
        return ApprovalRefusal("no_responsibility_evidence")
    anchor = posting.get("commercial_age_anchor")
    if isinstance(anchor, datetime):
        age_days = (moment - anchor).days
        if age_days > int(rule("approval_max_age_days")):
            return ApprovalRefusal("posting_too_old", {"age_days": age_days})
    else:
        age_days = None

    # employer
    employer_name = _clean(employer.get("canonical_name"))
    employer_domain = _clean(employer.get("domain")).lower()
    if not employer_name or not employer_domain:
        return ApprovalRefusal("employer_identity_incomplete", {"name": employer_name, "domain": employer_domain})
    count = employer.get("employee_count")
    if count is not None:
        if count < int(rule("min_employees")):
            return ApprovalRefusal("employer_too_small", {"employee_count": count})
        if count > int(rule("max_employees")):
            return ApprovalRefusal("employer_too_large", {"employee_count": count})
    elif rule("require_employee_count"):
        return ApprovalRefusal("insufficient_evidence:employee_count")
    if employer.get("excluded_industry"):
        return ApprovalRefusal("employer_excluded_industry", {"industry": employer.get("industry")})
    if employer.get("agency_flag") is True:
        return ApprovalRefusal("employer_is_agency")

    # person
    first, last = _clean(person.get("first_name")), _clean(person.get("last_name"))
    if not first or not last:
        return ApprovalRefusal("person_name_incomplete")
    if not _clean(person.get("title")):
        return ApprovalRefusal("person_title_missing")
    if rule("require_contact_linkedin") and not _clean(person.get("linkedin_url")):
        return ApprovalRefusal("person_linkedin_missing")
    if not person.get("contact_gate_passed"):
        return ApprovalRefusal("contact_gate_not_passed", {"reason": person.get("contact_gate_reason")})
    email = _clean(person.get("email")).lower()
    if not email or str(person.get("email_status") or "").lower() != "verified" or not person.get("email_gate_passed"):
        return ApprovalRefusal("email_not_verified", {"status": person.get("email_status")})

    open_role = display_role(posting.get("title"), function_key)
    focus = role_focus_text(responsibilities)
    if not open_role or not focus:
        return ApprovalRefusal("copy_fields_incomplete", {"open_role": open_role, "role_focus": focus})

    lk = make_lead_key(employer_domain, email, function_key)
    lead: Dict[str, Any] = {
        "lead_key": lk,
        "posting_id": posting.get("id"),
        "posting_source": posting.get("source"),
        "posting_provider_id": posting.get("provider_job_id"),
        "posting_url": posting.get("url") or "",
        "posting_title": posting.get("title") or "",
        "posting_date_posted": _iso(posting.get("date_posted")),
        "posting_first_seen_at": _iso(posting.get("first_seen_at")),
        "posting_age_days": age_days,
        "posting_valid_through": _iso(valid_through) if isinstance(valid_through, datetime) else "",
        "posting_content_hash": posting.get("content_hash") or "",
        "employer_id": employer.get("id"),
        "employer_name": employer_name,
        "employer_domain": employer_domain,
        "employer_linkedin_slug": employer.get("linkedin_slug") or "",
        "employee_count": count,
        "industry": employer.get("industry") or "",
        "function_key": function_key,
        "campaign_key": campaign.key,
        "campaign_name": campaign.name,
        "campaign_id": campaign_id,
        "person_id": person.get("id"),
        "apollo_person_id": person.get("apollo_person_id") or "",
        "first_name": first,
        "last_name": last,
        "buyer_title": _clean(person.get("title")),
        "linkedin_url": _clean(person.get("linkedin_url")),
        "email": email,
        "email_status": "verified",
        "email_authority": "apollo",
        "email_alignment": person.get("email_alignment") or "",
        "open_role": open_role,
        "role_focus": focus,
        "responsibilities": responsibilities[:3],
        "responsibility_excerpts": [r.get("excerpt", "")[:300] for r in (classification.get("responsibilities") or [])][:3],
        "classification_method": classification.get("method"),
        "policy_version": POLICY_VERSION,
        "approved_at": moment.isoformat(),
        "status": APPROVED_STATUS,
    }
    return ApprovedLead(lead=lead, lead_key=lk, fingerprint=fingerprint(lead, signing_key))


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value or "")


# ---------------------------------------------------------------------------
# Delivery payloads
# ---------------------------------------------------------------------------

def airtable_fields(lead: Dict[str, Any], fp: str) -> Dict[str, Any]:
    """The production field names. ``Status`` is ALWAYS Approved; ``Validation Version``
    is the core's policy version so the legacy Approved Sync skips these rows."""
    website = f"https://{lead['employer_domain']}"
    count = lead.get("employee_count")
    evidence = {
        "producer": "tgtc_core",
        "policy_version": lead["policy_version"],
        "classification_method": lead.get("classification_method"),
        "responsibilities": lead.get("responsibilities"),
        "responsibility_excerpts": lead.get("responsibility_excerpts"),
        "email_alignment": lead.get("email_alignment"),
        "posting_first_seen_at": lead.get("posting_first_seen_at"),
    }
    fields = {
        "Lead Key": lead["lead_key"],
        "Company": lead["employer_name"],
        "Outbound Company": lead["employer_name"],
        "Outbound Company Confidence": "high",
        "Outbound Company Identity": f"domain:{lead['employer_domain']}",
        "Website": website,
        "Open Role": lead.get("posting_title") or lead["open_role"],
        "Outbound Role": lead["open_role"],
        "Outbound Role Confidence": "high",
        "Role Focus": lead["role_focus"],
        "Focus Quality": "specific",
        "Focus Evidence": " | ".join(x for x in (lead.get("responsibility_excerpts") or []) if x),
        "Role Bucket": lead["function_key"],
        "Job URL": lead.get("posting_url") or None,
        "Job Source": lead.get("posting_source"),
        "Posted At": lead.get("posting_date_posted") or None,
        "Job Age Days": lead.get("posting_age_days"),
        "Job URL Status": "provider_active",
        "Job URL Source": lead.get("posting_source"),
        "Job Signal Notes": "producer=tgtc_core",
        "Hiring Manager": f"{lead['first_name']} {lead['last_name']}",
        "HM Title": lead["buyer_title"],
        "LinkedIn": lead.get("linkedin_url") or None,
        "Apollo Person ID": lead.get("apollo_person_id") or None,
        "Email": lead["email"],
        "Email Source": "apollo",
        "Apollo Email Status": "verified",
        "Employees": count,
        "Size Band": size_band(count),
        "Industry": lead.get("industry") or None,
        "Campaign ID": lead["campaign_id"],
        "Job ID": f"{lead.get('posting_source')}:{lead.get('posting_provider_id')}",
        "Final Decision": "FINAL_PASS",
        "Decision Reason": "APPROVED_BY_TGTC_CORE",
        "Evidence Status": "PASS",
        "Firmographics Status": "PASS",
        "Contact Alignment": "PASS",
        "Email Validation": "PASS",
        "Validation Version": VALIDATION_VERSION,
        "Validated At": lead["approved_at"],
        "Validation Fingerprint": fp,
        "Evidence Bundle": json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))[:95000],
        "Status": APPROVED_STATUS,
    }
    return {k: v for k, v in fields.items() if v not in (None, "", [])}


def instantly_payload(lead: Dict[str, Any], *, skip_if_in_workspace: bool, verify_on_import: bool) -> Dict[str, Any]:
    """Control-A payload shape with the exact custom-variable names production sends."""
    count = lead.get("employee_count")
    variables: Dict[str, Any] = {
        "open_role": lead["open_role"],
        "open_roles": lead["open_role"],
        "role_focus": lead["role_focus"],
        "matched_role": lead["open_role"],
        "role_bucket": lead["function_key"],
        "company_size": count,
        "company_size_band": size_band(count),
        "job_posted_at": lead.get("posting_date_posted"),
        "job_source": lead.get("posting_source"),
        "job_url": lead.get("posting_url"),
        "job_age_days": lead.get("posting_age_days"),
        "job_url_status": "provider_active",
        "job_url_source": lead.get("posting_source"),
        "relevance": "accept",
    }
    variables = {k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v))
                 for k, v in variables.items() if v not in (None, "")}
    assert set(variables) <= set(_CUSTOM_VARIABLE_NAMES)
    return {
        "campaign": lead["campaign_id"],
        "email": lead["email"],
        "first_name": lead["first_name"],
        "last_name": lead["last_name"],
        "company_name": lead["employer_name"],
        "job_title": lead["buyer_title"],
        "website": f"https://{lead['employer_domain']}",
        "skip_if_in_workspace": bool(skip_if_in_workspace),
        "skip_if_in_campaign": True,
        "verify_leads_on_import": bool(verify_on_import),
        "custom_variables": variables,
    }
