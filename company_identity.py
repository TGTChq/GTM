"""Company/contact identity guards used before any outbound record is reviewable.

Job aggregators frequently expose their own domain as ``employer_website`` even
when the visible employer name belongs to a different company. These helpers
keep publisher domains out of Apollo, validate name-only organization matches,
and ensure the selected person/email still belongs to the resolved employer.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable

from domain_utils import normalize_company_domain

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "sarl", "sa", "ag", "bv", "lp", "llp",
}
_GENERIC_NAME_TOKENS = {
    "group", "holdings", "holding", "partners", "partner", "solutions",
    "services", "service", "systems", "system", "technology", "technologies",
    "tech", "software", "digital", "labs", "lab", "global", "international",
    "ventures", "venture", "ai",
}
_FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
}


def is_intermediary_domain(domain_or_url: str | None, blocked_domains: Iterable[str]) -> bool:
    domain = normalize_company_domain(domain_or_url)
    if not domain:
        return False
    normalized_blocked = {
        normalized
        for blocked in blocked_domains
        if (normalized := normalize_company_domain(blocked))
    }
    return domain in normalized_blocked


def safe_company_domain(
    domain_or_url: str | None,
    blocked_domains: Iterable[str],
) -> str:
    domain = normalize_company_domain(domain_or_url)
    if not domain or is_intermediary_domain(domain, blocked_domains):
        return ""
    return domain


def _ascii_words(value: str | None) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", text)


def normalize_company_name(value: str | None) -> str:
    words = _ascii_words(value)
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _core_name_tokens(value: str | None) -> set[str]:
    return {
        token
        for token in normalize_company_name(value).split()
        if token not in _LEGAL_SUFFIXES and token not in _GENERIC_NAME_TOKENS
    }


def company_names_compatible(requested: str | None, resolved: str | None) -> bool:
    """Conservative organization-name validation for domainless Apollo lookups."""
    left = normalize_company_name(requested)
    right = normalize_company_name(resolved)
    if not left or not right:
        return False
    if left == right:
        return True

    # Accept safe brand extensions such as ``Kintsugi`` -> ``Kintsugi AI``.
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 5 and re.search(r"\b" + re.escape(shorter) + r"\b", longer):
        return True

    left_core = _core_name_tokens(left)
    right_core = _core_name_tokens(right)
    if left_core and right_core:
        overlap = left_core & right_core
        distinctive_overlap = {token for token in overlap if len(token) >= 4}
        if distinctive_overlap and (
            left_core <= right_core
            or right_core <= left_core
            or len(overlap) / len(left_core | right_core) >= 0.6
        ):
            return True

    return SequenceMatcher(None, left, right).ratio() >= 0.88


def domains_equivalent(left: str | None, right: str | None) -> bool:
    left_domain = normalize_company_domain(left)
    right_domain = normalize_company_domain(right)
    return bool(left_domain and right_domain and left_domain == right_domain)


def email_domain(email: str | None) -> str:
    value = str(email or "").strip().lower()
    if value.count("@") != 1:
        return ""
    return normalize_company_domain(value.rsplit("@", 1)[1])


def email_matches_company(email: str | None, allowed_domains: Iterable[str | None]) -> bool:
    candidate = email_domain(email)
    if not candidate or candidate in _FREE_EMAIL_DOMAINS:
        return False
    allowed = {
        normalized
        for value in allowed_domains
        if (normalized := normalize_company_domain(value))
    }
    return candidate in allowed
