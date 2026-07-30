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


def company_names_exactly_equal(requested: str | None, resolved: str | None) -> bool:
    """True only for an exact (alias-normalized) match -- the strong-evidence
    tier of company_names_compatible. Callers that need to distinguish "these
    are unambiguously the same company" from "these are plausibly related"
    (e.g. before trusting a domainless match for a hard reject decision) should
    use this instead of the full fuzzy compatibility check.
    """
    left = _name_alias(str(requested or ""))
    right = _name_alias(str(resolved or ""))
    return bool(left and right and left == right)


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


def domain_name_consistent(company_name: str | None, domain: str | None) -> bool:
    """Conservative check that a candidate domain plausibly belongs to the
    named company, before ever trusting it as a recovered company domain.

    A denylist of known intermediaries/aggregators (config.INTERMEDIARY_JOB_DOMAINS)
    is necessary but not sufficient: an unlisted third-party job board (a
    regional aggregator hosting a listing "at" a real employer, for example)
    passes a denylist check just as easily as the employer's own site would.
    Confirmed by direct evidence in the 2026-07-29 production corpus: a
    "Great Minds DC" job hosted at californiaconstructores.com, and a
    "Corning" listing on NC Biotech Center's own board -- neither domain is a
    known aggregator, but neither is the named employer's domain either.
    Reuses the same >=5-char substring/brand-extension threshold already
    proven conservative in company_names_compatible.
    """
    core_tokens = _core_name_tokens(company_name)
    if not core_tokens:
        return False
    labels = str(domain or "").strip().lower().split(".")
    if not labels or not labels[0]:
        return False
    brand = labels[0]
    if len(brand) < 3:
        return False
    if brand in core_tokens:
        return True

    ordered = [token for token in normalize_company_name(company_name).split() if token in core_tokens]
    for i in range(len(ordered) - 1):
        if "".join(ordered[i:i + 2]) == brand:
            return True
    joined = "".join(ordered)
    if joined and (
        joined == brand
        or (len(joined) >= 5 and joined in brand)
        or (len(brand) >= 5 and brand in joined)
    ):
        return True

    return any(
        len(token) >= 5 and len(brand) >= 5 and (token in brand or brand in token)
        for token in core_tokens
    )


_TEXT_DOMAIN_RE = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]{2,24})\b",
    re.I,
)
_TEXT_EMAIL_RE = re.compile(r"[\w.+-]+@([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,24})", re.I)


def extract_domain_from_text(
    text: str | None,
    company_name: str | None,
    blocked_domains: Iterable[str],
) -> str:
    """Find a safe, name-consistent company domain already sitting in free
    text (typically a job description) -- e.g. "visit us at acme.com" or a
    "jane@acme.com" contact address. Every candidate still passes through
    the same two independent checks as every other resolution route: the
    intermediary/aggregator denylist and domain_name_consistent(). Confirmed
    via the 2026-07-29 corpus to recover companies with no usable URL
    anywhere on the record but a plain-text domain or email mention in the
    description (FINAL_30_PLUS_SYSTEM_SPEC.md section 9).
    """
    body = str(text or "")
    if not body or not str(company_name or "").strip():
        return ""
    candidates: list[str] = []
    candidates.extend(match.group(1).lower() for match in _TEXT_EMAIL_RE.finditer(body))
    candidates.extend(match.group(1).lower() for match in _TEXT_DOMAIN_RE.finditer(body))
    seen: set[str] = set()
    for raw in candidates:
        domain = normalize_company_domain(raw)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        safe = safe_company_domain(domain, blocked_domains)
        if safe and domain_name_consistent(company_name, safe):
            return safe
    return ""


def canonical_company_key(
    *,
    domain: str | None = None,
    normalized_name: str = "",
    blocked_domains: Iterable[str] = (),
) -> str:
    """The one authoritative company-identity key construction in this
    codebase (FINAL_30_PLUS_SYSTEM_SPEC.md sections 18-19). Every module that
    needs a stable "this company" key should call this rather than building
    its own -- an exhaustive repo-wide audit found several independent
    constructions, including one (recovery_inventory._account_key(), now
    delegating here) that skipped the intermediary-denylist check every
    other resolution path applies, so it could key a company by an
    aggregator host if that host ever ended up in a domain field.

    Takes an already-normalized name rather than normalizing it here: callers
    across this codebase use different (legitimate, pre-existing) name
    normalizers -- company_identity.normalize_company_name() strips legal
    suffixes, job_filter.normalize_text() does not -- and this function must
    not silently change which one a given call site's persisted keys use,
    since that would be a breaking key-format migration, not a pure logic
    fix. Deliberately preserves the existing "domain:{domain}"/"name:{name}"
    key SHAPE already persisted by every known state store.
    """
    safe_domain = safe_company_domain(domain, blocked_domains) if domain else ""
    if safe_domain:
        return f"domain:{safe_domain}"
    return f"name:{normalized_name}" if normalized_name else ""


def _normalize_person_name(value: str | None) -> str:
    """Person-name normalization deliberately does not reuse
    normalize_company_name(), which strips trailing legal-entity suffixes
    (inc/llc/co/...) that are meaningless -- and occasionally wrong -- for a
    person's name."""
    return " ".join(_ascii_words(value))


def canonical_candidate_key(
    *,
    provider_person_id: str | None = None,
    linkedin_url: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company_key: str | None = None,
) -> str:
    """The one authoritative candidate-identity key construction, in the
    confidence order FINAL_30_PLUS_SYSTEM_SPEC.md section 18 specifies:
    provider person ID > stable LinkedIn identifier > verified email >
    normalized name + canonical company > explicit unresolved state.

    Never returns a bare/empty sentinel: the identity audit found
    hiring_manager.py falling back to candidate_id="" whenever a candidate
    lacked a provider person ID, which silently collided every such
    candidate onto the single reroute-tracking key "" -- an unresolved
    candidate must never collide with another unresolved candidate just
    because both lack the same piece of evidence.
    """
    pid = str(provider_person_id or "").strip()
    if pid:
        return f"pid:{pid}"
    linkedin = str(linkedin_url or "").strip().lower().rstrip("/")
    if linkedin:
        return f"li:{linkedin}"
    normalized_email = str(email or "").strip().lower()
    if normalized_email and "@" in normalized_email:
        return f"email:{normalized_email}"
    normalized_name = _normalize_person_name(name)
    if normalized_name:
        return f"name:{normalized_name}|{company_key or ''}"
    import uuid
    return f"unresolved:{uuid.uuid4().hex[:12]}"


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
