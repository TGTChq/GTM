"""Generate, store, write and (only when asked) deliver a weekly report.

The command-line layer stays thin: everything that decides WHAT a report contains,
and everything that decides whether it may be sent, lives here where it is tested.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

import psycopg

from . import metrics, render, store
from .window import PACIFIC_TZ_NAME, ReportWindow, explicit_window, partial_window, weekly_window


def window_for(*, now: datetime, kind: str = "weekly", weeks_back: int = 0,
               week_start: Optional[date] = None, tz_name: str = PACIFIC_TZ_NAME) -> ReportWindow:
    """The one place a window is chosen, so the report, its readiness check and its
    lead export can never be measuring three slightly different intervals."""
    if week_start is not None:
        return explicit_window(week_start, tz_name=tz_name, now=now)
    if kind == "partial":
        return partial_window(now, tz_name=tz_name)
    return weekly_window(now, weeks_back=weeks_back, tz_name=tz_name)


def build(conn: psycopg.Connection, *, now: datetime, kind: str = "weekly", weeks_back: int = 0,
          week_start: Optional[date] = None, tz_name: str = PACIFIC_TZ_NAME,
          compare_previous: bool = True, unit_prices: Optional[Dict[str, float]] = None,
          target_per_run: int = 1000, window: Optional[ReportWindow] = None) -> Dict[str, Any]:
    """Measure one window and return the report. Reads only; writes nothing."""
    if window is None:
        window = window_for(now=now, kind=kind, weeks_back=weeks_back, week_start=week_start, tz_name=tz_name)
    previous = None
    if compare_previous and window.kind == "weekly":
        earlier = weekly_window(window.end_utc, weeks_back=1, tz_name=tz_name)
        previous = metrics.build_report(conn, earlier, unit_prices=unit_prices, target_per_run=target_per_run)
    return metrics.build_report(conn, window, previous=previous, unit_prices=unit_prices,
                                target_per_run=target_per_run)


def artifacts(out_dir: Path | str, report: Dict[str, Any]) -> Dict[str, str]:
    """Write the JSON and the readable text beside each other.

    The JSON carries the checksum of the text, so a report that was forwarded can be
    matched back to the numbers it was generated from.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    report_id = report["window"]["report_id"]
    text = render.render_text(report)
    payload = dict(report)
    payload["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    json_path = directory / f"{report_id}.json"
    text_path = directory / f"{report_id}.txt"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    return {"json": str(json_path), "text": str(text_path), "text_sha256": payload["text_sha256"]}


def generate_and_store(conn: psycopg.Connection, *, now: datetime, out_dir: Optional[Path] = None,
                       store_report: bool = True, **kwargs) -> Dict[str, Any]:
    window = window_for(now=now, kind=kwargs.get("kind", "weekly"), weeks_back=kwargs.get("weeks_back", 0),
                        week_start=kwargs.get("week_start"), tz_name=kwargs.get("tz_name", PACIFIC_TZ_NAME))
    report = build(conn, now=now, window=window, **kwargs)
    result: Dict[str, Any] = {"report": report, "report_id": report["window"]["report_id"], "window": window}
    if store_report:
        result["stored"] = store.save(conn, report)
    if out_dir is not None:
        result["artifacts"] = artifacts(out_dir, report)
    return result
