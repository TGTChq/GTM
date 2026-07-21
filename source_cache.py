"""Small persistent JSON TTL caches for bounded source recovery."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JsonTtlCache:
    def __init__(self, directory: str | Path, ttl_hours: int):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=max(0, ttl_hours))

    @staticmethod
    def _key(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.directory / f"{self._key(key)}.json"

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(payload.get("cached_at", ""))
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            if self.ttl.total_seconds() and _now() - cached_at > self.ttl:
                return None
            value = payload.get("value")
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def set(self, key: str, value: Dict[str, Any]) -> None:
        path = self.path_for(key)
        payload = {"cached_at": _now().isoformat(), "value": value}
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
