"""Generate, store, write and (only when asked) deliver a weekly report.

The command-line layer stays thin: everything that decides WHAT a report contains,
and everything that decides whether it may be sent, lives here where it is tested.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import psycopg

from . import metrics, render, store
from .window import PACIFIC_TZ_NAME, ReportWindow, explicit_window, partial_window, weekly_window


def build(conn: psycopg.Connection, *, now: datetime, kind: str = "weekly", weeks_back: int = 0,
          week_start: Optional[date] = None, tz_name: str = PACIFIC_TZ_NAME,
          compare_previous: bool = True, unit_prices: Optional[Dict[str, float]] = None,
          target_per_run: int = 1000) -> Dict[str, Any]:
    """Measure one window and return the report. Reads only; writes nothing."""
    if week_start is not None:
        window: ReportWindow = explicit_window(week_start, tz_name=tz_name, now=now)
    elif kind == "partial":
        window = partial_window(now, tz_name=tz_name)
    else:
        window = weekly_window(now, weeks_back=weeks_back, tz_name=tz_name)
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


def slack_sender(webhook_url: str) -> Callable[[str, str], Dict[str, Any]]:
    """A sender for ``store.deliver``. The URL is a secret: it is never returned,
    logged or written into a receipt -- only the target NAME the caller passed."""

    def send(target: str, body: str) -> Dict[str, Any]:
        import requests  # local import: a report that is not sent needs no HTTP stack

        response = requests.post(webhook_url, json={"text": body}, timeout=30)
        if response.status_code >= 300:
            raise RuntimeError(f"{target} refused the report: HTTP {response.status_code} {response.text[:200]}")
        return {"target": target, "http_status": response.status_code,
                "provider_response": response.text[:200],
                "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    return send


def generate_and_store(conn: psycopg.Connection, *, now: datetime, out_dir: Optional[Path] = None,
                       store_report: bool = True, **kwargs) -> Dict[str, Any]:
    report = build(conn, now=now, **kwargs)
    result: Dict[str, Any] = {"report": report, "report_id": report["window"]["report_id"]}
    if store_report:
        result["stored"] = store.save(conn, report)
    if out_dir is not None:
        result["artifacts"] = artifacts(out_dir, report)
    return result
