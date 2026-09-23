"""The reporting week: Friday 00:00 to the following Friday 00:00, America/Los_Angeles,
half-open ``[start, end)``.

That definition is not invented here. It is the one recorded in
``WEEKLY_REPORTING.md`` ("Friday 00:00 to the following Friday 00:00, America/Los
Angeles, end exclusive") and re-stated independently in the 2026-09-09 recovery note
that produced the workbooks Brett read. ``tests_core/test_weekly_report_window.py``
asserts this module and the legacy ``weekly_report.timewindow`` still agree instant
for instant, so the two can never drift apart silently.

Two properties matter and are enforced by construction:

* **Local wall clock, not a fixed UTC offset.** Friday 00:00 Pacific is 07:00 UTC in
  PDT and 08:00 UTC in PST. A window pinned to one offset shifts by an hour twice a
  year and mis-attributes every run near a boundary.
* **Half-open.** A run finishing exactly at the boundary belongs to the next week, so
  two consecutive reports can never count it twice.

The IANA database via :mod:`zoneinfo` is authoritative. Where the runtime ships no tz
database, a codified US federal rule keeps the report correct rather than crashing --
and the window always records WHICH resolver produced it, so a reader can tell.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Optional, Tuple

PACIFIC_TZ_NAME = "America/Los_Angeles"

TZ_SOURCE_ZONEINFO = "zoneinfo:tzdata"
TZ_SOURCE_FALLBACK = "builtin:us_federal_dst_rule"

#: Monday=0 ... Sunday=6, matching ``datetime.weekday()``.
FRIDAY = 4
SUNDAY_INDEX = 6

_STD_OFFSET = timedelta(hours=-8)
_DST_DELTA = timedelta(hours=1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


class _UsPacificFallback(tzinfo):
    """The US federal DST rule in force since 2007: 2nd Sunday of March 02:00 local to
    the 1st Sunday of November 02:00 local, standard offset -08:00. Used only where no
    tz database exists; the report says so when it is."""

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        return _STD_OFFSET + (_DST_DELTA if self._is_dst(dt) else timedelta(0))

    def dst(self, dt: Optional[datetime]) -> timedelta:
        return _DST_DELTA if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt: Optional[datetime]) -> str:
        return "PDT" if self._is_dst(dt) else "PST"

    @staticmethod
    def _is_dst(dt: Optional[datetime]) -> bool:
        if dt is None:
            return False
        naive = dt.replace(tzinfo=None)
        start = datetime.combine(_nth_weekday(naive.year, 3, SUNDAY_INDEX, 2), time(2, 0))
        end = datetime.combine(_nth_weekday(naive.year, 11, SUNDAY_INDEX, 1), time(2, 0))
        return start <= naive < end


def resolve_timezone(name: str = PACIFIC_TZ_NAME) -> Tuple[tzinfo, str]:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name), TZ_SOURCE_ZONEINFO
    except Exception:  # noqa: BLE001 - a missing tz database must not stop a report
        if name != PACIFIC_TZ_NAME:
            raise
        return _UsPacificFallback(), TZ_SOURCE_FALLBACK


def iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ReportWindow:
    """A half-open ``[start, end)`` interval defined in local time.

    ``report_id`` is derived from the local start date, so re-running the report for
    the same week always addresses the same row: that is what makes a retry safe.
    """

    start_utc: datetime
    end_utc: datetime
    start_local: datetime
    end_local: datetime
    timezone_name: str
    timezone_source: str
    kind: str          # 'weekly' (a closed week) or 'partial' (week to date)
    data_cutoff: datetime

    @property
    def label(self) -> str:
        last_day = (self.end_local - timedelta(days=1)).date()
        return f"{self.start_local.strftime('%b %d')} - {last_day.strftime('%b %d, %Y')}"

    @property
    def report_id(self) -> str:
        return f"{self.kind}-{self.start_local.date().isoformat()}"

    @property
    def iso_week(self) -> str:
        year, week, _ = self.start_local.isocalendar()
        return f"{year}-W{week:02d}"

    @property
    def duration_hours(self) -> float:
        """Real elapsed hours. 167 or 169 across a DST transition, by design."""
        return (self.end_utc - self.start_utc).total_seconds() / 3600.0

    def contains(self, moment: datetime) -> bool:
        if moment.tzinfo is None:
            raise ValueError("window membership requires a timezone-aware instant")
        return self.start_utc <= moment.astimezone(timezone.utc) < self.end_utc

    def local_days(self):
        """Every local calendar date in the window, in order."""
        out, day = [], self.start_local.date()
        last = (self.end_local - timedelta(seconds=1)).date()
        while day <= last:
            out.append(day)
            day += timedelta(days=1)
        return out

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "kind": self.kind,
            "window_start_utc": iso_z(self.start_utc),
            "window_end_utc": iso_z(self.end_utc),
            "window_start_local": self.start_local.isoformat(),
            "window_end_local": self.end_local.isoformat(),
            "window_label": self.label,
            "iso_week": self.iso_week,
            "interval": "half_open [start, end)",
            "duration_hours": round(self.duration_hours, 2),
            "timezone": self.timezone_name,
            "timezone_source": self.timezone_source,
            "data_cutoff_utc": iso_z(self.data_cutoff),
        }


def _boundary_at_or_before(now_local: datetime, weekday: int, hour: int) -> datetime:
    """The latest ``weekday`` at ``hour``:00 local that is at or before ``now_local``."""
    candidate = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    candidate -= timedelta(days=(candidate.weekday() - weekday) % 7)
    if candidate > now_local:
        candidate -= timedelta(days=7)
    return candidate


def _localize(naive: datetime, tz: tzinfo) -> datetime:
    return naive.replace(tzinfo=tz)


def weekly_window(now: datetime, *, weeks_back: int = 0, boundary_weekday: int = FRIDAY,
                  boundary_hour: int = 0, tz_name: str = PACIFIC_TZ_NAME,
                  tz: Optional[tzinfo] = None, tz_source: Optional[str] = None) -> ReportWindow:
    """The most recently CLOSED reporting week as of ``now`` (``weeks_back=1`` the one
    before it, and so on).

    Running at 05:00 Pacific on Friday reports Friday-to-Friday for the week that just
    closed: it does not wait for Friday to finish and never reaches into the day the
    report is written.
    """
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    if weeks_back < 0:
        raise ValueError("weeks_back must be >= 0")
    if tz is None:
        tz, resolved = resolve_timezone(tz_name)
        tz_source = tz_source or resolved
    tz_source = tz_source or "caller_supplied"
    now_local = now.astimezone(tz)
    end_local = _boundary_at_or_before(now_local, boundary_weekday, boundary_hour) - timedelta(weeks=weeks_back)
    start_local = _localize(end_local.replace(tzinfo=None) - timedelta(days=7), tz)
    end_local = _localize(end_local.replace(tzinfo=None), tz)
    return ReportWindow(
        start_utc=start_local.astimezone(timezone.utc), end_utc=end_local.astimezone(timezone.utc),
        start_local=start_local, end_local=end_local, timezone_name=tz_name,
        timezone_source=tz_source, kind="weekly", data_cutoff=now.astimezone(timezone.utc))


def partial_window(now: datetime, *, boundary_weekday: int = FRIDAY, boundary_hour: int = 0,
                   tz_name: str = PACIFIC_TZ_NAME, tz: Optional[tzinfo] = None,
                   tz_source: Optional[str] = None) -> ReportWindow:
    """The week IN PROGRESS: its Friday boundary to ``now``. Reported so the current
    week can be rehearsed and watched, and labelled ``partial`` so it can never be
    mistaken for a closed week."""
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    if tz is None:
        tz, resolved = resolve_timezone(tz_name)
        tz_source = tz_source or resolved
    tz_source = tz_source or "caller_supplied"
    now_local = now.astimezone(tz)
    start_local = _localize(_boundary_at_or_before(now_local, boundary_weekday, boundary_hour).replace(tzinfo=None), tz)
    return ReportWindow(
        start_utc=start_local.astimezone(timezone.utc), end_utc=now.astimezone(timezone.utc),
        start_local=start_local, end_local=now_local, timezone_name=tz_name,
        timezone_source=tz_source, kind="partial", data_cutoff=now.astimezone(timezone.utc))


def explicit_window(start_local_date: date, *, weeks: int = 1, boundary_hour: int = 0,
                    tz_name: str = PACIFIC_TZ_NAME, now: Optional[datetime] = None) -> ReportWindow:
    """A named week, for rehearsal and for re-issuing an earlier report unchanged."""
    if weeks < 1:
        raise ValueError("weeks must be >= 1")
    tz, source = resolve_timezone(tz_name)
    start_local = _localize(datetime.combine(start_local_date, time(boundary_hour, 0)), tz)
    end_local = _localize(datetime.combine(start_local_date + timedelta(days=7 * weeks), time(boundary_hour, 0)), tz)
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return ReportWindow(
        start_utc=start_local.astimezone(timezone.utc), end_utc=end_local.astimezone(timezone.utc),
        start_local=start_local, end_local=end_local, timezone_name=tz_name,
        timezone_source=source, kind="weekly", data_cutoff=cutoff)


def is_due(now: datetime, *, weekday: int = FRIDAY, hour: int, minute: int = 0,
           tz_name: str = PACIFIC_TZ_NAME) -> bool:
    """True when ``now`` is at or after this week's delivery moment and still inside the
    same local day. The schedule is evaluated in the REPORT's timezone, never in UTC,
    so it does not drift by an hour across a DST change."""
    tz, _ = resolve_timezone(tz_name)
    local = now.astimezone(tz)
    if local.weekday() != weekday:
        return False
    return (local.hour, local.minute) >= (hour, minute)
