"""The sidecar's own state. Never a production table.

SQLite by default (an isolated file). Holds: people, cohort membership, phones and
their status, the Apollo reveal ledger (the pilot's separate credit cap), call
attempts, dispositions, referrals, closed job signals and global suppressions.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .config import COHORTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
  person_key TEXT PRIMARY KEY, apollo_person_id TEXT, linkedin TEXT, email TEXT,
  first_name TEXT, last_name TEXT, title TEXT, employer_id INTEGER, employer_name TEXT, employer_domain TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memberships (
  person_key TEXT PRIMARY KEY REFERENCES people(person_key),
  cohort TEXT NOT NULL CHECK (cohort IN ('EMAIL_FOLLOW_UP','CALL_FIRST')),
  status TEXT NOT NULL,             -- active | removed | closed_signal | completed_qualified | needs_phone
  opportunity_id INTEGER, employer_id INTEGER, campaign_key TEXT, persona TEXT,
  account_email_exposed INTEGER NOT NULL DEFAULT 0, prior_email_status TEXT, last_email_at TEXT,
  suggested_opener TEXT, phone_e164 TEXT, job_title TEXT, job_url TEXT, hiring_signal TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phones (
  phone_e164 TEXT PRIMARY KEY, person_key TEXT, type TEXT, source TEXT, status TEXT NOT NULL,
  dnc_status TEXT, confidence TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reveal_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, person_key TEXT, apollo_person_id TEXT, cohort TEXT,
  request_id TEXT, reserved_credits INTEGER NOT NULL, charged_credits INTEGER, credits_reported INTEGER,
  state TEXT NOT NULL,              -- reserved | pending | done | failed
  phone_returned INTEGER, callable INTEGER, outcome TEXT, created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE IF NOT EXISTS call_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, person_key TEXT NOT NULL, phone_e164 TEXT, disposition TEXT NOT NULL,
  notes TEXT, attempted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS referrals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, from_person_key TEXT NOT NULL, opportunity_id INTEGER,
  name TEXT, title TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'pending_validation',
  priority INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS signal_closures (
  opportunity_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS suppressions (
  key TEXT PRIMARY KEY, reason TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, summary_json TEXT);
"""

DISPOSITIONS = ("no_answer", "wrong_number", "former_employee", "not_decision_maker",
                "decision_maker_unavailable", "role_filled", "referral", "qualified_conversation", "do_not_call")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreditCapExceeded(RuntimeError):
    pass


class SidecarStore:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # --- suppression and membership -------------------------------------
    def suppress(self, key: str, reason: str, source: str = "sidecar") -> None:
        if key:
            self.db.execute("INSERT OR IGNORE INTO suppressions (key, reason, source, created_at) VALUES (?,?,?,?)",
                            (key, reason, source, _now()))
            self.db.commit()

    def suppressed(self) -> set:
        return {r["key"] for r in self.db.execute("SELECT key FROM suppressions")}

    def is_suppressed(self, *keys: str) -> bool:
        keys = [k for k in keys if k]
        if not keys:
            return False
        q = "SELECT 1 FROM suppressions WHERE key IN (%s) LIMIT 1" % ",".join("?" * len(keys))
        return self.db.execute(q, keys).fetchone() is not None

    def active_person_keys(self) -> set:
        return {r["person_key"] for r in self.db.execute(
            "SELECT person_key FROM memberships WHERE status IN ('active','needs_phone')")}

    def called_person_keys(self) -> set:
        return {r["person_key"] for r in self.db.execute("SELECT DISTINCT person_key FROM call_attempts")}

    def used_phones(self) -> set:
        return {r["phone_e164"] for r in self.db.execute("SELECT phone_e164 FROM phones")}

    def closed_signals(self) -> set:
        return {r["opportunity_id"] for r in self.db.execute("SELECT opportunity_id FROM signal_closures")}

    def count_active(self, cohort: str) -> int:
        return self.db.execute("SELECT count(*) FROM memberships WHERE cohort = ? AND status = 'active'",
                               (cohort,)).fetchone()[0]

    def add_member(self, *, person: Dict[str, Any], membership: Dict[str, Any], phone: Dict[str, Any]) -> None:
        """One active cohort per person, one person per phone -- enforced here, not only upstream."""
        if membership["cohort"] not in COHORTS:
            raise ValueError("unknown cohort")
        pk = person["person_key"]
        if self.db.execute("SELECT 1 FROM memberships WHERE person_key = ?", (pk,)).fetchone():
            raise ValueError("person already has a cohort membership")
        if self.db.execute("SELECT 1 FROM phones WHERE phone_e164 = ?", (phone["phone_e164"],)).fetchone():
            raise ValueError("phone already assigned")
        now = _now()
        self.db.execute(
            "INSERT OR REPLACE INTO people (person_key, apollo_person_id, linkedin, email, first_name, last_name, title,"
            " employer_id, employer_name, employer_domain, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pk, person.get("apollo_person_id"), person.get("linkedin"), person.get("email"), person.get("first_name"),
             person.get("last_name"), person.get("title"), person.get("employer_id"), person.get("employer_name"),
             person.get("employer_domain"), now))
        self.db.execute(
            "INSERT INTO phones (phone_e164, person_key, type, source, status, dnc_status, confidence, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (phone["phone_e164"], pk, phone.get("type"), phone.get("source"), "valid", phone.get("dnc_status"),
             phone.get("confidence"), now, now))
        m = membership
        self.db.execute(
            "INSERT INTO memberships (person_key, cohort, status, opportunity_id, employer_id, campaign_key, persona,"
            " account_email_exposed, prior_email_status, last_email_at, suggested_opener, phone_e164, job_title, job_url,"
            " hiring_signal, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pk, m["cohort"], "active", m.get("opportunity_id"), m.get("employer_id"), m.get("campaign_key"),
             m.get("persona"), 1 if m.get("account_email_exposed") else 0, m.get("prior_email_status"),
             m.get("last_email_at"), m.get("suggested_opener"), phone["phone_e164"], m.get("job_title"),
             m.get("job_url"), m.get("hiring_signal"), now, now))
        self.db.commit()

    # --- Apollo reveal ledger: the pilot's own hard credit cap -----------
    def credits_committed(self) -> int:
        """Credits charged by finished reveals plus the worst case of every open one."""
        row = self.db.execute(
            "SELECT coalesce(sum(CASE WHEN state IN ('done','failed') THEN coalesce(charged_credits, reserved_credits)"
            " ELSE reserved_credits END), 0) FROM reveal_ledger").fetchone()
        return int(row[0])

    def reserve_reveal(self, *, person_key: str, apollo_person_id: str, cohort: str, credits: int, cap: int) -> int:
        if self.credits_committed() + credits > cap:
            raise CreditCapExceeded(f"reveal would exceed the pilot cap of {cap} credits")
        cur = self.db.execute(
            "INSERT INTO reveal_ledger (person_key, apollo_person_id, cohort, reserved_credits, state, created_at)"
            " VALUES (?,?,?,?, 'reserved', ?)", (person_key, apollo_person_id, cohort, credits, _now()))
        self.db.commit()
        return int(cur.lastrowid)

    def mark_pending(self, ledger_id: int, request_id: str) -> None:
        self.db.execute("UPDATE reveal_ledger SET state='pending', request_id=? WHERE id=?", (request_id, ledger_id))
        self.db.commit()

    def settle_reveal(self, ledger_id: int, *, state: str, charged: int, reported: Optional[int],
                      phone_returned: bool, callable_: bool, outcome: str) -> None:
        self.db.execute(
            "UPDATE reveal_ledger SET state=?, charged_credits=?, credits_reported=?, phone_returned=?, callable=?,"
            " outcome=?, finished_at=? WHERE id=?",
            (state, charged, reported, 1 if phone_returned else 0, 1 if callable_ else 0, outcome, _now(), ledger_id))
        self.db.commit()

    def ledger_summary(self) -> Dict[str, Any]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM reveal_ledger")]
        return {
            "attempts": len(rows),
            "phone_returned": sum(1 for r in rows if r["phone_returned"]),
            "callable": sum(1 for r in rows if r["callable"]),
            "credits_charged": sum(int(r["charged_credits"] or 0) for r in rows if r["state"] in ("done", "failed")),
            "credits_reported_by_apollo": sum(int(r["credits_reported"] or 0) for r in rows),
            "open": sum(1 for r in rows if r["state"] in ("reserved", "pending")),
            "by_outcome": _count(r["outcome"] or r["state"] for r in rows),
            "by_cohort": _count(r["cohort"] for r in rows),
        }

    # --- dispositions: sidecar state only, never production -------------
    def record_disposition(self, person_key: str, disposition: str, *, phone_e164: str = "", notes: str = "",
                           referral: Optional[Dict[str, Any]] = None) -> None:
        if disposition not in DISPOSITIONS:
            raise ValueError(f"unknown disposition {disposition!r}")
        m = self.db.execute("SELECT * FROM memberships WHERE person_key = ?", (person_key,)).fetchone()
        if m is None:
            raise LookupError("no membership for this person")
        phone = phone_e164 or m["phone_e164"]
        now = _now()
        self.db.execute("INSERT INTO call_attempts (person_key, phone_e164, disposition, notes, attempted_at)"
                        " VALUES (?,?,?,?,?)", (person_key, phone, disposition, notes, now))

        def set_status(status):
            self.db.execute("UPDATE memberships SET status=?, updated_at=? WHERE person_key=?", (status, now, person_key))

        if disposition == "do_not_call":
            person = self.db.execute("SELECT * FROM people WHERE person_key = ?", (person_key,)).fetchone()
            for key in (f"person:{person_key}", f"phone:{phone}",
                        f"email:{person['email']}" if person and person["email"] else "",
                        f"li:{person['linkedin']}" if person and person["linkedin"] else ""):
                self.suppress(key, "do_not_call")
            set_status("removed")
        elif disposition == "former_employee":
            self.suppress(f"person:{person_key}", "former_employee")   # out of BOTH future cohorts
            set_status("removed")
        elif disposition == "wrong_number":
            # Invalidate only this phone; the person stays eligible with a different number.
            self.db.execute("UPDATE phones SET status='invalid_wrong_number', updated_at=? WHERE phone_e164=?", (now, phone))
            self.suppress(f"phone:{phone}", "wrong_number")
            set_status("needs_phone")
        elif disposition == "not_decision_maker":
            set_status("removed")
        elif disposition == "role_filled":
            if m["opportunity_id"] is not None:
                self.db.execute("INSERT OR IGNORE INTO signal_closures (opportunity_id, reason, created_at) VALUES (?,?,?)",
                                (m["opportunity_id"], "role_filled", now))
                self.db.execute("UPDATE memberships SET status='closed_signal', updated_at=? "
                                "WHERE opportunity_id=? AND status='active'", (now, m["opportunity_id"]))
        elif disposition == "referral":
            r = referral or {}
            self.db.execute("INSERT INTO referrals (from_person_key, opportunity_id, name, title, notes, status, priority,"
                            " created_at) VALUES (?,?,?,?,?, 'pending_validation', 1, ?)",
                            (person_key, m["opportunity_id"], r.get("name"), r.get("title"), notes, now))
        elif disposition == "qualified_conversation":
            set_status("completed_qualified")
        # no_answer, decision_maker_unavailable: the attempt is recorded; membership unchanged.
        self.db.commit()

    def members(self) -> List[Dict[str, Any]]:
        q = ("SELECT m.*, p.first_name, p.last_name, p.title, p.email, p.linkedin, p.employer_name, p.employer_domain,"
             " p.apollo_person_id, ph.type AS phone_type, ph.source AS phone_source, ph.status AS phone_status,"
             " ph.dnc_status, ph.confidence FROM memberships m JOIN people p ON p.person_key = m.person_key"
             " LEFT JOIN phones ph ON ph.phone_e164 = m.phone_e164 ORDER BY m.cohort, m.created_at")
        return [dict(r) for r in self.db.execute(q)]

    def start_run(self) -> int:
        cur = self.db.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, summary: Dict[str, Any]) -> None:
        self.db.execute("UPDATE runs SET finished_at=?, summary_json=? WHERE id=?",
                        (_now(), json.dumps(summary, default=str), run_id))
        self.db.commit()


def _count(values: Iterable) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out
