"""Context-aware extraction of critical job facts from official content."""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from evidence_types import EvidenceItem, EvidenceStatus, FactValue
from job_source_resolver import ResolvedJobSource


def _sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return [part.strip() for part in re.split(r"(?<=[.!?;])\s+|\n+", text) if part.strip()]


def _matching(sentences: Iterable[str], patterns: Iterable[str]) -> List[str]:
    output: List[str] = []
    for sentence in sentences:
        if any(re.search(pattern, sentence, re.I) for pattern in patterns):
            output.append(sentence[:700])
    return output


def _fact(name: str, value, source: ResolvedJobSource, excerpts: List[str], *, verified=True) -> FactValue:
    status = EvidenceStatus.VERIFIED_OFFICIAL if verified else EvidenceStatus.WEAK_PROVIDER_SIGNAL
    return FactValue(
        name,
        value,
        status,
        [
            EvidenceItem(
                name,
                value,
                status,
                source.source_type or "official_source",
                source.source_url,
                excerpt,
                0.98 if verified else 0.45,
            )
            for excerpt in excerpts[:3]
        ],
    )


def _unknown(name: str) -> FactValue:
    return FactValue(name, None, EvidenceStatus.UNKNOWN)


NON_ACTIVE = [
    r"(?m)^\s*(?:future openings?|future opportunities|talent pool|talent pipeline|general application|expression of interest)\s*$",
    r"\b(?:this|the) (?:posting|role|position|application) (?:is|exists|serves)\b[^.\n]{0,120}\b(?:future openings?|future opportunities|talent pool|talent pipeline|general application|expression of interest)\b",
    r"\bnot (?:an|a) active (?:opening|role|position)\b",
]
EMPLOYMENT_NEGATIVES: List[Tuple[str, List[str]]] = [
    ("part_time", [r"\bpart[- ]time\b", r"\b(?:under|up to|approximately)\s*\d{1,2}\s*(?:hours|hrs)\s*(?:per|/)\s*week\b"]),
    ("fixed_term", [r"\bfixed[- ]term\b", r"\b\d{1,2}[- ]month\s+(?:contract|term)\b"]),
    ("fractional", [r"\b(?:this|the) (?:is|role is|position is)\b[^.]{0,60}\bfractional\b", r"\bfractional (?:role|position|contractor|employee|engagement)\b"]),
    ("contract", [r"\b(?:this|the) (?:is|role is|position is)\b[^.]{0,80}\b(?:contract|contractor)\b", r"\bindependent contractor\b", r"\bcontract[- ]to[- ]hire\b"]),
    ("temporary", [r"\btemporary (?:role|position|job|assignment)\b", r"\btemp[- ]to[- ]hire\b"]),
    ("freelance", [r"\bfreelance(?:r)? (?:role|position|engagement)\b", r"\bseeking (?:a )?freelance"]),
    ("seasonal", [r"\bseasonal (?:role|position|job|employment)\b"]),
    ("internship", [r"\bintern(?:ship)?\b", r"\bexternship\b", r"\bfellowship\b", r"\bapprenticeship\b", r"\breturnship\b"]),
    ("unpaid", [r"\b(?:this|the) (?:is|role is|position is)\b[^.]{0,70}\bunpaid\b", r"\bequity[- ]only\b", r"\bcommission[- ]only\b", r"\bno financial compensation\b"]),
]
FULL_TIME = [r"\bfull[- ]time\b", r"\bregular employee\b", r"\bpermanent (?:role|position|employee)\b"]
REMOTE = [r"\bfully remote\b", r"\b100% remote\b", r"\bremote (?:role|position|job)\b", r"\bwork from home\b", r"\bhome[- ]based\b", r"\btelecommute\b"]
HYBRID = [r"\bhybrid (?:role|position|schedule|work model)\b", r"\b(?:one|two|three|four|five|[1-5]) days? (?:a|per) week[^.]{0,80}\boffice\b"]
ONSITE = [r"\bon[- ]site\b", r"\bonsite\b", r"\bin[- ]person\b", r"\boffice[- ]based\b", r"\bmust (?:work|report|be) (?:in|at) (?:the|our) office\b"]
FIELD = [r"\bfield[- ]based\b", r"\bregular(?:ly)? visit(?:ing)? (?:customer|client) sites\b", r"\bon customer sites\b"]
RELOCATION = [r"\brelocation (?:is )?(?:required|mandatory)\b"]
TRAVEL_HARD = [r"\btravel (?:up to |approximately |at least |minimum )?(?:20|2[5-9]|[3-9]\d|100)%\b", r"\bfrequent travel\b", r"\btravel regularly\b", r"\bmust live near (?:a|an) airport\b"]
US_SCOPE = [r"\bremote (?:within|in|across) (?:the )?(?:u\.?s\.?|usa|united states)\b", r"\b(?:u\.?s\.?|usa|united states)[- ]based\b", r"\banywhere in (?:the )?(?:u\.?s\.?|united states)\b"]
FOREIGN_ONLY = [r"\b(?:emea|apac|europe|european union|canada|uk|united kingdom|australia|india|philippines|latam)[- ]only\b", r"\bmust be (?:based|located|resident) in (?:emea|apac|europe|canada|the uk|australia|india|the philippines|latam)\b", r"\bopen only to candidates (?:based|located) in (?:emea|apac|europe|canada|the uk|australia|india|the philippines|latam)\b"]
LOCAL_PRESENCE = [r"\bmust (?:live|reside) within \d+ miles\b", r"\bwithin commuting distance\b", r"\bmust be based in [A-Z][a-z]+(?:,? [A-Z]{2})? to (?:work|attend|report|visit)\b"]
CLEARANCE = [r"\b(?:active |current )?(?:secret|top secret|ts/sci|security) clearance (?:is )?(?:required|mandatory|needed)\b", r"\bpublic trust(?: clearance)?\b"]
LICENSE = [r"\b(?:active|current|valid) [A-Za-z ]{0,40}(?:license|licensure) (?:is )?(?:required|mandatory)\b"]
FACILITY = [r"\bmust (?:work|operate) in (?:a|the) (?:laboratory|lab|warehouse|plant|factory|clinic|hospital)\b", r"\bphysical presence (?:is )?required\b"]


def extract_job_facts(job: Dict, source: ResolvedJobSource) -> Dict[str, FactValue]:
    text = source.description or str(job.get("job_description") or "")
    title = source.canonical_title or str(job.get("job_title") or "")
    joined = f"{title}. {source.location_text}. {text}"
    sentences = _sentences(joined)
    official = source.state == "ACTIVE_VERIFIED" and source.official

    facts: Dict[str, FactValue] = {}
    facts["active_status"] = _fact("active_status", bool(source.active), source, [source.state], verified=official) if source.active is not None else _unknown("active_status")
    facts["hiring_organization"] = _fact("hiring_organization", source.canonical_employer, source, [source.canonical_employer], verified=official) if source.canonical_employer else _unknown("hiring_organization")
    facts["canonical_title"] = _fact("canonical_title", source.canonical_title, source, [source.canonical_title], verified=official) if source.canonical_title else _unknown("canonical_title")

    non_active = _matching(sentences, NON_ACTIVE)
    if non_active:
        facts["active_status"] = _fact("active_status", False, source, non_active, verified=official)

    employment_value: Optional[str] = None
    employment_excerpt: List[str] = []
    for value, patterns in EMPLOYMENT_NEGATIVES:
        hits = _matching(sentences, patterns)
        if hits:
            # Incidental benefit/service language is deliberately excluded by the
            # subject-bearing patterns above.
            employment_value, employment_excerpt = value, hits
            break
    source_type_text = str(source.employment_type or "")
    if not employment_value and re.search(r"full.?time", source_type_text, re.I):
        employment_value, employment_excerpt = "full_time", [source_type_text]
    if not employment_value:
        hits = _matching(sentences, FULL_TIME)
        if hits:
            employment_value, employment_excerpt = "full_time", hits
    facts["employment_type"] = (
        _fact("employment_type", employment_value, source, employment_excerpt, verified=official)
        if employment_value else _unknown("employment_type")
    )
    facts["employment_duration"] = (
        _fact("employment_duration", "open_ended", source, employment_excerpt, verified=official)
        if employment_value == "full_time" else
        _fact("employment_duration", employment_value, source, employment_excerpt, verified=official)
        if employment_value else _unknown("employment_duration")
    )

    hybrid = _matching(sentences, HYBRID)
    onsite = _matching(sentences, ONSITE)
    field = _matching(sentences, FIELD)
    relocation = _matching(sentences, RELOCATION)
    local_presence = _matching(sentences, LOCAL_PRESENCE)
    travel = _matching(sentences, TRAVEL_HARD)
    remote = _matching(sentences, REMOTE)
    if hybrid:
        arrangement, arrangement_evidence = "hybrid_required", hybrid
    elif onsite:
        arrangement, arrangement_evidence = "onsite_required", onsite
    elif field:
        arrangement, arrangement_evidence = "field_work_required", field
    elif local_presence:
        arrangement, arrangement_evidence = "local_presence_required", local_presence
    elif relocation:
        arrangement, arrangement_evidence = "relocation_required", relocation
    elif travel:
        arrangement, arrangement_evidence = "travel_required", travel
    elif remote or re.search(r"TELECOMMUTE", source.location_text, re.I):
        arrangement, arrangement_evidence = "remote", remote or [source.location_text]
    else:
        arrangement, arrangement_evidence = None, []
    facts["work_arrangement"] = (
        _fact("work_arrangement", arrangement, source, arrangement_evidence, verified=official)
        if arrangement else _unknown("work_arrangement")
    )
    facts["travel_requirement"] = (
        _fact("travel_requirement", "substantial", source, travel, verified=official)
        if travel else _fact("travel_requirement", "none_found", source, ["No substantial travel requirement found in official posting"], verified=official)
        if official else _unknown("travel_requirement")
    )

    foreign = _matching(sentences, FOREIGN_ONLY)
    us = _matching(sentences, US_SCOPE)
    # Structured official location can establish US intent even when prose omits it.
    if re.search(r"\b(?:United States|US|USA)\b", source.location_text, re.I):
        us = us or [source.location_text]
    if foreign:
        geography, geo_evidence = "foreign_only", foreign
    elif us:
        geography, geo_evidence = "us_market", us
    else:
        country = str(job.get("job_country") or "")
        location = str(job.get("job_location") or "")
        if official and re.search(r"\b(?:United States|USA)\b", location, re.I):
            geography, geo_evidence = "us_market", [location]
        else:
            geography, geo_evidence = None, []
    facts["intent_market"] = (
        _fact("intent_market", geography, source, geo_evidence, verified=official)
        if geography else _unknown("intent_market")
    )

    for name, patterns in (
        ("security_clearance", CLEARANCE),
        ("professional_license", LICENSE),
        ("physical_facility", FACILITY),
    ):
        hits = _matching(sentences, patterns)
        facts[name] = (
            _fact(name, "required", source, hits, verified=official)
            if hits else _fact(name, "not_found", source, [f"No mandatory {name} found"], verified=official)
            if official else _unknown(name)
        )
    return facts
