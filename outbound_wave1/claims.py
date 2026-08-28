"""Static claim and role-page registry for Outbound Wave 1.

Every externally-checkable statement Challenger B can make is registered here
first. There is deliberately NO runtime lookup of any kind: no web fetch, no
crawl, no provider call. The registry is a file on disk, and a claim that is not
in it simply cannot render.

Two kinds of entry:

``role_pages``
    ``canonical_role -> {url, economics_available, monthly_cost_usd,
    local_comparison_published, claim_source}``. ``role_page_match`` is true
    only for an EXACT canonical-role entry that carries a URL. Economics may be
    quoted only when that same exact entry also carries
    ``economics_available`` and a ``claim_source``. One finance role's published
    number can therefore never be reused as an example for another role, because
    the number is only ever read out of the matched role's own entry.

``claims``
    Named non-economics claims (e.g. the remote-readiness assessment). A claim
    renders only when it is present AND ``verified``; otherwise the campaign
    falls back to wording that needs no external source.

The shipped registry is intentionally unpopulated for economics: no TGTC public
role page or published price exists anywhere in this repository, and inventing
one is exactly what the policy forbids. Populating it is an operator action, and
until then every campaign degrades to its safe, source-free wording.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = "tgtc-outbound-wave1-claims/1"

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "wave1_claims.json"


def _norm_role(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


@dataclass(frozen=True)
class RolePage:
    """One canonical role's public page and (optional) published economics."""

    canonical_role: str
    url: str
    economics_available: bool
    monthly_cost_usd: Optional[int]
    local_comparison_published: bool
    claim_source: str

    @property
    def has_page(self) -> bool:
        return bool(self.url)

    @property
    def can_quote_economics(self) -> bool:
        """Economics needs a page, an explicit availability flag AND a source."""
        return bool(self.url and self.economics_available and self.claim_source)


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str
    verified: bool
    claim_source: str

    @property
    def usable(self) -> bool:
        return bool(self.verified and self.text)


@dataclass(frozen=True)
class ClaimRegistry:
    schema: str
    role_pages: Dict[str, RolePage]
    claims: Dict[str, Claim]
    path: str = ""

    def role_page(self, canonical_role: str) -> Optional[RolePage]:
        """Exact-match lookup only. No fuzzy fallback, no nearest neighbour."""
        key = _norm_role(canonical_role)
        return self.role_pages.get(key) if key else None

    def claim(self, claim_id: str) -> Optional[Claim]:
        return self.claims.get(str(claim_id or "").strip()) if claim_id else None

    @property
    def economics_role_count(self) -> int:
        return sum(1 for page in self.role_pages.values() if page.can_quote_economics)


def _role_page(canonical_role: str, payload: Any) -> RolePage:
    data = payload if isinstance(payload, dict) else {}
    cost = data.get("monthly_cost_usd")
    return RolePage(
        canonical_role=str(data.get("canonical_role") or canonical_role),
        url=str(data.get("url") or "").strip(),
        economics_available=bool(data.get("economics_available")),
        monthly_cost_usd=int(cost) if isinstance(cost, (int, float)) and cost else None,
        local_comparison_published=bool(data.get("local_comparison_published")),
        claim_source=str(data.get("claim_source") or "").strip(),
    )


def _claim(claim_id: str, payload: Any) -> Claim:
    data = payload if isinstance(payload, dict) else {}
    return Claim(
        claim_id=claim_id,
        text=str(data.get("text") or "").strip(),
        verified=bool(data.get("verified")),
        claim_source=str(data.get("claim_source") or "").strip(),
    )


def empty_registry() -> ClaimRegistry:
    """A registry that licenses nothing. Every claim degrades to safe wording."""
    return ClaimRegistry(schema=SCHEMA, role_pages={}, claims={}, path="")


def load_claim_registry(path: Optional[str] = None) -> ClaimRegistry:
    """Read the static registry from disk.

    A missing or unreadable file is not an error: it yields a registry that
    licenses nothing, which is the safe outcome.
    """
    target = Path(path) if path else _DEFAULT_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_registry()
    if not isinstance(raw, dict):
        return empty_registry()
    pages_raw = raw.get("role_pages")
    claims_raw = raw.get("claims")
    role_pages = {
        _norm_role(role): _role_page(role, payload)
        for role, payload in (pages_raw or {}).items()
        if isinstance(pages_raw, dict) and _norm_role(role)
    }
    claims = {
        str(claim_id): _claim(str(claim_id), payload)
        for claim_id, payload in (claims_raw or {}).items()
        if isinstance(claims_raw, dict)
    }
    return ClaimRegistry(
        schema=str(raw.get("schema") or SCHEMA),
        role_pages=role_pages,
        claims=claims,
        path=str(target),
    )


def resolve_role_page(
    registry: ClaimRegistry,
    *,
    canonical_role: str,
    display_role: str,
) -> tuple[bool, Optional[RolePage], str]:
    """Decide ``role_page_match`` for one record.

    ``role_page_match`` is true only when BOTH hold:

    * the record's canonical (``Matched Role``) has an exact registry entry with
      a URL, and
    * the outbound display role is that same role verbatim.

    The second condition is what stops a modified title ("Senior Financial
    Analyst", "Billing Specialist II") from inheriting the plain role's page:
    a modifier changes the scope, and therefore changes the economics.
    """
    page = registry.role_page(canonical_role)
    if page is None:
        return False, None, "no_role_page_entry_for_canonical_role"
    if not page.has_page:
        return False, page, "role_page_entry_has_no_published_url"
    if _norm_role(display_role) != _norm_role(page.canonical_role):
        return False, page, "display_role_is_not_the_exact_mapped_role"
    return True, page, "exact_role_page_match"
