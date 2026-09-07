"""Regressions derived from the Sep 7 run; all providers stay offline."""
import json
from pathlib import Path

import pytest

from company_display_resolver import CompanyDisplayCache, resolve_company_display
from run_maintenance import send_safe_forensics
from tests.test_fantastic_send_safe_auto_approval import _fantastic_job


REVIEWED = [
    ("EndeavorB2B", "endeavorb2b", "endeavorbusinessmedia.com"),
    ("BS&B Safety Systems", "bsbsafetysystems", "bsbsystems.com"),
    ("Kai", "kaisecurity", "kai.security"),
    ("Nash", "", "usenash.com"),
    ("Zwicker & Associates, P.C.", "zwickerassociatespc", "zwickerpc.com"),
]


@pytest.mark.parametrize("name,slug,domain", REVIEWED)
def test_reviewed_identity_resolves_without_relaxing_other_identities(tmp_path, name, slug, domain):
    overrides = Path(__file__).resolve().parents[1] / "company_display_overrides.json"
    cache = CompanyDisplayCache(tmp_path / "cache.json", overrides_path=overrides)
    inputs = dict(organization=name, org_linkedin_slug=slug, employer_domain=domain)
    before = resolve_company_display(**inputs, cache=CompanyDisplayCache(tmp_path / "empty"), persist=False)
    after = resolve_company_display(**inputs, cache=cache, persist=False)
    assert before.hold is True
    assert after.hold is False
    assert after.name == name
    assert after.evidence["manual_override"] is True
    for other in ({"employer_domain": "unrelated.example"}, {"org_linkedin_slug": "unrelated"}):
        assert resolve_company_display(**{**inputs, **other}, cache=cache, persist=False).hold


def _corpus(tmp_path, jobs):
    path = tmp_path / "run_artifacts/r1/enrichment/batch/jobs_enriched_1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"jobs": jobs}))
    return path


def test_forensics_reaches_holds_after_row_200_and_keeps_more_than_25_details(tmp_path):
    jobs = [_fantastic_job() for _ in range(201)]
    jobs.extend(_fantastic_job(_outbound_company_hold=True, outbound_company_confidence="low")
                for _ in range(27))
    source = _corpus(tmp_path, jobs)
    original = source.read_bytes()
    row = send_safe_forensics(tmp_path, ["r1"])["runs"][0]
    assert row["examined"] == 228
    assert row["send_safe"] == 201
    assert row["reasons"] == {"outbound_company_held_for_review": 27}
    assert len(row["verified_withheld"]) == 27
    assert row["scan_complete"] is True
    assert source.read_bytes() == original


def test_explicit_diagnostic_limit_is_labelled_partial(tmp_path):
    _corpus(tmp_path, [_fantastic_job() for _ in range(3)])
    row = send_safe_forensics(tmp_path, ["r1"], limit=2)["runs"][0]
    assert row["examined"] == 2
    assert row["scan_complete"] is False
    assert row["omitted_by_limit"] == 1


def test_unreadable_file_cannot_be_reported_as_complete(tmp_path):
    source = _corpus(tmp_path, [_fantastic_job()])
    source.with_name("jobs_enriched_broken.json").write_text("{broken")
    row = send_safe_forensics(tmp_path, ["r1"])["runs"][0]
    assert row["scan_complete"] is False
    assert row["unavailable"]


def test_cross_file_populations_are_not_claimed_to_be_distinct_leads(tmp_path):
    source = _corpus(tmp_path, [_fantastic_job()])
    source.with_name("jobs_enriched_copy.json").write_bytes(source.read_bytes())
    row = send_safe_forensics(tmp_path, ["r1"])["runs"][0]
    assert row["examined"] == 2
    assert row["population_unit"] == "retained_row_occurrences"
    assert row["population_identity_reconciled"] is False
    assert len(row["files"]) == 2


def test_original_reason_counts_and_approved_receipts_are_independent_of_replay(tmp_path, capsys):
    from run_maintenance import print_send_safe_forensics
    source = _corpus(tmp_path, [_fantastic_job() for _ in range(3)])
    original = {"delivery": {"created": 28, "send_safe_withheld": 27, "detail": {"airtable": {
        "created_approved_lead_keys": ["key-with-email@example.test"] * 2,
        "created_approval_status_unknown": 1,
        "not_written_send_safe_reasons": {"outbound_company_held_for_review": 26,
                                         "apollo_email_not_verified": 1}}}}}
    result_path = source.parents[2] / "orchestrator_result.json"
    result_path.write_text(json.dumps(original))
    result = send_safe_forensics(tmp_path, ["r1"], limit=1)
    facts = result["runs"][0]["original_delivery"]
    assert facts["confirmed_approved"] == 1
    assert facts["created_approval_status_unknown"] == 1
    assert facts["send_safe_reasons_reconcile"] is True
    assert facts["send_safe_withheld"] == 27
    print_send_safe_forensics(result)
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()]
    assert records[0]["event"] == "send_safe_forensics_summary"
    assert records[0]["run_id"] == "r1"
    assert "@" not in output


def test_absent_original_receipts_stay_unknown():
    from orchestrator.delivery_evidence import delivery_evidence
    facts = delivery_evidence({"created": 28, "send_safe_withheld": 27})
    assert facts["confirmed_approved"] is None
    assert facts["send_safe_reasons"] is None
    assert facts["send_safe_reasons_reconcile"] is None


def test_source_yield_counts_billed_repeats_and_zero_kept_sources(tmp_path):
    from orchestrator.yield_ledger import YieldLedger
    from tests.test_yield_ledger_cache_allocator import _job
    ledger = YieldLedger(str(tmp_path / "ledger.jsonl"), "r1")
    ledger.record_acquired([_job(i, _acquisition_source="fantastic_jobs_ats") for i in range(328)])
    for i in range(22):
        ledger.mark(str(i), airtable_created=True)
    sources = ledger.summary(billed_by_source={"fantastic_jobs_ats": 500,
        "fantastic_jobs_linkedin": 100})["by_source"]
    assert sources["fantastic_jobs_ats"]["recorded_rows"] == 328
    assert sources["fantastic_jobs_ats"]["credits"] == 500
    assert sources["fantastic_jobs_ats"]["airtable_created_per_1k_credits"] == 44.0
    assert sources["fantastic_jobs_linkedin"]["credits"] == 100
    assert sources["fantastic_jobs_linkedin"]["airtable_created_per_1k_credits"] == 0
    assert ledger.summary()["credits"] is None
    disabled = YieldLedger(str(tmp_path / "disabled.jsonl"), "r1", enabled=False)
    absent = disabled.by_source(billed_by_source={"fantastic_jobs_ats": 500})
    assert absent["fantastic_jobs_ats"]["airtable_created_per_1k_credits"] is None


@pytest.mark.parametrize("name,slug,domain", REVIEWED)
def test_resolved_company_does_not_promote_unverified_email_or_unsafe_contact(tmp_path, name, slug, domain):
    from airtable_client import _job_to_fields, send_safe_facts
    cache = CompanyDisplayCache(tmp_path / "cache", overrides_path=
        Path(__file__).resolve().parents[1] / "company_display_overrides.json")
    display = resolve_company_display(organization=name, org_linkedin_slug=slug,
        employer_domain=domain, cache=cache, persist=False)
    job = _fantastic_job(canonical_company_name=name, company_domain=domain,
        hiring_manager_email=f"fixture@{domain}", outbound_company_name=display.name,
        outbound_company_confidence=display.confidence,
        outbound_company_identity_key=display.identity_key,
        _outbound_company_hold=display.hold)
    assert send_safe_facts(_job_to_fields(job)) == (True, "send_safe")
    assert send_safe_facts(_job_to_fields({**job, "apollo_email_status": "unverified"}))[0] is False
    assert send_safe_facts(_job_to_fields({**job, "_contact_gate_state": "NEEDS_CHECK"}))[0] is False
    assert send_safe_facts(_job_to_fields({**job, "_outbound_role_hold": True,
        "outbound_role_confidence": "low"}))[0] is False
