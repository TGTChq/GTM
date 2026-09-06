"""Rebuild a reporting corpus from captured production RUN SUMMARY logs.

WHAT THIS IS FOR. Railway keeps deployment logs long after the container is gone,
including for REMOVED deployments, so a run's RUN SUMMARY is recoverable at any
time. The volume that holds the run artifacts and the durable ledger is not: its
SFTP session is served through a live container, and a cron service has one only
while the run is in flight. When acceptance has to happen between runs, this is the
evidence that exists.

WHAT IT IS NOT. A RUN SUMMARY is a RENDERING of the artifacts, not the artifacts.
It prints roughly thirty counters; the report parses fields the summary never
mentions -- ``enrichment.funnel.qualification_input`` most importantly, which is
the only source for ``jobs_reviewed``. A corpus built from logs is therefore a
LOWER BOUND on what production can report, and a report rendered from it may
declare a metric unavailable that production measures perfectly well. Use it to
check the reporting code against real production VALUES; never to conclude that
production cannot measure something.

THE RULE THIS FILE FOLLOWS. A key the summary does not print is ABSENT here. Never
zero, never inferred from a neighbouring counter, never carried over from another
run. The reason to trust a number from this corpus is that a production run printed
exactly it.

TWO TRAPS THE MAPPING ENCODES.

1. ``JOBS_ANALYZED`` and ``raw_postings`` are the SAME variable in the printer --
   both are ``waterfall.unit_totals.postings``, the rows the lanes kept before
   cross-run dedupe. They are NOT net-new captured postings, and
   ``weekly_report.metrics`` deliberately dropped that field as a ``jobs_captured``
   candidate after it reported 6,205 provider rows as a week's throughput. So they
   are written where they actually come from, and a run whose build predates
   ``net_new_jobs_captured`` reads as UNAVAILABLE for capture rather than
   borrowing this number.

2. The corpus must write the SEPARATE artifact files, not just the embedded copies
   inside ``orchestrator_result.json``. ``weekly_report.metrics`` accepts either
   shape, but ``run_ledger._BACKFILL_FIELDS`` reads ``contacts_found`` only from
   the ``waterfall`` stem and ``sent_to_airtable`` only from the ``delivery`` stem.
   A corpus carrying just the embedded copies renders identically from artifacts
   and then loses both metrics from the ledger -- which looks exactly like a
   durability defect and is not one. Production writes ``waterfall.json`` and
   ``delivery.json`` on both pipeline paths (``pipeline.py`` 653/658 and
   1178/1179), so the faithful corpus writes them too.

    python acceptance/corpus_from_run_logs.py <evidence_dir> <out_dir>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: printed key -> every (artifact stem, dotted path) production would write it to.
#: Production writes each of these counters twice -- once in its own artifact file
#: and once in the ``orchestrator_result.json`` roll-up -- and the report and the
#: ledger backfill read different ones, so both are reproduced.
FIELD_MAP: Dict[str, Tuple[Tuple[str, str], ...]] = {
    # -- acquisition: roll-up only, which is where production keeps it ----------
    "net_new_jobs_captured": (
        ("orchestrator_result", "acquisition.cumulative.net_new_jobs_captured"),),
    "jobs_returned_billed": (
        ("orchestrator_result", "acquisition.cumulative.jobs_returned_billed"),),
    "jobs_quota_consumed": (
        ("orchestrator_result", "acquisition.cumulative.jobs_quota_consumed"),),
    "jobs_unique_kept": (
        ("orchestrator_result", "acquisition.cumulative.jobs_unique_kept"),),
    "cross_query_duplicates": (
        ("orchestrator_result", "acquisition.cumulative.cross_query_duplicates"),),
    "cross_source_duplicates": (
        ("orchestrator_result", "acquisition.cumulative.cross_source_duplicates"),),
    "historical_previously_seen": (
        ("orchestrator_result",
         "acquisition.cumulative.historical_previously_seen_duplicates"),),
    "canonical_duplicates_in_run": (
        ("orchestrator_result",
         "acquisition.cumulative.canonical_duplicates_in_run"),),
    "postings_missing_identity": (
        ("orchestrator_result", "acquisition.cumulative.postings_missing_identity"),),
    # -- waterfall unit totals: own file AND the roll-up ------------------------
    "raw_postings": (("waterfall", "unit_totals.postings"),
                     ("orchestrator_result", "waterfall.unit_totals.postings")),
    "unique_opportunities": (("waterfall", "unit_totals.opportunities"),
                             ("orchestrator_result",
                              "waterfall.unit_totals.opportunities")),
    "contacts_with_email": (("waterfall", "unit_totals.contacts"),
                            ("orchestrator_result", "waterfall.unit_totals.contacts")),
    "FINAL_PASS": (("waterfall", "final_pass_count"),
                   ("orchestrator_result", "waterfall.final_pass_count")),
    # -- enrichment funnel -----------------------------------------------------
    "target_role_eligible": (
        ("orchestrator_result", "enrichment.funnel.target_role_eligible"),),
    "companies_considered": (
        ("orchestrator_result", "enrichment.funnel.companies_considered"),),
    "icp_eligible_companies": (
        ("orchestrator_result", "enrichment.funnel.icp_eligible_companies"),),
    "hiring_managers_found": (
        ("orchestrator_result", "enrichment.funnel.hiring_managers_found"),),
    "verified_emails": (("orchestrator_result", "emails.verified"),),
    "unverified_emails": (("orchestrator_result", "emails.unverified"),),
    # -- delivery: own file AND the roll-up ------------------------------------
    "airtable_created": (("delivery", "created"),
                         ("orchestrator_result", "delivery.created")),
    "airtable_submitted": (("delivery", "reviewable_submitted"),
                           ("orchestrator_result", "delivery.reviewable_submitted")),
    "airtable_existing": (("delivery", "skipped_existing"),
                          ("orchestrator_result", "delivery.skipped_existing")),
    "airtable_failed": (("delivery", "failed"),
                        ("orchestrator_result", "delivery.failed")),
}

#: Fields the report needs that a RUN SUMMARY never prints. Recorded in the corpus
#: so the gap is visible on its face rather than discovered as a puzzling
#: "unavailable" halfway through a report.
NEVER_PRINTED = (
    "enrichment.funnel.qualification_input",         # the ONLY source of jobs_reviewed
    "enrichment.funnel.contact_discovery_entered",   # qualified_opportunities
    "waterfall.stages[acquisition_dedup].passed",    # pre-net-new capture recovery
)

_KV = re.compile(r"^(\S+)\s{2,}(.+?)\s*$")
_RUN_ID = re.compile(r"^run_id\s+(\S+)\s*$", re.M)
_TS = re.compile(r"^(\d{8})T(\d{6})Z-")
_STATUSES = ("complete", "incomplete", "partial", "failed", "resumed")


def _put(tree: Dict[str, Any], path: str, value: Any) -> None:
    node = tree
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _as_int(raw: str) -> Optional[int]:
    raw = raw.strip()
    if raw in ("", "None", "n/a", "-"):
        return None
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return None


def parse_log(text: str) -> Optional[Dict[str, Any]]:
    """One run's printed counters.

    Railway interleaves stdout, so keys are read across the whole file rather than
    by position. Safe here because a deployment log holds a single run and every
    mapped key appears once in a summary.
    """
    match = _RUN_ID.search(text)
    if not match:
        return None

    printed: Dict[str, int] = {}
    status, stop_reason = "", ""
    for line in text.splitlines():
        kv = _KV.match(line)
        if not kv:
            continue
        key, raw = kv.group(1), kv.group(2).strip()
        if key in FIELD_MAP:
            value = _as_int(raw)
            if value is not None:
                printed[key] = value
        elif key == "status" and raw in _STATUSES:
            status = raw
        elif key == "stop_reason" and not stop_reason:
            stop_reason = raw

    run_id = match.group(1)
    stamp = _TS.match(run_id)
    started = None
    if stamp:
        day, clock = stamp.group(1), stamp.group(2)
        started = (f"{day[:4]}-{day[4:6]}-{day[6:]}"
                   f"T{clock[:2]}:{clock[2:4]}:{clock[4:]}Z")
    return {
        "run_id": run_id,
        "printed": printed,
        "status": status,
        "stop_reason": stop_reason,
        "started_at": started,
        # The summary carries no completion instant. Attribution therefore falls
        # back to the instant the run_id encodes -- the run's own start. For a
        # window boundary that is the conservative choice: a run is attributed to
        # the window it began in.
        "finished_at": started,
    }


def build(runs: List[Dict[str, Any]], out: Path) -> None:
    (out / "run_artifacts").mkdir(parents=True, exist_ok=True)
    for run in runs:
        stems: Dict[str, Dict[str, Any]] = {}
        for key, value in run["printed"].items():
            for stem, path in FIELD_MAP[key]:
                _put(stems.setdefault(stem, {}), path, value)

        run_dir = out / "run_artifacts" / run["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        for stem, payload in stems.items():
            (run_dir / f"{stem}.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
        (run_dir / "run_manifest.json").write_text(json.dumps({
            "run_id": run["run_id"],
            "status": run["status"],
            "stop_reason": run["stop_reason"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "mode": "live_acquisition_and_enrichment",
            "_provenance": "reconstructed from " + run["source_log"],
        }, indent=2), encoding="utf-8")
        (run_dir / "run_status.json").write_text(json.dumps({
            "run_id": run["run_id"],
            "status": run["status"],
            "stop_reason": run["stop_reason"],
        }, indent=2), encoding="utf-8")
        print("  {run}  status={status:<9} {n} printed counters -> {stems}".format(
            run=run["run_id"], status=run["status"] or "unknown",
            n=len(run["printed"]),
            stems=",".join(sorted(stems)) or "none"))


def main() -> int:
    evidence, out = Path(sys.argv[1]), Path(sys.argv[2])
    runs = []
    for log in sorted(evidence.glob("*.log")):
        parsed = parse_log(log.read_text(encoding="utf-8", errors="replace"))
        if parsed:
            parsed["source_log"] = log.name
            runs.append(parsed)

    build(runs, out)
    (out / "CORPUS_PROVENANCE.json").write_text(json.dumps({
        "built_from": "railway deployment logs (RUN SUMMARY blocks)",
        "runs": [r["run_id"] for r in runs],
        "fields_the_summary_never_prints": list(NEVER_PRINTED),
        "caveat": "A lower bound on production's reporting capability. Metrics "
                  "reading fields the summary does not print read UNAVAILABLE here "
                  "even where production measures them.",
    }, indent=2), encoding="utf-8")
    print("corpus written to {} ({} run(s))".format(out, len(runs)))
    return 0 if runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
