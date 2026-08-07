"""Regression tests for hiring-manager / multi-function observability.

These lock the semantics Brett's follow-up hinges on:

* the failure UNIT is company x role_bucket, and the distinct-company figure is
  a *separate*, smaller number;
* a company with jobs in different functions stays SEPARATE opportunities --
  different function buckets, different HM title ladders, different lead_keys --
  and a success in one function never suppresses another function's search;
* same company + same function + same contact does NOT duplicate;
* the auto-emitted failure CSV carries company/function/failure context but no
  personal PII (person name / email / LinkedIn);
* the summaries reconcile with the pipeline totals.
"""

import csv
import json

import pytest

import hm_observability as O
import hiring_manager as HM
import role_mapping as RM


# --------------------------------------------------------------------------
# Fixtures: synthetic per-bucket lead rows shaped exactly like hiring_manager's
# ``all_leads`` (one lead == one company x function role_bucket).
# --------------------------------------------------------------------------
def _lead(company, domain, bucket, *, found, status, reason="",
          jobs=("j1",), people=0, emp=120, gate="PASS", final="UNVERIFIED"):
    return {
        "_step3_status": status,
        "hiring_manager_name": f"HM {bucket}" if found else None,
        # deliberately attach contact PII to prove it never reaches the CSV:
        "hiring_manager_email": f"person@{domain}" if found else "",
        "hiring_manager_linkedin": f"https://linkedin.com/in/{bucket}" if found else "",
        "_role_bucket": bucket,
        "_hiring_manager_buckets": [bucket],
        "company_domain": domain,
        "canonical_company_name": company,
        "company_employee_count": emp,
        "company_industry": "SaaS",
        "_account_gate_state": gate,
        "related_job_ids": list(jobs),
        "related_open_roles": [f"{bucket} role"],
        "job_source": "ats",
        "job_url": f"https://{domain}/jobs/{bucket}",
        "_final_state": final,
        "_step3_reason": reason,
        "_final_primary_reason": reason,
        "_row2_diagnostic": {
            "bucket": bucket,
            "people_search_call": True,
            "people_returned": people,
            "title_matched_candidates": 0,
            "person_match_attempts": 0 if not found else 1,
            "terminal_reason": None if found else (reason or "apollo_zero_people"),
        },
    }


@pytest.fixture
def leads():
    return [
        # Acme: Marketing FOUND + Sales NOT found (cross-function independence)
        _lead("Acme", "acme.com", "marketing", found=True, status="found",
              jobs=("j1", "j2")),
        _lead("Acme", "acme.com", "gtm_revenue", found=False, status="unverified",
              reason="unverified_no_valid_contact", jobs=("j3",), people=0),
        # Beta: single-function Marketing FOUND
        _lead("Beta", "beta.com", "marketing", found=True, status="found",
              jobs=("j4",), emp=60, gate="NEEDS_CHECK"),
        # Delta: TWO buckets, BOTH not found (distinct-company vs bucket unit)
        _lead("Delta", "delta.com", "finance", found=False, status="unverified",
              reason="apollo_zero_people", jobs=("j5",)),
        _lead("Delta", "delta.com", "engineering", found=False, status="reroute",
              reason="reroute_wrong_organization", jobs=("j6",), people=2,
              final="REROUTE"),
        # Gamma: firmographically EXCLUDED -> not eligible, no search
        _lead("Gamma", "gamma.com", "operations", found=False, status="excluded",
              reason="REJECT_COMPANY_TOO_LARGE", jobs=("j7",), gate="REJECT",
              final="REJECT"),
    ]


# --------------------------------------------------------------------------
# 1 + 2 + 3: bucket grouping + separate ladders (uses the REAL role mapping)
# --------------------------------------------------------------------------
def test_two_marketing_jobs_share_one_marketing_bucket():
    """Two marketing postings map to the SAME function bucket (they may group
    within Marketing)."""
    j1 = {"_matched_role": "Content Marketing Specialist"}
    j2 = {"_matched_role": "Content Marketing Specialist"}
    assert RM.get_bucket_name_for_job(j1) == RM.get_bucket_name_for_job(j2) == "marketing"


def test_marketing_and_sales_are_two_distinct_buckets():
    mkt = {"_matched_role": "Content Marketing Specialist"}
    sales = {"_matched_role": "Account Executive"}
    assert RM.get_bucket_name_for_job(mkt) == "marketing"
    assert RM.get_bucket_name_for_job(sales) == "gtm_revenue"
    assert RM.get_bucket_name_for_job(mkt) != RM.get_bucket_name_for_job(sales)


def test_marketing_and_sales_use_different_hm_ladders():
    mkt_titles = RM.get_target_titles("Content Marketing Specialist")
    sales_titles = RM.get_target_titles("Account Executive")
    assert mkt_titles and sales_titles
    assert mkt_titles != sales_titles
    # No overlap between the top marketing and sales buyer titles.
    assert not (set(mkt_titles[:4]) & set(sales_titles[:4]))


# --------------------------------------------------------------------------
# 4: a successful Marketing contact does NOT suppress the Sales search
# --------------------------------------------------------------------------
def test_marketing_success_does_not_suppress_sales_search(leads):
    mf = {r["company"]: r for r in O.multi_function_rows(leads)}
    acme = mf["Acme"]
    # Both functions were searched even though marketing already found a contact.
    assert acme["expected_hm_searches"] == 2
    assert acme["actual_hm_searches"] == 2
    assert acme["collapse_detected"] is False
    # The unfound Sales bucket still surfaces as its own failure row.
    sales_failures = [r for r in O.hm_failure_rows(leads)
                      if r["company"] == "Acme" and r["role_bucket"] == "gtm_revenue"]
    assert len(sales_failures) == 1


# --------------------------------------------------------------------------
# 5 + 6: lead_key preserves cross-function opportunities / dedups same function
# --------------------------------------------------------------------------
def test_lead_key_distinguishes_functions_same_company():
    mkt = HM._lead_key("acme.com", "hm@acme.com", "marketing")
    sales = HM._lead_key("acme.com", "hm@acme.com", "gtm_revenue")
    assert mkt != sales  # same company+contact, different function -> distinct


def test_lead_key_dedups_same_company_same_function_same_contact():
    a = HM._lead_key("acme.com", "hm@acme.com", "marketing")
    b = HM._lead_key("ACME.com", "HM@acme.com", "marketing")
    assert a == b  # case-insensitive identical key -> one lead, no duplicate


# --------------------------------------------------------------------------
# 7: failure artifact has correct UNIT semantics
# --------------------------------------------------------------------------
def test_failure_unit_is_company_x_role_bucket(leads):
    hm = O.hm_summary(leads)
    assert hm["hm_not_found_unit"] == "company_x_role_bucket"
    rows = O.hm_failure_rows(leads)
    # One row per not-found eligible bucket (Acme sales, Delta finance, Delta eng)
    assert len(rows) == hm["hm_not_found"] == 3
    # Excluded companies (Gamma) are NOT failure rows.
    assert all(r["company"] != "Gamma" for r in rows)


# --------------------------------------------------------------------------
# 8: distinct-company failure count is SEPARATE from company-bucket count
# --------------------------------------------------------------------------
def test_distinct_company_count_differs_from_bucket_count(leads):
    hm = O.hm_summary(leads)
    # Delta contributes TWO not-found buckets but is ONE company without HM.
    assert hm["hm_not_found"] == 3
    assert hm["distinct_companies_without_hm"] == 1  # only Delta
    assert hm["distinct_companies_without_hm"] < hm["hm_not_found"]
    # Acme is NOT a company-without-HM (its marketing bucket found one).
    assert hm["distinct_companies_with_hm"] == 2  # Acme, Beta


# --------------------------------------------------------------------------
# 9: auto CSV carries context but NO personal PII
# --------------------------------------------------------------------------
def test_failure_csv_has_no_personal_pii(tmp_path, leads):
    obs = O.write_run_artifacts(leads, str(tmp_path))
    text = (tmp_path / "hiring_manager_failures.csv").read_text(encoding="utf-8")

    with (tmp_path / "hiring_manager_failures.csv").open(encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    for banned in ("hiring_manager_name", "hiring_manager_email",
                   "hiring_manager_linkedin", "email", "linkedin"):
        assert banned not in header
    # No contact email / LinkedIn VALUE leaks into the file body.
    assert "person@" not in text
    assert "linkedin.com" not in text
    # But it DOES carry the business context a reviewer needs.
    assert "role_bucket" in header and "company" in header and "hm_failure_reason" in header
    assert obs["failure_row_count"] == 3


# --------------------------------------------------------------------------
# 10: summaries reconcile with pipeline totals + all four artifacts written
# --------------------------------------------------------------------------
def test_summaries_reconcile_and_all_artifacts_written(tmp_path, leads):
    obs = O.write_run_artifacts(leads, str(tmp_path))
    hm = obs["hiring_manager"]
    mf = obs["multi_function"]

    # HM identity: found + not_found == eligible buckets (matches step3 math).
    assert hm["hm_found"] + hm["hm_not_found"] == hm["eligible_company_buckets"]
    assert obs["failure_row_count"] == hm["hm_not_found"]
    # eligible buckets exclude the firmographically excluded Gamma bucket.
    assert hm["eligible_company_buckets"] == 5

    # Multi-function reconciliation: Acme + Delta are multi-function.
    assert mf["multi_function_companies"] == 2
    assert mf["expected_hm_searches"] == mf["actual_hm_searches"] == 4
    assert mf["collapse_count"] == 0

    # All four permanent artifacts exist and parse.
    for name in ("hiring_manager_failures.csv", "multi_function_accounts.csv",
                 "hiring_manager_summary.json", "multi_function_summary.json"):
        assert (tmp_path / name).exists()
    json.loads((tmp_path / "hiring_manager_summary.json").read_text(encoding="utf-8"))
    json.loads((tmp_path / "multi_function_summary.json").read_text(encoding="utf-8"))


def test_empty_run_yields_header_only_artifacts(tmp_path):
    obs = O.write_run_artifacts([], str(tmp_path))
    assert obs["failure_row_count"] == 0
    assert obs["hiring_manager"]["eligible_company_buckets"] == 0
    # Header-only CSV still exists (never a missing artifact).
    text = (tmp_path / "hiring_manager_failures.csv").read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("company,")


def test_stdout_summary_is_pii_free_and_labelled(leads):
    hm = O.hm_summary(leads)
    mf = O.multi_function_summary(leads)
    out = "\n".join(O.stdout_summary(hm, mf))
    assert "---- Hiring Manager Coverage ----" in out
    assert "---- Multi-Function Accounts ----" in out
    assert "person@" not in out and "linkedin.com" not in out
    assert "HM HM" not in out  # no person-name tokens
