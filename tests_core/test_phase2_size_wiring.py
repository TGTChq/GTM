"""Phase 2 audit task 5c (2026-09-20): wire the three-state company-size policy
(facts.py tasks 4-5, ``size_state``/``resolve_company_size``) into the live
pipeline and measure the real effect.

A review established that no production caller passed ``company=`` to
``extract_job_facts``, so the policy changed nothing live -- every size figure
reported for it came from an offline replay. This file is the TDD anchor: the
first test below proves the CURRENT live path (``services.opportunity
.OpportunityService.process``, called from ``Runner.work("qualify_opportunity")``
-- the genuinely reachable size gate; see the report for the full traced call
graph) rejects a conflicting row wrongly, using only ``employers.employee_count``
and never looking at the employer's own declared LinkedIn size band.

See .superpowers/sdd/phase2/task-5c-brief.md and task-5c-report.md.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tgtc_core.db import apply_schema
from tgtc_core.db.connection import jsonb
from tgtc_core.domain.facts import (
    TARGET_MAX_EMPLOYEES, TARGET_MIN_EMPLOYEES, resolve_company_size, size_reject_reason, size_state,
)
from tgtc_core.domain import approval as ap
from tgtc_core.services.opportunity import OpportunityService
from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, seed_opportunity
from tests_core.test_approval_gate import _inputs


# ---------------------------------------------------------------------------
# TDD anchor: the CURRENT live path (services/opportunity.py, called from
# both the pre- and post-enrichment points in process()) rejects a
# conflicting row wrongly, reading employee_count alone.
# ---------------------------------------------------------------------------

def _size_gate_svc():
    svc = OpportunityService.__new__(OpportunityService)
    svc.conn = MagicMock()  # only the conflict/unknown branch touches this (an evidence insert)
    closed = {}

    def _close(oid, reason):
        closed["reason"] = reason
        return ("closed", reason)

    svc._close = _close
    return svc, closed


def test_size_gate_does_not_reject_a_firmographic_conflict():
    """headcount alone (5000) reads employer_too_large; the employer's own
    declared LinkedIn size band (51-200 employees) reads clearly in range.
    Decision 2 (2026-09-19): sources conflicting across the boundary must
    become firmographic_conflict, never a reject -- 'never discard a
    potentially eligible company merely because two sources conflict.'

    Before this task's services/opportunity.py fix, ``process()``'s size
    check read ``emp.get("employee_count")`` alone (at both the pre- and
    post-enrichment points) and closed the opportunity as
    ``employer_too_large`` -- this is the real defect this task closes; this
    test is the TDD anchor the brief asked for."""
    svc, closed = _size_gate_svc()
    emp = {"id": 1, "employee_count": 5000, "size_band": "51-200 employees"}
    posting = {"description_text": "Own the customer success motion end to end."}
    outcome = svc._size_gate(1, emp, posting)
    assert outcome is None and closed == {}


def test_size_gate_still_rejects_a_genuine_single_source_out_of_range():
    """Regression: no size_band at all (single reliable source) must still
    reject exactly as before this task."""
    svc, closed = _size_gate_svc()
    emp = {"id": 1, "employee_count": 5000, "size_band": None}
    outcome = svc._size_gate(1, emp, {"description_text": ""})
    assert outcome == ("closed", "employer_too_large")
    assert closed["reason"] == "employer_too_large"


def test_size_gate_still_rejects_when_every_source_agrees_outside():
    """Both sources populated and agreeing outside (not a conflict) must still
    reject -- size_state's "out_of_range", not "firmographic_conflict"."""
    svc, closed = _size_gate_svc()
    emp = {"id": 1, "employee_count": 5000, "size_band": "1,001-5,000 employees"}
    outcome = svc._size_gate(1, emp, {"description_text": ""})
    assert outcome == ("closed", "employer_too_large")


@pytest.mark.parametrize("count,reason", [(12, "employer_too_small"), (5000, "employer_too_large")])
def test_size_gate_still_rejects_too_small_and_too_large(count, reason):
    svc, closed = _size_gate_svc()
    emp = {"id": 1, "employee_count": count, "size_band": None}
    outcome = svc._size_gate(1, emp, {"description_text": ""})
    assert outcome == ("closed", reason)


def test_size_gate_records_a_conflict_as_evidence_not_a_silent_drop():
    svc, _closed = _size_gate_svc()
    emp = {"id": 42, "employee_count": 5000, "size_band": "51-200 employees"}
    outcome = svc._size_gate(1, emp, {"description_text": ""})
    assert outcome is None
    (call,) = [c for c in svc.conn.cursor().__enter__().execute.call_args_list]
    sql, params = call.args
    assert "INSERT INTO evidence" in sql
    assert params[0] == 42 and params[1] == "company:firmographic_conflict"


# ---------------------------------------------------------------------------
# End-to-end through the real database: identity resolution persists the
# declared size band, and OpportunityService.process reads it back and does
# not reject -- proving the wiring reaches the ACTUAL live tables, not just
# the in-memory gate function.
# ---------------------------------------------------------------------------

def test_live_pipeline_persists_size_band_and_lets_a_conflict_through(conn, clock):
    pid, eid, oid = seed_opportunity(
        conn, clock, function_key="customer_success", domain="conflict.example", org_name="Conflict Co",
        headcount=5000, size_band="51-200 employees",
    )
    assert eid is not None and oid is not None, "identity/classification must not reject on size (unchanged)"
    assert sql1(conn, "SELECT size_band FROM employers WHERE id = %s", (eid,)) == "51-200 employees"
    assert sql1(conn, "SELECT employee_count FROM employers WHERE id = %s", (eid,)) == 5000

    svc = opportunity_service(conn, apollo_for("conflict.example", "Conflict Co", headcount=5000), clock)
    out = svc.process(oid)
    assert not (out.outcome == "closed" and out.reason in ("employer_too_small", "employer_too_large")), out.reason

    evidence = sqlall(conn, "SELECT fact FROM evidence WHERE subject_kind = 'employer' AND subject_id = %s", (eid,))
    assert any(e["fact"] == "company:firmographic_conflict" for e in evidence), \
        "a conflict must be recorded in its own reported bucket, never silently dropped"


def test_live_pipeline_still_rejects_a_genuine_out_of_range_employer(conn, clock):
    pid, eid, oid = seed_opportunity(
        conn, clock, function_key="customer_success", domain="toolarge.example", org_name="Too Large Co",
        headcount=5000, size_band="1,001-5,000 employees",
    )
    assert eid is not None and oid is not None
    svc = opportunity_service(conn, apollo_for("toolarge.example", "Too Large Co", headcount=5000), clock)
    out = svc.process(oid)
    assert out.outcome == "closed" and out.reason == "employer_too_large"


# ---------------------------------------------------------------------------
# domain/approval.py: the final gate before delivery shares the same predicate.
# ---------------------------------------------------------------------------

def test_approval_gate_does_not_refuse_a_firmographic_conflict():
    inputs = _inputs()
    inputs["employer"].update(employee_count=5000, size_band="51-200 employees")
    out = ap.build_approved_lead(**inputs)
    assert isinstance(out, ap.ApprovedLead)


def test_approval_gate_still_refuses_a_genuine_single_source_out_of_range():
    inputs = _inputs()
    inputs["employer"].update(employee_count=5000)
    out = ap.build_approved_lead(**inputs)
    assert isinstance(out, ap.ApprovalRefusal) and out.reason == "employer_too_large"


def test_approval_gate_still_refuses_when_every_source_agrees_outside():
    inputs = _inputs()
    inputs["employer"].update(employee_count=5000, size_band="1,001-5,000 employees")
    out = ap.build_approved_lead(**inputs)
    assert isinstance(out, ap.ApprovalRefusal) and out.reason == "employer_too_large"


# ---------------------------------------------------------------------------
# facts.py: the shared predicate itself, and field-name reconciliation.
# ---------------------------------------------------------------------------

def test_resolve_company_size_matches_size_state_when_no_resolution_is_needed():
    for hc, band in ((400, "201-500"), (5000, "1,001-5,000"), (None, None)):
        state, _excerpt, _eff = resolve_company_size(hc, band)
        assert state == size_state(headcount=hc, size_band=band)


def test_resolve_company_size_reports_the_effective_headcount_that_decided_it():
    state, _excerpt, effective = resolve_company_size(5000, None)
    assert state == "out_of_range" and effective == 5000
    state, _excerpt, effective = resolve_company_size(
        400, "1,001-5,000", description="We are a company of about 3,000 employees worldwide.")
    assert state == "out_of_range" and effective == 3000


def test_size_reject_reason_names_the_correct_boundary():
    assert size_reject_reason(TARGET_MIN_EMPLOYEES - 1) == "employer_too_small"
    assert size_reject_reason(TARGET_MAX_EMPLOYEES + 1) == "employer_too_large"
    assert size_reject_reason(None) == "employer_size_out_of_range"


# ---------------------------------------------------------------------------
# Fix round 1 (2026-09-20, independent review, Changes Requested)
# ---------------------------------------------------------------------------

# I1 (IMPORTANT): the live gates must read policy.requirements.rule(...), not
# facts.py's own hardcoded TARGET_MIN/MAX_EMPLOYEES -- otherwise describe()'s
# policy manifest silently stops governing the two gates that matter.

def _custom_rule(**overrides):
    """The real `rule()`, with the given keys overridden -- so a test can move
    just min_employees/max_employees without breaking every OTHER rule() call
    the same function makes (require_contact_linkedin, approval_max_age_days...)."""
    from tgtc_core.policy.requirements import rule as real_rule

    def fake(key):
        return overrides[key] if key in overrides else real_rule(key)
    return fake


def test_size_gate_uses_rule_min_max_not_hardcoded_constants(monkeypatch):
    import tgtc_core.services.opportunity as opp_module
    monkeypatch.setattr(opp_module, "rule", _custom_rule(min_employees=100))
    svc, closed = _size_gate_svc()
    # 50 is inside facts.py's own hardcoded default (25-1,000) but OUTSIDE the
    # policy-manifest min_employees=100 this test injects -- if the gate is
    # still reading the hardcoded default, this proceeds instead of rejecting.
    emp = {"id": 1, "employee_count": 50, "size_band": None}
    outcome = svc._size_gate(1, emp, {"description_text": ""})
    assert outcome == ("closed", "employer_too_small")
    assert closed["reason"] == "employer_too_small"


def test_approval_gate_uses_rule_min_max_not_hardcoded_constants(monkeypatch):
    import tgtc_core.domain.approval as approval_module
    monkeypatch.setattr(approval_module, "rule", _custom_rule(max_employees=40))
    inputs = _inputs()
    inputs["employer"].update(employee_count=50)  # inside the default 25-1,000, outside policy max=40
    out = ap.build_approved_lead(**inputs)
    assert isinstance(out, ap.ApprovalRefusal) and out.reason == "employer_too_large"


# I3 (IMPORTANT): the conflict/unknown evidence bucket must not duplicate a
# row per pass/call-site, and must carry a rule_version.

def test_size_gate_conflict_evidence_is_deduped_and_carries_rule_version(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO employers (canonical_name, name_key, domain, employee_count, size_band) "
            "VALUES ('Acme', 'acme', 'acme.com', 5000, '51-200 employees') RETURNING id"
        )
        eid = int(cur.fetchone()["id"])
    conn.commit()
    svc = OpportunityService.__new__(OpportunityService)
    svc.conn = conn
    emp = {"id": eid, "employee_count": 5000, "size_band": "51-200 employees"}
    posting = {"description_text": ""}
    # Mirrors process() calling _size_gate at both the pre- and post-enrichment
    # points, and a retried/rescheduled pass calling it again later.
    svc._size_gate(1, emp, posting)
    svc._size_gate(1, emp, posting)
    svc._size_gate(2, emp, posting)
    rows = sqlall(conn, "SELECT fact, value FROM evidence WHERE subject_kind = 'employer' AND subject_id = %s", (eid,))
    assert len(rows) == 1, "one conflicting employer must not accumulate a row per pass/call-site/retry"
    assert rows[0]["fact"] == "company:firmographic_conflict"
    assert rows[0]["value"].get("rule_version")


# I4 (IMPORTANT): resolve_company_size's docstring must not claim a caller
# that does not exist.

def test_resolve_company_size_docstring_names_only_real_callers():
    import tgtc_core.domain.facts as facts_module
    doc = facts_module.resolve_company_size.__doc__ or ""
    assert "services/acquisition.py" not in doc


# C1 (CRITICAL): a firmographic_conflict/unknown_firmographics approval must
# not enter the confirmed-25-1,000 KPI, and must not ship a confident size
# label to Airtable/Instantly.

def test_approved_lead_marks_a_conflict_as_not_confirmed_in_range():
    inputs = _inputs()
    inputs["employer"].update(employee_count=5000, size_band="51-200 employees")
    out = ap.build_approved_lead(**inputs)
    assert isinstance(out, ap.ApprovedLead)
    assert out.lead["company_size_state"] == "firmographic_conflict"


def test_approved_lead_marks_a_confirmed_in_range_employer_as_such():
    out = ap.build_approved_lead(**_inputs())  # default employee_count=120, no size_band -> in_range
    assert out.lead["company_size_state"] == "in_range"


def test_delivery_payloads_suppress_size_fields_when_not_confirmed():
    inputs = _inputs()
    inputs["employer"].update(employee_count=5000, size_band="51-200 employees")
    out = ap.build_approved_lead(**inputs)
    fields = ap.airtable_fields(out.lead, out.fingerprint)
    assert "Employees" not in fields and "Size Band" not in fields
    payload = ap.instantly_payload(out.lead, skip_if_in_workspace=True, verify_on_import=False)
    variables = payload["custom_variables"]
    assert "company_size" not in variables and "company_size_band" not in variables


def test_delivery_payloads_still_assert_size_when_confirmed_in_range():
    out = ap.build_approved_lead(**_inputs())
    fields = ap.airtable_fields(out.lead, out.fingerprint)
    assert fields["Employees"] == 120 and fields["Size Band"]
    payload = ap.instantly_payload(out.lead, skip_if_in_workspace=True, verify_on_import=False)
    variables = payload["custom_variables"]
    assert variables["company_size"] == 120 and variables["company_size_band"]


def test_live_pipeline_approval_persists_company_size_state_and_ledger_splits_confirmed_vs_review(conn, clock):
    pid, eid, oid = seed_opportunity(
        conn, clock, function_key="customer_success", domain="conflictledger.example", org_name="Conflict Ledger Co",
        headcount=5000, size_band="51-200 employees",
    )
    assert eid is not None and oid is not None
    svc = opportunity_service(conn, apollo_for("conflictledger.example", "Conflict Ledger Co", headcount=5000), clock)
    out = svc.process(oid)
    assert out.outcome == "approved", out

    state = sql1(conn, "SELECT company_size_state FROM approvals WHERE opportunity_id = %s", (oid,))
    assert state == "firmographic_conflict"

    from tgtc_core.services.metrics import ledger
    report = ledger(conn)
    assert report["approved_distinct"] == 1
    assert report["approved_confirmed_size"] == 0, "a conflict must not be counted as confirmed 25-1,000"
    assert report["approved_review_size"] == 1


# M2 (MINOR, fix in this round): testing/fakes.py::make_posting_row must not
# hardcode a size band inconsistent with whatever headcount= it is given --
# a landmine that silently builds a conflicting fixture for the next task.

# ---------------------------------------------------------------------------
# Fix round 2 (2026-09-20, independent review, Changes Requested)
# ---------------------------------------------------------------------------

# I3 (IMPORTANT, partially addressed in round 1): migration 009's
# `CREATE UNIQUE INDEX` has no pre-dedup step. A database where the pre-fix-
# round-1 code (commit db79e4f: a plain INSERT, no constraint, at both call
# sites) already ran holds duplicate (employer, fact) rows for the two
# company-size facts -- apply_schema() must dedupe them first, not hard-fail
# with "could not create unique index ... is duplicated" and permanently
# wedge every future apply_schema() call against that database.

def test_apply_schema_dedupes_preexisting_duplicate_company_size_evidence_before_indexing(conn):
    with conn.cursor() as cur:
        # Simulate a database that already ran the pre-fix-round-1 code, on
        # an OLDER schema snapshot that predates this migration's index (the
        # fixture's own reset_schema() already created it; drop it first so
        # this test genuinely exercises "the index does not exist yet").
        cur.execute("DROP INDEX IF EXISTS evidence_employer_company_size_uq")
        cur.execute(
            "INSERT INTO employers (canonical_name, name_key, domain, employee_count, size_band) "
            "VALUES ('Acme', 'acme-dup', 'acmedup.com', 5000, '51-200 employees') RETURNING id"
        )
        eid = int(cur.fetchone()["id"])
        # Exactly what the plain, unconstrained INSERT in db79e4f produced:
        # one row per call site/pass, no ON CONFLICT.
        for _ in range(3):
            cur.execute(
                "INSERT INTO evidence (subject_kind, subject_id, fact, value, status, source, excerpt) "
                "VALUES ('employer', %s, 'company:firmographic_conflict', %s, 'recorded', 'tgtc_core', 'dup')",
                (eid, jsonb({"employee_count": 5000, "size_band": "51-200 employees"})),
            )
        # A second employer with only ONE row -- must survive untouched, not
        # be collapsed by an over-eager dedupe.
        cur.execute(
            "INSERT INTO employers (canonical_name, name_key, domain, employee_count, size_band) "
            "VALUES ('Widgets', 'widgets-solo', 'widgetssolo.com', 5000, '51-200 employees') RETURNING id"
        )
        eid_solo = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO evidence (subject_kind, subject_id, fact, value, status, source, excerpt) "
            "VALUES ('employer', %s, 'company:firmographic_conflict', %s, 'recorded', 'tgtc_core', 'solo')",
            (eid_solo, jsonb({"employee_count": 5000, "size_band": "51-200 employees"})),
        )
    conn.commit()

    apply_schema(conn)  # must not raise "could not create unique index ... is duplicated"

    rows = sqlall(conn, "SELECT id FROM evidence WHERE subject_kind = 'employer' AND subject_id = %s "
                        "AND fact = 'company:firmographic_conflict'", (eid,))
    assert len(rows) == 1, "duplicates must be collapsed to exactly one row per (employer, fact)"
    solo_rows = sqlall(conn, "SELECT id FROM evidence WHERE subject_kind = 'employer' AND subject_id = %s "
                             "AND fact = 'company:firmographic_conflict'", (eid_solo,))
    assert len(solo_rows) == 1, "a non-duplicated row must survive untouched"
    # Applying again (the constraint now exists and holds) must still be a no-op.
    apply_schema(conn)


def test_make_posting_row_default_size_band_is_consistent_with_headcount():
    from datetime import datetime, timezone

    from tgtc_core.domain.facts import size_state
    from tgtc_core.testing.fakes import make_posting_row

    for hc in (5, 12, 30, 150, 800, 2000, 50000):
        row = make_posting_row(id=f"job-{hc}", title="X", organization="Acme", domain="acme.com",
                               description="d", date_created=datetime.now(timezone.utc), headcount=hc)
        state = size_state(row["org_linkedin_headcount"], row["org_linkedin_size"])
        assert state in ("in_range", "out_of_range"), (hc, row["org_linkedin_size"], state)
