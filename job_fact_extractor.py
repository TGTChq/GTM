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
    if not verified:
        status = EvidenceStatus.WEAK_PROVIDER_SIGNAL
    elif source.official:
        status = EvidenceStatus.VERIFIED_OFFICIAL
    else:
        status = EvidenceStatus.VERIFIED_CROSS_SOURCE
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



def _provider_signal_fact(name: str, value, job: Dict, excerpts: List[str]) -> FactValue:
    status = EvidenceStatus.WEAK_PROVIDER_SIGNAL
    return FactValue(
        name,
        value,
        status,
        [
            EvidenceItem(
                name,
                value,
                status,
                "jsearch_provider",
                str(job.get("job_apply_link") or job.get("job_google_link") or ""),
                excerpt,
                0.55,
                {"provider_only": True},
            )
            for excerpt in excerpts[:3]
        ],
    )


def _unknown(name: str) -> FactValue:
    return FactValue(name, None, EvidenceStatus.UNKNOWN)


def _cross_source_fact(
    name: str,
    value,
    source: ResolvedJobSource,
    job: Dict,
    excerpts: List[str],
    *,
    provider_fields: List[str],
) -> FactValue:
    """Build a traceable fact from an active official posting plus provider data.

    The official page remains the authority for job identity and contradictions.
    Provider/prefilter signals may fill a field only when the official posting is
    active, official and silent on that field.  This avoids throwing away real
    jobs because an ATS omits a label such as ``FULL_TIME`` from its rendered
    prose, without allowing provider metadata to override an official conflict.
    """
    provider_snapshot = {
        field: job.get(field)
        for field in provider_fields
        if job.get(field) not in (None, "", [], {})
    }
    evidence = [
        EvidenceItem(
            name,
            value,
            EvidenceStatus.VERIFIED_OFFICIAL,
            source.source_type or "official_source",
            source.source_url,
            "Active official posting identity verified",
            0.98,
            {"role": "identity_anchor"},
        ),
        EvidenceItem(
            name,
            value,
            EvidenceStatus.VERIFIED_CROSS_SOURCE,
            "jsearch_prefilter",
            str(job.get("job_apply_link") or ""),
            " | ".join(excerpts)[:700],
            0.90,
            {"provider_fields": provider_snapshot},
        ),
    ]
    return FactValue(name, value, EvidenceStatus.VERIFIED_CROSS_SOURCE, evidence)


def _normalized_employment_type(job: Dict) -> str:
    return re.sub(
        r"[^a-z]", "", str(job.get("job_employment_type") or "").lower()
    )


def _provider_full_time(job: Dict) -> bool:
    return bool(
        _normalized_employment_type(job) == "fulltime"
        and str(job.get("_employment_quality") or "") == "full_time"
    )


def _provider_remote(job: Dict) -> bool:
    return bool(
        job.get("job_is_remote") is True
        and str(job.get("_work_arrangement") or "") == "remote"
    )


def _provider_us_market(job: Dict) -> bool:
    return bool(
        str(job.get("_remote_scope") or "") in {"us_explicit", "us_provider_confirmed"}
        and str(job.get("_us_eligibility_reason") or "")
    )


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
CLEARANCE = [
    r"\b(?:active |current )?(?:secret|top secret|ts/sci|security) clearance (?:is )?(?:required|mandatory|needed)\b",
    r"\b(?:ability|eligible|required|must(?: be able)?|willing) to\b[^.;]{0,160}\b(?:obtain|maintain)\b[^.;]{0,120}\b(?:secret|top secret|ts/sci|security) clearance\b",
    r"\b(?:obtain|maintain|obtain and maintain)\b[^.;]{0,100}\b(?:secret|top secret|ts/sci|security) clearance\b",
    r"\bpublic trust(?: clearance)?\b",
]
LICENSE = [r"\b(?:active|current|valid) [A-Za-z ]{0,40}(?:license|licensure) (?:is )?(?:required|mandatory)\b"]
FACILITY = [r"\bmust (?:work|operate) in (?:a|the) (?:laboratory|lab|warehouse|plant|factory|clinic|hospital)\b", r"\bphysical presence (?:is )?required\b"]


def extract_job_facts(job: Dict, source: ResolvedJobSource) -> Dict[str, FactValue]:
    official_text = str(source.description or "")
    provider_text = str(job.get("job_description") or "")
    title = source.canonical_title or str(job.get("job_title") or "")
    official_sentences = _sentences(f"{title}. {source.location_text}. {official_text}")
    provider_sentences = _sentences(
        f"{job.get('job_title') or ''}. {job.get('job_location') or ''}. {provider_text}"
    )
    official = source.state in {"ACTIVE_VERIFIED", "ACTIVE_CORROBORATED"} and source.corroborated

    facts: Dict[str, FactValue] = {}
    facts["active_status"] = (
        _fact("active_status", bool(source.active), source, [source.state], verified=official)
        if source.active is not None else _unknown("active_status")
    )
    facts["hiring_organization"] = (
        _fact("hiring_organization", source.canonical_employer, source, [source.canonical_employer], verified=official)
        if source.canonical_employer else _unknown("hiring_organization")
    )
    facts["canonical_title"] = (
        _fact("canonical_title", source.canonical_title, source, [source.canonical_title], verified=official)
        if source.canonical_title else _unknown("canonical_title")
    )

    official_non_active = _matching(official_sentences, NON_ACTIVE)
    provider_non_active = _matching(provider_sentences, NON_ACTIVE)
    if official_non_active:
        facts["active_status"] = _fact("active_status", False, source, official_non_active, verified=official)
    elif provider_non_active:
        facts["active_status"] = _provider_signal_fact("active_status", False, job, provider_non_active)

    employment_value: Optional[str] = None
    employment_excerpt: List[str] = []
    employment_provider_only = False
    for value, patterns in EMPLOYMENT_NEGATIVES:
        hits = _matching(official_sentences, patterns)
        if hits:
            employment_value, employment_excerpt = value, hits
            break
    if not employment_value:
        for value, patterns in EMPLOYMENT_NEGATIVES:
            hits = _matching(provider_sentences, patterns)
            if hits:
                employment_value, employment_excerpt, employment_provider_only = value, hits, True
                break
    source_type_text = str(source.employment_type or "")
    if not employment_value and re.search(r"full.?time", source_type_text, re.I):
        employment_value, employment_excerpt = "full_time", [source_type_text]
    if not employment_value:
        hits = _matching(official_sentences, FULL_TIME)
        if hits:
            employment_value, employment_excerpt = "full_time", hits
    if employment_value:
        maker = (
            lambda name, value: _provider_signal_fact(name, value, job, employment_excerpt)
            if employment_provider_only
            else _fact(name, value, source, employment_excerpt, verified=official)
        )
        facts["employment_type"] = maker("employment_type", employment_value)
        facts["employment_duration"] = maker(
            "employment_duration", "open_ended" if employment_value == "full_time" else employment_value
        )
    elif official and _provider_full_time(job):
        provider_excerpt = [
            f"job_employment_type={job.get('job_employment_type')}",
            f"_employment_quality={job.get('_employment_quality')}",
            str(job.get("_employment_quality_reason") or ""),
        ]
        facts["employment_type"] = _cross_source_fact(
            "employment_type", "full_time", source, job, provider_excerpt,
            provider_fields=["job_employment_type", "_employment_quality", "_employment_quality_reason"],
        )
        facts["employment_duration"] = _cross_source_fact(
            "employment_duration", "open_ended", source, job, provider_excerpt,
            provider_fields=["job_employment_type", "_employment_quality", "_employment_quality_reason"],
        )
    else:
        facts["employment_type"] = _unknown("employment_type")
        facts["employment_duration"] = _unknown("employment_duration")

    arrangement: Optional[str] = None
    arrangement_evidence: List[str] = []
    arrangement_provider_only = False
    arrangement_rules = [
        ("hybrid_required", HYBRID), ("onsite_required", ONSITE),
        ("field_work_required", FIELD), ("local_presence_required", LOCAL_PRESENCE),
        ("relocation_required", RELOCATION), ("travel_required", TRAVEL_HARD),
    ]
    for value, patterns in arrangement_rules:
        hits = _matching(official_sentences, patterns)
        if hits:
            arrangement, arrangement_evidence = value, hits
            break
    if not arrangement:
        remote = _matching(official_sentences, REMOTE)
        if remote or re.search(r"TELECOMMUTE", source.location_text, re.I):
            arrangement, arrangement_evidence = "remote", remote or [source.location_text]
    if not arrangement:
        for value, patterns in arrangement_rules:
            hits = _matching(provider_sentences, patterns)
            if hits:
                arrangement, arrangement_evidence, arrangement_provider_only = value, hits, True
                break
    if arrangement:
        facts["work_arrangement"] = (
            _provider_signal_fact("work_arrangement", arrangement, job, arrangement_evidence)
            if arrangement_provider_only
            else _fact("work_arrangement", arrangement, source, arrangement_evidence, verified=official)
        )
    elif official and _provider_remote(job):
        facts["work_arrangement"] = _cross_source_fact(
            "work_arrangement", "remote", source, job,
            [f"job_is_remote={job.get('job_is_remote')}", f"_work_arrangement={job.get('_work_arrangement')}", str(job.get("_work_arrangement_reason") or "")],
            provider_fields=["job_is_remote", "_work_arrangement", "_work_arrangement_reason"],
        )
    else:
        facts["work_arrangement"] = _unknown("work_arrangement")

    official_travel = _matching(official_sentences, TRAVEL_HARD)
    provider_travel = _matching(provider_sentences, TRAVEL_HARD)
    if official_travel:
        facts["travel_requirement"] = _fact("travel_requirement", "substantial", source, official_travel, verified=official)
    elif provider_travel:
        facts["travel_requirement"] = _provider_signal_fact("travel_requirement", "substantial", job, provider_travel)
    else:
        facts["travel_requirement"] = _unknown("travel_requirement")

    official_foreign = _matching(official_sentences, FOREIGN_ONLY)
    provider_foreign = _matching(provider_sentences, FOREIGN_ONLY)
    us = _matching(official_sentences, US_SCOPE)
    if re.search(r"\b(?:United States|US|USA)\b", source.location_text, re.I):
        us = us or [source.location_text]
    if official_foreign:
        facts["intent_market"] = _fact("intent_market", "foreign_only", source, official_foreign, verified=official)
    elif us:
        facts["intent_market"] = _fact("intent_market", "us_market", source, us, verified=official)
    elif provider_foreign:
        facts["intent_market"] = _provider_signal_fact("intent_market", "foreign_only", job, provider_foreign)
    elif official and _provider_us_market(job):
        facts["intent_market"] = _cross_source_fact(
            "intent_market", "us_market", source, job,
            [f"_remote_scope={job.get('_remote_scope')}", f"_us_eligibility_reason={job.get('_us_eligibility_reason')}"],
            provider_fields=["_remote_scope", "_us_eligibility_reason", "job_country"],
        )
    else:
        facts["intent_market"] = _unknown("intent_market")

    for name, patterns in (
        ("security_clearance", CLEARANCE),
        ("professional_license", LICENSE),
        ("physical_facility", FACILITY),
    ):
        official_hits = _matching(official_sentences, patterns)
        provider_hits = _matching(provider_sentences, patterns)
        if official_hits:
            facts[name] = _fact(name, "required", source, official_hits, verified=official)
        elif provider_hits:
            facts[name] = _provider_signal_fact(name, "required", job, provider_hits)
        else:
            facts[name] = _unknown(name)
    return facts
