"""How a stable identity anchor is DERIVED from a published company name.

A LinkedIn slug and an employer domain are the two stable identifiers this system
has for an organization. They routinely disagree as strings while naming one
company, and the previous rule -- "the published name must be literally contained
in both" -- read every such disagreement as evidence of two companies. Production
2026-09-07 held five distinct shapes of ONE company under that rule.

This module answers a narrower, checkable question instead:

    can this anchor be CONSTRUCTED from the words of this published name?

If it can, the anchor is corroboration, not conflict -- and the construction is
returned so the decision is auditable rather than asserted. Nothing here consults a
list of company names, and nothing here compares two companies to each other: every
function relates ONE published name to ONE anchor. Two organizations are therefore
never merged by anything in this file; the identity keys stay exactly what they were.

THE FOUR DERIVATIONS, each observed in production and each a naming convention
rather than a guess:

* ``exact`` -- the anchor is the name's words concatenated. The existing bar.
* ``domain_full_label_key`` -- the whole domain including its TLD spells the name.
  Modern brand TLDs make the second-level label a fragment: ``kai.security`` is not
  the company "Kai" plus a suffix, it is the brand. Splitting on the first dot threw
  the other half away and manufactured a disagreement with the slug ``kaisecurity``.
* ``vanity_prefix_stripped`` -- ``usenash.com`` is "Nash". ``use/get/try/join/my``
  and friends are a registrar-era convention for taking a short brand when the bare
  domain is gone. The remainder must equal the name EXACTLY; a prefix match here
  would let ``usergroup`` become "Rgroup".
* ``token_subsequence`` -- the anchor is an ordered subset of the name's own words,
  beginning at the first word: ``BS&B Safety Systems`` -> ``bsbsystems``,
  ``Zwicker & Associates, P.C.`` -> ``zwickerpc``. Companies shorten their own
  domains by dropping interior words. Whole words only, so ``Technology`` can never
  become ``tech`` and an initialism can never be invented.

WHAT IS DELIBERATELY NOT HERE. No acronym/initialism rule: three- and four-letter
initialisms collide across unrelated companies and nothing observed needs one. No
edit distance, no token-overlap ratio, no similarity score -- a textual difference
does not prove two companies and a textual similarity does not prove one, so only
exact constructions count. When no derivation exists the caller is told which
evidence is missing, and the row stays held.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Registrar-era prefixes taken when the bare brand domain is unavailable. A CLOSED
#: set, and deliberately a SHORT one: the stripped remainder has to equal the
#: published name exactly, but the name is the side we are trying to corroborate, so
#: a common word-opening prefix manufactures agreement instead of finding it.
#: ``the`` is the clearest example -- it would read ``theresa.com`` as the company
#: "Resa" -- and ``go``/``hi`` are short enough to open ordinary brands, so none of
#: the three are here. What remains are prefixes that are not themselves plausible
#: openings of a brand name.
VANITY_DOMAIN_PREFIXES: Tuple[str, ...] = (
    "weare", "join", "meet", "team", "with", "try", "use", "get", "hey", "my",
)

#: Shortest brand a vanity prefix may leave behind. Together with the exact-equality
#: requirement this bounds the rule to remainders long enough to be a real brand.
_MIN_VANITY_REMAINDER = 4

#: Shortest anchor a multi-word derivation may produce. Below this an ordered
#: subsequence of common words starts colliding by chance rather than by naming.
_MIN_DERIVED_ANCHOR = 6

#: Shortest leading token that may carry a rename across two published names.
#: ``Micro Focus`` (5) must not bridge to ``Microsoft``; ``Endeavor`` (8) may.
_MIN_RENAME_TOKEN = 6

#: Bound on the subsequence search. Names longer than this are not shortened into
#: a domain in practice, and the bound keeps the walk linear in the anchor.
_MAX_SUBSEQUENCE_WORDS = 10

_LEGAL_WORDS = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "plc", "gmbh", "sa", "sarl", "bv", "llp", "lp", "co", "company", "ag", "nv",
    "pty", "spa", "srl", "oy", "ab", "as",
})


def name_words(value: Any) -> List[str]:
    """The published name as lowercase ASCII words, in order."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", text)


def name_key(value: Any, *, drop_legal: bool = False) -> str:
    words = name_words(value)
    if drop_legal:
        while words and words[-1] in _LEGAL_WORDS:
            words.pop()
    return "".join(words)


def _alnum(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


@dataclass(frozen=True)
class AnchorForm:
    """One readable spelling of a stable identifier.

    A single domain yields several: the second-level label alone, the whole domain
    including its TLD, and the label with a vanity prefix removed. They are
    alternatives, not competitors -- corroboration by any one of them is
    corroboration, because they all name the same registered domain.
    """

    form: str
    key: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnchorDerivation:
    """Why an anchor is corroborated by a name, in enough detail to re-check."""

    rule: str
    anchor_form: str
    anchor_key: str
    words_used: Tuple[str, ...] = ()
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def exact(self) -> bool:
        return self.rule in {"exact", "domain_full_label_key", "domain_leading_label"}

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "anchor_form": self.anchor_form,
                "anchor_key": self.anchor_key, "words_used": list(self.words_used),
                **({"detail": dict(self.detail)} if self.detail else {})}


def slug_anchor_forms(slug: str) -> List[AnchorForm]:
    """A LinkedIn slug has exactly one spelling; it is already the identifier."""
    key = _alnum(slug)
    return [AnchorForm("linkedin_slug", key)] if key else []


def domain_anchor_forms(domain: str) -> List[AnchorForm]:
    """Every reading of a registrable domain that could spell a company name.

    Ordered most-literal first. Duplicates are collapsed so a ``.com`` domain does
    not report its second-level label twice under two names.
    """
    text = str(domain or "").strip().lower().strip(".")
    if not text:
        return []
    labels = [label for label in text.split(".") if label]
    if not labels:
        return []
    forms: List[AnchorForm] = []
    seen: set = set()

    def add(form: str, key: str, **detail: Any) -> None:
        if key and key not in seen:
            seen.add(key)
            forms.append(AnchorForm(form, key, detail))

    add("domain_second_level", _alnum(labels[0]), label=labels[0])
    # The whole domain read as one word. This is what makes a brand TLD legible:
    # ``kai.security`` -> ``kaisecurity``. On a ``.com`` it simply adds ``com`` and
    # matches nothing, which is why no TLD allowlist is needed here.
    add("domain_full_label_key", _alnum("".join(labels)), labels=list(labels))
    lead = _alnum(labels[0])
    for prefix in VANITY_DOMAIN_PREFIXES:
        if lead.startswith(prefix) and len(lead) - len(prefix) >= _MIN_VANITY_REMAINDER:
            add("vanity_prefix_stripped", lead[len(prefix):], prefix=prefix,
                label=labels[0])
            break  # longest-first ordering makes the first strip the right one
    return forms


def _subsequence_uses(words: Sequence[str], anchor: str) -> Optional[Tuple[str, ...]]:
    """An ordered subset of ``words``, starting at ``words[0]``, spelling ``anchor``.

    Whole words only and the first word is mandatory, so a generic trailing word can
    never carry the match on its own and no letters are invented. Returns the words
    consumed, or None.
    """
    if not words or not anchor or not anchor.startswith(words[0]):
        return None
    limit = min(len(words), _MAX_SUBSEQUENCE_WORDS)

    def walk(index: int, position: int, used: Tuple[str, ...]) -> Optional[Tuple[str, ...]]:
        if position == len(anchor):
            return used
        if index >= limit:
            return None
        word = words[index]
        if anchor.startswith(word, position):
            taken = walk(index + 1, position + len(word), used + (word,))
            if taken is not None:
                return taken
        return walk(index + 1, position, used)  # skip this word

    return walk(1, len(words[0]), (words[0],))


def derive_anchor(published_name: Any, form: AnchorForm) -> Optional[AnchorDerivation]:
    """How ``form`` is constructible from ``published_name``, or None.

    Tried strongest-first. Every branch is an equality between the anchor and some
    concatenation of the name's own words -- never a similarity.
    """
    anchor = form.key
    if not anchor:
        return None
    words = name_words(published_name)
    if not words:
        return None
    trimmed = list(words)
    while trimmed and trimmed[-1] in _LEGAL_WORDS:
        trimmed.pop()
    if not trimmed:
        return None

    full = "".join(trimmed)
    # The RULE is named after the anchor FORM that matched, never after the
    # comparison. A vanity-stripped label equalling the name is still a
    # vanity-prefix derivation -- calling it "exact" because the final string
    # compare was an equality would silently promote the one rule whose input is a
    # naming convention rather than the name's own letters.
    if anchor == full or "".join(words) == anchor:
        used = tuple(trimmed) if anchor == full else tuple(words)
        detail: Dict[str, Any] = dict(form.detail)
        if anchor != full:
            detail["legal_suffix_retained"] = True
        if form.form in {"vanity_prefix_stripped", "domain_full_label_key"}:
            return AnchorDerivation(form.form, form.form, anchor, used, detail)
        # A whole leading label spelling the name: "Kai" on ``kai.security``. Exact
        # equality with a real label, so it cannot fire on a fragment.
        if form.form == "domain_second_level" and _alnum(form.detail.get("label")) == anchor:
            return AnchorDerivation("domain_leading_label", form.form, anchor, used, detail)
        return AnchorDerivation("exact", form.form, anchor, used, detail)

    if len(anchor) >= _MIN_DERIVED_ANCHOR and len(trimmed) >= 2:
        used = _subsequence_uses(trimmed, anchor)
        if used is not None and len(used) >= 2 and len(used) < len(trimmed):
            return AnchorDerivation("token_subsequence", form.form, anchor, used,
                                    {"words_available": list(trimmed)})
    return None


def best_derivation(published_name: Any,
                    forms: Sequence[AnchorForm]) -> Optional[AnchorDerivation]:
    """The strongest derivation of any spelling of one identifier."""
    found = [d for d in (derive_anchor(published_name, f) for f in forms) if d]
    if not found:
        return None
    return sorted(found, key=lambda d: (0 if d.exact else 1, -len(d.anchor_key)))[0]


def organization_attests_correspondence(org_linkedin_website: Any,
                                        employer_domain: Any) -> Dict[str, Any]:
    """Does the LinkedIn organization's OWN profile tie this slug to this domain?

    The strongest correspondence available without leaving the provider record: the
    organization states its website on the page the slug identifies. When that
    website is the employer domain we are holding, the two anchors are attested to
    belong to one organization and their spelling difference is not evidence of two.

    Deliberately an equality on the registrable domain. A subdomain, a redirect
    target we cannot see, or a merely similar host is NOT attestation.
    """
    from domain_utils import normalize_company_domain

    declared = normalize_company_domain(org_linkedin_website)
    employer = normalize_company_domain(employer_domain)
    attested = bool(declared and employer and declared == employer)
    return {"attested": attested, "linkedin_declared_website": declared,
            "employer_domain": employer,
            "basis": "linkedin_organization_profile_website" if attested else ""}


def rename_correspondence(names: Sequence[str]) -> Dict[str, Any]:
    """Do two published names look like one organization under two brands?

    Only ever consulted when EACH name is already exactly corroborated by a
    DIFFERENT stable anchor of the SAME provider record -- that is the situation a
    rebrand produces and an unrelated pairing does not. The extra bar here is that
    one name's leading token must open the other's key, so ``Endeavor Business
    Media``/``EndeavorB2B`` bridges and ``Micro Focus``/``Microsoft`` does not: the
    token has to be at least :data:`_MIN_RENAME_TOKEN` characters.
    """
    cleaned = [str(n or "").strip() for n in names if str(n or "").strip()]
    if len(cleaned) < 2:
        return {"related": False}
    for left in cleaned:
        for right in cleaned:
            if left == right:
                continue
            words = name_words(left)
            if not words or len(words[0]) < _MIN_RENAME_TOKEN:
                continue
            token = words[0]
            other = name_key(right, drop_legal=True)
            if other != token and other.startswith(token):
                return {"related": True, "shared_leading_token": token,
                        "from_name": left, "to_name": right,
                        "basis": "leading_token_of_one_name_opens_the_other"}
    return {"related": False}
