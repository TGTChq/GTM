"""One-time cleanup for fragile Google Jobs URLs already saved in Airtable.

The script uses the richest locally saved JSearch payload (raw or enriched) to
select a stable employer/ATS/publisher application link. It updates only job
URL metadata and leaves reviewer decisions, contacts, and statuses untouched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import airtable_client
import config
from http_utils import request_with_retry, safe_json
from job_signal import _is_google_jobs_url, select_job_url


def _iter_local_payloads() -> Iterable[Path]:
    patterns = (
        "data/enriched/jobs_enriched_*.json",
        "data/raw/jobs_*.json",
        "data/raw/jobs_raw_*.json",
    )
    paths: List[Path] = []
    for pattern in patterns:
        paths.extend(Path(".").glob(pattern))
    return sorted(
        {path.resolve(): path for path in paths}.values(),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _load_local_jobs() -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    """Return local jobs indexed by Job ID and Lead Key, newest payload first."""
    by_job_id: Dict[str, Dict] = {}
    by_lead_key: Dict[str, Dict] = {}
    for path in _iter_local_payloads():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("job_id") or "").strip()
            lead_key = str(job.get("lead_key") or "").strip()
            if job_id and job_id not in by_job_id:
                by_job_id[job_id] = job
            if lead_key and lead_key not in by_lead_key:
                by_lead_key[lead_key] = job
    return by_job_id, by_lead_key


def _all_records() -> List[Dict]:
    records: List[Dict] = []
    offset = None
    wanted = [
        "Lead Key", "Job ID", "Company", "Website", "Job URL",
        "Job URL Status", "Job URL Source", "Job Signal Notes",
    ]
    while True:
        params: List[tuple[str, str | int]] = [("pageSize", 100)]
        params.extend(("fields[]", field) for field in wanted)
        if offset:
            params.append(("offset", offset))
        response = request_with_retry(
            "GET",
            airtable_client._base_url(),
            headers=airtable_client._headers(),
            params=params,
        )
        data = safe_json(response)
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return records
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)


def _append_note(existing: object, note: str) -> str:
    current = str(existing or "").strip()
    return f"{current} | {note}".strip(" |")


def main() -> None:
    airtable_client.validate_preflight()
    by_job_id, by_lead_key = _load_local_jobs()
    records = _all_records()

    updates: List[Dict] = []
    google_urls_found = 0
    replaced = 0
    cleared_no_fallback = 0
    skipped_no_local_payload = 0
    unchanged = 0

    for record in records:
        fields = record.get("fields") or {}
        current_url = str(fields.get("Job URL") or "").strip()
        if not _is_google_jobs_url(current_url):
            continue
        google_urls_found += 1

        job_id = str(fields.get("Job ID") or "").strip()
        lead_key = str(fields.get("Lead Key") or "").strip()
        source_job = by_job_id.get(job_id) or by_lead_key.get(lead_key)
        if not source_job:
            skipped_no_local_payload += 1
            continue

        candidate = dict(source_job)
        if not candidate.get("employer_website"):
            candidate["employer_website"] = fields.get("Website")
        selected, status, source, reason = select_job_url(
            candidate,
            company_domain=str(fields.get("Website") or ""),
            probe=True,
        )

        patch: Dict = {
            "Job URL Status": status,
            "Job URL Source": source,
            "Job Signal Notes": _append_note(
                fields.get("Job Signal Notes"),
                f"v3.5_job_url_repair={reason}",
            ),
        }
        if selected and not _is_google_jobs_url(selected):
            patch["Job URL"] = selected
            replaced += 1
        else:
            # A blank cell is preferable to a misleading Google Jobs viewer
            # link that repeatedly opens a generic results page.
            patch["Job URL"] = None
            patch["Job URL Status"] = "unverified_review"
            patch["Job URL Source"] = "missing"
            cleared_no_fallback += 1

        if selected == current_url:
            unchanged += 1
            continue
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
        "google_job_urls_found": google_urls_found,
        "records_updated": updated,
        "replaced_with_stable_url": replaced,
        "cleared_when_no_stable_fallback": cleared_no_fallback,
        "skipped_no_local_payload": skipped_no_local_payload,
        "unchanged": unchanged,
        "failed": failed,
    }, indent=2))


if __name__ == "__main__":
    main()
