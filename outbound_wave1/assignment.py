"""Global, account-level A/B assignment for Outbound Wave 1.

Assignment is a pure function of the experiment id and one account key, so:

* every opportunity and every contact at the same company inherits the same arm;
* a company can never receive A in one campaign and B in another, because the
  campaign is not an input;
* reruns produce the same answer -- nothing is stored, sampled or shuffled.

The campaign is still recorded on every resolution, so results remain
analysable stratified by campaign even though assignment ignores it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, Optional

ARM_A = "A"
ARM_B = "B"
#: Not in the experiment at all. A record that cannot safely render the challenger
#: is suppressed with this arm -- it is NEVER relabelled A. Reassigning a copy
#: failure to the control would quietly make Control A's population different from
#: Challenger B's, and the comparison would stop being a treatment effect.
ARM_NONE = "NONE"

#: Ordered account-key sources. The first that yields a value wins, so the key
#: is as stable as the identity the pipeline already resolved for the company.
#: ``Outbound Company Identity`` is the canonical identity key written by
#: ``company_display_resolver`` (e.g. ``domain:acme.com``), which is exactly the
#: account-level grain we want.
_KEY_SOURCES = (
    "Outbound Company Identity",
    "Website",
    "Outbound Company",
    "Company",
)

_HOST_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/:?#]+)", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _domain_from_url(value: str) -> str:
    match = _HOST_RE.match(value.strip())
    if not match:
        return ""
    host = match.group(1).lower().strip(".")
    return host


def _normalize_name(value: str) -> str:
    return _NON_ALNUM.sub("", value.strip().lower())


def company_assignment_key(fields: Dict) -> str:
    """Return the stable account key for one Airtable record.

    Returns ``""`` when no usable identity exists; the caller must treat that as
    "not assignable" rather than defaulting to an arm.
    """
    for source in _KEY_SOURCES:
        raw = str(fields.get(source) or "").strip()
        if not raw:
            continue
        if source == "Outbound Company Identity":
            # Already canonical (``domain:...`` / ``linkedin:...``).
            return raw.lower()
        if source == "Website":
            host = _domain_from_url(raw)
            if host:
                return f"domain:{host}"
            continue
        normalized = _normalize_name(raw)
        if normalized:
            return f"name:{normalized}"
    return ""


def _bucket_of(experiment_id: str, key: str, salt: str = "") -> int:
    """Deterministic 0-99 bucket for one account key."""
    material = f"{experiment_id}|{salt}|{key}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big") % 100


@dataclass(frozen=True)
class Assignment:
    key: str
    arm: str
    bucket: int
    assignable: bool
    reason: str


def account_assignment(
    fields: Dict,
    *,
    experiment_id: str,
    b_split_pct: int = 50,
    salt: str = "",
    key: Optional[str] = None,
) -> Assignment:
    """Assign one record's company to arm A or B.

    ``b_split_pct`` is the percentage of ACCOUNTS routed to the challenger. It is
    clamped to 0..100; 0 means "everyone stays on Control A", which is the safe
    setting before launch.
    """
    account_key = key if key is not None else company_assignment_key(fields)
    if not account_key:
        # No identity -> not randomisable, so the record is outside the experiment.
        # It is NOT labelled A: an unrandomisable record in the control group would
        # contaminate the control population.
        return Assignment("", ARM_NONE, -1, False, "no_resolvable_company_assignment_key")
    split = max(0, min(100, int(b_split_pct)))
    bucket = _bucket_of(experiment_id, account_key, salt)
    arm = ARM_B if bucket < split else ARM_A
    return Assignment(account_key, arm, bucket, True, "assigned_by_account_key_hash")
