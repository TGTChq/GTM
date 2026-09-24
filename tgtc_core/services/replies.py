"""Bringing Instantly's replies into the core, and deciding what each one means.

Audited 2026-09-24: Instantly receives the replies and the core had never seen one --
``outcome_events`` and ``suppressions`` were both empty and no webhook was configured.
This is the path that closes that, as a poller rather than a receiver, because every
production process here is a cron with no inbound HTTP.

Three properties it is built around:

* **Exactly once.** Each reply is keyed by Instantly's own message id, and
  ``apply_outcome_event`` inserts on that key with ``ON CONFLICT DO NOTHING``. Re-polling
  the same page changes nothing.
* **The cursor only moves over work that was written.** It advances after a page is
  recorded, so a crash re-reads a page instead of skipping one.
* **Reading never sends.** Ingestion classifies, records and queues. What follows --
  waiting for a return date, replacing a departed contact -- is a work item a later,
  separate step picks up, and a reply written by a person never becomes an automatic
  email.

Only the nine Challenger campaigns are ingested. The rest of the workspace (Control,
Wave 1, older campaigns) is another project's history and is skipped by id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

import psycopg

from ..db.connection import jsonb
from ..domain.reply_classification import (HUMAN_REPLY, NO_LONGER_HERE, OUT_OF_OFFICE, UNKNOWN,
                                           ReplyVerdict, classify)
from .suppression import apply_outcome_event

#: Where the poll cursor lives (``provider_state.details``).
CURSOR_PROVIDER = "instantly_replies"

#: Work the ingestion queues for a later, separate step. Named so they can be found.
FOLLOW_UP_WHEN_BACK = "reply_followup_when_back"
REPLACE_DEPARTED_CONTACT = "replace_departed_contact"
REVIEW_REPLY = "review_reply"


@dataclass
class PollReport:
    pages: int = 0
    seen: int = 0
    ours: int = 0
    skipped_other_campaigns: int = 0
    recorded: int = 0
    duplicates: int = 0
    by_label: Dict[str, int] = field(default_factory=dict)
    queued: Dict[str, int] = field(default_factory=dict)
    suppressed: int = 0
    unmatched_people: int = 0
    cursor: Optional[str] = None
    stopped_at_known_ground: bool = False
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"pages": self.pages, "replies_seen": self.seen, "for_the_nine_campaigns": self.ours,
                "skipped_other_campaigns": self.skipped_other_campaigns, "recorded": self.recorded,
                "already_known": self.duplicates, "by_label": self.by_label, "queued": self.queued,
                "addresses_suppressed": self.suppressed, "replies_with_no_person_in_the_core": self.unmatched_people,
                "cursor": self.cursor, "stopped_at_known_ground": self.stopped_at_known_ground,
                "dry_run": self.dry_run}


# --------------------------------------------------------------------------------
# cursor
# --------------------------------------------------------------------------------

def read_cursor(conn: psycopg.Connection) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT details FROM provider_state WHERE provider = %s", (CURSOR_PROVIDER,))
        row = cur.fetchone()
    conn.rollback()
    return (row["details"] or {}).get("cursor") if row else None


def write_cursor(conn: psycopg.Connection, cursor: Optional[str], *, seen: int, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO provider_state (provider, state, last_attempt_at, last_served_at, details) "
            "VALUES (%s, 'serving', %s, %s, %s) "
            "ON CONFLICT (provider) DO UPDATE SET state = 'serving', last_attempt_at = EXCLUDED.last_attempt_at, "
            "last_served_at = EXCLUDED.last_served_at, details = EXCLUDED.details, updated_at = now()",
            (CURSOR_PROVIDER, now, now, jsonb({"cursor": cursor, "last_page_replies": seen})))
    conn.commit()


# --------------------------------------------------------------------------------
# what a reply belongs to
# --------------------------------------------------------------------------------

def _locate(conn: psycopg.Connection, email: str) -> Optional[Dict[str, Any]]:
    """The approval this address belongs to, if the core knows it."""
    if not email:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id AS approval_id, a.person_id, a.opportunity_id, a.employer_id, a.campaign_key,
                   a.campaign_id, a.state
            FROM approvals a JOIN people p ON p.id = a.person_id
            WHERE lower(p.email) = lower(%s) ORDER BY a.approved_at DESC LIMIT 1
            """, (email,))
        row = cur.fetchone()
    conn.rollback()
    return dict(row) if row else None


def _queue(conn: psycopg.Connection, kind: str, subject_kind: str, subject_id: int, *,
           available_at: datetime, waiting_on: str = "") -> bool:
    """Put one piece of follow-up work on the queue, once."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (kind, subject_kind, subject_id, state, available_at, waiting_on) "
            "VALUES (%s, %s, %s, 'ready', %s, %s) "
            "ON CONFLICT (kind, subject_kind, subject_id) DO NOTHING RETURNING id",
            (kind, subject_kind, subject_id, available_at, waiting_on or None))
        created = cur.fetchone() is not None
    return created


# --------------------------------------------------------------------------------
# the poll
# --------------------------------------------------------------------------------

def ingest_one(conn: psycopg.Connection, item: Dict[str, Any], *, now: datetime,
               dry_run: bool = False) -> Dict[str, Any]:
    """Classify one reply, record it once, and queue what follows -- never a send."""
    message_id = str(item.get("id") or item.get("message_id") or "").strip()
    email = str(item.get("from_address_email") or "").strip().lower()
    body = item.get("body")
    text = str((body or {}).get("text") or "") if isinstance(body, dict) else str(body or "")
    verdict: ReplyVerdict = classify(str(item.get("subject") or ""), text or str(item.get("content_preview") or ""),
                                     provider_label=item.get("i_status"), received_at=now)
    located = _locate(conn, email)
    out = {"message_id": message_id, "label": verdict.label, "queued": None, "recorded": None,
           "person_known": located is not None}
    if dry_run:
        out["recorded"] = "dry_run"
        return out

    # The event, keyed by Instantly's own id: re-polling a page records nothing new.
    # Only opt-out and a departure are suppressing kinds; an out-of-office is not, and
    # neither is a reply a person wrote.
    out["recorded"] = apply_outcome_event(
        conn, provider="instantly", event_type=verdict.label,
        dedupe_key=f"instantly:email:{message_id}", email=email,
        campaign_id=str(item.get("campaign_id") or ""), external_id=message_id,
        occurred_at=_parsed(item.get("timestamp_email")) or now,
        payload={**verdict.to_dict(), "subject": str(item.get("subject") or "")[:140],
                 "approval_id": (located or {}).get("approval_id")})
    if out["recorded"] == "duplicate" or located is None:
        return out

    if verdict.label == OUT_OF_OFFICE:
        # Wait. Nothing is suppressed and nobody else is contacted; if the reply gave a
        # date, the follow-up becomes available on it rather than immediately.
        when = datetime.combine(verdict.returns_on, datetime.min.time(), tzinfo=timezone.utc) \
            if verdict.returns_on else now + timedelta(days=7)
        if _queue(conn, FOLLOW_UP_WHEN_BACK, "approval", int(located["approval_id"]),
                  available_at=when, waiting_on="out_of_office"):
            out["queued"] = FOLLOW_UP_WHEN_BACK
    elif verdict.label == NO_LONGER_HERE:
        # That address is done. The unit still has a vacancy to fill, which is a search,
        # not a send, and it happens in the ordinary contact path with every gate.
        if _queue(conn, REPLACE_DEPARTED_CONTACT, "opportunity", int(located["opportunity_id"]),
                  available_at=now, waiting_on="contact_departed"):
            out["queued"] = REPLACE_DEPARTED_CONTACT
    elif verdict.label in (HUMAN_REPLY, UNKNOWN):
        if _queue(conn, REVIEW_REPLY, "approval", int(located["approval_id"]), available_at=now,
                  waiting_on=verdict.label):
            out["queued"] = REVIEW_REPLY
    conn.commit()
    return out


def _parsed(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def poll(conn: psycopg.Connection, client, *, campaign_ids: Sequence[str], now: datetime,
         page_size: int = 100, max_pages: int = 10, dry_run: bool = False,
         resume: bool = False) -> PollReport:
    """Read new replies for the nine campaigns and record what they mean.

    The feed is newest-first, so each poll starts at the TOP and stops as soon as a page
    holds nothing new: resuming from the last cursor would walk further into history and
    never see the replies that arrived since. The dedupe key makes that cheap -- a page
    of replies already known costs one request and no writes -- and it makes an
    interrupted poll safe, because re-reading is free and a missed departure is not.

    ``resume`` continues from the stored cursor instead, which is how a first sweep
    through older history is finished.
    """
    ours = {str(c) for c in campaign_ids if c}
    report = PollReport(dry_run=dry_run)
    cursor = read_cursor(conn) if resume else None
    for _ in range(max_pages):
        result = client.list_received_emails(limit=page_size, starting_after=cursor)
        if not result.ok:
            report.cursor = cursor
            return report
        items = (result.data or {}).get("items") or []
        if not items:
            break
        report.pages += 1
        new_on_this_page = 0
        for item in items:
            report.seen += 1
            if str(item.get("campaign_id") or "") not in ours:
                report.skipped_other_campaigns += 1
                continue
            report.ours += 1
            outcome = ingest_one(conn, item, now=now, dry_run=dry_run)
            report.by_label[outcome["label"]] = report.by_label.get(outcome["label"], 0) + 1
            if outcome["recorded"] == "duplicate":
                report.duplicates += 1
            else:
                new_on_this_page += 1
                if outcome["recorded"] in ("applied", "recorded"):
                    report.recorded += 1
            if outcome["recorded"] == "applied":
                report.suppressed += 1
            if not outcome["person_known"]:
                report.unmatched_people += 1
            if outcome["queued"]:
                report.queued[outcome["queued"]] = report.queued.get(outcome["queued"], 0) + 1
        cursor = (result.data or {}).get("next_starting_after") or (result.data or {}).get("next_cursor")
        if not dry_run:
            write_cursor(conn, cursor, seen=len(items), now=now)
        if not cursor:
            break
        if new_on_this_page == 0 and report.ours:
            # Everything on this page was already known: the rest is older still.
            report.stopped_at_known_ground = True
            break
    report.cursor = cursor
    return report
