"""Offline counterfactual scope check. No API, secrets, SQL, or eligibility claims.

python -m rebuild.audit_acquisition_profiles --cohort exported-cohort.json
"""
import argparse
import json
from pathlib import Path

from tgtc_core.domain.acquisition_query import PRIORITY_PROFILE, DISCOVERY_PROFILE, profile_filters
from tgtc_core.testing.acquisition_scope import matches_filters


def audit(cohort):
    rows = cohort["postings"]
    unique = {(r["source"], str(r["provider_job_id"])): r for r in rows}
    results = []
    for posting in unique.values():
        structured = posting.get("structured_json") or {}
        provider = {**(structured.get("tracked") or {}), **(posting.get("org_json") or {}),
                    **{k: structured[k] for k in ("ai_employment_type", "ai_taxonomies_a") if k in structured}}
        p = matches_filters(provider, profile_filters(PRIORITY_PROFILE))
        d = matches_filters(provider, profile_filters(DISCOVERY_PROFILE))
        results.append(dict(posting_id=posting["id"], title=posting.get("title"), priority=p, discovery=d,
                            unknown_headcount=provider.get("org_linkedin_headcount") is None,
                            stored_state=posting.get("state"), stored_reason=posting.get("close_reason")))
    return dict(method="offline_documented_filter_approximation_not_live_or_gold_labels",
                total_unique=len(unique), priority_candidates=sum(r["priority"] for r in results),
                discovery_candidates=sum(r["discovery"] for r in results),
                discovery_only_candidates=sum(r["discovery"] and not r["priority"] for r in results),
                warning="Retrievable candidates are NOT qualified jobs or verified leads. No recall rate inferred.",
                profiles={p: profile_filters(p) for p in (PRIORITY_PROFILE, DISCOVERY_PROFILE)}, rows=results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(audit(json.loads(args.cohort.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2)
    if args.output:
        with args.output.open("x", encoding="utf-8") as output:
            output.write(result + "\n")
    else:
        print(result)
