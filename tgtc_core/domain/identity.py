"""Identity: employer, posting, person and lead keys.

Ported from ``company_identity.py`` / ``domain_utils.py`` semantics at ``5d87851``
without the legacy config alias lookup: aliases live in ``employer_aliases``.
``normalize_company_domain`` (public-suffix aware) and ``ATS_DOMAINS`` are the two
legacy utilities imported directly (pure, no config import).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Optional, Sequence, Tuple

from domain_utils import normalize_company_domain  # pure, PSL-backed
from source_domains import ATS_DOMAINS  # pure set

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "plc", "gmbh", "sarl", "sa", "ag", "bv", "lp", "llp", "pc",
}
_GENERIC_NAME_TOKENS = {
    "group", "holdings", "holding", "partners", "partner", "solutions", "services", "service",
    "systems", "system", "technology", "technologies", "tech", "software", "digital", "labs",
    "lab", "global", "international", "ventures", "venture", "ai",
}
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "msn.com",
})
URL_SHORTENER_DOMAINS = frozenset({
    "bit.ly", "bitly.com", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "buff.ly", "lnkd.in",
    "rebrand.ly", "cutt.ly", "is.gd", "shorturl.at", "trib.al", "dlvr.it", "ift.tt",
    "hubs.ly", "okt.to", "rb.gy", "linktr.ee",
})
#: Publishers/aggregators that appear in employer URL fields but are never an
#: employer identity (``config.INTERMEDIARY_JOB_DOMAINS`` head + ATS registry).
AGGREGATOR_DOMAINS = frozenset({
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "careerbuilder.com",
    "adzuna.com", "jooble.org", "talent.com", "lensa.com", "jobright.ai", "jobgether.com",
    "jora.com", "whatjobs.com", "grabjobs.co", "jobicy.com", "wellfound.com", "angel.co",
    "ycombinator.com", "workatastartup.com", "himalayas.app", "remotive.com", "remoteok.com",
    "weworkremotely.com", "monster.com", "dice.com", "simplyhired.com", "builtin.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "tiktok.com",
})
GENERIC_MAILBOXES = re.compile(
    r"^(?:info|hello|contact|sales|support|careers|jobs|recruiting|recruitment|hr|admin|"
    r"office|team|marketing|press|media|help|billing|noreply|no-reply|talent|people)$", re.I)
PLACEHOLDER_NAMES = frozenset({
    "anonymous", "anonymous company", "anonymous employer", "client", "company", "company name",
    "confidential", "confidential company", "confidential employer", "employer", "employer name",
    "hiring company", "name", "name withheld", "not disclosed", "not provided", "organization",
    "organisation", "our client", "private company", "reputed company", "stealth", "stealth startup",
    "the company", "the employer", "undisclosed", "undisclosed company", "undisclosed employer", "unknown",
})


def _ascii_words(value: Optional[str]) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", text)


def normalize_company_name(value: Optional[str]) -> str:
    words = _ascii_words(value)
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def name_key(value: Optional[str]) -> str:
    return normalize_company_name(value).replace(" ", "")


def is_placeholder_company_name(value: Optional[str]) -> bool:
    normalized = normalize_company_name(value)
    return bool(normalized) and (
        normalized in PLACEHOLDER_NAMES
        or bool(re.fullmatch(r"(?:confidential|undisclosed|anonymous)(?: company| employer)?", normalized))
    )


def _core_tokens(value: Optional[str]) -> set[str]:
    return {t for t in normalize_company_name(value).split()
            if t not in _LEGAL_SUFFIXES and t not in _GENERIC_NAME_TOKENS}


def company_names_compatible(left: Optional[str], right: Optional[str]) -> bool:
    """Conservative: exact, safe brand extension, distinctive-token overlap, or >= 0.88 ratio."""
    a, b = normalize_company_name(left), normalize_company_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 5 and re.search(r"\b" + re.escape(shorter) + r"\b", longer):
        return True
    ca, cb = _core_tokens(a), _core_tokens(b)
    if ca and cb:
        overlap = ca & cb
        if {t for t in overlap if len(t) >= 4} and (ca <= cb or cb <= ca or len(overlap) / len(ca | cb) >= 0.6):
            return True
    return SequenceMatcher(None, a, b).ratio() >= 0.88


def company_names_exactly_equal(left: Optional[str], right: Optional[str]) -> bool:
    a, b = normalize_company_name(left), normalize_company_name(right)
    return bool(a and b and a == b)


def domain_name_consistent(company_name: Optional[str], domain: Optional[str]) -> bool:
    """Can the domain's brand label be constructed from the company's own words?"""
    core = _core_tokens(company_name)
    if not core:
        return False
    labels = str(domain or "").strip().lower().split(".")
    brand = labels[0] if labels else ""
    if len(brand) < 3:
        return False
    if brand in core:
        return True
    ordered = [t for t in normalize_company_name(company_name).split() if t in core]
    for i in range(len(ordered) - 1):
        if "".join(ordered[i:i + 2]) == brand:
            return True
    joined = "".join(ordered)
    if joined and (joined == brand or (len(joined) >= 5 and joined in brand) or (len(brand) >= 5 and brand in joined)):
        return True
    return any(len(t) >= 5 and len(brand) >= 5 and (t in brand or brand in t) for t in core)


def is_intermediary_host(domain_or_url: Optional[str]) -> bool:
    d = normalize_company_domain(domain_or_url)
    return bool(d) and (d in ATS_DOMAINS or d in AGGREGATOR_DOMAINS or d in URL_SHORTENER_DOMAINS)


def safe_employer_domain(domain_or_url: Optional[str]) -> str:
    """A registrable employer domain, or '' when it is not an employer identity."""
    d = normalize_company_domain(domain_or_url)
    if not d or is_intermediary_host(d) or d in FREE_MAIL_DOMAINS:
        return ""
    return d


def linkedin_slug(value: Optional[str]) -> str:
    text = str(value or "").strip().lower().strip("/")
    if "/company/" in text:
        text = text.split("/company/", 1)[1].split("/", 1)[0]
    text = text.split("?", 1)[0]
    return re.sub(r"[^a-z0-9-]+", "", text)


def email_domain(email: Optional[str]) -> str:
    value = str(email or "").strip().lower()
    if value.count("@") != 1:
        return ""
    return normalize_company_domain(value.rsplit("@", 1)[1])


def is_generic_mailbox(email: Optional[str]) -> bool:
    local = str(email or "").strip().lower().split("@", 1)[0]
    return bool(GENERIC_MAILBOXES.fullmatch(local))


def email_on_domains(email: Optional[str], allowed: Iterable[Optional[str]]) -> bool:
    candidate = email_domain(email)
    if not candidate or candidate in FREE_MAIL_DOMAINS or candidate in URL_SHORTENER_DOMAINS:
        return False
    allowed_set = {normalize_company_domain(a) for a in allowed if a}
    allowed_set.discard("")
    return candidate in allowed_set


def content_hash(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part if part is not None else "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def normalize_title(title: Optional[str]) -> str:
    text = " ".join(_ascii_words(title))
    text = re.sub(r"\b(remote|hybrid|onsite|on site|us|usa|united states)\b", " ", text)
    return " ".join(text.split())


def posting_canonical_key(employer_key: str, title: Optional[str], description: Optional[str]) -> str:
    """Cross-source identity for ONE job: employer + normalised title + description digest.

    Two provider rows with the same key are the same job seen twice; two rows with
    different keys at the same employer are distinct jobs and are never merged.
    """
    desc = re.sub(r"\s+", " ", str(description or "")).strip().lower()[:4000]
    return content_hash("posting", employer_key, normalize_title(title), hashlib.sha1(desc.encode()).hexdigest()[:16])


def lead_key(domain: str, email: str, function_key: str) -> str:
    """Legacy-compatible ``domain|email|bucket`` so existing Airtable rows suppress."""
    return f"{normalize_company_domain(domain) or domain}|{str(email or '').strip().lower()}|{function_key}"


def person_ref(*, apollo_person_id: Optional[str] = None, linkedin_url: Optional[str] = None,
               email: Optional[str] = None, name: Optional[str] = None, employer_key: str = "") -> str:
    """Stable candidate reference in confidence order: provider id > LinkedIn > email > name+employer."""
    if apollo_person_id:
        return f"pid:{str(apollo_person_id).strip()}"
    if linkedin_url:
        return f"li:{str(linkedin_url).strip().lower().rstrip('/')}"
    if email and "@" in str(email):
        return f"email:{str(email).strip().lower()}"
    words = " ".join(_ascii_words(name))
    if words:
        return f"name:{words}|{employer_key}"
    return ""


def employer_anchors(org: dict) -> Tuple[str, str, str]:
    """(domain, linkedin_slug, name_key) derived from a provider organization block.

    Domain precedence: organization_url -> domain_derived -> org_linkedin_website.
    An ATS/aggregator/shortener host is never an anchor. The name is an anchor only
    when it is not a placeholder.
    """
    domain = ""
    for candidate in (org.get("organization_url"), org.get("domain_derived"), org.get("org_linkedin_website")):
        domain = safe_employer_domain(candidate)
        if domain:
            break
    slug = linkedin_slug(org.get("org_linkedin_slug") or org.get("linkedin_url") or "")
    name = str(org.get("organization") or org.get("org_linkedin_name") or "").strip()
    nk = "" if is_placeholder_company_name(name) else name_key(name)
    return domain, slug, nk


def employer_key(domain: str, slug: str, nk: str) -> str:
    if domain:
        return f"domain:{domain}"
    if slug:
        return f"linkedin:{slug}"
    if nk:
        return f"name:{nk}"
    return ""
