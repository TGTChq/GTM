"""Deterministic conversational outbound job-title resolution.

``role-display/2`` treats the classified role as a semantic anchor, not as a
replacement for the source title. A visible/alias match is required before the
resolver reduces a title to that anchor. Canonical title, classification, Role
Focus, and other job evidence are read-only inputs.

The resolver deliberately fails closed. Competing role heads or a material
classification/title disagreement return an ambiguous, held result instead of
an outreach guess.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable


RESOLVER_VERSION = "role-display/2"

_ROLE_HEAD_RE = re.compile(
    r"\b(?:accountant|administrator|advisor|advocate|analyst|architect|assistant|"
    r"associate|bookkeeper|broker|consultant|coordinator|copywriter|designer|"
    r"counsel|developer|director|driver|editor|engineer|executive|expert|generalist|"
    r"head|lead|leader|liaison|manager|marketer|officer|partner|producer|recruiter|representative|"
    r"scientist|specialist|strategist|technician|therapist|writer)\b",
    re.I,
)

_SENIORITY_MODIFIERS = (
    ("founding", "Founding"),
    ("head of", "Head of"),
    ("entry level", "Junior"),
    ("head", "Head"),
    ("principal", "Principal"),
    ("staff", "Staff"),
    ("executive", "Executive"),
    ("senior", "Senior"),
    ("junior", "Junior"),
    ("associate", "Associate"),
    ("assistant", "Assistant"),
    ("lead", "Lead"),
)

_CONVENTIONAL_MODIFIERS = (
    ("inside sales", "Inside Sales"),
    ("outside sales", "Outside Sales"),
    ("mid market", "Mid-Market"),
    ("small business", "SMB"),
    ("enterprise", "Enterprise"),
    ("technical", "Technical"),
    ("strategic", "Strategic"),
    ("commercial", "Commercial"),
    ("national", "National"),
    ("regional", "Regional"),
    ("channel", "Channel"),
    ("partner", "Partner"),
    ("key", "Key"),
    ("smb", "SMB"),
    ("inside", "Inside"),
)

_ROLE_SPECIFIC_MODIFIERS = {
    "accountant": (
        ("accounts payable", "Accounts Payable"),
        ("accounts receivable", "Accounts Receivable"),
        ("general ledger", "General Ledger"),
        ("property", "Property"),
        ("revenue", "Revenue"),
        ("cost", "Cost"),
        ("tax", "Tax"),
        ("gl", "GL"),
        ("ap", "AP"),
        ("ar", "AR"),
    ),
    "bookkeeper": (("full charge", "Full Charge"),),
    "software engineer": (
        ("full stack", "Full-Stack"),
        ("frontend", "Frontend"),
        ("backend", "Backend"),
    ),
    "developer": (
        ("full stack", "Full-Stack"),
        ("frontend", "Frontend"),
        ("backend", "Backend"),
    ),
}

# Displays produced by role-specific modifiers. These qualify the role head
# itself (Frontend Developer, Cost Accountant) and therefore belong immediately
# before the head rather than at the very front of a title.
_ROLE_SPECIFIC_DISPLAYS = frozenset(
    display for definitions in _ROLE_SPECIFIC_MODIFIERS.values() for _, display in definitions
)

_KEY_ALIASES = (
    (r"\bap\b", "accounts payable"),
    (r"\bar\b", "accounts receivable"),
    (r"\bbdr\b", "business development representative"),
    (r"\bsdr\b", "sales development representative"),
    (r"\btam\b", "technical account manager"),
    (r"\bcsm\b", "customer success manager"),
    (r"\bclient success\b", "customer success"),
    (r"\bgtm\b", "go to market"),
    (r"\bqa\b", "quality assurance"),
    (r"\bseo\b", "search engine optimization"),
    (r"\bsmb\b", "small business"),
)

_WORK_RE = re.compile(
    r"\b(?:remote|hybrid|on[ -]?site|work from home|wfh|remote[- ]eligible|"
    r"full[ -]?time|part[ -]?time|contract|temporary|temp|fee for service|"
    r"shift|working hours|flexible hours|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)\b",
    re.I,
)
_LOCATION_RE = re.compile(
    r"\b(?:u\.?s\.?a?|united states|north america|southwest|southeast|"
    r"west coast|sf bay area|latam|europe|americas|nyc|atl|canada|india|"
    r"australia|united kingdom|uk|germany|france|spain|mexico|brazil|"
    r"denmark|norway|est|edt|cst|cdt|mst|mdt|pst|pdt|"
    r"north(?:ern)? region|south(?:ern)? region|east(?:ern)? region|"
    r"west(?:ern)? region|central region)\b|"
    r"\b[A-Za-z][A-Za-z .'-]+,\s*[A-Z]{2}\b|"
    r"\b[A-Z]{2}\s+territory\b",
    re.I,
)
_SALARY_RE = re.compile(
    r"(?:USD\s*)?\$\s*\d[\d,.]*(?:\.\d+)?\s*[kKmM]?\+?"
    r"(?:\s*(?:-|–|—|to)\s*(?:USD\s*)?\$?\s*\d[\d,.]*(?:\.\d+)?\s*[kKmM]?\+?)?"
    r"|\b\d{2,3}\s*[kK]\b|\b(?:ote|commission|stock options|flexible pto|"
    r"great benefits|great pay|compensation package|growth & pto)\b|\bp/?y\b",
    re.I,
)
_PROMO_RE = re.compile(
    r"\b(?:is hiring|now hiring|apply now|job at|opportunit(?:y|ies)|"
    r"join our team|growing manufacturer|high[ -]?growth|vc backed|career growth|"
    r"no third parties|locals only|team expansion|immediate opening)\b",
    re.I,
)
_ATS_RE = re.compile(
    r"(?:^|\s)(?:req(?:uisition)?|job)\s*(?:id|#)?\s*[:#-]?\s*[a-z0-9-]+|"
    r"^\d{4,}\s*[-–—]|#\d{3,}|\bcareers\b|\s-\s\d{5,}$",
    re.I,
)
_LANGUAGE_RE = re.compile(
    r"\b(?:bilingual|multilingual|spanish|chinese|french|german)\b", re.I
)
_REDUNDANT_RE = re.compile(r"\b(?:roles|jobs|openings|opportunities)\b", re.I)

_FUNCTION_GROUPS = {
    "sales": re.compile(
        r"\b(?:account executive|account manager|sales|business development|"
        r"partnerships?|sdr|bdr|revenue)\b",
        re.I,
    ),
    "marketing": re.compile(
        r"\b(?:marketing|media|seo|search engine optimization|content|campaign|"
        r"ecommerce|growth|advertising|ppc)\b",
        re.I,
    ),
    "engineering": re.compile(
        r"\b(?:engineer|developer|software|data scientist|machine learning|qa)\b",
        re.I,
    ),
    "finance": re.compile(
        r"\b(?:accountant|accounting|financial|finance|billing|payroll|bookkeeper|"
        r"underwriting|accounts payable|accounts receivable|fp&a)\b",
        re.I,
    ),
    "people": re.compile(
        r"\b(?:recruiter|recruiting|talent|human resources|hr|people|compensation)\b",
        re.I,
    ),
    "customer": re.compile(
        r"\b(?:customer success|client success|customer support|customer care|onboarding|implementation)\b",
        re.I,
    ),
    "operations": re.compile(
        r"\b(?:operations|automation|crm|gtm|go to market|enablement|systems|revops)\b",
        re.I,
    ),
    "creative": re.compile(
        r"\b(?:designer|design|copywriter|writer|producer|editor|creative)\b",
        re.I,
    ),
}


@dataclass(frozen=True)
class RoleDisplayResult:
    name: str
    confidence: str
    changed: bool
    hold: bool
    ambiguous: bool
    evidence: Dict[str, Any]
    resolver_version: str = RESOLVER_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[‐‑‒–—―−]", "-", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_abbreviations(value: Any) -> str:
    text = _text(value)
    text = re.sub(r"\bSr\.?(?=\s|$)", "Senior", text, flags=re.I)
    text = re.sub(r"\bJr\.?(?=\s|$)", "Junior", text, flags=re.I)
    text = re.sub(r"\bMid[ -]?Market\b", "Mid-Market", text, flags=re.I)
    text = re.sub(r"\bFull[ -]?Stack\b", "Full-Stack", text, flags=re.I)
    text = re.sub(r"\bFront[ -]?End\b", "Frontend", text, flags=re.I)
    text = re.sub(r"\bBack[ -]?End\b", "Backend", text, flags=re.I)
    text = re.sub(r"\bCopy\s+Writer\b", "Copywriter", text, flags=re.I)
    text = re.sub(r"\bRep\.?\b", "Representative", text, flags=re.I)
    text = re.sub(r"(?i)(?<=[A-Za-z])\s+ll\b", " II", text)
    return _text(text)


def _collapse_serialized_duplicate(value: Any) -> tuple[str, list[str]]:
    text = _text(value)
    if not text.startswith("["):
        return _normalize_abbreviations(text), []
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return _normalize_abbreviations(text), []
    if not isinstance(payload, list):
        return _normalize_abbreviations(text), []
    values = [_normalize_abbreviations(item) for item in payload if isinstance(item, str) and _text(item)]
    if values and len({_key(item) for item in values}) == 1:
        return values[0], ["serialized_duplicate_title_collapsed"]
    return _normalize_abbreviations(text), []


def _key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _normalize_abbreviations(_text(value)))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"\be[ -]?commerce\b", "ecommerce", text)
    text = re.sub(r"\bfp\s*&\s*a\b", "fpa", text)
    text = re.sub(r"\bmid[ -]?market\b", "mid market", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _alias_key(value: Any) -> str:
    text = f" {_key(value)} "
    for pattern, replacement in _KEY_ALIASES:
        text = re.sub(pattern, replacement, text)
    return " ".join(text.split())


def _tokens(value: Any) -> set[str]:
    return {
        token for token in _alias_key(value).split()
        if token not in {"a", "an", "and", "of", "the", "to"}
    }


def _token_overlap(title: str, matched_role: str) -> float:
    wanted = _tokens(matched_role)
    return len(wanted & _tokens(title)) / len(wanted) if wanted else 0.0


def _find_anchor(title: str, matched_role: str) -> tuple[int, int]:
    if not matched_role:
        return -1, -1
    title_key = _alias_key(title)
    anchor_key = _alias_key(matched_role)
    match = re.search(rf"(?:^| )({re.escape(anchor_key)})(?: |$)", title_key)
    if not match:
        return -1, -1
    return match.start(1), match.end(1)


def _role_heads(value: str) -> list[str]:
    # These phrases contain words that can also be job heads, but here they are
    # conventional business/title qualifiers rather than a competing position.
    text = re.sub(r"\blead generation\b", "generation", value, flags=re.I)
    text = re.sub(r"\bthought leader\b", "thought", text, flags=re.I)
    return [match.group(0).lower() for match in _ROLE_HEAD_RE.finditer(text)]


def _has_delimited_competing_heads(title: str) -> bool:
    for parts in (
        re.split(r"\s*[/|]\s*", title),
        re.split(r"\s+(?:and|&)\s+", title, flags=re.I),
    ):
        meaningful = [part for part in parts if _role_heads(part)]
        if len(meaningful) >= 2:
            return True
    outer = re.sub(r"\([^)]*\)", "", title)
    if _role_heads(outer):
        for inner in re.findall(r"\(([^)]*)\)", title):
            if re.fullmatch(
                r"\s*(?:associate|senior|junior|entry)[ -]?level\s*",
                inner,
                re.I,
            ):
                continue
            if _role_heads(inner):
                return True
    comma_parts = [part for part in re.split(r"\s*,\s*", title) if part]
    return len([part for part in comma_parts if _role_heads(part)]) >= 2


def _anchor_is_secondary_or_conflicting(title: str, matched_role: str) -> bool:
    start, end = _find_anchor(title, matched_role)
    if start < 0:
        return False
    key = _alias_key(title)
    before = key[:start].strip()
    after = key[end:].strip()
    anchor_heads = _role_heads(_alias_key(matched_role))

    # Remove conventional modifiers before testing for an independent role
    # head. Associate/Assistant/Executive can be nouns elsewhere, but directly
    # adjacent to the corroborated anchor they are conventional modifiers.
    for phrase, _ in (*_SENIORITY_MODIFIERS, *_CONVENTIONAL_MODIFIERS):
        before = re.sub(rf"(?:^| ){re.escape(_key(phrase))}(?: |$)", " ", before)
    before = " ".join(before.split())

    # Parenthetical acronym restatements normalize to the same alias key, for
    # example Business Development Representative (BDR).
    anchor_key = _alias_key(matched_role)
    if after == anchor_key or after.startswith(f"{anchor_key} "):
        after = after[len(anchor_key):].strip()
    after = re.sub(
        r"^(?:associate|senior|junior|entry) level(?: |$)", "", after
    ).strip()

    # A broader function such as "Customer Support" may legitimately be
    # followed by a single conventional role head: Customer Support Specialist.
    if not anchor_heads:
        extension = re.match(
            r"^(?:specialist|representative|associate|advisor|manager|coordinator)(?: |$)",
            after,
        )
        if extension:
            after = after[extension.end():].strip()

    # A short adjacent noun modifier (Driver Recruiter) describes the hiring
    # specialization. A longer phrase containing another role head is unsafe.
    before_heads = _role_heads(before)
    if before_heads and not re.search(r"\s[-,()/|]\s|\(|\)", title):
        if len(before.split()) <= 1:
            before = ""

    return bool(_role_heads(before) or _role_heads(after))


def _structured_values(job: Dict[str, Any], keys: Iterable[str]) -> set[str]:
    values: set[str] = set()
    for key in keys:
        text = _key(job.get(key))
        if not text:
            continue
        values.add(text)
        for part in re.split(r"\s*[,|/]\s*", text):
            if len(part) >= 2:
                values.add(part)
    return values


def _context_categories(title: str, job: Dict[str, Any]) -> list[str]:
    categories: set[str] = set()
    if _WORK_RE.search(title):
        categories.add("work_or_employment_metadata")
    if _LOCATION_RE.search(title):
        categories.add("location_or_territory")
    if _SALARY_RE.search(title):
        categories.add("salary_or_benefits")
    if _PROMO_RE.search(title):
        categories.add("promotional_hiring_copy")
    if _ATS_RE.search(title):
        categories.add("ats_source_or_requisition")
    if _LANGUAGE_RE.search(title):
        categories.add("language_requirement")
    if _REDUNDANT_RE.search(title):
        categories.add("redundant_posting_words")

    locations = _structured_values(job, (
        "canonical_location", "job_location", "_normalized_location", "location", "Location",
    ))
    title_key = _key(title)
    if any(value and value in title_key for value in locations):
        categories.add("location_or_territory")
    if re.search(r"\s[-,|:]\s*\S|\([^)]{3,}\)", title):
        categories.add("contextual_posting_qualifier")
    return sorted(categories)


def _segment_keys(title: str) -> list[str]:
    return [
        _key(part) for part in re.split(r"[,()|;/]|\s+-\s+", title)
        if _key(part)
    ]


def _phrase_in_isolated_segment(title: str, phrase: str) -> bool:
    phrase_key = _key(phrase)
    allowed = {
        phrase_key,
        f"{phrase_key} market",
        f"{phrase_key} markets",
        f"{phrase_key} sales",
    }
    return any(segment in allowed for segment in _segment_keys(title))


def _modifier_positions(prefix: str, definitions: Iterable[tuple[str, str]]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    prefix_key = _key(prefix)
    for phrase, display in definitions:
        match = re.search(rf"(?:^| )({re.escape(_key(phrase))})(?: |$)", prefix_key)
        if match:
            found.append((match.start(1), display))
    return found


def _render_from_anchor(title: str, matched_role: str) -> tuple[str, list[str]]:
    title_key = _alias_key(title)
    anchor_key = _alias_key(matched_role)
    anchor_start = title_key.find(anchor_key)
    prefix = title_key[:anchor_start].strip() if anchor_start >= 0 else ""
    modifiers = _modifier_positions(prefix, _SENIORITY_MODIFIERS)
    modifiers.extend(_modifier_positions(prefix, _CONVENTIONAL_MODIFIERS))

    for phrase, display in _CONVENTIONAL_MODIFIERS:
        if _key(phrase) not in _key(matched_role) and _phrase_in_isolated_segment(title, phrase):
            modifiers.append((len(prefix) + 1, display))

    base_key = _key(matched_role)
    specific_key = next(
        (key for key in _ROLE_SPECIFIC_MODIFIERS if base_key == key or base_key.endswith(f" {key}")),
        "",
    )
    if specific_key:
        for phrase, display in _ROLE_SPECIFIC_MODIFIERS[specific_key]:
            if _key(phrase) in base_key:
                continue
            if re.search(rf"(?:^| ){re.escape(_key(phrase))}(?: |$)", _key(prefix)) or _phrase_in_isolated_segment(title, phrase):
                modifiers.append((len(prefix), display))

    ordered: list[str] = []
    for _, display in sorted(modifiers, key=lambda item: item[0]):
        if _key(display) not in _key(matched_role) and display not in ordered:
            ordered.append(display)
    if "Head of" in ordered:
        ordered = [item for item in ordered if item != "Head"]
    if "Inside Sales" in ordered:
        ordered = [item for item in ordered if item != "Inside"]

    # "Founding & Lead Recruiter Roles" describes alternative openings. The
    # modifier nearest the corroborated role head is the conversational choice.
    if re.search(r"\bfounding\s*(?:&|and)\s*lead\b", title, re.I):
        ordered = [item for item in ordered if item != "Founding"]

    display_anchor = _normalize_abbreviations(matched_role)
    result = " ".join(ordered + [display_anchor])

    # Preserve a conventional role head when matched_role is a broader
    # functional label (Customer Support -> Customer Support Specialist).
    _, end = _find_anchor(title, matched_role)
    remainder = _alias_key(title)[end:].strip() if end >= 0 else ""
    extension = re.match(
        r"^(specialist|representative|associate|advisor|manager|coordinator)(?: |$)",
        remainder,
    )
    if not _role_heads(matched_role) and extension:
        head = extension.group(1).title()
        if not _key(result).endswith(_key(head)):
            result = f"{result} {head}"

    level = re.search(
        rf"(?:^| ){re.escape(anchor_key)}\s+(i|ii|iii|iv|[1-4])(?: |$)", title_key
    )
    if level and not _key(result).endswith(level.group(1)):
        suffix = level.group(1).upper() if not level.group(1).isdigit() else level.group(1)
        result = f"{result} {suffix}"
    return _normalize_abbreviations(result), ordered


def _fallback_promoted_modifiers(title: str) -> list[str]:
    """Return only whitelisted modifiers isolated from the role phrase."""
    found: list[tuple[int, str]] = []
    for phrase, display in (*_SENIORITY_MODIFIERS, *_CONVENTIONAL_MODIFIERS):
        if _phrase_in_isolated_segment(title, phrase):
            found.append((_key(title).find(_key(phrase)), display))
    title_key = _key(title)
    for role_key, definitions in _ROLE_SPECIFIC_MODIFIERS.items():
        if role_key not in title_key:
            continue
        for phrase, display in definitions:
            if _phrase_in_isolated_segment(title, phrase):
                found.append((title_key.find(_key(phrase)), display))
    ordered: list[str] = []
    for _, display in sorted(found, key=lambda item: item[0]):
        if display not in ordered:
            ordered.append(display)
    if "Head of" in ordered:
        ordered = [item for item in ordered if item != "Head"]
    return ordered


def _self_sufficient_role_prefix(value: str) -> bool:
    """Whether ``value`` already names a role without needing a suffix."""
    text = _normalize_abbreviations(value).strip(" -|,;:")
    if not text or _has_delimited_competing_heads(text):
        return False
    if re.match(r"^(?:founding\s+)?head of\s+\S+", text, re.I):
        return True
    heads = list(_ROLE_HEAD_RE.finditer(text))
    if not heads:
        return False
    final = heads[-1]
    prefix_tokens = set(_key(text[:final.start()]).split())
    generic = {
        "founding", "principal", "staff", "executive", "senior", "junior",
        "associate", "assistant", "lead", "head", "of", "the", "a", "an",
        "global", "entry", "level",
    }
    meaningful_prefix = prefix_tokens - generic
    if meaningful_prefix:
        return True
    return final.group(0).lower() in {
        "accountant", "bookkeeper", "broker", "counsel", "recruiter",
        "therapist", "writer", "copywriter",
    }


def _insert_before_final_head(text: str, display: str) -> str:
    """Place a role-specific modifier immediately before the final role head.

    Seniority/conventional modifiers front the whole title, but a role-specific
    qualifier attaches to the head it describes (Senior + Frontend + Developer),
    so it must sit after any preserved seniority word rather than at position 0.
    """
    heads = list(_ROLE_HEAD_RE.finditer(text))
    if not heads:
        return f"{display} {text}"
    final = heads[-1]
    return f"{text[:final.start()]}{display} {text[final.start():]}".strip()


def _strip_obvious_metadata(title: str, job: Dict[str, Any], rules: list[str]) -> str:
    text = title
    promoted_modifiers = _fallback_promoted_modifiers(title)
    substitutions = (
        (r"^(?:latam|u\.?s\.?a?|north america|americas|canada|europe)\s*[-:]?\s+", "", "leading_location_removed"),
        (r"^\[?\s*(?:remote|hybrid|on[ -]?site)\s*\]?\s+", "", "leading_work_arrangement_removed"),
        (r"^(?:part[ -]?time|full[ -]?time|bilingual|temporary|temp)\s+", "", "leading_posting_metadata_removed"),
        (r"^\d{4,}\s*[-:]\s*", "", "ats_prefix_removed"),
        (r"^.+?\bis hiring\s*:\s*", "", "hiring_prefix_removed"),
        (r"\s+job at\s+.+$", "", "ats_source_suffix_removed"),
        (r"\s+-\s+(?:careers\s+-\s+.+|now hiring|no third parties|locals only)\s*$", "", "promotional_suffix_removed"),
        (r"\s*#\d{3,}\s*$", "", "requisition_removed"),
    )
    changed = True
    while changed:
        changed = False
        for pattern, replacement, rule in substitutions:
            updated = re.sub(pattern, replacement, text, flags=re.I)
            if updated != text:
                text = updated
                if rule not in rules:
                    rules.append(rule)
                changed = True

    for_context = re.match(r"^(?P<role>.+?\b)\s+for\s+.+$", text, re.I)
    if (
        for_context
        and _role_heads(for_context.group("role"))
        and not re.search(r"\bfee\s*$", for_context.group("role"), re.I)
    ):
        text = for_context.group("role")
        rules.append("for_clause_posting_context_removed")

    # Other non-role parentheticals are posting context when the outer text is
    # already a recognizable single position.
    def drop_parenthetical(match: re.Match[str]) -> str:
        inner = match.group(1)
        if _role_heads(inner):
            return match.group(0)
        rules.append("parenthetical_posting_metadata_removed")
        return ""

    if _role_heads(re.sub(r"\([^)]*\)", "", text)):
        text = re.sub(r"\(([^()]*)\)", drop_parenthetical, text)
        updated = re.sub(
            r"\[([^]]*(?:remote|hybrid|on[ -]?site|shift|monday|tuesday|"
            r"wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)[^]]*)\]",
            "",
            text,
            flags=re.I,
        )
        if updated != text:
            text = updated
            rules.append("bracketed_posting_metadata_removed")

    trailing = re.match(r"^(?P<role>.+?)\s+-\s+(?P<tail>[^|]+)$", text)
    if trailing and _role_heads(trailing.group("role")):
        tail = trailing.group("tail").strip()
        location_values = _structured_values(job, (
            "canonical_location", "job_location", "_normalized_location", "location", "Location",
        ))
        if (
            _WORK_RE.search(tail)
            or _LOCATION_RE.search(tail)
            or _SALARY_RE.search(tail)
            or _PROMO_RE.search(tail)
            or _key(tail) in location_values
        ):
            text = trailing.group("role")
            rules.append("trailing_posting_metadata_removed")

    # Without an exact anchor, remove only delimited suffixes positively
    # identified as structured/noise context.
    parts = [part.strip() for part in re.split(r"\s*[|]\s*", text)]
    if len(parts) > 1 and _role_heads(parts[0]):
        kept = [parts[0]]
        for part in parts[1:]:
            if _WORK_RE.search(part) or _LOCATION_RE.search(part) or _SALARY_RE.search(part) or _PROMO_RE.search(part):
                rules.append("delimited_posting_context_removed")
            else:
                kept.append(part)
        text = " | ".join(kept)

    # A comma/dash/colon suffix is context only when the left side is already
    # a self-sufficient role. This avoids blindly splitting generic titles such
    # as "Manager, Research Ethics and Compliance".
    contextual = re.match(
        r"^(?P<role>.+?)(?:\s+-\s+|,\s+|:\s+)(?P<tail>.+)$", text
    )
    if contextual and _self_sufficient_role_prefix(contextual.group("role")):
        text = contextual.group("role")
        rules.append("delimited_role_context_removed")

    # When location evidence occurs after a complete role without a delimiter,
    # everything after that role head is posting context. This covers titles
    # such as "Sales Representative Acute Care ... Denmark & Norway" without
    # maintaining a list of product/industry examples.
    head_matches = list(_ROLE_HEAD_RE.finditer(text))
    if head_matches:
        final_head = head_matches[-1]
        level = re.match(r"\s+(?:I|II|III|IV|[1-4])\b", text[final_head.end():], re.I)
        role_end = final_head.end() + (level.end() if level else 0)
        role_prefix = text[:role_end]
        trailing_context = text[role_end:]
        if (
            trailing_context.strip()
            and _LOCATION_RE.search(trailing_context)
            and _self_sufficient_role_prefix(role_prefix)
        ):
            text = role_prefix
            rules.append("trailing_location_context_removed")

    before_salary = text
    text = _SALARY_RE.sub("", text)
    if text != before_salary:
        rules.append("salary_removed")
    for display in reversed(promoted_modifiers):
        if _key(display) not in _key(text):
            if display in _ROLE_SPECIFIC_DISPLAYS:
                text = _insert_before_final_head(text, display)
            else:
                text = f"{display} {text}"
            rules.append("isolated_conventional_modifier_preserved")
    return _normalize_abbreviations(text).strip(" -|,;:")


def _functional_groups(value: str) -> set[str]:
    key = _alias_key(value)
    return {
        name for name, pattern in _FUNCTION_GROUPS.items()
        if pattern.search(key)
    }


def _natural_single_title(title: str) -> bool:
    if not title or _has_delimited_competing_heads(title):
        return False
    heads = _role_heads(title)
    if len(set(heads)) > 1:
        # Associate/Assistant/Executive/Partner are conventional modifiers when
        # they precede the final role head without a role-separating delimiter.
        # This keeps titles such as "Senior Associate Software Engineer" and
        # "Partner Relationship Manager" from becoming false ambiguities.
        key = _alias_key(title)
        final_head = heads[-1]
        final_start = key.rfind(final_head)
        preceding_heads = heads[:-1]
        modifier_heads = {"associate", "assistant", "executive", "partner"}
        if (
            not set(preceding_heads).issubset(modifier_heads)
            or re.search(r"\s(?:/|\||,|&|and)\s", key[:final_start], re.I)
        ):
            return False
    if not heads and not re.search(r"\b(?:customer support|sales|operations)\b", title, re.I):
        return False
    return True


def _result(
    *,
    name: str,
    raw_source: str,
    confidence: str,
    hold: bool,
    status: str,
    evidence: Dict[str, Any],
) -> RoleDisplayResult:
    normalized_name = _normalize_abbreviations(name)
    evidence = {
        **evidence,
        "status": status,
        "hold": hold,
        "resolver_version": RESOLVER_VERSION,
    }
    return RoleDisplayResult(
        name=normalized_name,
        confidence=confidence,
        changed=normalized_name != _text(raw_source),
        hold=hold,
        ambiguous=hold,
        evidence=evidence,
    )


_CATALOG_VERBATIM_CACHE: Dict[str, str] = {}


def catalog_role_verbatim(title: str, matched_role: str) -> str:
    """Return the catalog role exactly as it appears in ``title``, else "".

    The competing-head guards exist to stop the resolver PICKING a head when a title
    contains several. They do not apply when we are not picking: the catalog
    ``Matched Role`` is the role this row was already qualified, bucketed and
    HM-searched on. If that exact phrase sits in the posting title on token
    boundaries, using it is extraction, not choice -- nothing is invented and no
    responsibility is asserted that the title does not already state.
    """
    raw = _text(title)
    role = _text(matched_role)
    if not raw or not role:
        return ""
    match = re.search(
        r"(?<![A-Za-z0-9])" + re.escape(role) + r"(?![A-Za-z0-9])", raw, flags=re.I)
    return raw[match.start():match.end()] if match else ""


def resolve_role_display(job: Dict[str, Any]) -> RoleDisplayResult:
    """Resolve one concise outbound title without mutating ``job``."""
    raw_source = _text(job.get("canonical_job_title") or job.get("job_title"))
    raw, initial_rules = _collapse_serialized_duplicate(raw_source)
    matched_role = _normalize_abbreviations(job.get("_matched_role") or job.get("matched_role"))
    role_focus = _text(job.get("role_focus") or job.get("Role Focus"))
    categories = _context_categories(raw, job) if raw else []
    evidence: Dict[str, Any] = {
        "rules": list(initial_rules),
        "raw_title": raw_source,
        "normalized_title": raw,
        "matched_role": matched_role,
        "role_focus": role_focus,
        "anchor_corroborated": False,
        "preserved_modifiers": [],
        "context_categories": categories,
    }

    if not raw:
        evidence["rules"].append("missing_title")
        return _result(
            name="", raw_source=raw_source, confidence="low", hold=True,
            status="AMBIGUOUS", evidence=evidence,
        )

    if _has_delimited_competing_heads(raw):
        # INVARIANT (do not relax): a delimiter-separated title may be advertising two
        # DISTINCT roles, and naming one of them misrepresents the opening. The current
        # ontology cannot separate a qualifier+role ("Sales Associate (Account
        # Executive)") from two different roles ("Customer Care Associate (Business
        # Development Representative)") -- _role_heads returns bare nouns and
        # _functional_groups is too sparse to discriminate. Until it can, these hold.
        evidence["rules"].append("competing_role_heads")
        return _result(
            name=raw, raw_source=raw_source, confidence="low", hold=True,
            status="AMBIGUOUS", evidence=evidence,
        )

    anchor_start, _ = _find_anchor(raw, matched_role)
    if matched_role and anchor_start >= 0:
        if _anchor_is_secondary_or_conflicting(raw, matched_role):
            evidence["rules"].append("matched_role_secondary_or_competing_head")
            verbatim = catalog_role_verbatim(raw, matched_role)
            if verbatim:
                evidence["rules"].append("catalog_role_extracted_verbatim_from_title")
                evidence["anchor_corroborated"] = True
                evidence["anchor_method"] = "catalog_role_verbatim"
                return _result(
                    name=verbatim, raw_source=raw_source, confidence="medium", hold=False,
                    status="RESOLVED", evidence=evidence,
                )
            return _result(
                name=raw, raw_source=raw_source, confidence="low", hold=True,
                status="AMBIGUOUS", evidence=evidence,
            )
        name, modifiers = _render_from_anchor(raw, matched_role)
        evidence["anchor_corroborated"] = True
        evidence["anchor_method"] = "visible_alias_match"
        evidence["preserved_modifiers"] = modifiers
        if name != raw:
            evidence["rules"].append("rendered_from_corroborated_role_anchor")
            if categories:
                evidence["rules"].append("posting_context_excluded")
            if role_focus:
                evidence["rules"].append("context_retained_in_role_focus")
        else:
            evidence["rules"].append("already_conversational")
        return _result(
            name=name, raw_source=raw_source, confidence="high", hold=False,
            status="RESOLVED" if name != raw else "CLEAN", evidence=evidence,
        )

    cleanup_rules: list[str] = []
    cleaned = _strip_obvious_metadata(raw, job, cleanup_rules)
    evidence["rules"].extend(cleanup_rules)
    if not matched_role:
        evidence["anchor_method"] = "missing_matched_role"
        evidence["rules"].append("natural_title_used_without_matched_role")
        if _natural_single_title(cleaned):
            return _result(
                name=cleaned, raw_source=raw_source, confidence="medium", hold=False,
                status="RESOLVED" if cleaned != raw else "CLEAN", evidence=evidence,
            )
        evidence["rules"].append("no_safe_single_role_head")
        return _result(
            name=cleaned or raw, raw_source=raw_source, confidence="low", hold=True,
            status="AMBIGUOUS", evidence=evidence,
        )

    overlap = _token_overlap(cleaned, matched_role)
    evidence["anchor_method"] = "token_overlap"
    evidence["anchor_token_overlap"] = round(overlap, 3)
    if _natural_single_title(cleaned):
        title_groups = _functional_groups(cleaned)
        matched_groups = _functional_groups(matched_role)
        evidence["title_function_groups"] = sorted(title_groups)
        evidence["matched_function_groups"] = sorted(matched_groups)
        if title_groups and matched_groups and title_groups.isdisjoint(matched_groups):
            evidence["rules"].append("material_cross_function_disagreement")
        else:
            evidence["rules"].append(
                "natural_title_retained_for_broader_role_classifier"
                if overlap >= 0.5
                else "natural_title_functionally_corroborated"
            )
            return _result(
                name=cleaned, raw_source=raw_source, confidence="medium", hold=False,
                status="RESOLVED" if cleaned != raw else "CLEAN", evidence=evidence,
            )

    evidence["rules"].append("matched_role_materially_conflicts_with_title")
    return _result(
        name=cleaned or raw, raw_source=raw_source, confidence="low", hold=True,
        status="AMBIGUOUS", evidence=evidence,
    )
