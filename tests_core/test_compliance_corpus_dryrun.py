"""The corpus dry-run harness: the units it counts and the ones it refuses to.

``rebuild/audit_compliance_corpus.py`` runs the country gates over the purchased
records, in-process and offline. Two things about it need tests of their own,
because both are ways a dry-run report goes wrong rather than ways the gates go
wrong:

1. **It reuses the pipeline's own readers.** The organization block comes from
   ``services.acquisition.org_block`` and the jurisdiction from
   ``domain.jurisdiction``, so the report measures what the pipeline would do
   rather than what a second copy of the rules in a script would do.

2. **It never invents the unit it is missing.** The corpus is JOB records: it
   contains no contact, so ``contact_country`` is unknown for every row and the
   real cold-email verdict is unknown-jurisdiction for every row. The harness
   reports that, and reports the company-country counterfactual SEPARATELY and
   labelled, never added into the real one.
"""

from __future__ import annotations

import pytest

from rebuild.audit_compliance_corpus import CorpusRow, aggregate, classify_record
from tgtc_core.policy import compliance as c

UK_ROW = {"id": "1", "countries_derived": ["United Kingdom"], "organization": "Acme Ltd",
          "organization_url": "https://acme.co.uk", "org_linkedin_slug": "acme",
          "org_linkedin_locations": ["1 High St, London, GB"]}
DE_ROW = {"id": "2", "countries_derived": ["Germany"], "organization": "Beta GmbH",
          "organization_url": "https://beta.de", "org_linkedin_slug": "beta",
          "org_linkedin_locations": ["Friedrichstrasse 1, Berlin, DE"]}
FR_ROW = {"id": "3", "countries_derived": ["France"], "organization": "Gamma",
          "organization_url": "https://gamma.fr", "org_linkedin_slug": "gamma"}


def test_a_record_is_classified_from_its_own_provider_fields():
    row = classify_record(UK_ROW)
    assert row.job_country == "UK"
    assert row.company_country == "UK"
    assert row.corporate_subscriber_status == c.CORPORATE
    assert row.job_acquisition_status == c.ALLOWED
    assert row.aggregate_capacity_status == c.ALLOWED


def test_the_contact_gate_is_unknown_for_every_corpus_row_because_there_is_no_contact():
    """The corpus is job records. Pretending otherwise is the one way this
    report could produce a ready-to-send number that does not exist."""
    for raw in (UK_ROW, DE_ROW, FR_ROW):
        row = classify_record(raw)
        assert row.contact_country == ""
        assert row.cold_email_status == c.UNKNOWN_JURISDICTION
        assert row.capacity_category == c.CATEGORY_BLOCKED_UNKNOWN_JURISDICTION
        assert row.outreach_eligible is False


def test_person_enrichment_is_decidable_from_the_company_country():
    assert classify_record(DE_ROW).person_enrichment_status == c.DISABLED
    assert classify_record(UK_ROW).person_enrichment_status == c.CONDITIONAL
    assert classify_record(FR_ROW).person_enrichment_status == c.UNKNOWN_JURISDICTION


def test_a_country_outside_the_matrix_is_unknown_on_every_gate():
    row = classify_record(FR_ROW)
    assert row.job_country == "" and row.company_country == ""
    assert row.job_acquisition_status == c.UNKNOWN_JURISDICTION
    assert row.aggregate_capacity_status == c.UNKNOWN_JURISDICTION


def test_the_counterfactual_is_computed_separately_and_labelled():
    """"If the contact were in the company's country" is a projection, not a
    measurement. It is a different field so it can never be added into the
    measured one by accident."""
    assert classify_record(DE_ROW).counterfactual_cold_email_status == c.DISABLED
    assert classify_record(UK_ROW).counterfactual_cold_email_status == c.CONDITIONAL
    assert classify_record(UK_ROW).cold_email_status == c.UNKNOWN_JURISDICTION


def test_aggregate_reports_records_and_unique_jobs_as_separate_units():
    """Provider records returned and unique jobs are units 1 and 2 of the
    funnel. The same posting appears in more than one purchased arm, so they
    are never the same number and are never added together."""
    rows = [classify_record(UK_ROW), classify_record(UK_ROW), classify_record(DE_ROW)]
    out = aggregate(rows)
    assert out["totals"]["provider_records"] == 3
    assert out["totals"]["unique_jobs"] == 2
    assert out["totals"]["unique_companies"] == 2


def test_aggregate_splits_every_category_by_country_and_reconciles():
    rows = [classify_record(UK_ROW), classify_record(DE_ROW), classify_record(FR_ROW)]
    out = aggregate(rows)
    assert out["totals"]["outreach_eligible_contacts"] == 0
    assert out["totals"]["compliance_blocked_contacts"] == 3
    # Reconciliation: every unique job lands in exactly one capacity category.
    assert sum(out["capacity_category_by_unique_job"].values()) == out["totals"]["unique_jobs"]
    assert sum(out["job_country"].values()) == out["totals"]["unique_jobs"]


def test_aggregate_counts_the_two_fail_closed_reasons_separately():
    """"how many fail closed for unknown jurisdiction or unknown entity type"
    -- two different failures that must not be merged into one number."""
    rows = [classify_record(UK_ROW), classify_record(FR_ROW)]
    out = aggregate(rows)
    assert out["fail_closed"]["unknown_contact_jurisdiction"] == 2
    assert out["fail_closed"]["unknown_company_jurisdiction"] == 1
    assert out["fail_closed"]["unknown_entity_type"] == 1     # Gamma names no legal form
    assert out["entity_type_by_status"][c.CORPORATE] == 1     # Acme Ltd


def test_the_harness_makes_no_network_and_no_database_call():
    """Isolated dry run: in-process, over local files, never production."""
    import inspect

    import rebuild.audit_compliance_corpus as mod

    source = inspect.getsource(mod)
    for forbidden in ("psycopg", "requests", "urllib", "http", "connect(", "TGTC_DATABASE_URL"):
        assert forbidden not in source, f"harness references {forbidden!r}"


def test_a_corpus_row_carries_the_rule_version_it_was_decided_under():
    assert classify_record(UK_ROW).compliance_rule_version == c.COMPLIANCE_RULE_VERSION
    assert aggregate([classify_record(UK_ROW)])["compliance_rule_version"] == c.COMPLIANCE_RULE_VERSION


@pytest.mark.parametrize("raw", [{}, {"id": ""}, {"countries_derived": None}])
def test_a_degenerate_row_is_classified_rather_than_crashing_or_being_dropped(raw):
    row = classify_record(raw)
    assert isinstance(row, CorpusRow)
    assert row.outreach_eligible is False
