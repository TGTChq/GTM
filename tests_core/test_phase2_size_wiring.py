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
