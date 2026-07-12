"""Controlled end-to-end test for Steps 1-4.

External enrichment calls consume credits. The script requires an explicit
--companies count and does not enroll anything in Instantly.

In this version, --companies means *eligible* companies after Apollo
firmographic checks. Companies rejected for size/industry do not consume the
requested test target.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import airtable_client
import hiring_manager
import job_filter
import jsearch_scraper


def _print_step(message: str) -> None:
    print(message, flush=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--companies",
        type=int,
        required=True,
        help="Target number of eligible unique companies to enrich",
    )
    parser.add_argument("--push-airtable", action="store_true")
    args = parser.parse_args()
    if args.companies < 1:
        raise ValueError("--companies must be at least 1")

    _print_step("[1/4] Scraping fresh jobs from JSearch...")
    scrape = jsearch_scraper.run_daily_scrape()
    _print_step(f"[1/4] Selected {scrape.total_jobs} role-relevant jobs after query matching.")
    if scrape.errors:
        _print_step("[1/4] Warnings: " + " | ".join(scrape.errors))
    if scrape.total_jobs == 0:
        _print_step("ERROR: JSearch produced zero usable jobs. Stopping before enrichment.")
        return 1

    _print_step("[2/4] Applying staffing, aggregator, geography, CRM, and dedupe filters...")
    filtered = job_filter.run_filter(scrape.output_path)
    _print_step(
        f"[2/4] Kept {filtered.kept_count}; rejected {filtered.rejected_count}. "
        f"Details: {json.dumps(filtered.stats, ensure_ascii=False)}"
    )
    if filtered.kept_count == 0:
        _print_step("ERROR: The filter kept zero jobs. Review the rejected output before enriching.")
        return 1

    _print_step(
        f"[3/4] Searching the filtered pool until {args.companies} eligible unique "
        "companies are found, then running Apollo/Hunter..."
    )
    enriched = hiring_manager.run_hiring_manager_identification(
        filtered.output_path,
        target_eligible_companies=args.companies,
    )
    eligible_buckets = enriched.hiring_manager_found + enriched.hiring_manager_not_found
    print(
        json.dumps(
            {
                "target_eligible_companies": args.companies,
                "target_reached": enriched.target_reached,
                "companies_considered": enriched.companies_considered,
                "eligible_companies": enriched.eligible_companies,
                "company_criteria_excluded_companies": enriched.company_criteria_excluded_companies,
                "eligible_company_buckets": eligible_buckets,
                "hiring_managers_identified": enriched.hiring_manager_found,
                "hiring_managers_not_identified": enriched.hiring_manager_not_found,
                "identification_rate": enriched.match_rate,
                "contactable_hiring_managers": enriched.contactable_hiring_managers,
                "uncontactable_hiring_managers": enriched.uncontactable_hiring_managers,
                "contactable_rate": enriched.contactable_rate,
                "output": enriched.output_path,
            },
            indent=2,
        ),
        flush=True,
    )

    if not enriched.target_reached:
        _print_step(
            f"WARNING: The filtered pool was exhausted after {enriched.companies_considered} "
            f"companies and only {enriched.eligible_companies} eligible companies were found."
        )

    if args.push_airtable:
        _print_step("[4/4] Syncing reviewable leads to Airtable...")
        enriched_payload = json.loads(Path(enriched.output_path).read_text(encoding="utf-8"))
        print(
            json.dumps(
                airtable_client.push_leads(enriched_payload.get("jobs", [])),
                indent=2,
            ),
            flush=True,
        )
    else:
        _print_step("[4/4] Airtable push skipped (use --push-airtable to enable it).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
