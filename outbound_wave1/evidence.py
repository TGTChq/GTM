"""Reading Role Focus / Focus Evidence safely.

Two different things are stored on every row and they must not be confused:

``Role Focus``
    A canonical, controlled noun-phrase fragment built by ``role_focus.py``. It
    is designed for insertion into outbound copy and is safe to render.

``Focus Evidence``
    The matched JD evidence that LICENSED that fragment. It is matched evidence,
    not necessarily a verbatim JD span, and it is never rendered. Its only job
    here is to answer "how many usable evidence items does this row actually
    have?", which is what decides whether an evidence-led T1 template may run.

When ``Focus Quality`` is ``manual_required`` the fragment came from a role-level
fallback (``fallback_from_role:<role>``) rather than from the posting. That is
zero usable evidence, and every evidence-led template degrades.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

#: A rendered email may never carry more than three focus items.
MAX_FOCUS_ITEMS_IN_EMAIL = 3

_FALLBACK_PREFIX = "fallback_from_role:"
_SPLIT_RE = re.compile(r",\s*and\s+|,\s*|\s+and\s+", re.I)


def _clean(value: str) -> str:
    return " ".join(str(value or "").strip(" ,.;•-").split())


@dataclass(frozen=True)
class FocusEvidence:
    """What one row's stored focus evidence supports."""

    #: Canonical, renderable phrases parsed out of ``Role Focus``.
    focus_items: Tuple[str, ...]
    #: Stored evidence entries that genuinely came from the posting.
    usable_items: Tuple[str, ...]
    #: Raw stored entries, including any fallback marker.
    raw_items: Tuple[str, ...]
    quality: str
    fallback_only: bool

    @property
    def usable_count(self) -> int:
        return len(self.usable_items)

    @property
    def is_specific(self) -> bool:
        return self.quality == "specific" and not self.fallback_only

    def renderable(self, limit: int = MAX_FOCUS_ITEMS_IN_EMAIL) -> Tuple[str, ...]:
        """Focus phrases licensed for rendering.

        Capped by BOTH the number of usable evidence items and the hard
        three-item ceiling, so copy can never imply more evidence than the row
        actually carries.
        """
        if not self.is_specific:
            return ()
        allowed = min(self.usable_count, int(limit), MAX_FOCUS_ITEMS_IN_EMAIL)
        return tuple(self.focus_items[:allowed]) if allowed > 0 else ()


def split_focus_text(role_focus: str) -> Tuple[str, ...]:
    """Split a rendered Role Focus fragment back into its phrases."""
    text = _clean(role_focus)
    if not text:
        return ()
    parts = [_clean(part) for part in _SPLIT_RE.split(text)]
    seen: set[str] = set()
    ordered: List[str] = []
    for part in parts:
        key = part.casefold()
        if part and key not in seen:
            seen.add(key)
            ordered.append(part)
    return tuple(ordered)


def read_focus_evidence(fields: Dict) -> FocusEvidence:
    """Parse ``Role Focus`` / ``Focus Evidence`` / ``Focus Quality`` off a row."""
    quality = str(fields.get("Focus Quality") or "").strip().lower()
    raw_field = str(fields.get("Focus Evidence") or "")
    raw_items = tuple(
        item for item in (_clean(part) for part in raw_field.split("|")) if item
    )
    fallback_only = bool(raw_items) and all(
        item.lower().startswith(_FALLBACK_PREFIX) for item in raw_items
    )
    usable = tuple(
        item for item in raw_items if not item.lower().startswith(_FALLBACK_PREFIX)
    )
    if quality != "specific":
        # A row that is not marked specific never licenses an evidence-led claim,
        # even if it happens to carry evidence strings.
        usable = ()
    return FocusEvidence(
        focus_items=split_focus_text(fields.get("Role Focus")),
        usable_items=usable,
        raw_items=raw_items,
        quality=quality,
        fallback_only=fallback_only,
    )


def render_evidence_list(items: Tuple[str, ...]) -> str:
    """Grammatical English list from however many items actually exist.

    ``()`` -> ``""``; one item stays bare; two are joined with "and"; three or
    more take the serial comma. Nothing is padded to reach a fixed shape.
    """
    clean = [item for item in (_clean(i) for i in items) if item]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return f"{', '.join(clean[:-1])}, and {clean[-1]}"
