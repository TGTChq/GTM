"""Audit-only, population-wide concise outbound-role quality analysis.

This module does not alter the production resolver or any provider.  It consumes
the redacted read-only population extract and emits record/title classifications
plus exact Instantly lead IDs for review.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CLASS_CLEAN = "CLEAN_ALREADY"
CLASS_SIMPLIFY = "SHOULD_SIMPLIFY"
CLASS_AMBIGUOUS = "AMBIGUOUS"

ROLE_NOUN_RE = re.compile(
    r"\b(?:accountant|administrator|advisor|advocate|analyst|architect|associate|"
    r"broker|consultant|coordinator|copywriter|designer|developer|director|editor|engineer|"
    r"executive|generalist|lead|leader|liaison|manager|marketer|producer|recruiter|"
    r"representative|scientist|specialist|strategist|therapist|technician|writer)\b",
    re.I,
)
CORE_ROLE_NOUN_RE = re.compile(
    r"\b(?:accountant|administrator|advisor|advocate|analyst|architect|broker|consultant|"
    r"coordinator|copywriter|designer|developer|director|driver|editor|engineer|executive|"
    r"generalist|leader|liaison|manager|marketer|producer|recruiter|representative|"
    r"scientist|specialist|strategist|therapist|technician|writer)\b",
    re.I,
)
SENIORITY = (
    ("founding", "Founding"),
    ("principal", "Principal"),
    ("staff", "Staff"),
    ("lead", "Lead"),
    ("senior", "Senior"),
    ("junior", "Junior"),
    ("associate", "Associate"),
    ("assistant", "Assistant"),
)
MARKET_MODIFIERS = (
    ("mid market", "Mid-Market"),
    ("enterprise", "Enterprise"),
    ("smb", "SMB"),
    ("strategic", "Strategic"),
    ("commercial", "Commercial"),
    ("key", "Key"),
    ("national", "National"),
    ("regional", "Regional"),
    ("technical", "Technical"),
    ("partner", "Partner"),
    ("channel", "Channel"),
    ("inside sales", "Inside Sales"),
    ("outside sales", "Outside Sales"),
    ("inside", "Inside"),
)
ROLE_SPECIFIC_MODIFIERS = {
    "accountant": (
        ("accounts payable", "Accounts Payable"),
        ("accounts receivable", "Accounts Receivable"),
        ("general ledger", "General Ledger"),
        ("gl", "GL"),
        ("cost", "Cost"),
        ("property", "Property"),
        ("corporate", "Corporate"),
        ("revenue", "Revenue"),
        ("tax", "Tax"),
    ),
    "bookkeeper": (("full charge", "Full Charge"),),
    "software engineer": (
        ("full stack", "Full-Stack"),
        ("frontend", "Frontend"),
        ("backend", "Backend"),
    ),
}

CATEGORY_LABELS = {
    "location_or_territory": "Location, country, region, or sales territory",
    "work_or_employment_metadata": "Remote/hybrid/on-site, schedule, or employment metadata",
    "salary_or_benefits": "Salary, OTE, commission, benefits, or numeric compensation noise",
    "product_industry_customer_context": "Product, industry, customer, technology, or specialization context",
    "business_unit_company_team": "Company, business-unit, department, or team descriptors",
    "promotional_hiring_copy": "Hiring, growth, benefits, or promotional posting copy",
    "ats_source_or_requisition": "ATS/source wording, requisition IDs, or posting identifiers",
    "language_requirement": "Language requirements",
    "redundant_posting_words": "Redundant roles/jobs/opportunities wording",
    "format_or_abbreviation": "Non-conversational formatting or abbreviations",
    "multiple_role_heads": "Composite or competing role heads",
    "missing_semantic_anchor": "Missing or uncorroborated matched-role anchor",
}


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _normalize(value))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"\bsr\.?\b", "senior", text)
    text = re.sub(r"\bjr\.?\b", "junior", text)
    text = re.sub(r"\bfront[ -]?end\b", "frontend", text)
    text = re.sub(r"\bback[ -]?end\b", "backend", text)
    text = re.sub(r"\bfull[ -]?stack\b", "full stack", text)
    text = re.sub(r"\be[ -]?commerce\b", "ecommerce", text)
    text = re.sub(r"\bmid[ -]?market\b", "mid market", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _alias_key(value: Any) -> str:
    text = f" {_key(value)} "
    replacements = {
        " accounts payable ": " ap ",
        " accounts receivable ": " ar ",
        " business development representative ": " bdr ",
        " sales development representative ": " sdr ",
        " go to market ": " gtm ",
        " quality assurance ": " qa ",
        " user experience user interface ": " ux ui ",
        " small business ": " smb ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return " ".join(text.split())


def _tokens(value: Any) -> set[str]:
    return {token for token in _alias_key(value).split() if token not in {"of", "and", "the"}}


def _anchor_present(title: str, matched: str) -> bool:
    if not matched:
        return False
    title_key = _alias_key(title)
    matched_key = _alias_key(matched)
    return bool(re.search(rf"(?:^| ){re.escape(matched_key)}(?: |$)", title_key))


def _token_overlap(title: str, matched: str) -> float:
    wanted = _tokens(matched)
    return len(wanted & _tokens(title)) / len(wanted) if wanted else 0.0


def _location_tokens(record: dict[str, Any]) -> set[str]:
    location = _normalize(record.get("location"))
    tokens: set[str] = set()
    if location:
        tokens.add(_key(location))
        for part in re.split(r"\s*[,|/]\s*", location):
            if len(_key(part)) >= 2:
                tokens.add(_key(part))
    return tokens


def _has_competing_role_heads(text: str) -> bool:
    slash_or_pipe = [part for part in re.split(r"[/|]", text) if part.strip()]
    if len([part for part in slash_or_pipe if ROLE_NOUN_RE.search(part)]) >= 2:
        return True
    for inner in re.findall(r"\(([^)]*)\)", text):
        if CORE_ROLE_NOUN_RE.search(inner) and ROLE_NOUN_RE.search(re.sub(r"\([^)]*\)", "", text)):
            return True
    chunks = [part for part in re.split(r"\s+(?:and|&)\s+", text, flags=re.I) if part.strip()]
    return len([part for part in chunks if CORE_ROLE_NOUN_RE.search(part)]) >= 2


def _detect_categories(title: str, record: dict[str, Any], matched: str) -> list[str]:
    text = _normalize(title)
    key = _key(text)
    categories: set[str] = set()
    locations = _location_tokens(record)

    if any(loc and re.search(rf"(?:^| ){re.escape(loc)}(?: |$)", key) for loc in locations) or re.search(
        r"\b(?:u\.?s\.?a?|united states|north america|southwest|southeast|west coast|"
        r"sf bay area|latam|europe|americas|nyc|atl|canada|denmark|norway)\b",
        text,
        re.I,
    ):
        categories.add("location_or_territory")
    if re.search(
        r"\b(?:remote|hybrid|on[ -]?site|work from home|wfh|full[ -]?time|part[ -]?time|"
        r"contract|temporary|temp|fee for service|shift|working hours|flexible hours|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
        re.I,
    ):
        categories.add("work_or_employment_metadata")
    if re.search(
        r"\$\s*\d|\b\d{2,3}\s*[kK]\b|\b(?:ote|commission|stock options|flexible pto|"
        r"great benefits|great pay|compensation package)\b|\bp/?y\b",
        text,
        re.I,
    ):
        categories.add("salary_or_benefits")
    if re.search(
        r"\b(?:is hiring|now hiring|job at|opportunit(?:y|ies)|join our team|growing|"
        r"high[ -]?growth|vc backed|career growth|no third parties|locals only|"
        r"team expansion|bold brands|growth & pto)\b",
        text,
        re.I,
    ):
        categories.add("promotional_hiring_copy")
    if re.search(
        r"(?:^|\s)(?:req(?:uisition)?|job)\s*(?:id|#)?\s*[:#-]?\s*[a-z0-9-]+|"
        r"^\d{4,}\s*[-–—]|#\d{3,}|\bcareers\b|\bjob at\b|\s-\s\d{5,}$",
        text,
        re.I,
    ):
        categories.add("ats_source_or_requisition")
    if re.search(r"\b(?:bilingual|multilingual|spanish|chinese|french|german)\b", text, re.I):
        categories.add("language_requirement")
    if re.search(r"\b(?:roles|jobs|openings|opportunities)\b", text, re.I):
        categories.add("redundant_posting_words")
    if (
        re.search(r"\b(?:sr|jr)\.?\b|---+|\[[^]]+\]|^[A-Z][A-Z\s-]{8,}$", text)
        or text.startswith('["')
        or "*" in text
    ):
        categories.add("format_or_abbreviation")

    # Structural context after/before a corroborated role head.  Standard
    # seniority/market qualifiers are excluded later by the renderer.
    if matched and _anchor_present(text, matched):
        matched_key = _alias_key(matched)
        title_key = _alias_key(text)
        index = title_key.find(matched_key)
        before = title_key[:index].strip()
        after = title_key[index + len(matched_key):].strip()
        allowed_before = {item[0] for item in SENIORITY} | {item[0] for item in MARKET_MODIFIERS} | {
            "bilingual", "remote", "hybrid", "temp", "temporary", "part time", "full time"
        }
        before_clean = before
        for phrase in sorted(allowed_before, key=len, reverse=True):
            before_clean = re.sub(rf"(?:^| ){re.escape(phrase)}(?: |$)", " ", before_clean)
        before_clean = " ".join(before_clean.split())
        if before_clean:
            categories.add("business_unit_company_team")
        if after and not re.fullmatch(r"(?:i|ii|iii|iv|1|2|3|4|level [1-4])", after):
            categories.add("product_industry_customer_context")

    if re.search(r"\b(?:team|department|division|group|studio|office of the|supporting)\b", text, re.I):
        categories.add("business_unit_company_team")
    if _has_competing_role_heads(text):
        categories.add("multiple_role_heads")
    if re.search(r"\bmanager\s+leader\b", text, re.I):
        categories.add("format_or_abbreviation")

    if not matched or (not _anchor_present(text, matched) and _token_overlap(text, matched) < 0.67):
        categories.add("missing_semantic_anchor")

    contextual_structure = bool(re.search(r"\s[-–—|;/]\s|[,:]\s*\S|\([^)]{3,}\)|\[[^]]+\]", text))
    if contextual_structure and not categories.intersection({
        "location_or_territory", "work_or_employment_metadata", "salary_or_benefits",
        "promotional_hiring_copy", "ats_source_or_requisition", "language_requirement",
        "multiple_role_heads",
    }):
        categories.add("product_industry_customer_context")
    return sorted(categories)


def _recognized_modifier_present(title: str, anchor_key: str, phrase: str) -> bool:
    title_key = _alias_key(title)
    phrase = _alias_key(phrase)
    if re.search(rf"(?:^| ){re.escape(phrase)} {re.escape(anchor_key)}(?: |$)", title_key):
        return True
    # A post-anchor modifier is preserved only when it is an entire delimited
    # segment, optionally followed by the generic word market(s) or sales.
    allowed_segments = {phrase, f"{phrase} market", f"{phrase} markets", f"{phrase} sales"}
    segments = [_alias_key(part) for part in re.split(r"[,()|;/]|\s[-–—]\s", title)]
    return any(segment in allowed_segments for segment in segments)


def _render_from_anchor(title: str, matched: str) -> tuple[str, list[str], bool]:
    title_key = _alias_key(title)
    anchor_key = _alias_key(matched)
    modifiers: list[str] = []

    # Choose the closest explicit seniority modifier to the anchor.  This turns
    # "Founding & Lead Recruiter Roles" into "Lead Recruiter" rather than
    # carrying the multi-opening construction into email copy.
    anchor_index = title_key.find(anchor_key)
    prefix_words = title_key[:anchor_index].split() if anchor_index >= 0 else []
    closest_seniority: tuple[int, str] | None = None
    for phrase, display in SENIORITY:
        phrase_words = phrase.split()
        for index in range(0, len(prefix_words) - len(phrase_words) + 1):
            if prefix_words[index:index + len(phrase_words)] == phrase_words:
                distance = len(prefix_words) - (index + len(phrase_words))
                if distance <= 3 and (closest_seniority is None or distance < closest_seniority[0]):
                    closest_seniority = (distance, display)
    if re.search(r"\bentry[ -]level\b", title, re.I):
        closest_seniority = (0, "Junior")
    if closest_seniority and _key(closest_seniority[1]) not in anchor_key.split():
        modifiers.append(closest_seniority[1])

    market_found: list[str] = []
    for phrase, display in MARKET_MODIFIERS:
        if phrase in anchor_key:
            continue
        if _recognized_modifier_present(title, anchor_key, phrase):
            market_found.append(display)
    # Multiple competing market segments are not collapsed arbitrarily.
    market_ambiguous = len(set(market_found)) > 1
    if not market_ambiguous:
        modifiers.extend(market_found)

    for phrase, display in ROLE_SPECIFIC_MODIFIERS.get(anchor_key, ()):
        if _recognized_modifier_present(title, anchor_key, phrase):
            modifiers.append(display)

    result = " ".join(dict.fromkeys(modifiers + [_normalize(matched)]))
    level = re.search(rf"\b{re.escape(anchor_key)}\s+(i|ii|iii|iv|[1-4])\b", title_key)
    if level and not result.lower().endswith(f" {level.group(1)}"):
        result = f"{result} {level.group(1).upper()}"
    return result, list(dict.fromkeys(modifiers)), market_ambiguous


def _strip_obvious_metadata(title: str, record: dict[str, Any]) -> str:
    text = _normalize(title)
    text = re.sub(r'^\[?\s*(?:remote|hybrid|on[ -]?site)\s*\]?\s+', "", text, flags=re.I)
    text = re.sub(r"^(?:part[ -]?time|full[ -]?time|bilingual)\s+", "", text, flags=re.I)
    text = re.sub(r"^\d{4,}\s*[-–—]\s*", "", text)
    text = re.sub(r"^.+?\bis hiring\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"\s+job at\s+.+$", "", text, flags=re.I)
    text = re.sub(r"\s+-\s+(?:careers\s+-\s+.+|now hiring|no third parties|locals only)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*\((?:remote[^)]*|hybrid[^)]*|on[ -]?site[^)]*|contract|part[ -]?time|full[ -]?time|bilingual|[^)]*working hours[^)]*)\)\s*", " ", text, flags=re.I)
    text = re.sub(r"\s+[-–—|]\s+(?:remote|hybrid|on[ -]?site|us|usa|north america|west|southwest|southeast).*$", "", text, flags=re.I)
    text = re.sub(r"\s+-\s+\d{5,}\s*$", "", text)
    return _normalize(text).strip(" -–—|,;:")


@dataclass(frozen=True)
class AuditDecision:
    classification: str
    proposed: str
    confidence: str
    categories: list[str]
    evidence: list[str]


def decide(record: dict[str, Any]) -> AuditDecision:
    current = _normalize(record.get("outbound_role") or record.get("raw_title"))
    raw = _normalize(record.get("raw_title") or record.get("canonical_title") or current)
    matched = _normalize(record.get("matched_role"))
    categories = _detect_categories(raw or current, record, matched)
    evidence: list[str] = []

    if not current:
        return AuditDecision(CLASS_AMBIGUOUS, matched, "low", sorted(set(categories + ["missing_semantic_anchor"])), ["missing_current_and_raw_title"])

    if matched and _anchor_present(raw, matched):
        proposed, modifiers, market_ambiguous = _render_from_anchor(raw, matched)
        evidence.append("matched_role_lexically_corroborated")
        if modifiers:
            evidence.append("recognized_title_modifiers_preserved:" + ",".join(modifiers))
        if market_ambiguous:
            return AuditDecision(CLASS_AMBIGUOUS, proposed, "low", categories, evidence + ["competing_market_qualifiers"])
        if "multiple_role_heads" in categories:
            return AuditDecision(
                CLASS_AMBIGUOUS,
                proposed,
                "low",
                categories,
                evidence + ["competing_role_heads_require_review"],
            )
        if "multiple_role_heads" in categories and _alias_key(raw).find(_alias_key(matched)) > 0:
            # A matched anchor outside the first role phrase may be a secondary
            # responsibility rather than the advertised position.
            first_head = ROLE_NOUN_RE.search(raw)
            anchor_pos = _key(raw).find(_key(matched))
            if first_head and anchor_pos > len(_key(raw[: first_head.end()])):
                return AuditDecision(CLASS_AMBIGUOUS, proposed, "low", categories, evidence + ["matched_role_is_secondary_role_head"])
        classification = CLASS_CLEAN if _normalize(current) == _normalize(proposed) else CLASS_SIMPLIFY
        return AuditDecision(classification, proposed, "high", categories, evidence)

    cleaned = _strip_obvious_metadata(current, record)
    overlap = _token_overlap(cleaned, matched) if matched else 0.0
    noisy = bool(categories) and categories != ["missing_semantic_anchor"]
    multi = "multiple_role_heads" in categories
    if not noisy and current:
        return AuditDecision(CLASS_CLEAN, current, "medium" if matched else "low", categories, ["natural_single_title_retained"])
    if cleaned != current and ROLE_NOUN_RE.search(cleaned) and not multi and (not matched or overlap >= 0.5):
        return AuditDecision(CLASS_SIMPLIFY, cleaned, "medium", categories, ["metadata_only_cleanup_without_exact_anchor"])
    return AuditDecision(
        CLASS_AMBIGUOUS,
        cleaned if cleaned != current else (matched or current),
        "low",
        sorted(set(categories + (["missing_semantic_anchor"] if not matched else []))),
        ["no_safe_single_role_head"] if multi else ["semantic_anchor_not_safely_corroborated"],
    )


def audit(population: dict[str, Any]) -> dict[str, Any]:
    audited: list[dict[str, Any]] = []
    category_records: Counter[str] = Counter()
    category_titles: dict[str, set[str]] = defaultdict(set)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    record_classes: Counter[str] = Counter()

    for record in population.get("records") or []:
        decision = decide(record)
        current = _normalize(record.get("outbound_role") or record.get("raw_title"))
        row = {
            **record,
            "current_outbound_role": current,
            "proposed_outbound_role": decision.proposed,
            "classification": decision.classification,
            "audit_confidence": decision.confidence,
            "noise_categories": decision.categories,
            "audit_evidence": decision.evidence,
            "would_change": decision.classification == CLASS_SIMPLIFY and _normalize(current) != _normalize(decision.proposed),
        }
        audited.append(row)
        record_classes[decision.classification] += 1
        for category in decision.categories:
            category_records[category] += 1
            category_titles[category].add(current)
            if len(examples[category]) < 8 and current != decision.proposed:
                examples[category].append({
                    "before": current,
                    "after": decision.proposed,
                    "matched_role": record.get("matched_role"),
                    "classification": decision.classification,
                })

    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in audited:
        by_title[row["current_outbound_role"]].append(row)
    title_rows: list[dict[str, Any]] = []
    title_classes: Counter[str] = Counter()
    for current, rows in sorted(by_title.items(), key=lambda item: item[0].lower()):
        proposals = sorted({_normalize(row["proposed_outbound_role"]) for row in rows})
        classes = {row["classification"] for row in rows}
        if len(proposals) > 1 or CLASS_AMBIGUOUS in classes:
            classification = CLASS_AMBIGUOUS
        elif CLASS_SIMPLIFY in classes:
            classification = CLASS_SIMPLIFY
        else:
            classification = CLASS_CLEAN
        title_classes[classification] += 1
        title_rows.append({
            "current_outbound_role": current,
            "record_count": len(rows),
            "classification": classification,
            "proposals": proposals,
            "matched_roles": sorted({_normalize(row.get("matched_role")) for row in rows if row.get("matched_role")}),
            "raw_titles": sorted({_normalize(row.get("raw_title")) for row in rows if row.get("raw_title")}),
            "role_focuses": sorted({_normalize(row.get("role_focus")) for row in rows if row.get("role_focus")})[:10],
            "noise_categories": sorted({category for row in rows for category in row["noise_categories"]}),
        })

    instantly_changes = [
        {
            "instantly_lead_id": row.get("instantly_lead_id"),
            "airtable_record_id": row.get("airtable_record_id"),
            "campaign_id": row.get("campaign_id"),
            "campaign_name": row.get("campaign_name"),
            "raw_title": row.get("raw_title"),
            "canonical_title": row.get("canonical_title"),
            "matched_role": row.get("matched_role"),
            "role_focus": row.get("role_focus"),
            "current_outbound_role": row["current_outbound_role"],
            "proposed_outbound_role": row["proposed_outbound_role"],
            "classification": row["classification"],
            "audit_confidence": row["audit_confidence"],
            "noise_categories": row["noise_categories"],
        }
        for row in audited
        if row.get("population") == "instantly_unsent" and row["would_change"]
    ]
    instantly_ambiguous = [
        {
            "instantly_lead_id": row.get("instantly_lead_id"),
            "airtable_record_id": row.get("airtable_record_id"),
            "campaign_id": row.get("campaign_id"),
            "current_outbound_role": row["current_outbound_role"],
            "proposed_outbound_role": row["proposed_outbound_role"],
            "matched_role": row.get("matched_role"),
            "noise_categories": row["noise_categories"],
        }
        for row in audited
        if row.get("population") == "instantly_unsent" and row["classification"] == CLASS_AMBIGUOUS
    ]
    unchanged_examples = []
    for row in audited:
        if row["classification"] != CLASS_CLEAN:
            continue
        if row["current_outbound_role"] in {item["title"] for item in unchanged_examples}:
            continue
        unchanged_examples.append({
            "title": row["current_outbound_role"],
            "matched_role": row.get("matched_role"),
            "reason": row["audit_evidence"],
        })
        if len(unchanged_examples) >= 20:
            break

    return {
        "schema": "tgtc-outbound-role-quality-audit/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_local_analysis",
        "source_population_generated_at": population.get("generated_at"),
        "external_writes": 0,
        "summary": {
            "records_inspected": len(audited),
            "distinct_current_titles_inspected": len(title_rows),
            "record_classification_counts": dict(record_classes),
            "distinct_title_classification_counts": dict(title_classes),
            "instantly_unsent_records": sum(row.get("population") == "instantly_unsent" for row in audited),
            "airtable_queued_records": sum(row.get("population") == "airtable_queued" for row in audited),
            "instantly_unsent_would_change": len(instantly_changes),
            "instantly_unsent_ambiguous": len(instantly_ambiguous),
            "records_missing_matched_role": sum(not _normalize(row.get("matched_role")) for row in audited),
            "records_missing_role_focus": sum(not _normalize(row.get("role_focus")) for row in audited),
            "records_missing_outbound_role": sum(not _normalize(row.get("outbound_role")) for row in audited),
            "separate_raw_vs_canonical_signal_available": any(
                _normalize(row.get("raw_title")) != _normalize(row.get("canonical_title")) for row in audited
            ),
        },
        "noise_categories": [
            {
                "key": key,
                "label": CATEGORY_LABELS[key],
                "record_count": category_records[key],
                "distinct_title_count": len(category_titles[key]),
                "representative_examples": examples[key],
            }
            for key in CATEGORY_LABELS
            if category_records[key]
        ],
        "unchanged_examples": unchanged_examples,
        "instantly_unsent_changes": instantly_changes,
        "instantly_unsent_ambiguous": instantly_ambiguous,
        "distinct_titles": title_rows,
        "records": audited,
        "recommended_resolver": {
            "decision_order": [
                "normalize Unicode, whitespace, dash variants, and conventional abbreviations",
                "use matched_role as primary semantic anchor; require lexical/alias corroboration before aggressive reduction",
                "preserve only adjacent recognized seniority and conventional market modifiers",
                "render a concise title from the corroborated anchor instead of carrying posting suffixes",
                "use structured location, work arrangement, employment type, salary, and Role Focus as removal evidence",
                "retain an already-natural single title when matched_role is not lexically exact",
                "return AMBIGUOUS when multiple role heads compete or the anchor cannot safely identify one position",
            ],
            "production_recommendation": "replace raw-title-led cleanup with anchor-first role-display/2; keep raw/canonical/classification fields unchanged",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="reports/outbound_role_population_20260817.json")
    parser.add_argument("--output", default="reports/outbound_role_quality_audit_20260817.json")
    args = parser.parse_args()
    population = json.loads(Path(args.input).read_text(encoding="utf-8"))
    result = audit(population)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output.resolve()),
        "summary": result["summary"],
        "noise_categories": [
            {key: row[key] for key in ("key", "record_count", "distinct_title_count")}
            for row in result["noise_categories"]
        ],
        "external_writes": 0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
