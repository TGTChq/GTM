"""Fair-share claiming between the fresh and backfill lanes.

Under contention the fresh lane receives ``fresh_share_pct`` of claims (default 80)
and backfill the rest. When one lane has nothing claimable the other borrows the
slot, so capacity is never idle. The rotation is deterministic (no randomness) so
it is testable: over any window of 10 claims with both lanes non-empty, exactly
8 go to fresh and 2 to backfill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import psycopg

from ..db import work_queue
from ..db.work_queue import WorkItem


@dataclass
class FairShare:
    fresh_share_pct: int = 80
    _slot: int = field(default=0, repr=False)
    fresh_claims: int = 0
    backfill_claims: int = 0
    borrowed_by_fresh: int = 0
    borrowed_by_backfill: int = 0

    def preferred_lane(self) -> str:
        # Slots 0..9; fresh gets the first `share` slots of every ten.
        share = max(0, min(10, round(self.fresh_share_pct / 10)))
        lane = "fresh" if (self._slot % 10) < share else "backfill"
        self._slot += 1
        return lane

    def claim(self, conn: psycopg.Connection, *, kind: str, lease_seconds: int,
              now: Optional[datetime] = None) -> Optional[WorkItem]:
        preferred = self.preferred_lane()
        other = "backfill" if preferred == "fresh" else "fresh"
        item = work_queue.claim(conn, kind=kind, lane=preferred, lease_seconds=lease_seconds, now=now)
        if item is not None:
            conn.commit()
            self._count(item.lane, borrowed=False)
            return item
        conn.rollback()
        item = work_queue.claim(conn, kind=kind, lane=other, lease_seconds=lease_seconds, now=now)
        if item is not None:
            conn.commit()
            self._count(item.lane, borrowed=True)
            return item
        conn.rollback()
        return None

    def _count(self, lane: str, *, borrowed: bool) -> None:
        if lane == "fresh":
            self.fresh_claims += 1
            if borrowed:
                self.borrowed_by_fresh += 1
        else:
            self.backfill_claims += 1
            if borrowed:
                self.borrowed_by_backfill += 1

    def to_dict(self) -> dict:
        return {"fresh_share_pct": self.fresh_share_pct, "fresh_claims": self.fresh_claims,
                "backfill_claims": self.backfill_claims, "borrowed_by_fresh": self.borrowed_by_fresh,
                "borrowed_by_backfill": self.borrowed_by_backfill}
