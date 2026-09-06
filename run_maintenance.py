"""Bounded maintenance on the production volume. Acquires nothing, delivers nothing.

WHY THIS EXISTS. The recovery and reporting work needs the production volume, and
the volume is reachable only from inside a running container. The only container
that ever ran was the daily pipeline, so every remaining acceptance item was stuck
behind "catch a short cron window" -- or behind starting the pipeline, which buys
jobs. This is the narrow entry point instead: it does the maintenance and nothing
else, and it prints its whole result to stdout so the evidence survives the
container exiting.

WHAT IT CANNOT DO, structurally and not by convention:

  * no acquisition -- it never imports an acquisition adapter and never calls a
    provider; it also refuses to start unless FANTASTIC_JOBS_ENABLED is falsey;
  * no paid enrichment -- it never imports Apollo/Hunter and makes no HTTP call
    except the OPTIONAL read-only Instantly count the report needs, which is off
    unless --instantly is passed;
  * no Slack -- the reporter is invoked with slack disabled and no webhook read;
  * no writes to Airtable or Instantly -- neither client is imported;
  * no reporting anchor advanced and no receipt written -- the window is built in
    memory and nothing is persisted by the report path.

WHAT IT WRITES, and the order matters:

  1. a BACKUP of the reporting ledger and the run manifests, before anything else;
  2. `pending_work` adoption from existing run artifacts -- idempotent, bounded;
  3. nothing else.

    python run_maintenance.py --artifact-root /app/data/state/orchestrator_v2 \
        --window-start 2026-09-04T07:00:00Z [--instantly]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import config


def _say(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _refuse_if_acquisition_is_live() -> None:
    """A maintenance pass must never run while the paid source is armed."""
    if bool(getattr(config, "FANTASTIC_JOBS_ENABLED", False)):
        print("REFUSING: FANTASTIC_JOBS_ENABLED is true. Maintenance runs only while "
              "paid acquisition is paused, so it can never be confused with a run.")
        raise SystemExit(2)


def backup(root: Path, out: Path) -> dict:
    """Copy the evidence this pass could touch, BEFORE it touches anything."""
    info = {"ledger_entries": 0, "run_manifests": 0, "path": str(out)}
    out.mkdir(parents=True, exist_ok=True)
    ledger = root / "reporting_ledger"
    if ledger.is_dir():
        shutil.copytree(ledger, out / "reporting_ledger", dirs_exist_ok=True)
        info["ledger_entries"] = len(list((out / "reporting_ledger").glob("*.json")))
    artifacts = root / "run_artifacts"
    if artifacts.is_dir():
        for run_dir in sorted(d for d in artifacts.iterdir() if d.is_dir()):
            for name in ("run_manifest.json", "run_status.json",
                         "orchestrator_result.json", "waterfall.json", "delivery.json"):
                src = run_dir / name
                if src.is_file():
                    dst = out / "run_artifacts" / run_dir.name / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    info["run_manifests"] += 1
    return info


def inventory(root: Path) -> dict:
    """What the volume actually holds, per run -- the reconciliation input."""
    rows = []
    artifacts = root / "run_artifacts"
    if artifacts.is_dir():
        for run_dir in sorted(d for d in artifacts.iterdir() if d.is_dir()):
            postings = run_dir / "enrichment" / "postings.json"
            entry = {"run_id": run_dir.name,
                     "has_orchestrator_result": (run_dir / "orchestrator_result.json").is_file(),
                     "has_postings_json": postings.is_file(),
                     "opportunities_in_postings": None}
            if postings.is_file():
                try:
                    data = json.loads(postings.read_text(encoding="utf-8"))
                    jobs = data.get("jobs") if isinstance(data, dict) else data
                    entry["opportunities_in_postings"] = len(jobs or [])
                except (OSError, ValueError) as exc:
                    entry["opportunities_in_postings"] = f"unreadable: {type(exc).__name__}"
            rows.append(entry)
    return {"runs": rows, "unit": "normalized_opportunity"}


def reconcile(root: Path, run_id: str) -> dict:
    """Reconcile ONE run's retained payloads against what its result claims.

    Counting units are kept apart deliberately: `postings.json` holds NORMALIZED
    OPPORTUNITIES, while `net_new_jobs_captured` counts postings that survived
    cross-run dedupe. They are expected to agree for a run that reached enrichment,
    and any difference is reported rather than explained away.
    """
    run_dir = root / "run_artifacts" / run_id
    out = {"run_id": run_id, "artifact_dir_present": run_dir.is_dir(),
           "net_new_jobs_captured": None, "opportunities_retained": None,
           "identities_distinct": None, "unavailable": []}
    if not run_dir.is_dir():
        out["unavailable"].append("run_artifacts directory absent")
        return out

    result = run_dir / "orchestrator_result.json"
    if result.is_file():
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
            out["net_new_jobs_captured"] = (
                ((data.get("acquisition") or {}).get("cumulative") or {})
                .get("net_new_jobs_captured"))
        except (OSError, ValueError) as exc:
            out["unavailable"].append(f"orchestrator_result unreadable: {type(exc).__name__}")
    else:
        out["unavailable"].append("orchestrator_result.json absent")

    postings = run_dir / "enrichment" / "postings.json"
    if postings.is_file():
        try:
            from retrieval_measurement.accounting import posting_identity
            data = json.loads(postings.read_text(encoding="utf-8"))
            jobs = [j for j in (data.get("jobs") or []) if isinstance(j, dict)]
            out["opportunities_retained"] = len(jobs)
            keys = set()
            for job in jobs:
                try:
                    strength, key = posting_identity(job)
                    if key and strength != "none":
                        keys.add(key)
                except Exception:  # noqa: BLE001
                    pass
            out["identities_distinct"] = len(keys)
        except (OSError, ValueError) as exc:
            out["unavailable"].append(f"postings.json unreadable: {type(exc).__name__}")
    else:
        out["unavailable"].append("enrichment/postings.json absent (pruned or never written)")

    a, b = out["net_new_jobs_captured"], out["opportunities_retained"]
    out["agrees"] = (a == b) if (a is not None and b is not None) else None
    return out


def adopt(root: Path) -> dict:
    """Lift retained opportunity lists into custody. Idempotent and bounded."""
    from orchestrator import pending_work
    from orchestrator.state import STORES  # noqa: F401  (documents the store name)

    store = root / pending_work.STORE
    seen_terminal: set = set()
    try:
        seen_path = root / "seen_suppression" / "seen.json"
        if seen_path.is_file():
            data = json.loads(seen_path.read_text(encoding="utf-8"))
            ids = data.get("postings") if isinstance(data, dict) else data
            seen_terminal = {str(i) for i in (ids or [])}
    except (OSError, ValueError):
        seen_terminal = set()

    before = pending_work.summary(store)
    result = pending_work.adopt_from_artifacts(
        root, store,
        limit=int(getattr(config, "PENDING_WORK_RESUME_MAX_PER_RUN", 2000) or 2000),
        exclude_keys=seen_terminal)
    after = pending_work.summary(store)
    return {"before": before, "adoption": result, "after": after,
            "terminal_ids_excluded": len(seen_terminal)}


def drop_empty_run(root: Path, run_id: str) -> dict:
    """Remove a PHANTOM run: an artifact directory with no artifacts, plus its
    ledger entry. Refuses on anything that carries evidence.

    A maintenance pass used to construct a StateManager, which creates a run
    directory; the ledger backfill then lifted that empty directory in as an
    INTERRUPTED RUN, and the report counted it as an eligible run that had failed
    to record its metrics. That single phantom degraded every headline metric in
    Brett's report from `measured` to `partial`. The entry point no longer creates
    one; this removes the one already written.

    Guarded three ways: the directory must contain no files at all, the ledger
    entry must carry no metrics, and its state must not be complete.
    """
    out = {"run_id": run_id, "removed_dir": False, "removed_ledger": False,
           "refused": ""}
    run_dir = root / "run_artifacts" / run_id
    files = [f for f in run_dir.rglob("*") if f.is_file()] if run_dir.is_dir() else []
    if files:
        out["refused"] = f"directory holds {len(files)} file(s); not a phantom"
        return out

    from orchestrator.run_ledger import ledger_dir

    entry_path = ledger_dir(root) / f"{run_id}.json"
    if entry_path.is_file():
        try:
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            out["refused"] = f"ledger entry unreadable: {type(exc).__name__}"
            return out
        if entry.get("metrics"):
            out["refused"] = "ledger entry carries metrics; not a phantom"
            return out
        if str(entry.get("state") or "").lower() == "complete":
            out["refused"] = "ledger entry is complete; not a phantom"
            return out
        entry_path.unlink()
        out["removed_ledger"] = True
    if run_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)
        out["removed_dir"] = not run_dir.is_dir()
    return out


def ab_and_report(root: Path, window_start: str, use_instantly: bool) -> int:
    """The real artifacts+ledger vs ledger-only comparison, on production files."""
    from orchestrator.run_ledger import (LEDGER_STORE, backfill_from_artifacts,
                                         read_entries)
    from weekly_report.render import render_stakeholder_summary
    from weekly_report.report import build_report
    from weekly_report.timewindow import anchored_window

    start = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    window = anchored_window(start, now, tz_name="America/Los_Angeles")

    # ISOLATED COPY. Production state is read, never written, by this comparison.
    work = Path(tempfile.mkdtemp(prefix="ab_")) / "orchestrator_v2"
    shutil.copytree(root, work, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.tmp", ".run.lock"))
    before = {e["run_id"] for e in read_entries(work)[0]}
    lift = backfill_from_artifacts(work)
    after = {e["run_id"] for e in read_entries(work)[0]}

    instantly = None
    if use_instantly:
        from weekly_report.external import collect_instantly
        instantly = collect_instantly(window, cfg=config)

    report_a = build_report(window, artifact_roots=[str(work)], instantly=instantly, now=now)
    text_a = render_stakeholder_summary(report_a)

    ledger_only = Path(tempfile.mkdtemp(prefix="ledger_only_")) / "orchestrator_v2"
    ledger_only.mkdir(parents=True)
    if (work / LEDGER_STORE).is_dir():
        shutil.copytree(work / LEDGER_STORE, ledger_only / LEDGER_STORE)
    assert not (ledger_only / "run_artifacts").exists()
    report_b = build_report(window, artifact_roots=[str(ledger_only)],
                            instantly=instantly, now=now)
    text_b = render_stakeholder_summary(report_b)

    keys = ("jobs_captured", "jobs_reviewed", "qualified_opportunities",
            "contacts_found", "sent_to_airtable", "sent_to_instantly")

    def facts(rep):
        return {"runs": sorted(rep.run_ids),
                "metrics": {k: {"value": rep.metrics[k].value,
                                "status": rep.metrics[k].status,
                                "unit": rep.metrics[k].counted_unit}
                            for k in keys if k in rep.metrics}}

    fa, fb = facts(report_a), facts(report_b)
    agree = (text_a == text_b) and (fa == fb)

    print(f"ledger before backfill : {sorted(before)}")
    print(f"ledger after backfill  : {sorted(after)}")
    print(f"written by backfill    : {lift.get('written')}")
    print(f"period                 : {window.start_utc.isoformat()} -> {window.end_utc.isoformat()}")
    print(f"A runs                 : {fa['runs']}")
    print(f"B runs (ledger only)   : {fb['runs']}")
    print(f"text identical         : {text_a == text_b}")
    print(f"values identical       : {fa == fb}")
    print(f"ACCEPTED               : {agree}")
    if not agree:
        for k in keys:
            if fa["metrics"].get(k) != fb["metrics"].get(k):
                print(f"  DIFFERS {k}: A={fa['metrics'].get(k)} B={fb['metrics'].get(k)}")

    _say("BRETT'S REPORT (from production evidence)")
    print(text_a)

    _say("RUN CENSUS")
    census = report_a.census
    print(f"all_reconcile: {census['all_reconcile']}")
    for day, ids in census["included_runs_by_local_day"].items():
        print(f"  {day}: {len(ids)} run(s) -> {ids}")
    for row in census["runs"]:
        print(f"  {row['run_id']}  {row['state']:<9} {row['decision']:<8} "
              f"evidence={row['evidence']} reconstructed={row['reconstructed']}")
        print(f"        contributes {({k: v['value'] for k, v in row['contributes'].items()})}")
    for key, chk in census["reconciles"].items():
        print(f"    {key:26s} census={chk['census_total']} reported={chk['reported_value']} "
              f"agrees={chk['agrees']}")

    _say("PROVENANCE")
    for k in keys:
        m = report_a.metrics.get(k)
        if m is not None:
            print(f"  {m.key:26s} {str(m.value):>8}  {m.status:12s} unit={m.counted_unit}")
    return 0 if agree else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifact-root", required=True)
    ap.add_argument("--window-start", default="2026-09-04T07:00:00Z")
    ap.add_argument("--reconcile-run", default="20260906T030230Z-2f74ac7c")
    ap.add_argument("--instantly", action="store_true",
                    help="read-only Instantly count for the report window")
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--drop-empty-run", default="",
                    help="remove a phantom run dir + ledger entry "
                         "(refuses if either carries evidence)")
    a = ap.parse_args(argv)

    _refuse_if_acquisition_is_live()
    root = Path(a.artifact_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.backup_dir) if a.backup_dir else root / "maintenance_backups" / stamp

    _say(f"MAINTENANCE {stamp}  root={root}")
    print(f"exists={root.is_dir()}  FANTASTIC_JOBS_ENABLED={config.FANTASTIC_JOBS_ENABLED}")
    if not root.is_dir():
        print("REFUSING: artifact root does not exist. Wrong volume or wrong path.")
        return 2

    _say("1. BACKUP (before any mutation)")
    print(json.dumps(backup(root, out), indent=2))

    _say("2. VOLUME INVENTORY")
    print(json.dumps(inventory(root), indent=2))

    _say(f"3. RECONCILE {a.reconcile_run}")
    print(json.dumps(reconcile(root, a.reconcile_run), indent=2))

    _say("4. ADOPT INTERRUPTED WORK INTO pending_work")
    print(json.dumps(adopt(root), indent=2, default=str))

    if a.drop_empty_run:
        _say(f"4b. DROP PHANTOM RUN {a.drop_empty_run}")
        print(json.dumps(drop_empty_run(root, a.drop_empty_run), indent=2))

    _say("5. REPORTING: artifacts+ledger VS ledger-only, on production files")
    rc = ab_and_report(root, a.window_start, a.instantly)

    _say(f"MAINTENANCE COMPLETE rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
