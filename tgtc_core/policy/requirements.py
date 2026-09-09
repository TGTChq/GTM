"""Explicit, versioned eligibility policy with provenance.

Every rule the core applies is declared here with WHERE it came from. Two
provenance classes exist and the distinction is load-bearing:

* ``blueprint`` -- stated in TGTC_REBUILD_BLUEPRINT.md.
* ``legacy_default_pending_confirmation`` -- the value production runs today,
  imported as the starting policy. These are decisions Luis has not re-confirmed
  for the rebuild; the manifest (``describe()``) says so next to each one.

Nothing in this module reads the environment. Runtime overrides come through
``tgtc_core.config.Settings`` and are recorded in the run manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Tuple

from .campaigns import POLICY_VERSION

BLUEPRINT = "blueprint"
LEGACY = "legacy_default_pending_confirmation"


@dataclass(frozen=True)
class Rule:
    key: str
    value: object
    provenance: str
    meaning: str


RULES: Tuple[Rule, ...] = (
    Rule("market", "us_market", LEGACY,
         "Job must be for the US market: explicit US scope or provider country includes US, and no foreign-only clause."),
    Rule("employment", "full_time_open_ended", LEGACY,
         "Full-time, open-ended only; part-time/contract/fixed-term/fractional/temporary/freelance/seasonal/internship/unpaid excluded."),
    Rule("deliverability", "remote_deliverable_knowledge_work", BLUEPRINT,
         "Field work, mandatory physical facility, substantial travel, security clearance and mandatory licences are excluded. Remote/hybrid/onsite labels are data, not gates."),
    Rule("job_seniority_excluded", ("intern", "director", "vp", "chief", "principal", "staff", "lead"), LEGACY,
         "Leadership and principal/staff/lead IC roles are outside the offer; senior IC allowed (ROLE_ALLOW_SENIOR_IC=1)."),
    Rule("people_management_excluded", True, LEGACY,
         "A posting whose duties include managing direct reports is excluded (job_quality.has_people_authority)."),
    Rule("min_employees", 25, LEGACY, "Employer size lower bound when a count is known (MIN_EMPLOYEES)."),
    Rule("max_employees", 1000, LEGACY, "Employer size upper bound when a count is known (MAX_EMPLOYEES)."),
    Rule("require_employee_count", False, LEGACY,
         "Unknown headcount does not reject by itself (REJECT_UNKNOWN_FIRMOGRAPHICS is an env value, not a code truth)."),
    Rule("founder_fallback_max_employees", 99, LEGACY,
         "Founders/CEO are searched as buyers only at employers with <= 99 employees."),
    Rule("max_match_attempts_per_opportunity", 3, LEGACY,
         "Paid person enrichments attempted per opportunity before it closes as no_verified_buyer (APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET)."),
    Rule("email_verification_authority", ("apollo",), BLUEPRINT,
         "Only Apollo email_status == 'verified' proves an email. Hunter never promotes. unknown/accept_all/extrapolated never pass."),
    Rule("email_domain_rule", "employer_domain_or_corroborated_alternate", BLUEPRINT,
         "Email must be on the employer domain, or on an alternate domain corroborated by Apollo organization evidence for the same employer."),
    Rule("require_contact_linkedin", True, LEGACY, "A buyer without a LinkedIn profile is not approved (REQUIRE_CONTACT_LINKEDIN=1)."),
    Rule("require_current_employment_evidence", True, LEGACY,
         "Apollo must show the buyer currently at the resolved employer (REQUIRE_CURRENT_EMPLOYMENT_EVIDENCE=1)."),
    Rule("account_level_suppression", False, LEGACY,
         "One-contact-per-company is OFF in production (AIRTABLE_SUPPRESS_ACCOUNT_LEVEL=false, container-verified 2026-09-07)."),
    Rule("company_function_suppression", True, LEGACY,
         "An active Airtable row for the same company x function suppresses a new one."),
    Rule("person_employer_uniqueness", True, BLUEPRINT,
         "A person is approved once per employer across functions; skip_if_in_workspace follows this."),
    Rule("person_evidence_ttl_days", 45, LEGACY, "A verified email is reusable without a new paid call for this long (APOLLO_CACHE_PERSON_MATCH_TTL_DAYS)."),
    Rule("fresh_share_pct", 80, BLUEPRINT, "Claim share reserved for the fresh lane under contention; borrows both ways."),
    Rule("commercial_max_age_days", 30, LEGACY,
         "Postings older than this at first observation enter the backfill lane; older than 90 days at approval time are not approved (RECOVERY_MAX_JOB_AGE_DAYS / freshness tiers)."),
    Rule("approval_max_age_days", 90, LEGACY, "No approval for a posting whose commercial age exceeds this."),
)

RULE_BY_KEY: Dict[str, Rule] = {r.key: r for r in RULES}


def rule(key: str):
    return RULE_BY_KEY[key].value


#: Apollo industry taxonomy labels that exclude an employer
#: (``config.APOLLO_EXCLUDED_INDUSTRY_KEYWORDS`` at 5d87851). Exact match, or the
#: label followed by " / " or " - " (Apollo appends qualifiers that way).
EXCLUDED_INDUSTRIES: FrozenSet[str] = frozenset({
    "staffing and recruiting", "staffing", "recruiting", "government administration",
    "nonprofit organization management", "hospital & health care", "hospitals and health care",
    "health care", "healthcare", "mental health care", "mental health", "medical practice",
    "human resources services", "outsourcing/offshoring", "events services", "broadcast media",
    "online media", "internet news", "news media", "media production", "digital news",
    "financial news", "newspapers", "book publishing", "chemicals",
})

STAFFING_INDUSTRIES: FrozenSet[str] = frozenset({
    "staffing and recruiting", "staffing", "recruiting", "human resources services",
})


def excluded_industry(industry: str) -> str:
    """Return the matching excluded label, or '' when the industry is allowed."""
    norm = " ".join(str(industry or "").lower().replace("&", "&").split())
    if not norm:
        return ""
    if norm in EXCLUDED_INDUSTRIES:
        return norm
    for label in EXCLUDED_INDUSTRIES:
        if norm.startswith(label + " / ") or norm.startswith(label + " - "):
            return label
    return ""


@dataclass
class PolicyManifest:
    policy_version: str = POLICY_VERSION
    rules: List[Dict[str, object]] = field(default_factory=list)


def describe() -> PolicyManifest:
    return PolicyManifest(rules=[
        {"key": r.key, "value": r.value, "provenance": r.provenance, "meaning": r.meaning}
        for r in RULES
    ])
