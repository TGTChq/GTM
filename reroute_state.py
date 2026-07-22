"""Persistent contact-reroute state with reason-specific expiration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Set

import config


_PERMANENT_MARKERS = (
    "WRONG_ORGANIZATION",
    "FUNCTION_MISMATCH",
    "SENIORITY_MISMATCH",
    "TERRITORY_MISMATCH",
    "NOT_CURRENT_EMPLOYEE",
    "EMAIL_IDENTITY_MISMATCH",
    "EMAIL_DOMAIN_MISMATCH",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _expiry_for_reason(reason: str) -> datetime:
    upper = str(reason or "").upper()
    if any(marker in upper for marker in _PERMANENT_MARKERS):
        return _now() + timedelta(days=max(1, config.REROUTE_PERMANENT_TTL_DAYS))
    return _now() + timedelta(hours=max(1, config.REROUTE_TEMPORARY_TTL_HOURS))


class RerouteRegistry:
    def __init__(self, path: str | None = None):
        self.path = Path(path or config.REROUTE_STATE_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.payload = self._load()

    def _load(self) -> Dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"version": 2, "accounts": {}}
        except (OSError, json.JSONDecodeError):
            return {"version": 2, "accounts": {}}

    def attempted_ids(self, account_bucket_key: str) -> Set[str]:
        accounts = self.payload.setdefault("accounts", {})
        record = accounts.get(account_bucket_key) or {}
        people = record.get("people")
        # Backward-compatible migration from the v0.4 account-wide TTL shape.
        if not isinstance(people, dict):
            updated = _parse(str(record.get("updated_at") or "")) or _now()
            expires = updated + timedelta(days=max(1, config.REROUTE_STATE_TTL_DAYS))
            people = {
                str(person_id): {
                    "reason": str(record.get("last_reason") or "legacy_reroute"),
                    "expires_at": expires.isoformat(),
                }
                for person_id in record.get("person_ids", [])
                if person_id
            }
            record = {"people": people, "updated_at": _now().isoformat()}
            accounts[account_bucket_key] = record

        active: Set[str] = set()
        changed = False
        for person_id, item in list(people.items()):
            expires = _parse(str((item or {}).get("expires_at") or ""))
            if not expires or expires <= _now():
                people.pop(person_id, None)
                changed = True
                continue
            active.add(str(person_id))
        if not people:
            accounts.pop(account_bucket_key, None)
            changed = True
        if changed:
            self._persist()
        return active

    def _persist(self) -> None:
        self.payload["version"] = 2
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.payload, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def record(self, account_bucket_key: str, person_ids: Iterable[str], reason: str) -> None:
        accounts = self.payload.setdefault("accounts", {})
        current = accounts.setdefault(account_bucket_key, {"people": {}})
        people = current.setdefault("people", {})
        expires = _expiry_for_reason(reason).isoformat()
        for person_id in person_ids:
            if person_id:
                people[str(person_id)] = {
                    "reason": str(reason or ""),
                    "expires_at": expires,
                    "recorded_at": _now().isoformat(),
                }
        current["last_reason"] = reason
        current["updated_at"] = _now().isoformat()
        self._persist()

    def clear(self, account_bucket_key: str) -> None:
        accounts = self.payload.setdefault("accounts", {})
        if account_bucket_key in accounts:
            accounts.pop(account_bucket_key)
            self._persist()
