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

_PLACEHOLDER_COMPANY_NAMES = {
    "anonymous",
    "anonymous company",
    "anonymous employer",
    "client",
    "company",
    "company name",
    "confidential",
    "confidential company",
    "confidential employer",
    "employer",
    "employer name",
    "hiring company",
    "name",
    "name withheld",
    "not disclosed",
    "not provided",
    "organization",
    "organisation",
    "our client",
    "private company",
    "reputed company",
    "stealth",
    "stealth startup",
    "the company",
    "the employer",
    "undisclosed",
    "undisclosed company",
    "undisclosed employer",
    "unknown",
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


def is_placeholder_company_name(value: str | None) -> bool:
    """Return whether a company label is a non-identity placeholder.

    Matching is exact after normalization so legitimate brands containing words
    such as ``company`` or ``name`` remain valid.
    """
    normalized = normalize_company_name(value)
    if not normalized:
        return False
    if normalized in _PLACEHOLDER_COMPANY_NAMES:
        return True
    return bool(
        re.fullmatch(
            r"(?:confidential|undisclosed|anonymous)(?: company| employer)?",
            normalized,
        )
        or re.fullmatch(
            r"(?:company|employer|organization|organisation) name",
            normalized,
        )
    )


def _name_alias(value: str) -> str:
    try:
        import config
        aliases = dict(getattr(config, "COMPANY_NAME_ALIASES", {}) or {})
    except Exception:
        aliases = {}
    normalized = normalize_company_name(value)
    normalized_aliases: dict[str, str] = {}
    for key, target in aliases.items():
        source = normalize_company_name(key)
        destination = normalize_company_name(target)
        normalized_aliases[source] = destination
        normalized_aliases[source.replace(" ", "")] = destination
    seen: set[str] = set()
    while normalized not in seen:
        seen.add(normalized)
        next_value = normalized_aliases.get(normalized) or normalized_aliases.get(
            normalized.replace(" ", "")
        )
        if not next_value:
            break
        normalized = next_value
    return normalized


def _domain_alias(value: str | None) -> str:
    try:
        import config
        aliases = dict(getattr(config, "COMPANY_DOMAIN_ALIASES", {}) or {})
    except Exception:
        aliases = {}
    normalized = normalize_company_domain(value)
    normalized_aliases = {
        normalize_company_domain(key): normalize_company_domain(target)
        for key, target in aliases.items()
    }
    seen: set[str] = set()
    while normalized in normalized_aliases and normalized not in seen:
        seen.add(normalized)
        normalized = normalized_aliases[normalized]
    return normalized


def _core_name_tokens(value: str | None) -> set[str]:
    return {
        token
        for token in normalize_company_name(value).split()
        if token not in _LEGAL_SUFFIXES and token not in _GENERIC_NAME_TOKENS
    }


def company_names_compatible(requested: str | None, resolved: str | None) -> bool:
    """Conservative organization-name validation for domainless Apollo lookups."""
    left = _name_alias(str(requested or ""))
    right = _name_alias(str(resolved or ""))
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
    left_domain = _domain_alias(left)
    right_domain = _domain_alias(right)
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
