"""Atomic, request-keyed paid replies outside prunable run artifacts.

This is evidence reuse, not approval reuse: current gates still evaluate a reply.
One file per request avoids rewriting a growing corpus after every paid result.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class PaidReplyCustody:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, kind, identity):
        if kind not in {"org", "match"} or len(identity) != 64 or any(
                ch not in "0123456789abcdef" for ch in identity):
            raise ValueError("Invalid paid request identity")
        return self.root / kind / (identity + ".json")

    def get(self, kind, identity, ttl_days, retry_ttl_days=None):
        path = self._path(kind, identity)
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if (not isinstance(item, dict) or item.get("schema") != "paid-reply/1"
                or item.get("request_identity") != identity
                or not isinstance(item.get("result"), dict)
                or not isinstance(item.get("stored_at"), (int, float))):
            raise ValueError("Unreadable paid reply; preserve for reconciliation")
        result = item["result"]
        positive = (result.get("found") if kind == "org" else
                    result.get("person_found") and result.get("email_status") == "verified")
        if not positive and retry_ttl_days is not None:
            ttl_days = min(ttl_days, retry_ttl_days)
        if ttl_days <= 0 or time.time() - item["stored_at"] > ttl_days * 86400:
            return None
        return item["result"]

    def put(self, kind, identity, result):
        path = self._path(kind, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        # Provider replies can contain personal data. The run lock serializes
        # this writer; files are private and never placed in reporting artifacts.
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"schema": "paid-reply/1", "request_identity": identity,
                       "stored_at": time.time(), "result": result}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
