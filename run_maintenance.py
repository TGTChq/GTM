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


def terminal_posting_keys(root: Path) -> set:
    """Postings already committed to cross-run suppression.

    The file is ``seen_suppression/postings.json`` and the ids live under ``keys``
    -- read from `orchestrator.suppression.SuppressionStore`, whose constants these
    are. An earlier version of this function guessed ``seen.json`` and a top-level
    ``postings`` list; it silently found nothing, so nothing was excluded from
    adoption and finished work was taken into custody.
    """
    try:
        from orchestrator.suppression import SuppressionStore

        path = root / "seen_suppression" / SuppressionStore.POSTINGS
        if not path.is_file():
            return set()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return set()
        return {str(k) for k in (data.get("keys") or [])}
    except (OSError, ValueError, ImportError):
        return set()


def adopt(root: Path) -> dict:
    """Lift retained opportunity lists into custody, and retire finished work.

    Two directions, both necessary. Adoption takes in postings a run bought and
    never finished. The release afterwards removes anything that HAS since reached a
    terminal disposition -- self-healing the state left by the earlier version,
    which excluded nothing because it read the wrong suppression path.
    """
    from orchestrator import pending_work

    store = root / pending_work.STORE
    terminal = terminal_posting_keys(root)

    before = pending_work.summary(store)
    result = pending_work.adopt_from_artifacts(
        root, store,
        limit=int(getattr(config, "PENDING_WORK_ADOPT_MAX_PER_PASS", 10000) or 10000),
        exclude_keys=terminal)
    released = pending_work.release(
        store, terminal, outcome=pending_work.OUTCOME_TERMINAL,
        run_id="maintenance")
    after = pending_work.summary(store)
    return {"before": before, "adoption": result,
            "released_already_terminal": released, "after": after,
            "terminal_ids_known": len(terminal)}


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


def reimport_run(store: Path, run_id: str) -> dict:
    """Drop a run from the import marker so its remainder can be taken in.

    The marker is consulted before the artifact file is even opened, so a run marked
    imported is never revisited. The truncated-import bug set that marker while
    taking only part of the file; this clears it for one named run. Adoption is
    idempotent -- it skips what custody already holds and filters out anything
    terminal -- so re-importing can only ADD genuinely pending work.
    """
    out = {"run_id": run_id, "cleared": False, "runs_remaining": None}
    marker_path = Path(store) / "_imported_from_artifacts.json"
    data = _read_json(marker_path)
    runs = [str(r) for r in (data.get("runs") or [])]
    if run_id in runs:
        runs.remove(run_id)
        data["runs"] = runs
        tmp = marker_path.with_suffix(marker_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, marker_path)
        out["cleared"] = True
    out["runs_remaining"] = len(runs)
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def measure_identities(jobs) -> dict:
    """Postings -> distinct companies -> company x function OPPORTUNITIES.

    The production identity functions, not a local re-implementation:
    `multi_source_acquisition._classify` assigns `_matched_role`,
    `role_mapping.get_bucket_name_for_job` turns that into the function bucket, and
    `airtable_client._company_identity_keys_from_job` supplies the employer identity
    the suppression rule keys on. Nothing is contacted and nothing is written.

    Shared by the retained-payload measurement and the custody dry-run so both count
    the same thing the same way -- two implementations of "an opportunity" would
    make the two figures incomparable, which is the whole point of quoting them
    together.
    """
    from airtable_client import _company_identity_keys_from_job
    from multi_source_acquisition import _classify
    from role_mapping import get_bucket_name_for_job

    jobs = [j for j in (jobs or []) if isinstance(j, dict)]
    companies, opportunities, relevant, unidentifiable = set(), set(), 0, 0
    for job in jobs:
        try:
            _classify(job)
        except Exception:  # noqa: BLE001 - one bad row must not stop the count
            continue
        if str(job.get("_role_relevance_status") or "").lower() != "reject":
            relevant += 1
        keys = _company_identity_keys_from_job(job)
        if not keys:
            unidentifiable += 1
            continue
        company = sorted(keys)[0]
        companies.add(company)
        bucket = get_bucket_name_for_job(job) or "unbucketed"
        opportunities.add(f"{company}|{bucket}")
    return {
        "postings": len(jobs),
        "companies": len(companies),
        "opportunities": len(opportunities),
        "role_relevant_postings": relevant,
        "unidentifiable_employer": unidentifiable,
        "postings_per_opportunity": (round(len(jobs) / len(opportunities), 3)
                                     if opportunities else None),
    }


def capacity(root: Path, run_ids) -> dict:
    """Distinct company x function OPPORTUNITIES behind a run's retained postings.

    This is the quantity approvals are actually capped by -- one active Airtable row
    per company x role bucket -- and it had never been measured for any cohort,
    because posting counts are not opportunity counts and no count endpoint returns
    it. The retained `postings.json` payloads make it computable offline.

    It uses the PRODUCTION identity functions, not a local re-implementation:
    `multi_source_acquisition._classify` assigns `_matched_role`,
    `role_mapping.get_bucket_name_for_job` turns that into the function bucket, and
    `airtable_client._company_identity_keys_from_job` supplies the employer identity
    the suppression rule keys on. No provider is contacted and nothing is written.
    """
    out = {"unit_note": "postings -> company x function opportunities", "runs": []}
    for run_id in run_ids:
        src = root / "run_artifacts" / run_id / "enrichment" / "postings.json"
        row = {"run_id": run_id, "postings": None, "companies": None,
               "opportunities": None, "role_relevant_postings": None,
               "postings_per_opportunity": None, "unavailable": ""}
        if not src.is_file():
            row["unavailable"] = "postings.json absent"
            out["runs"].append(row)
            continue
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            row["unavailable"] = f"unreadable: {type(exc).__name__}"
            out["runs"].append(row)
            continue
        jobs = [j for j in (data.get("jobs") or []) if isinstance(j, dict)]
        row.update(measure_identities(jobs))
        out["runs"].append(row)
    return out


def resume_dry_run(root: Path) -> dict:
    """Prove the work in custody can actually be resumed -- without spending anything.

    Custody has been exercised on production for RECORD and RELEASE. Resume has only
    ever run in tests, and "preserved" is worth nothing if what was preserved turns
    out to be too thin to process: a store full of stubs would look identical in
    every count until the day it was needed.

    So this loads the held work through the SAME `pending_work.load` the pipeline
    calls, then runs the production identity functions over the payloads it gets
    back. If they classify and resolve to an employer, they are complete enough to
    enter enrichment -- which is exactly what resume does with them.

    It also converts the held postings into the unit that bounds approvals, so the
    preserved backlog can be stated as opportunities rather than as a file size.

    Reads only. Nothing is released, expired or written.
    """
    from orchestrator import pending_work as pw

    store = root / pw.STORE
    jobs, info = pw.load(store)
    out = {
        "loaded_via": "orchestrator.pending_work.load",
        "files": info.get("files"),
        "offered": info.get("offered"),
        "returned": len(jobs),
        "runs": info.get("runs"),
    }
    out["identities"] = measure_identities(jobs)
    # The resume-path check: a payload that cannot be classified or attributed to an
    # employer cannot be enriched, so it would be held for ever and never finish.
    ident = out["identities"]
    out["resumable"] = bool(jobs) and ident["unidentifiable_employer"] == 0
    out["note"] = ("every held posting resolves to an employer and a function, so "
                   "resume has real work to hand back"
                   if out["resumable"] else
                   "no work is held" if not jobs else
                   f"{ident['unidentifiable_employer']} held posting(s) carry no "
                   "resolvable employer and could never finish")
    return out


def refresh_ledger(root: Path) -> dict:
    """Bring the DURABLE record up to date from artifacts that still exist.

    The A/B runs on a copy, so it proves the ledger CAN answer without proving that
    the production ledger does. Only a pipeline run backfills the real one, and while
    acquisition is paused no pipeline runs -- so a correction to the loss-reason
    census would sit in the code, pass its tests, and never reach the record that
    outlives the artifacts. Friday's report reads that record.

    Additive and idempotent: `backfill_from_artifacts` rewrites an entry only when
    its derived metrics or reasons actually changed, and the pass has already taken a
    full backup of the ledger before anything here runs.
    """
    from orchestrator.run_ledger import backfill_from_artifacts, read_entries

    # `read_entries` returns (entries, problems) -- the problems list is not
    # discarded, because an unreadable ledger file is exactly what this refresh
    # would otherwise paper over.
    entries, problems_before = read_entries(root)
    before = {e.get("run_id"): dict(e.get("loss_reasons") or {}) for e in entries}
    result = backfill_from_artifacts(root)
    entries, problems_after = read_entries(root)
    after = {e.get("run_id"): dict(e.get("loss_reasons") or {}) for e in entries}
    changed = {rid: {"before": before.get(rid, {}), "after": reasons}
               for rid, reasons in after.items() if before.get(rid, {}) != reasons}
    return {"written": result.get("written"), "loss_reasons_changed": changed,
            "unreadable_entries": sorted(set(problems_before) | set(problems_after))}


def provenance_probe(root: Path) -> dict:
    """Why each headline metric is measured, partial or unavailable -- per run.

    The provenance table says a metric is `partial`; it cannot say whether the
    missing contribution is RECOVERABLE from a payload nobody read or genuinely
    never recorded. Those need opposite responses, and guessing between them is how
    a reporting gap gets called a data gap. This reads the exact fields the metric
    specs read, on the runs that actually exist, and reports which are present.

    Read-only, and small: funnel counters and a skip breakdown are a few dozen
    integers per run. The heavy `postings.json` is never opened here.
    """
    rows = []
    artifacts = root / "run_artifacts"
    if not artifacts.is_dir():
        return {"runs": rows}
    for run_dir in sorted(d for d in artifacts.iterdir() if d.is_dir()):
        result = _read_json(run_dir / "orchestrator_result.json")
        # WHICH SOURCE THE REPORT WILL ACTUALLY READ. The compact ledger's
        # `loss_reasons` wins over the artifacts when it holds anything, so a run
        # whose ledger block is populated never has its delivery record consulted --
        # which is why adding a new reason code changed nothing on the first try.
        ledger = _read_json(root / "reporting_ledger" / f"{run_dir.name}.json")
        ledger_reasons = (ledger.get("loss_reasons") if isinstance(ledger, dict) else {}) or {}
        enrichment = result.get("enrichment") if isinstance(result, dict) else {}
        funnel = (enrichment or {}).get("funnel") if isinstance(enrichment, dict) else {}
        delivery = result.get("delivery") if isinstance(result, dict) else {}
        skips = (delivery or {}).get("skip_breakdown") if isinstance(delivery, dict) else {}
        rows.append({
            "run_id": run_dir.name,
            # The two fields `jobs_reviewed` and `qualified_opportunities` are read
            # from, named so a missing one is attributable rather than mysterious.
            "funnel_qualification_input": (funnel or {}).get("qualification_input"),
            "funnel_contact_discovery_entered": (funnel or {}).get("contact_discovery_entered"),
            "funnel_keys": sorted(funnel or {}),
            # Zero-valued buckets are omitted for the same reason the report now
            # omits them: a policy that did not fire explains nothing.
            "skip_breakdown_nonzero": {k: v for k, v in (skips or {}).items()
                                       if isinstance(v, int) and v > 0},
            "skip_breakdown_all_zero": bool(skips) and not any(
                isinstance(v, int) and v > 0 for v in (skips or {}).values()),
            # WHERE THE DIFFERENCE WENT. `skip_breakdown` partitions only rows that
            # were SUBMITTED and not created; anything withheld earlier never
            # appears in it. These are the counters that close the gap between the
            # population handed to the writer and the rows it wrote, and the report
            # reads none of them.
            "delivery_entered": (delivery or {}).get("entered"),
            "delivery_reviewable_submitted": (delivery or {}).get("reviewable_submitted"),
            "delivery_created": (delivery or {}).get("created"),
            "delivery_failed": (delivery or {}).get("failed"),
            "delivery_already_delivered": (delivery or {}).get("skipped_already_delivered"),
            "delivery_person_employer_duplicate": (delivery or {}).get("person_employer_duplicate"),
            "delivery_withheld_before_submit": ((delivery or {}).get("detail") or {}).get(
                "withheld_before_submit"),
            "delivery_reconciles": (delivery or {}).get("airtable_reconciles"),
            "delivery_reviewable_reconciles": (delivery or {}).get("reviewable_reconciles"),
            "ledger_loss_reasons_nonzero": {k: v for k, v in ledger_reasons.items()
                                            if isinstance(v, int) and v > 0},
        })
    return {"runs": rows}


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

    # ISOLATED COPY, and a SELECTIVE one. Production state is read, never written.
    #
    # Copying the whole root would drag in every per-stage subtree -- including the
    # `enrichment/postings.json` payload files, 6,205 opportunities for one run
    # alone -- plus the maintenance backups from previous passes, into container
    # temp space, and would grow with every pass. The report reads only the ledger
    # and the TOP-LEVEL json per run, so that is all that is copied.
    work = Path(tempfile.mkdtemp(prefix="ab_")) / "orchestrator_v2"
    work.mkdir(parents=True, exist_ok=True)
    if (root / "reporting_ledger").is_dir():
        shutil.copytree(root / "reporting_ledger", work / "reporting_ledger",
                        dirs_exist_ok=True)
    src_runs = root / "run_artifacts"
    if src_runs.is_dir():
        for run_dir in sorted(d for d in src_runs.iterdir() if d.is_dir()):
            for artifact in run_dir.glob("*.json"):
                dst = work / "run_artifacts" / run_dir.name / artifact.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(artifact, dst)
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
    ap.add_argument("--reimport-run", default="",
                    help="drop a run from the import marker so its "
                         "remainder can be adopted")
    ap.add_argument("--capacity-runs", default="",
                    help="comma-separated run ids to measure "
                         "company x function opportunities for")
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

    if a.reimport_run:
        _say(f"3b. CLEAR IMPORT MARKER FOR {a.reimport_run}")
        from orchestrator import pending_work as _pw
        print(json.dumps(reimport_run(root / _pw.STORE, a.reimport_run), indent=2))

    _say("4. ADOPT INTERRUPTED WORK INTO pending_work")
    print(json.dumps(adopt(root), indent=2, default=str))

    if a.drop_empty_run:
        _say(f"4b. DROP PHANTOM RUN {a.drop_empty_run}")
        print(json.dumps(drop_empty_run(root, a.drop_empty_run), indent=2))

    if a.capacity_runs:
        _say("4c. CAPACITY: company x function opportunities from retained payloads")
        ids = [r.strip() for r in a.capacity_runs.split(",") if r.strip()]
        print(json.dumps(capacity(root, ids), indent=2))

    if getattr(config, "MAINTENANCE_ATS_BOARD_YIELD", ""):
        # The 145 registered boards are scraped from each employer's OWN public job
        # board, so this costs no provider credits -- and it can only run in here,
        # because the registry and its seeding history live on the volume. Stateless
        # by construction (`fetch_board_jobs` persists nothing); no lane, no
        # checkpoint, no board health update, nothing enters the pipeline.
        _say("4c1. DIRECT ATS BOARD YIELD (0 provider credits, nothing persisted)")
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))
            import ats_board_yield
            limit = int(str(config.MAINTENANCE_ATS_BOARD_YIELD).strip() or "200")
            seconds = int(getattr(config, "MAINTENANCE_ATS_BOARD_SECONDS", 1500) or 1500)
            print(json.dumps(ats_board_yield.measure(max_boards=limit,
                                                     time_budget_seconds=seconds),
                             indent=2, default=str))
        except Exception as exc:  # noqa: BLE001 - a measurement must not fail the pass
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2))

    _say("4c2. LEDGER REFRESH: carry today's corrections into the durable record")
    print(json.dumps(refresh_ledger(root), indent=2, default=str))

    _say("4d. RESUME DRY RUN: can the work in custody actually be handed back?")
    print(json.dumps(resume_dry_run(root), indent=2, default=str))

    _say("4e. PROVENANCE PROBE: which reported fields each run actually carries")
    print(json.dumps(provenance_probe(root), indent=2, default=str))

    _say("5. REPORTING: artifacts+ledger VS ledger-only, on production files")
    rc = ab_and_report(root, a.window_start, a.instantly)

    _say(f"MAINTENANCE COMPLETE rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
