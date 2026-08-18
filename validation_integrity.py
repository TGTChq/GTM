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
    "Outbound Company", "Outbound Company Confidence", "Outbound Company Identity",
    "Outbound Company Evidence", "Outbound Hold",
    "Outbound Role", "Outbound Roles", "Outbound Role Confidence", "Outbound Role Evidence",
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


def fingerprint_payload(fields: Dict) -> Dict:
    """Return the canonical payload used by validation fingerprints.

    Airtable omits unchecked checkbox fields from record responses.  Preserve
    that provider-specific semantic only for Outbound Hold: missing/None/False
    all mean an unchecked checkbox, while an explicit True remains held.
    """
    payload = {key: fields.get(key) for key in SIGNED_FIELDS}
    payload["Outbound Hold"] = fields.get("Outbound Hold") is True
    return payload


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validation_fingerprint(fields: Dict) -> str:
    payload = fingerprint_payload(fields)
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
