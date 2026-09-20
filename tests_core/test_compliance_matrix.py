"""Country permission matrix `tgtc-compliance/1` -- the PURE policy layer.

Specification: ``funnel_audit_20260919/COMPLIANCE_MATRIX.md`` (authoritative,
researched, supplied by Luis 2026-09-20). It supersedes the earlier blanket
"only the United States has a legal basis", which wrongly erased four markets
from capacity analysis.

Three things this file exists to prove, because each one has a specific way of
being got wrong:

1. **Acquisition, aggregate capacity modelling, person enrichment and cold
   email are FOUR independent permissions.** A single generic geography flag is
   explicitly forbidden: DE/AE/SA are `yes` for acquisition and capacity and
   `no` for enrichment and outreach, so any code that collapses them either
   deletes four markets from capacity analysis or emails into a jurisdiction
   that forbids it. There is a deterministic test per gate per country below
   (4 x 5 = 20 cells, enumerated from the matrix table, not from the
   implementation).

2. **Jurisdiction is an argument, never a fallback.** Every gate takes the
   country it decides on as its first positional argument. Nothing in
   ``policy/compliance.py`` may read one country field when another is missing;
   ``evaluate`` is the only place that binds record fields to gates, and it
   binds ``job_country`` to the two job gates and ``contact_country`` to the two
   person gates, with no fallback between them.

3. **Unknown fails closed for sending and open for capacity.** An unknown or
   out-of-matrix jurisdiction is never `allowed`; the record is still retained,
   categorised and counted, never dropped and never counted as ready to send.
"""

from __future__ import annotations

import pytest

from tgtc_core.policy import compliance as c


# ---------------------------------------------------------------------------
# The matrix itself, transcribed from COMPLIANCE_MATRIX.md's table. Written
# out here rather than imported so a silent edit to the policy data fails a
# test instead of quietly changing what the pipeline is permitted to do.
# ---------------------------------------------------------------------------
MATRIX = {
    #            acquisition  capacity     enrichment          cold email
    "US": (c.ALLOWED, c.ALLOWED, c.ALLOWED, c.CONDITIONAL),
    "UK": (c.ALLOWED, c.ALLOWED, c.CONDITIONAL, c.CONDITIONAL),
    "DE": (c.ALLOWED, c.ALLOWED, c.DISABLED, c.DISABLED),
    "AE": (c.ALLOWED, c.ALLOWED, c.DISABLED, c.DISABLED),
    "SA": (c.ALLOWED, c.ALLOWED, c.DISABLED, c.DISABLED),
}

#: The matrix's own vocabulary for the four disabled cold-email cells.
COLD_EMAIL_LABELS = {
    "US": "ENABLED_CONDITIONAL",
    "UK": "ENABLED_CONDITIONAL_CORPORATE_SUBSCRIBERS_ONLY",
    "DE": "DISABLED_PENDING_CONSENT",
    "AE": "DISABLED_PENDING_DOCUMENTED_OPT_IN",
    "SA": "DISABLED_PENDING_CONSENT",
}


def _uk_ready(**over):
    """Every UK condition satisfied; each test then breaks exactly one."""
    kwargs = dict(
        corporate_subscriber_status=c.CORPORATE,
        employer_legal_entity_type="Limited",
        legal_basis="legitimate_interests",
        legal_basis_evidence="lia-2026-09-20",
        privacy_notice_configured=True,
        privacy_notice_due_at="2026-10-18T00:00:00Z",
        opt_out_status=c.OPT_OUT_NONE,
        email="buyer@acme.co.uk",
        email_alignment="EXACT_EMPLOYER_DOMAIN",
        email_status="verified",
        unsubscribe_available=True,
        suppression_available=True,
    )
    kwargs.update(over)
    return kwargs


# ---------------------------------------------------------------------------
# 1. Four independent gates, one deterministic test per country gate.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("country", sorted(MATRIX))
def test_job_acquisition_gate_per_country(country):
    d = c.job_acquisition_allowed(country)
    assert d.gate == c.GATE_JOB_ACQUISITION
    assert d.status == MATRIX[country][0]
    assert d.allowed is (MATRIX[country][0] == c.ALLOWED)
    assert d.rule_version == c.COMPLIANCE_RULE_VERSION


@pytest.mark.parametrize("country", sorted(MATRIX))
def test_aggregate_capacity_modelling_gate_per_country(country):
    d = c.aggregate_capacity_modelling_allowed(country)
    assert d.gate == c.GATE_AGGREGATE_CAPACITY
    assert d.status == MATRIX[country][1]
    assert d.allowed is (MATRIX[country][1] == c.ALLOWED)


@pytest.mark.parametrize("country", sorted(MATRIX))
def test_person_enrichment_gate_per_country(country):
    """UK is CONDITIONAL, so it is evaluated with its conditions satisfied; the
    per-condition failures are their own tests below."""
    d = c.person_enrichment_allowed(country, **_uk_ready())
    assert d.gate == c.GATE_PERSON_ENRICHMENT
    assert d.status == MATRIX[country][2]
    assert d.allowed is (country in {"US", "UK"})


@pytest.mark.parametrize("country", sorted(MATRIX))
def test_cold_email_gate_per_country(country):
    d = c.cold_email_allowed(country, **_uk_ready())
    assert d.gate == c.GATE_COLD_EMAIL
    assert d.status == MATRIX[country][3]
    assert d.allowed is (country in {"US", "UK"})
    assert d.matrix_label == COLD_EMAIL_LABELS[country]


@pytest.mark.parametrize("country", ["DE", "AE", "SA"])
def test_the_three_blocked_countries_are_acquirable_and_modellable_but_never_emailable(country):
    """The whole point of the matrix superseding "only the US has a legal
    basis": these four markets stay in capacity analysis."""
    assert c.job_acquisition_allowed(country).allowed is True
    assert c.aggregate_capacity_modelling_allowed(country).allowed is True
    assert c.person_enrichment_allowed(country, **_uk_ready()).allowed is False
    assert c.cold_email_allowed(country, **_uk_ready()).allowed is False


def test_no_single_generic_geography_flag_exists():
    """A generic `geography_allowed`/`country_allowed` helper is forbidden: it
    is precisely the collapse the matrix says never to make."""
    banned = {"geography_allowed", "country_allowed", "geo_allowed", "country_permitted"}
    assert banned.isdisjoint(dir(c))


# ---------------------------------------------------------------------------
# 2. Country normalisation, and out-of-matrix / unknown jurisdictions.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("US", "US"), ("us", "US"), ("USA", "US"), ("United States", "US"), ("United States of America", "US"),
    ("UK", "UK"), ("GB", "UK"), ("gb", "UK"), ("United Kingdom", "UK"), ("Great Britain", "UK"),
    ("DE", "DE"), ("Germany", "DE"), ("Deutschland", "DE"),
    ("AE", "AE"), ("United Arab Emirates", "AE"), ("UAE", "AE"),
    ("SA", "SA"), ("Saudi Arabia", "SA"), ("KSA", "SA"),
])
def test_country_normalisation(raw, expected):
    assert c.normalize_country(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "   ", "Narnia", "FR", "France", "CA", "Canada"])
def test_a_country_outside_the_matrix_normalises_to_unknown(raw):
    assert c.normalize_country(raw) == ""


@pytest.mark.parametrize("gate", [
    c.job_acquisition_allowed, c.aggregate_capacity_modelling_allowed,
])
@pytest.mark.parametrize("raw", ["", None, "France"])
def test_every_job_gate_fails_closed_on_an_unknown_jurisdiction(gate, raw):
    d = gate(raw)
    assert d.status == c.UNKNOWN_JURISDICTION
    assert d.allowed is False
    assert d.reason.startswith("compliance:unknown_jurisdiction")


@pytest.mark.parametrize("raw", ["", None, "France"])
def test_person_enrichment_fails_closed_on_an_unknown_jurisdiction(raw):
    d = c.person_enrichment_allowed(raw, **_uk_ready())
    assert d.status == c.UNKNOWN_JURISDICTION and d.allowed is False


@pytest.mark.parametrize("raw", ["", None, "France"])
def test_cold_email_fails_closed_on_an_unknown_jurisdiction(raw):
    d = c.cold_email_allowed(raw, **_uk_ready())
    assert d.status == c.UNKNOWN_JURISDICTION and d.allowed is False
    assert d.reason.startswith("compliance:unknown_jurisdiction")


# ---------------------------------------------------------------------------
# 3. The UK deterministic gate: every condition, each one decisive on its own.
# ---------------------------------------------------------------------------

def test_the_uk_gate_passes_only_when_every_condition_holds():
    assert c.cold_email_allowed("UK", **_uk_ready()).allowed is True


@pytest.mark.parametrize("over,reason", [
    ({"corporate_subscriber_status": c.ENTITY_UNKNOWN}, "uk:not_a_verified_corporate_subscriber"),
    ({"employer_legal_entity_type": ""}, "uk:not_a_verified_corporate_subscriber"),
    ({"employer_legal_entity_type": "Sole Trader"}, "uk:not_a_verified_corporate_subscriber"),
    ({"employer_legal_entity_type": "General Partnership"}, "uk:not_a_verified_corporate_subscriber"),
    ({"legal_basis": ""}, "uk:no_lawful_basis_record"),
    ({"legal_basis_evidence": ""}, "uk:no_lawful_basis_record"),
    ({"privacy_notice_configured": False}, "uk:privacy_notice_process_not_configured"),
    ({"privacy_notice_due_at": None}, "uk:privacy_notice_not_due_dated"),
    ({"opt_out_status": c.OPTED_OUT}, "outreach:historically_opted_out"),
    ({"email_alignment": "CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN"}, "uk:not_an_exact_domain_verified_work_email"),
    ({"email_status": "unknown"}, "uk:not_an_exact_domain_verified_work_email"),
    ({"email": "buyer@gmail.com", "email_alignment": ""}, "uk:not_an_exact_domain_verified_work_email"),
    ({"unsubscribe_available": False}, "outreach:unsubscribe_not_available"),
    ({"suppression_available": False}, "outreach:suppression_not_available"),
])
def test_each_uk_condition_is_decisive_on_its_own(over, reason):
    d = c.cold_email_allowed("UK", **_uk_ready(**over))
    assert d.allowed is False
    assert d.reason == f"compliance:{reason}", d.reason


def test_a_free_mail_domain_is_never_a_uk_work_email_even_when_apollo_calls_it_exact():
    """"no personal or free email domain" is its own condition: a provider that
    reports an exact-domain match on a free-mail address must not satisfy it."""
    d = c.cold_email_allowed("UK", **_uk_ready(email="buyer@gmail.com"))
    assert d.allowed is False
    assert d.reason == "compliance:uk:personal_or_free_email_domain"


@pytest.mark.parametrize("entity", ["", None, "   ", "Unknown", "Privately Held", "Public Company", "Nonprofit"])
def test_an_unknown_or_self_declared_entity_type_is_never_corporate(entity):
    """"An unknown entity type is never treated as corporate." LinkedIn's own
    ownership descriptors ("Privately Held", "Public Company") are self-declared
    and are NOT a verified legal form, so they classify as unknown, not
    corporate."""
    assert c.classify_corporate_subscriber(entity) == c.ENTITY_UNKNOWN
    d = c.cold_email_allowed("UK", **_uk_ready(employer_legal_entity_type=entity,
                                               corporate_subscriber_status=c.CORPORATE))
    assert d.allowed is False
    assert d.reason == "compliance:uk:not_a_verified_corporate_subscriber"


@pytest.mark.parametrize("entity", ["Sole Trader", "sole proprietorship", "Self-Employed",
                                    "General Partnership", "Unincorporated Association"])
def test_the_excluded_uk_legal_forms_are_never_corporate(entity):
    assert c.classify_corporate_subscriber(entity) == c.NOT_CORPORATE


@pytest.mark.parametrize("entity", ["Ltd", "Limited", "ACME LIMITED", "PLC", "LLP",
                                    "Limited Liability Partnership", "Community Interest Company"])
def test_the_recognised_uk_corporate_legal_forms_classify_as_corporate(entity):
    assert c.classify_corporate_subscriber(entity) == c.CORPORATE


def test_a_stored_corporate_status_alone_cannot_override_the_entity_type():
    """The stored ``corporate_subscriber_status`` is a cross-check, never the
    sole authority: both it and the legal form must say corporate."""
    d = c.cold_email_allowed("UK", **_uk_ready(employer_legal_entity_type="Sole Trader",
                                               corporate_subscriber_status=c.CORPORATE))
    assert d.allowed is False


def test_uk_person_enrichment_needs_the_processing_conditions_but_not_the_send_ones():
    """Enrichment is a PROCESSING permission: it needs a corporate subscriber, a
    lawful basis and a privacy-notice process. It does not need an unsubscribe
    mechanism or a verified work email -- those do not exist yet at enrichment
    time and are conditions of the SEND."""
    assert c.person_enrichment_allowed("UK", **_uk_ready(unsubscribe_available=False,
                                                         email="", email_alignment="", email_status="")).allowed is True
    assert c.person_enrichment_allowed("UK", **_uk_ready(legal_basis="")).allowed is False
    assert c.person_enrichment_allowed("UK", **_uk_ready(corporate_subscriber_status=c.ENTITY_UNKNOWN)).allowed is False


# ---------------------------------------------------------------------------
# 4. The US conditional gate.
# ---------------------------------------------------------------------------

def test_us_cold_email_needs_no_lawful_basis_record_but_does_need_the_send_controls():
    """CAN-SPAM is an opt-out regime: no lawful-basis record and no corporate
    subscriber test, but suppression before every send, a visible unsubscribe
    and honoured opt-outs."""
    ok = _uk_ready(legal_basis="", legal_basis_evidence="", privacy_notice_configured=False,
                   privacy_notice_due_at=None, corporate_subscriber_status=c.ENTITY_UNKNOWN,
                   employer_legal_entity_type="")
    assert c.cold_email_allowed("US", **ok).allowed is True
    assert c.cold_email_allowed("US", **dict(ok, unsubscribe_available=False)).allowed is False
    assert c.cold_email_allowed("US", **dict(ok, suppression_available=False)).allowed is False
    assert c.cold_email_allowed("US", **dict(ok, opt_out_status=c.OPTED_OUT)).allowed is False


def test_us_person_enrichment_is_unconditional():
    assert c.person_enrichment_allowed("US").allowed is True


def test_the_shared_send_conditions_are_the_same_objects_for_us_and_uk():
    """Share a predicate, never copy it: opt-out, unsubscribe and suppression
    are ONE definition each, referenced by both regimes."""
    shared = set(c.US_SEND_CONDITIONS)
    assert shared and shared.issubset(set(c.UK_SEND_CONDITIONS))


# ---------------------------------------------------------------------------
# 5. Record-level evaluation: fail closed for sending, open for capacity.
# ---------------------------------------------------------------------------

def _record(**over) -> c.ComplianceRecord:
    base = dict(job_country="UK", company_country="UK", contact_country="UK", **_uk_ready())
    base.pop("privacy_notice_due_at", None)
    base["privacy_notice_due_at"] = "2026-10-18T00:00:00Z"
    base.update(over)
    return c.ComplianceRecord(**base)


def test_a_fully_satisfied_uk_record_is_outreach_eligible():
    d = c.evaluate(_record())
    assert d.outreach_eligible is True
    assert d.outreach_block_reason == ""
    assert d.capacity_category == c.CATEGORY_OUTREACH_ELIGIBLE
    assert d.compliance_rule_version == c.COMPLIANCE_RULE_VERSION


def test_an_unknown_contact_country_is_not_eligible_but_is_retained_for_capacity():
    d = c.evaluate(_record(contact_country=""))
    assert d.outreach_eligible is False
    assert d.retained_for_capacity is True
    assert d.capacity_category == c.CATEGORY_BLOCKED_UNKNOWN_JURISDICTION
    assert d.outreach_block_reason.startswith("compliance:unknown_jurisdiction")


def test_an_unknown_uk_corporate_status_is_not_eligible_but_is_retained_for_capacity():
    d = c.evaluate(_record(corporate_subscriber_status=c.ENTITY_UNKNOWN, employer_legal_entity_type=""))
    assert d.outreach_eligible is False
    assert d.retained_for_capacity is True
    assert d.capacity_category == c.CATEGORY_BLOCKED_ENTITY_UNKNOWN
    assert d.outreach_block_reason == "compliance:uk:not_a_verified_corporate_subscriber"


@pytest.mark.parametrize("country", ["DE", "AE", "SA"])
def test_a_de_ae_sa_contact_is_blocked_capacity_never_eligible(country):
    d = c.evaluate(_record(job_country=country, company_country=country, contact_country=country))
    assert d.outreach_eligible is False
    assert d.retained_for_capacity is True
    assert d.capacity_category == c.CATEGORY_BLOCKED_CAPACITY
    # Still acquirable and still modellable -- the record is not erased.
    assert d.gates[c.GATE_JOB_ACQUISITION].allowed is True
    assert d.gates[c.GATE_AGGREGATE_CAPACITY].allowed is True


def test_a_job_in_one_country_never_decides_the_contacts_jurisdiction():
    """The single most important property of the whole module: a US job with a
    German contact is a German contact."""
    d = c.evaluate(_record(job_country="US", company_country="US", contact_country="DE"))
    assert d.gates[c.GATE_JOB_ACQUISITION].country == "US"
    assert d.gates[c.GATE_COLD_EMAIL].country == "DE"
    assert d.outreach_eligible is False
    assert d.capacity_category == c.CATEGORY_BLOCKED_CAPACITY


def test_a_german_job_with_a_us_contact_is_decided_on_the_us_contact():
    """And the converse, which is the reason a generic geography flag is
    forbidden rather than merely discouraged."""
    d = c.evaluate(_record(job_country="DE", company_country="DE", contact_country="US",
                           legal_basis="", legal_basis_evidence="", privacy_notice_configured=False,
                           corporate_subscriber_status=c.ENTITY_UNKNOWN, employer_legal_entity_type=""))
    assert d.gates[c.GATE_COLD_EMAIL].country == "US"
    assert d.outreach_eligible is True


def test_every_gate_decision_is_reported_for_every_record():
    d = c.evaluate(_record())
    assert set(d.gates) == set(c.GATES)


def test_evaluate_never_falls_back_from_one_country_field_to_another():
    """`company_country` is stored and reported, but it is NOT what the person
    gates decide on -- otherwise "stored, never inferred" is a slogan."""
    d = c.evaluate(_record(contact_country="", company_country="US"))
    assert d.gates[c.GATE_COLD_EMAIL].country == ""
    assert d.outreach_eligible is False
