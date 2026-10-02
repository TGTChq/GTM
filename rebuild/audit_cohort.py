"""Read-only, offline reproduction of cohort facts. No enrichment or approval.

Run from repository root: python -m rebuild.audit_cohort cohort.json
Output is JSON on stdout; contains job/company evidence but no buyer data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from tgtc_core.domain.acquisition_query import EXCLUDED_LINKEDIN_INDUSTRIES
from tgtc_core.domain.employer_attribution import employer_attribution_conflict
from tgtc_core.domain.facts import extract_job_facts
from tgtc_core.domain.identity import employer_anchors
from tgtc_core.policy.requirements import excluded_industry, rule


def audit(data: dict) -> dict:
    postings = data["postings"]
    classifications = {int(c["posting_id"]): c for c in data["classifications"]}
    if len(postings) != data["posting_count"] or len({p["id"] for p in postings}) != len(postings):
        raise ValueError("cohort count/identity mismatch")
    rows = []
    for p in sorted(postings, key=lambda p: int(p["id"])):
        org = p.get("org_json") or {}
        count, industry = org.get("org_linkedin_headcount"), org.get("org_linkedin_industry") or ""
        industry_excluded = bool(excluded_industry(industry))
        size_excluded = count is not None and not rule("min_employees") <= count <= rule("max_employees")
        facts = extract_job_facts(title=p.get("title"), description=p.get("description_text"),
            employment_type=p.get("employment_type"), ai_employment_type=(p.get("structured_json") or {}).get("ai_employment_type"),
            location_type=p.get("location_type"), countries=p.get("countries") or [],
            location_text=p.get("location_text"), employer_name=p.get("employer_name"),
            agency_flag=org.get("org_linkedin_recruitment_agency_derived"), org_industry=industry)
        domain, _, _ = employer_anchors(org)
        conflict = employer_attribution_conflict(p.get("description_text"), employer_name=p.get("employer_name") or "", employer_domain=domain)
        old = classifications[int(p["id"])]
        rows.append({"id": p["id"], "title": p["title"], "employer": p["employer_name"],
                     "industry": industry, "headcount": count,
                     "old_excluded": old["excluded"], "old_reason": old["exclusion_reason"],
                     "old_functions": old["compatible_functions"], "old_method": old["method"],
                     "industry_excluded": industry_excluded, "size_excluded": size_excluded,
                     "new_query_industry_filter": industry in EXCLUDED_LINKEDIN_INDUSTRIES,
                     "new_fact_exclusions": [{"reason": e.reason, "excerpt": e.excerpt} for e in facts.exclusions],
                     "employer_conflict": conflict})
    return {"mode": "offline_facts_not_new_approvals", "total": len(rows),
            "industry_excluded": sum(r["industry_excluded"] for r in rows),
            "size_excluded": sum(r["size_excluded"] for r in rows),
            "company_union_excluded": sum(r["industry_excluded"] or r["size_excluded"] for r in rows),
            "new_query_industry_filter": sum(r["new_query_industry_filter"] for r in rows),
            "old_rejected": sum(r["old_excluded"] for r in rows),
            "employer_conflicts": sum(bool(r["employer_conflict"]) for r in rows), "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", type=Path)
    args = parser.parse_args()
    raw = args.cohort.read_bytes()
    result = audit(json.loads(raw))
    result["input_sha256"] = hashlib.sha256(raw).hexdigest()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
