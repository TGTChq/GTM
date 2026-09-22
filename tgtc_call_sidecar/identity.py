"""Stable identity keys: one normalisation for every source."""
from __future__ import annotations

import re
from typing import Optional

_LINKEDIN = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.I)


def norm_email(value) -> str:
    v = str(value or "").strip().lower()
    return v if "@" in v and "." in v.split("@")[-1] else ""


def norm_linkedin(value) -> str:
    m = _LINKEDIN.search(str(value or ""))
    return f"linkedin.com/in/{m.group(1).lower().rstrip('/')}" if m else ""


def norm_domain(value) -> str:
    v = str(value or "").strip().lower()
    v = re.sub(r"^https?://", "", v)
    v = v.split("/")[0].split("?")[0]
    return v[4:] if v.startswith("www.") else v


def norm_phone_us(value) -> Optional[str]:
    """E.164 for a US/NANP number, else None (the pilot calls US numbers only)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw.split("x")[0].split("ext")[0])
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return None
    return "+1" + digits


def name_company_key(first, last, company) -> str:
    def clean(s):
        return re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    f, l, c = clean(first), clean(last), clean(company)
    return f"{f}|{l}|{c}" if f and l and c else ""


def person_key(*, apollo_person_id="", linkedin="", email="") -> str:
    """The sidecar's own person key: Apollo id first, then LinkedIn, then email."""
    if apollo_person_id:
        return f"apollo:{apollo_person_id}"
    li = norm_linkedin(linkedin)
    if li:
        return f"li:{li}"
    em = norm_email(email)
    return f"email:{em}" if em else ""
