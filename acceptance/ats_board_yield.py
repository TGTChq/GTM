"""Measure what the 145 registered direct ATS boards would contribute. Zero credits.

WHY. The boards are registered, health-tracked and scheduled, and they have
contributed nothing for weeks because the lane is built only when `"ats"` is in
`--lanes`. Their yield has therefore been quoted as "unmeasured" in every capacity
statement -- while the 1,000/day target turns on exactly that number. They are
scraped from each employer's OWN public job board (Greenhouse, Lever, Ashby,
SmartRecruiters, Workable, Workday, Cornerstone), so measuring them costs **no
provider credits at all**: the only reason not to was that nobody had.

WHAT THIS IS NOT. It is not an acquisition run. It calls
`ats_board_registry.fetch_board_jobs`, which is stateless -- it returns
`(jobs, error)` and persists nothing. No lane is constructed, no checkpoint is
written, no scheduler state or board health is updated, no suppression entry is read
or written, nothing enters the pipeline and nothing is delivered. The postings are
counted and thrown away.

WHAT IT REPORTS, using the production identity functions
(`multi_source_acquisition._classify` -> `role_mapping.get_bucket_name_for_job` ->
`airtable_client._company_identity_keys_from_job`) so the figures are comparable with
the Fantastic cohorts measured the same way:

  * postings found, and how many are ROLE-RELEVANT under the production catalog;
  * distinct companies and **company x function opportunities** -- the unit approvals
    are actually capped by;
  * postings newer than N days, which is the per-day contribution rate;
  * per-provider yield, so a dead adapter is visible rather than averaged away.

A board corpus is also the one title-UNFILTERED sample available for free: these
listings arrive without a title query, so the role-relevant fraction here is a real
measurement of how much of an employer's hiring is in catalogue -- the question the
500-row Fantastic sample was requested to answer.

    python acceptance/ats_board_yield.py [--max-boards 145] [--out yield.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, ".")

import config  # noqa: E402


def _posted_at(job: Dict[str, Any]) -> Optional[datetime]:
    for key in ("date_posted", "posted_at", "published_at", "created_at",
                "date_created", "updated_at"):
        raw = job.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def measure(max_boards: int = 200, recent_days: int = 7,
            time_budget_seconds: int = 900) -> Dict[str, Any]:
    from ats_board_registry import AtsBoardRegistry, fetch_board_jobs
    from free_job_sources import default_fetcher

    registry = AtsBoardRegistry()
    if bool(getattr(config, "ATS_REGISTRY_AUTO_SEED_HISTORY", True)):
        try:
            registry.seed_from_history()
        except Exception:  # noqa: BLE001 - seeding is best-effort, as in preflight
            pass
    # `entries` is a MAPPING keyed by board id; iterating it yields KEYS, and a
    # string board silently produces zero postings from every provider.
    boards = list(registry.entries.values())[:max(1, max_boards)]

    started = time.monotonic()
    jobs: List[Dict[str, Any]] = []
    per_provider: Dict[str, Dict[str, int]] = {}
    errors: Counter = Counter()
    scanned = 0
    for board in boards:
        if time.monotonic() - started > time_budget_seconds:
            break
        provider = str(board.get("provider") or "unknown")
        stats = per_provider.setdefault(provider, {"boards": 0, "postings": 0, "failed": 0})
        stats["boards"] += 1
        scanned += 1
        try:
            found, err = fetch_board_jobs(board, default_fetcher)
        except Exception as exc:  # noqa: BLE001 - one bad board must not stop the sweep
            errors[f"{provider}:{type(exc).__name__}"] += 1
            stats["failed"] += 1
            continue
        if err:
            errors[f"{provider}:{err}"] += 1
            stats["failed"] += 1
            continue
        found = [j for j in (found or []) if isinstance(j, dict)]
        for job in found:
            job.setdefault("company_name", board.get("company_name"))
            job.setdefault("employer_name", board.get("company_name"))
        stats["postings"] += len(found)
        jobs.extend(found)

    out: Dict[str, Any] = {
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider_credits": 0,
        "boards_registered": len(registry.entries),
        "boards_scanned": scanned,
        "boards_timed_out": len(boards) - scanned,
        "postings": len(jobs),
        "per_provider": per_provider,
        "errors": dict(errors.most_common(12)),
    }

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, recent_days))
    recent = [j for j in jobs if (_posted_at(j) or datetime.min.replace(
        tzinfo=timezone.utc)) >= cutoff]
    dated = sum(1 for j in jobs if _posted_at(j) is not None)
    out["postings_dated"] = dated
    out["postings_recent"] = len(recent)
    out["recent_days"] = recent_days
    if recent:
        out["postings_per_day_recent"] = round(len(recent) / recent_days, 1)

    # The same identity measurement used on the retained Fantastic payloads, so the
    # two cohorts are comparable rather than merely adjacent.
    import run_maintenance
    out["identities_all"] = run_maintenance.measure_identities(jobs)
    out["identities_recent"] = run_maintenance.measure_identities(recent)
    ident = out["identities_all"]
    if ident.get("postings"):
        # The title-unfiltered relevance rate: what share of an employer's own
        # postings the production role catalog recognises.
        out["role_relevant_fraction"] = round(
            ident["role_relevant_postings"] / ident["postings"], 4)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-boards", type=int, default=200)
    ap.add_argument("--recent-days", type=int, default=7)
    ap.add_argument("--seconds", type=int, default=900)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    result = measure(max_boards=a.max_boards, recent_days=a.recent_days,
                     time_budget_seconds=a.seconds)
    text = json.dumps(result, indent=2, default=str)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
