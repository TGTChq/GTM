"""Durable custody of postings that were paid for but not finished.

THE FAILURE THIS EXISTS FOR. On 2026-09-06 a run bought 5,444 provider rows, kept
226 net-new postings, qualified them, and then hit Apollo's
``BILLING.LIMIT.CREDITS_EXHAUSTED``. The circuit opened and the run stopped -- and
every safeguard behaved exactly as designed:

* nothing was committed to suppression (``terminal_posting_ids()`` is empty when no
  lead reaches FINAL_PASS/REJECT), so the postings were not blacklisted;
* the watermark was not committed, so the window stayed in flight.

And the work was still lost. "Not suppressed" only means a posting MAY be processed
again; it does not mean anything will ever hand it back. The window's per-source
offsets had already advanced (100 -> 2822) and are replayed FORWARD, so the next run
resumes past the rows it already bought. The postings existed only in memory and in
``run_artifacts/<run_id>/enrichment/postings.json`` -- which no later run reads and
which retention deletes after four runs.

So this store keeps custody of acquired-but-unfinished work:

* it is written at the acquisition checkpoint, BEFORE enrichment runs and before the
  process can exit;
* it lives beside ``run_artifacts``, not inside it, so ``StateStore.prune`` cannot
  remove it -- prune only ever deletes under ``run_artifacts``;
* a later run loads it and re-enters the postings into the SAME enrichment and
  delivery path, so every existing gate, idempotency rule and budget still applies;
* an entry is released only when its posting reaches a TERMINAL disposition, on the
  identical id set that is committed to suppression. Terminal means finished, and
  finished is the only thing that ends custody.

It never re-acquires and never re-bills: these are rows already purchased.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from retrieval_measurement.accounting import posting_identity

logger = logging.getLogger(__name__)

#: Store name. Registered in ``StateStore.STORES`` so the directory exists and,
#: crucially, sits OUTSIDE ``run_artifacts`` where prune cannot reach it.
STORE = "pending_work"
SCHEMA = "pending-work/1"


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


def record(store: str | Path, run_id: str, opportunities: Sequence[Any]) -> Dict[str, Any]:
    """Take custody of one slice's deduped opportunities.

    Idempotent per posting identity, so a re-run of the same slice does not double
    the file, and additive across slices of one run.
    """
    result = {"recorded": 0, "already_held": 0, "unidentifiable": 0, "path": ""}
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
        _write(path, {
            "schema": SCHEMA,
            "run_id": run_id,
            "recorded_at": _now().isoformat(),
            "jobs": jobs,
        })
        result["path"] = str(path)
    except Exception as exc:  # noqa: BLE001 - custody is never allowed to fail a run
        logger.warning("Could not record pending work for %s: %s", run_id, exc)
    return result


def load(
    store: str | Path,
    *,
    exclude_run_id: str = "",
    limit: Optional[int] = None,
    max_age_days: Optional[int] = None,
    exclude_keys: Iterable[str] = (),
) -> Tuple[List[Any], Dict[str, Any]]:
    """Postings still owed work, oldest run first.

    ``exclude_keys`` is the current run's own opportunity keys: a posting that this
    run re-acquired anyway must not be handed back a second time. ``limit`` bounds
    how much unfinished work one run adopts -- unbounded resume would let a long
    outage hand a single run more enrichment than its budget can serve.
    """
    info: Dict[str, Any] = {"files": 0, "offered": 0, "adopted": 0,
                            "skipped_current_run": 0, "expired": 0, "runs": []}
    base = Path(store)
    if not base.is_dir():
        return [], info

    skip = {str(k) for k in exclude_keys if k}
    cutoff = None
    if max_age_days:
        cutoff = _now() - timedelta(days=int(max_age_days))

    out: List[Any] = []
    seen_keys: set = set()
    for path in sorted(base.glob("*.json")):
        held = _read(path)
        if held.get("schema") != SCHEMA:
            continue
        run_id = str(held.get("run_id") or path.stem)
        if exclude_run_id and run_id == exclude_run_id:
            info["skipped_current_run"] += 1
            continue
        info["files"] += 1
        if cutoff is not None:
            try:
                stamp = datetime.fromisoformat(str(held.get("recorded_at")))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if stamp < cutoff:
                    info["expired"] += 1
                    continue
            except (TypeError, ValueError):
                pass
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


def release(store: str | Path, terminal_ids: Iterable[str]) -> Dict[str, Any]:
    """Drop postings that reached a terminal disposition; delete emptied files.

    Takes the SAME id set the pipeline commits to suppression, so custody ends
    exactly when the posting is genuinely finished -- never on a deferred outcome,
    which is the distinction that makes a provider outage survivable.
    """
    info = {"released": 0, "files_emptied": 0, "still_held": 0}
    base = Path(store)
    if not base.is_dir():
        return info
    done = {str(i) for i in terminal_ids if i}
    if not done:
        for path in base.glob("*.json"):
            info["still_held"] += len(_read(path).get("jobs") or [])
        return info

    for path in sorted(base.glob("*.json")):
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
            held["recorded_at"] = held.get("recorded_at") or _now().isoformat()
            _write(path, held)
    return info


def summary(store: str | Path) -> Dict[str, Any]:
    """What is currently owed, for the run summary and the weekly report."""
    base = Path(store)
    runs: List[Dict[str, Any]] = []
    total = 0
    if base.is_dir():
        for path in sorted(base.glob("*.json")):
            held = _read(path)
            if held.get("schema") != SCHEMA:
                continue
            count = len(held.get("jobs") or [])
            total += count
            runs.append({"run_id": str(held.get("run_id") or path.stem),
                         "postings": count,
                         "recorded_at": str(held.get("recorded_at") or "")})
    return {"pending_postings": total, "pending_runs": len(runs), "runs": runs}
