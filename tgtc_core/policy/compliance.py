"""Country permission matrix and the four independent gates (`tgtc-compliance/1`).

Specification: ``funnel_audit_20260919/COMPLIANCE_MATRIX.md``, authoritative and
researched, supplied by Luis 2026-09-20. It **supersedes** the earlier blanket
statement that "only the United States has a legal basis", which was too broad
and which wrongly erased four markets from capacity analysis.

| Country | acquisition | aggregate capacity | person enrichment | cold email |
|---|---|---|---|---|
| US | yes | yes | yes | ENABLED_CONDITIONAL |
| UK | yes | yes | ENABLED_CONDITIONAL | ENABLED_CONDITIONAL, corporate subscribers only |
| DE | yes | yes | no (production outreach) | DISABLED_PENDING_CONSENT |
| AE | yes | yes | no (production outreach) | DISABLED_PENDING_DOCUMENTED_OPT_IN |
| SA | yes | yes | no (production outreach) | DISABLED_PENDING_CONSENT |

Four things about the shape of this module are load-bearing, not stylistic.

**Four permissions, never one flag.** Acquisition, aggregate capacity
modelling, person enrichment and cold email are four separate permissions with
four separate functions. DE/AE/SA are `yes` for the first two and `no` for the
last two, so a single generic geography flag can only be wrong in one of two
directions: it either deletes four markets from capacity analysis (the mistake
this matrix supersedes) or it emails into a jurisdiction that forbids it.

**The country is an argument, never a fallback.** Every gate takes the country
it decides on as its first positional argument, and nothing here reads a second
country field when the first is missing. A job's location does not determine
the contact's jurisdiction. ``evaluate`` is the ONLY place record fields are
bound to gates: ``job_country`` to the two job gates, ``contact_country`` to the
two person gates, and never one in place of the other.

**Fail closed for sending, open for capacity.** An unknown or out-of-matrix
jurisdiction is never ``allowed``. It is also never discarded: ``evaluate``
returns ``retained_for_capacity=True`` for every record and a named
``capacity_category`` so the record can be counted in its own bucket instead of
being deleted or silently added to a "ready to send" total.

**Share a predicate, never copy it.** The send conditions common to the US and
UK regimes (opt-out honoured, unsubscribe available, suppression available) are
ONE ``Condition`` object each, referenced by both ``US_SEND_CONDITIONS`` and
``UK_SEND_CONDITIONS``. The UK processing conditions are likewise referenced by
both the enrichment gate and the cold-email gate rather than restated.

Scope of what these predicates can honestly decide. The CAN-SPAM controls that
live in the email TEMPLATE -- accurate sender/header/subject, commercial
identification, a physical postal address -- are NOT decidable from any stored
field, so no condition here claims to check them. They remain a template and
operations responsibility, and that gap is stated in the task report rather than
papered over with a default-true flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from ..domain.identity import FREE_MAIL_DOMAINS, email_domain

COMPLIANCE_RULE_VERSION = "tgtc-compliance/1"

# --- gate names -------------------------------------------------------------
GATE_JOB_ACQUISITION = "job_acquisition_allowed"
GATE_AGGREGATE_CAPACITY = "aggregate_capacity_modelling_allowed"
GATE_PERSON_ENRICHMENT = "person_enrichment_allowed"
GATE_COLD_EMAIL = "cold_email_allowed"
GATES: Tuple[str, ...] = (GATE_JOB_ACQUISITION, GATE_AGGREGATE_CAPACITY,
                          GATE_PERSON_ENRICHMENT, GATE_COLD_EMAIL)

# --- permission statuses ----------------------------------------------------
#: The matrix says yes, with no further condition.
ALLOWED = "allowed"
#: The matrix says yes SUBJECT to conditions that this module evaluates.
CONDITIONAL = "enabled_conditional"
#: The matrix says no for this country.
DISABLED = "disabled"
#: The country is unknown or outside the researched matrix: fail closed.
UNKNOWN_JURISDICTION = "unknown_jurisdiction"

# --- corporate subscriber status (UK) ---------------------------------------
CORPORATE = "corporate"
NOT_CORPORATE = "not_corporate"
#: Never treated as corporate. An unknown entity type is the DEFAULT, so every
#: path that forgets to establish one fails closed rather than open.
ENTITY_UNKNOWN = "unknown"

# --- opt-out status ---------------------------------------------------------
OPT_OUT_NONE = "none"
OPTED_OUT = "opted_out"
OPT_OUT_UNKNOWN = "unknown"

# --- capacity categories (counting rule) ------------------------------------
CATEGORY_OUTREACH_ELIGIBLE = "OUTREACH_ELIGIBLE"
#: DE/AE/SA: a real, counted market that can never reach a ready-to-send total.
#: "COMPLIANCE_BLOCKED_CAPACITY ... is not the same as zero-yield."
CATEGORY_BLOCKED_CAPACITY = "COMPLIANCE_BLOCKED_CAPACITY"
CATEGORY_BLOCKED_UNKNOWN_JURISDICTION = "COMPLIANCE_BLOCKED_UNKNOWN_JURISDICTION"
CATEGORY_BLOCKED_ENTITY_UNKNOWN = "COMPLIANCE_BLOCKED_ENTITY_UNKNOWN"
CATEGORY_BLOCKED_CONDITION = "COMPLIANCE_BLOCKED_CONDITION"
CAPACITY_CATEGORIES: Tuple[str, ...] = (
    CATEGORY_OUTREACH_ELIGIBLE, CATEGORY_BLOCKED_CAPACITY,
    CATEGORY_BLOCKED_UNKNOWN_JURISDICTION, CATEGORY_BLOCKED_ENTITY_UNKNOWN,
    CATEGORY_BLOCKED_CONDITION,
)

_REASON_PREFIX = "compliance:"


# ---------------------------------------------------------------------------
# Country normalisation
# ---------------------------------------------------------------------------
#: Only the five researched geographies resolve. Everything else -- including
#: real countries the matrix simply does not cover -- resolves to "" (unknown)
#: and fails closed. The matrix is the authority on what has been researched;
#: this table must not be extended without a researched row to extend it with.
_COUNTRY_ALIASES: Mapping[str, str] = {
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "united states": "US",
    "united states of america": "US", "the united states": "US",
    "uk": "UK", "gb": "UK", "gbr": "UK", "united kingdom": "UK", "great britain": "UK",
    "england": "UK", "scotland": "UK", "wales": "UK", "northern ireland": "UK",
    "united kingdom of great britain and northern ireland": "UK",
    "de": "DE", "deu": "DE", "germany": "DE", "deutschland": "DE",
    "ae": "AE", "are": "AE", "uae": "AE", "united arab emirates": "AE", "the united arab emirates": "AE",
    "sa": "SA", "sau": "SA", "ksa": "SA", "saudi arabia": "SA", "kingdom of saudi arabia": "SA",
}


def normalize_country(value: Any) -> str:
    """The matrix key for ``value``, or ``""`` when it is unknown or out of scope."""
    return _COUNTRY_ALIASES.get(" ".join(str(value or "").strip().lower().split()), "")


# ---------------------------------------------------------------------------
# The matrix, as data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CountryPermissions:
    country: str
    job_acquisition: str
    aggregate_capacity_modelling: str
    person_enrichment: str
    cold_email: str
    #: The matrix's own vocabulary, carried through so a report can quote it.
    person_enrichment_label: str
    cold_email_label: str
    note: str
    source: str


COUNTRY_PERMISSIONS: Dict[str, CountryPermissions] = {
    "US": CountryPermissions(
        "US", ALLOWED, ALLOWED, ALLOWED, CONDITIONAL,
        "ENABLED", "ENABLED_CONDITIONAL",
        "Accurate sender, header and subject; commercial identification where required; physical "
        "postal address; visible unsubscribe; suppression checked before every send; opt-outs "
        "honoured within ten business days.",
        "https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business"),
    "UK": CountryPermissions(
        "UK", ALLOWED, ALLOWED, CONDITIONAL, CONDITIONAL,
        "ENABLED_CONDITIONAL", "ENABLED_CONDITIONAL_CORPORATE_SUBSCRIBERS_ONLY",
        "Corporate subscribers only: incorporated companies, LLPs and other bodies with separate "
        "legal personality. Sole traders, unincorporated businesses, certain partnerships and "
        "personal email addresses are excluded, as is any entity whose legal type cannot be "
        "verified. Named employees and individual work emails remain personal data.",
        "https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-"
        "communications/business-to-business-marketing/"),
    "DE": CountryPermissions(
        "DE", ALLOWED, ALLOWED, DISABLED, DISABLED,
        "DISABLED_PRODUCTION_OUTREACH", "DISABLED_PENDING_CONSENT",
        "Sec. 7 UWG generally requires prior express consent for marketing email; the narrow "
        "existing-customer exception does not cover cold prospects.",
        "https://www.gesetze-im-internet.de/uwg_2004/__7.html"),
    "AE": CountryPermissions(
        "AE", ALLOWED, ALLOWED, DISABLED, DISABLED,
        "DISABLED_PRODUCTION_OUTREACH", "DISABLED_PENDING_DOCUMENTED_OPT_IN",
        "TDRA policy includes email within electronic addresses and uses opt-in as the default; a "
        "UAE link includes a recipient located in the UAE even when the message originates abroad.",
        "https://tdra.gov.ae/-/media/About/regulations-and-ruling/EN/Unsolicited-Elrctronic-"
        "Commuincations--pdf.ashx"),
    "SA": CountryPermissions(
        "SA", ALLOWED, ALLOWED, DISABLED, DISABLED,
        "DISABLED_PRODUCTION_OUTREACH", "DISABLED_PENDING_CONSENT",
        "Recipient consent is required before advertising material is sent, and personal data used "
        "for marketing must be collected directly from the data subject; Apollo-sourced contacts "
        "do not establish this.",
        "https://www.mewa.gov.sa/en/Ministry/AboutMinistry/RulesAndConditions/Pages/PolicyOfUse.aspx"),
}


# ---------------------------------------------------------------------------
# UK legal-entity classification
# ---------------------------------------------------------------------------
#: Verified legal FORMS with separate legal personality. A form is recognised
#: here only when the words themselves name the legal form.
_CORPORATE_FORMS: Tuple[str, ...] = (
    "ltd", "ltd.", "limited", "private limited company", "public limited company", "plc", "plc.",
    "llp", "limited liability partnership", "limited partnership", "lp",
    "community interest company", "cic", "company limited by guarantee", "clg",
    "incorporated", "inc", "inc.", "corporation", "corp", "corp.", "company limited",
    "limited company", "gmbh", "ug (haftungsbeschraenkt)", "ag", "bv", "nv", "sa", "sarl", "oy", "ab",
)
#: Forms the matrix excludes by name, plus the ownership descriptors that are
#: NOT a legal form at all. Anything not in either table is ENTITY_UNKNOWN --
#: and an unknown entity type is never treated as corporate.
_NOT_CORPORATE_FORMS: Tuple[str, ...] = (
    "sole trader", "sole proprietor", "sole proprietorship", "sole-proprietorship",
    "self employed", "self-employed", "freelance", "freelancer", "individual",
    "partnership", "general partnership", "ordinary partnership",
    "unincorporated", "unincorporated association", "unincorporated business",
    "unincorporated partnership", "trust", "charitable trust",
)
_MULTI_WORD_CORPORATE_FORMS: Tuple[str, ...] = tuple(f for f in _CORPORATE_FORMS if " " in f)
_SINGLE_TOKEN_CORPORATE_FORMS = frozenset(f.strip(".") for f in _CORPORATE_FORMS if " " not in f)


def _norm_entity(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace(",", " ").split())


def classify_corporate_subscriber(entity_type: Any) -> str:
    """``CORPORATE`` / ``NOT_CORPORATE`` / ``ENTITY_UNKNOWN`` for a legal form.

    "An unknown entity type is never treated as corporate." That includes the
    self-declared ownership descriptors a provider supplies in place of a legal
    form ("Privately Held", "Public Company", "Nonprofit", "Educational",
    "Government Agency"): they say who owns the body, not whether it has
    separate legal personality, so they classify as ENTITY_UNKNOWN. Only the
    words of a recognised legal form produce CORPORATE.
    """
    norm = _norm_entity(entity_type)
    if not norm:
        return ENTITY_UNKNOWN
    # Exact legal forms decide first, in that order. An LLP is a qualifying
    # corporate body under the matrix even though the word "partnership" is in
    # its name, so the recognised corporate forms are matched BEFORE the
    # excluded ones; a substring rule alone would classify every LLP as an
    # excluded partnership.
    if norm in _CORPORATE_FORMS:
        return CORPORATE
    if norm in _NOT_CORPORATE_FORMS:
        return NOT_CORPORATE
    for form in _MULTI_WORD_CORPORATE_FORMS:
        if form in norm:
            return CORPORATE
    for form in _NOT_CORPORATE_FORMS:
        if form in norm:
            return NOT_CORPORATE
    if {w.strip(".") for w in norm.replace(".", " ").split()} & _SINGLE_TOKEN_CORPORATE_FORMS:
        return CORPORATE
    return ENTITY_UNKNOWN


# ---------------------------------------------------------------------------
# Conditions: ONE definition each, referenced by every regime that needs it
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Condition:
    """A named, deterministic requirement. ``holds(ctx)`` answers it from the
    stored evidence in ``ctx``; ``reason`` is what the record is blocked with."""
    reason: str
    holds: Callable[[Dict[str, Any]], bool]
    meaning: str

    def __hash__(self):  # referenced in sets by identity of its name
        return hash(self.reason)


def _text(ctx: Dict[str, Any], key: str) -> str:
    return str(ctx.get(key) or "").strip()


# -- UK processing conditions (enrichment AND outreach) ----------------------
UK_CORPORATE_SUBSCRIBER = Condition(
    "uk:not_a_verified_corporate_subscriber",
    lambda ctx: (classify_corporate_subscriber(ctx.get("employer_legal_entity_type")) == CORPORATE
                 and str(ctx.get("corporate_subscriber_status") or ENTITY_UNKNOWN) == CORPORATE),
    "Incorporated company or qualifying corporate body, and no sole trader or unincorporated "
    "partnership. The stored corporate_subscriber_status is a cross-check, never the sole "
    "authority: the legal form must independently classify as corporate, so an unknown entity "
    "type can never be treated as corporate.",
)
UK_LAWFUL_BASIS = Condition(
    "uk:no_lawful_basis_record",
    lambda ctx: bool(_text(ctx, "legal_basis") and _text(ctx, "legal_basis_evidence")),
    "A documented legitimate-interests assessment or other valid lawful basis, with its evidence "
    "reference, must be recorded before activation.",
)
UK_PRIVACY_NOTICE_PROCESS = Condition(
    "uk:privacy_notice_process_not_configured",
    lambda ctx: bool(ctx.get("privacy_notice_configured")),
    "UK GDPR privacy information, and a process that delivers it, must be configured.",
)
#: Referenced by BOTH the enrichment gate and the cold-email gate.
UK_PROCESSING_CONDITIONS: Tuple[Condition, ...] = (
    UK_CORPORATE_SUBSCRIBER, UK_LAWFUL_BASIS, UK_PRIVACY_NOTICE_PROCESS,
)

# -- send conditions shared by every enabled regime --------------------------
OPT_OUT_HONOURED = Condition(
    "outreach:historically_opted_out",
    lambda ctx: str(ctx.get("opt_out_status") or OPT_OUT_UNKNOWN) != OPTED_OUT,
    "A record that has ever opted out is never contacted again. An absent opt-out record is not "
    "an opt-out; the suppression check below is what proves the history was consulted.",
)
UNSUBSCRIBE_AVAILABLE = Condition(
    "outreach:unsubscribe_not_available",
    lambda ctx: bool(ctx.get("unsubscribe_available")),
    "A visible unsubscribe / direct-marketing objection mechanism must exist on the send path.",
)
SUPPRESSION_AVAILABLE = Condition(
    "outreach:suppression_not_available",
    lambda ctx: bool(ctx.get("suppression_available")),
    "Permanent suppression must be available and checked before every send.",
)
#: The US regime is exactly these three. They are the SAME objects the UK
#: regime references below -- one definition, never a copy.
US_SEND_CONDITIONS: Tuple[Condition, ...] = (
    OPT_OUT_HONOURED, UNSUBSCRIBE_AVAILABLE, SUPPRESSION_AVAILABLE,
)

# -- UK send-only conditions -------------------------------------------------
UK_PRIVACY_NOTICE_DUE = Condition(
    "uk:privacy_notice_not_due_dated",
    lambda ctx: ctx.get("privacy_notice_due_at") not in (None, ""),
    "Privacy information must be delivered no later than one month after obtaining third-party "
    "data, so the record carries the date by which it is due.",
)
UK_EXACT_DOMAIN_VERIFIED_WORK_EMAIL = Condition(
    "uk:not_an_exact_domain_verified_work_email",
    lambda ctx: (_text(ctx, "email_alignment") == "EXACT_EMPLOYER_DOMAIN"
                 and _text(ctx, "email_status").lower() == "verified"),
    "Business email only, on the employer's own domain, verified. A corroborated ALTERNATE domain "
    "is not an exact-domain match and does not satisfy this.",
)
UK_NO_FREE_OR_PERSONAL_EMAIL = Condition(
    "uk:personal_or_free_email_domain",
    lambda ctx: bool(_text(ctx, "email")) and email_domain(_text(ctx, "email")) not in FREE_MAIL_DOMAINS,
    "No personal or free email domain, whatever a provider reports about domain alignment.",
)
UK_SEND_CONDITIONS: Tuple[Condition, ...] = UK_PROCESSING_CONDITIONS + (
    UK_PRIVACY_NOTICE_DUE,
) + US_SEND_CONDITIONS + (
    UK_EXACT_DOMAIN_VERIFIED_WORK_EMAIL, UK_NO_FREE_OR_PERSONAL_EMAIL,
)


def _first_failure(conditions: Tuple[Condition, ...], ctx: Dict[str, Any]) -> str:
    for condition in conditions:
        if not condition.holds(ctx):
            return condition.reason
    return ""


# ---------------------------------------------------------------------------
# Gate decisions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateDecision:
    gate: str
    #: The NORMALISED country this gate decided on, or "" when it was unknown.
    #: Reported so a caller can prove which jurisdiction a decision came from.
    country: str
    status: str
    allowed: bool
    reason: str = ""
    matrix_label: str = ""
    rule_version: str = COMPLIANCE_RULE_VERSION

    def as_dict(self) -> Dict[str, Any]:
        return {"gate": self.gate, "country": self.country, "status": self.status,
                "allowed": self.allowed, "reason": self.reason, "matrix_label": self.matrix_label,
                "rule_version": self.rule_version}


def _unknown(gate: str, raw: Any) -> GateDecision:
    return GateDecision(gate, "", UNKNOWN_JURISDICTION, False,
                        f"{_REASON_PREFIX}unknown_jurisdiction:{str(raw or '').strip()[:40] or 'absent'}")


def job_acquisition_allowed(country: Any) -> GateDecision:
    """May a job posting in ``country`` be acquired at all? Yes in all five
    researched geographies; unknown countries fail closed.

    Reachability: this gate is an API and a recorded field. It is deliberately
    NOT enforced anywhere in the live acquisition path -- every in-scope
    geography is `yes`, so enforcing it could only ever delete out-of-scope
    rows, and "never delete it" is the rule.
    """
    key = normalize_country(country)
    if not key:
        return _unknown(GATE_JOB_ACQUISITION, country)
    perms = COUNTRY_PERMISSIONS[key]
    return GateDecision(GATE_JOB_ACQUISITION, key, perms.job_acquisition,
                        perms.job_acquisition == ALLOWED, "", "yes")


def aggregate_capacity_modelling_allowed(country: Any) -> GateDecision:
    """May records from ``country`` be counted in aggregate capacity analysis?

    Yes everywhere in the matrix, including the three countries that can never
    be emailed. This is the gate the superseded "only the US has a legal basis"
    reading got wrong.
    """
    key = normalize_country(country)
    if not key:
        return _unknown(GATE_AGGREGATE_CAPACITY, country)
    perms = COUNTRY_PERMISSIONS[key]
    return GateDecision(GATE_AGGREGATE_CAPACITY, key, perms.aggregate_capacity_modelling,
                        perms.aggregate_capacity_modelling == ALLOWED, "", "yes")


def person_enrichment_allowed(
    country: Any,
    *,
    corporate_subscriber_status: str = ENTITY_UNKNOWN,
    employer_legal_entity_type: Any = "",
    legal_basis: Any = "",
    legal_basis_evidence: Any = "",
    privacy_notice_configured: bool = False,
    **_ignored: Any,
) -> GateDecision:
    """May a person in ``country`` be enriched (paid personal-data processing)?

    A PROCESSING permission, so it evaluates the UK PROCESSING conditions and
    not the send-only ones: an unsubscribe mechanism and a verified work email
    do not exist yet at enrichment time. ``**_ignored`` lets one record's full
    condition set be passed to every gate without each caller pruning it.
    """
    key = normalize_country(country)
    if not key:
        return _unknown(GATE_PERSON_ENRICHMENT, country)
    perms = COUNTRY_PERMISSIONS[key]
    if perms.person_enrichment == DISABLED:
        return GateDecision(GATE_PERSON_ENRICHMENT, key, DISABLED, False,
                            f"{_REASON_PREFIX}person_enrichment_not_permitted:{key}",
                            perms.person_enrichment_label)
    if perms.person_enrichment == ALLOWED:
        return GateDecision(GATE_PERSON_ENRICHMENT, key, ALLOWED, True, "", perms.person_enrichment_label)
    ctx = {"corporate_subscriber_status": corporate_subscriber_status,
           "employer_legal_entity_type": employer_legal_entity_type,
           "legal_basis": legal_basis, "legal_basis_evidence": legal_basis_evidence,
           "privacy_notice_configured": privacy_notice_configured}
    failure = _first_failure(UK_PROCESSING_CONDITIONS, ctx)
    return GateDecision(GATE_PERSON_ENRICHMENT, key, CONDITIONAL, not failure,
                        f"{_REASON_PREFIX}{failure}" if failure else "", perms.person_enrichment_label)


def cold_email_allowed(
    country: Any,
    *,
    corporate_subscriber_status: str = ENTITY_UNKNOWN,
    employer_legal_entity_type: Any = "",
    legal_basis: Any = "",
    legal_basis_evidence: Any = "",
    privacy_notice_configured: bool = False,
    privacy_notice_due_at: Any = None,
    opt_out_status: str = OPT_OUT_UNKNOWN,
    email: Any = "",
    email_alignment: Any = "",
    email_status: Any = "",
    unsubscribe_available: bool = False,
    suppression_available: bool = False,
    **_ignored: Any,
) -> GateDecision:
    """May a person in ``country`` be cold-emailed? The one gate that decides
    whether a record may ever reach a "ready to send" total.

    Every default is the fail-closed value, so a caller that forgets to supply
    a condition gets a block, not a send.
    """
    key = normalize_country(country)
    if not key:
        return _unknown(GATE_COLD_EMAIL, country)
    perms = COUNTRY_PERMISSIONS[key]
    if perms.cold_email == DISABLED:
        return GateDecision(GATE_COLD_EMAIL, key, DISABLED, False,
                            f"{_REASON_PREFIX}cold_email_not_permitted:{key}", perms.cold_email_label)
    ctx = {"corporate_subscriber_status": corporate_subscriber_status,
           "employer_legal_entity_type": employer_legal_entity_type,
           "legal_basis": legal_basis, "legal_basis_evidence": legal_basis_evidence,
           "privacy_notice_configured": privacy_notice_configured,
           "privacy_notice_due_at": privacy_notice_due_at, "opt_out_status": opt_out_status,
           "email": email, "email_alignment": email_alignment, "email_status": email_status,
           "unsubscribe_available": unsubscribe_available, "suppression_available": suppression_available}
    conditions = UK_SEND_CONDITIONS if key == "UK" else US_SEND_CONDITIONS
    failure = _first_failure(conditions, ctx)
    return GateDecision(GATE_COLD_EMAIL, key, CONDITIONAL, not failure,
                        f"{_REASON_PREFIX}{failure}" if failure else "", perms.cold_email_label)


# ---------------------------------------------------------------------------
# Record-level evaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ComplianceRecord:
    """The stored jurisdiction facts, exactly as persisted. Three separate
    country fields, never derived from one another."""
    job_country: str = ""
    company_country: str = ""
    contact_country: str = ""
    employer_legal_entity_type: str = ""
    corporate_subscriber_status: str = ENTITY_UNKNOWN
    legal_basis: str = ""
    legal_basis_evidence: str = ""
    privacy_notice_configured: bool = False
    privacy_notice_due_at: Any = None
    opt_out_status: str = OPT_OUT_UNKNOWN
    email: str = ""
    email_alignment: str = ""
    email_status: str = ""
    unsubscribe_available: bool = False
    suppression_available: bool = False

    def conditions(self) -> Dict[str, Any]:
        return {
            "corporate_subscriber_status": self.corporate_subscriber_status,
            "employer_legal_entity_type": self.employer_legal_entity_type,
            "legal_basis": self.legal_basis, "legal_basis_evidence": self.legal_basis_evidence,
            "privacy_notice_configured": self.privacy_notice_configured,
            "privacy_notice_due_at": self.privacy_notice_due_at,
            "opt_out_status": self.opt_out_status, "email": self.email,
            "email_alignment": self.email_alignment, "email_status": self.email_status,
            "unsubscribe_available": self.unsubscribe_available,
            "suppression_available": self.suppression_available,
        }


@dataclass(frozen=True)
class ComplianceDecision:
    outreach_eligible: bool
    outreach_block_reason: str
    capacity_category: str
    gates: Dict[str, GateDecision] = field(default_factory=dict)
    compliance_rule_version: str = COMPLIANCE_RULE_VERSION
    #: Always true. A blocked record is retained and counted in its own bucket;
    #: it is never deleted and never silently folded into another total.
    retained_for_capacity: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {"outreach_eligible": self.outreach_eligible,
                "outreach_block_reason": self.outreach_block_reason,
                "capacity_category": self.capacity_category,
                "compliance_rule_version": self.compliance_rule_version,
                "retained_for_capacity": self.retained_for_capacity,
                "gates": {k: v.as_dict() for k, v in self.gates.items()}}


def _category(cold: GateDecision) -> str:
    if cold.allowed:
        return CATEGORY_OUTREACH_ELIGIBLE
    if cold.status == UNKNOWN_JURISDICTION:
        return CATEGORY_BLOCKED_UNKNOWN_JURISDICTION
    if cold.status == DISABLED:
        return CATEGORY_BLOCKED_CAPACITY
    if cold.reason == f"{_REASON_PREFIX}{UK_CORPORATE_SUBSCRIBER.reason}":
        return CATEGORY_BLOCKED_ENTITY_UNKNOWN
    return CATEGORY_BLOCKED_CONDITION


def evaluate(record: ComplianceRecord) -> ComplianceDecision:
    """Run all four gates over one record and produce the outreach verdict.

    The ONLY place record fields are bound to gates, and the binding is fixed:
    ``job_country`` decides the two job gates, ``contact_country`` decides the
    two person gates. There is no fallback between them -- a US job with a
    German contact is a German contact, and a German job with a US contact is a
    US contact.
    """
    conditions = record.conditions()
    gates = {
        GATE_JOB_ACQUISITION: job_acquisition_allowed(record.job_country),
        GATE_AGGREGATE_CAPACITY: aggregate_capacity_modelling_allowed(record.job_country),
        GATE_PERSON_ENRICHMENT: person_enrichment_allowed(record.contact_country, **conditions),
        GATE_COLD_EMAIL: cold_email_allowed(record.contact_country, **conditions),
    }
    cold = gates[GATE_COLD_EMAIL]
    return ComplianceDecision(
        outreach_eligible=cold.allowed,
        outreach_block_reason="" if cold.allowed else cold.reason,
        capacity_category=_category(cold),
        gates=gates,
    )


def describe() -> Dict[str, Any]:
    """The matrix as a manifest, for a run artifact or a report."""
    return {
        "compliance_rule_version": COMPLIANCE_RULE_VERSION,
        "countries": {k: {"job_acquisition_allowed": v.job_acquisition,
                          "aggregate_capacity_modelling_allowed": v.aggregate_capacity_modelling,
                          "person_enrichment_allowed": v.person_enrichment,
                          "cold_email_allowed": v.cold_email,
                          "person_enrichment_label": v.person_enrichment_label,
                          "cold_email_label": v.cold_email_label,
                          "note": v.note, "source": v.source}
                      for k, v in COUNTRY_PERMISSIONS.items()},
        "uk_processing_conditions": [{"reason": c.reason, "meaning": c.meaning} for c in UK_PROCESSING_CONDITIONS],
        "uk_send_conditions": [{"reason": c.reason, "meaning": c.meaning} for c in UK_SEND_CONDITIONS],
        "us_send_conditions": [{"reason": c.reason, "meaning": c.meaning} for c in US_SEND_CONDITIONS],
        "capacity_categories": list(CAPACITY_CATEGORIES),
    }
