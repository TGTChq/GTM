"""Render Brett's report twice and require the two to AGREE.

    python brett_ab.py <artifact_root> <window_start_iso>

  A  heavy run artifacts + the reporting ledger, exactly as production holds them;
  B  the BACKFILLED ledger alone, in a copy where run_artifacts does not exist.

B is what the report will be reading once retention has evicted this period's heavy
artifacts, which for the 2026-09-04 run happens well before the 2026-09-11 report.
Equality is therefore an acceptance check on the durable record, not a formatting
check: it asks whether the compact store still answers everything the artifacts did.

The backfill is the REAL `orchestrator.run_ledger.backfill_from_artifacts`, run on an
isolated copy in the same order the pipeline runs it. Production state is not read
and not written; the window is built in memory, so no anchor is consumed and no
receipt is created.

Compared: stakeholder text, contributing runs, values, completeness status, and
counted unit. NOT compared: the artifact field each number came from, which is
expected to differ -- that is the whole point of a compact record.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator.run_ledger import LEDGER_STORE, backfill_from_artifacts, read_entries  # noqa: E402
from weekly_report.render import render_stakeholder_summary  # noqa: E402
from weekly_report.report import build_report  # noqa: E402
from weekly_report.timewindow import anchored_window  # noqa: E402

KEYS = ("jobs_captured", "jobs_reviewed", "qualified_opportunities",
        "contacts_found", "sent_to_airtable", "sent_to_instantly")


def _facts(report):
    """Everything the two renderings must agree on."""
    return {
        "runs": sorted(report.run_ids),
        "metrics": {k: {"value": report.metrics[k].value,
                        "status": report.metrics[k].status,
                        "unit": report.metrics[k].counted_unit,
                        "runs": sorted(report.metrics[k].contributing_run_ids)}
                    for k in KEYS if k in report.metrics},
    }


def main() -> int:
    source = Path(sys.argv[1])
    start = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    window = anchored_window(start, now, tz_name="America/Los_Angeles")

    # Isolated copy. The original is never touched.
    work = Path(tempfile.mkdtemp(prefix="ab_")) / "orchestrator_v2"
    shutil.copytree(source, work)

    before = {e["run_id"] for e in read_entries(work)[0]}
    result = backfill_from_artifacts(work)
    after = {e["run_id"] for e in read_entries(work)[0]}
    reconstructed = sorted(e["run_id"] for e in read_entries(work)[0]
                           if e.get("backfilled_from_artifacts"))

    instantly = None
    if os.environ.get("PREVIEW_INSTANTLY") == "1":
        import config
        from weekly_report.external import collect_instantly
        instantly = collect_instantly(window, cfg=config)

    report_a = build_report(window, artifact_roots=[str(work)], instantly=instantly, now=now)
    text_a = render_stakeholder_summary(report_a)

    # B: the ledger, and nothing else. Copying only that directory is the point --
    # it reproduces the state retention leaves behind.
    ledger_only = Path(tempfile.mkdtemp(prefix="ledger_only_")) / "orchestrator_v2"
    ledger_only.mkdir(parents=True)
    shutil.copytree(work / LEDGER_STORE, ledger_only / LEDGER_STORE)
    assert not (ledger_only / "run_artifacts").exists(), "B must have no heavy artifacts"

    report_b = build_report(window, artifact_roots=[str(ledger_only)], instantly=instantly, now=now)
    text_b = render_stakeholder_summary(report_b)

    facts_a, facts_b = _facts(report_a), _facts(report_b)
    agree = text_a == text_b and facts_a == facts_b

    print(f"ledger entries before backfill : {sorted(before)}")
    print(f"ledger entries after backfill  : {sorted(after)}")
    print(f"written by backfill            : {result.get('written')}")
    print(f"marked as reconstructed        : {reconstructed}")
    print(f"period                         : {window.start_utc.isoformat()} -> {window.end_utc.isoformat()}")
    print(f"A runs                         : {facts_a['runs']}")
    print(f"B runs (ledger only)           : {facts_b['runs']}")
    print(f"stakeholder text identical     : {text_a == text_b}")
    print(f"values/status/units identical  : {facts_a == facts_b}")
    print(f"ACCEPTED                       : {agree}")
    if not agree:
        for key in KEYS:
            a, b = facts_a["metrics"].get(key), facts_b["metrics"].get(key)
            if a != b:
                print(f"  DIFFERS {key}: A={a} B={b}")
        if text_a != text_b:
            print("--- A ---"); print(text_a)
            print("--- B ---"); print(text_b)
    print()
    print("=" * 72)
    print(text_a)
    print("=" * 72)
    print()
    print("RUN CENSUS (A)")
    ca = report_a.census
    print(f"  all_reconcile: {ca['all_reconcile']}")
    for day, run_ids in ca["included_runs_by_local_day"].items():
        print(f"  {day}: {len(run_ids)} run(s) -> {run_ids}")
    for row in ca["runs"]:
        contrib = {k: v["value"] for k, v in row["contributes"].items()}
        print(f"  {row['run_id']}  {row['state']:<9} {row['decision']:<8} "
              f"evidence={row['evidence']:<16} reconstructed={row['reconstructed']}")
        print(f"        contributes {contrib}")
        if row["reason"]:
            print(f"        reason: {row['reason']}")
    print("  reconciliation:")
    for key, chk in ca["reconciles"].items():
        print(f"    {key:26s} census={str(chk['census_total']):>6} "
              f"reported={str(chk['reported_value']):>6} agrees={chk['agrees']}")
    print(f"  census(B) all_reconcile: {report_b.census['all_reconcile']}  "
          f"included={len(report_b.census['included'])}")
    print()
    print("PROVENANCE (A)")
    for key in KEYS:
        m = report_a.metrics.get(key)
        if m is None:
            continue
        print(f"  {m.key:26s} {str(m.value):>6}  {m.status:10s} unit={m.counted_unit}")
        for line in m.evidence:
            print(f"        <- {line}")
        if m.runs_missing_field:
            print(f"        silent runs: {m.runs_missing_field}")
    return 0 if agree else 1


if __name__ == "__main__":
    raise SystemExit(main())
