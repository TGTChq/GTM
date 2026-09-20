"""Run the country compliance gates over a purchased corpus, offline.

    python rebuild/audit_compliance_corpus.py <records-dir> [--json out.json]

An ISOLATED dry run: it reads local ``*.jsonl.gz`` files, evaluates every record
in-process, and writes counts. It opens no database, makes no provider call and
writes nothing outside the output file it is given -- a test asserts the module
mentions no database or HTTP client at all.

It reuses the pipeline's own readers rather than restating them:
``services.acquisition.org_block`` for the organization block,
``domain.jurisdiction`` for the three jurisdictions and the legal form, and
``policy.compliance`` for the four gates. A report built on a second copy of the
rules measures the copy.

**What this corpus can and cannot answer.** The purchased records are JOB
records. They carry the job's country, the employer's published addresses and
the employer's name -- so the job gates, the aggregate-capacity gate, the
employer's jurisdiction and its legal form are all genuinely measurable. They
carry no CONTACT. ``contact_country`` is therefore unknown for every row, which
is what the outreach gate actually decides on, so the measured cold-email
verdict is "unknown jurisdiction, fail closed" for every row in the corpus.
That is a real result, not a defect in the harness, and it is reported as the
measurement.

Because "every row is unknown" is not, on its own, useful for planning, each row
ALSO carries ``counterfactual_cold_email_status``: what the outreach gate would
say if the contact turned out to be in the employer's own country. It is a
separate field with "counterfactual" in its name, reported in its own table and
never added into a measured total -- a projection, labelled as one.

Units are kept apart throughout. Provider records returned and unique jobs are
different numbers (the same posting was purchased in more than one arm), unique
companies is a third, and contacts are a fourth that this corpus does not
contain at all.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tgtc_core.domain.identity import employer_anchors, employer_key  # noqa: E402
from tgtc_core.domain.jurisdiction import (  # noqa: E402
    corporate_subscriber_status, observe_company_country, observe_job_country, observe_legal_entity_type,
)
from tgtc_core.policy import compliance as cp  # noqa: E402
from tgtc_core.services.acquisition import org_block  # noqa: E402


@dataclass(frozen=True)
class CorpusRow:
    """One provider record, with every gate's verdict on it."""
    provider_id: str
    company_key: str
    #: Jurisdictions, each from its own subject. contact_country is "" for every
    #: row in a corpus of job records, and that is the point rather than a gap
    #: to be filled from one of the others.
    job_country: str
    company_country: str
    contact_country: str
    employer_legal_entity_type: str
    corporate_subscriber_status: str
    job_acquisition_status: str
    aggregate_capacity_status: str
    #: Decided on the COMPANY's country: the only jurisdiction known before a
    #: person has been enriched, and the one the live pipeline's pre-spend gate
    #: uses.
    person_enrichment_status: str
    #: Decided on the CONTACT's country, which this corpus does not carry.
    cold_email_status: str
    outreach_eligible: bool
    outreach_block_reason: str
    capacity_category: str
    #: PROJECTION, never a measurement: what the outreach gate would say if the
    #: contact were in the employer's own country.
    counterfactual_cold_email_status: str
    compliance_rule_version: str = cp.COMPLIANCE_RULE_VERSION


def classify_record(row: Mapping[str, Any]) -> CorpusRow:
    """Every gate's verdict on one provider record, from its own fields."""
    row = dict(row or {})
    org = org_block(row)
    domain, slug, name_key = employer_anchors({**org, "organization": org.get("organization") or ""})
    entity_type = observe_legal_entity_type(org)
    record = cp.ComplianceRecord(
        job_country=observe_job_country(row.get("countries_derived")),
        company_country=observe_company_country(org),
        # No contact exists in a corpus of job records. Left empty on purpose:
        # filling it from the job or the company is the single inference the
        # matrix forbids.
        contact_country="",
        employer_legal_entity_type=entity_type,
        corporate_subscriber_status=corporate_subscriber_status(entity_type),
    )
    decision = cp.evaluate(record)
    gates = decision.gates
    return CorpusRow(
        provider_id=str(row.get("id") or ""),
        company_key=employer_key(domain, slug, name_key) or f"unresolved:{row.get('organization') or row.get('id') or ''}",
        job_country=record.job_country,
        company_country=record.company_country,
        contact_country=record.contact_country,
        employer_legal_entity_type=entity_type,
        corporate_subscriber_status=record.corporate_subscriber_status,
        job_acquisition_status=gates[cp.GATE_JOB_ACQUISITION].status,
        aggregate_capacity_status=gates[cp.GATE_AGGREGATE_CAPACITY].status,
        person_enrichment_status=cp.person_enrichment_allowed(
            record.company_country, **record.conditions()).status,
        cold_email_status=gates[cp.GATE_COLD_EMAIL].status,
        outreach_eligible=decision.outreach_eligible,
        outreach_block_reason=decision.outreach_block_reason,
        capacity_category=decision.capacity_category,
        counterfactual_cold_email_status=cp.cold_email_allowed(
            record.company_country, **record.conditions()).status,
    )


def aggregate(rows: Iterable[CorpusRow]) -> Dict[str, Any]:
    """Counts, with every unit named and never added to another.

    Per-country and per-category counts are over UNIQUE JOBS, not over provider
    records: the same posting was purchased in more than one arm, and counting
    records would inflate every geography by its own duplication rate.
    """
    rows = list(rows)
    by_job: Dict[str, CorpusRow] = {}
    for row in rows:
        by_job.setdefault(row.provider_id or f"anon:{len(by_job)}", row)
    unique = list(by_job.values())
    counted = lambda key: Counter(getattr(r, key) or "unknown" for r in unique)  # noqa: E731
    return {
        "compliance_rule_version": cp.COMPLIANCE_RULE_VERSION,
        "totals": {
            "provider_records": len(rows),
            "unique_jobs": len(unique),
            "unique_companies": len({r.company_key for r in unique}),
            # Contacts: this corpus contains none. Reported as 0 with its reason
            # rather than omitted, so nobody reads the absence as a yield.
            "contacts_found": 0,
            "outreach_eligible_contacts": sum(1 for r in unique if r.outreach_eligible),
            "compliance_blocked_contacts": sum(1 for r in unique if not r.outreach_eligible),
        },
        "not_measured_by_this_corpus": [
            "qualified jobs (needs the classifier, a different task's number)",
            "unique company x campaign units (needs campaign mapping)",
            "contacts found / correct verified contacts / suppression-adjusted net-new "
            "(this corpus contains no contact records and no paid call was made)",
        ],
        "job_country": dict(counted("job_country")),
        "company_country": dict(counted("company_country")),
        "contact_country": dict(counted("contact_country")),
        "job_acquisition_status": dict(counted("job_acquisition_status")),
        "aggregate_capacity_status": dict(counted("aggregate_capacity_status")),
        "person_enrichment_status_by_company_country": dict(counted("person_enrichment_status")),
        "cold_email_status_measured": dict(counted("cold_email_status")),
        "capacity_category_by_unique_job": dict(counted("capacity_category")),
        "entity_type_by_status": dict(counted("corporate_subscriber_status")),
        "fail_closed": {
            "unknown_contact_jurisdiction": sum(1 for r in unique if not r.contact_country),
            "unknown_company_jurisdiction": sum(1 for r in unique if not r.company_country),
            "unknown_job_jurisdiction": sum(1 for r in unique if not r.job_country),
            "unknown_entity_type": sum(1 for r in unique if r.corporate_subscriber_status == cp.ENTITY_UNKNOWN),
            "entity_type_excluded_form": sum(1 for r in unique if r.corporate_subscriber_status == cp.NOT_CORPORATE),
        },
        "PROJECTION_counterfactual_cold_email_if_contact_were_in_company_country":
            dict(Counter(r.counterfactual_cold_email_status or "unknown" for r in unique)),
        "per_company_country": _per_country(unique),
    }


def _per_country(unique: List[CorpusRow]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in unique:
        key = row.company_country or "unknown"
        cell = out.setdefault(key, {"unique_jobs": 0, "unique_companies": set(),
                                    "person_enrichment_status": Counter(),
                                    "entity_type_by_status": Counter(),
                                    "capacity_category": Counter(),
                                    "PROJECTION_counterfactual_cold_email": Counter()})
        cell["unique_jobs"] += 1
        cell["unique_companies"].add(row.company_key)
        cell["person_enrichment_status"][row.person_enrichment_status] += 1
        cell["entity_type_by_status"][row.corporate_subscriber_status] += 1
        cell["capacity_category"][row.capacity_category] += 1
        cell["PROJECTION_counterfactual_cold_email"][row.counterfactual_cold_email_status] += 1
    return {k: {"unique_jobs": v["unique_jobs"], "unique_companies": len(v["unique_companies"]),
                "person_enrichment_status": dict(v["person_enrichment_status"]),
                "entity_type_by_status": dict(v["entity_type_by_status"]),
                "capacity_category": dict(v["capacity_category"]),
                "PROJECTION_counterfactual_cold_email": dict(v["PROJECTION_counterfactual_cold_email"])}
            for k, v in sorted(out.items())}


def read_records(directory: Path) -> Iterator[Dict[str, Any]]:
    """Every provider row in every ``*.jsonl.gz`` under ``directory``.

    A malformed line is skipped and counted by the caller, never silently
    treated as an empty record.
    """
    for path in sorted(directory.glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                envelope = json.loads(line)
                row = envelope.get("row")
                if isinstance(row, str):
                    row = ast.literal_eval(row)
                if isinstance(row, dict):
                    yield row


def run(directory: Path) -> Dict[str, Any]:
    rows = [classify_record(row) for row in read_records(directory)]
    out = aggregate(rows)
    out["source"] = {"records_dir": str(directory),
                     "files": len(sorted(directory.glob("*.jsonl.gz")))}
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records_dir", help="directory of purchased *.jsonl.gz record files")
    parser.add_argument("--json", dest="json_out", default="", help="write the counts here")
    args = parser.parse_args(argv)
    directory = Path(args.records_dir)
    if not directory.is_dir():
        parser.error(f"not a directory: {directory}")
    out = run(directory)
    text = json.dumps(out, indent=2, sort_keys=True, default=str)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CorpusRow", "aggregate", "classify_record", "read_records", "run", "main"]
