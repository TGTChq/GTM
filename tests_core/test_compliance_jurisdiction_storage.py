"""Jurisdiction is STORED, never inferred: migration 011 and its schema mirror.

"A job's location does NOT determine the contact's jurisdiction. Store
independently: job_country, company_country, contact_country,
employer_legal_entity_type, corporate_subscriber_status, compliance_rule_version,
legal_basis, legal_basis_evidence, privacy_notice_due_at, opt_out_status,
outreach_eligible, outreach_block_reason." -- COMPLIANCE_MATRIX.md

Storage is what makes that rule enforceable rather than aspirational: three
separate country columns on the approval, so no later reader can reconstruct one
from another, plus the employer- and person-level observations they are copied
from. Follows this branch's established migration pattern -- additive,
IF NOT EXISTS, nullable, NO backfill, mirrored into schema.sql (migrations
007-010).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tgtc_core.db import apply_schema
from tgtc_core.db.migrate import SCHEMA_PATH, applied_versions
from tests_core.helpers import sql1

#: table -> the columns migration 011 adds, and what each one is for.
COMPLIANCE_COLUMNS = {
    "employers": ("company_country", "employer_legal_entity_type", "corporate_subscriber_status"),
    "people": ("contact_country", "opt_out_status"),
    "approvals": ("job_country", "company_country", "contact_country", "employer_legal_entity_type",
                  "corporate_subscriber_status", "compliance_rule_version", "legal_basis",
                  "legal_basis_evidence", "privacy_notice_due_at", "opt_out_status",
                  "outreach_eligible", "outreach_block_reason"),
}

#: Every field the matrix names, and the table that holds the per-lead copy.
MATRIX_FIELDS = COMPLIANCE_COLUMNS["approvals"]


def _column(conn, table: str, column: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s", (table, column))
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


@pytest.mark.parametrize("table,column", [(t, c) for t, cols in COMPLIANCE_COLUMNS.items() for c in cols])
def test_every_jurisdiction_column_exists_is_nullable_and_has_no_default(conn, table, column):
    """Nullable with no default IS the "no backfill" guarantee: an existing row
    reads NULL (never observed), which every gate treats as unknown and fails
    closed on, rather than an invented value that would read as a decision."""
    info = _column(conn, table, column)
    assert info is not None, f"{table}.{column} missing"
    assert info["is_nullable"] == "YES"
    assert info["column_default"] is None


def test_the_approval_holds_all_twelve_matrix_fields(conn):
    assert len(MATRIX_FIELDS) == 12
    for column in MATRIX_FIELDS:
        assert _column(conn, "approvals", column) is not None


def test_the_three_country_fields_are_three_separate_columns(conn):
    """The load-bearing one. If job_country, company_country and contact_country
    were one column, "stored, never inferred" could not be checked at all."""
    columns = {c for c in MATRIX_FIELDS if c.endswith("_country")}
    assert columns == {"job_country", "company_country", "contact_country"}
    for column in columns:
        assert _column(conn, "approvals", column)["data_type"] == "text"


def test_outreach_eligible_is_a_boolean_that_starts_unknown(conn):
    info = _column(conn, "approvals", "outreach_eligible")
    assert info["data_type"] == "boolean" and info["is_nullable"] == "YES" and info["column_default"] is None


def test_privacy_notice_due_at_is_a_timestamp(conn):
    assert _column(conn, "approvals", "privacy_notice_due_at")["data_type"] == "timestamp with time zone"


def test_migration_011_is_applied_and_recorded(conn):
    assert 11 in applied_versions(conn)


def test_reapplying_the_schema_is_harmless(conn):
    apply_schema(conn)
    apply_schema(conn)
    assert _column(conn, "approvals", "contact_country") is not None
    assert sql1(conn, "SELECT count(*) FROM approvals") == 0


@pytest.mark.parametrize("table,column", [(t, c) for t, cols in COMPLIANCE_COLUMNS.items() for c in cols])
def test_schema_sql_mirrors_the_migration_for_fresh_installs(table, column):
    """apply_schema() runs schema.sql FIRST on every call, so a fresh install
    that never replays 011 must still get these columns (the precedent every
    migration 007-010 follows)."""
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    assert column in text, f"{table}.{column} is not mirrored into schema.sql"


def test_the_migration_is_additive_only():
    """No DROP, no UPDATE, no DELETE: an additive migration can never destroy a
    record, which is the storage half of "never delete it"."""
    sql = Path(SCHEMA_PATH.parent, "migrations", "011_country_compliance_jurisdiction.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--")).lower()
    for forbidden in (" drop ", "drop table", "drop column", "update ", "delete ", "truncate", "not null"):
        assert forbidden not in body, f"migration 011 contains {forbidden!r}"
    assert body.count("add column if not exists") == sum(len(v) for v in COMPLIANCE_COLUMNS.values())
