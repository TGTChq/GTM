"""One-time V3.3 Airtable cleanup.

- Converts legacy 0-8 relevance points into a clear 0-100 score.
- Re-checks legacy aggregator_review URLs; a working aggregator URL becomes
  verified, while inaccessible/broken URLs keep a review/blocking status.
- Recalculates freshness from the richest local job payload available, using
  the oldest conflicting posted-date signal conservatively.
- Supports a deliberate manual age override for cases where an external source
  visibly disagrees with JSearch's syndication timestamp.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Dict, List

import airtable_client
import config
from http_utils import request_with_retry, safe_json
from job_signal import annotate_job
from role_relevance import normalize_relevance_score


def _latest_local_jobs() -> Dict[str, Dict]:
    paths = sorted(
        Path("data/enriched").glob("jobs_enriched_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not paths:
        return {}
    try:
        payload = json.loads(paths[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    jobs = payload.get("jobs") or []
    return {
        str(job.get("lead_key")): job
        for job in jobs
        if isinstance(job, dict) and job.get("lead_key")
    }


def _all_records() -> List[Dict]:
    records: List[Dict] = []
    offset = None
    wanted = [
        "Lead Key", "Company", "Open Role", "Job URL", "Posted At", "Website",
        "Job Freshness", "Job Age Days", "Job URL Status", "Job URL Source",
        "Job Signal Notes", "Relevance Score",
    ]
    while True:
        params: List[tuple[str, str | int]] = [("pageSize", 100)]
        params.extend(("fields[]", field) for field in wanted)
        if offset:
            params.append(("offset", offset))
        response = request_with_retry(
            "GET", airtable_client._base_url(), headers=airtable_client._headers(), params=params
        )
        data = safe_json(response)
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return records
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)


def _freshness_from_age(age_days: int) -> str:
    if age_days <= 7:
        return "fresh"
    if age_days < 30:
        return "aging"
    return "stale_review"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", help="Optional company name for a manual age override")
    parser.add_argument("--age-days", type=int, help="Visible age to set for the manual override")
    args = parser.parse_args()
    if (args.company is None) != (args.age_days is None):
        parser.error("--company and --age-days must be supplied together")

    airtable_client.validate_preflight()
    local_jobs = _latest_local_jobs()
    records = _all_records()
    updates: List[Dict] = []
    score_updates = 0
    signal_updates = 0
    manual_updates = 0

    for record in records:
        fields = record.get("fields") or {}
        patch: Dict = {}
        lead_key = str(fields.get("Lead Key") or "")

        old_score = fields.get("Relevance Score")
        if isinstance(old_score, (int, float)) and 0 < float(old_score) <= 8:
            patch["Relevance Score"] = normalize_relevance_score(old_score)
            score_updates += 1

        source_job = local_jobs.get(lead_key) or {
            "job_posted_at_datetime_utc": fields.get("Posted At"),
            "job_apply_link": fields.get("Job URL"),
            "employer_website": fields.get("Website"),
        }
        assessed = annotate_job(source_job, probe_url=True)

        old_age = fields.get("Job Age Days")
        new_age = assessed.get("job_age_days")
        if new_age is not None and (
            old_age is None or not isinstance(old_age, (int, float)) or new_age > old_age
        ):
            patch["Job Age Days"] = new_age
            patch["Job Freshness"] = assessed.get("job_freshness")
            patch["Job Signal Notes"] = assessed.get("job_signal_notes")
            signal_updates += 1

        if str(fields.get("Job URL Status") or "").lower() == "aggregator_review":
            patch["Job URL"] = assessed.get("job_url_selected") or fields.get("Job URL")
            patch["Job URL Status"] = assessed.get("job_url_status")
            patch["Job URL Source"] = assessed.get("job_url_source")
            patch["Job Signal Notes"] = assessed.get("job_signal_notes")
            signal_updates += 1

        if args.company and args.company.casefold() in str(fields.get("Company") or "").casefold():
            age = max(0, args.age_days)
            patch["Job Age Days"] = age
            patch["Job Freshness"] = _freshness_from_age(age)
            existing_notes = str(patch.get("Job Signal Notes") or fields.get("Job Signal Notes") or "")
            manual_note = f"manual_visible_age_override={age}"
            patch["Job Signal Notes"] = f"{existing_notes} | {manual_note}".strip(" |")
            manual_updates += 1

        if patch:
            updates.append({"id": record.get("id"), "fields": patch})

    updated = 0
    failed = 0
    for index in range(0, len(updates), 10):
        batch = updates[index:index + 10]
        try:
            response = request_with_retry(
                "PATCH",
                airtable_client._base_url(),
                headers=airtable_client._headers(),
                json_body={"records": batch, "typecast": True},
            )
            updated += len(safe_json(response).get("records", []))
        except Exception:
            failed += len(batch)
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)

    print(json.dumps({
        "records_scanned": len(records),
        "records_updated": updated,
        "legacy_relevance_scores_normalized": score_updates,
        "job_signals_rechecked": signal_updates,
        "manual_age_overrides": manual_updates,
        "failed": failed,
    }, indent=2))


if __name__ == "__main__":
    main()
