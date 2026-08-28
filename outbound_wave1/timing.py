"""Frozen Challenger sequence timing: Day 1 -> Day 4 -> Day 8 -> Day 13.

Business days only. When a calculated send date lands on a Saturday or Sunday it
moves to the following Monday, and the NEXT gap is measured from that shifted
date -- the sequence is never compressed to catch back up to the nominal day.

This module produces timing METADATA only. Nothing here schedules anything in
Instantly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence, Tuple

#: Nominal day numbers of the four steps.
SEQUENCE_DAY_LABELS: Tuple[int, ...] = (1, 4, 8, 13)

#: Calendar-day gaps BETWEEN consecutive steps (1->4, 4->8, 8->13).
SEQUENCE_OFFSET_DAYS: Tuple[int, ...] = (3, 4, 5)

_SATURDAY = 5


def _shift_off_weekend(value: date) -> Tuple[date, bool]:
    """Move a weekend date to the following Monday."""
    weekday = value.weekday()
    if weekday < _SATURDAY:
        return value, False
    return value + timedelta(days=7 - weekday), True


@dataclass(frozen=True)
class SequenceStep:
    step: int              # 1..4
    day_label: int         # nominal Day 1 / 4 / 8 / 13
    send_date: str         # ISO date
    weekend_shifted: bool
    #: Actual calendar days from the FIRST send, after any weekend shifts.
    offset_from_start_days: int
    same_thread: bool


def sequence_schedule(start: Optional[date | datetime | str] = None) -> List[SequenceStep]:
    """Return the four Challenger send dates for a sequence starting ``start``.

    ``start`` itself is moved off a weekend before Day 1 is emitted, so the
    sequence never opens on a Saturday.
    """
    if start is None:
        anchor = date.today()
    elif isinstance(start, datetime):
        anchor = start.date()
    elif isinstance(start, date):
        anchor = start
    else:
        anchor = date.fromisoformat(str(start)[:10])

    current, shifted = _shift_off_weekend(anchor)
    first = current
    steps = [
        SequenceStep(
            step=1,
            day_label=SEQUENCE_DAY_LABELS[0],
            send_date=current.isoformat(),
            weekend_shifted=shifted,
            offset_from_start_days=0,
            # E1 opens the thread.
            same_thread=False,
        )
    ]
    for index, gap in enumerate(SEQUENCE_OFFSET_DAYS, start=1):
        # The gap is measured from the date the previous step actually sends,
        # which is what stops a weekend shift from compressing the sequence.
        current, shifted = _shift_off_weekend(current + timedelta(days=gap))
        steps.append(
            SequenceStep(
                step=index + 1,
                day_label=SEQUENCE_DAY_LABELS[index],
                send_date=current.isoformat(),
                weekend_shifted=shifted,
                offset_from_start_days=(current - first).days,
                same_thread=True,
            )
        )
    return steps


def schedule_as_dicts(steps: Sequence[SequenceStep]) -> List[dict]:
    return [
        {
            "step": step.step,
            "day_label": step.day_label,
            "send_date": step.send_date,
            "weekend_shifted": step.weekend_shifted,
            "offset_from_start_days": step.offset_from_start_days,
            "same_thread": step.same_thread,
        }
        for step in steps
    ]
