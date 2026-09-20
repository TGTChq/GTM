"""Deterministic job facts and hard exclusions from posting text and provider fields.

Regex families are ported from ``job_fact_extractor.py``, ``job_quality.py`` and
``business_model_classifier.py`` at ``5d87851`` (all proven on production corpora)
with attribution; the surrounding control flow (NEEDS_CHECK, role catalogue) is
deliberately not ported.

Nothing here depends on a job title being present. A title, when present, is
evidence about the ROLE LEVEL only (a posting titled "VP Finance" is a leadership
role); it never decides which function the work belongs to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PROVIDER = "provider"
TEXT = "text"
UNKNOWN = "unknown"

#: Phase 2 offline audit (2026-09-19): tasks 1-3 correct false rejections in the
#: seniority, people-management and physical-facility rules below. Facts and
#: exclusions produced by a changed decision path carry this rule_version so a
#: replay can tell audited output from pre-audit (be3af32) output. See
#: .superpowers/sdd/phase2/task-1-3-brief.md. No policy switch existed to keep the
#: old behaviour reachable in-process; the prior behaviour is only replayable from
#: git history (commit before each task) or the counterfactual harness.
RULE_VERSION = "tgtc-core/3-audit"


@dataclass(frozen=True)
class Fact:
    name: str
    value: object
    status: str  # provider | text | unknown
    excerpt: str = ""
    rule_version: str = ""

    @property
    def known(self) -> bool:
        return self.status != UNKNOWN


@dataclass(frozen=True)
class Exclusion:
    reason: str
    excerpt: str
    source: str
    rule_version: str = ""


@dataclass
class JobFacts:
    facts: Dict[str, Fact] = field(default_factory=dict)
    exclusions: List[Exclusion] = field(default_factory=list)

    def get(self, name: str) -> Fact:
        return self.facts.get(name, Fact(name, None, UNKNOWN))

    @property
    def excluded(self) -> bool:
        return bool(self.exclusions)

    def to_dict(self) -> Dict[str, object]:
        return {
            "facts": {k: {"value": v.value, "status": v.status, "excerpt": v.excerpt[:300], "rule_version": v.rule_version}
                     for k, v in self.facts.items()},
            "exclusions": [{"reason": e.reason, "excerpt": e.excerpt[:300], "source": e.source, "rule_version": e.rule_version}
                          for e in self.exclusions],
        }


# --- sentence handling (job_fact_extractor) ----------------------------------

def sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return [p.strip() for p in re.split(r"(?<=[.!?;])\s+|\n+|•", text) if p.strip()]


def _matching(sents: Iterable[str], patterns: Sequence[str]) -> List[str]:
    out: List[str] = []
    for s in sents:
        match = next((m for p in patterns if (m := re.search(p, s, re.I))), None)
        if match:
            # A long scraped paragraph must retain the MATCH, not its first
            # unrelated 400 characters, in the audit evidence.
            start = max(0, match.start() - 100) if len(s) > 400 else 0
            out.append(s[start:start + 400])
    return out


# --- pattern families (job_fact_extractor.py) --------------------------------

NON_ACTIVE = [
    r"(?m)^\s*(?:future openings?|future opportunities|talent pool|talent pipeline|general application|expression of interest)\s*$",
    r"\b(?:this|the) (?:posting|role|position|application) (?:is|exists|serves)\b[^.\n]{0,120}\b(?:future openings?|future opportunities|talent pool|talent pipeline|general application|expression of interest)\b",
    r"\bnot (?:an|a) active (?:opening|role|position)\b",
]
EMPLOYMENT_NEGATIVES: List[Tuple[str, List[str]]] = [
    ("part_time", [
        r"\b(?:this|the) (?:is|role is|position is)\b[^.]{0,60}\bpart[- ]time\b",
        r"\bpart[- ]time (?:role|position|job|opportunity|schedule|hours)\b",
        r"\b(?:seeking|hiring|looking for) (?:a |an )?part[- ]time\b",
        r"\b(?:under|up to|approximately)\s*\d{1,2}\s*(?:hours|hrs)\s*(?:per|/)\s*week\b",
    ]),
    ("fixed_term", [r"\bfixed[- ]term\b", r"\b\d{1,2}[- ]month\s+(?:contract|term)\b"]),
    ("fractional", [r"\b(?:this|the) (?:is|role is|position is)\b[^.]{0,60}\bfractional\b", r"\bfractional (?:role|position|contractor|employee|engagement)\b"]),
    ("contract", [r"\b(?:this|the) (?:is|role is|position is)\b[^.]{0,80}\b(?:contract|contractor)\b", r"\b(?:work(?:ing)?|engaged|hired|join us) as an? independent contractor\b", r"^independent contractor(?: role| position)?$", r"\bcontract[- ]to[- ]hire\b"]),
    ("temporary", [r"\btemporary (?:role|position|job|assignment)\b", r"\btemp[- ]to[- ]hire\b"]),
    ("freelance", [r"\bfreelance(?:r)? (?:role|position|engagement)\b", r"\bseeking (?:a )?freelance"]),
    ("seasonal", [r"\bseasonal (?:role|position|job|employment)\b"]),
    ("internship", [
        r"\b(?:this|the) (?:is|role is|position is)\b[^.]{0,60}\b(?:intern(?:ship)?|externship|apprenticeship|returnship|fellowship)\b",
        r"\b(?:intern(?:ship)?|externship|apprenticeship|returnship) (?:role|position|opportunity)\b",
        r"\b(?:seeking|hiring|apply for|applying for) (?:an? )?(?:intern(?:ship)?|externship|apprentice(?:ship)?)\b",
        r"\bas an intern\b",
    ]),
    ("unpaid", [r"\b(?:this|the) (?:is|role is|position is)\b[^.]{0,70}\bunpaid\b", r"\bequity[- ]only\b", r"\bcommission[- ]only\b", r"\bno financial compensation\b"]),
]
FULL_TIME = [r"\bfull[- ]time\b", r"\bregular employee\b", r"\bpermanent (?:role|position|employee)\b"]
REMOTE = [r"\bfully remote\b", r"\b100% remote\b", r"\bremote (?:role|position|job)\b", r"\bwork from home\b", r"\bhome[- ]based\b", r"\btelecommute\b"]
HYBRID = [r"\bhybrid (?:role|position|schedule|work model)\b", r"\b(?:one|two|three|four|five|[1-5]) days? (?:a|per) week[^.]{0,80}\boffice\b", r"\bin[- ]office requirement\b"]
ONSITE = [r"\bon[- ]site\b", r"\bonsite\b", r"\bin[- ]person\b", r"\boffice[- ]based\b", r"\bmust (?:work|report|be) (?:in|at) (?:the|our) office\b"]
FIELD = [r"\bfield[- ]based\b", r"\bregular(?:ly)? visit(?:ing)? (?:customer|client) sites\b", r"\bon customer sites\b"]
TRAVEL_HARD = [r"\btravel (?:up to |approximately |at least |minimum )?(?:20|2[5-9]|[3-9]\d|100)%", r"\bfrequent travel\b", r"\btravel regularly\b", r"\bmust live near (?:a|an) airport\b"]
US_SCOPE = [r"\bremote (?:within|in|across) (?:the )?(?:u\.?s\.?|usa|united states)\b", r"\b(?:u\.?s\.?|usa|united states)[- ]based\b", r"\banywhere in (?:the )?(?:u\.?s\.?|united states)\b", r"\b(?:authorized|eligible) to work in the (?:u\.?s\.?|united states)\b"]
FOREIGN_ONLY = [
    r"\b(?:emea|apac|europe|european union|canada|uk|united kingdom|australia|india|philippines|latam)[- ]only\s+(?:role|position|job|candidates?|applicants?)\b",
    r"\b(?:role|position|job|candidates?|applicants?)\s+(?:is |are )?(?:emea|apac|europe|canada|uk|australia|india|philippines|latam)[- ]only\b",
    r"\bmust be (?:based|located|resident) in (?:emea|apac|europe|canada|the uk|australia|india|the philippines|latam)\b",
    r"\bopen only to candidates (?:based|located) in (?:emea|apac|europe|canada|the uk|australia|india|the philippines|latam)\b",
]
CLEARANCE = [
    r"\b(?:active |current )?(?:secret|top secret|ts/sci|security) clearance (?:is )?(?:required|mandatory|needed)\b",
    r"\b(?:ability|eligible|required|must(?: be able)?|willing) to\b[^.;]{0,160}\b(?:obtain|maintain)\b[^.;]{0,120}\b(?:secret|top secret|ts/sci|security) clearance\b",
    r"\bpublic trust(?: clearance)?\b",
    r"\b(?:top secret|ts/sci|ts sci)\b",
]
LICENSE = [
    r"\b(?:active|current|valid) [A-Za-z ]{0,40}(?:license|licensure) (?:is )?(?:required|mandatory)\b",
    r"\blicensure (?:is )?required\b",
    r"\bmust (?:be able to )?(?:acquire|obtain|maintain)(?: and maintain)? (?:a |an )?(?:gaming|nursing|medical|professional|state) license\b",
]
FACILITY = [
    r"^(?:you will |duties include )?(?:provide|provides|providing) front desk support by greeting visitors\b",
    r"\bmust (?:work|operate) in (?:a|the) (?:laboratory|lab|warehouse|plant|factory|clinic|hospital)\b",
    r"\bphysical presence (?:is )?required\b",
    r"\b(?:lift|lifting)\s+(?:up to\s+)?\d{2,3}\s*(?:lbs|pounds)\b",
    r"\b(?:forklift|pallet jack)\b",
    r"\b(?:patient care|bedside)\b[^.]{0,80}\b(?:required|responsibilit)",
]

# --- role level (job_quality.py) ---------------------------------------------

# Leadership / principal titles (job_quality.py + role_gate.py at 5d87851). ``staff`` and
# ``principal`` count only in front of an engineering-style noun ("Staff Engineer"),
# never alone: "Staff Accountant" is an IC title. ``lead`` never counts inside
# "lead generation" / "lead routing" style phrases.
TITLE_LEADERSHIP = re.compile(
    r"\b(?:director|vice\s+president|vp|chief|c[-\s]?level|head\s+of|head\s*,)\b"
    r"|\b(?:principal|staff)\s+(?:[a-z0-9&/-]+\s+){0,3}(?:engineer|developer|designer|analyst|scientist|administrator|architect)\b"
    r"|\blead\s+(?!generation\b|gen\b|qualification\b|scoring\b|routing\b|enrichment\b)(?:[a-z0-9&/-]+\s+){0,5}(?:engineer|developer|designer|analyst|scientist|administrator|manager)\b"
    r"|\b(?:engineer|developer|designer|analyst|scientist|administrator)\s+lead\b", re.I)
_PEOPLE_NOUN = (r"(?:direct\s+reports?|reports|staff|employees|personnel|engineers|analysts|accountants|"
                r"developers|designers|specialists|associates|representatives|technicians|coordinators|team\s+members?|people)")
PASSIVE_SUPERVISION = re.compile(
    r"(?:under\s+(?:the\s+)?(?:direct\s+|close\s+|general\s+|moderate\s+)?supervision|requires?\s+(?:limited|minimal|little|general|close)\s+supervision|"
    r"with\s+(?:guidance\s+and\s+)?supervision|is\s+supervised\s+by|subject\s+to\s+\w+\s+(?:direct\s+)?supervision|works?\s+under)", re.I)
PEOPLE_AUTHORITY = [re.compile(p, re.I) for p in (
    r"\b(?:oversee|manage|supervis|lead|coach|develop|mentor)\w*\s+(?:\w+\s+){0,3}\bdirect\s+reports?\b",
    r"\b\d+\s+direct\s+reports?\b",
    r"\b(?:will\s+have|has|with)\s+(?:\w+\s+){0,2}direct\s+reports?\b",
    r"\bsupervis(?:e|es|ing)\b\s+(?:[\w/&-]+\s+){0,3}" + _PEOPLE_NOUN,
    r"\bsupervisory\s+(?:scope|responsibilit\w+)\b",
    r"\b(?:manag|lead)\w*\s+(?:and\s+\w+ing\s+)?(?:a|the|our|your)?\s*teams?\s+of\s+(?:[\w-]+\s+){0,5}" + _PEOPLE_NOUN,
    r"\b(?:manag|lead)\w*\s+(?:a|the|our|your)\s+(?:high[-\s]performing\s+)?team\s+of\b",
    r"\bperformance\s+(?:review|appraisal)\w*\s+(?:of|for)\s+(?:\w+\s+){0,2}" + _PEOPLE_NOUN,
    r"\b(?:conduct|deliver|write|complete)\w*\s+(?:\w+\s+){0,2}performance\s+(?:review|appraisal)\w*",
    r"\bpeople\s+(?:manager|management|leadership)\s+(?:responsibilit|experience|role)",
    r"\bhire\s+and\s+(?:manage|lead|develop|retain)\b",
    r"\b(?:develop|set|communicate|establish|define)\w*\s+(?:and\s+\w+\s+)?(?:clear\s+|individual\s+)?performance\s+expectations?\b",
)]
CLAUSE_SPLIT = re.compile(r"(?:(?<=[.;!?])\s+|\n+|•|\r)")
ROLE_LEVEL_IN_TEXT = re.compile(
    r"\b(?:the|this) (?:role|position)\s*(?:is|:)?\s*(?:a|an|the)?\s*(?:senior\s+)?(director|vice president|vp|head of|chief)\b", re.I)

PROGRAM_PATTERNS = {
    "skillbridge_or_transition_program": r"\b(?:skillbridge|military spouse fellowship|career transition program)\b",
    "externship": r"\bextern(?:ship)?\b",
    "apprenticeship": r"\bapprentice(?:ship)?\b",
    "returnship": r"\breturnship\b",
    "internship": r"\bintern(?:ship)?\b",
    "co_op": r"\bco[- ]?op(?:erative education)?\b",
    "volunteer": r"\bvolunteer (?:advisory )?(?:role|position|opportunity)\b|\(volunteer\)",
}
GOVERNMENT_TITLE = re.compile(r"\b(?:federal|public sector|coast guard|department of homeland security|dhs)\b", re.I)

# --- agency / outsourcing (config + job_quality + business_model_classifier) ---

KNOWN_OUTSOURCING_EMPLOYERS = (
    "concentrix", "teleperformance", "foundever", "sitel", "ttec", "alorica", "taskus", "transcom",
    "genpact", "wns", "conduent", "supportninja", "helpware", "cloudstaff", "bruntwork", "cyberbacker",
    "wing assistant", "wing assistants", "outsourced doers", "boldr", "remote staff", "agileengine",
    "anomaly squared", "cognizant", "brillio", "boldly", "cleardesk",
)
KNOWN_STAFFING_EMPLOYERS = (
    "teksystems", "tek systems", "actalent", "aerotek", "allegis", "randstad", "robert half",
    "kelly services", "kforce", "insight global", "apex systems", "motion recruitment", "cybercoders",
    "hays", "adecco", "manpower", "spherion", "express employment", "staffmark", "pridestaff",
    "aquent", "synergisticit", "she recruits", "creative circle", "digital people", "virtual coworker",
)
STAFFING_TEXT = [
    r"\bwe (?:recruit|place|staff|connect) (?:talent|candidates|professionals)\b",
    r"\bour (?:staffing|recruiting|recruitment|executive search) (?:services|solutions|agency)\b",
    r"\b(?:staffing|recruiting|recruitment|executive search) (?:firm|agency|company)\b",
    r"\brecruitment process outsourcing\b", r"\bRPO services\b",
    r"\b(?:the|our) client\b[^.\n]{0,140}\b(?:is seeking|is hiring|has engaged us|needs)\b",
    r"\b(?:one of )?our staffing partners?\b[^.\n]{0,160}\b(?:hire|hiring|role|position)\b",
]
OUTSOURCING_TEXT = [
    r"\bwe are (?:a|an) (?:global )?(?:business process outsourcing|bpo) (?:company|provider)\b",
    r"\bour (?:business process outsourcing|outsourcing) services\b",
    r"\bwe provide (?:virtual assistant|outsourced staffing|offshore staffing) services\b",
    r"\bwe (?:provide|deliver|offer) (?:outsourced )?(?:call|contact) center services\b",
    r"\bwe (?:provide|deliver|offer) outsourced (?:customer support|customer service|back[- ]office) services\b",
    r"\bmanaged (?:customer support|customer service|contact center) services for clients?\b",
    r"\bstaff augmentation (?:services|company|solutions)\b",
    r"\b(?:software|IT|business process) outsourcing (?:company|services|solutions)\b",
    r"\b(?:professional employer organization|peo services?|co[- ]employment|worksite employees?)\b",
]


def has_people_authority(description: str) -> Optional[str]:
    """Clause-scoped: returns the offending clause, or None."""
    for clause in (c.strip() for c in CLAUSE_SPLIT.split(description or "") if c.strip()):
        if PASSIVE_SUPERVISION.search(clause):
            continue
        if any(p.search(clause) for p in PEOPLE_AUTHORITY):
            return clause[:300]
    return None


def _norm_emp_values(value: object) -> List[str]:
    """Normalize scalar or multi-valued provider employment labels.

    Fantastic can return ``ai_employment_type`` as a list.  Stringifying that
    list collapsed values such as ``FULL_TIME`` + ``CONTRACTOR`` into one
    unrecognized token, allowing the posting to escape the downstream gate.
    Preserve every label so an explicit non-full-time value can win.
    """
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        out: List[str] = []
        for item in value:
            out.extend(_norm_emp_values(item))
        return list(dict.fromkeys(out))
    normalized = re.sub(r"[^a-z]", "", str(value).lower())
    return [normalized] if normalized else []


def extract_job_facts(
    *,
    title: Optional[str],
    description: Optional[str],
    employment_type: object = None,
    ai_employment_type: object = None,
    location_type: Optional[str] = None,
    countries: Sequence[str] = (),
    location_text: Optional[str] = None,
    employer_name: Optional[str] = None,
    agency_flag: Optional[bool] = None,
    org_industry: Optional[str] = None,
) -> JobFacts:
    """Facts first, exclusions second. Absent text yields UNKNOWN facts, never a guess."""
    jf = JobFacts()
    title = str(title or "").strip()
    desc = str(description or "")
    sents = sentences(f"{title}. {location_text or ''}. {desc}")
    emp_name_norm = re.sub(r"[^a-z0-9]+", " ", str(employer_name or "").lower()).strip()

    # active status
    hits = _matching(sents, NON_ACTIVE)
    if hits:
        jf.facts["active_status"] = Fact("active_status", False, TEXT, hits[0])
        jf.exclusions.append(Exclusion("active:non_active_posting", hits[0], TEXT))
    else:
        jf.facts["active_status"] = Fact("active_status", True, PROVIDER, "provider_active_feed")

    # employment
    emp_value, emp_excerpt, emp_status = None, "", UNKNOWN
    for value, patterns in EMPLOYMENT_NEGATIVES:
        hits = _matching(sents, patterns)
        if hits:
            emp_value, emp_excerpt, emp_status = value, hits[0], TEXT
            break
    if not emp_value:
        provider_values = list(dict.fromkeys(
            _norm_emp_values(ai_employment_type) + _norm_emp_values(employment_type)
        ))
        negative_values = {
            "parttime": "part_time", "contractor": "contract", "contract": "contract",
            "fixedterm": "fixed_term", "temporary": "temporary", "temp": "temporary",
            "freelance": "freelance", "seasonal": "seasonal", "intern": "internship",
            "internship": "internship", "volunteer": "volunteer", "unpaid": "unpaid",
            "fractional": "fractional", "other": "other",
        }
        # Provider filters are inclusion filters: a posting may be tagged both
        # FULL_TIME and CONTRACTOR.  Any explicit incompatible label wins.
        provider_negative = next((negative_values[v] for v in provider_values if v in negative_values), "")
        provider_excerpt = f"provider employment_type={ai_employment_type!r}; raw_employment_type={employment_type!r}"
        if provider_negative:
            emp_value, emp_excerpt, emp_status = provider_negative, provider_excerpt, PROVIDER
        elif "fulltime" in provider_values:
            emp_value, emp_excerpt, emp_status = "full_time", provider_excerpt, PROVIDER
    if not emp_value:
        hits = _matching(sents, FULL_TIME)
        if hits:
            emp_value, emp_excerpt, emp_status = "full_time", hits[0], TEXT
    jf.facts["employment_type"] = Fact("employment_type", emp_value, emp_status if emp_value else UNKNOWN, emp_excerpt)
    if emp_value and emp_value != "full_time":
        jf.exclusions.append(Exclusion(f"employment:{emp_value}", emp_excerpt, emp_status))

    # arrangement / deliverability
    arrangement, arr_excerpt = None, ""
    for value, patterns in (("field_work", FIELD), ("travel", TRAVEL_HARD), ("hybrid", HYBRID), ("onsite", ONSITE)):
        hits = _matching(sents, patterns)
        if hits:
            arrangement, arr_excerpt = value, hits[0]
            break
    if not arrangement:
        hits = _matching(sents, REMOTE)
        lt = str(location_type or "").strip().lower()
        if hits or lt == "remote":
            arrangement, arr_excerpt = "remote", (hits[0] if hits else f"provider location_type={lt}")
        elif lt in {"hybrid", "onsite", "on-site"}:
            arrangement, arr_excerpt = lt.replace("on-site", "onsite"), f"provider location_type={lt}"
    jf.facts["work_arrangement"] = Fact("work_arrangement", arrangement, TEXT if arrangement else UNKNOWN, arr_excerpt)
    if arrangement in {"field_work", "travel"}:
        jf.exclusions.append(Exclusion(f"deliverability:{arrangement}", arr_excerpt, TEXT))

    for name, patterns, reason in (
        ("security_clearance", CLEARANCE, "deliverability:security_clearance"),
        ("professional_license", LICENSE, "deliverability:professional_license"),
        ("physical_facility", FACILITY, "deliverability:physical_facility"),
    ):
        hits = _matching(sents, patterns)
        if name == "physical_facility":
            hits = [s for s in hits if not (
                re.search(r"\bexperience (?:in |with )?(?:bedside|patient care)\b", s, re.I)
                and not any(re.search(p, s, re.I) for p in FACILITY[:-1]))]
            # Incidental office lifting is not evidence that the role itself is
            # physical. Keep independent duties such as reception or machinery.
            hits = [s for s in hits if not (
                re.search(r"\blight lifting\s+(?:up to\s+)?(?:1\d|20)\s*(?:lbs|pounds)\b", s, re.I)
                and not any(re.search(p, s, re.I) for p in FACILITY if "(?:lift|lifting)" not in p))]
        jf.facts[name] = Fact(name, "required" if hits else None, TEXT if hits else UNKNOWN, hits[0] if hits else "")
        if hits:
            jf.exclusions.append(Exclusion(reason, hits[0], TEXT))

    # market
    foreign = _matching(sents, FOREIGN_ONLY)
    us_text = _matching(sents, US_SCOPE)
    us_provider = any(str(c or "").strip().upper() in {"US", "USA", "UNITED STATES"} for c in countries)
    if foreign:
        jf.facts["intent_market"] = Fact("intent_market", "foreign_only", TEXT, foreign[0])
        jf.exclusions.append(Exclusion("market:foreign_only", foreign[0], TEXT))
    elif us_text:
        jf.facts["intent_market"] = Fact("intent_market", "us_market", TEXT, us_text[0])
    elif us_provider:
        jf.facts["intent_market"] = Fact("intent_market", "us_market", PROVIDER, f"provider countries={list(countries)}")
    else:
        jf.facts["intent_market"] = Fact("intent_market", None, UNKNOWN)

    # role level: title (when present) and role statements in the text.
    #
    # Phase 2 audit task 1 (2026-09-19, Luis): a leadership/principal title is a FACT
    # for later decision-maker mapping. It is never, on its own, a reason to reject the
    # job -- measured 397 decisive rejects on this alone, 54.5% of a labelled sample
    # invalid, ~217 valid jobs lost per pool. Do not resurrect the old
    # `Exclusion("seniority:leadership_or_principal", ...)` append below; campaign fit
    # and actual responsibilities decide eligibility, not the title word. The pre-audit
    # behaviour is only replayable from git history (this file before this commit) or
    # the counterfactual harness -- this module has no policy switch to invent one.
    level_excerpt = ""
    if title and TITLE_LEADERSHIP.search(title):
        level_excerpt = title
    m = ROLE_LEVEL_IN_TEXT.search(desc)
    if not level_excerpt and m:
        level_excerpt = m.group(0)
    if level_excerpt:
        jf.facts["role_level"] = Fact("role_level", "leadership_or_principal", TEXT, level_excerpt, RULE_VERSION)
    else:
        jf.facts["role_level"] = Fact("role_level", "ic_or_manager_unstated", TEXT if title else UNKNOWN, title, RULE_VERSION)
    # Phase 2 audit task 2 (2026-09-19, Luis): managing people, owning direct reports,
    # or a bare "Supervisory Responsibilities" heading is a FACT for later
    # decision-maker mapping, never on its own a reason to reject the job -- measured
    # 374 decisive rejects on this alone (57 on a bare heading, 16 of those explicitly
    # negated), FN rate 21.4%, ~80 valid jobs lost per pool. `has_people_authority`
    # (the clause-scoped extractor) is unchanged; only the automatic exclusion below is
    # removed. As with task 1, this module has no policy switch, so the pre-audit
    # behaviour is only replayable from git history or the counterfactual harness.
    authority = has_people_authority(desc)
    jf.facts["people_management"] = Fact("people_management", bool(authority), TEXT if authority else UNKNOWN, authority or "", RULE_VERSION)

    # programmes / government
    head = desc[:2500]
    for reason, pattern in PROGRAM_PATTERNS.items():
        if (title and re.search(pattern, title, re.I)) or re.search(
                rf"\b(?:this is|join|apply for|seeking|hiring for)\b[^.\n]{{0,80}}{pattern}", head, re.I):
            jf.exclusions.append(Exclusion(f"program:{reason}", title or head[:200], TEXT))
            break
    if title and GOVERNMENT_TITLE.search(title):
        jf.exclusions.append(Exclusion("government:public_sector_title", title, TEXT))

    # agency / outsourcing employer
    if agency_flag is True:
        jf.exclusions.append(Exclusion("agency:provider_recruitment_agency_flag", str(employer_name or ""), PROVIDER))
    for known in KNOWN_OUTSOURCING_EMPLOYERS + KNOWN_STAFFING_EMPLOYERS:
        if emp_name_norm and (emp_name_norm == known or re.search(r"\b" + re.escape(known) + r"\b", emp_name_norm)):
            jf.exclusions.append(Exclusion("agency:known_intermediary_employer", known, TEXT))
            break
    if emp_name_norm and re.search(r"\b(?:call|contact) center\b|\bbpo\b|\boutsourc(?:ing|ed)\b|\bstaffing\b|\brecruit(?:ing|ment)\b", emp_name_norm):
        jf.exclusions.append(Exclusion("agency:service_model_in_employer_name", emp_name_norm, TEXT))
    for patterns, reason in ((STAFFING_TEXT, "agency:staffing_text"), (OUTSOURCING_TEXT, "agency:outsourcing_text")):
        hits = _matching(sents, patterns)
        if hits:
            jf.exclusions.append(Exclusion(reason, hits[0], TEXT))
            break
    industry = str(org_industry or "").strip().lower()
    if industry in {"staffing and recruiting", "staffing & recruiting", "human resources services", "outsourcing/offshoring", "outsourcing and offshoring consulting"}:
        jf.exclusions.append(Exclusion("agency:provider_industry", industry, PROVIDER))

    jf.facts["description_present"] = Fact("description_present", len(desc.strip()) >= 200, TEXT if desc.strip() else UNKNOWN, f"{len(desc.strip())} chars")
    return jf
