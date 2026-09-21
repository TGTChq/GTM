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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

from ..policy.campaigns import (CAMPAIGN_BY_FUNCTION, KNOWN_CONTROL_CAMPAIGN_IDS, POLICY_VERSION,
                               campaign_id_allowed, size_band)
from ..policy.compliance import (
    COMPLIANCE_RULE_VERSION, ENTITY_UNKNOWN, OPT_OUT_NONE, ComplianceRecord, evaluate,
)
from ..policy.requirements import rule
from .jurisdiction import observe_job_country, resolve_person_contact_country
from .facts import MIN_CORROBORATING_SIZE_SOURCES, resolve_company_size, size_reject_reason, size_sources_agreeing
from .identity import lead_key as make_lead_key
from .employer_attribution import employer_attribution_conflict

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
    outreach_controls: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ApprovedLead | ApprovalRefusal:
    """Every requirement of PRODUCT_CONTRACT §8, checked in order, with a named refusal.

    ``outreach_controls`` carries the deployment-level half of the country
    compliance decision (`tgtc-compliance/1`): the lawful basis and its evidence
    reference, whether a privacy-notice process is configured and within how
    many days it falls due, and whether the send path actually provides an
    unsubscribe mechanism and a suppression check. The jurisdiction half comes
    from the stored fields on the posting, employer and person and from nowhere
    else.

    The compliance verdict is NOT a refusal. "Fail closed for sending, open for
    capacity": a blocked record is approved, stored, categorised and counted --
    it simply carries ``outreach_eligible = False`` and a named
    ``outreach_block_reason``, and ``_commit_approval`` creates its outbox items
    already blocked. Refusing here instead would delete exactly the records the
    matrix says to retain.
    """
    moment = now or datetime.now(timezone.utc)
    if suppression_hits:
        return ApprovalRefusal("suppressed", {"hits": list(suppression_hits)})
    campaign = CAMPAIGN_BY_FUNCTION.get(function_key)
    if not campaign:
        return ApprovalRefusal("unknown_function", {"function_key": function_key})
    if not campaign_id:
        return ApprovalRefusal("no_campaign_configured", {"function_key": function_key})
    if not campaign_id_allowed(campaign_id, allowed_campaign_ids, env):
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
    conflict = employer_attribution_conflict(posting.get("description_text"),
                                            employer_name=employer_name, employer_domain=employer_domain)
    if conflict:
        return ApprovalRefusal("employer_attribution_conflict", conflict)
    # Company size (Decision 2, 2026-09-19; wired live, task 5c 2026-09-20): the
    # SAME resolve_company_size predicate services/opportunity.py's own size
    # gate calls -- a stored headcount alone can no longer mask a conflict
    # against the employer's declared LinkedIn size band. This is the final
    # approval-time check, downstream of opportunity.py's own gate; a genuine
    # firmographic conflict/unknown was already let through there (never
    # discarded merely because two sources conflict) and is let through here
    # too -- it is never a reject, only out_of_range is.
    #
    # Fix round 1, I1 (IMPORTANT, independent review): min_employees/
    # max_employees are read from rule(), not facts.py's own hardcoded
    # defaults -- otherwise this gate silently forks from the policy manifest
    # describe() reports.
    min_employees, max_employees = int(rule("min_employees")), int(rule("max_employees"))
    count = employer.get("employee_count")
    company_size_state, _size_excerpt, effective = resolve_company_size(
        count, employer.get("size_band"), description=str(posting.get("description_text") or ""),
        min_employees=min_employees, max_employees=max_employees)
    # Final whole-branch review, C3 (CRITICAL): how many sources back that
    # verdict travels with the lead beside the verdict itself. An in_range read
    # from ONE source is still not a reject (below) -- it is simply not
    # CONFIRMED, and must not be counted or asserted as if it were.
    company_size_sources = size_sources_agreeing(count, employer.get("size_band"),
                                                 min_employees=min_employees, max_employees=max_employees)
    if company_size_state == "out_of_range":
        return ApprovalRefusal(size_reject_reason(effective, min_employees=min_employees),
                               {"employee_count": count, "size_band": employer.get("size_band")})
    if company_size_state == "unknown_firmographics" and rule("require_employee_count"):
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

    compliance = compliance_decision(posting=posting, employer=employer, person=person,
                                     email=email, moment=moment, controls=outreach_controls)
    lk = make_lead_key(employer_domain, email, function_key)
    lead: Dict[str, Any] = {
        "lead_key": lk,
        **compliance,
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
        # Fix round 1, C1 (CRITICAL, independent review): the resolved
        # company-size state travels WITH the lead from here on -- into the
        # approvals row (_commit_approval), the KPI split (metrics.ledger),
        # and the delivery payloads below, all reading this ONE field rather
        # than each re-deciding or silently assuming "in_range". Only
        # "in_range" means both reliable sources agree the company is
        # confirmed 25-1,000; "firmographic_conflict"/"unknown_firmographics"
        # reached here BECAUSE they are not a reject (Decision 2), not
        # because they are confirmed.
        "company_size_state": company_size_state,
        #: C3: the corroboration count behind that state (0-2). "in_range" on
        #: ONE source is the shape of nearly every live row (migration 007
        #: backfills no size_band) and is NOT confirmation; ``size_confirmed``
        #: below is the one place that judgement is made.
        "company_size_sources": company_size_sources,
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
# Country compliance (`tgtc-compliance/1`)
# ---------------------------------------------------------------------------

#: The twelve fields COMPLIANCE_MATRIX.md names, in the order migration 011
#: stores them. Written once so the lead, the approvals row and the ledger all
#: agree on what "the compliance fields" means.
COMPLIANCE_LEAD_FIELDS: Sequence[str] = (
    "job_country", "company_country", "contact_country", "employer_legal_entity_type",
    "corporate_subscriber_status", "compliance_rule_version", "legal_basis", "legal_basis_evidence",
    "privacy_notice_due_at", "opt_out_status", "outreach_eligible", "outreach_block_reason",
)


def compliance_decision(*, posting: Mapping[str, Any], employer: Mapping[str, Any], person: Mapping[str, Any],
                        email: str, moment: datetime,
                        controls: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The twelve stored compliance fields for one lead.

    Each of the three countries comes from ITS OWN subject and from nothing
    else: the job's from the posting's provider country list, the company's from
    the employer row, the contact's from the person row. There is no fallback
    between them, which is what makes "a job's location does not determine the
    contact's jurisdiction" a property of the code rather than a comment.

    ``opt_out_status``: the person's stored value when one exists. Otherwise
    ``none``, and that is a fact rather than an assumption -- this function is
    unreachable unless the caller's suppression check came back clean, since
    ``build_approved_lead`` refuses on any suppression hit before reaching here.
    """
    controls = dict(controls or {})
    privacy_configured = bool(controls.get("privacy_notice_configured"))
    due_days = int(controls.get("privacy_notice_days") or 30)
    privacy_due_at = moment + timedelta(days=due_days) if privacy_configured else None
    # The person's own country: the column, else the person's own stored
    # Apollo evidence. Never the employer's location. See
    # jurisdiction.resolve_person_contact_country for the measured need.
    contact_country, contact_country_provenance = resolve_person_contact_country(person)
    record = ComplianceRecord(
        job_country=observe_job_country(posting.get("countries")),
        company_country=str(employer.get("company_country") or ""),
        contact_country=contact_country,
        employer_legal_entity_type=str(employer.get("employer_legal_entity_type") or ""),
        corporate_subscriber_status=str(employer.get("corporate_subscriber_status") or ENTITY_UNKNOWN),
        legal_basis=str(controls.get("legal_basis") or ""),
        legal_basis_evidence=str(controls.get("legal_basis_evidence") or ""),
        privacy_notice_configured=privacy_configured,
        privacy_notice_due_at=privacy_due_at,
        opt_out_status=str(person.get("opt_out_status") or OPT_OUT_NONE),
        email=email,
        email_alignment=str(person.get("email_alignment") or ""),
        email_status=str(person.get("email_status") or ""),
        unsubscribe_available=bool(controls.get("unsubscribe_available")),
        suppression_available=bool(controls.get("suppression_available")),
    )
    decision = evaluate(record)
    return {
        "job_country": record.job_country,
        "company_country": record.company_country,
        "contact_country": record.contact_country,
        "contact_country_provenance": contact_country_provenance,
        "employer_legal_entity_type": record.employer_legal_entity_type,
        "corporate_subscriber_status": record.corporate_subscriber_status,
        "compliance_rule_version": COMPLIANCE_RULE_VERSION,
        "legal_basis": record.legal_basis,
        "legal_basis_evidence": record.legal_basis_evidence,
        # ISO text, not a datetime: the lead travels through ``jsonb`` into
        # approvals.lead_json and both delivery payloads, and json.dumps has no
        # datetime encoder. PostgreSQL casts the text into the timestamptz
        # column; ``None`` (no configured privacy-notice process) stays NULL.
        "privacy_notice_due_at": _iso(privacy_due_at) if privacy_due_at else None,
        "opt_out_status": record.opt_out_status,
        "outreach_eligible": decision.outreach_eligible,
        "outreach_block_reason": decision.outreach_block_reason,
        "compliance_capacity_category": decision.capacity_category,
        "compliance_gates": {k: v.as_dict() for k, v in decision.gates.items()},
    }


def outreach_blocked_reason(row: Mapping[str, Any]) -> str:
    """``""`` when this approved lead may be sent to; the named block reason when
    it may not.

    ONE definition, asked by both call sites that must agree: the approval
    writer, which decides whether an outbox item is created ``pending`` or
    already ``blocked``, and the delivery pre-check, which refuses to send.
    Copying it would let the two drift, and the direction of the drift that
    matters is the one where a record blocked at approval becomes sendable at
    delivery.

    Anything other than an explicit ``True`` blocks -- including ``None``, which
    is what a lead approved before these columns existed carries. Unknown is
    never "yes".
    """
    if row.get("outreach_eligible") is True:
        return ""
    return str(row.get("outreach_block_reason") or "") or "compliance:outreach_eligibility_unknown"


# ---------------------------------------------------------------------------
# Delivery payloads
# ---------------------------------------------------------------------------

def size_confirmed(lead: Dict[str, Any]) -> bool:
    """May this lead's company size be asserted as fact?

    Final whole-branch review, C3 (CRITICAL): ``airtable_fields`` and
    ``instantly_payload`` each carried their own ``lead.get("company_size_state")
    == "in_range"`` line -- two copies of one judgement, and both of them wrong
    for a single-source read. The judgement lives here once: the range verdict
    must be ``in_range`` AND at least ``MIN_CORROBORATING_SIZE_SOURCES``
    sources must have backed it (``domain.facts.size_sources_agreeing``,
    recorded on the lead at approval time). A lead written before that field
    existed carries no count and is treated as unconfirmed, never as confirmed.
    """
    return (lead.get("company_size_state") == "in_range"
            and int(lead.get("company_size_sources") or 0) >= MIN_CORROBORATING_SIZE_SOURCES)


def airtable_fields(lead: Dict[str, Any], fp: str) -> Dict[str, Any]:
    """The production field names. ``Status`` is ALWAYS Approved; ``Validation Version``
    is the core's policy version so the legacy Approved Sync skips these rows.

    Fix round 1, C1 (CRITICAL, independent review): ``Employees``/``Size Band``
    are both DERIVED FROM ``employee_count``, the field Decision 2 (2026-09-19)
    can leave in dispute (``firmographic_conflict``) -- shipping them
    unconditionally asserted a confident size label CRM/ops readers (and,
    for the Instantly twin below, an outbound email TEMPLATE) would trust as
    fact even when it is not one. Both are omitted (never a wrong or
    unverified number) unless ``size_confirmed(lead)``.

    Final whole-branch review, I1 (IMPORTANT, 2026-09-20): omitting the two
    size fields was not enough. The row still asserted ``"Firmographics
    Status": "PASS"`` unconditionally, so a firmographic_conflict lead landed
    in the CRM claiming firmographics passed, with the size merely ABSENT and
    no field anywhere naming ``company_size_state`` -- the review bucket
    existed in the database and in ``ledger()``, but not in the artifact a
    human reads. The gate field now says NEEDS_CHECK (the established legacy
    vocabulary: ``airtable_client.py``'s send-safety accepts PASS and
    NEEDS_CHECK and blocks only an explicit REJECT, so the row stays
    reviewable and deliverable), and the state and its corroboration count
    travel in two fields this producer already writes -- no new Airtable
    field name is invented, which an unprepared base would reject.
    """
    website = f"https://{lead['employer_domain']}"
    confirmed = size_confirmed(lead)
    count = lead.get("employee_count") if confirmed else None
    company_size_state = str(lead.get("company_size_state") or "unknown_firmographics")
    company_size_sources = int(lead.get("company_size_sources") or 0)
    evidence = {
        "producer": "tgtc_core",
        "policy_version": lead["policy_version"],
        "classification_method": lead.get("classification_method"),
        "responsibilities": lead.get("responsibilities"),
        "responsibility_excerpts": lead.get("responsibility_excerpts"),
        "email_alignment": lead.get("email_alignment"),
        "posting_first_seen_at": lead.get("posting_first_seen_at"),
        "company_size_state": company_size_state,
        "company_size_sources": company_size_sources,
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
        "Job Signal Notes": f"producer=tgtc_core | company_size_state={company_size_state} "
                            f"({company_size_sources} corroborating source(s))",
        "Hiring Manager": f"{lead['first_name']} {lead['last_name']}",
        "HM Title": lead["buyer_title"],
        "LinkedIn": lead.get("linkedin_url") or None,
        "Apollo Person ID": lead.get("apollo_person_id") or None,
        "Email": lead["email"],
        "Email Source": "apollo",
        "Apollo Email Status": "verified",
        "Employees": count,
        "Size Band": size_band(count) if confirmed else None,
        "Industry": lead.get("industry") or None,
        "Campaign ID": lead["campaign_id"],
        "Job ID": f"{lead.get('posting_source')}:{lead.get('posting_provider_id')}",
        "Final Decision": "FINAL_PASS",
        "Decision Reason": "APPROVED_BY_TGTC_CORE",
        "Evidence Status": "PASS",
        "Firmographics Status": "PASS" if confirmed else "NEEDS_CHECK",
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
    """Control-A payload shape with the exact custom-variable names production sends.

    Fix round 1, C1 (CRITICAL, independent review): ``company_size``/
    ``company_size_band`` are custom variables an outbound email TEMPLATE can
    interpolate directly into copy a real recipient reads -- omitted (never a
    disputed or unverified number) unless the size is actually confirmed
    (see ``airtable_fields``'s own docstring for the full rationale, shared
    here via the same ``lead["company_size_state"]`` field).
    """
    confirmed = size_confirmed(lead)
    count = lead.get("employee_count") if confirmed else None
    variables: Dict[str, Any] = {
        "open_role": lead["open_role"],
        "open_roles": lead["open_role"],
        "role_focus": lead["role_focus"],
        "matched_role": lead["open_role"],
        "role_bucket": lead["function_key"],
        "company_size": count,
        "company_size_band": size_band(count) if confirmed else None,
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
