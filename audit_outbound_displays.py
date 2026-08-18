"""Read-only audit of queued Airtable display values.

This command performs GET requests only.  It never updates Airtable, calls
Instantly, or persists resolver-cache decisions.  Output intentionally excludes
contact names and email addresses.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from company_display_resolver import CompanyDisplayCache, resolve_company_display
from role_display_resolver import resolve_role_display


DEFAULT_STATUSES = ("Pending", "Approved", "Error")


def _changed(before: Any, after: Any) -> bool:
    return str(before or "").strip() != str(after or "").strip()


def audit_records(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = []
    company_confidence: Counter[str] = Counter()
    role_confidence: Counter[str] = Counter()
    company_changes = role_changes = 0
    held = []
    cache_path = os.getenv(
        "OUTBOUND_COMPANY_CACHE_PATH",
        str(Path(__file__).resolve().parent / "data" / "state" / "company_display_cache.json"),
    )
    overrides_path = os.getenv(
        "OUTBOUND_COMPANY_OVERRIDES_PATH",
        str(Path(__file__).resolve().parent / "company_display_overrides.json"),
    )
    cache = CompanyDisplayCache(cache_path, overrides_path=overrides_path)

    for record in records:
        fields = record.get("fields") or {}
        company = str(fields.get("Company") or "").strip()
        website = str(fields.get("Website") or "").strip()
        current_role = str(fields.get("Open Role") or "").strip()
        identity = str(fields.get("Outbound Company Identity") or "")
        slug = identity.split(":", 1)[1] if identity.startswith("linkedin:") else ""
        company_result = resolve_company_display(
            organization=company,
            canonical_company_name=company,
            org_linkedin_slug=slug,
            employer_domain=website,
            canonical_identity_verified=str(fields.get("Firmographics Status") or "").upper() == "PASS",
            cache=cache,
            persist=False,
        )
        role_result = resolve_role_display({
            "job_title": current_role,
            "canonical_job_title": current_role,
            "_matched_role": fields.get("Matched Role"),
            "role_focus": fields.get("Role Focus"),
            "job_location": fields.get("Location"),
            "canonical_employment_type": fields.get("Employment Type"),
            "job_employment_type": fields.get("Employment Type"),
        })
        company_confidence[company_result.confidence] += 1
        role_confidence[role_result.confidence] += 1
        company_changed = _changed(company, company_result.name)
        role_changed = _changed(current_role, role_result.name)
        company_changes += int(company_changed)
        role_changes += int(role_changed)
        item = {
            "record_id": record.get("id"),
            "status": fields.get("Status"),
            "company_before": company,
            "company_after": company_result.name,
            "company_confidence": company_result.confidence,
            "company_hold": company_result.hold,
            "company_identity_key": company_result.identity_key,
            "company_reasons": company_result.evidence.get("reasons") or [],
            "role_before": current_role,
            "role_after": role_result.name,
            "role_confidence": role_result.confidence,
            "role_hold": role_result.hold,
            "role_reasons": role_result.evidence.get("rules") or [],
            "company_changed": company_changed,
            "role_changed": role_changed,
        }
        rows.append(item)
        if company_result.hold or role_result.hold:
            held.append(item)

    changed_examples = [
        row for row in rows if row["company_changed"] or row["role_changed"]
    ]
    return {
        "mode": "read_only",
        "writes": 0,
        "instantly_called": False,
        "total_inspected": len(rows),
        "company_names_changed": company_changes,
        "open_roles_changed": role_changes,
        "company_confidence": {
            level: company_confidence.get(level, 0) for level in ("high", "medium", "low")
        },
        "role_confidence": {
            level: role_confidence.get(level, 0) for level in ("high", "medium", "low")
        },
        "held_count": len(held),
        "held_cases": held,
        "changed_examples": changed_examples[:30],
    }


def run(statuses: Iterable[str] = DEFAULT_STATUSES) -> Dict[str, Any]:
    records: list[Dict[str, Any]] = []
    status_counts: Dict[str, int] = {}
    for status in statuses:
        fetched = _fetch_status_records(status)
        records.extend(fetched)
        status_counts[status] = len(fetched)
    result = audit_records(records)
    result["status_counts"] = status_counts
    return result


def _fetch_status_records(status: str) -> list[Dict[str, Any]]:
    """Minimal Airtable GET client used only by this read-only audit."""
    token = str(os.getenv("AIRTABLE_TOKEN") or "").strip()
    base_id = str(os.getenv("AIRTABLE_BASE_ID") or "").strip()
    table = str(os.getenv("AIRTABLE_TABLE_NAME") or "Leads").strip()
    if not token or not base_id or not table:
        raise ValueError("AIRTABLE_TOKEN, AIRTABLE_BASE_ID, and AIRTABLE_TABLE_NAME are required")
    base_url = f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"
    records: list[Dict[str, Any]] = []
    offset = ""
    while True:
        params = [
            ("filterByFormula", f"{{Status}} = '{status}'"),
            ("pageSize", "100"),
        ]
        if offset:
            params.append(("offset", offset))
        request = Request(
            f"{base_url}?{urlencode(params)}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed Airtable host
            payload = json.loads(response.read().decode("utf-8"))
        records.extend(payload.get("records") or [])
        offset = str(payload.get("offset") or "")
        if not offset:
            break
        time.sleep(0.25)
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--statuses",
        default=",".join(DEFAULT_STATUSES),
        help="Comma-separated Airtable statuses; default: Pending,Approved,Error",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit counts plus compact held/change rows (still includes every hold).",
    )
    args = parser.parse_args(argv)
    statuses = [value.strip() for value in args.statuses.split(",") if value.strip()]
    result = run(statuses)
    if args.compact:
        result["held_cases"] = [
            {
                "record_id": row["record_id"],
                "status": row["status"],
                "company": row["company_before"],
                "identity_key": row["company_identity_key"],
                "reason": (row["company_reasons"] or [""])[0],
            }
            for row in result["held_cases"]
        ]
        result["changed_examples"] = [
            {
                "record_id": row["record_id"],
                "status": row["status"],
                "company": [row["company_before"], row["company_after"]],
                "role": [row["role_before"], row["role_after"]],
            }
            for row in result["changed_examples"]
        ]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
