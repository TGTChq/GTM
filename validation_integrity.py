"""Immutable validation timestamp and fingerprint helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Dict

import config


SIGNED_FIELDS = (
    # Identity and destination.
    "Company", "Website", "Open Role", "Open Roles", "Role Focus",
    "Matched Role", "Role Bucket", "Campaign ID", "Employees",
    # Job evidence used at approval and sent to Instantly.
    "Job URL", "Job URL Status", "Job URL Source", "Job ID",
    "Location", "Employment Type",
    # Contact and email identity.
    "Hiring Manager", "HM Title", "LinkedIn", "Apollo Person ID", "Email",
    # Qualification boundary. Status is intentionally excluded because a
    # reviewer must be able to change Pending -> Approved.
    "Final Decision", "Validation Version", "Validated At",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validation_fingerprint(fields: Dict) -> str:
    payload = {key: fields.get(key) for key in SIGNED_FIELDS}
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    key = str(config.VALIDATION_SIGNING_KEY or "")
    if not key:
        if config.PRODUCTION and not os.getenv("PYTEST_CURRENT_TEST"):
            raise ValueError("VALIDATION_SIGNING_KEY is required in production")
        key = "offline-test-key"
    return hmac.new(key.encode(), serialized.encode(), hashlib.sha256).hexdigest()


def fingerprint_matches(fields: Dict) -> bool:
    supplied = str(fields.get("Validation Fingerprint") or "")
    return bool(supplied and hmac.compare_digest(supplied, validation_fingerprint(fields)))
