"""Repair legacy, awkward Role Focus strings in Airtable.

This script is intentionally conservative. It only updates records when:
1) the Airtable Role Focus contains two or more literal " and " joins, which
   matches the legacy robotic-list bug; and
2) the corresponding lead can be found in a local enriched JSON file; and
3) the new deterministic extractor produces a different non-empty value.

It does not change Status, Relevance, campaign routing, contacts, or emails.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Iterable, List
from urllib.parse import quote

import config
from http_utils import request_with_retry, safe_json
from role_focus import extract_role_focus

AIRTABLE_API_BASE = "https://api.airtable.com/v0"


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {config.AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    table = quote(config.AIRTABLE_TABLE_NAME, safe="")
    return f"{AIRTABLE_API_BASE}/{config.AIRTABLE_BASE_ID}/{table}"


def _iter_jobs(payload) -> Iterable[Dict]:
    if isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, dict))
        return
    if not isinstance(payload, dict):
        return
    for key in ("jobs", "leads", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            yield from (item for item in value if isinstance(item, dict))
            return


def _load_latest_jobs() -> Dict[str, Dict]:
    paths = sorted(
        Path("data/enriched").glob("jobs_enriched_*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    jobs_by_key: Dict[str, Dict] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for job in _iter_jobs(payload):
            lead_key = str(job.get("lead_key") or "").strip()
            if lead_key:
                jobs_by_key[lead_key] = job
    return jobs_by_key


def _fetch_airtable_records() -> List[Dict]:
    records: List[Dict] = []
    offset = None
    while True:
        params: List[tuple[str, str | int]] = [
            ("pageSize", 100),
            ("fields[]", "Lead Key"),
            ("fields[]", "Role Focus"),
            ("fields[]", "Focus Quality"),
            ("fields[]", "Focus Evidence"),
            ("fields[]", "Matched Role"),
            ("fields[]", "Open Role"),
        ]
        if offset:
            params.append(("offset", offset))
        response = request_with_retry("GET", _base_url(), headers=_headers(), params=params)
        data = safe_json(response)
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return records
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)


def _looks_like_legacy_join(text: str) -> bool:
    normalized = f" {str(text or '').strip().lower()} "
    return normalized.count(" and ") >= 2


def main() -> None:
    if not config.AIRTABLE_TOKEN or not config.AIRTABLE_BASE_ID or not config.AIRTABLE_TABLE_NAME:
        raise SystemExit("Missing Airtable configuration in .env")

    jobs_by_key = _load_latest_jobs()
    records = _fetch_airtable_records()
    updates: List[Dict] = []
    skipped_not_legacy = 0
    skipped_no_local_job = 0
    skipped_no_change = 0

    for record in records:
        fields = record.get("fields") or {}
        current = str(fields.get("Role Focus") or "").strip()
        if not _looks_like_legacy_join(current):
            skipped_not_legacy += 1
            continue

        lead_key = str(fields.get("Lead Key") or "").strip()
        job = jobs_by_key.get(lead_key)
        if not job:
            skipped_no_local_job += 1
            continue

        matched_role = (
            job.get("_matched_role")
            or job.get("matched_role")
            or fields.get("Matched Role")
            or ""
        )
        result = extract_role_focus(job, str(matched_role))
        if not result.text or result.text == current:
            skipped_no_change += 1
            continue

        updates.append(
            {
                "id": record.get("id"),
                "before": current,
                "after": result.text,
                "fields": {
                    "Role Focus": result.text,
                    "Focus Quality": result.quality,
                    "Focus Evidence": " | ".join(result.evidence),
                },
            }
        )

    updated = 0
    for index in range(0, len(updates), 10):
        batch = updates[index : index + 10]
        body = {
            "records": [
                {"id": item["id"], "fields": item["fields"]}
                for item in batch
            ],
            "typecast": True,
        }
        response = request_with_retry(
            "PATCH", _base_url(), headers=_headers(), json_body=body
        )
        data = safe_json(response)
        updated += len(data.get("records", []))
        time.sleep(config.AIRTABLE_RATE_LIMIT_DELAY)

    print(
        json.dumps(
            {
                "legacy_role_focus_found": len(updates),
                "updated": updated,
                "skipped_not_legacy": skipped_not_legacy,
                "skipped_no_local_job": skipped_no_local_job,
                "skipped_no_change": skipped_no_change,
                "changes": [
                    {"before": item["before"], "after": item["after"]}
                    for item in updates
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
