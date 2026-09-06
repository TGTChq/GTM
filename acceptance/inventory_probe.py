"""Measure DAILY ADDRESSABLE INVENTORY across every Fantastic source. Zero credits.

WHY THIS EXISTS. The capacity assessment bounded the pipeline at "<=881 postings/day"
from two count probes taken on ONE day, with the union of the two unresolved and two
enabled sources not counted in the same window at all. A ceiling that decides whether
a business target is reachable cannot rest on that. `/v1/active-jb-count` returns a
COUNT and never rows -- it costs **0 Jobs credits** and one request -- so the honest
version of the measurement is affordable and there was no reason to publish the
cheap one.

WHAT IT MEASURES, per matched 24h `date_created` window, using the PRODUCTION query
builder (`build_jb_params`) and the PRODUCTION title expression
(`build_title_query_plan`) so the numbers describe what production would actually
address:

  * each enabled source separately -- linkedin, ats, wellfound, ycombinator;
  * the UNION, by omitting `source` while keeping every other production filter.
    The per-source counts cannot be added: the LinkedIn query sends
    `exclude_ats_duplicate`, so a posting present on both is counted once there and
    again under ats. Only a single query without the source predicate answers it;
  * the same union WITHOUT the title expression, which is the size of the inventory
    the role catalog excludes -- the one lever of the right order of magnitude.

Read-only and side-effect free: no rows are fetched, no watermark, cursor, quota
snapshot or suppression entry is read or written, and nothing is persisted except the
JSON this prints.

    python acceptance/inventory_probe.py --days 5 [--out probe.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, ".")

import config  # noqa: E402
import fantastic_jobs_adapter as fja  # noqa: E402
from http_utils import request_with_retry  # noqa: E402

#: The sources the plan can enable. Probed regardless of their runtime flag, because
#: "what would this source contribute" is exactly the question a disabled source
#: cannot answer for itself -- and the count costs nothing either way.
SOURCES = ("linkedin", "ats", "wellfound", "ycombinator")


#: The ATS source is a DIFFERENT DATASET on a different endpoint, not a `source`
#: value -- which is why the first version of this probe read `ats: 0`. The two are
#: complementary and non-overlapping by construction: the active-jb queries send
#: `exclude_ats_duplicate=true`, so a posting carried by both is returned by
#: active-ats only. That is what makes them addable.
_ATS_COUNT_ENDPOINT = "/v1/active-ats-count"


def _count(params: Dict[str, Any], endpoint: str = "") -> Optional[int]:
    """One count request. Returns None on any non-200. Never returns rows."""
    base = str(getattr(config, "FANTASTIC_JOBS_BASE_URL", "") or
               "https://api.fantastic.jobs").rstrip("/")
    url = f"{base}{endpoint or fja._COUNT_ENDPOINT}"
    # Same auth the adapter uses -- a bearer token, not an api-key header.
    headers = {"Authorization": f"Bearer {config.FANTASTIC_JOBS_API_KEY}"}
    resp = request_with_retry("GET", url, headers=headers, params=params, timeout=60)
    if resp is None or getattr(resp, "status_code", 0) != 200:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, (int, float)):
        return int(body)
    if isinstance(body, dict):
        for key in ("count", "total", "jobs", "result"):
            value = body.get(key)
            if isinstance(value, (int, float)):
                return int(value)
    if isinstance(body, list) and body and isinstance(body[0], dict):
        for key in ("count", "total"):
            value = body[0].get(key)
            if isinstance(value, (int, float)):
                return int(value)
    return None


def _window_params(source: str, lo: str, hi: str, title_expr: str) -> Dict[str, Any]:
    """Production filters for one source over one explicit `date_created` window.

    `time_frame` is dropped: the count endpoint offers a different frame set (1m
    default, no 7d) and an explicit `date_created_gte/lt` is what makes the windows
    comparable. Everything else is the production builder's own output.
    """
    params = fja.build_jb_params(source, title_advanced_expr=title_expr)
    params.pop("time_frame", None)
    params["date_created_gte"] = lo
    params["date_created_lt"] = hi
    return params


def _union_params(lo: str, hi: str, title_expr: str) -> Dict[str, Any]:
    """Every production filter EXCEPT the source predicate.

    Built from the linkedin shape because that is the firmographics-carrying one;
    dropping `source` widens it to the whole feed under the same filters, which is
    the only way to count a posting that appears under two sources exactly once.
    """
    params = _window_params("linkedin", lo, hi, title_expr)
    params.pop("source", None)
    return params


def _ats_params(lo: str, hi: str, title_expr: str) -> Dict[str, Any]:
    """Production `/v1/active-ats` filters over one explicit window."""
    params = fja.build_ats_params(title_expr)
    params.pop("time_frame", None)
    # The count endpoint REJECTS this with a 400: it shapes the row payload, and
    # this endpoint returns no rows. Verified by bisecting the parameters -- the
    # same production filters minus this one return 200.
    params.pop("include_basic_organization_details", None)
    params["date_created_gte"] = lo
    params["date_created_lt"] = hi
    return params


def probe(days: int = 5, lag_hours: int = 2) -> Dict[str, Any]:
    plan = fja.build_title_query_plan()
    title_expr = str(plan.get("expression", "") or "")
    now = datetime.now(timezone.utc) - timedelta(hours=lag_hours)
    out: Dict[str, Any] = {
        "measured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title_expression_chars": len(title_expr),
        "endpoint": fja._COUNT_ENDPOINT,
        "jobs_credits": 0,
        "windows": [],
    }
    requests = 0
    for day in range(days):
        hi_dt = now - timedelta(days=day)
        lo_dt = hi_dt - timedelta(days=1)
        lo, hi = (lo_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  hi_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        row: Dict[str, Any] = {"from": lo, "to": hi, "sources": {}}
        for source in ("linkedin", "wellfound", "ycombinator"):
            row["sources"][source] = _count(_window_params(source, lo, hi, title_expr))
            requests += 1
        row["sources"]["ats"] = _count(_ats_params(lo, hi, title_expr),
                                       _ATS_COUNT_ENDPOINT)
        requests += 1
        # The active-jb union under the LinkedIn shape, which KEEPS the
        # null-excluding firmographic predicates and therefore addresses exactly
        # what LinkedIn addresses. Wellfound and Y Combinator carry no provider
        # firmographics, so they are dropped from this and counted separately --
        # they are additions to it, not double counts of it.
        row["union_jb_titled"] = _count(_union_params(lo, hi, title_expr))
        requests += 1
        row["union_jb_untitled"] = _count(_union_params(lo, hi, ""))
        requests += 1
        row["ats_untitled"] = _count(_ats_params(lo, hi, ""), _ATS_COUNT_ENDPOINT)
        requests += 1
        parts = [row["union_jb_titled"], row["sources"].get("ats"),
                 row["sources"].get("wellfound"), row["sources"].get("ycombinator")]
        if all(isinstance(v, int) for v in parts):
            row["addressable_total"] = sum(parts)
        untitled = [row.get("union_jb_untitled"), row.get("ats_untitled")]
        if all(isinstance(v, int) for v in untitled):
            row["addressable_untitled_total"] = sum(untitled)
        out["windows"].append(row)
    out["requests"] = requests

    titled = [w["addressable_total"] for w in out["windows"]
              if isinstance(w.get("addressable_total"), int)]
    untitled = [w["addressable_untitled_total"] for w in out["windows"]
                if isinstance(w.get("addressable_untitled_total"), int)]
    if titled:
        out["addressable_per_day"] = {
            "n": len(titled), "min": min(titled), "max": max(titled),
            "mean": round(sum(titled) / len(titled), 1)}
    for key in ("linkedin", "ats", "wellfound", "ycombinator"):
        vals = [w["sources"][key] for w in out["windows"]
                if isinstance(w.get("sources", {}).get(key), int)]
        if vals:
            out.setdefault("per_source_per_day", {})[key] = {
                "n": len(vals), "min": min(vals), "max": max(vals),
                "mean": round(sum(vals) / len(vals), 1)}
    if untitled and titled:
        t_mean = sum(titled) / len(titled)
        out["title_filter_excludes"] = {
            "untitled_mean": round(sum(untitled) / len(untitled), 1),
            "titled_mean": round(t_mean, 1),
            "multiple": round((sum(untitled) / len(untitled)) / max(1.0, t_mean), 1)}
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--lag-hours", type=int, default=2,
                    help="end the newest window this far back, so the feed has settled")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    result = probe(days=max(1, a.days), lag_hours=max(0, a.lag_hours))
    text = json.dumps(result, indent=2)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
