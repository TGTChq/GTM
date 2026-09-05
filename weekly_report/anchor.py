"""Durable run-anchored reporting boundary.

A fixed wall-clock boundary cannot express "Friday's own acquisition is in
Friday's report". The report runs at ``S + D`` where ``D`` is the run duration and
varies (measured 2026-09-04: 3.3 h expected, 6.7 h at 2x). A fixed boundary ``B``
would have to satisfy ``S + D_max < B <= S + D_min`` -- a contradiction. Every
fixed-clock variant either drops the Friday run or opens a coverage gap.

So the boundary is anchored to the REPORT instead of the clock:

    window = [ previously persisted report_end , this report's generation time )

Half-open, and the next window starts at exactly this window's end, so the two
meet at a point: no gap, no overlap, and a run that finishes exactly on the
boundary belongs to the later window only.

WHEN THE MARKER ADVANCES -- after the report ARTIFACT is durably on disk, NOT
after Slack accepts. The two failure modes are not symmetric:

  * advance on artifact: if Slack then fails, the window is closed and the
    artifact exists. A retry re-reads that same artifact and re-attempts delivery,
    guarded by the existing receipt, so there is no duplicate and no skipped week.
  * advance on Slack receipt: a persistent Slack outage would hold the window open
    forever. Each retry would report an ever-widening span, and when delivery
    finally succeeded Brett would get one merged report while the intermediate
    weeks never existed as distinct windows.

The first is recoverable, the second loses the week structure. Delivery
idempotency is already the receipt's job (``weekly_report/slack.py``); the anchor's
job is only to make windows contiguous.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from weekly_report.timewindow import iso_z, parse_instant

ANCHOR_SCHEMA = "tgtc-weekly-report-anchor/1"
ANCHOR_FILENAME = "weekly_report_anchor.json"


def anchor_path_for(out_dir: Path) -> Path:
    """The marker lives beside the reports it bounds, so a restored artifact root
    restores the boundary with it."""
    return Path(out_dir) / ANCHOR_FILENAME


def read_anchor(path: Path) -> Optional[datetime]:
    """The previous ``report_end``, or ``None`` when no report has been anchored.

    Unreadable, malformed or wrong-schema content returns ``None`` rather than
    raising: a corrupt marker must degrade to "no anchor yet" (the caller then
    falls back to the fixed weekly boundary) instead of stopping the report.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != ANCHOR_SCHEMA:
        return None
    return parse_instant(data.get("report_end"))


def write_anchor(path: Path, report_end: datetime, *, window: Optional[Dict[str, Any]] = None,
                 report_path: Optional[Path] = None) -> Path:
    """Advance the marker atomically: it is either the old value or the new one.

    A half-written marker would be read as "no anchor" on the next run, which would
    silently re-report a span that was already delivered.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": ANCHOR_SCHEMA,
        "report_end": iso_z(report_end),
        "updated_at": iso_z(datetime.now(report_end.tzinfo)),
        "window": window or {},
        "report": str(report_path) if report_path else "",
    }
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".anchor-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def seed_from_last_report(out_dir: Path) -> Optional[datetime]:
    """Where the LAST DELIVERED report's window ended, read from the reports
    themselves. Used only when no anchor file exists yet.

    Anchoring was introduced after reports had already been going out on a fixed
    Friday-to-Friday boundary. Without this, the first anchored report seeds from
    ``weekly_window``, which returns the most recently CLOSED week -- a span the
    previous report had already covered and Brett had already read. At the real
    firing instants that produces a 13.9-day first window labelled as a week, and
    re-reports the earlier one inside it.

    The anchor's own semantics answer this: the next window starts where the last
    report ended. That instant is recorded in every report document as
    ``reporting_window_end``, so it is recoverable from the artifact even though
    the anchor did not exist to record it.

    Returns the LATEST such instant, or ``None`` when no report is on disk (a
    genuinely first-ever run, where the weekly seed is correct).
    """
    directory = Path(out_dir)
    if not directory.is_dir():
        return None
    latest: Optional[datetime] = None
    for path in sorted(directory.glob("weekly_report_*.json")):
        if path.name == ANCHOR_FILENAME:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A half-written or hand-edited report must not stop the seed; the
            # remaining documents still answer the question.
            continue
        moment = parse_instant((document or {}).get("reporting_window_end"))
        if moment is not None and (latest is None or moment > latest):
            latest = moment
    return latest
