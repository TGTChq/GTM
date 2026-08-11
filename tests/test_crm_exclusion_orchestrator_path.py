"""Regression tests: company-level CRM exclusion in the ORCHESTRATOR path.

Before this fix, ``job_filter.is_in_crm`` was reachable only from
``job_filter.run_filter`` (Step 2) and ``audit_filter``. The production
orchestrator path is
``RealEnrichmentStage -> run_precontact_qualification -> hiring_manager``,
which never calls ``run_filter`` -- so the CRM exclusion list was inert for
every orchestrator run, and CRM companies could reach Apollo/Hunter enrichment
and Airtable delivery.

These tests pin the fixed behaviour:
  * a company present in the CRM CSV is excluded on the orchestrator path;
  * the exclusion is ACCOUNT-WIDE, not function-specific;
  * a company absent from the CRM CSV remains eligible;
  * a malformed/empty exclusion configuration fails per the CURRENT production
    safety policy (raise under PRODUCTION, degrade to empty otherwise).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import config
import hiring_manager


def _job(job_id: str, company: str, website: str, title: str, bucket_role: str) -> dict:
    """A minimal job record shaped like the orchestrator's qualification output."""
    return {
        "job_id": job_id,
        "job_title": title,
        "employer_name": company,
        "employer_website": website,
        "_employer_domain_input": website,
        "job_description": "We are hiring. " * 40,
        "job_apply_link": f"https://{website}/careers/{job_id}",
        "_matched_role": bucket_role,
        "_acquisition_source": "fantastic_jobs_linkedin",
        "_provider_record_structured": True,
        # marks the payload as strict-mode input for run_hiring_manager_identification
        "_job_gate_state": "UNVERIFIED",
    }


@pytest.fixture(autouse=True)
def _clear_crm_cache():
    hiring_manager.reset_crm_exclusion_cache()
    yield
    hiring_manager.reset_crm_exclusion_cache()


def _write_crm(tmp_path: Path, rows: list[str]) -> str:
    path = tmp_path / "crm_companies.csv"
    path.write_text("\n".join(["Company", *rows]) + "\n", encoding="utf-8")
    return str(path)


def _write_input(tmp_path: Path, jobs: list[dict]) -> str:
    path = tmp_path / "jobs_contact_eligible.json"
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return str(path)


def _run(tmp_path: Path, jobs: list[dict], crm_path: str):
    """Run the real entry point with Apollo stubbed out.

    ``process_company`` is patched so the test never needs a provider: if a
    company reaches it, the CRM exclusion failed to remove it. The set of
    company keys that reach enrichment is the assertion surface.
    """
    reached: list[str] = []

    def _fake_process_company(company_jobs):
        reached.append(hiring_manager.company_key_for_job(company_jobs[0]))
        return [], {}

    with (
        patch.object(config, "CRM_EXCLUSION_FILE", crm_path),
        patch.object(config, "STEP3_OUTPUT_DIR", str(tmp_path / "out")),
        patch.object(hiring_manager, "validate_preflight", return_value=None),
        patch.object(hiring_manager, "process_company", side_effect=_fake_process_company),
    ):
        Path(tmp_path / "out").mkdir(parents=True, exist_ok=True)
        result = hiring_manager.run_hiring_manager_identification(
            _write_input(tmp_path, jobs)
        )
    return result, reached


def test_company_in_crm_is_excluded_on_orchestrator_path(tmp_path):
    crm = _write_crm(tmp_path, ["Acme Analytics"])
    jobs = [_job("j1", "Acme Analytics", "acmeanalytics.com", "Marketing Coordinator",
                 "Marketing Coordinator")]

    result, reached = _run(tmp_path, jobs, crm)

    assert reached == [], "CRM company reached enrichment; exclusion did not apply"
    payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
    assert payload["crm_excluded_companies"] == 1
    assert payload["crm_excluded_jobs"] == 1
    assert "acmeanalytics.com" in payload["crm_excluded_company_reasons"]


def test_crm_exclusion_is_account_wide_not_function_specific(tmp_path):
    """One CRM match must remove EVERY function bucket of that company."""
    crm = _write_crm(tmp_path, ["Acme Analytics"])
    jobs = [
        _job("j1", "Acme Analytics", "acmeanalytics.com", "Marketing Coordinator",
             "Marketing Coordinator"),                       # marketing
        _job("j2", "Acme Analytics", "acmeanalytics.com", "Staff Accountant",
             "Staff Accountant"),                            # finance
        _job("j3", "Acme Analytics", "acmeanalytics.com", "Account Executive",
             "Account Executive"),                           # gtm_revenue
    ]

    result, reached = _run(tmp_path, jobs, crm)

    assert reached == [], "a CRM company must not reach enrichment in ANY function"
    payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
    assert payload["crm_excluded_companies"] == 1
    assert payload["crm_excluded_jobs"] == 3, "all functions must be removed, not just one"


def test_crm_exclusion_removes_buckets_grouped_before_the_matching_job(tmp_path):
    """The match may arrive on a later posting; earlier buckets must still go.

    The first posting carries no website, so it cannot match on domain; the
    second does. The company must be removed account-wide regardless of order.
    """
    crm = _write_crm(tmp_path, ["Acme Analytics"])
    first = _job("j1", "Acme Analytics", "acmeanalytics.com", "Staff Accountant",
                 "Staff Accountant")
    second = _job("j2", "Acme Analytics", "acmeanalytics.com", "Account Executive",
                  "Account Executive")

    result, reached = _run(tmp_path, [first, second], crm)

    assert reached == []
    payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
    assert payload["crm_excluded_jobs"] == 2


def test_company_absent_from_crm_remains_eligible(tmp_path):
    crm = _write_crm(tmp_path, ["Some Other Company"])
    jobs = [_job("j1", "Acme Analytics", "acmeanalytics.com", "Marketing Coordinator",
                 "Marketing Coordinator")]

    result, reached = _run(tmp_path, jobs, crm)

    assert reached == ["acmeanalytics.com"], "non-CRM company must still be enriched"
    payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
    assert payload["crm_excluded_companies"] == 0
    assert payload["crm_excluded_jobs"] == 0


def test_distinct_companies_are_not_collapsed_by_one_crm_match(tmp_path):
    crm = _write_crm(tmp_path, ["Acme Analytics"])
    jobs = [
        _job("j1", "Acme Analytics", "acmeanalytics.com", "Account Executive",
             "Account Executive"),
        _job("j2", "Beta Robotics", "betarobotics.com", "Account Executive",
             "Account Executive"),
    ]

    _result, reached = _run(tmp_path, jobs, crm)

    assert reached == ["betarobotics.com"]


def test_missing_crm_file_raises_under_production_safety_policy(tmp_path):
    """Current production policy (job_filter.load_crm_companies): a missing file
    is fatal under PRODUCTION rather than silently disabling the exclusion."""
    missing = str(tmp_path / "does_not_exist.csv")
    jobs = [_job("j1", "Acme Analytics", "acmeanalytics.com", "Account Executive",
                 "Account Executive")]

    with (
        patch.object(config, "PRODUCTION", True),
        patch.object(config, "CRM_EXCLUSION_FILE", missing),
        patch.object(config, "STEP3_OUTPUT_DIR", str(tmp_path / "out")),
        patch.object(hiring_manager, "validate_preflight", return_value=None),
    ):
        Path(tmp_path / "out").mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError):
            hiring_manager.run_hiring_manager_identification(_write_input(tmp_path, jobs))


def test_empty_crm_file_raises_under_production_safety_policy(tmp_path):
    path = tmp_path / "crm_companies.csv"
    path.write_text("", encoding="utf-8")
    jobs = [_job("j1", "Acme Analytics", "acmeanalytics.com", "Account Executive",
                 "Account Executive")]

    with (
        patch.object(config, "PRODUCTION", True),
        patch.object(config, "CRM_EXCLUSION_FILE", str(path)),
        patch.object(config, "STEP3_OUTPUT_DIR", str(tmp_path / "out")),
        patch.object(hiring_manager, "validate_preflight", return_value=None),
    ):
        Path(tmp_path / "out").mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValueError):
            hiring_manager.run_hiring_manager_identification(_write_input(tmp_path, jobs))


def test_header_only_crm_file_raises_under_production_safety_policy(tmp_path):
    """A header with zero company rows loads zero companies -- production policy
    treats that as a misconfiguration, not as 'nothing to exclude'."""
    crm = _write_crm(tmp_path, [])
    jobs = [_job("j1", "Acme Analytics", "acmeanalytics.com", "Account Executive",
                 "Account Executive")]

    with (
        patch.object(config, "PRODUCTION", True),
        patch.object(config, "CRM_EXCLUSION_FILE", crm),
        patch.object(config, "STEP3_OUTPUT_DIR", str(tmp_path / "out")),
        patch.object(hiring_manager, "validate_preflight", return_value=None),
    ):
        Path(tmp_path / "out").mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValueError):
            hiring_manager.run_hiring_manager_identification(_write_input(tmp_path, jobs))


def test_missing_crm_file_is_tolerated_outside_production(tmp_path):
    """Non-production keeps the existing permissive behaviour: empty sets, run
    continues. This is the current policy in job_filter.load_crm_companies and
    the fix must not change it."""
    missing = str(tmp_path / "does_not_exist.csv")
    jobs = [_job("j1", "Acme Analytics", "acmeanalytics.com", "Account Executive",
                 "Account Executive")]

    with patch.object(config, "PRODUCTION", False):
        _result, reached = _run(tmp_path, jobs, missing)

    assert reached == ["acmeanalytics.com"]


def test_crm_matching_uses_the_canonical_implementation(tmp_path):
    """There must be exactly one CRM matcher. hiring_manager must delegate to
    job_filter.is_in_crm rather than reimplementing normalization."""
    crm = _write_crm(tmp_path, ["Acme Analytics"])
    jobs = [_job("j1", "Acme Analytics", "acmeanalytics.com", "Account Executive",
                 "Account Executive")]

    with patch.object(hiring_manager, "is_in_crm", wraps=hiring_manager.is_in_crm) as spy:
        _run(tmp_path, jobs, crm)

    assert spy.called, "hiring_manager must delegate to job_filter.is_in_crm"


def test_crm_normalization_semantics_are_preserved(tmp_path):
    """Legal-suffix / case / punctuation normalization is job_filter's existing
    behaviour and must be unchanged by routing through the orchestrator."""
    crm = _write_crm(tmp_path, ["Acme Analytics, Inc."])
    jobs = [_job("j1", "ACME ANALYTICS INC", "acmeanalytics.com", "Account Executive",
                 "Account Executive")]

    _result, reached = _run(tmp_path, jobs, crm)

    assert reached == [], "existing CRM normalization must still match this company"
