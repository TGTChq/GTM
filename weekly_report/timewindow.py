"""America/Los_Angeles-correct reporting windows.

Two hard requirements drive this module:

1. **The window is defined in Pacific local time, not UTC.** "The week ending
   Friday morning" means Friday 00:00 *Pacific*, which is 07:00 UTC in PDT and
   08:00 UTC in PST. A window pinned to one UTC offset silently shifts by an hour
   twice a year and mis-attributes every run near a boundary.

2. **No hardcoded offset.** The offset is resolved per instant. The IANA database
   via :mod:`zoneinfo` is authoritative and is used whenever it is installed
   (``tzdata`` is a declared dependency so the container always has it). Where the
   platform ships no tz database at all, a codified fallback of the US federal
   rule (DST from the 2nd Sunday of March 02:00 local to the 1st Sunday of
   November 02:00 local, standard offset -08:00) keeps the report correct instead
   of crashing -- and the report always records *which* resolver produced it, so a
   reader can tell the difference.

Windows are half-open ``[start, end)``: a run finishing exactly at the boundary
belongs to the next week, so consecutive reports never double-count a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Optional, Tuple

PACIFIC_TZ_NAME = "America/Los_Angeles"

#: Resolver identifiers recorded on every report.
TZ_SOURCE_ZONEINFO = "zoneinfo:tzdata"
TZ_SOURCE_FALLBACK = "builtin:us_federal_dst_rule"

_STD_OFFSET = timedelta(hours=-8)
_DST_DELTA = timedelta(hours=1)

#: Monday=0 ... Sunday=6, matching ``datetime.weekday()``.
FRIDAY = 4
SUNDAY_INDEX = 6


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th (1-based) ``weekday`` of ``month``. Monday=0 ... Sunday=6."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def us_dst_bounds(year: int) -> Tuple[datetime, datetime]:
    """Naive local wall-clock instants at which US DST starts and ends.

    Codifies the rule in force since the Energy Policy Act of 2005 (effective
    2007): start on the 2nd Sunday of March at 02:00, end on the 1st Sunday of
    November at 02:00.
    """
    start = datetime.combine(_nth_weekday(year, 3, SUNDAY_INDEX, 2), time(2, 0))
    end = datetime.combine(_nth_weekday(year, 11, SUNDAY_INDEX, 1), time(2, 0))
    return start, end


class UsPacificFallback(tzinfo):
    """US Pacific time from the codified federal rule.

    Used only when no IANA database is available. It is deliberately explicit
    rather than an approximation: the same rule ``zoneinfo`` applies, minus the
    historical table and minus fold handling inside the one ambiguous autumn
    hour. Report boundaries sit at local midnight, which is never ambiguous.
    """

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        return _STD_OFFSET + self.dst(dt)

    def dst(self, dt: Optional[datetime]) -> timedelta:
        if dt is None:
            return timedelta(0)
        naive = dt.replace(tzinfo=None)
        start, end = us_dst_bounds(naive.year)
        return _DST_DELTA if start <= naive < end else timedelta(0)

    def tzname(self, dt: Optional[datetime]) -> str:
        return "PDT" if self.dst(dt) else "PST"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UsPacificFallback(America/Los_Angeles)"


def resolve_timezone(name: str = PACIFIC_TZ_NAME) -> Tuple[tzinfo, str]:
    """Return ``(tzinfo, source_id)``. Prefers IANA data; never raises for Pacific."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name), TZ_SOURCE_ZONEINFO
    except Exception:  # noqa: BLE001 - a missing tz database must not break the report
        if name != PACIFIC_TZ_NAME:
            raise
        return UsPacificFallback(), TZ_SOURCE_FALLBACK


@dataclass(frozen=True)
class ReportingWindow:
    """A half-open ``[start, end)`` interval, defined in local time."""

    start_utc: datetime
    end_utc: datetime
    start_local: datetime
    end_local: datetime
    timezone_name: str
    timezone_source: str
    label: str
    iso_week: str

    def contains(self, moment: datetime) -> bool:
        if moment.tzinfo is None:
            raise ValueError("window membership requires a timezone-aware instant")
        as_utc = moment.astimezone(timezone.utc)
        return self.start_utc <= as_utc < self.end_utc

    @property
    def duration_hours(self) -> float:
        """Real elapsed hours. 167 or 169 across a DST transition, by design."""
        return (self.end_utc - self.start_utc).total_seconds() / 3600.0

    def to_dict(self) -> dict:
        return {
            "reporting_window_start": iso_z(self.start_utc),
            "reporting_window_end": iso_z(self.end_utc),
            "reporting_window_start_local": self.start_local.isoformat(),
            "reporting_window_end_local": self.end_local.isoformat(),
            "reporting_window_label": self.label,
            "reporting_window_iso_week": self.iso_week,
            "reporting_window_interval": "half_open [start, end)",
            "reporting_window_duration_hours": round(self.duration_hours, 2),
            "timezone": self.timezone_name,
            "timezone_source": self.timezone_source,
        }


def iso_z(moment: datetime) -> str:
    """The project's canonical UTC stamp, matching ``retrieval_measurement.identity``."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _localize(naive: datetime, tz: tzinfo) -> datetime:
    return naive.replace(tzinfo=tz)


def _format_label(start_local: datetime, last_day: date) -> str:
    return f"{start_local.strftime('%b %d')} - {last_day.strftime('%b %d, %Y')}"


def weekly_window(
    now: datetime,
    *,
    boundary_weekday: int = FRIDAY,
    boundary_hour: int = 0,
    weeks: int = 1,
    tz: Optional[tzinfo] = None,
    tz_name: str = PACIFIC_TZ_NAME,
    tz_source: Optional[str] = None,
) -> ReportingWindow:
    """The most recently *closed* reporting week as of ``now``.

    ``end`` is the latest ``boundary_weekday`` at ``boundary_hour`` local time that
    is at or before ``now``; ``start`` is ``weeks`` calendar weeks earlier. Both
    boundaries are anchored to local wall-clock time, so each is exactly
    ``boundary_hour``:00 Pacific whatever the offset that day happens to be.

    Running at 05:00 Pacific on Friday therefore reports Friday-to-Friday for the
    week that just closed -- it does not wait for Friday to finish, and it never
    reaches forward into the day the report is written.
    """
    if weeks < 1:
        raise ValueError("weeks must be >= 1")
    if not 0 <= boundary_weekday <= 6:
        raise ValueError("boundary_weekday must be 0 (Mon) .. 6 (Sun)")
    if not 0 <= boundary_hour <= 23:
        raise ValueError("boundary_hour must be 0..23")
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    if tz is None:
        tz, resolved_source = resolve_timezone(tz_name)
        tz_source = tz_source or resolved_source
    else:
        tz_source = tz_source or "caller_supplied"

    local_now = now.astimezone(tz)
    # Walk back to the most recent boundary at or before `now`, in wall-clock terms.
    back = (local_now.date().weekday() - boundary_weekday) % 7
    boundary_day = local_now.date() - timedelta(days=back)
    end_local = _localize(datetime.combine(boundary_day, time(boundary_hour)), tz)
    if end_local > local_now:
        end_local = _localize(
            datetime.combine(boundary_day - timedelta(days=7), time(boundary_hour)), tz
        )
    start_local = _localize(
        datetime.combine(end_local.date() - timedelta(days=7 * weeks), time(boundary_hour)), tz
    )

    last_day = end_local.date() - timedelta(days=1)
    iso_year, iso_week, _ = last_day.isocalendar()
    return ReportingWindow(
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
        start_local=start_local,
        end_local=end_local,
        timezone_name=tz_name,
        timezone_source=tz_source or "unknown",
        label=_format_label(start_local, last_day),
        iso_week=f"{iso_year}-W{iso_week:02d}",
    )


def explicit_window(
    start_local_date: date,
    end_local_date: date,
    *,
    boundary_hour: int = 0,
    tz: Optional[tzinfo] = None,
    tz_name: str = PACIFIC_TZ_NAME,
) -> ReportingWindow:
    """A caller-pinned window, still anchored to local wall-clock boundaries."""
    tz_source = None
    if tz is None:
        tz, tz_source = resolve_timezone(tz_name)
    if end_local_date <= start_local_date:
        raise ValueError("end date must be after start date")
    start_local = _localize(datetime.combine(start_local_date, time(boundary_hour)), tz)
    end_local = _localize(datetime.combine(end_local_date, time(boundary_hour)), tz)
    last_day = end_local_date - timedelta(days=1)
    iso_year, iso_week, _ = last_day.isocalendar()
    return ReportingWindow(
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
        start_local=start_local,
        end_local=end_local,
        timezone_name=tz_name,
        timezone_source=tz_source or "caller_supplied",
        label=_format_label(start_local, last_day),
        iso_week=f"{iso_year}-W{iso_week:02d}",
    )


def local_date_key(moment: datetime, tz: tzinfo) -> str:
    """The local calendar date a UTC instant falls on -- the daily bucket key."""
    return moment.astimezone(tz).strftime("%Y-%m-%d")


def parse_instant(value: object) -> Optional[datetime]:
    """Best-effort ISO-8601 -> aware UTC datetime. Returns ``None``, never raises.

    Accepts the project's ``...Z`` stamps, offset-bearing ISO strings, and naive
    ISO strings (interpreted as UTC, which is what every pipeline artifact writes).
    """
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
