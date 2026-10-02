"""Why a DEPLOY must not start a production run, and the window that stops it.

A Railway cron service runs its start command when a deployment goes live, not
only on the schedule. `cronSchedule` governs the schedule and nothing else. So on
2026-10-02, with `cronSchedule` deliberately set to `None`, two deployments each
started a full acquisition run anyway (23:09:28Z and 23:33:22Z) -- the second one
got past the capacity gate and began buying.

Pausing the cron therefore does NOT make a deploy safe, and there was no other
mechanism that did.

This is that mechanism. A production run may begin only inside the UTC hours the
schedule actually fires in. A deploy at any other hour starts the container,
declines, and exits 0 before acknowledging spend, claiming a budget or taking the
run lock. The default window matches the intent already written into the service's
own start command, which classified a run by hour before `TGTC_RUN_KIND` was set
and began overriding it.

`TGTC_RUN_FORCE=1` runs regardless, for a deliberate manual run.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Mapping, Optional, Tuple

WINDOW_ENV = "TGTC_RUN_WINDOW_UTC"
FORCE_ENV = "TGTC_RUN_FORCE"
#: The hours the `0 3 * * *` schedule can land in, allowing for a slow start.
DEFAULT_WINDOW = "3-5"

REASON_FORCED = "forced"
REASON_INSIDE = "inside_scheduled_window"
REASON_OUTSIDE = "outside_scheduled_window"
REASON_WINDOW_UNREADABLE = "window_unreadable"


def parse_window(raw: Optional[str]) -> Optional[Tuple[int, int]]:
    """``"3-5"`` -> ``(3, 5)`` inclusive. ``None`` when it cannot be read.

    An unreadable window is NOT treated as "any hour": that would quietly restore
    the behaviour this module exists to prevent.
    """
    text = str(raw if raw is not None else DEFAULT_WINDOW).strip()
    if not text:
        return None
    try:
        if "-" in text:
            first, last = text.split("-", 1)
            start, end = int(first), int(last)
        else:
            start = end = int(text)
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None
    return start, end


def hour_in_window(hour: int, window: Tuple[int, int]) -> bool:
    """Inclusive, and wraps midnight so ``22-2`` means 22, 23, 0, 1, 2."""
    start, end = window
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end


def start_allowed(now: Optional[datetime] = None,
                  env: Optional[Mapping[str, str]] = None) -> Tuple[bool, str]:
    """``(allowed, reason)`` for beginning a production run right now."""
    environ = os.environ if env is None else env
    if str(environ.get(FORCE_ENV, "") or "").strip() == "1":
        return True, REASON_FORCED
    window = parse_window(environ.get(WINDOW_ENV))
    if window is None:
        return False, "{}:{!r}".format(REASON_WINDOW_UNREADABLE, environ.get(WINDOW_ENV))
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if hour_in_window(moment.hour, window):
        return True, "{}:{:02d}-{:02d}".format(REASON_INSIDE, *window)
    return False, "{}:{:02d}Z_not_in_{:02d}-{:02d}".format(
        REASON_OUTSIDE, moment.hour, *window)


__all__ = ["start_allowed", "parse_window", "hour_in_window", "WINDOW_ENV", "FORCE_ENV",
           "DEFAULT_WINDOW", "REASON_FORCED", "REASON_INSIDE", "REASON_OUTSIDE",
           "REASON_WINDOW_UNREADABLE"]
