"""Read-only live validation for JSearch discovery and local filtering.

This command intentionally does not call Apollo, Hunter, Airtable, or Instantly.
It is the safe production-like check for the complete Intent Outbound 2.0 search
catalog before merging or deploying the branch.
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
from pathlib import Path

import config
import job_filter
import jsearch_scraper
from pipeline_state import SeenJobsRegistry


def _top_queries(query_metrics: dict, limit: int = 20) -> list[dict]:
    rows = []
    for search_role, metrics in query_metrics.items():
        rows.append({
            "search_role": search_role,
            "status": metrics.get("status"),
            "raw_jobs": metrics.get("raw_jobs", 0),
            "selected_jobs": metrics.get("selected_jobs", 0),
            "duration_seconds": metrics.get("duration_seconds", 0),
            "quota_remaining_after": metrics.get("quota_remaining_after"),
            "error": metrics.get("error", ""),
        })
    rows.sort(
        key=lambda row: (
            row["selected_jobs"],
            row["raw_jobs"],
            -row["duration_seconds"],
        ),
        reverse=True,
    )
    return rows[:limit]




def _is_diagnostic_scope(args: argparse.Namespace, stats: dict) -> bool:
    """Return True when the command intentionally runs less than production scope.

    An explicit positive --max-queries value is diagnostic even when ROLES_JSON
    temporarily contains the same number of roles. --max-queries 0 explicitly
    requests the complete configured catalog and therefore keeps production
    health gates enabled.
    """
    return bool(
        args.roles is not None
        or (args.max_queries is not None and args.max_queries > 0)
        or stats.get("query_plan_truncated")
    )

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-queries",
        type=int,
        default=None,
        help=(
            "Optional diagnostic cap. Omit to honor "
            "JSEARCH_MAX_QUERIES_PER_RUN; pass 0 to force the complete catalog."
        ),
    )
    parser.add_argument(
        "--roles",
        nargs="*",
        help="Optional explicit search roles. Omit to use config.ROLES.",
    )
    args = parser.parse_args()
    if args.max_queries is not None and args.max_queries < 0:
        raise ValueError("--max-queries cannot be negative")

    # A temporary seen-jobs registry prevents historical production state from
    # hiding current JSearch results during this validation.
    with tempfile.TemporaryDirectory(prefix="tgtc-jsearch-test-") as temp_dir:
        registry = SeenJobsRegistry(path=str(Path(temp_dir) / "seen_jobs.json"))
        scrape = jsearch_scraper.run_daily_scrape(
            registry=registry,
            search_roles=args.roles or None,
            max_queries=args.max_queries,
        )

    filtered = job_filter.run_filter(scrape.output_path)
    stats = scrape.stats
    report = {
        "mode": "jsearch_and_filter_only",
        "external_writes": {
            "apollo": False,
            "hunter": False,
            "airtable": False,
            "instantly": False,
        },
        "catalog_roles_configured": len(config.ROLES),
        "queries_planned": stats.get("queries_planned"),
        "queries_scheduled": stats.get("queries_scheduled"),
        "queries_attempted": stats.get("queries_attempted"),
        "queries_succeeded": stats.get("queries_succeeded"),
        "queries_failed": stats.get("queries_failed"),
        "num_pages_per_query": stats.get("num_pages_per_query"),
        "estimated_request_units": stats.get("estimated_request_units"),
        "estimated_unit_budget": stats.get("estimated_unit_budget"),
        "allowed_role_failures": stats.get("allowed_role_failures"),
        "role_failure_rate": stats.get("role_failure_rate"),
        "zero_result_queries": len(stats.get("zero_result_roles", [])),
        "quota": stats.get("quota", {}),
        "selected_jobs": scrape.total_jobs,
        "roles_with_selected_jobs": scrape.roles_with_results,
        "filtered_jobs_kept": filtered.kept_count,
        "filtered_jobs_rejected": filtered.rejected_count,
        "filter_stats": filtered.stats,
        "scrape_health_errors": scrape.errors,
        "top_queries": _top_queries(stats.get("query_metrics", {})),
        "raw_output": scrape.output_path,
        "filtered_output": filtered.output_path,
        "rejected_output": filtered.rejected_path,
    }
    diagnostic_scope = _is_diagnostic_scope(args, stats)
    api_requests_ok = (
        stats.get("queries_succeeded", 0) > 0
        and stats.get("queries_failed", 0) == 0
    )
    production_catalog_health_ok = bool(
        api_requests_ok and scrape.total_jobs > 0 and not scrape.errors
    )
    report["validation"] = {
        "diagnostic_scope": diagnostic_scope,
        "api_requests_ok": api_requests_ok,
        "market_yield_observed": scrape.total_jobs > 0,
        "production_health_gates_applicable": not diagnostic_scope,
        "production_catalog_health_ok": production_catalog_health_ok,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))

    # A limited smoke test validates authentication, quota access, request
    # execution, and local parsing. A valid market query may return zero role-
    # relevant jobs, which is not an API failure. The complete-catalog run keeps
    # the stricter production volume and role-distribution gates.
    if diagnostic_scope:
        return 0 if api_requests_ok else 1
    return 0 if production_catalog_health_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
