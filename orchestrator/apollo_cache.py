"""Cross-run Apollo caches (storage + semantics; flag-gated, default OFF).

Four SEPARATE namespaces, each with its own key, TTL and invalidation so no
cache can be over-applied:

  org            key = normalized employer domain            TTL ~60d
  people_pos     key = domain + search family (role bucket)  TTL ~45d
  zero_people    key = domain                                TTL ~21d  (negative)
  zero_title     key = domain + role bucket                  TTL ~14d  (negative)
  person_match   key = domain + person id                    TTL ~45d

Invariants:
* A negative cache NEVER suppresses forever -- every entry carries an expiry and a
  positive observation for the same key clears the negative entry.
* Entries are stamped with a ``rules_fingerprint`` (the ICP-rule/config fingerprint
  in force when written); reads with a different fingerprint are treated as MISS
  (so an ICP change invalidates firmographic decisions).
* A domain change for a company invalidates its entries (key is the domain).
* Ambiguous/untrusted outcomes (e.g. a domain mismatch flagged by ContactGate /
  EmailGate) must NOT be written: ``put_*`` callers pass ``trusted=True`` only when
  the outcome is authoritative; untrusted outcomes are ignored (counted).
* Provenance: every entry stores ``source``, ``observed_at``, ``expires_at``.
* All I/O is best-effort; a cache failure never affects the pipeline.

Metrics: cache_hit, cache_miss, negative_cache_hit, calls_saved, untrusted_skipped,
expired, invalidated_fingerprint.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CACHE_SCHEMA = "apollo-cache/1"
NAMESPACES = ("org", "people_pos", "zero_people", "zero_title", "person_match")


def normalize_domain(value: str) -> str:
    """Lower-case registrable host without scheme/www/path. Social hosts (linkedin,
    facebook, ...) are NOT valid cache identities -> returns ""."""
    s = str(value or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^https?://", "", s).split("/")[0].split("?")[0]
    s = s[4:] if s.startswith("www.") else s
    if any(s == h or s.endswith("." + h)
           for h in ("linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com")):
        return ""
    return s if "." in s else ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApolloCache:
    def __init__(self, path: str, *, enabled: bool, ttl_days: Dict[str, int],
                 rules_fingerprint: str = "", now: Optional[datetime] = None) -> None:
        self.path = str(path or "")
        self.enabled = bool(enabled and self.path)
        self.ttl_days = {ns: int(ttl_days.get(ns, 30)) for ns in NAMESPACES}
        self.rules_fingerprint = str(rules_fingerprint or "")
        self._now = now
        self.metrics: Dict[str, int] = {k: 0 for k in (
            "cache_hit", "cache_miss", "negative_cache_hit", "calls_saved",
            "untrusted_skipped", "expired", "invalidated_fingerprint", "writes")}
        self.state: Dict[str, Any] = self._load() if self.enabled else {}
        self._dirty = False

    def now(self) -> datetime:
        return self._now or _utcnow()

    # -- persistence --------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("schema") == CACHE_SCHEMA:
                return data
        except (OSError, ValueError):
            pass
        return {"schema": CACHE_SCHEMA, "ns": {ns: {} for ns in NAMESPACES}}

    def save(self) -> None:
        if not self.enabled or not self._dirty:
            return
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            self.state["schema"] = CACHE_SCHEMA
            self.state["updated_at"] = _utcnow().isoformat()
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as exc:
            logger.warning("apollo cache not persisted: %s", type(exc).__name__)

    # -- core -----------------------------------------------------------------
    def _ns(self, ns: str) -> Dict[str, Any]:
        return self.state.setdefault("ns", {}).setdefault(ns, {})

    def get(self, ns: str, key: str, *, fingerprint_sensitive: bool = False) -> Optional[Dict[str, Any]]:
        """Return the cached payload or None (MISS). Expired entries are evicted;
        fingerprint mismatches (when sensitive) are treated as MISS."""
        if not self.enabled or not key:
            return None
        entry = self._ns(ns).get(key)
        if not entry:
            self.metrics["cache_miss"] += 1
            return None
        try:
            exp = datetime.fromisoformat(str(entry.get("expires_at")))
        except (ValueError, TypeError):
            exp = self.now()
        if self.now() >= exp:
            self._ns(ns).pop(key, None)
            self._dirty = True
            self.metrics["expired"] += 1
            self.metrics["cache_miss"] += 1
            return None
        if fingerprint_sensitive and str(entry.get("rules_fingerprint", "")) != self.rules_fingerprint:
            self.metrics["invalidated_fingerprint"] += 1
            self.metrics["cache_miss"] += 1
            return None
        if ns in ("zero_people", "zero_title"):
            self.metrics["negative_cache_hit"] += 1
        else:
            self.metrics["cache_hit"] += 1
        self.metrics["calls_saved"] += 1
        return dict(entry.get("payload") or {})

    def put(self, ns: str, key: str, payload: Dict[str, Any], *, trusted: bool = True,
            source: str = "apollo") -> None:
        if not self.enabled or not key:
            return
        if not trusted:
            self.metrics["untrusted_skipped"] += 1
            return
        ttl = timedelta(days=max(1, self.ttl_days.get(ns, 30)))
        self._ns(ns)[key] = {
            "payload": dict(payload or {}), "source": source,
            "observed_at": self.now().isoformat(), "expires_at": (self.now() + ttl).isoformat(),
            "rules_fingerprint": self.rules_fingerprint,
        }
        # A positive observation clears the corresponding negative entries.
        if ns == "people_pos":
            dom = key.split("|", 1)[0]
            self._ns("zero_people").pop(dom, None)
            self._ns("zero_title").pop(key, None)
        if ns == "org":
            pass
        self._dirty = True
        self.metrics["writes"] += 1

    def invalidate_domain(self, domain: str) -> int:
        """Drop every entry for a domain across namespaces (domain change)."""
        dom = normalize_domain(domain)
        if not self.enabled or not dom:
            return 0
        n = 0
        for ns in NAMESPACES:
            bucket = self._ns(ns)
            for k in [k for k in bucket if k == dom or k.startswith(dom + "|")]:
                bucket.pop(k, None)
                n += 1
        if n:
            self._dirty = True
        return n

    # -- convenience keys ----------------------------------------------------------
    @staticmethod
    def people_key(domain: str, bucket: str) -> str:
        d = normalize_domain(domain)
        return f"{d}|{str(bucket or '').strip().lower()}" if d else ""

    @staticmethod
    def person_key(domain: str, person_id: str) -> str:
        d = normalize_domain(domain)
        return f"{d}|{str(person_id or '').strip()}" if (d and person_id) else ""

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "metrics": dict(self.metrics),
                "sizes": {ns: len(self._ns(ns)) for ns in NAMESPACES} if self.enabled else {}}


def build_cache(cfg, *, rules_fingerprint: str = "", now: Optional[datetime] = None) -> ApolloCache:
    return ApolloCache(
        str(getattr(cfg, "APOLLO_CACHE_PATH", "") or ""),
        enabled=bool(getattr(cfg, "APOLLO_CACHE_ENABLED", False)),
        ttl_days={
            "org": int(getattr(cfg, "APOLLO_CACHE_ORG_TTL_DAYS", 60) or 60),
            "people_pos": int(getattr(cfg, "APOLLO_CACHE_PEOPLE_POSITIVE_TTL_DAYS", 45) or 45),
            "zero_people": int(getattr(cfg, "APOLLO_CACHE_ZERO_PEOPLE_TTL_DAYS", 21) or 21),
            "zero_title": int(getattr(cfg, "APOLLO_CACHE_ZERO_TITLE_TTL_DAYS", 14) or 14),
            "person_match": int(getattr(cfg, "APOLLO_CACHE_PERSON_MATCH_TTL_DAYS", 45) or 45),
        },
        rules_fingerprint=rules_fingerprint, now=now)
