"""Deterministic, identity-aware company names for outbound copy.

Canonical/source company fields remain untouched.  This module produces a
separate display value and a fail-closed confidence decision, backed by a small
versioned alias cache.  The cache is evidence, never a replacement for the
LinkedIn-slug/domain identity boundary.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from domain_utils import normalize_company_domain


RESOLVER_VERSION = "company-display/1"
CACHE_SCHEMA = "company-display-cache/1"

_LEGAL_SUFFIX_RE = re.compile(
    r"(?:,?\s+)(incorporated|inc\.?|llc\.?|l\.l\.c\.?|ltd\.?|limited|"
    r"corp\.?|corporation|plc\.?|gmbh|s\.a\.?|sarl|b\.v\.?|llp|lp)\s*$",
    re.I,
)
_PARENT_DESCRIPTOR_RE = re.compile(
    r"^(?P<brand>.+?),\s+(?:an?\s+)?(?P<parent>.+?)\s+company\s*$", re.I
)
_DUAL_LEGAL_ENTITY_RE = re.compile(
    r"^(?P<brand>.+?)\s*/\s*(?P<entity>.+?(?:inc\.?|incorporated|llc\.?|"
    r"ltd\.?|limited|corp\.?|corporation|plc\.?|gmbh))\s*$",
    re.I,
)
_AMBIGUOUS_SEPARATOR_RE = re.compile(r"\s(?:/|\||-|–|—)\s")
_MALFORMED_NAME_RE = re.compile(r"^(?:null\s*,|\d{2,}\s)", re.I)
_LEGAL_WORDS = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "plc", "gmbh", "sa", "sarl", "bv", "llp", "lp",
}


def normalize_display_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("®", "").replace("™", "")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n")


def normalize_linkedin_slug(value: Any) -> str:
    text = normalize_display_text(value).lower().strip("/")
    if "/company/" in text:
        text = text.split("/company/", 1)[1].split("/", 1)[0]
    return re.sub(r"[^a-z0-9]+", "", text)


def _name_words(value: Any) -> list[str]:
    text = unicodedata.normalize("NFKD", normalize_display_text(value))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", text)


def _name_key(value: Any, *, drop_legal: bool = False) -> str:
    words = _name_words(value)
    if drop_legal:
        while words and words[-1] in _LEGAL_WORDS:
            words.pop()
    return "".join(words)


def _domain_brand(domain: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", domain.split(".", 1)[0].lower()) if domain else ""


def _anchor_match(name_key: str, anchor: str) -> str:
    if not name_key or not anchor:
        return ""
    if name_key == anchor:
        return "exact"
    # A brand can safely be the leading component of a longer LinkedIn tenant
    # slug (common for franchise pages), but this is weaker than an exact match.
    if len(name_key) >= 4 and anchor.startswith(name_key) and len(name_key) / len(anchor) >= 0.45:
        return "prefix"
    if len(anchor) >= 4 and name_key.startswith(anchor) and len(anchor) / len(name_key) >= 0.45:
        return "extension"
    return ""


def _anchors_conflict(slug_anchor: str, domain_anchor: str) -> bool:
    if not slug_anchor or not domain_anchor:
        return False
    if slug_anchor == domain_anchor:
        return False
    shorter, longer = sorted((slug_anchor, domain_anchor), key=len)
    if len(shorter) >= 4 and longer.startswith(shorter):
        return False
    return True


@dataclass(frozen=True)
class CompanyDisplayResult:
    name: str
    confidence: str
    hold: bool
    identity_key: str
    identity_safe: bool
    evidence: Dict[str, Any]
    resolver_version: str = RESOLVER_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CompanyDisplayCache:
    """Small atomic JSON cache. Manual entries are never overwritten."""

    def __init__(self, path: str | Path, *, overrides_path: str | Path | None = None):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()
        self._merge_manual_overrides(Path(overrides_path) if overrides_path else None)

    def _empty(self) -> Dict[str, Any]:
        return {
            "schema": CACHE_SCHEMA,
            "resolver_version": RESOLVER_VERSION,
            "entries": {},
            "aliases": {},
        }

    def _load(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("schema") == CACHE_SCHEMA:
                payload.setdefault("entries", {})
                payload.setdefault("aliases", {})
                return payload
        except (OSError, ValueError, TypeError):
            pass
        return self._empty()

    def _merge_manual_overrides(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        entries = payload.get("entries") if isinstance(payload, dict) else None
        aliases = payload.get("aliases") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return
        target_entries = self._data.setdefault("entries", {})
        for key, entry in entries.items():
            if isinstance(entry, dict) and bool(entry.get("manual_override")):
                target_entries[str(key)] = dict(entry)
        if isinstance(aliases, dict):
            target_aliases = self._data.setdefault("aliases", {})
            for alias, primary in aliases.items():
                if primary in target_entries and bool(target_entries[primary].get("manual_override")):
                    target_aliases[str(alias)] = str(primary)

    def lookup(self, linkedin_slug: str, domain: str) -> Optional[Dict[str, Any]]:
        slug_key = f"linkedin:{linkedin_slug}" if linkedin_slug else ""
        domain_key = f"domain:{domain}" if domain else ""
        with self._lock:
            entries = self._data.get("entries") or {}
            if slug_key and isinstance(entries.get(slug_key), dict):
                return dict(entries[slug_key])
            primary = (self._data.get("aliases") or {}).get(domain_key) if domain_key else ""
            entry = entries.get(primary) if primary else None
            if not isinstance(entry, dict):
                return None
            keys = set(entry.get("identity_keys") or [])
            # A domain alias cannot bridge two distinct LinkedIn organizations.
            if slug_key and any(k.startswith("linkedin:") for k in keys) and slug_key not in keys:
                return None
            return dict(entry)

    def put(self, result: CompanyDisplayResult) -> None:
        if result.hold or result.confidence not in {"high", "medium"} or not result.identity_key:
            return
        with self._lock:
            entries = self._data.setdefault("entries", {})
            current = entries.get(result.identity_key)
            if isinstance(current, dict) and bool(current.get("manual_override")):
                return
            identity_keys = list(result.evidence.get("identity_keys") or [])
            entry = {
                "display_name": result.name,
                "confidence": result.confidence,
                "identity_safe": result.identity_safe,
                "identity_keys": identity_keys,
                "evidence": result.evidence,
                "resolver_version": RESOLVER_VERSION,
                "manual_override": False,
            }
            entries[result.identity_key] = entry
            aliases = self._data.setdefault("aliases", {})
            for key in identity_keys:
                if key.startswith("domain:"):
                    aliases[key] = result.identity_key
            self._data["resolver_version"] = RESOLVER_VERSION
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)


_CACHE_INSTANCES: Dict[str, CompanyDisplayCache] = {}
_CACHE_LOCK = threading.Lock()


def default_cache() -> CompanyDisplayCache:
    import config

    path = str(config.OUTBOUND_COMPANY_CACHE_PATH)
    with _CACHE_LOCK:
        if path not in _CACHE_INSTANCES:
            _CACHE_INSTANCES[path] = CompanyDisplayCache(
                path,
                overrides_path=config.OUTBOUND_COMPANY_OVERRIDES_PATH,
            )
        return _CACHE_INSTANCES[path]


def _candidate_sources(
    organization: Any,
    org_linkedin_name: Any,
    canonical_company_name: Any,
) -> Iterable[tuple[str, str]]:
    seen: set[str] = set()
    for source, value in (
        ("organization", organization),
        ("org_linkedin_name", org_linkedin_name),
        ("canonical_company_name", canonical_company_name),
    ):
        text = normalize_display_text(value)
        key = _name_key(text)
        if text and key and key not in seen:
            seen.add(key)
            yield source, text


def _clean_candidate(raw: str, slug_anchor: str, domain_anchor: str) -> tuple[str, list[str]]:
    value = normalize_display_text(raw)
    transformations: list[str] = []
    if value != str(raw or "").strip():
        transformations.append("unicode_whitespace_or_mark_normalized")

    parent = _PARENT_DESCRIPTOR_RE.fullmatch(value)
    if parent:
        brand = normalize_display_text(parent.group("brand"))
        if any(_anchor_match(_name_key(brand, drop_legal=True), a) == "exact"
               for a in (slug_anchor, domain_anchor) if a):
            value = brand
            transformations.append("corroborated_parent_company_descriptor_removed")

    dual = _DUAL_LEGAL_ENTITY_RE.fullmatch(value)
    if dual:
        brand = normalize_display_text(dual.group("brand"))
        if any(_anchor_match(_name_key(brand, drop_legal=True), a) == "exact"
               for a in (slug_anchor, domain_anchor) if a):
            value = brand
            transformations.append("corroborated_legal_entity_alias_removed")

    legal = _LEGAL_SUFFIX_RE.search(value)
    if legal:
        brand = normalize_display_text(value[:legal.start()])
        brand_key = _name_key(brand)
        raw_key = _name_key(value)
        safe = len(brand_key) >= 3 and any(
            _anchor_match(brand_key, anchor) in {"exact", "prefix"}
            or _anchor_match(raw_key, anchor) == "exact"
            for anchor in (slug_anchor, domain_anchor) if anchor
        )
        if safe:
            value = brand
            transformations.append("corroborated_trailing_legal_suffix_removed")

    return normalize_display_text(value), transformations


def resolve_company_display(
    *,
    organization: Any = "",
    org_linkedin_name: Any = "",
    canonical_company_name: Any = "",
    org_linkedin_slug: Any = "",
    org_linkedin_website: Any = "",
    employer_domain: Any = "",
    canonical_identity_verified: bool = False,
    cache: Optional[CompanyDisplayCache] = None,
    persist: Optional[bool] = None,
) -> CompanyDisplayResult:
    slug = normalize_linkedin_slug(org_linkedin_slug)
    domain = normalize_company_domain(employer_domain or org_linkedin_website)
    slug_anchor = slug
    domain_anchor = _domain_brand(domain)
    identity_keys = [key for key in (
        f"linkedin:{slug}" if slug else "",
        f"domain:{domain}" if domain else "",
    ) if key]
    identity_key = identity_keys[0] if identity_keys else ""
    cache = cache or default_cache()
    cached = cache.lookup(slug, domain)
    if cached and cached.get("display_name"):
        manual = bool(cached.get("manual_override"))
        cached_keys = set(cached.get("identity_keys") or [])
        identity_safe = bool(cached.get("identity_safe", True)) and not (
            identity_keys and cached_keys and not (set(identity_keys) & cached_keys)
        )
        if manual or identity_safe:
            confidence = "high" if manual else str(cached.get("confidence") or "medium")
            return CompanyDisplayResult(
                normalize_display_text(cached["display_name"]), confidence, False,
                identity_key or str(next(iter(cached_keys), "")), True,
                {
                    "identity_keys": identity_keys or sorted(cached_keys),
                    "selected_source": "manual_cache" if manual else "display_cache",
                    "cache_hit": True,
                    "manual_override": manual,
                    "reasons": ["sticky_manual_override" if manual else "identity_safe_cache_hit"],
                },
            )

    conflict = _anchors_conflict(slug_anchor, domain_anchor)
    canonical_raw_key = _name_key(canonical_company_name)
    evaluated: list[Dict[str, Any]] = []
    for source, raw in _candidate_sources(organization, org_linkedin_name, canonical_company_name):
        cleaned, transformations = _clean_candidate(raw, slug_anchor, domain_anchor)
        key = _name_key(cleaned, drop_legal=True)
        raw_key = _name_key(raw)
        matches = {
            "linkedin": _anchor_match(key, slug_anchor),
            "domain": _anchor_match(key, domain_anchor),
        }
        if "corroborated_trailing_legal_suffix_removed" in transformations:
            for label, anchor in (("linkedin", slug_anchor), ("domain", domain_anchor)):
                if not matches[label] and _anchor_match(raw_key, anchor) == "exact":
                    matches[label] = "legal"
        exact = sum(value == "exact" for value in matches.values())
        weak = sum(bool(value) for value in matches.values())
        ambiguous = bool(_AMBIGUOUS_SEPARATOR_RE.search(cleaned))
        malformed = bool(_MALFORMED_NAME_RE.search(cleaned))
        verified_canonical_pair = bool(
            canonical_identity_verified
            and canonical_raw_key
            and _name_key(cleaned) == canonical_raw_key
        )
        score = exact * 100 + weak * 30 - len(cleaned) / 10
        if ambiguous or malformed:
            score -= 120
        if transformations:
            score += 15
        evaluated.append({
            "source": source,
            "raw": raw,
            "cleaned": cleaned,
            "transformations": transformations,
            "identity_matches": matches,
            "ambiguous_separator": ambiguous,
            "malformed_name": malformed,
            "verified_canonical_pair": verified_canonical_pair,
            "score": score,
        })

    evaluated.sort(key=lambda item: (-item["score"], len(item["cleaned"]), item["source"]))
    chosen = evaluated[0] if evaluated else None
    name = str((chosen or {}).get("cleaned") or "")
    confidence = "low"
    identity_safe = False
    reasons: list[str] = []

    if not chosen:
        reasons.append("no_company_name_candidate")
    elif not identity_keys:
        reasons.append("no_stable_linkedin_or_domain_identity")
    elif conflict:
        reasons.append("linkedin_slug_domain_disagreement")
    elif chosen["ambiguous_separator"]:
        reasons.append("unresolved_multi_entity_or_franchise_name")
    elif chosen["malformed_name"]:
        reasons.append("malformed_or_coded_company_name")
    else:
        matches = chosen["identity_matches"]
        exact_count = sum(v == "exact" for v in matches.values())
        match_count = sum(bool(v) for v in matches.values())
        plausible_names = {
            _name_key(item["cleaned"], drop_legal=True)
            for item in evaluated
            if any(item["identity_matches"].values()) and not item["ambiguous_separator"]
        }
        disagreement = len({value for value in plausible_names if value}) > 1
        if disagreement and exact_count == 0:
            reasons.append("identity_consistent_candidates_disagree")
        elif exact_count:
            identity_safe = True
            legal_cleanup = "corroborated_trailing_legal_suffix_removed" in chosen["transformations"]
            confidence = "medium" if legal_cleanup else "high"
            reasons.append("display_name_exactly_corroborated_by_identity")
        elif match_count:
            identity_safe = True
            confidence = "medium"
            reasons.append(
                "legal_suffix_removal_corroborated_by_original_identity"
                if any(v == "legal" for v in matches.values())
                else "display_name_weakly_corroborated_by_identity"
            )
        elif chosen["verified_canonical_pair"]:
            identity_safe = True
            confidence = "medium"
            reasons.append("unchanged_canonical_name_domain_pair_verified_by_account_gate")
        else:
            reasons.append("selected_name_not_corroborated_by_identity")

    hold = confidence == "low" or not identity_safe or not name
    evidence = {
        "identity_keys": identity_keys,
        "selected_source": (chosen or {}).get("source", ""),
        "selected_transformations": (chosen or {}).get("transformations", []),
        "cache_hit": False,
        "manual_override": False,
        "identity_conflict": conflict,
        "reasons": reasons,
        "candidates": evaluated,
    }
    result = CompanyDisplayResult(
        name=name, confidence=confidence, hold=hold, identity_key=identity_key,
        identity_safe=identity_safe, evidence=evidence,
    )
    if persist is None:
        persist = not bool(os.getenv("PYTEST_CURRENT_TEST") or "unittest" in sys.modules)
    if persist and not result.hold:
        cache.put(result)
    return result
