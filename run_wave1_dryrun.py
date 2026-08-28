"""Zero-write Outbound Wave 1 dry run over REAL stored opportunities.

This script reads local production artifacts, rebuilds Airtable-shaped records
with the production field builder (``airtable_client._job_to_fields``, which is
pure and performs no I/O), resolves every record through the Wave 1 policy, and
writes a review artifact.

It performs NO network calls of any kind: no Airtable, no Instantly, no Apollo,
no Hunter. ``PRODUCTION=0`` is forced before config import purely so the local
validation fingerprint can be computed without a production signing key.

Population fidelity is recorded per row:

``contact_present``
    True when the stored record carries a real hiring-manager name. Only those
    rows could actually be enrolled.
``contact_synthetic``
    True when the stored record has no contact (Apollo contact enrichment did
    not run on that batch). Every other input is real; only the salutation token
    is substituted, so the rendered copy can still be reviewed. These rows are
    excluded from the enrollable counts.

Usage::

    python run_wave1_dryrun.py --limit 120 --out reports/wave1_dryrun.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# The dry run is an offline transform of local artifacts. Production mode is
# only about the validation signing key here, and nothing is written anywhere.
os.environ.setdefault("PRODUCTION", "0")

import airtable_client  # noqa: E402
from outbound_wave1 import resolve_batch  # noqa: E402
from outbound_wave1.assignment import company_assignment_key  # noqa: E402
from outbound_wave1.claims import load_claim_registry  # noqa: E402
from outbound_wave1.measurement import analyze, build_frame  # noqa: E402
from outbound_wave1.resolver import DEFAULT_EXPERIMENT_ID  # noqa: E402

#: Local production artifacts, richest first. Every one is a real pipeline run.
CORPORA: Tuple[Tuple[str, str], ...] = (
    ("2026-08-07 full run", "reports/jobs_enriched_20260807T095512Z-8b8355aa.json"),
    ("2026-08-18 reprocess", "data/orchestrator_reprocess/enrichment/enrichment/jobs_enriched_2026-08-18.json"),
    ("2026-08-19 recovery remainder", "recovery_20260818T213748Z/work/enriched/jobs_enriched_2026-08-19_remainder.json"),
    ("2026-08-23 external batch", "data/external_batch/orchestrator/run_artifacts/apify5k-20260823/enrichment/enrichment/jobs_enriched_2026-08-23.json"),
)

#: Already-built Airtable records (highest fidelity: real contact included).
WRITE_READY = "reports/apify_batch_20260823/write_ready_send_safe_leads.json"

#: Salutation stand-in for rows whose stored record genuinely has no contact.
SYNTHETIC_FIRST_NAME = "Reviewer"


def _load_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _records_from_write_ready() -> List[Dict[str, Any]]:
    payload = _load_json(WRITE_READY)
    records = (payload or {}).get("records") or []
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(records):
        fields = dict(item.get("fields") or {})
        if not fields:
            continue
        out.append({
            "id": f"write_ready:{index}",
            "fields": fields,
            "_source": "write_ready_send_safe_leads",
            "_contact_present": bool(str(fields.get("Hiring Manager") or "").strip()),
        })
    return out


def _records_from_corpus(label: str, path: str) -> List[Dict[str, Any]]:
    payload = _load_json(path)
    jobs = (payload or {}).get("jobs") or []
    out: List[Dict[str, Any]] = []
    for index, job in enumerate(jobs):
        try:
            fields = airtable_client._job_to_fields(job)
        except Exception:  # noqa: BLE001 - a malformed historical row is skipped
            continue
        if not fields.get("Role Bucket"):
            continue
        contact = bool(str(fields.get("Hiring Manager") or "").strip())
        if not contact:
            fields = dict(fields)
            fields["Hiring Manager"] = SYNTHETIC_FIRST_NAME
        out.append({
            "id": f"{path}:{index}",
            "fields": fields,
            "_source": label,
            "_contact_present": contact,
        })
    return out


def _tier_interest(record: Dict[str, Any]) -> int:
    """Rank a record by how much of the policy it would exercise.

    Rows with specific focus evidence or several openings reach T1; a review that
    only ever sees the T3 fallback cannot QA the rest of the policy.
    """
    fields = record["fields"]
    score = 0
    if str(fields.get("Focus Quality") or "").lower() == "specific":
        score += 2
    roles = str(fields.get("Outbound Roles") or fields.get("Open Roles") or "")
    if len([part for part in roles.split("|") if part.strip()]) >= 2:
        score += 2
    if record.get("_contact_present"):
        score += 1
    return score


def build_population(limit: int) -> List[Dict[str, Any]]:
    """Assemble a campaign-balanced dry-run population of real records.

    Selection is round-robin across role buckets so every live campaign present in
    the local corpora is reviewable, and within a bucket the rows that exercise
    the most of the policy come first. Contact-complete rows are preferred but a
    bucket is never dropped just because its stored rows have no contact.
    """
    pool: List[Dict[str, Any]] = list(_records_from_write_ready())
    for label, path in CORPORA:
        pool.extend(_records_from_corpus(label, path))

    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for record in pool:
        fields = record["fields"]
        key = "|".join([
            str(fields.get("Outbound Company") or ""),
            str(fields.get("Outbound Role") or ""),
            str(fields.get("Role Bucket") or ""),
            str(fields.get("Job ID") or ""),
        ]).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)

    # Keep whole companies together first. In production the resolver runs over a
    # whole approved queue, so every row for a company is present and the
    # cross-bucket signal can fire; sampling one row per company would hide it.
    by_key: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in deduped:
        key = company_assignment_key(record["fields"])
        if key:
            by_key[key].append(record)
    multi_company = sorted(
        (
            (key, rows) for key, rows in by_key.items()
            if len({str(r["fields"].get("Role Bucket") or "") for r in rows}) >= 2
        ),
        key=lambda item: (
            "people_hr" not in {str(r["fields"].get("Role Bucket") or "") for r in item[1]},
            -len(item[1]),
            item[0],
        ),
    )

    selected: List[Dict[str, Any]] = []
    chosen_ids: set[str] = set()
    company_budget = max(0, int(limit * 0.35))
    for _key, rows in multi_company:
        if len(selected) >= company_budget:
            break
        for record in rows:
            if record["id"] not in chosen_ids:
                selected.append(record)
                chosen_ids.add(record["id"])

    by_bucket: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in deduped:
        bucket = str(record["fields"].get("Role Bucket") or "")
        if bucket and record["id"] not in chosen_ids:
            by_bucket[bucket].append(record)
    for rows in by_bucket.values():
        rows.sort(key=_tier_interest, reverse=True)

    buckets = sorted(by_bucket)
    cursor = {bucket: 0 for bucket in buckets}
    while len(selected) < limit and any(cursor[b] < len(by_bucket[b]) for b in buckets):
        for bucket in buckets:
            if len(selected) >= limit:
                break
            index = cursor[bucket]
            if index < len(by_bucket[bucket]):
                selected.append(by_bucket[bucket][index])
                cursor[bucket] = index + 1
    return selected


def _counts(resolutions: Iterable[Dict[str, Any]], key: str) -> Dict[str, int]:
    return dict(sorted(Counter(str(r.get(key) or "") for r in resolutions).items()))


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    challenger = [r for r in rows if r.get("experiment_arm") == "B"]
    reviewable = [r for r in rows if r.get("copy_reviewable")]
    qa_reason_counts: Counter = Counter()
    for row in reviewable:
        for reason in row.get("qa_reasons") or []:
            qa_reason_counts[reason] += 1
    return {
        "evaluated": len(rows),
        "by_arm": _counts(rows, "experiment_arm"),
        "by_campaign": _counts(rows, "campaign"),
        "by_signal_tier": _counts(reviewable, "signal_tier"),
        "by_signal_type": _counts(reviewable, "signal_type"),
        "by_proof": _counts(reviewable, "proof_type"),
        "by_offer": _counts(reviewable, "outbound_offer_type"),
        "by_offer_class": _counts(reviewable, "offer_class"),
        "by_friction_angle": _counts(reviewable, "friction_angle"),
        "role_page_match": {
            "true": sum(1 for r in reviewable if r.get("role_page_match")),
            "false": sum(1 for r in reviewable if not r.get("role_page_match")),
        },
        "challenger_assigned": len(challenger),
        "copy_reviewed": len(reviewable),
        "qa_pass": sum(1 for r in reviewable if r.get("qa_pass")),
        "qa_fail": sum(1 for r in reviewable if not r.get("qa_pass")),
        "qa_reason_counts": dict(sorted(qa_reason_counts.items(), key=lambda kv: -kv[1])),
        "by_source_artifact": {
            source: {
                "reviewed": sum(1 for r in reviewable if r.get("source_artifact") == source),
                "qa_pass": sum(
                    1 for r in reviewable
                    if r.get("source_artifact") == source and r.get("qa_pass")
                ),
            }
            for source in sorted({str(r.get("source_artifact") or "") for r in reviewable})
        },
        "contact_present": sum(1 for r in rows if r.get("contact_present")),
        "contact_synthetic": sum(1 for r in rows if not r.get("contact_present")),
        "enrollable_challenger_qa_pass": sum(
            1 for r in rows
            if r.get("experiment_arm") == "B" and r.get("contact_present") and r.get("qa_pass")
        ),
    }


def run(
    limit: int,
    out_path: str,
    *,
    b_split_pct: int,
    experiment_id: str,
    as_of: Optional[datetime] = None,
    claims_path: str = "",
) -> Dict[str, Any]:
    population = build_population(limit)
    registry = load_claim_registry(claims_path or None)

    resolutions, previews, batch_failures = resolve_batch(
        population,
        experiment_id=experiment_id,
        b_split_pct=b_split_pct,
        registry=registry,
        as_of=as_of,
        challenger_preview=True,
    )
    preview_by_record = {p.record_id: p for p in previews}
    meta_by_record = {r["id"]: r for r in population}

    rows: List[Dict[str, Any]] = []
    for resolution in resolutions:
        meta = meta_by_record.get(resolution.record_id, {})
        payload = resolution.to_dict()
        # For a control-arm record the challenger fields are a PREVIEW only: the
        # record would be sent the live Control A sequence, unchanged.
        preview = preview_by_record.get(resolution.record_id)
        if preview is not None:
            payload = {**preview.to_dict(), "experiment_arm": resolution.experiment_arm}
            payload["challenger_preview_only"] = True
        else:
            payload["challenger_preview_only"] = False
        payload["source_artifact"] = meta.get("_source", "")
        payload["contact_present"] = bool(meta.get("_contact_present"))
        payload["copy_reviewable"] = bool(payload.get("rendered_email_1"))
        rows.append(payload)

    artifact = {
        "schema": "tgtc-outbound-wave1-dryrun/1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "external_writes": 0,
        "provider_operations": [],
        "experiment_id": experiment_id,
        "b_split_pct": b_split_pct,
        "as_of": (as_of or datetime.now(timezone.utc)).isoformat(),
        "claim_registry": {
            "path": registry.path,
            "role_pages": len(registry.role_pages),
            "roles_with_quotable_economics": registry.economics_role_count,
            "verified_claims": sorted(
                claim_id for claim_id, claim in registry.claims.items() if claim.usable
            ),
        },
        "population_note": (
            "Real stored opportunities from local production run artifacts, rebuilt "
            "with airtable_client._job_to_fields. Rows with contact_present=false "
            "carry no stored hiring manager (Apollo contact enrichment did not run on "
            "that batch); only the salutation token is substituted so the copy can be "
            "reviewed, and those rows are excluded from enrollable counts."
        ),
        "batch_qa_failures": batch_failures,
        # Denominators only: no outcomes exist yet, so every rate is null. This
        # is the frame the primary metric will be computed over at read-out.
        "experiment_frame": analyze(build_frame(resolutions, previews)),
        "summary": summarize(rows),
        "records": rows,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--out", default="reports/wave1_dryrun.json")
    parser.add_argument("--b-split-pct", type=int, default=50)
    parser.add_argument(
        "--claims",
        default="",
        help=(
            "Alternate static claim registry. Used to review the economics path "
            "against real records before any public role page is published."
        ),
    )
    parser.add_argument("--experiment-id", default=DEFAULT_EXPERIMENT_ID)
    parser.add_argument(
        "--as-of",
        default="",
        help=(
            "ISO date used as 'today' when deriving job age. Job age is the only "
            "time-dependent input, so this exercises the T2 window against the same "
            "real records."
        ),
    )
    args = parser.parse_args()

    as_of = None
    if args.as_of:
        as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=timezone.utc)
    artifact = run(
        args.limit,
        args.out,
        b_split_pct=args.b_split_pct,
        experiment_id=args.experiment_id,
        as_of=as_of,
        claims_path=args.claims,
    )
    summary = artifact["summary"]
    print(json.dumps(summary, indent=2))
    print(f"\nartifact: {args.out}")
    if artifact["batch_qa_failures"]:
        print("BATCH QA FAILURES:", artifact["batch_qa_failures"])


if __name__ == "__main__":
    main()
