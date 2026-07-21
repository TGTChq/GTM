"""Persistent bounded contact-reroute state."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Set

import config


class RerouteRegistry:
    def __init__(self, path: str | None = None):
        self.path = Path(path or config.REROUTE_STATE_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.payload = self._load()

    def _load(self) -> Dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"accounts": {}}
        except (OSError, json.JSONDecodeError):
            return {"accounts": {}}

    def attempted_ids(self, account_bucket_key: str) -> Set[str]:
        accounts = self.payload.setdefault("accounts", {})
        record = accounts.get(account_bucket_key) or {}
        updated_raw = str(record.get("updated_at") or "")
        if updated_raw:
            try:
                updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated < datetime.now(timezone.utc) - timedelta(days=max(1, config.REROUTE_STATE_TTL_DAYS)):
                    accounts.pop(account_bucket_key, None)
                    self._persist()
                    return set()
            except ValueError:
                accounts.pop(account_bucket_key, None)
                self._persist()
                return set()
        return {str(value) for value in record.get("person_ids", []) if value}

    def _persist(self) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.payload, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def record(self, account_bucket_key: str, person_ids: Iterable[str], reason: str) -> None:
        accounts = self.payload.setdefault("accounts", {})
        current = accounts.setdefault(account_bucket_key, {"person_ids": []})
        current["person_ids"] = sorted(set(current.get("person_ids", [])) | {str(v) for v in person_ids if v})
        current["last_reason"] = reason
        current["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._persist()

    def clear(self, account_bucket_key: str) -> None:
        accounts = self.payload.setdefault("accounts", {})
        if account_bucket_key in accounts:
            accounts.pop(account_bucket_key)
            self._persist()
