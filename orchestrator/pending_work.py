"""Durable custody of postings that were paid for but not finished.

THE FAILURE THIS EXISTS FOR. On 2026-09-06 a run bought 5,444 provider rows, kept
226 net-new postings, qualified them, and then hit Apollo's
``BILLING.LIMIT.CREDITS_EXHAUSTED``. Every safeguard behaved as designed: nothing
was committed to suppression (``terminal_posting_ids()`` is empty when no lead
reaches FINAL_PASS/REJECT) and the watermark stayed in flight.

The work was lost anyway. "Not suppressed" only means a posting MAY be processed
again; it does not mean anything hands it back. The window's per-source offsets had
already advanced (100 -> 2822) and are replayed FORWARD, so the next run resumes
past the rows it already bought.

FOUR PROPERTIES, and the third is the one that is easy to get wrong.

1. Custody is taken BEFORE the continuation is irreversibly advanced -- not merely
   "before enrichment". The offsets become durable inside
   ``DateCreatedWatermarkEngine.checkpoint()``, which runs at the END of acquisition
   and therefore BEFORE the pipeline has even seen the postings. So the adapter
   calls a custody hook first and refuses to persist offsets if it fails: re-billing
   a page is recoverable, losing it is not.

2. The store is a SIBLING of ``run_artifacts``, because ``StateStore.prune`` only
   ever deletes under ``run_artifacts``. Retention cannot destroy paid-for work.

3. Work never leaves custody silently. Every departure is written to an append-only
   audit with an explicit outcome, and the three outcomes are kept apart:
   ``terminal`` (finished, the only success), ``deduped`` (the row was not new work
   in the first place), and ``expired_unresolved`` (custody aged out with the work
   STILL UNDONE). An expiry is not a completion and must never read as one, so the
   payloads are moved to ``expired/`` rather than deleted -- the outcome is
   auditable and the data is still recoverable.

4. Adoption re-enters postings into the SAME enrichment and delivery path, so every
   existing gate, idempotency rule and budget still applies. It never re-acquires
   and never re-bills: these rows are already bought.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from retrieval_measurement.accounting import posting_identity

logger = logging.getLogger(__name__)

#: Registered in ``StateStore.STORES``: the directory exists and, crucially, sits
#: OUTSIDE ``run_artifacts`` where prune cannot reach it.
STORE = "pending_work"
SCHEMA = "pending-work/1"

#: Where custody ends. Only ``OUTCOME_TERMINAL`` means the work was done.
OUTCOME_TERMINAL = "terminal"
OUTCOME_DEDUPED = "deduped"
OUTCOME_EXPIRED = "expired_unresolved"

AUDIT = "_audit.jsonl"
EXPIRED_DIR = "expired"
IMPORTED = "_imported_from_artifacts.json"

#: Where a completed run leaves the opportunity list it handed to enrichment.
#: NOTE THE UNIT: these are NORMALIZED OPPORTUNITIES (post-dedupe, one dict per
#: surviving posting), not the raw provider payloads and not necessarily every row
#: the run billed. Recovery counts them and reports the count; it never asserts
#: that the file contains everything the run acquired.
ARTIFACT_RELPATH = ("enrichment", "postings.json")


def _key(job: Any) -> str:
    try:
        strength, key = posting_identity(job)
    except Exception:  # noqa: BLE001 - a malformed row must not break custody
        return ""
    return "" if not key or strength == "none" else str(key)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a torn file would lose the very work we hold


def _entry_files(base: Path) -> List[Path]:
    """Custody files only -- never the audit, and never the expired archive."""
    return sorted(p for p in base.glob("*.json") if not p.name.startswith("_"))


def _audit(base: Path, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        base.mkdir(parents=True, exist_ok=True)
        with (base / AUDIT).open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, default=str) + "\n")
    except OSError as exc:
        logger.warning("Could not append pending-work audit: %s", exc)


# -- taking custody ----------------------------------------------------------


def record(store: str | Path, run_id: str, opportunities: Sequence[Any]) -> Dict[str, Any]:
    """Take custody of postings. Idempotent per posting identity.

    Returns ``{"ok": bool, ...}``. ``ok`` is what the adapter's pre-checkpoint hook
    checks before it allows the continuation to advance.
    """
    result: Dict[str, Any] = {"ok": True, "recorded": 0, "already_held": 0,
                              "unidentifiable": 0, "path": ""}
    if not opportunities:
        return result
    base = Path(store)
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{run_id}.json"
        held = _read(path)
        jobs: List[Any] = list(held.get("jobs") or [])
        known = {_key(j) for j in jobs}
        known.discard("")
        for job in opportunities:
            key = _key(job)
            if not key:
                result["unidentifiable"] += 1
                continue
            if key in known:
                result["already_held"] += 1
                continue
            known.add(key)
            jobs.append(job)
            result["recorded"] += 1
        _write(path, {"schema": SCHEMA, "run_id": run_id,
                      "recorded_at": _now().isoformat(), "jobs": jobs})
        result["path"] = str(path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not take custody of acquired work for %s: %s", run_id, exc)
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


# -- handing it back ---------------------------------------------------------


def load(
    store: str | Path,
    *,
    exclude_run_id: str = "",
    limit: Optional[int] = None,
    exclude_keys: Iterable[str] = (),
) -> Tuple[List[Any], Dict[str, Any]]:
    """Postings still owed work, oldest run first.

    ``exclude_keys`` is the current run's own opportunity keys: a posting this run
    re-acquired anyway must not be handed back twice. ``limit`` bounds how much
    unfinished work one run adopts -- unbounded resume would let a long outage hand
    a single run more enrichment than its budget can serve.

    Expiry is NOT applied here: it is a separate, audited step, so nothing ever
    disappears as a side effect of a read.
    """
    info: Dict[str, Any] = {"files": 0, "offered": 0, "adopted": 0,
                            "skipped_current_run": 0, "runs": []}
    base = Path(store)
    if not base.is_dir():
        return [], info

    skip = {str(k) for k in exclude_keys if k}
    out: List[Any] = []
    seen_keys: Set[str] = set()
    for path in _entry_files(base):
        held = _read(path)
        if held.get("schema") != SCHEMA:
            continue
        run_id = str(held.get("run_id") or path.stem)
        if exclude_run_id and run_id == exclude_run_id:
            info["skipped_current_run"] += 1
            continue
        info["files"] += 1
        adopted_here = 0
        for job in held.get("jobs") or []:
            info["offered"] += 1
            key = _key(job)
            if not key or key in skip or key in seen_keys:
                continue
            if limit is not None and len(out) >= int(limit):
                break
            seen_keys.add(key)
            out.append(job)
            adopted_here += 1
        if adopted_here:
            info["runs"].append({"run_id": run_id, "adopted": adopted_here})
        if limit is not None and len(out) >= int(limit):
            break
    info["adopted"] = len(out)
    return out, info


def release(
    store: str | Path,
    ids: Iterable[str],
    *,
    outcome: str = OUTCOME_TERMINAL,
    run_id: str = "",
) -> Dict[str, Any]:
    """Remove postings from custody, recording WHY.

    ``OUTCOME_TERMINAL`` takes the same id set the pipeline commits to suppression,
    so custody ends exactly when the posting is genuinely finished -- never on a
    deferred outcome, which is the distinction that makes a provider outage
    survivable. ``OUTCOME_DEDUPED`` retires rows that dedupe proved were not new
    work.
    """
    info = {"released": 0, "files_emptied": 0, "still_held": 0, "outcome": outcome}
    base = Path(store)
    if not base.is_dir():
        return info
    done = {str(i) for i in ids if i}
    if not done:
        for path in _entry_files(base):
            info["still_held"] += len(_read(path).get("jobs") or [])
        return info

    stamp = _now().isoformat()
    trail: List[Dict[str, Any]] = []
    for path in _entry_files(base):
        held = _read(path)
        if held.get("schema") != SCHEMA:
            continue
        jobs = list(held.get("jobs") or [])
        keep = []
        for job in jobs:
            key = _key(job)
            pid = str((job or {}).get("posting_id") or "") if isinstance(job, dict) else ""
            if (key and key in done) or (pid and pid in done):
                info["released"] += 1
                trail.append({"ts": stamp, "run_id": str(held.get("run_id") or path.stem),
                              "released_by": run_id, "key": key or pid,
                              "outcome": outcome})
            else:
                keep.append(job)
        if not keep:
            try:
                path.unlink()
                info["files_emptied"] += 1
            except OSError:
                pass
            continue
        info["still_held"] += len(keep)
        if len(keep) != len(jobs):
            held["jobs"] = keep
            _write(path, held)
    _audit(base, trail)
    return info


def expire(store: str | Path, *, max_age_days: int, run_id: str = "") -> Dict[str, Any]:
    """Age out custody WITHOUT pretending the work got done.

    An expiry is not a completion. The payloads are moved to ``expired/`` and the
    outcome is written to the audit as ``expired_unresolved``, so the work remains
    recoverable and the record shows plainly that it was never finished. Deleting
    it -- or letting a read silently skip it -- would turn a retention policy into
    invisible data loss, which is the failure this whole store exists to prevent.
    """
    info = {"expired_postings": 0, "expired_runs": 0, "archive": ""}
    base = Path(store)
    if not base.is_dir() or not max_age_days:
        return info
    cutoff = _now() - timedelta(days=int(max_age_days))
    stamp = _now().isoformat()
    archive = base / EXPIRED_DIR
    trail: List[Dict[str, Any]] = []

    for path in _entry_files(base):
        held = _read(path)
        if held.get("schema") != SCHEMA:
            continue
        try:
            recorded = datetime.fromisoformat(str(held.get("recorded_at")))
            if recorded.tzinfo is None:
                recorded = recorded.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if recorded >= cutoff:
            continue
        jobs = list(held.get("jobs") or [])
        if not jobs:
            continue
        held_run = str(held.get("run_id") or path.stem)
        try:
            archive.mkdir(parents=True, exist_ok=True)
            _write(archive / f"{held_run}.json", {
                "schema": SCHEMA, "run_id": held_run,
                "recorded_at": held.get("recorded_at"),
                "expired_at": stamp, "expired_by": run_id,
                "outcome": OUTCOME_EXPIRED,
                "note": "Custody aged out with the work UNFINISHED. Not a terminal "
                        "disposition and not delivered. Payloads retained here so "
                        "the work stays recoverable.",
                "jobs": jobs,
            })
            path.unlink()
        except OSError as exc:
            logger.warning("Could not archive expired pending work %s: %s", held_run, exc)
            continue
        info["expired_postings"] += len(jobs)
        info["expired_runs"] += 1
        trail.append({"ts": stamp, "run_id": held_run, "released_by": run_id,
                      "count": len(jobs), "outcome": OUTCOME_EXPIRED})
    if info["expired_runs"]:
        info["archive"] = str(archive)
    _audit(base, trail)
    return info


# -- recovering work that predates the store ---------------------------------


def adopt_from_artifacts(
    root: str | Path,
    store: str | Path,
    *,
    limit: Optional[int] = None,
    max_runs: int = 8,
    exclude_keys: Iterable[str] = (),
) -> Dict[str, Any]:
    """Import opportunity lists left by runs that finished before custody existed.

    The 2026-09-06 run wrote its 226 postings to
    ``run_artifacts/<run_id>/enrichment/postings.json`` and nothing ever read them
    back; retention deletes that file after four runs. This lifts such files into
    custody BEFORE prune can remove them -- the same ordering the reporting ledger's
    backfill already relies on.

    Idempotent: a run is marked imported once, and ``record`` dedupes by identity
    besides. Bounded by ``max_runs`` and ``limit``.

    COUNTING UNIT: ``postings.json`` holds NORMALIZED OPPORTUNITIES -- the deduped
    list handed to enrichment. It is not the raw provider payload set and is not
    guaranteed to contain every row the run billed. The per-run counts are reported
    so the difference stays visible instead of being assumed away.
    """
    info: Dict[str, Any] = {"runs_scanned": 0, "runs_imported": 0,
                            "postings_imported": 0, "runs": [], "already_imported": 0}
    base_root, base_store = Path(root), Path(store)
    artifacts = base_root / "run_artifacts"
    if not artifacts.is_dir():
        return info

    marker_path = base_store / IMPORTED
    marker = _read(marker_path)
    done: Set[str] = set(marker.get("runs") or [])
    skip = {str(k) for k in exclude_keys if k}
    budget = None if limit is None else int(limit)

    for run_dir in sorted((d for d in artifacts.iterdir() if d.is_dir()), reverse=True):
        if info["runs_imported"] >= int(max_runs):
            break
        run_id = run_dir.name
        if run_id in done:
            info["already_imported"] += 1
            continue
        source = run_dir.joinpath(*ARTIFACT_RELPATH)
        if not source.is_file():
            continue
        info["runs_scanned"] += 1
        payload = _read(source)
        jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
        found = len(jobs)
        jobs = [j for j in jobs if _key(j) and _key(j) not in skip]
        if budget is not None:
            jobs = jobs[:max(0, budget)]
        outcome = record(base_store, run_id, jobs) if jobs else {"ok": True, "recorded": 0}
        if not outcome.get("ok"):
            continue
        done.add(run_id)
        info["runs_imported"] += 1
        info["postings_imported"] += int(outcome.get("recorded", 0))
        if budget is not None:
            budget -= int(outcome.get("recorded", 0))
        info["runs"].append({
            "run_id": run_id,
            "opportunities_in_artifact": found,
            "eligible_after_exclusions": len(jobs),
            "newly_held": int(outcome.get("recorded", 0)),
            "already_held": int(outcome.get("already_held", 0)),
            "unit": "normalized_opportunity",
        })
        if budget is not None and budget <= 0:
            break

    if info["runs_imported"]:
        try:
            base_store.mkdir(parents=True, exist_ok=True)
            _write(marker_path, {"schema": SCHEMA, "runs": sorted(done),
                                 "updated_at": _now().isoformat()})
        except OSError as exc:
            logger.warning("Could not persist pending-work import marker: %s", exc)
    return info


# -- observability -----------------------------------------------------------


def pending_run_ids(store: str | Path) -> Set[str]:
    """Runs whose artifacts must survive retention: they still owe work, or their
    opportunity list has not been imported yet."""
    base = Path(store)
    out: Set[str] = set()
    if not base.is_dir():
        return out
    for path in _entry_files(base):
        held = _read(path)
        if held.get("schema") == SCHEMA and (held.get("jobs") or []):
            out.add(str(held.get("run_id") or path.stem))
    return out


def summary(store: str | Path) -> Dict[str, Any]:
    """What is currently owed, for the run summary and the weekly report."""
    base = Path(store)
    runs: List[Dict[str, Any]] = []
    total = 0
    if base.is_dir():
        for path in _entry_files(base):
            held = _read(path)
            if held.get("schema") != SCHEMA:
                continue
            count = len(held.get("jobs") or [])
            total += count
            runs.append({"run_id": str(held.get("run_id") or path.stem),
                         "postings": count,
                         "recorded_at": str(held.get("recorded_at") or "")})
    expired_total = 0
    archive = base / EXPIRED_DIR
    if archive.is_dir():
        for path in sorted(archive.glob("*.json")):
            expired_total += len(_read(path).get("jobs") or [])
    return {"pending_postings": total, "pending_runs": len(runs), "runs": runs,
            "expired_unresolved_postings": expired_total,
            "unit": "normalized_opportunity"}
