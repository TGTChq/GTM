"""Rebuild a blocked lead around a DIFFERENT hiring manager at the same employer.

A lead whose selected HM email Apollo will not verify is not recoverable by swapping
the address: ``Hiring Manager``, ``HM Title``, ``LinkedIn``, ``Apollo Person ID`` and
``Email`` are all signed, and ``Lead Key`` encodes ``domain|email|bucket``. The contact
IS the identity, so an alternate person is a new identity and every dependent field is
recomputed through the production gates -- never patched piecemeal.

Two measured facts shaped this module:

* Re-enriching the SAME person recovers nothing (0/10 measured): Apollo returns the
  same cached ``extrapolated`` record. Only a different person can help.
* A 10-credit alternate probe returned 7/10 Apollo-``verified`` emails, but 4 failed
  strict employer-domain equality -- their verified address lives on a different
  (brand/corporate) domain. So the binding constraint was the single-domain rule, not
  Apollo coverage. :func:`classify_alignment` replaces that naive rule with an
  evidence-based one that still refuses anything it cannot deterministically prove.

Everything here is pure: no network, no Airtable writes. Callers supply the Apollo
``PersonMatch`` and decide what to persist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Set

import config
from company_identity import (
    URL_SHORTENER_DOMAINS,
    company_names_exactly_equal,
    domain_name_consistent,
    email_domain,
    is_intermediary_domain,
    safe_company_domain,
)
from domain_utils import normalize_company_domain

RECOVERY_VERSION = "alternate-contact-recovery/1"

# --- alignment classes -------------------------------------------------------
EXACT_EMPLOYER_DOMAIN = "EXACT_EMPLOYER_DOMAIN"
CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN = "CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN"
PARENT_DOMAIN_AMBIGUOUS = "PARENT_DOMAIN_AMBIGUOUS"
BRAND_DOMAIN_AMBIGUOUS = "BRAND_DOMAIN_AMBIGUOUS"
UNRELATED_DOMAIN = "UNRELATED_DOMAIN"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

#: ONLY these two may pass automatically.
PASSING_ALIGNMENTS = (EXACT_EMPLOYER_DOMAIN, CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN)

# --- outcome classes ---------------------------------------------------------
VERIFIED_EXACT_DOMAIN = "VERIFIED_EXACT_DOMAIN"
VERIFIED_CORROBORATED_DOMAIN = "VERIFIED_CORROBORATED_DOMAIN"
VERIFIED_DOMAIN_AMBIGUOUS = "VERIFIED_DOMAIN_AMBIGUOUS"
VERIFIED_DOMAIN_MISMATCH = "VERIFIED_DOMAIN_MISMATCH"
EXTRAPOLATED = "EXTRAPOLATED"
NO_EMAIL = "NO_EMAIL"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
PERSON_EMPLOYER_DUPLICATE = "PERSON_EMPLOYER_DUPLICATE"
GATE_REJECTED = "GATE_REJECTED"
OTHER = "OTHER"

#: Hosts that can never be an employer identity. Shorteners come from the shared
#: company_identity list; social/ATS/job-board hosts are publishers, not employers.
NON_EMPLOYER_HOSTS = frozenset(URL_SHORTENER_DOMAINS) | frozenset({
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "tiktok.com", "glassdoor.com", "indeed.com", "ziprecruiter.com",
    "monster.com", "dice.com", "lever.co", "greenhouse.io", "workable.com",
    "smartrecruiters.com", "myworkdayjobs.com", "icims.com", "bamboohr.com",
    "jobvite.com", "ashbyhq.com", "gmail.com", "googlemail.com",
})


def _is_employer_domain(domain: str) -> bool:
    """A host that could plausibly BE an employer (not a publisher/redirector)."""
    normalized = normalize_company_domain(domain)
    if not normalized or normalized in NON_EMPLOYER_HOSTS:
        return False
    return not is_intermediary_domain(normalized, config.INTERMEDIARY_JOB_DOMAINS)


def build_trusted_domains(
    *,
    canonical_domain: str = "",
    apollo_org_domain: str = "",
    identity_key: str = "",
    extra: Iterable[str] = (),
) -> Set[str]:
    """Collect the employer domains supported by already-available evidence.

    Publishers, shorteners, ATS hosts and free-mail hosts are never included, so the
    trusted set can only ever contain plausible employer identities.
    """
    candidates = [canonical_domain, apollo_org_domain, *extra]
    key = str(identity_key or "")
    if key.startswith("domain:"):
        candidates.append(key.split(":", 1)[1])
    trusted = set()
    for value in candidates:
        normalized = safe_company_domain(value, config.INTERMEDIARY_JOB_DOMAINS)
        if normalized and _is_employer_domain(normalized):
            trusted.add(normalized)
    return trusted


@dataclass
class AlignmentResult:
    alignment: str
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def passes(self) -> bool:
        return self.alignment in PASSING_ALIGNMENTS


def classify_alignment(
    *,
    email: str,
    canonical_domain: str,
    canonical_company_name: str = "",
    apollo_org_domain: str = "",
    apollo_org_name: str = "",
    apollo_org_id: str = "",
) -> AlignmentResult:
    """Decide whether a contact's email domain legitimately represents the employer.

    Passing requires either exact equality with the canonical employer domain, or TWO
    independent corroborations for an alternate domain:

    1. Apollo asserts this person's organization resolves to that same domain
       (provider-asserted employment, not our inference), AND
    2. the organization is name-identical to the trusted company, or the domain itself
       is name-consistent with it.

    Name similarity alone is never sufficient, and a shared name without a matching
    provider-asserted domain is explicitly rejected as ambiguous rather than passed.
    """
    candidate = email_domain(email)
    canonical = normalize_company_domain(canonical_domain)
    org_domain = normalize_company_domain(apollo_org_domain)
    evidence = {
        "contact_email_domain": candidate,
        "canonical_employer_domain": canonical,
        "apollo_person_organization_domain": org_domain,
        "apollo_person_organization_id": str(apollo_org_id or ""),
        "apollo_person_organization_name": str(apollo_org_name or ""),
    }

    if not candidate:
        return AlignmentResult(INSUFFICIENT_EVIDENCE, "no_contact_email", evidence)
    if not _is_employer_domain(candidate):
        return AlignmentResult(
            UNRELATED_DOMAIN, "contact_email_domain_is_not_an_employer_host", evidence)
    if not canonical:
        return AlignmentResult(
            INSUFFICIENT_EVIDENCE, "no_canonical_employer_domain", evidence)

    if candidate == canonical:
        return AlignmentResult(
            EXACT_EMPLOYER_DOMAIN, "email_domain_equals_canonical_employer_domain", evidence)

    # --- alternate domain: needs provider-asserted employment + an identity match ---
    provider_asserted = bool(org_domain) and org_domain == candidate
    name_identical = bool(apollo_org_name) and company_names_exactly_equal(
        canonical_company_name, apollo_org_name)
    domain_named = bool(canonical_company_name) and domain_name_consistent(
        canonical_company_name, candidate)
    evidence.update({
        "provider_asserted_employment": provider_asserted,
        "organization_name_identical": name_identical,
        "domain_name_consistent_with_company": domain_named,
    })

    if provider_asserted and (name_identical or domain_named):
        return AlignmentResult(
            CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN,
            "apollo_org_domain_matches_contact_email_and_identity_corroborated",
            evidence,
        )
    if provider_asserted:
        # Apollo puts the person on this domain, but nothing independently ties that
        # organization to OUR employer -- a parent/holding company would look exactly
        # like this, so it is never auto-accepted.
        return AlignmentResult(
            PARENT_DOMAIN_AMBIGUOUS,
            "provider_asserted_domain_without_independent_identity_corroboration",
            evidence,
        )
    if name_identical or domain_named:
        # The name lines up but the provider does not place the person on this domain.
        return AlignmentResult(
            BRAND_DOMAIN_AMBIGUOUS,
            "identity_suggests_same_company_but_no_provider_asserted_domain",
            evidence,
        )
    return AlignmentResult(UNRELATED_DOMAIN, "no_corroboration_for_alternate_domain", evidence)


def classify_outcome(person, alignment: AlignmentResult, *, expected_person_id: str = "") -> str:
    """Map an enrichment result + alignment onto the reporting taxonomy."""
    status = str(getattr(person, "email_status", "") or "").strip().lower()
    email = str(getattr(person, "email", "") or "").strip()
    if expected_person_id and str(getattr(person, "person_id", "") or "") != expected_person_id:
        return IDENTITY_MISMATCH
    if not email:
        return NO_EMAIL
    if status == "extrapolated":
        return EXTRAPOLATED
    if status != "verified":
        return OTHER
    if alignment.alignment == EXACT_EMPLOYER_DOMAIN:
        return VERIFIED_EXACT_DOMAIN
    if alignment.alignment == CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN:
        return VERIFIED_CORROBORATED_DOMAIN
    if alignment.alignment in (PARENT_DOMAIN_AMBIGUOUS, BRAND_DOMAIN_AMBIGUOUS):
        return VERIFIED_DOMAIN_AMBIGUOUS
    return VERIFIED_DOMAIN_MISMATCH


@dataclass
class RecoveryOutcome:
    outcome: str
    reason: str = ""
    patch: Dict[str, Any] = field(default_factory=dict)
    alignment: Optional[AlignmentResult] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def recovered(self) -> bool:
        return bool(self.patch) and self.outcome in (
            VERIFIED_EXACT_DOMAIN, VERIFIED_CORROBORATED_DOMAIN)


#: Fields the contact rebuild owns. Everything else on the row is job/company context
#: and is preserved untouched.
CONTACT_FIELDS = (
    "Hiring Manager", "HM Title", "LinkedIn", "Apollo Person ID", "Email",
    "Apollo Email Status", "Email Validation", "Contact Alignment", "Email Source",
    "Lead Key",
)


def build_recovery(
    fields: Dict[str, Any],
    person,
    *,
    target_titles,
    active_person_employer_keys: Set[str] = frozenset(),
    expected_person_id: str = "",
) -> RecoveryOutcome:
    """Build the COMPLETE replacement contact state, or explain why it is refused.

    Transactional by construction: a patch is returned only when every gate passes, so
    a caller can never half-apply a contact. Returns an empty patch otherwise and the
    original row must be left untouched.
    """
    from contact_gate import ContactGate
    from decision_types import GateState
    from email_gate import EmailGate
    from hiring_manager import _lead_key, rank_candidates
    from validation_integrity import fingerprint_matches, validation_fingerprint

    canonical_domain = safe_company_domain(
        fields.get("Website") or "", config.INTERMEDIARY_JOB_DOMAINS)
    company_name = str(fields.get("Company") or "")
    org_domain = str(getattr(person, "organization_domain", "") or "")
    org_raw = getattr(person, "raw", None) or {}
    org_block = org_raw.get("organization") if isinstance(org_raw, dict) else {}
    org_block = org_block if isinstance(org_block, dict) else {}
    alignment = classify_alignment(
        email=str(getattr(person, "email", "") or ""),
        canonical_domain=canonical_domain,
        canonical_company_name=company_name,
        apollo_org_domain=org_domain,
        apollo_org_name=str(getattr(person, "organization_name", "") or ""),
        apollo_org_id=str(org_block.get("id") or ""),
    )
    outcome_class = classify_outcome(person, alignment, expected_person_id=expected_person_id)
    provenance = {
        "alternate_contact_recovery_attempted": True,
        "alternate_contact_previous_person_id": str(fields.get("Apollo Person ID") or ""),
        "alternate_contact_result": outcome_class,
        "alignment_class": alignment.alignment,
        "alignment_evidence": alignment.evidence,
        "recovery_version": RECOVERY_VERSION,
    }

    def refuse(reason: str, outcome: str = "") -> RecoveryOutcome:
        provenance["alternate_contact_failure_reason"] = reason
        provenance["alternate_contact_result"] = outcome or outcome_class
        return RecoveryOutcome(outcome or outcome_class, reason,
                               alignment=alignment, provenance=provenance)

    if outcome_class not in (VERIFIED_EXACT_DOMAIN, VERIFIED_CORROBORATED_DOMAIN):
        return refuse(f"not_recoverable:{outcome_class}")
    if not alignment.passes:
        return refuse(f"alignment_not_passing:{alignment.alignment}")

    email = str(person.email or "").strip().lower()
    bucket = str(fields.get("Role Bucket") or "")
    # Person/employer uniqueness: production policy is ONE resolved email per employer
    # across ALL role buckets, so a person already active there is never re-enrolled.
    trusted = build_trusted_domains(
        canonical_domain=canonical_domain, apollo_org_domain=org_domain,
        identity_key=str(fields.get("Outbound Company Identity") or ""))
    for domain in {canonical_domain, email_domain(email)} | trusted:
        if domain and f"{domain}|{email}" in active_person_employer_keys:
            return refuse("already_active_at_this_employer", PERSON_EMPLOYER_DUPLICATE)

    # Re-run the REAL production gates against the new contact.
    contact_decision = ContactGate().evaluate(
        person=person, target_titles=list(target_titles or []),
        company_domains=set(trusted), company_name=company_name)
    if contact_decision.state != GateState.PASS:
        return refuse(f"contact_gate:{contact_decision.state_value}", GATE_REJECTED)
    email_decision = EmailGate().evaluate(
        person=person, hunter_result=None, company_domains=set(trusted))
    if email_decision.state != GateState.PASS:
        return refuse(f"email_gate:{email_decision.state_value}", GATE_REJECTED)
    if not rank_candidates([{"id": person.person_id, "title": person.title,
                             "linkedin_url": person.linkedin_url}], list(target_titles or [])):
        return refuse("hm_rank_rejected", GATE_REJECTED)

    full_name = " ".join(
        part for part in (str(getattr(person, "first_name", "") or ""),
                          str(getattr(person, "last_name", "") or "")) if part).strip()
    if not full_name or not person.person_id:
        return refuse("incomplete_contact_identity", GATE_REJECTED)

    patch = {
        "Hiring Manager": full_name,
        "HM Title": str(person.title or ""),
        "LinkedIn": str(person.linkedin_url or ""),
        "Apollo Person ID": str(person.person_id or ""),
        "Email": email,
        "Apollo Email Status": str(person.email_status or ""),
        "Email Validation": email_decision.state_value,
        "Contact Alignment": contact_decision.state_value,
        "Email Source": "apollo",
        "Lead Key": _lead_key(canonical_domain or email_domain(email), email, bucket),
    }
    # The fingerprint signs the contact, so it MUST be regenerated -- and only over a
    # row that is currently authentic, so a broken row is never signed into validity.
    if not fingerprint_matches(fields):
        return refuse("original_row_fingerprint_invalid", GATE_REJECTED)
    merged = {**fields, **patch}
    patch["Validation Fingerprint"] = validation_fingerprint(merged)

    provenance["alternate_contact_recovery_rank"] = 1
    provenance["contact_email_domain"] = email_domain(email)
    provenance["trusted_employer_domains"] = sorted(trusted)
    return RecoveryOutcome(outcome_class, "recovered", patch, alignment, provenance)


def recovered_status(fields: Dict[str, Any]) -> str:
    """The canonical post-recovery Airtable status.

    Traced, not assumed: ``_job_to_fields`` creates a Fantastic lead whose stored FACTS
    are all send-safe as Status=Approved WITHOUT human review
    (``FANTASTIC_AUTO_APPROVE_SEND_SAFE``), because approval here is fact-based and
    Approved Sync independently re-checks ``send_safe_facts``. A recovered contact that
    passes the same fact test therefore qualifies under the same rule. Anything that
    does not -- or a non-Fantastic row, or the flag being off -- stays Pending for
    review rather than being silently treated as approved.
    """
    from airtable_client import send_safe_facts

    if not bool(getattr(config, "FANTASTIC_AUTO_APPROVE_SEND_SAFE", False)):
        return config.AIRTABLE_STATUS_PENDING
    return (config.AIRTABLE_STATUS_APPROVED if send_safe_facts(fields)[0]
            else config.AIRTABLE_STATUS_PENDING)


#: Artifact schema for one recovery attempt. Deliberately non-PII: it records WHY an
#: attempt failed so a follow-up decision (e.g. "first alternate had no email -> try a
#: second") can be made WITHOUT re-enriching the first person, which would re-bill.
ATTEMPT_SCHEMA = "alternate-contact-attempt/1"


def attempt_record(
    *,
    record_id: str,
    outcome: RecoveryOutcome,
    rank: int = 1,
    candidate_person_id: str = "",
    candidate_pool_depth: int = 0,
    original_block_reason: str = "",
    employer_domain: str = "",
) -> Dict[str, Any]:
    """Build the persisted, non-PII provenance for ONE alternate-contact attempt.

    The first sweep persisted only successes, so the 28 failures could not later be
    segmented by failure class and the information was unrecoverable without paying
    Apollo again. Every attempt -- recovered or not -- must now be written.

    Raw e-mail addresses and person names are never included; only the domain, the
    Apollo person id and the classification are.
    """
    prov = dict(outcome.provenance or {})
    alignment = outcome.alignment
    return {
        "schema": ATTEMPT_SCHEMA,
        "record_id": str(record_id or ""),
        "alternate_contact_recovery_attempted": True,
        "alternate_contact_rank": int(rank),
        "alternate_contact_result": outcome.outcome,
        "alternate_contact_failure_reason": (
            "" if outcome.recovered else (outcome.reason or "")),
        "alternate_contact_previous_person_id": prov.get(
            "alternate_contact_previous_person_id", ""),
        "candidate_apollo_person_id": str(candidate_person_id or ""),
        "candidate_pool_depth": int(candidate_pool_depth or 0),
        "alignment_class": (alignment.alignment if alignment else ""),
        "alignment_reason": (alignment.reason if alignment else ""),
        "email_outcome": _email_outcome(outcome),
        "contact_email_domain": prov.get("contact_email_domain", "")
        or ((alignment.evidence or {}).get("contact_email_domain", "") if alignment else ""),
        "employer_domain": str(employer_domain or ""),
        "original_block_reason": str(original_block_reason or ""),
        "recovered": outcome.recovered,
        "recovery_version": RECOVERY_VERSION,
    }


def _email_outcome(outcome: RecoveryOutcome) -> str:
    """Coarse e-mail disposition, independent of domain alignment."""
    if outcome.outcome == NO_EMAIL:
        return "no_email"
    if outcome.outcome == EXTRAPOLATED:
        return "extrapolated"
    if outcome.outcome in (VERIFIED_EXACT_DOMAIN, VERIFIED_CORROBORATED_DOMAIN,
                           VERIFIED_DOMAIN_AMBIGUOUS, VERIFIED_DOMAIN_MISMATCH):
        return "verified"
    return "other"


#: First-alternate failures worth a SECOND candidate. A missing or extrapolated e-mail
#: is a property of THAT PERSON, so another employee is an independent draw. A domain
#: mismatch is a property of the EMPLOYER, so a colleague reproduces it -- measured:
#: the alternate-domain rule contributed 0 recoveries across 42 first alternates.
SECOND_ALTERNATE_WORTHWHILE = frozenset({NO_EMAIL, EXTRAPOLATED})


def second_alternate_eligible(attempt: Dict[str, Any], *, pool_depth: int = 0) -> bool:
    """True when a company's first-alternate failure justifies a second candidate."""
    if attempt.get("recovered"):
        return False
    if str(attempt.get("alternate_contact_result") or "") not in SECOND_ALTERNATE_WORTHWHILE:
        return False
    depth = int(pool_depth or attempt.get("candidate_pool_depth") or 0)
    return depth >= 2


def write_attempts(path: str, attempts: Iterable[Dict[str, Any]]) -> int:
    """Append attempt records as JSONL. Best-effort: never raises into the caller."""
    rows = [a for a in attempts if a]
    if not rows:
        return 0
    try:
        import os

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        return len(rows)
    except Exception:  # pragma: no cover - observability must never break recovery
        return 0
