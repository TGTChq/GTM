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

from . import detail, drive, metrics, render, store
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
                       store_report: bool = True, lead_detail: bool = True, **kwargs) -> Dict[str, Any]:
    window = window_for(now=now, kind=kwargs.get("kind", "weekly"), weeks_back=kwargs.get("weeks_back", 0),
                        week_start=kwargs.get("week_start"), tz_name=kwargs.get("tz_name", PACIFIC_TZ_NAME))
    report = build(conn, now=now, window=window, **kwargs)
    result: Dict[str, Any] = {"report": report, "report_id": report["window"]["report_id"], "window": window}
    if lead_detail and store_report:
        # The file is generated and reconciled with the headline BEFORE the report is
        # stored, so a report can never be published claiming a detail that does not
        # match it -- or that does not exist.
        stored_detail = detail.store(conn, window, detail.build(conn, window))
    else:
        stored_detail = detail.load(conn, window.report_id) if store_report else None
    report["detail"] = detail.summarise(stored_detail, report["headline"]["added_to_instantly"])
    if report["detail"]["generated"] and not report["detail"]["reconciles"]:
        message = (f"the lead-level detail holds {report['detail']['rows']} rows while the report counts "
                   f"{report['detail']['headline_added_to_instantly']} added to Instantly")
        report["integrity_alerts"] = [message] + report["integrity_alerts"]
        report["alerts"] = [message] + report["alerts"]
        report["flags"] = [message] + report["flags"]
        report["status"] = "integrity"
    if store_report:
        result["stored"] = store.save(conn, report)
    if out_dir is not None:
        result["artifacts"] = artifacts(out_dir, report)
    return result


#: Named people who should be able to open the weekly detail, comma separated. The
#: folder's own access already governs it; this only adds explicit readers.
DETAIL_READERS_ENV = "TGTC_REPORT_DETAIL_READERS"


def publish_detail(conn: psycopg.Connection, report: Dict[str, Any], *,
                   env: Optional[Dict[str, str]] = None, channel: str = "") -> Dict[str, Any]:
    """Put this week's lead file where the readers can open it, once.

    Every refusal is a named, honest state rather than a link: no credential, no folder,
    no stored file, a week that is not closed, or a file that turned out to be readable
    by anyone with the link. A week that has already been published is left exactly as it is -- the
    retry that runs twenty minutes later must not re-upload, re-share, or move the link.
    """
    import os as _os

    env = _os.environ if env is None else env
    window = report["window"]
    report_id = window["report_id"]
    if window["kind"] != "weekly":
        return {"published": False, "reason": "only a closed week is published"}
    stored = detail.load(conn, report_id)
    if stored is None:
        return {"published": False, "reason": "no lead detail is stored for this week"}
    if stored.get("published_url"):
        return {"published": False, "reason": "already_published", "url": stored["published_url"],
                "rows": stored["row_count"]}
    if (env.get("TGTC_REPORT_DETAIL_DESTINATION") or "").strip().lower() == "slack":
        from . import slack_file

        token = (env.get("SLACK_BOT_TOKEN") or "").strip()
        if not token or not channel:
            return {"published": False, "reason": "Slack file upload is not configured",
                    "missing": [name for name, value in (("SLACK_BOT_TOKEN", token),
                                                         ("--slack-channel", channel)) if not value]}
        if not report.get("detail", {}).get("reconciles"):
            return {"published": False, "reason": "lead detail does not reconcile with the headline"}
        try:
            out = slack_file.publish(token, channel, drive.file_name(report_id), stored["csv"],
                                     report_id=report_id, rows=stored["row_count"])
        except slack_file.SlackFileError as exc:
            return {"published": False, "reason": str(exc)}
        except Exception as exc:  # a transport timeout must not suppress the headline
            import requests

            if not isinstance(exc, requests.RequestException):
                raise
            return {"published": False, "reason": f"Slack file transport failed: {type(exc).__name__}"}
        detail.record_publication(conn, report_id, url=out["url"],
                                  viewers=[f"slack-channel:{channel}:{out['channel_id']}"])
        return {"published": True, "rows": stored["row_count"], "sha256": stored["sha256"], **out}
    folder_id = (env.get("TGTC_REPORT_DRIVE_FOLDER_ID") or "").strip()
    try:
        credentials = drive.credentials_from_env(env)
    except drive.DriveError as exc:
        return {"published": False, "reason": f"credential unusable: {exc}"}
    if credentials is None or not folder_id:
        missing = [name for name, value in (("TGTC_DRIVE_SERVICE_ACCOUNT_JSON", credentials),
                                            ("TGTC_REPORT_DRIVE_FOLDER_ID", folder_id)) if not value]
        return {"published": False, "reason": "Drive is not configured on this service",
                "missing": missing}
    readers = [a.strip() for a in str(env.get(DETAIL_READERS_ENV, "") or "").split(",") if a.strip()]
    try:
        out = drive.publish(drive.DriveFolder(drive.authorised_session(credentials), folder_id),
                            report_id=report_id, content=stored["csv"], readers=readers)
    except drive.DriveError as exc:
        return {"published": False, "reason": str(exc)}
    # What the file is actually readable by, never a claim about it. The folder's own
    # access is the access control; named readers are recorded only when granted.
    viewers = [f"drive-folder:{folder_id}"] + [f"reader:{a}" for a in out.get("named_readers_granted", [])]
    detail.record_publication(conn, report_id, url=out["url"], viewers=viewers)
    return {"published": True, "rows": stored["row_count"], "sha256": stored["sha256"], **out}
