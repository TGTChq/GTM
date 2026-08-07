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


def _has_domain(lead: Dict[str, Any]) -> bool:
    """True iff the lead carries a resolvable company domain -- i.e. a search
    was *possible*. A bucket with a domain that was still not searched is the
    only thing that could indicate a genuine within-run collapse; a bucket with
    NO domain is a domain-resolution shortfall, never a collapse."""
    return bool(str(_first(lead, "company_domain", default="") or "").strip())


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
    "true_collapse_detected",
    "search_shortfall",
    "no_search_domain_shortfall",
    "excluded_buckets",
    "shortfall_cause",
]


def _company_bucket_stats(leads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-company multi-function accounting with a STRICT collapse definition.

    For each function bucket at the company we know whether it was ICP-eligible,
    whether a resolvable domain existed (a search was *possible*), whether a
    people-search was actually issued, and whether a contact was found.

    * expected_hm_searches   = eligible buckets (a search is warranted)
    * actual_hm_searches     = buckets that issued a people-search
    * search_shortfall       = eligible buckets that did NOT get searched (any cause)
    * no_search_domain       = eligible+unsearched buckets that had NO domain
                               (a domain-resolution shortfall, NOT a collapse)
    * true_collapse          = eligible buckets that HAD a resolvable domain (so a
                               search was possible) yet were NOT searched -- the only
                               signal that a distinct function was collapsed/suppressed
                               within the run. By construction a no_search_domain
                               bucket can never be a true_collapse.
    """
    buckets = sorted({_bucket(l) for l in leads})
    found_by_bucket: Dict[str, int] = {}
    leads_by_bucket: Dict[str, int] = {}
    expected = actual = search_shortfall = no_domain = true_collapse = excluded = 0
    causes: Dict[str, int] = defaultdict(int)
    for bucket in buckets:
        bl = [l for l in leads if _bucket(l) == bucket]
        leads_by_bucket[bucket] = len(bl)
        found_by_bucket[bucket] = sum(1 for l in bl if _has_hm(l))
        eligible = any(_is_eligible(l) for l in bl)
        searched = any(_searched(l) for l in bl)
        has_domain = any(_has_domain(l) for l in bl)
        if not eligible:
            excluded += 1
            continue
        expected += 1
        if searched:
            actual += 1
            continue
        # eligible but not searched -> a shortfall; classify the cause
        search_shortfall += 1
        if not has_domain:
            no_domain += 1
            causes["no_search_domain"] += 1
        else:
            # eligible AND a domain existed AND still no search == genuine collapse
            true_collapse += 1
            causes["true_collapse"] += 1
    return {
        "buckets": buckets,
        "found_by_bucket": found_by_bucket,
        "leads_by_bucket": leads_by_bucket,
        "expected_hm_searches": expected,
        "actual_hm_searches": actual,
        "search_shortfall": search_shortfall,
        "no_search_domain_shortfall": no_domain,
        "true_collapse": true_collapse,
        "excluded_buckets": excluded,
        "causes": dict(causes),
    }


def multi_function_rows(all_leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for ck, leads in _by_company(all_leads).items():
        functions = sorted({_bucket(l) for l in leads})
        if len(functions) < 2:
            continue
        s = _company_bucket_stats(leads)
        rows.append({
            "company": _company_label(leads[0]),
            "domain": _first(leads[0], "company_domain", default=""),
            "relevant_jobs": _distinct_job_count(leads),
            "functions": ";".join(functions),
            "num_functions": len(functions),
            "expected_hm_searches": s["expected_hm_searches"],
            "actual_hm_searches": s["actual_hm_searches"],
            "contacts_found_by_function": ";".join(
                f"{b}:{n}" for b, n in sorted(s["found_by_bucket"].items())),
            "leads_by_function": ";".join(
                f"{b}:{n}" for b, n in sorted(s["leads_by_bucket"].items())),
            # TRUE collapse only: a distinct function that had a resolvable domain
            # (search was possible) but was not searched. A no_search_domain
            # shortfall is reported separately and is NEVER a collapse.
            "true_collapse_detected": bool(s["true_collapse"]),
            "search_shortfall": s["search_shortfall"],
            "no_search_domain_shortfall": s["no_search_domain_shortfall"],
            "excluded_buckets": s["excluded_buckets"],
            "shortfall_cause": ";".join(f"{k}:{v}" for k, v in sorted(s["causes"].items())),
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

    total_buckets = eligible_buckets = excluded_buckets = 0
    expected_searches = actual_searches = 0
    search_shortfall = no_domain_shortfall = true_collapse = 0
    contacts_found = leads_created = 0
    for leads in multi.values():
        s = _company_bucket_stats(leads)
        total_buckets += len(s["buckets"])
        excluded_buckets += s["excluded_buckets"]
        eligible_buckets += s["expected_hm_searches"]
        expected_searches += s["expected_hm_searches"]
        actual_searches += s["actual_hm_searches"]
        search_shortfall += s["search_shortfall"]
        no_domain_shortfall += s["no_search_domain_shortfall"]
        true_collapse += s["true_collapse"]
        contacts_found += sum(s["found_by_bucket"].values())
        leads_created += sum(1 for l in leads if _has_hm(l))

    # Reconciliation invariant (asserted in tests): every eligible multi-function
    # bucket is either searched, a no_search_domain shortfall, or a true collapse.
    #   expected_hm_searches == actual_hm_searches + search_shortfall
    #   search_shortfall     == no_search_domain_shortfall + true_collapse
    return {
        "multi_function_companies": len(multi),
        "total_role_buckets": total_buckets,
        "eligible_role_buckets": eligible_buckets,
        "excluded_role_buckets": excluded_buckets,
        "expected_hm_searches": expected_searches,
        "actual_hm_searches": actual_searches,
        "search_shortfall_count": search_shortfall,
        "no_search_domain_shortfall_count": no_domain_shortfall,
        "true_collapse_count": true_collapse,
        "contacts_found": contacts_found,
        "leads_created": leads_created,
    }


def domain_resolution_summary(all_leads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-run employer-domain resolution metrics (non-PII). An eligible company×
    bucket either resolved a domain (a people-search was possible) or is classified
    by why it did not -- so staffing/aggregator posters are never conflated with a
    genuine resolver failure. Reconciles: resolved + unresolved == eligible buckets."""
    eligible = [l for l in all_leads if _is_eligible(l)]
    resolved = unresolved = 0
    by_class: Dict[str, int] = defaultdict(int)
    by_reason: Dict[str, int] = defaultdict(int)
    by_method: Dict[str, int] = defaultdict(int)
    by_source: Dict[str, int] = defaultdict(int)
    by_source_unresolved: Dict[str, int] = defaultdict(int)
    staffing_or_hidden = aggregator = intermediary = ats_known = direct = 0
    unlocked_by_recovery = 0
    for lead in eligible:
        diag = _diag(lead)
        src = str(_first(lead, "_acquisition_source", "job_publisher", default="unknown") or "unknown")
        by_source[src] += 1
        if _has_domain(lead) or _searched(lead):
            resolved += 1
            method = str(diag.get("domain_resolution_method") or "enrichment_resolved")
            by_class["direct_employer"] += 1
            by_method[method] += 1
            direct += 1
            # a recovered (non-enrichment) method is a search unlocked by this layer
            if method not in ("enrichment_resolved", "none", ""):
                unlocked_by_recovery += 1
        else:
            unresolved += 1
            cls = str(diag.get("domain_classification") or "unresolved_no_evidence")
            by_class[cls] += 1
            by_reason[str(diag.get("domain_unresolved_reason") or "unknown")] += 1
            by_source_unresolved[src] += 1
            if cls == "intermediary_unknown_client" or cls == "intermediary_known_client":
                intermediary += 1
                staffing_or_hidden += 1
            elif cls == "aggregator_employer_unresolved":
                aggregator += 1
            elif cls == "ats_employer_known":
                ats_known += 1
    return {
        # Units: these are company×role_bucket "postings evaluated" for HM search.
        "postings_evaluated": len(eligible),
        "eligible_company_buckets": len(eligible),
        "employer_resolved": resolved,
        "employer_unresolved": unresolved,
        "domain_resolved": resolved,
        "domain_unresolved": unresolved,
        "resolution_rate": round(resolved / len(eligible), 4) if eligible else 0.0,
        "hm_searches_unlocked_by_recovery": unlocked_by_recovery,
        "direct_employer": direct,
        "ats_employer_known": ats_known,
        "aggregator": aggregator,
        "intermediary": intermediary,
        "staffing_or_hidden_client": staffing_or_hidden,
        "classification": dict(sorted(by_class.items())),
        "resolved_by_method": dict(sorted(by_method.items())),
        "unresolved_by_reason": dict(sorted(by_reason.items())),
        "by_source": dict(sorted(by_source.items())),
        "unresolved_by_source": dict(sorted(by_source_unresolved.items())),
    }


# --------------------------------------------------------------------------
# stdout (operator) summary -- counts and reason codes only, no PII
# --------------------------------------------------------------------------
_MAJOR_BUCKETS = (
    "marketing", "gtm_revenue", "engineering", "data", "it",
    "customer_success", "customer_support", "finance", "people_hr",
    "operations", "product", "ecommerce", "partnerships",
)


def stdout_summary(hm: Dict[str, Any], mf: Dict[str, Any],
                   dr: Optional[Dict[str, Any]] = None) -> List[str]:
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
    lines.append(f"{'search_shortfall':<32}{mf.get('search_shortfall_count', 0)}")
    lines.append(f"{'no_search_domain_shortfall':<32}{mf.get('no_search_domain_shortfall_count', 0)}")
    # TRUE collapse only -- a distinct function suppressed by another. Must be 0
    # in a healthy run; a no_search_domain shortfall never counts here.
    lines.append(f"{'true_collapse_detected':<32}{mf.get('true_collapse_count', 0)}")
    if dr:
        lines.append("---- Employer / Domain Resolution ----")
        for key in ("postings_evaluated", "direct_employer", "ats_employer_known",
                    "aggregator", "intermediary", "staffing_or_hidden_client",
                    "employer_resolved", "employer_unresolved",
                    "hm_searches_unlocked_by_recovery", "resolution_rate"):
            lines.append(f"{key:<34}{dr.get(key, 0)}")
        for reason, n in (dr.get("unresolved_by_reason") or {}).items():
            lines.append(f"  unresolved:{reason:<24} {n}")
        for src, n in (dr.get("unresolved_by_source") or {}).items():
            lines.append(f"  by_source:{src:<25} {n}")
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
    dr_sum = domain_resolution_summary(all_leads)

    paths = {
        "hiring_manager_failures_csv": out / "hiring_manager_failures.csv",
        "multi_function_accounts_csv": out / "multi_function_accounts.csv",
        "hiring_manager_summary_json": out / "hiring_manager_summary.json",
        "multi_function_summary_json": out / "multi_function_summary.json",
        "domain_resolution_summary_json": out / "domain_resolution_summary.json",
        "employer_resolution_summary_json": out / "employer_resolution_summary.json",
    }
    _write_csv(paths["hiring_manager_failures_csv"], HM_FAILURE_COLUMNS, failure_rows)
    _write_csv(paths["multi_function_accounts_csv"], MULTI_FUNCTION_COLUMNS, mf_rows)
    paths["hiring_manager_summary_json"].write_text(
        json.dumps(hm_sum, indent=2), encoding="utf-8")
    paths["multi_function_summary_json"].write_text(
        json.dumps(mf_sum, indent=2), encoding="utf-8")
    paths["domain_resolution_summary_json"].write_text(
        json.dumps(dr_sum, indent=2), encoding="utf-8")
    # Richer employer/source-vs-employer view (superset of domain_resolution).
    paths["employer_resolution_summary_json"].write_text(
        json.dumps(dr_sum, indent=2), encoding="utf-8")

    return {
        "paths": {k: str(v) for k, v in paths.items()},
        "hiring_manager": hm_sum,
        "multi_function": mf_sum,
        "domain_resolution": dr_sum,
        "failure_row_count": len(failure_rows),
        "multi_function_row_count": len(mf_rows),
    }
