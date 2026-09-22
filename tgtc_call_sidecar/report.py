"""Outputs: a PRIVATE call file (names, phones) and a NON-PII report (counts only)."""
from __future__ import annotations

import csv
import json
from collections import Counter
from typing import Any, Dict, List

from .config import CAMPAIGN_LABEL

CALL_FILE_COLUMNS = [
    "cohort", "contact_name", "first_name", "last_name", "title", "persona_type", "company", "company_domain",
    "phone", "phone_type", "phone_source", "phone_status", "open_role", "job_url", "campaign", "hiring_signal",
    "prior_email_status", "last_email_at", "suggested_opener_type", "account_email_exposed", "linkedin_url",
    "sidecar_person_key",
    # disposition fields, filled in by the caller:
    "call_attempted_at", "disposition", "disposition_notes", "callback_at", "referral_name", "referral_title",
]


def write_call_file(path: str, members: List[Dict[str, Any]]) -> int:
    rows = [m for m in members if m["status"] == "active"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CALL_FILE_COLUMNS)
        w.writeheader()
        for m in rows:
            w.writerow({
                "cohort": m["cohort"], "contact_name": f"{m['first_name'] or ''} {m['last_name'] or ''}".strip(),
                "first_name": m["first_name"], "last_name": m["last_name"], "title": m["title"],
                "persona_type": m["persona"], "company": m["employer_name"], "company_domain": m["employer_domain"],
                "phone": m["phone_e164"], "phone_type": m["phone_type"], "phone_source": m["phone_source"],
                "phone_status": m["phone_status"], "open_role": m["job_title"], "job_url": m["job_url"],
                "campaign": CAMPAIGN_LABEL.get(m["campaign_key"], m["campaign_key"]), "hiring_signal": m["hiring_signal"],
                "prior_email_status": m["prior_email_status"], "last_email_at": m["last_email_at"] or "",
                "suggested_opener_type": m["suggested_opener"],
                "account_email_exposed": "true" if m["account_email_exposed"] else "false",
                "linkedin_url": m["linkedin"] or "", "sidecar_person_key": m["person_key"],
            })
    return len(rows)


def non_pii_report(members: List[Dict[str, Any]], summary: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    active = [m for m in members if m["status"] == "active"]
    by_cohort: Dict[str, List[Dict[str, Any]]] = {}
    for m in active:
        by_cohort.setdefault(m["cohort"], []).append(m)
    companies = {c: {m["employer_id"] for m in ms} for c, ms in by_cohort.items()}
    both = set.intersection(*companies.values()) if len(companies) == 2 else set()
    return {
        "completed_by_cohort": {c: len(ms) for c, ms in by_cohort.items()},
        "campaign_distribution": {c: dict(Counter(CAMPAIGN_LABEL.get(m["campaign_key"], m["campaign_key"]) for m in ms))
                                  for c, ms in by_cohort.items()},
        "persona_distribution": {c: dict(Counter(m["persona"] for m in ms)) for c, ms in by_cohort.items()},
        "phone_type_distribution": {c: dict(Counter(m["phone_type"] for m in ms)) for c, ms in by_cohort.items()},
        "unique_companies": {c: len(s) for c, s in companies.items()},
        "companies_in_both_cohorts": len(both),
        "call_first_account_email_exposed": sum(1 for m in by_cohort.get("CALL_FIRST", []) if m["account_email_exposed"]),
        "unique_people": len({m["person_key"] for m in active}),
        "unique_phones": len({m["phone_e164"] for m in active}),
        "run": summary,
        **extra,
    }


def write_report(path: str, report: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
