"""Offline quality audit for an Airtable Leads CSV export.

This script makes no network calls and consumes no JSearch, Apollo, Hunter,
Airtable, or Instantly credits. It replays the current quality gates against
existing Airtable rows and adds deterministic decision/reason columns.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, Iterable, List, Set

import apollo_client
import hiring_manager
import job_filter
from company_identity import normalize_company_name
from domain_utils import normalize_company_domain


def _to_int(value: str | None) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _company_keys(row: Dict[str, str]) -> Set[str]:
    keys: Set[str] = set()
    domain = normalize_company_domain(row.get("Website"))
    if domain:
        keys.add(f"domain:{domain}")
    name = normalize_company_name(row.get("Company"))
    if name:
        keys.add(f"name:{name}")
    return keys


def _row_to_job(row: Dict[str, str]) -> Dict:
    return {
        "job_id": row.get("Job ID"),
        "job_title": row.get("Open Role"),
        "job_description": "",
        "job_location": row.get("Location"),
        "job_country": "",
        "job_is_remote": True,
        "job_employment_type": row.get("Employment Type"),
        "employer_name": row.get("Company"),
        "employer_website": row.get("Website"),
        "job_apply_link": row.get("Job URL"),
        "job_publisher": row.get("Job Source"),
        "_matched_role": row.get("Matched Role"),
    }


def audit_rows(rows: List[Dict[str, str]], last: int | None = None) -> List[Dict[str, str]]:
    start = max(0, len(rows) - last) if last else 0
    prior_company_keys: Set[str] = set()
    audited: List[Dict[str, str]] = []

    for index, row in enumerate(rows):
        row_keys = _company_keys(row)
        is_duplicate_company = bool(row_keys & prior_company_keys)
        prior_company_keys.update(row_keys)
        if index < start:
            continue

        reasons: List[str] = []
        job = _row_to_job(row)
        assessment = job_filter.assess_pre_enrichment_viability(job)
        if not assessment.eligible:
            reasons.append(f"step2:{assessment.stat_name}:{assessment.reason}")

        org = apollo_client.OrgEnrichment(
            found=bool(row.get("Website") or row.get("Industry") or row.get("Employees")),
            name=row.get("Company") or None,
            domain=normalize_company_domain(row.get("Website")) or None,
            employee_count=_to_int(row.get("Employees")),
            founded_year=_to_int(row.get("Founded")),
            industry=row.get("Industry") or None,
        )
        company_ok, company_reason, company_needs_review = hiring_manager.passes_company_criteria(
            org, row.get("Company") or ""
        )
        if not company_ok:
            reasons.append(f"company:{company_reason}")
        elif company_needs_review:
            reasons.append(f"review:{company_reason}")

        hm_tier = hiring_manager._selection_tier(row.get("HM Title"))
        employees = _to_int(row.get("Employees"))
        if hm_tier == "founder_fallback" and (
            employees is None or employees > hiring_manager.config.FOUNDER_FALLBACK_MAX_EMPLOYEES
        ):
            reasons.append("manager:founder_fallback_disallowed_for_company_size")

        if is_duplicate_company:
            reasons.append("airtable:existing_company_duplicate")

        hard_reject = any(not reason.startswith("review:") for reason in reasons)
        decision = "REJECT" if hard_reject else ("REVIEW" if reasons else "PASS")
        output = dict(row)
        output.update(
            {
                "Quality Decision": decision,
                "Quality Reasons": " | ".join(reasons),
                "Normalized Location": assessment.geography.display_location,
                "US Evidence": assessment.geography.reason,
                "Employment Evidence": assessment.employment.reason,
                "Manager Tier": hm_tier,
            }
        )
        audited.append(output)
    return audited


def write_csv(path: Path, rows: Iterable[Dict[str, str]]) -> None:
    rows = list(rows)
    if not rows:
        raise ValueError("No rows matched the requested audit window")
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", help="Airtable Leads CSV export")
    parser.add_argument("--last", type=int, default=None, help="Audit only the last N rows")
    parser.add_argument("--output", default="lead_quality_audit.csv")
    args = parser.parse_args()

    source = Path(args.csv_path)
    with source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    audited = audit_rows(rows, last=args.last)
    destination = Path(args.output)
    write_csv(destination, audited)

    counts: Dict[str, int] = {}
    for row in audited:
        counts[row["Quality Decision"]] = counts.get(row["Quality Decision"], 0) + 1
    print(f"Audited {len(audited)} rows -> {destination}")
    print(" | ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
