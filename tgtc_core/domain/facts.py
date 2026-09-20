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
    #: Phase 2 audit tasks 4-5 (2026-09-19, Luis): a record can need human/paid-step
    #: review WITHOUT being excluded -- contradictory employment evidence, or a
    #: company-size conflict between reliable sources. Each entry is a
    #: "<stage>:<code>" string (mirrors Exclusion.reason's own convention), empty by
    #: default. Never read to decide exclusion; it is its own reported bucket.
    review_reasons: List[str] = field(default_factory=list)

    def get(self, name: str) -> Fact:
        return self.facts.get(name, Fact(name, None, UNKNOWN))

    @property
    def excluded(self) -> bool:
        return bool(self.exclusions)

    @property
    def employment(self) -> str:
        """The employment_type Fact's value, or "unknown" -- convenience accessor
        matching the vocabulary `size_state()` also uses (a plain string, not a
        `Fact`), since contradictory evidence deliberately leaves this Fact's
        value at None (see extract_job_facts's employment block)."""
        fact = self.facts.get("employment_type")
        if fact is None or fact.value is None:
            return "unknown"
        return str(fact.value)

    def to_dict(self) -> Dict[str, object]:
        return {
            "facts": {k: {"value": v.value, "status": v.status, "excerpt": v.excerpt[:300], "rule_version": v.rule_version}
                     for k, v in self.facts.items()},
            "exclusions": [{"reason": e.reason, "excerpt": e.excerpt[:300], "source": e.source, "rule_version": e.rule_version}
                          for e in self.exclusions],
            "review_reasons": list(self.review_reasons),
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
#: Phase 2 audit task 4 (2026-09-19, Luis): measured defect -- a bare "fixed term"
#: mention that only qualifies an at-will/introductory/probationary CLAUSE (not the
#: position's own employment type) trips the employment rule today. Mirrors task
#: 3's FACILITY_LIFT_INCIDENTAL narrowing: the explicit "N-month contract/term"
#: phrasing (EMPLOYMENT_NEGATIVES's other fixed_term pattern) is untouched and
#: still excludes on its own.
FIXED_TERM_INCIDENTAL = re.compile(
    r"\bat[- ]will\b|\b(?:introductory|probationary|trial|onboarding)\s+period\b", re.I)
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
#: Phase 3 audit (2026-09-20, Luis D5): "Only an actual security clearance
#: (Secret/TS/SCI) or an explicitly stated federal clearance requirement excludes. A
#: Public Trust determination, a Tier 1 investigation, an HSPD-12 PIV card and ordinary
#: background checks do NOT." The approved exclusion is "required federal clearance",
#: and a Public Trust determination is a suitability finding, not a clearance.
#:
#: The bare ``\bpublic trust(?: clearance)?\b`` pattern that used to sit here is
#: therefore gone. It was also the rule's one pure substring match: the frozen rubric
#: §4.H names "the phrase 'public trust' used in a non-clearance sense ('building
#: public trust')" as explicitly NOT sufficient, and the corpora contain exactly that
#: (a Public Information Officer "building public trust and community engagement").
#: Measured on the two frozen corpora (7,349 rows): 46 rows are excluded on that
#: substring with NO other clearance evidence anywhere in the posting; another 20
#: mention public trust beside a real clearance and still exclude on the real one.
#:
#: Narrowing only. A posting that states Secret/Top Secret/TS/SCI, or an explicit
#: requirement to hold or obtain a security clearance, is untouched.
CLEARANCE = [
    r"\b(?:active |current )?(?:secret|top secret|ts/sci|security) clearance (?:is )?(?:required|mandatory|needed)\b",
    r"\b(?:ability|eligible|required|must(?: be able)?|willing) to\b[^.;]{0,160}\b(?:obtain|maintain)\b[^.;]{0,120}\b(?:secret|top secret|ts/sci|security) clearance\b",
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
#: Phase 2 audit task 3 (2026-09-19, Luis): ADA-boilerplate lifting language is not
#: evidence the JOB is physical. Measured: of 541 decisive physical_facility rejects,
#: the matched spans were lift/lifting NN lbs 390, forklift/pallet jack 150, and one real
#: clinical duty; FN rate 7.7%, ~42 valid jobs lost per pool. This narrows the
#: lift/lifting pattern only -- forklift/pallet jack and the other FACILITY spans are
#: untouched, so a genuine physical core duty still excludes.
FACILITY_LIFT_INCIDENTAL = re.compile(
    r"\b(?:occasionally|occasional|as needed|from time to time|infrequently|rarely|"
    r"with (?:or without )?(?:reasonable )?accommodations?)\b", re.I)

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

# --- company size (Phase 2 audit task 5, 2026-09-19, Luis) -------------------

#: Target company size, 25-1,000 employees. Two independently populated provider
#: fields (a company's own headcount and its declared size BAND) can and do
#: disagree -- measured 1,436 rows where headcount is inside 25-1,000 while the
#: declared band is above 1,000 (71 rows the other way), on top of 216 too_large
#: rejects from a single-field read. A conflict is never silently resolved in
#: either direction: it is its own bucket (`firmographic_conflict` /
#: `unknown_firmographics`), never a reject and never a confirmed in-range
#: company, unless a free (non-paid) resolution succeeds first.
TARGET_MIN_EMPLOYEES = 25
TARGET_MAX_EMPLOYEES = 1000

_SIZE_BAND_RANGE = re.compile(r"^\s*(\d[\d,]*)\s*-\s*(\d[\d,]*)\s*(?:employees?)?\s*$", re.I)
_SIZE_BAND_PLUS = re.compile(r"^\s*(\d[\d,]*)\s*\+\s*(?:employees?)?\s*$", re.I)
_SIZE_BAND_SINGLE = re.compile(r"^\s*(\d[\d,]*)\s*employees?\s*$", re.I)

#: Cheapest-resolution step 1 (task 5): an employee count the EMPLOYER states
#: about itself in the posting text. Deliberately narrow self-description
#: phrasing only -- never a bare number near "employees" (which also matches
#: eligibility-threshold boilerplate unrelated to the company's own size).
STATED_HEADCOUNT_PATTERNS = [
    r"\b(?:we (?:are|have)|team of|company of|organization of|organisation of|staff of|"
    r"workforce of|employs?)\s+(?:approximately |about |over |more than |roughly )?"
    r"(\d{1,3}(?:,\d{3})*)\+?\s*(?:people|employees|team members|staff|professionals)\b",
    r"\b(\d{1,3}(?:,\d{3})*)\+?\s*(?:employees|team members)\s+"
    r"(?:worldwide|globally|across|strong|company[- ]wide)\b",
]


def _to_int(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_size_band(band: Optional[str]) -> Optional[Tuple[int, Optional[int]]]:
    """A provider size-band string ("201-500", "1,001-5,000 employees", "10,001+")
    to a numeric (low, high) range. ``high=None`` means open-ended. Returns None
    when absent or not one of the recognised shapes -- an unparsable band is not
    a reliable populated source.
    """
    s = str(band or "").strip()
    if not s:
        return None
    m = _SIZE_BAND_RANGE.match(s)
    if m:
        lo, hi = _to_int(m.group(1)), _to_int(m.group(2))
        return (lo, hi) if lo is not None and hi is not None else None
    m = _SIZE_BAND_PLUS.match(s)
    if m:
        lo = _to_int(m.group(1))
        return (lo, None) if lo is not None else None
    m = _SIZE_BAND_SINGLE.match(s)
    if m:
        lo = _to_int(m.group(1))
        return (lo, lo) if lo is not None else None
    return None


def _classify_size_range(lo: int, hi: Optional[int], *, min_employees: int = TARGET_MIN_EMPLOYEES,
                         max_employees: int = TARGET_MAX_EMPLOYEES) -> str:
    """inside / outside / indeterminate against the [min_employees, max_employees]
    target for ONE source's numeric range. A range that straddles a target
    boundary (e.g. "501-2,000") is indeterminate, not a vote either way.

    Fix round 1, I1 (IMPORTANT, independent review): ``min_employees``/
    ``max_employees`` are keyword-only overrides of the module defaults (25,
    1,000) precisely so a caller that already reads the SAME two numbers from
    ``policy.requirements.rule("min_employees"/"max_employees")`` -- the live
    gates in ``services/opportunity.py`` and ``domain/approval.py`` -- can
    pass those values through instead of silently forking onto a hardcoded
    copy `describe()`'s policy manifest does not govern. The defaults keep
    every caller that has no `rule()` access (this module itself, and the
    NOT-live ``domain/candidate_qualification.py``) working unchanged.
    """
    if hi is not None and lo >= min_employees and hi <= max_employees:
        return "inside"
    if hi is not None and (hi < min_employees or lo > max_employees):
        return "outside"
    if hi is None and lo > max_employees:
        return "outside"
    return "indeterminate"


def _size_votes(headcount: Optional[object] = None, size_band: Optional[str] = None, *,
                min_employees: int = TARGET_MIN_EMPLOYEES, max_employees: int = TARGET_MAX_EMPLOYEES) -> List[str]:
    """One inside/outside/indeterminate vote per POPULATED reliable source, in a
    fixed order (headcount, then declared size band).

    Extracted so ``size_state`` (what the sources say) and
    ``size_sources_agreeing`` (how many of them say it) read the SAME votes --
    final whole-branch review C3 asked for a corroboration count, and a second
    vote-building loop beside this one is exactly the copied predicate
    reviewers have rejected twice on this branch.
    """
    votes: List[str] = []
    hc = _to_int(headcount)
    if hc is not None:
        votes.append(_classify_size_range(hc, hc, min_employees=min_employees, max_employees=max_employees))
    band_range = parse_size_band(size_band) if size_band not in (None, "") else None
    if band_range is not None:
        votes.append(_classify_size_range(*band_range, min_employees=min_employees, max_employees=max_employees))
    return votes


#: How many independent, populated, DETERMINATE company-size sources must agree
#: before a size may be called confirmed -- reported in the ``approved_confirmed_size``
#: KPI, and asserted as fact in an Airtable field or an outbound email variable.
MIN_CORROBORATING_SIZE_SOURCES = 2


def size_sources_agreeing(headcount: Optional[object] = None, size_band: Optional[str] = None, *,
                          min_employees: int = TARGET_MIN_EMPLOYEES,
                          max_employees: int = TARGET_MAX_EMPLOYEES) -> int:
    """How many reliable populated sources DETERMINATELY back the verdict
    ``size_state`` returned for the same inputs; 0 when they clash or when none
    is determinate.

    0 is therefore compatible with an ``in_range`` STATE: ``resolve_company_size``
    can settle a clash from the employer's own stated headcount, and a settled
    clash is a decision, not corroboration. Migration 010 documents the same
    thing for the stored column.

    Final whole-branch review, C3 (CRITICAL, 2026-09-20): ``size_state`` answers
    a range question ("do the sources put this employer inside 25-1,000?") and
    answers it correctly from one source. It was being read as if it answered a
    CONFIDENCE question, so ``size_state(400, None) == "in_range"`` -- the shape
    of nearly every live row, since migration 007 backfills no ``size_band`` and
    ``services/acquisition.py`` freezes it once ``enriched_at`` is set -- was
    counted as a confirmed 25-1,000 match and shipped as fact into CRM fields
    and outbound copy. This is the orthogonal second question, kept separate on
    purpose: a single-source ``in_range`` still PROCEEDS (Decision 2: never
    discard a potentially eligible company), it is simply not corroborated.
    """
    votes = _size_votes(headcount, size_band, min_employees=min_employees, max_employees=max_employees)
    determinate = [v for v in votes if v != "indeterminate"]
    if not determinate:
        return 0
    if "inside" in determinate and "outside" in determinate:
        return 0
    return len(determinate)


def size_corroborated(headcount: Optional[object] = None, size_band: Optional[str] = None, *,
                      min_employees: int = TARGET_MIN_EMPLOYEES, max_employees: int = TARGET_MAX_EMPLOYEES) -> bool:
    """THE "may this size be asserted as fact" predicate (C3): at least
    ``MIN_CORROBORATING_SIZE_SOURCES`` reliable populated sources determinately
    agree. Every caller that reports a size as confirmed -- the approvals KPI
    split, the Airtable/Instantly size fields, the founder-tier and
    campaign-size decisions in ``services/opportunity.py`` -- asks THIS
    function, never its own ``>= 2`` arithmetic."""
    return size_sources_agreeing(headcount, size_band, min_employees=min_employees,
                                 max_employees=max_employees) >= MIN_CORROBORATING_SIZE_SOURCES


def size_state(headcount: Optional[int] = None, size_band: Optional[str] = None, *,
               min_employees: int = TARGET_MIN_EMPLOYEES, max_employees: int = TARGET_MAX_EMPLOYEES) -> str:
    """Three-state, source-agnostic company-size decision (Decision 2, 2026-09-19):

    - all reliable populated sources agree inside [min_employees, max_employees] -> "in_range"
    - all reliable populated sources agree outside -> "out_of_range"
    - reliable sources conflict across the boundary -> "firmographic_conflict"
    - nothing usable (absent, or the lone source is itself indeterminate) ->
      "unknown_firmographics"

    Deliberately takes no job posting -- it is a pure function of whatever
    company-size sources are available, reusable outside `extract_job_facts`
    (e.g. task 5b's source-accuracy measurement). ``min_employees``/
    ``max_employees`` default to this module's own 25/1,000 constants; a
    caller with access to ``policy.requirements.rule(...)`` should pass those
    values explicitly (see ``_classify_size_range``'s docstring, fix round 1 I1).
    """
    votes = _size_votes(headcount, size_band, min_employees=min_employees, max_employees=max_employees)
    if not votes:
        return "unknown_firmographics"
    determinate = [v for v in votes if v != "indeterminate"]
    if any(v == "inside" for v in determinate) and any(v == "outside" for v in determinate):
        return "firmographic_conflict"
    if len(votes) == 1:
        return {"inside": "in_range", "outside": "out_of_range", "indeterminate": "unknown_firmographics"}[votes[0]]
    # 2+ populated sources, no inside/outside clash between them.
    if determinate and all(v == "inside" for v in determinate):
        return "in_range"
    if determinate and all(v == "outside" for v in determinate):
        return "out_of_range"
    return "unknown_firmographics"


def _extract_stated_headcount(text: str) -> Optional[int]:
    for pattern in STATED_HEADCOUNT_PATTERNS:
        m = re.search(pattern, text or "", re.I)
        if m:
            n = _to_int(m.group(1))
            if n is not None:
                return n
    return None


def resolve_company_size(
    headcount: Optional[object] = None,
    size_band: Optional[str] = None,
    *,
    description: str = "",
    company: Optional[Dict[str, object]] = None,
    min_employees: int = TARGET_MIN_EMPLOYEES,
    max_employees: int = TARGET_MAX_EMPLOYEES,
) -> Tuple[str, str, Optional[int]]:
    """``size_state(headcount, size_band)``, then the SAME free-only resolution
    (Decision 2, 2026-09-19) for a ``firmographic_conflict`` that
    ``extract_job_facts``'s own company block used to inline: (1) an employee
    count the employer states about itself in a posting description, (2) any
    other already-populated allow-listed field on ``company``
    (``COMPANY_SIZE_SURROGATE_FIELDS``). Never a paid call.

    Task 5c (2026-09-20, wiring): pulled out of ``extract_job_facts`` into its
    own function so it is THE one resolution path -- ``extract_job_facts``'s
    company block and every live company-size gate that wires the three-state
    policy in (``services/opportunity.py``, ``domain/approval.py``) call this
    instead of re-implementing it. Reviewers on this branch have twice
    rejected a copied predicate.

    ``min_employees``/``max_employees`` (fix round 1, I1): keyword-only,
    default to this module's own 25/1,000 -- pass ``rule("min_employees")``/
    ``rule("max_employees")`` explicitly from a caller that has that import,
    so the live gates stay governed by the SAME policy manifest `describe()`
    reports instead of a silently-forked hardcoded copy.

    Returns ``(state, excerpt, effective_headcount)``. ``effective_headcount``
    is whichever single numeric reading DECIDED a determinate state --
    ``headcount`` itself when it alone (or together with a non-conflicting
    band) was enough, the free-resolved value when a conflict was resolved,
    or a size-band bound/midpoint when only the band was populated and
    determinate on its own. It is for a caller that wants to report WHICH
    boundary an ``out_of_range`` verdict crossed (small vs large); it is
    never used to decide the state itself, only to describe one already
    decided by ``size_state``.
    """
    state = size_state(headcount, size_band, min_employees=min_employees, max_employees=max_employees)
    excerpt = f"headcount={headcount!r}; size_band={size_band!r}"
    effective = _to_int(headcount)
    if state == "firmographic_conflict":
        resolved, resolved_source = _extract_stated_headcount(description), "description_stated_headcount"
        if resolved is None and company:
            for key in COMPANY_SIZE_SURROGATE_FIELDS:
                value = company.get(key)
                if value in (None, ""):
                    continue
                cand = _to_int(value)
                if cand is None:
                    band = parse_size_band(str(value))
                    if band is not None and _classify_size_range(
                            *band, min_employees=min_employees, max_employees=max_employees) != "indeterminate":
                        lo, hi = band
                        cand = lo if hi is None else (lo + hi) // 2
                if cand is not None:
                    resolved, resolved_source = cand, f"provider_field:{key}"
                    break
        if resolved is not None and _plausible_headcount(resolved):
            resolved_state = size_state(resolved, None, min_employees=min_employees, max_employees=max_employees)
            if resolved_state in {"in_range", "out_of_range"}:
                state = resolved_state
                excerpt += f"; resolved via {resolved_source}={resolved}"
                effective = resolved
    elif state == "out_of_range" and effective is None:
        band_range = parse_size_band(size_band) if size_band not in (None, "") else None
        if band_range is not None:
            lo, hi = band_range
            effective = lo if hi is None else (lo + hi) // 2
    return state, excerpt, effective


def size_reject_reason(effective_headcount: Optional[int], *, min_employees: int = TARGET_MIN_EMPLOYEES) -> str:
    """The established too_small/too_large vocabulary (``services/opportunity.py``,
    ``domain/approval.py``, ``domain/candidate_qualification.py`` all already used
    these two reason strings before this task) for an ``out_of_range`` verdict,
    from whichever number decided it. ``min_employees`` (fix round 1, I1):
    keyword-only, defaults to this module's own 25 -- pass
    ``rule("min_employees")`` explicitly so the too_small/too_large boundary
    named in the reason matches the SAME rule the state itself was decided
    against. Falls back to a generic reason only when no single number is
    available to name a side (state was decided by two
    non-conflicting-but-unresolvable-to-a-number votes -- not reachable from
    ``resolve_company_size`` today, kept only so this is total)."""
    if effective_headcount is None:
        return "employer_size_out_of_range"
    return "employer_too_small" if effective_headcount < min_employees else "employer_too_large"


#: Fix round 1 (2026-09-19, CRITICAL): fields on the `company` mapping that
#: plausibly denote an employee count, for free-resolution step 2 (an
#: already-populated PROVIDER field, not the employer's own text statement --
#: step 1 above). Iterating `company.items()` with no semantic filter treated
#: ANY numeric-parseable field (founded_year, a follower count, a phone-number
#: fragment) as a headcount surrogate, silently able to flip a
#: firmographic_conflict into a false in_range or, worse, a false
#: out_of_range reject -- exactly what Decision 2 forbids ("never discard a
#: potentially eligible company just because two sources conflict"). A tuple,
#: not a set, so the resolution order among surrogate fields is deterministic.
#:
#: Task 5c (2026-09-20, wiring): this tuple was invented -- ``employee_count``,
#: ``headcount_estimate``, ``staff_count``, ``num_employees`` -- and no real
#: Fantastic payload carries any of them (checked against
#: ``C:\TGTC\tgtc_canary_evidence\run_20260919T061752Z\net_new_rows.jsonl.gz``
#: and every ``phase3_paid\records\*.jsonl.gz`` file: the only two size-shaped
#: keys present anywhere are ``org_linkedin_headcount`` and
#: ``org_linkedin_size``, already read as the PRIMARY two sources below, not
#: surrogates). The allow-list is deliberately left empty rather than
#: re-populated with a guess -- add a real field name here only once a
#: payload is observed to carry a genuine third size-bearing field distinct
#: from the two primary sources.
COMPANY_SIZE_SURROGATE_FIELDS: Tuple[str, ...] = ()

#: A plausible employee-count value, applied to the resolved candidate
#: regardless of which step produced it (an allow-listed field can still hold a
#: garbage value, e.g. the phone-number-fragment example above).
def _plausible_headcount(n: Optional[int]) -> bool:
    return n is not None and 1 <= n <= 10_000_000


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
    company: Optional[Dict[str, object]] = None,
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

    # employment (Phase 2 audit task 4, 2026-09-19): the employer's own explicit
    # statement about the position and a provider's structured tag are two
    # independent signals. Measured: the deployed full-time filter removed 16.8%
    # of US LinkedIn / 21.2% of US ATS postings (almost none missing values), and
    # an incidental "fixed term" mention on an at-will/introductory clause tripped
    # the rule on an otherwise full-time posting (see FIXED_TERM_INCIDENTAL).
    # Luis's decision: an explicit statement of an EXCLUDED type (either source)
    # still excludes, unconditionally; a provider tag that CONTRADICTS an
    # explicit FULL-TIME statement produces unknown + a review reason, never an
    # automatic rejection.
    text_negative_value, text_negative_excerpt = None, ""
    for value, patterns in EMPLOYMENT_NEGATIVES:
        hits = _matching(sents, patterns)
        if value == "fixed_term":
            hits = [s for s in hits if not (
                re.search(r"\bfixed[- ]term\b", s, re.I) and FIXED_TERM_INCIDENTAL.search(s)
                and not re.search(r"\b\d{1,2}[- ]month\s+(?:contract|term)\b", s, re.I))]
        if hits:
            text_negative_value, text_negative_excerpt = value, hits[0]
            break
    text_full_time_hits = _matching(sents, FULL_TIME)
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
    # FULL_TIME and CONTRACTOR. Any explicit incompatible label wins.
    provider_negative = next((negative_values[v] for v in provider_values if v in negative_values), "")
    provider_full_time = "fulltime" in provider_values
    provider_excerpt = f"provider employment_type={ai_employment_type!r}; raw_employment_type={employment_type!r}"

    emp_value, emp_excerpt, emp_status = None, "", UNKNOWN
    if text_negative_value:
        # An explicit employer statement of an excluded type always excludes,
        # independent of any provider tag ("an explicitly excluded type still
        # excludes").
        emp_value, emp_excerpt, emp_status = text_negative_value, text_negative_excerpt, TEXT
    elif text_full_time_hits and provider_negative:
        # Explicit full-time statement contradicted by a provider tag naming an
        # excluded type: contradictory evidence becomes unknown/review, never an
        # automatic rejection. Resolved in neither direction here.
        jf.review_reasons.append("role:employment:conflicting_provider_tag")
    elif provider_negative:
        emp_value, emp_excerpt, emp_status = provider_negative, provider_excerpt, PROVIDER
    elif provider_full_time or text_full_time_hits:
        if text_full_time_hits:
            emp_value, emp_excerpt, emp_status = "full_time", text_full_time_hits[0], TEXT
        else:
            emp_value, emp_excerpt, emp_status = "full_time", provider_excerpt, PROVIDER
    # Fix round 1 (2026-09-19, IMPORTANT): employment is a changed decision path
    # in this batch (task 4's priority restructuring + FIXED_TERM_INCIDENTAL
    # narrowing) and must carry RULE_VERSION like tasks 1-3's changed paths and
    # task 5's own company_size Fact/Exclusion already do -- on the WHOLE
    # employment_type Fact/Exclusion, not conditionally per row, mirroring how
    # task 3 stamps every physical_facility Fact/Exclusion regardless of
    # whether that specific row's outcome was actually altered by the change.
    jf.facts["employment_type"] = Fact("employment_type", emp_value, emp_status if emp_value else UNKNOWN, emp_excerpt, RULE_VERSION)
    if emp_value and emp_value != "full_time":
        jf.exclusions.append(Exclusion(f"employment:{emp_value}", emp_excerpt, emp_status, RULE_VERSION))

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
            # Task 3 (2026-09-19): ADA-boilerplate ("occasionally", "as needed", "with or
            # without reasonable accommodation") on a lift/lifting clause is incidental
            # boilerplate, not a physical core duty. Narrows the rule, does not delete
            # it -- forklift/pallet jack and the other FACILITY spans below still
            # exclude on their own.
            hits = [s for s in hits if not (
                re.search(FACILITY[3], s, re.I) and FACILITY_LIFT_INCIDENTAL.search(s)
                and not any(re.search(p, s, re.I) for p in FACILITY if p != FACILITY[3]))]
        # Changed decision paths carry the audit rule version on BOTH the Fact and the
        # Exclusion: physical_facility (phase 2 task 3) and security_clearance (phase 3,
        # Luis D5 -- a Public Trust determination is not a clearance).
        rv = RULE_VERSION if name in {"physical_facility", "security_clearance"} else ""
        jf.facts[name] = Fact(name, "required" if hits else None, TEXT if hits else UNKNOWN, hits[0] if hits else "", rv)
        if hits:
            jf.exclusions.append(Exclusion(reason, hits[0], TEXT, rv))

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

    # company size (Phase 2 audit task 5, 2026-09-19; wired live, task 5c,
    # 2026-09-20): only computed when the caller supplies `company` -- every
    # caller before task 5c omitted it, so an absent/empty `company` stays
    # fully backward compatible (no Fact/Exclusion/review_reason). The real
    # Fantastic field names are `org_linkedin_headcount` (int) and
    # `org_linkedin_size` (a band string like "201-500 employees") -- verified
    # against saved provider rows; see COMPANY_SIZE_SURROGATE_FIELDS's own
    # comment. A caller can pass either the raw provider org block (its own
    # field names) or a pre-mapped dict with the same two keys.
    if company:
        headcount = company.get("org_linkedin_headcount")
        size_band = company.get("org_linkedin_size")
        state, excerpt, _effective = resolve_company_size(headcount, size_band, description=desc, company=company)
        jf.facts["company_size"] = Fact("company_size", state, PROVIDER, excerpt, RULE_VERSION)
        if state == "out_of_range":
            jf.exclusions.append(Exclusion("company:size:out_of_range", excerpt, PROVIDER, RULE_VERSION))
        elif state in {"firmographic_conflict", "unknown_firmographics"}:
            # Its own reported review/unknown bucket -- never a reject, never
            # counted as a confirmed 25-1,000 company (Decision 2).
            jf.review_reasons.append(f"company:{state}")

    jf.facts["description_present"] = Fact("description_present", len(desc.strip()) >= 200, TEXT if desc.strip() else UNKNOWN, f"{len(desc.strip())} chars")
    return jf
