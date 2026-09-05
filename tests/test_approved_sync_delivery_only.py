"""Regression tests: Approved Sync is DELIVERY-ONLY.

Incident 2026-08-12: 627 Approved rows were marked Error with
"Validation is stale; rerun qualification before enrollment". The worker was
re-running the full qualification pipeline (Apollo org enrichment, Apollo person
match, Hunter, JobSourceResolver) plus a 24h validation-age gate before
enrollment. Every record failed the age gate at
``approved_revalidation.py:41`` -- before any provider call -- so nothing was
enrolled and nothing reached Instantly.

Approved is now the authorization boundary: the worker delivers, it does not
re-qualify. These tests pin that contract.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import airtable_client
import config
import instantly_client
import run_approved


def _fields(**over):
    f = {
        "Status": "Approved",
        "Final Decision": "FINAL_PASS",
        "Validation Version": config.VALIDATION_VERSION,
        "Validation Fingerprint": "fp",
        "Email": "hm@acme.com",
        "Apollo Email Status": "verified",
        "Email Validation": "PASS",
        "Contact Alignment": "PASS",
        "Company": "Acme",
        "Outbound Company": "Acme",
        "Outbound Company Confidence": "high",
        "Outbound Company Identity": "domain:acme.com",
        "Outbound Hold": False,
        "Open Role": "Account Executive",
        "Outbound Role": "Account Executive",
        "Outbound Roles": "Account Executive",
        "Outbound Role Confidence": "medium",
        "Role Focus": "pipeline development",
        "Role Bucket": "gtm_revenue",
        "Campaign ID": "camp-123",
        "Employees": 120,
        "Job URL Status": "verified",
        # deliberately ANCIENT -- age must never block delivery
        "Validated At": (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(),
    }
    f.update(over)
    return f


def _rec(rid="rec1", **over):
    return {"id": rid, "fields": _fields(**over)}


@pytest.fixture(autouse=True)
def _fingerprint_ok():
    with patch.object(run_approved, "fingerprint_matches", return_value=True), \
         patch.object(airtable_client, "fingerprint_matches", return_value=True):
        yield


class _Tripwire:
    """Any call is a contract violation."""

    def __init__(self, name):
        self.name = name
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise AssertionError(f"{self.name} must NOT be called by Approved Sync")


# ------------------------------------------------------------------ 1. age
def test_record_older_than_24h_is_still_eligible():
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    category, reason = airtable_client.approved_row_eligibility(_fields(**{"Validated At": old}))
    assert category == "eligible", f"age must not disqualify: {reason}"


def test_delivery_precheck_accepts_an_ancient_but_valid_record():
    ok, reason = run_approved._delivery_precheck(_rec())
    assert ok, reason
    assert "stale" not in reason.lower()


def test_stale_validation_rejection_is_gone_from_the_enrollment_path():
    import inspect
    src = inspect.getsource(run_approved)
    assert "revalidate_approved_record" not in src
    assert "Validation is stale" not in src


# ------------------------------------------- 2/3/4. zero provider + network
def _run_with_tripwires(records, enroll_result):
    apollo_org = _Tripwire("apollo.enrich_organization")
    apollo_person = _Tripwire("apollo.match_person")
    hunter_verify = _Tripwire("hunter.verify_email")
    resolver = _Tripwire("JobSourceResolver.resolve")
    import apollo_client
    import hunter_client
    import job_source_resolver
    with patch.object(airtable_client, "select_eligible_approved",
                      return_value=(records, {"approved_seen": len(records),
                                               "approved_eligible": len(records),
                                               "approved_skipped_legacy": 0,
                                               "approved_skipped_invalid": 0})), \
         patch.object(airtable_client, "mark_error"), \
         patch.object(airtable_client, "mark_enrolled"), \
         patch.object(apollo_client, "enrich_organization", apollo_org), \
         patch.object(apollo_client, "match_person", apollo_person), \
         patch.object(hunter_client, "verify_email", hunter_verify), \
         patch.object(job_source_resolver.JobSourceResolver, "resolve", resolver), \
         patch.object(instantly_client, "enroll_approved_leads",
                      return_value=enroll_result) as enroll:
        result = run_approved.run()
    return result, enroll, (apollo_org, apollo_person, hunter_verify, resolver)


def _ok_enroll(ids):
    return {"enrolled_record_ids": list(ids), "enrolled": len(ids), "duplicates": 0,
            "failed": 0, "failures": []}


def test_approved_sync_makes_zero_apollo_hunter_and_resolver_calls():
    records = [_rec("recA"), _rec("recB")]
    result, _enroll, tripwires = _run_with_tripwires(records, _ok_enroll(["recA", "recB"]))
    for tw in tripwires:
        assert tw.calls == 0, f"{tw.name} was called"
    assert result["approved_attempted"] == 2


def test_enroll_record_does_not_probe_the_job_url():
    """probe=True would call select_job_url(probe=True) -- a live fetch."""
    seen = {}

    def fake_builder(record, *, probe=True):
        seen["probe"] = probe
        return {"email": "hm@acme.com", "campaign": "camp-123"}

    with patch.object(instantly_client, "airtable_record_to_lead", fake_builder), \
         patch.object(instantly_client, "validate_preflight"), \
         patch.object(instantly_client, "request_with_retry",
                      return_value=SimpleNamespace(status_code=200, text="{}",
                                                   json=lambda: {})), \
         patch.object(instantly_client, "safe_json", return_value={}), \
         patch.object(instantly_client, "debug_dump"):
        instantly_client.enroll_record(_rec())
    assert seen["probe"] is False, "delivery must not probe the job URL"


# ------------------------------------------------------- 5/6/7. enrollment
def test_valid_record_is_enrolled_and_marked_enrolled():
    records = [_rec("recA")]
    with patch.object(airtable_client, "select_eligible_approved",
                      return_value=(records, {"approved_seen": 1, "approved_eligible": 1,
                                              "approved_skipped_legacy": 0,
                                              "approved_skipped_invalid": 0})), \
         patch.object(airtable_client, "mark_error") as mark_error, \
         patch.object(airtable_client, "mark_enrolled") as mark_enrolled, \
         patch.object(instantly_client, "enroll_approved_leads",
                      return_value=_ok_enroll(["recA"])):
        result = run_approved.run()
    mark_enrolled.assert_called_once_with(["recA"])
    mark_error.assert_not_called()
    assert result["enrolled"] == 1


def test_instantly_failure_never_marks_enrolled():
    records = [_rec("recA")]
    failure = {"enrolled_record_ids": [], "enrolled": 0, "duplicates": 0, "failed": 1,
               "failures": [{"record_id": "recA", "email": "hm@acme.com",
                             "error": "Instantly 500"}]}
    with patch.object(airtable_client, "select_eligible_approved",
                      return_value=(records, {"approved_seen": 1, "approved_eligible": 1,
                                              "approved_skipped_legacy": 0,
                                              "approved_skipped_invalid": 0})), \
         patch.object(airtable_client, "mark_error") as mark_error, \
         patch.object(airtable_client, "mark_enrolled") as mark_enrolled, \
         patch.object(instantly_client, "enroll_approved_leads", return_value=failure):
        result = run_approved.run()
    mark_enrolled.assert_not_called()
    assert mark_error.called, "a delivery failure must be recorded for retry"
    assert result["failed"] == 1 and result["airtable_marked_enrolled"] == 0


def _duplicate_run(search_campaigns):
    """Drive enroll_record through a 409 duplicate with a given membership answer.

    A duplicate rejection proves the email EXISTS; it does not prove the lead is in
    the campaign we targeted. Membership is therefore resolved authoritatively.
    """
    import requests
    resp = SimpleNamespace(status_code=409, text="lead already exists")
    err = requests.HTTPError("409"); err.response = resp

    def request(method, url, **kw):
        if url.endswith("/leads"):
            raise err
        return SimpleNamespace()

    with patch.object(instantly_client, "validate_preflight"), \
         patch.object(instantly_client, "airtable_record_to_lead",
                      return_value={"email": "hm@acme.com", "campaign": "c"}), \
         patch.object(instantly_client, "request_with_retry", side_effect=request), \
         patch.object(instantly_client, "safe_json",
                      return_value={"items": [{"id": c} for c in search_campaigns]}):
        return instantly_client.enroll_record(_rec("recDup", Email="hm@acme.com"))


def test_duplicate_in_the_target_campaign_leaves_the_queue():
    """Already in the campaign we targeted -> delivered, so the row is done."""
    out = _duplicate_run(["c"])
    assert out.success and out.status == "duplicate"
    assert out.membership == instantly_client.ALREADY_IN_TARGET_CAMPAIGN
    assert not out.net_new


def test_duplicate_in_another_campaign_is_not_delivered():
    """A duplicate elsewhere was never delivered to our target -- never Enrolled."""
    out = _duplicate_run(["some-other-campaign"])
    assert not out.success
    assert out.status == "not_delivered"
    assert out.membership == instantly_client.EXISTING_OTHER_CAMPAIGN


# ------------------------------------------------------------ 9. local gates
@pytest.mark.parametrize("override,expected", [
    ({"Email": ""}, "invalid"),
    ({"Final Decision": "REJECT"}, "legacy"),
    ({"Validation Version": "old-version"}, "legacy"),
    ({"Validation Fingerprint": ""}, "legacy"),
])
def test_missing_local_delivery_fields_still_fail_closed(override, expected):
    category, _reason = airtable_client.approved_row_eligibility(_fields(**override))
    assert category == expected


def test_precheck_rejects_a_record_that_cannot_build_a_payload():
    with patch.object(instantly_client, "airtable_record_to_lead",
                      side_effect=ValueError("Missing required approved-lead fields: Company")):
        ok, reason = run_approved._delivery_precheck(_rec())
    assert not ok and "Company" in reason


def test_no_campaign_is_invalid_not_eligible():
    with patch.object(config, "resolve_campaign_id", return_value=""):
        category, reason = airtable_client.approved_row_eligibility(
            _fields(**{"Campaign ID": ""}))
    assert category == "invalid" and reason == "no_campaign_configured"


# ------------------------------------------------------------- 10. recovery
def test_stale_error_records_recover_without_re_enrichment():
    """The 627: reset to Approved, then the corrected flow delivers them with
    zero provider calls and no age rejection."""
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    records = [_rec(f"rec{i}", **{"Validated At": old}) for i in range(3)]
    result, enroll, tripwires = _run_with_tripwires(
        records, _ok_enroll([r["id"] for r in records]))
    for tw in tripwires:
        assert tw.calls == 0, f"{tw.name} was called during recovery"
    assert result["approved_attempted"] == 3
    assert result["enrolled"] == 3
    assert result["airtable_mark_error"] == 0


# --------------------------------------------------------------- logging
def test_failure_logging_is_bounded(caplog):
    failures = [{"record_id": f"rec{i}", "email": f"a{i}@x.com",
                 "error": "Instantly 500: upstream"} for i in range(627)]
    result = {"approved": 627, "approved_attempted": 627, "enrolled": 0,
              "duplicates": 0, "failed": 627, "airtable_marked_enrolled": 0,
              "airtable_mark_error": 627, "failures": failures}
    with caplog.at_level("INFO"):
        run_approved._log_enrollment_result(result)
    text = caplog.text
    assert len(caplog.records) <= 3, "must not emit one line per failure"
    assert text.count("rec") <= run_approved._MAX_LOGGED_FAILURE_EXAMPLES + 5
    assert "failed=627" in text
    for i in range(50, 627):
        assert f"a{i}@x.com" not in text, "must not dump every failure object"


# ------------------------------------------- 8. the contract is also STRUCTURAL
#
# The behavioural tests above prove the delivery path made no provider call on a
# given input. These prove it CANNOT, by reading what the module actually imports
# and what the deprecated switch actually selects -- which is the shape a
# reintroduction would take.


def test_run_approved_does_not_import_the_legacy_revalidation_module():
    """``approved_revalidation`` calls Apollo, Hunter and JobSourceResolver.

    It is retained for the gate-composition tests that exercise it directly, and
    for the record of what the delivery path deliberately does not do. Reaching it
    from the delivery path is exactly the 2026-08-12 regression.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("run_approved.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"approved_revalidation", "apollo_client", "hunter_client",
                 "job_source_resolver", "fantastic_jobs_adapter",
                 "qualification_pipeline", "hiring_manager"}
    assert not (imported & forbidden), (
        f"run_approved must stay delivery-only; it imports {sorted(imported & forbidden)}"
    )


def test_the_deprecated_revalidate_switch_selects_nothing(caplog):
    """``APPROVED_SYNC_REVALIDATE_PROVIDERS`` is `true` on the live service.

    It must remain a name that changes no behaviour, so a reader of the Railway
    variables cannot conclude that provider revalidation is on.
    """
    records = [_rec("recA")]
    with patch.object(airtable_client, "select_eligible_approved",
                      return_value=(records, {"approved_seen": 1, "approved_eligible": 1,
                                              "approved_skipped_legacy": 0,
                                              "approved_skipped_invalid": 0})), \
         patch.object(instantly_client, "enroll_approved_leads",
                      return_value=_ok_enroll(["recA"])), \
         patch.object(airtable_client, "mark_enrolled"), \
         patch.object(airtable_client, "mark_error"), \
         patch.object(config, "APPROVED_SYNC_REVALIDATE_PROVIDERS", True):
        with_flag = run_approved.run()
        with patch.object(config, "APPROVED_SYNC_REVALIDATE_PROVIDERS", False):
            without_flag = run_approved.run()

    assert with_flag["approved_attempted"] == without_flag["approved_attempted"] == 1
    assert with_flag["revalidation_failed"] == without_flag["revalidation_failed"] == 0


def test_passing_revalidate_providers_is_ignored_and_says_so(caplog):
    records = [_rec("recA")]
    with caplog.at_level("WARNING"), \
         patch.object(airtable_client, "select_eligible_approved",
                      return_value=(records, {"approved_seen": 1, "approved_eligible": 1,
                                              "approved_skipped_legacy": 0,
                                              "approved_skipped_invalid": 0})), \
         patch.object(instantly_client, "enroll_approved_leads",
                      return_value=_ok_enroll(["recA"])), \
         patch.object(airtable_client, "mark_enrolled"), \
         patch.object(airtable_client, "mark_error"):
        result = run_approved.run(revalidate_providers=True)

    assert result["approved_attempted"] == 1, "the record is still delivered"
    assert "ignored" in caplog.text.lower()
