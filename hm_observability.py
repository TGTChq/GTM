"""Permanent per-run hiring-manager (HM) observability artifacts.

Pure, side-effect-light helpers over the enrichment *lead list* produced by
``hiring_manager.run_hiring_manager_identification`` (its ``all_leads``). The
governing invariant of that list is:

    one lead  ==  one (company  x  function role_bucket)

so every count here is expressed in that unit. This is the SAME unit the
production funnel reports as ``hiring_manager_not_found`` (see
``hiring_manager.py`` where ``not_identified = eligible_buckets - identified``):
it is a *company x role_bucket* count, NOT a distinct-company count and NOT a
job count. These helpers make that unit explicit and also derive the
distinct-company figure separately so the two can never be confused again.

PII policy: person names, emails, and LinkedIn URLs are NEVER emitted. Only
company firmographics, the function bucket, counts, and reason codes are
written -- exactly the company/function/failure context a reviewer needs.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: The documented semantic unit of ``hiring_manager_not_found`` / every count
#: in this module. Emitted into the summaries so downstream never re-guesses.
HM_NOT_FOUND_UNIT = "company_x_role_bucket"

# A lead whose ``_step3_status`` is this value was hard-excluded by the company
# criteria (firmographic REJECT) and is NOT part of the eligible denominator --
# it never gets an HM search. Mirrors hiring_manager.py's ``eligible_leads``.
_EXCLUDED_STATUS = "excluded"
_FOUND_STATUS = "found"


# --------------------------------------------------------------------------
# Small field accessors (defensive: leads are plain dicts assembled upstream)
# --------------------------------------------------------------------------
def _first(mapping: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        val = mapping.get(key)
        if val not in (None, ""):
            return val
    return default


def _company_key(lead: Dict[str, Any]) -> str:
    """Stable domain-or-name company identity, mirroring
    ``hiring_manager.company_key_for_job`` (domain first, else normalized name).
    Kept local so this module has no import cycle with hiring_manager."""
    domain = str(_first(lead, "company_domain") or "").strip().lower()
    if domain:
        return domain
    name = str(_first(lead, "canonical_company_name", "employer_name") or "").strip().lower()
    return name or "unknown"


def _company_label(lead: Dict[str, Any]) -> str:
    return str(_first(lead, "canonical_company_name", "employer_name", default="") or "")


def _is_eligible(lead: Dict[str, Any]) -> bool:
    return str(lead.get("_step3_status") or "") != _EXCLUDED_STATUS


def _has_hm(lead: Dict[str, Any]) -> bool:
    return bool(lead.get("hiring_manager_name"))


def _bucket(lead: Dict[str, Any]) -> str:
    return str(_first(lead, "_role_bucket", default="unknown") or "unknown")


def _diag(lead: Dict[str, Any]) -> Dict[str, Any]:
    diag = lead.get("_row2_diagnostic")
    return diag if isinstance(diag, dict) else {}


def _failure_reason(lead: Dict[str, Any]) -> str:
    diag = _diag(lead)
    return str(
        diag.get("terminal_reason")
        or _first(lead, "_step3_reason", "_final_primary_reason", default="unknown")
        or "unknown"
    )


def _searched(lead: Dict[str, Any]) -> bool:
    """True iff an Apollo people-search call was actually issued for this
    company x bucket (per the row-2 diagnostic stamped in hiring_manager.py)."""
    return bool(_diag(lead).get("people_search_call"))


# --------------------------------------------------------------------------
# Per-company aggregation (functions present, jobs, searches)
# --------------------------------------------------------------------------
def _by_company(all_leads: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for lead in all_leads:
        grouped[_company_key(lead)].append(lead)
    return grouped


def _distinct_job_count(leads: Iterable[Dict[str, Any]]) -> int:
    ids: set = set()
    for lead in leads:
        for jid in lead.get("related_job_ids") or []:
            if jid:
                ids.add(str(jid))
    return len(ids)


# --------------------------------------------------------------------------
# Phase C: hiring-manager failure rows (one per not-found company x bucket)
# --------------------------------------------------------------------------
HM_FAILURE_COLUMNS = [
    "company",
    "domain",
    "company_employee_count",
    "company_industry",
    "icp_status",
    "role_bucket",
    "hiring_manager_buckets",
    "relevant_job_title",
    "job_url",
    "job_source",
    "num_relevant_jobs_at_company",
    "num_distinct_functions_at_company",
    "apollo_people_search_attempted",
    "candidate_people_returned",
    "title_matched_candidates",
    "person_match_attempts",
    "candidate_rejected_reason",
    "hm_failure_reason",
    "final_disposition",
    "notes",
]


def hm_failure_rows(all_leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per ACTUAL failure unit: an ELIGIBLE (not firmographically
    excluded) company x role_bucket lead that never identified a hiring
    manager. This is precisely the population counted by
    ``hiring_manager_not_found``."""
    by_company = _by_company(all_leads)
    functions_per_company: Dict[str, int] = {
        ck: len({_bucket(l) for l in leads}) for ck, leads in by_company.items()
    }
    jobs_per_company: Dict[str, int] = {
        ck: _distinct_job_count(leads) for ck, leads in by_company.items()
    }

    rows: List[Dict[str, Any]] = []
    for lead in all_leads:
        if not _is_eligible(lead) or _has_hm(lead):
            continue
        ck = _company_key(lead)
        diag = _diag(lead)
        titles = lead.get("related_open_roles") or []
        rows.append({
            "company": _company_label(lead),
            "domain": _first(lead, "company_domain", default=""),
            "company_employee_count": lead.get("company_employee_count", ""),
            "company_industry": _first(lead, "company_industry", default=""),
            "icp_status": _first(lead, "_account_gate_state", default=""),
            "role_bucket": _bucket(lead),
            "hiring_manager_buckets": ";".join(lead.get("_hiring_manager_buckets") or []),
            "relevant_job_title": (titles[0] if titles else _first(
                lead, "canonical_job_title", "job_title", default="")),
            "job_url": _first(lead, "job_url", "job_apply_link", default=""),
            "job_source": _first(lead, "job_source", "_acquisition_source", default=""),
            "num_relevant_jobs_at_company": jobs_per_company.get(ck, 0),
            "num_distinct_functions_at_company": functions_per_company.get(ck, 0),
            "apollo_people_search_attempted": bool(diag.get("people_search_call")),
            "candidate_people_returned": diag.get("people_returned", ""),
            "title_matched_candidates": diag.get("title_matched_candidates", ""),
            "person_match_attempts": diag.get("person_match_attempts", ""),
            "candidate_rejected_reason": _first(
                lead, "_step3_reason", "_final_primary_reason", default=""),
            "hm_failure_reason": _failure_reason(lead),
            "final_disposition": _first(lead, "_final_state", default=""),
            "notes": "" if diag else "no row2 diagnostic on lead",
        })
    return rows


# --------------------------------------------------------------------------
# Phase F: multi-function account rows (companies with >1 function bucket)
# --------------------------------------------------------------------------
MULTI_FUNCTION_COLUMNS = [
    "company",
    "domain",
    "relevant_jobs",
    "functions",
    "num_functions",
    "expected_hm_searches",
    "actual_hm_searches",
    "contacts_found_by_function",
    "leads_by_function",
    "collapse_detected",
    "collapse_boundary",
]


def _company_search_stats(leads: List[Dict[str, Any]]) -> Tuple[int, int, Dict[str, int], Dict[str, int]]:
    """Return (expected_searches, actual_searches, found_by_bucket, leads_by_bucket)
    for one company's leads. Expected = eligible buckets (a search is warranted);
    actual = buckets that actually issued an Apollo people-search."""
    buckets = sorted({_bucket(l) for l in leads})
    found_by_bucket: Dict[str, int] = defaultdict(int)
    leads_by_bucket: Dict[str, int] = defaultdict(int)
    expected = 0
    actual = 0
    for bucket in buckets:
        bleads = [l for l in leads if _bucket(l) == bucket]
        leads_by_bucket[bucket] = len(bleads)
        if any(_is_eligible(l) for l in bleads):
            expected += 1
        if any(_searched(l) for l in bleads):
            actual += 1
        found_by_bucket[bucket] = sum(1 for l in bleads if _has_hm(l))
    return expected, actual, dict(found_by_bucket), dict(leads_by_bucket)


def multi_function_rows(all_leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for ck, leads in _by_company(all_leads).items():
        functions = sorted({_bucket(l) for l in leads})
        if len(functions) < 2:
            continue
        expected, actual, found_by_bucket, leads_by_bucket = _company_search_stats(leads)
        # Within a single run the pipeline issues one people-search per eligible
        # bucket, so a shortfall (actual < expected) is the ONLY in-run collapse
        # signal. Cross-run company suppression is a delivery-layer effect and is
        # reported in the multi-function SUMMARY, not per row.
        collapse = actual < expected
        rows.append({
            "company": _company_label(leads[0]),
            "domain": _first(leads[0], "company_domain", default=""),
            "relevant_jobs": _distinct_job_count(leads),
            "functions": ";".join(functions),
            "num_functions": len(functions),
            "expected_hm_searches": expected,
            "actual_hm_searches": actual,
            "contacts_found_by_function": ";".join(
                f"{b}:{n}" for b, n in sorted(found_by_bucket.items())),
            "leads_by_function": ";".join(
                f"{b}:{n}" for b, n in sorted(leads_by_bucket.items())),
            "collapse_detected": collapse,
            "collapse_boundary": "hm_search_shortfall" if collapse else "",
        })
    return rows


# --------------------------------------------------------------------------
# Summaries (JSON) -- reconcile with the pipeline totals
# --------------------------------------------------------------------------
def hm_summary(all_leads: List[Dict[str, Any]]) -> Dict[str, Any]:
    eligible = [l for l in all_leads if _is_eligible(l)]
    found = [l for l in eligible if _has_hm(l)]
    not_found = [l for l in eligible if not _has_hm(l)]

    eligible_companies = {_company_key(l) for l in eligible}
    companies_with_hm = {_company_key(l) for l in found}
    companies_without_hm = eligible_companies - companies_with_hm

    failures_by_bucket: Dict[str, int] = defaultdict(int)
    eligible_by_bucket: Dict[str, int] = defaultdict(int)
    found_by_bucket: Dict[str, int] = defaultdict(int)
    for lead in eligible:
        b = _bucket(lead)
        eligible_by_bucket[b] += 1
        if _has_hm(lead):
            found_by_bucket[b] += 1
        else:
            failures_by_bucket[b] += 1

    failures_by_reason: Dict[str, int] = defaultdict(int)
    for lead in not_found:
        failures_by_reason[_failure_reason(lead)] += 1

    coverage_by_bucket = {
        b: round(found_by_bucket.get(b, 0) / eligible_by_bucket[b], 4)
        for b in sorted(eligible_by_bucket)
    }

    return {
        "hm_not_found_unit": HM_NOT_FOUND_UNIT,
        "eligible_companies": len(eligible_companies),
        "eligible_company_buckets": len(eligible),
        "hm_searches": sum(1 for l in eligible if _searched(l)),
        "hm_found": len(found),
        "hm_not_found": len(not_found),
        "distinct_companies_with_hm": len(companies_with_hm),
        "distinct_companies_without_hm": len(companies_without_hm),
        "failure_counts_by_bucket": dict(sorted(failures_by_bucket.items())),
        "failure_counts_by_reason": dict(sorted(failures_by_reason.items())),
        "hm_coverage_by_bucket": coverage_by_bucket,
    }


def multi_function_summary(all_leads: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_company = _by_company(all_leads)
    multi = {ck: leads for ck, leads in by_company.items()
             if len({_bucket(l) for l in leads}) >= 2}

    total_buckets = 0
    expected_searches = 0
    actual_searches = 0
    contacts_found = 0
    leads_created = 0
    collapse_count = 0
    for leads in multi.values():
        expected, actual, found_by_bucket, _ = _company_search_stats(leads)
        total_buckets += len({_bucket(l) for l in leads})
        expected_searches += expected
        actual_searches += actual
        contacts_found += sum(found_by_bucket.values())
        leads_created += sum(1 for l in leads if _has_hm(l))
        if actual < expected:
            collapse_count += 1

    return {
        "multi_function_companies": len(multi),
        "total_role_buckets": total_buckets,
        "expected_hm_searches": expected_searches,
        "actual_hm_searches": actual_searches,
        "contacts_found": contacts_found,
        "leads_created": leads_created,
        "collapse_count": collapse_count,
    }


# --------------------------------------------------------------------------
# stdout (operator) summary -- counts and reason codes only, no PII
# --------------------------------------------------------------------------
_MAJOR_BUCKETS = (
    "marketing", "gtm_revenue", "engineering", "data", "it",
    "customer_success", "customer_support", "finance", "people_hr",
    "operations", "product", "ecommerce", "partnerships",
)


def stdout_summary(hm: Dict[str, Any], mf: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    lines.append("---- Hiring Manager Coverage ----")
    for key in ("eligible_companies", "eligible_company_buckets", "hm_searches",
                "hm_found", "hm_not_found", "distinct_companies_without_hm"):
        lines.append(f"{key:<32}{hm.get(key, 0)}")
    coverage = hm.get("hm_coverage_by_bucket") or {}
    eligible_by = None  # coverage already normalized
    if coverage:
        lines.append("HM coverage by role bucket:")
        for bucket in _MAJOR_BUCKETS:
            if bucket in coverage:
                pct = round(100.0 * float(coverage[bucket]), 1)
                nf = (hm.get("failure_counts_by_bucket") or {}).get(bucket, 0)
                lines.append(f"  {bucket:<18} {pct:>5.1f}%  (not_found={nf})")
    lines.append("---- Multi-Function Accounts ----")
    lines.append(f"{'multi_function_companies':<32}{mf.get('multi_function_companies', 0)}")
    lines.append(f"{'role_buckets':<32}{mf.get('total_role_buckets', 0)}")
    lines.append(f"{'separate_searches':<32}"
                 f"{mf.get('actual_hm_searches', 0)}/{mf.get('expected_hm_searches', 0)}")
    lines.append(f"{'contacts_found':<32}{mf.get('contacts_found', 0)}")
    lines.append(f"{'collapse_detected':<32}{mf.get('collapse_count', 0)}")
    return lines


# --------------------------------------------------------------------------
# Artifact writers
# --------------------------------------------------------------------------
def _write_csv(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_run_artifacts(all_leads: List[Dict[str, Any]],
                        out_dir: str) -> Dict[str, Any]:
    """Emit the four permanent per-run artifacts into ``out_dir`` and return a
    dict with the file paths plus the two summary dicts (for stdout + plumbing).
    Never raises on empty input -- an empty run yields header-only CSVs and
    zeroed summaries so the artifacts always exist."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    failure_rows = hm_failure_rows(all_leads)
    mf_rows = multi_function_rows(all_leads)
    hm_sum = hm_summary(all_leads)
    mf_sum = multi_function_summary(all_leads)

    paths = {
        "hiring_manager_failures_csv": out / "hiring_manager_failures.csv",
        "multi_function_accounts_csv": out / "multi_function_accounts.csv",
        "hiring_manager_summary_json": out / "hiring_manager_summary.json",
        "multi_function_summary_json": out / "multi_function_summary.json",
    }
    _write_csv(paths["hiring_manager_failures_csv"], HM_FAILURE_COLUMNS, failure_rows)
    _write_csv(paths["multi_function_accounts_csv"], MULTI_FUNCTION_COLUMNS, mf_rows)
    paths["hiring_manager_summary_json"].write_text(
        json.dumps(hm_sum, indent=2), encoding="utf-8")
    paths["multi_function_summary_json"].write_text(
        json.dumps(mf_sum, indent=2), encoding="utf-8")

    return {
        "paths": {k: str(v) for k, v in paths.items()},
        "hiring_manager": hm_sum,
        "multi_function": mf_sum,
        "failure_row_count": len(failure_rows),
        "multi_function_row_count": len(mf_rows),
    }
