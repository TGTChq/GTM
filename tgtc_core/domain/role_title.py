"""A concrete job title taken from the employer's own posting, or nothing.

`display_role` used to fall back to the function noun whenever a posting title failed
its display-safety test, which is what produced subject lines like ``operations role``
and ``customer support role``. Measured on the 7,839 approvals exported 2026-10-03:
**691 of them (8.8%)** carried such a subject, and two of them reached the outbox on
2026-10-02 — after the empty-copy fix was live, so this was current behaviour, not
incident residue. The production gates never objected: `role_display_send_safe` tests
length, characters, appended qualifiers and headlines, and none of that refuses copy
that is complete and simply says nothing.

What this module does instead is take a title the employer actually wrote.

Guardrails, so that nothing is invented:

* every candidate is a **literal, contiguous substring** of the posting title, obtained
  by splitting on separators only. No word is added, reordered or inflected, and that
  is asserted at runtime, not assumed.
* ``/`` is deliberately NOT a separator. A slash joins alternatives inside a noun
  phrase (``Principal OT Cybersecurity / ICS Security Architect``), so cutting there
  yields a fragment rather than a title.
* a coordination truncated to its first element is rejected. Measured on
  ``Executive Director and Assistant, Associate or Professor of Medicine``, whose comma
  split produced ``Executive Director and Assistant``. The guard fires only when the
  conjunction sits immediately before the final word, so
  ``Legal Coordinator & Executive Assistant`` is untouched.
* a candidate must END in a role noun that production **already accepts** as a title.
  That vocabulary is measured, not hand-written: it is the terminal words of the 7,148
  displays `display_role` accepts today, kept at three or more uses, and it lives in
  ``data/role_title_nouns.json`` with its provenance.
* punctuation alone never disqualifies a real title. ``Senior Product Manager, Ad
  Monetization`` is refused by the old display test for its comma; here it yields
  ``Senior Product Manager``.

When no candidate survives, this returns ``""`` — and the caller refuses the lead
rather than inventing a noun. That refusal is reachable before any paid enrichment.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, Tuple

#: `/` is absent on purpose: see the module docstring. U+FFFD is present because it is
#: what a mis-decoded em dash becomes in these feeds, and it IS a delimiter there:
#: "Healthcare Associate Actuary � Provider & Value-Based Care".
_SEPARATORS = re.compile(r"\s*[,\|;:–—�\(\)\[\]\{\}]\s*|\s+-\s+|\s+•\s+")
_WORDS = re.compile(r"[A-Za-z][A-Za-z&'\.]*")
_CONJUNCTIONS = frozenset({"and", "or", "&"})

#: Words that describe a function or a vacancy rather than a person's job.
_NOT_A_TITLE = frozenset({
    "role", "position", "opening", "opportunity", "job", "vacancy", "req", "requisition",
    "career", "careers", "hiring", "posting", "listing",
})

_VOCABULARY_FILE = Path(__file__).resolve().parents[2] / "data" / "role_title_nouns.json"

#: A segment only becomes a subject line if production already accepts at least this
#: many titles ending in the same word. Measured over the 691 generic cases: a floor of
#: 1 recovers 380 but sends "Provider & Value-Based Care" (care=4) for an actuary
#: posting; a floor of 10 recovers 353 and refuses that one; 25 also loses the perfectly
#: good "Priority Initiative Leader". So 10 -- it costs 27 recoveries, and those become
#: refusals BEFORE any paid enrichment, which is the safe direction.
MIN_ACCEPTED_USES = 10


@lru_cache(maxsize=1)
def role_noun_counts() -> Dict[str, int]:
    """The measured terminal role nouns and how often production accepts each.

    The counts are the ranking signal: one posting title can yield several valid
    segments, and the one whose terminal noun production uses most is the one that
    names a person rather than a business area. Measured example:
    ``Product Operations, Senior Associate | Housing`` -- ``associate`` appears 156
    times, ``operations`` 66, so the subject becomes ``Senior Associate``.

    Empty when the file is missing or malformed, which makes this module decline
    rather than guess.
    """
    try:
        with _VOCABULARY_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    nouns = data.get("nouns") if isinstance(data, dict) else None
    if isinstance(nouns, list):                      # tolerate the earlier list form
        nouns = {n: 1 for n in nouns}
    if not isinstance(nouns, dict):
        return {}
    out: Dict[str, int] = {}
    for word, count in nouns.items():
        key = str(word).strip().lower()
        if key and key not in _NOT_A_TITLE:
            try:
                out[key] = int(count)
            except (TypeError, ValueError):
                out[key] = 1
    return out


def role_nouns() -> FrozenSet[str]:
    return frozenset(role_noun_counts())


def reads_as_a_title(text: str) -> bool:
    """Two or more words, not a function noun, ending in a role noun production already
    accepts, and not a coordination cut at its first element."""
    words = _WORDS.findall(text or "")
    if len(words) < 2:
        return False
    lowered = {w.lower() for w in words}
    if lowered <= _NOT_A_TITLE:
        return False
    if words[-1].lower() in _NOT_A_TITLE:
        return False
    if len(words) >= 3 and words[-2].lower() in _CONJUNCTIONS:
        return False
    return words[-1].lower() in role_noun_counts()


def candidates(title: str) -> Tuple[str, ...]:
    """Literal substrings of the posting title, longest first."""
    source = re.sub(r"\s+", " ", str(title or "")).strip()
    if not source:
        return ()
    seen, out = set(), []
    position = 0
    for piece in _SEPARATORS.split(source):
        piece = re.sub(r"\s+", " ", (piece or "").strip(" -–—\t"))
        if not piece or piece.lower() in seen:
            continue
        # Literal, always: a derived title that is not in the source is a bug, not a
        # fallback, so it must not reach a recipient.
        if piece not in source:
            continue
        seen.add(piece.lower())
        out.append((position, piece))
        position += 1
    # Ranked by how often production accepts a title ending in that word, then by the
    # segment's position in the posting (titles conventionally lead), then by length.
    # Longest-first was WRONG: on "Healthcare Associate Actuary - Provider & Value-Based
    # Care" it chose the business area, and on "Product Operations, Senior Associate" it
    # chose the function.
    counts = role_noun_counts()

    def rank(item):
        index, piece = item
        words = _WORDS.findall(piece)
        weight = counts.get(words[-1].lower(), 0) if words else 0
        return (-weight, index, -len(piece))

    out.sort(key=rank)
    return tuple(piece for _, piece in out)


def concrete_title(title: str, accepts) -> str:
    """The employer's own words for the job, or ``""``.

    ``accepts`` is the caller's own display gate, passed in so this module never
    restates it: whatever approval and delivery already require of a display is
    required of the derived one too.
    """
    counts = role_noun_counts()
    for candidate in candidates(title):
        if not reads_as_a_title(candidate):
            continue
        words = _WORDS.findall(candidate)
        if counts.get(words[-1].lower(), 0) < MIN_ACCEPTED_USES:
            continue
        if accepts(candidate):
            return candidate
    return ""


__all__ = ["concrete_title", "candidates", "reads_as_a_title", "role_nouns",
           "role_noun_counts", "MIN_ACCEPTED_USES"]
