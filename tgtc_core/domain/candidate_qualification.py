"""Candidate job-level qualification policy. Feature flag ``TGTC_CANDIDATE_QUALIFICATION=1``; OFF by default.

Isolated candidate for the approved requirements of 2026-09-19, including the business decisions
confirmed with the provider-strategy canary brief (policy ``tgtc-candidate/2``): explicit federal
clearance, government employers, quota-carrying sales and essential field travel are hard exclusions;
incidental security/trust wording, product companies selling to government, incidental travel,
seniority and people management are not. It does NOT replace
``domain.classification`` / ``domain.facts`` and nothing in production imports it. It reuses the
production fact extraction and responsibility lexicon and changes only the decisions those
requirements contradict. Every change is a named step, so a replay can apply one change at a
time; with no steps enabled the outcome is identical to the current canary qualifier
(``services.daily_24h_canary.qualify_row``).

Principles:

* Deterministic only. No model is consulted and no model confidence is read. Provider AI fields
  (``ai_taxonomies_a``, ``ai_core_responsibilities`` ...) never approve a job; the employer's own
  title and description decide the role.
* A hard exclusion needs quoted evidence of an approved rule. Anything uncertain or contradictory is
  PRESERVED as ``review:*`` (never silently rejected) or carried as a flag on a qualified job.
* Qualification is job-level and pre-contact. A qualified job enters contact discovery; failing to
  find a usable decision maker is a contact-stage outcome, not a qualification rejection.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from .classification import LEXICON, _dominant, score_functions
from .employer_attribution import employer_attribution_conflict
from .facts import (
    EMPLOYMENT_NEGATIVES, FACILITY, NON_ACTIVE, OUTSOURCING_TEXT, PROGRAM_PATTERNS, STAFFING_TEXT, TRAVEL_HARD,
    extract_job_facts, sentences,
)
from .identity import employer_anchors, employer_key, name_key, normalize_company_name, posting_canonical_key
from ..policy.requirements import excluded_industry, rule

FLAG_ENV = "TGTC_CANDIDATE_QUALIFICATION"
POLICY_VERSION = "tgtc-candidate/2"

#: The four TGTC offer groups and the production function keys (= Instantly campaigns) that serve them.
GROUPS: Dict[str, Tuple[str, ...]] = {
    "ai_engineering_automation": ("engineering",),
    "gtm_revops_salesops": ("gtm_revenue",),
    "marketing_creative": ("marketing",),
    "customer_success_support": ("customer_success", "customer_support"),
}
GROUP_BY_FUNCTION: Dict[str, str] = {fn: g for g, fns in GROUPS.items() for fn in fns}
OUT_OF_GROUP_FUNCTIONS = tuple(fn for fn in LEXICON if fn not in GROUP_BY_FUNCTION)

# --- policy steps (one change each) -------------------------------------------------------------
SENIORITY_MANAGEMENT = "seniority_and_people_management_as_flags"
ROLE_MAPPING = "title_plus_responsibility_group_mapping"
PHYSICAL_EVIDENCE = "physical_rejection_requires_duty_evidence"
EMPLOYMENT_PRECISION = "employment_and_activity_precision"
INTERMEDIARY_PRECISION = "intermediary_text_precision"
FIELD_PRECISION = "field_work_precision"
TRAVEL_PRECISION = "travel_rejection_requires_majority_travel"
ELIGIBILITY_FLAGS = "federal_clearance_excluded_license_public_sector_flags"
INDUSTRY_LABELS = "approved_industry_list_current_linkedin_labels"
CRM_EXCLUSION = "crm_company_exclusion"
GOVERNMENT_EMPLOYER = "government_employer_excluded"
QUOTA_SALES = "quota_carrying_sales_outside_gtm"
ESSENTIAL_FIELD = "essential_field_territory_travel_excluded"
THIRD_PARTY_REPOST = "third_party_repost_excluded"
#: Alias kept for replay scripts written against tgtc-candidate/1.
ELIGIBILITY = ELIGIBILITY_FLAGS

ALL_STEPS: Tuple[str, ...] = (
    SENIORITY_MANAGEMENT, ROLE_MAPPING, PHYSICAL_EVIDENCE, EMPLOYMENT_PRECISION, INTERMEDIARY_PRECISION,
    FIELD_PRECISION, TRAVEL_PRECISION, ELIGIBILITY_FLAGS, INDUSTRY_LABELS, CRM_EXCLUSION,
    GOVERNMENT_EMPLOYER, QUOTA_SALES, ESSENTIAL_FIELD, THIRD_PARTY_REPOST,
)


def candidate_enabled(env: Optional[Mapping[str, str]]) -> bool:
    return str((env or {}).get(FLAG_ENV, "") or "").strip() == "1"


# --- helpers ------------------------------------------------------------------------------------

def _rx(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern, re.I)


def _first(sents: Sequence[str], pattern: "re.Pattern[str]", skip: Optional["re.Pattern[str]"] = None) -> str:
    """First match whose surrounding clause (80 chars before, 60 after) is not incidental."""
    for s in sents:
        for m in pattern.finditer(s):
            if skip and skip.search(s[max(0, m.start() - 80):m.end() + 60]):
                continue
            start = max(0, m.start() - 100) if len(s) > 300 else 0
            return s[start:start + 300]
    return ""


def _any_first(sents: Sequence[str], patterns: Iterable[str], skip: Optional["re.Pattern[str]"] = None) -> str:
    for p in patterns:
        hit = _first(sents, _rx(p), skip)
        if hit:
            return hit
    return ""


def _bool(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# A sentence that only states a preference, prior experience, a negation, or someone else's work is
# not evidence that THIS job requires the thing it mentions.
INCIDENTAL = _rx(
    r"\b(?:preferred|a plus|is a plus|nice to have|desired|desirable|bonus|not required|optional|"
    r"experience (?:with|in|at|as|working|operating|using)|\w+ experience|years of|background in|familiarity with|exposure to|knowledge of|"
    r"certification|certified|license|ability to obtain|may require|may include|occasional(?:ly)?|as needed|periodic(?:ally)?|"
    r"when visiting|from time to time|"
    r"our (?:software|platform|product|products|app|solution|solutions|customers|clients)|"
    r"(?:customers|clients) (?:in|across|such as)|serv(?:e|es|ing) (?:customers|clients|industries))\b")

# --- employment / activity precision ------------------------------------------------------------

_PART_TIME_HOURS = _rx(r"\b(?:under|up to|approximately|about)\s*(\d{1,2})\s*(?:hours|hrs)\s*(?:per|/|a)\s*week\b")
_BOTH_SCHEDULES = _rx(r"\b(?:part[- ]time and full[- ]time|full[- ]time and part[- ]time|full[- ]time (?:or|/) part[- ]time|part[- ]time (?:or|/) full[- ]time)\b")
_CONTRACT_INCIDENTAL = _rx(
    r"\bcontract (?:revenue|management|negotiation|negotiations|award|performance|renewals?|terms|value|vehicles?|"
    r"compliance|administration|manufactur\w*|research|lifecycle|review|pricing|documents?|language|drafting|analysis)\b"
    r"|\bnot (?:a|an) (?:contract|contractor|temporary)\b|\bnon[- ](?:temporary|contract)\b|\bmore than a temporary\b"
    r"|\bgovernment contract(?:s|or)?\b|\bcontract(?:s)? with\b")
_CONTINGENT = _rx(r"\bcontingent (?:up)?on (?:the )?(?:successful )?(?:contract )?award\b|\bpending contract award\b|\bpotential future position\b")
_CTH_WITH_PERMANENT = _rx(r"\bcontract[- ]to[- ]hire\b[^.]{0,80}\b(?:full[- ]time|permanent|direct[- ]hire)\b|\b(?:full[- ]time|permanent|direct[- ]hire)\b[^.]{0,80}\bcontract[- ]to[- ]hire\b")
_INTERNSHIP_EXPERIENCE = _rx(r"\bintern(?:ship)?s? (?:experience|or (?:work )?experience|and (?:work )?experience)\b|\b(?:prior|previous|relevant|completed) internships?\b|\bexperience (?:through|from|including) (?:an? )?internships?\b")
_FUTURE_GROWTH = _rx(r"\b(?:grow|growth|advance|advancement|career|promot\w*)\b[^.]{0,80}\bfuture opportunities\b|\bfuture opportunities\b[^.]{0,40}\b(?:within|to grow|for growth|for advancement)\b")
_PROVIDER_NEGATIVE = {"parttime": "part_time", "contractor": "contract", "contract": "contract", "fixedterm": "fixed_term",
                      "temporary": "temporary", "temp": "temporary", "freelance": "freelance", "seasonal": "seasonal",
                      "intern": "internship", "internship": "internship", "volunteer": "volunteer", "unpaid": "unpaid",
                      "fractional": "fractional", "other": "other"}


def _labels(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        out: List[str] = []
        for item in value:
            out.extend(_labels(item))
        return out
    norm = re.sub(r"[^a-z]", "", str(value).lower())
    return [norm] if norm else []


def _employment_decision(title: str, sents: Sequence[str], row: Mapping[str, Any]) -> Tuple[str, str, List[str]]:
    """(reason, evidence, flags). reason '' means no employment/activity exclusion is proven."""
    flags: List[str] = []
    for value, patterns in EMPLOYMENT_NEGATIVES:
        for pattern in patterns:
            for s in sents:
                m = re.search(pattern, s, re.I)
                if not m:
                    continue
                if value == "part_time":
                    hours = _PART_TIME_HOURS.search(s)
                    if hours and m.group(0) == hours.group(0) and int(hours.group(1)) >= 30:
                        continue  # "approximately 40 hours per week" is a full-time schedule
                    if _BOTH_SCHEDULES.search(s):
                        flags.append("employment:part_and_full_time_offered")
                        continue
                if value in {"contract", "temporary", "fixed_term"}:
                    if _CONTINGENT.search(s):
                        return "active:contingent_on_contract_award", s[:300], flags
                    if _CONTRACT_INCIDENTAL.search(s) and not re.search(r"\b(?:contract (?:role|position|job|assignment)|independent contractor|\d+[- ]month)\b", s, re.I):
                        continue
                    if _CTH_WITH_PERMANENT.search(s):
                        flags.append("employment:contract_to_hire_or_permanent")
                        continue
                if value == "internship" and _INTERNSHIP_EXPERIENCE.search(s):
                    continue
                return f"employment:{value}", s[:300], flags
    for s in sents:
        for pattern in NON_ACTIVE:
            if re.search(pattern, s, re.I) and not _FUTURE_GROWTH.search(s):
                return "active:non_active_posting", s[:300], flags
    labels = list(dict.fromkeys(_labels(row.get("ai_employment_type")) + _labels(row.get("employment_type"))))
    negatives = [_PROVIDER_NEGATIVE[v] for v in labels if v in _PROVIDER_NEGATIVE]
    if negatives:
        excerpt = f"provider employment_type={row.get('ai_employment_type')!r}; raw_employment_type={row.get('employment_type')!r}"
        if "fulltime" in labels:
            flags.append("employment:provider_tags_conflict")   # FULL_TIME and a negative label: preserve, flag
        else:
            return f"employment:{negatives[0]}", excerpt, flags
    head = " ".join(sents)[:2500]
    for reason, pattern in PROGRAM_PATTERNS.items():
        if re.search(pattern, title, re.I):
            return f"program:{reason}", title, flags
        m = re.search(rf"\b(?:this is|join|apply for|seeking|hiring for)\b[^.\n]{{0,80}}{pattern}", head, re.I)
        if m and not _INTERNSHIP_EXPERIENCE.search(head[max(0, m.start() - 40):m.end() + 60]):
            return f"program:{reason}", m.group(0)[:300], flags
    return "", "", flags


# --- intermediary precision ---------------------------------------------------------------------

_AGENCY_DISCLAIMER = _rx(
    r"\b(?:unsolicited|third[- ]party (?:agenc|recruit)|agency fees?|do(?:es)? not accept|will not (?:accept|pay)|"
    r"not (?:be )?responsible for (?:any )?(?:fees|agency)|identify themselves|no (?:agencies|recruiters)|"
    r"recruiting partner or staffing agency|staffing agency (?:does not|will not)|external recruiter)\b")
INTERMEDIARY_TEXT = [
    r"\bwe (?:recruit|place|staff) (?:talent|candidates|professionals)\b",
    r"\bour (?:staffing|recruiting|recruitment|executive search) (?:services|solutions|agency|firm)\b",
    r"\bwe are (?:a|an) (?:[\w-]+ ){0,3}(?:staffing|recruiting|recruitment|executive search|rpo) (?:firm|agency|company|partner)\b",
    r"\brecruitment process outsourcing\b", r"\bRPO services\b",
    r"\b(?:our|the) client,?\s+(?:[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,5},?\s+)?(?:is seeking|is hiring|is looking for|has engaged us|has retained us)\b",
    r"\b(?:submit|present|refer) (?:you|candidates|your (?:resume|profile|application)) (?:directly )?to (?:our|the) client\b",
    r"\bon behalf of (?:our|a|the) client\b",
    r"\b(?:one of )?our staffing partners?\b[^.\n]{0,160}\b(?:hire|hiring|role|position)\b",
]


def _intermediary(sents: Sequence[str], precision: bool) -> Tuple[str, str]:
    if not precision:
        for patterns, reason in ((STAFFING_TEXT, "agency:staffing_text"), (OUTSOURCING_TEXT, "agency:outsourcing_text")):
            hit = _any_first(sents, patterns)
            if hit:
                return reason, hit
        return "", ""
    hit = _any_first(sents, INTERMEDIARY_TEXT, skip=_AGENCY_DISCLAIMER)
    if hit:
        return "agency:staffing_text", hit
    hit = _any_first(sents, OUTSOURCING_TEXT, skip=_rx(r"\bexperience (?:working )?(?:with|in|at)\b"))
    return ("agency:outsourcing_text", hit) if hit else ("", "")


# --- field / physical / location-bound duty evidence --------------------------------------------

FIELD_ROLE = [
    r"\b(?:this|the|a) (?:[\w-]+ ){0,4}field[- ]based (?:role|position|job|individual contributor|sales|territory)\b",
    r"\b(?:remote,? )?field[- ]based (?:role|position|individual contributor|sales role|sales position)\b",
    r"\b(?:role|position) is (?:a |an )?(?:remote,? )?field[- ]based\b",
    r"\bregular(?:ly)? visit(?:ing)? (?:customer|client) sites\b",
    r"\b(?:work|working|perform\w*|stationed|based) (?:on|at) (?:customer|client) sites\b",
]
_FIELD_INCIDENTAL = _rx(r"\b(?:variety of roles|roles with a mix|depending on the role|colleagues|field-based industry|industry experience)\b")

#: Travel that IS the job: covering a territory in person, regular customer visits, driving to dealer or
#: customer locations. "Travel to client sites as needed" or conference travel stays incidental.
ESSENTIAL_FIELD_TRAVEL = [
    r"\btravel(?:s|ing|ling)?\b[^.]{0,30}\b(?:within|throughout|across|around) (?:the |your |an |a |their )?(?:assigned |designated |defined |sales )?(?:territory|territories|region|district)\b",
    r"\bregular(?:ly)? (?:joint )?(?:customer|client|account|dealer|office|site|store|partner) visits\b",
    r"\b(?:visit|visiting|visits|call on|calling on|calls on)\b[^.]{0,30}\b(?:customers|clients|accounts|dealers|dealerships|stores|offices|physicians|doctors|hcps|providers|practices|hospitals|retailers|distributors)\b[^.]{0,40}\b(?:in person|face[- ]to[- ]face|on[- ]site|within (?:the|your|an) (?:assigned )?territory|throughout (?:the|your) territory|in (?:the|your) territory|daily|weekly)\b",
    r"\btravel(?:ing|ling)? to (?:dealership|customer|client|store|retail|dealer|distributor)s?'? (?:locations|sites|facilities)\b",
    r"\b(?:cover|covering|manage|managing) (?:a |an |the )?(?:geographic|multi[- ]state|regional) territory\b[^.]{0,60}\b(?:travel|visit|drive|driving|in person)\b",
]
_ESSENTIAL_FIELD = [_rx(x) for x in ESSENTIAL_FIELD_TRAVEL]


def essential_field_travel(sents: Sequence[str]) -> str:
    for rx in _ESSENTIAL_FIELD:
        hit = _first(sents, rx, skip=INCIDENTAL)
        if hit:
            return hit
    return ""

#: Explicit duties that can only be performed in person. Deliberately duty-shaped (verb + physical
#: object) so a word such as "retail", "patient", "client site" or "lift" alone never rejects.
LOCATION_BOUND_DUTY = [
    r"\b(?:in[- ]home (?:appointments|consultations|visits|sales)|door[- ]to[- ]door|door knock\w*)\b",
    r"\b(?:on|across) the (?:retail|store|showroom|production|shop|warehouse|manufacturing) floor\b",
    r"\b(?:work|working|works|stationed|based) (?:in|at|on) (?:a|the|our) (?:retail )?(?:store|showroom floor|branch|dealership|restaurant|hotel|clinic|warehouse|plant|factory)\b",
    r"\b(?:greet|greeting|greets|welcome|welcoming|check(?:ing)? in) (?:visitors|guests|customers|patients|clients|members|residents) (?:in person|at the|as they)\b",
    r"\b(?:drive|driving|operate|operating) (?:a |an |the |company |commercial )?(?:truck|van|vehicle|bus|forklift|pallet jack|heavy equipment|machinery|crane|loader)\b",
    r"\b(?:deliver|delivering) (?:packages|freight|orders|parcels|groceries|meals|materials|equipment) (?:to|for)\b|\b(?:load|loading|unload|unloading) (?:trucks|trailers|freight|packages|pallets)\b",
    r"\b(?:install|installing|repair|repairing|servicing|maintain|maintaining|troubleshoot\w*) (?:and \w+ )?(?:hvac|machinery|vehicles|aircraft|engines|elevators|generators|appliances|plumbing|electrical (?:systems|panels|wiring)|solar panels|equipment on[- ]site|heavy equipment|industrial equipment)\b",
    r"\b(?:work|working|works|report|reporting|based|perform\w*|supervis\w*|visit\w*) (?:on |at |to )?(?:the |our |various |multiple )?(?:job|construction|project) ?sites?\b",
    r"\b(?:provide|providing|deliver|delivering|perform|performing)\b[^.]{0,40}\b(?:direct patient care|bedside care|hands-on patient care|nursing care|clinical care)\b",
    r"\b(?:administer|administering) (?:medications?|injections|vaccines)\b|\b(?:take|taking|record|recording) vital signs\b",
    r"\b(?:prepare|preparing|cook|cooking|serve|serving) (?:food|meals|drinks|beverages)\b",
    r"\b(?:clean|cleaning|sanitiz\w*|mop\w*|vacuum\w*) (?:rooms|floors|restrooms|facilities|equipment|buildings)\b",
    r"\b(?:patrol|patrolling|guard|guarding) (?:the )?(?:site|premises|grounds|property|building)\b",
    r"\b(?:care for|supervise) (?:children|infants|toddlers|residents|clients) (?:in|at|during)\b",
    r"\b(?:perform|performing|conduct|conducting) (?:laboratory|bench|wet[- ]lab) (?:experiments|work|procedures|tests)\b",
    r"\b(?:stock|stocking|restock\w*) (?:shelves|merchandise|inventory)\b|\bcash register\b|\boperate (?:the )?register\b",
    r"\bbehind (?:a|the) (?:parts |service |front |sales )?counter\b",
    r"\bmust be (?:physically )?(?:present|on[- ]site) (?:at|in) (?:the|our|a) (?:store|clinic|hospital|plant|site|facility|warehouse)\b",
]
_LOCATION_BOUND = [_rx(p) for p in LOCATION_BOUND_DUTY]


def location_bound_evidence(sents: Sequence[str]) -> str:
    for rx in _LOCATION_BOUND:
        hit = _first(sents, rx, skip=INCIDENTAL)
        if hit:
            return hit
    return ""


_TRAVEL_PCT = _rx(r"\btravel\b[^.%\d]{0,50}?(?:\d{2,3}\s*[-\u2013\u2014]\s*)?(\d{2,3})\s*%(?!\s*(?:onsite|on-site|remote|in[- ]office|work\b|local\b))"
                  r"|(?:\d{2,3}\s*%?\s*[-\u2013\u2014]\s*)?(\d{2,3})\s*%\s*(?:of (?:the |your )?time\s*)?(?:domestic |overnight |regional |national |international )?travel\b")
_TRAVEL_PRIMARY = _rx(r"\b(?:primary|main|essential) (?:responsibility|function|duty)\b[^.]{0,60}\btravel\b|\btravel (?:is|will be) (?:the |a )?(?:primary|majority|essential)\b")
_TRAVEL_VAGUE = _rx(r"\bfrequent travel\b|\btravel regularly\b|\bregular travel\b|\bmust live near (?:a|an) airport\b")


def _travel(sents: Sequence[str]) -> Tuple[str, List[str]]:
    """(hard-exclusion evidence, flags). Majority or primary travel is location-bound work;
    20-49% travel, or a frequency without a share, is preserved as a flag."""
    pct, pct_hit = 0, ""
    for s in sents:
        for m in _TRAVEL_PCT.finditer(s):
            value = int(m.group(1) or m.group(2))
            if 0 < value <= 100 and value > pct:
                pct, pct_hit = value, s[max(0, m.start() - 60):m.end() + 60]
    primary = _first(sents, _TRAVEL_PRIMARY)
    if pct >= 50:
        return pct_hit, []
    if primary:
        return primary, []
    flags: List[str] = []
    if pct >= 20:
        flags.append(f"substantial_travel:{pct}%")
    elif _first(sents, _TRAVEL_VAGUE):
        flags.append("travel_frequency_unspecified")
    return "", flags


def _physical_facility(sents: Sequence[str], precision: bool) -> Tuple[str, List[str]]:
    """Production FACILITY semantics, or with precision: lifting boilerplate and preferences are flags."""
    if not precision:
        return "", []
    flags: List[str] = []
    lifting = [p for p in FACILITY if "(?:lift|lifting)" in p]
    duties = [p for p in FACILITY if p not in lifting and "forklift" not in p]
    if _any_first(sents, lifting):
        flags.append("physical_demands_boilerplate")
    hit = _any_first(sents, duties, skip=INCIDENTAL)
    return hit or location_bound_evidence(sents), flags


# --- eligibility: clearance / licence / public sector title -------------------------------------

_PUBLIC_TRUST_PHRASE = _rx(r"\bpublic trust\b")
#: A federal clearance or suitability determination named as a condition of the job.
_FEDERAL_CLEARANCE = [_rx(x) for x in (
    r"\b(?:active|current|existing|interim)\s+(?:dod\s+|us\s+|u\.s\.\s+|federal\s+)?(?:secret|top[- ]secret|ts\s*/\s*sci|ts)\b",
    r"\b(?:secret|top[- ]secret|ts\s*/\s*sci|ts)\s+(?:level\s+)?(?:security\s+)?clearance\b|\bts\s*/\s*sci\b|\btop[- ]secret\b",
    r"\b(?:security|federal|government|dod|doe|agency)\s+(?:security\s+)?clearance\b",
    r"\bpublic trust\b[^.]{0,40}\b(?:clearance|determination|position|investigation|background|eligib\w*)\b",
    r"\b(?:obtain|maintain|hold|possess)\w*\b[^.]{0,60}\bpublic trust\b",
)]
_CLEARANCE_REQUIRED = _rx(r"\b(?:required|requires|require|requirement|mandatory|must|need(?:ed|s)?|ability to obtain|able to obtain|"
                          r"eligible (?:to|for)|eligibility|obtain and maintain|maintain|hold|possess|active|current|at (?:the )?time of hire|"
                          r"condition of employment|prior to start)\b")
_CLEARANCE_OPTIONAL = _rx(r"\b(?:preferred|a plus|is a plus|desired|desirable|nice to have|bonus|not required|advantage|advantageous|"
                          r"may (?:be )?requir\w*|might requir\w*|could requir\w*|some (?:contracts|positions|roles|projects))\b")
_CLAUSE_BREAK = _rx(r"[.;)\]]")
#: "Desired Skills" / "Preferred Qualifications" headings that FOLLOW a requirement belong to the next list.
_SECTION_HEADING = _rx(r"\b(?:desired|preferred|nice to have|bonus|additional) (?:skills|qualifications|experience|requirements)\b")
_CLEARANCE_TITLE = _rx(r"\b(?:ts\s*/\s*sci|top[- ]secret|secret clearance|security clearance|\(ts\)|ts clearance|clearance required)\b")


def federal_clearance(title: str, sents: Sequence[str]) -> Tuple[str, bool]:
    """(evidence of a REQUIRED federal clearance, whether a clearance is merely mentioned)."""
    if _CLEARANCE_TITLE.search(title or ""):
        return title, True
    mentioned = False
    for sent in sents:
        for rx in _FEDERAL_CLEARANCE:
            for m in rx.finditer(sent):
                window = sent[max(0, m.start() - 120):m.end() + 120]
                # optional wording must sit in the SAME clause as the clearance, not the previous item
                before = _CLAUSE_BREAK.split(sent[max(0, m.start() - 80):m.start()])[-1]
                after = _CLAUSE_BREAK.split(sent[m.end():m.end() + 40])[0]
                mentioned = True
                if _CLEARANCE_OPTIONAL.search(before) or (_CLEARANCE_OPTIONAL.search(after) and not _SECTION_HEADING.search(after)):
                    continue
                if _CLEARANCE_REQUIRED.search(window):
                    start = max(0, m.start() - 100) if len(sent) > 300 else 0
                    return sent[start:start + 300], True
    return "", mentioned
_PUBLIC_TRUST_CLEARANCE = _rx(r"\bpublic trust\b[^.]{0,40}\b(?:clearance|determination|position|investigation|background|eligib\w*)\b|\b(?:obtain|maintain|hold|possess)\w*\b[^.]{0,60}\bpublic trust\b")

# --- industries (approved list, current LinkedIn labels) ----------------------------------------

#: The twelve approved industry exclusions plus staffing/RPO, as the exact LinkedIn labels Fantastic returns
#: (``exclude_organization_industry`` is an exact, case-sensitive match). ``rename`` = the same industry under
#: LinkedIn's current (V2) label; ``child`` = a V2 sub-industry of an approved parent (government employers
#: and healthcare providers, approved 2026-09-19).
APPROVED_INDUSTRIES: Tuple[Tuple[str, str, str], ...] = (
    ("Government Administration", "government_administration", "exact"),
    ("Administration of Justice", "government_administration", "child"),
    ("Public Safety", "government_administration", "child"),
    ("Health and Human Services", "government_administration", "child"),
    ("Public Policy Offices", "government_administration", "child"),
    ("Armed Forces", "government_administration", "child"),
    ("Legislative Offices", "government_administration", "child"),
    ("Executive Offices", "government_administration", "child"),
    ("Courts of Law", "government_administration", "child"),
    ("Correctional Institutions", "government_administration", "child"),
    ("Fire Protection", "government_administration", "child"),
    ("Law Enforcement", "government_administration", "child"),
    ("Non-profit Organization Management", "nonprofit", "exact"),
    ("Nonprofit Organization Management", "nonprofit", "exact"),
    ("Non-profit Organizations", "nonprofit", "rename"),
    ("Hospital & Health Care", "hospitals_and_healthcare", "exact"),
    ("Hospitals and Health Care", "hospitals_and_healthcare", "exact"),
    ("Health Care", "hospitals_and_healthcare", "exact"),
    ("Healthcare", "hospitals_and_healthcare", "exact"),
    ("Hospitals", "hospitals_and_healthcare", "child"),
    ("Home Health Care Services", "hospitals_and_healthcare", "child"),
    ("Nursing Homes and Residential Care Facilities", "hospitals_and_healthcare", "child"),
    ("Mental Health Care", "mental_healthcare", "exact"),
    ("Mental Health", "mental_healthcare", "exact"),
    ("Medical Practice", "medical_practices", "exact"),
    ("Medical Practices", "medical_practices", "rename"),
    ("Medical and Diagnostic Laboratories", "medical_practices", "child"),
    ("Physicians", "medical_practices", "child"),
    ("Dentists", "medical_practices", "child"),
    ("Optometrists", "medical_practices", "child"),
    ("Chiropractors", "medical_practices", "child"),
    ("Outpatient Care Centers", "medical_practices", "child"),
    ("Physical, Occupational and Speech Therapists", "medical_practices", "child"),
    ("Alternative Medicine", "medical_practices", "child"),
    ("Ambulance Services", "medical_practices", "child"),
    ("Human Resources Services", "human_resources_services", "exact"),
    ("Human Resources", "human_resources_services", "exact"),
    ("Outsourcing/Offshoring", "outsourcing_offshoring", "exact"),
    ("Outsourcing and Offshoring Consulting", "outsourcing_offshoring", "rename"),
    ("Events Services", "events_services", "exact"),
    ("Broadcast Media", "broadcast_media", "exact"),
    ("Broadcast Media Production and Distribution", "broadcast_media", "rename"),
    ("Newspapers", "newspapers", "exact"),
    ("Newspaper Publishing", "newspapers", "rename"),
    ("Book Publishing", "book_publishing", "exact"),
    ("Book and Periodical Publishing", "book_publishing", "rename"),
    ("Chemicals", "chemicals", "exact"),
    ("Chemical Manufacturing", "chemicals", "rename"),
    ("Staffing and Recruiting", "staffing_recruiting", "exact"),
    ("Staffing & Recruiting", "staffing_recruiting", "exact"),
    ("Staffing", "staffing_recruiting", "exact"),
    ("Recruiting", "staffing_recruiting", "exact"),
    ("Executive Search Services", "staffing_recruiting", "child"),
    ("Temporary Help Services", "staffing_recruiting", "child"),
)
APPROVED_INDUSTRY_LABELS: Dict[str, Tuple[str, str]] = {
    label.lower(): (category, provenance) for label, category, provenance in APPROVED_INDUSTRIES}


def approved_industry_query_labels() -> Tuple[str, ...]:
    """The exact labels to send as ``exclude_organization_industry`` (old and current spellings)."""
    return tuple(label for label, _, _ in APPROVED_INDUSTRIES)


#: An employer that IS a government body (name-level company evidence). A product company that sells to
#: government ("Public Sector Account Executive") is not matched: only the employer name is read.
_GOVERNMENT_EMPLOYER = _rx(
    r"^(?:the )?(?:city|town|county|village|borough|township|state|commonwealth|district|parish|port|municipality) of\b"
    r"|\b(?:county|city|state|municipal|tribal) government\b|\bdepartment of\b"
    r"|\b(?:state|county|municipal|superior|district|circuit|supreme|federal|juvenile|family) courts?\b|\bjudiciary\b"
    r"|\bsheriff'?s? (?:office|department)\b|\bpolice department\b|\bfire (?:department|district|rescue)\b"
    r"|\bpublic schools\b|\b(?:independent |unified |consolidated |community |public )?school district\b|\bboard of education\b"
    r"|\b(?:housing|transit|transportation|port|water|airport|turnpike) authority\b"
    r"|\bu\.?\s?s\.? (?:army|navy|air force|marine corps|coast guard|space force|department)\b")


#: A posting whose own company section describes the company WITHOUT ever naming the posting employer is a
#: third-party repost (a job board or aggregator republishing another company's opening): not the true
#: hiring company, and a contact search would target the wrong employer.
_REPOST_HEADER = _rx(r"^\s*about the company\b")


def third_party_repost(employer_name: Any, description: Any) -> str:
    text = str(description or "")
    if not _REPOST_HEADER.search(text):
        return ""
    name = normalize_company_name(employer_name)
    tokens = [t for t in name.split() if len(t) >= 4] or name.split()
    body = normalize_company_name(text)
    if not name or name in body or any(t in body.split() for t in tokens):
        return ""
    return text[:200]


def government_employer(name: Any) -> str:
    return str(name or "") if _GOVERNMENT_EMPLOYER.search(str(name or "")) else ""


def approved_industry_exclusion(industry: Any) -> Tuple[str, str]:
    """(approved category, label provenance) or ('', '')."""
    label = " ".join(str(industry or "").strip().lower().split())
    return APPROVED_INDUSTRY_LABELS.get(label, ("", ""))


# --- role mapping: title families + responsibilities --------------------------------------------

#: Titles that name a four-group function. Ordered: the first match wins. A title match is SUPPORTING
#: evidence only: the description must corroborate it (see ``_decide_role``).
IN_GROUP_TITLES: Tuple[Tuple[str, str, str], ...] = (
    ("gtm_revops_salesops", "crm_systems", r"\b(?:salesforce|hubspot|crm|dynamics 365|zoho|marketo) (?:administrator|admin|developer|consultant|analyst|specialist|architect|manager|lead|engineer|owner)\b"),
    ("gtm_revops_salesops", "revenue_operations", r"\b(?:revenue|sales|gtm|go[- ]to[- ]market|marketing|commercial) (?:operations|ops|systems|enablement|strategy (?:and|&) operations)\b|\b(?:revops|rev ops|salesops|deal desk|gtm engineer)\b|\b(?:sales|revenue|pipeline) analyst\b|\bsales (?:compensation|commissions?|support|coordinator|administrator|administration)\b"),
    ("gtm_revops_salesops", "sales_development", r"\b(?:sales|business|market) development (?:rep|representative|associate|specialist|executive|manager|director|lead|coordinator)\b|\b(?:sdr|bdr|adr)\b|\binside sales\b|\blead generation (?:specialist|representative|rep)\b|\bappointment setter\b"),
    ("gtm_revops_salesops", "sales_execution", r"\baccount executive\b|\b(?:sales|revenue) (?:executive|manager|director|lead|representative|rep|specialist|consultant|professional|engineer|leader)\b|\b(?:vp|vice president|head|director),? (?:of )?(?:sales|revenue|business development)\b|\bchief (?:revenue|sales|commercial) officer\b|\bcro\b|\bbusiness development (?:manager|director|executive|lead)\b|\b(?:partnerships?|channel|alliances?) (?:manager|director|lead|executive)\b|\b(?:solutions?|pre[- ]?sales) (?:engineer|consultant)\b"),
    ("customer_success_support", "account_management", r"\b(?:key |strategic |enterprise |client |customer |national )?account (?:manager|management|director|lead|specialist)\b"),
    ("customer_success_support", "customer_success", r"\b(?:customer|client) success\b|\bcsm\b|\b(?:customer|client|member) (?:experience|onboarding|retention|loyalty|adoption|engagement|advocacy|renewals?) (?:manager|specialist|lead|associate|director|representative|advocate|coordinator|analyst|partner|consultant)\b|\bimplementation (?:manager|specialist|consultant|lead|engineer|analyst|coordinator)\b|\bonboarding (?:specialist|manager|lead)\b|\bchief customer officer\b|\bclient services? (?:manager|director|lead|specialist|associate|coordinator|representative|executive)\b|\brenewals? (?:manager|specialist)\b"),
    ("customer_success_support", "customer_support", r"\b(?:customer|client|member|technical|tech|product|application|software|it|help ?desk|service ?desk|tier (?:[123]|i{1,3})|l[123]) (?:support|service|care)\b|\bsupport (?:engineer|specialist|agent|representative|rep|analyst|associate|advocate|lead|manager|coordinator|consultant|technician)\b|\bhelp ?desk\b|\bservice desk\b|\b(?:call|contact) cent(?:er|re) (?:agent|representative|specialist|associate)\b|\bcustomer (?:care|service)\b"),
    ("marketing_creative", "creative_design", r"\b(?:graphic|visual|brand|motion|digital|web|marketing|creative|multimedia|presentation|print|ui|ux|ui/ux|ux/ui|interaction|product|content) designer\b|\b(?:creative|art|design) director\b|\bcreative (?:strategist|producer|lead|manager|services)\b|\b(?:video|content) (?:editor|producer|creator)\b|\bmotion graphics\b|\banimator\b|\billustrator\b|\bcopywriter\b|\bcopy (?:writer|editor)\b|\bux writer\b"),
    ("marketing_creative", "marketing", r"\bmarketing\b|\bmarketer\b|\b(?:growth|demand gen(?:eration)?|performance|paid (?:media|search|social|acquisition)|ppc|sem|seo|user acquisition|lifecycle|email|crm marketing) (?:manager|specialist|lead|strategist|analyst|director|coordinator|associate)\b|\bcontent (?:strategist|manager|specialist|lead|writer|marketer)\b|\bsocial media\b|\b(?:online |digital )?community (?:manager|lead|specialist)\b|\binfluencer\b|\bbrand (?:manager|strategist|director|lead|specialist)\b|\b(?:marketing |corporate |external )?communications (?:manager|specialist|director|coordinator|lead|strategist|associate)\b|\bpublic relations\b|\bpr (?:manager|specialist|coordinator|director)\b|\bmedia (?:relations|buyer|planner)\b|\bchief marketing officer\b|\bcmo\b"),
    ("ai_engineering_automation", "software_data_ai", r"\b(?:software|backend|back[- ]end|frontend|front[- ]end|full[- ]?stack|web|mobile|ios|android|app|application|platform|cloud|devops|devsecops|site reliability|data|analytics|machine learning|ml|mlops|ai|artificial intelligence|llm|nlp|genai|generative ai|computer vision|deep learning|python|java|javascript|typescript|react|node|\.net|golang|ruby|php|scala|kotlin|swift|embedded software|firmware|database|etl|bi|business intelligence|integration|integrations|automation|rpa|test automation|qa automation|qa|software quality assurance|software quality|security|cybersecurity|application security|forward deployed|prompt|api|blockchain|search|analytics) (?:engineer|developer|architect|programmer|scientist|tester)\b|\b(?:developer|programmer|sdet|data scientist|software|machine learning|mlops|devops|site reliability)\b|\b(?:data|bi|business intelligence|analytics|reporting|insights) analyst\b|\b(?:bi|business intelligence|analytics) (?:developer|lead|manager)\b|\b(?:cto|chief technology officer|chief (?:ai|data) officer|engineering manager)\b|\b(?:vp|vice president|head|director),? (?:of )?(?:engineering|software|ai|data|machine learning|platform)\b|\b(?:ai|automation|rpa|no[- ]code|low[- ]code|workflow automation) (?:specialist|consultant|lead|manager|analyst|architect|strategist)\b|\b(?:tech(?:nical)? lead|data science)\b|\b(?:ai|ml|llm|genai)\b[^,;|()]{0,30}\b(?:engineer|developer|architect|scientist)\b|\b(?:software|solutions?|cloud|data|enterprise|technical|ai|ml|platform|integration|salesforce|security) architect\b"),
)

#: Titles naming work outside the four groups. Used only to CLOSE a job that also has no four-group
#: responsibilities; a title never closes a job whose description carries group evidence.
OUT_OF_GROUP_TITLES: Tuple[Tuple[str, str], ...] = (
    ("clinical_healthcare", r"\b(?:registered nurse|nurse|nursing|rn|lpn|lvn|cna|physician|surgeon|hospitalist|dentist|dental|hygienist|pharmacist|pharmacy|therapist|therapy|phlebotom\w*|medical assistant|caregiver|home health|emt|paramedic|radiolog\w*|sonograph\w*|optometrist|optician|veterinar\w*|vet|psychiatr\w*|psycholog\w*|neurolog\w*|oncolog\w*|hematolog\w*|anesthes\w*|midwife|clinician|clinical|behavior technician|rbt|direct support|chiropract\w*|dietitian|audiolog\w*|counselor|patient)\b"),
    ("skilled_trades_field", r"\b(?:technician|mechanic|electrician|plumber|welder|fabricator|machinist|carpenter|painter|installer|hvac|millwright|lineman|lineworker|journeyman|apprentice|foreman|superintendent|roofer|glazier|mason|pipefitter|boilermaker|operator|assembler|cnc|laborer|labourer|groundskeeper|landscap\w*|custodian|janitor\w*|housekeep\w*|cleaner|porter|maintenance|handyman|locksmith|cutter|finisher|inspector|surveyor|estimator|craftsman|tradesman|plant|shift (?:lead|supervisor)|line (?:lead|worker|supervisor)|(?:medication|slot|pharmacy|lab|vet|surgical|dental|sterile processing|automotive|auto|diesel|lube|tire|pos) tech|metrology|production (?:supervisor|manager|worker|associate|lead)|quality (?:control|assurance) (?:inspector|technician|supervisor|manager|specialist|lead|associate))\b"),
    ("logistics_driving", r"\b(?:driver|cdl|courier|delivery (?:driver|associate|helper)|warehouse|material handler|forklift|picker|packer|loader|shipping|receiving|dock|stocker|fulfillment associate|dispatcher|mover|freight|logistics|supply chain|inventory|purchasing|procurement|buyer|planner|scheduler)\b"),
    ("hospitality_food_retail", r"\b(?:chef|cook|kitchen|server(?!\s+(?:engineer|administrator|admin|architect|developer))|bartender|barista|dishwasher|host|hostess|busser|cashier|retail|store|sales associate|keyholder|merchandiser|front desk|concierge|valet|banquet|catering|restaurant|hotel|lifeguard|(?:head |assistant |varsity |jv |athletic |basketball |football |soccer |hockey |swim |volleyball |baseball |softball |track |tennis |golf )coach|food (?:&|and) beverage|wellness|teller|universal banker|banker|make[- ]?up artist|stylist|barber|esthetician|massage|fitness|personal trainer|attendant|guest services?)\b"),
    ("non_software_engineering", r"\b(?:electrical|mechanical|civil|structural|process|manufacturing|chemical|industrial|controls?|hardware|rf|mechatronics|aerospace|avionics|piping|geotechnical|environmental|water|wastewater|transportation|traffic|nuclear|power|substation|reliability|quality|production|packaging|facilities|safety|validation|materials|optical|biomedical|petroleum|mining|energy|solar|design|field|project|test|manufacturing|electronics|systems|network|telecommunications|construction|plant) (?:engineer|engineering|designer|drafter)\b|\b(?:drafter|draftsman|cad)\b"),
    ("finance_accounting", r"\b(?:accountant|accounting|bookkeep\w*|controller|comptroller|auditor|audit|tax|payroll|accounts? (?:payable|receivable)|billing|collections?|collector|credit|underwrit\w*|actuar\w*|treasury|financial|fp&a|cfo|finance|investment|portfolio manager|loan|mortgage|escrow|claims|revenue cycle|wealth|insurance agent|insurance producer|financial advisor)\b"),
    ("people_hr_recruiting", r"\b(?:human resources|hr|hris|people (?:operations|partner|business partner)|recruit\w*|talent acquisition|sourcer|benefits|compensation|learning (?:and|&) development|training|trainer)\b"),
    ("legal_compliance", r"\b(?:attorney|lawyer|paralegal|counsel|legal|litigation|court|judicial|clerk|compliance|regulatory|contracts? (?:manager|administrator|specialist))\b"),
    ("education", r"\b(?:teacher|teaching|instructor|professor|faculty|tutor|educator|aide|paraprofessional|principal of|assistant principal|school|dean|lecturer|curriculum|admissions)\b"),
    ("science_research", r"\b(?:chemist|biologist|microbiolog\w*|scientist|assay|research (?:associate|assistant|technician)|lab|laboratory|geologist|epidemiolog\w*)\b"),
    ("social_services", r"\b(?:social worker|case (?:manager|worker|aide)|caseworker|youth|child ?care|babysitter|nanny|family (?:advocate|support)|residential (?:counselor|aide|care|treatment|staff|supervisor|youth)|shelter|volunteer|donor|fundraising|development officer|grant)\b"),
    ("real_estate_property", r"\b(?:property manager|property management|leasing|real estate|realtor|broker|appraiser|community association)\b"),
    ("operations_admin_pm", r"\b(?:administrative|admin assistant|executive assistant|office (?:manager|administrator|coordinator|assistant)|receptionist|secretary|data entry|operations|chief operating officer|coo|chief of staff|project (?:manager|coordinator|director|lead|controls|administrator)|program (?:manager|coordinator|director|specialist|analyst|assistant)|coordinator|general manager|president|chief executive officer|ceo|founder|owner|managing director|executive director|product (?:manager|owner)|business analyst|consultant|security (?:officer|guard)|pilot|flight|aircraft|safety|ehs|environmental health)\b"),
    ("public_safety_government", r"\b(?:police|sheriff|deputy|correction(?:al|s) officer|firefighter|probation|parole|detention|dispatcher)\b"),
    ("clergy_religious", r"\b(?:pastor|worship|minister|chaplain|priest|ministry)\b"),
)
_IN_GROUP = [(g, fam, _rx(p)) for g, fam, p in IN_GROUP_TITLES]
_OUT_GROUP = [(fam, _rx(p)) for fam, p in OUT_OF_GROUP_TITLES]

#: A title naming a non-US territory (and no US / North America / global scope) is not a US opportunity.
_FOREIGN_TITLE = _rx(r"\b(?:south africa|emea|apac|asia[- ]pacific|anz|australia|new zealand|united kingdom|uk|ireland|europe|european|"
                     r"dach|germany|france|spain|italy|nordics|benelux|middle east|mena|gcc|india|japan|china|singapore|latam|"
                     r"latin america|brazil|mexico)\b")
_US_SCOPE_TITLE = _rx(r"\b(?:us|u\.s\.?|usa|united states|north america|americas|global|worldwide|canada)\b")

#: Title qualifiers that make a four-group-sounding title in-person work ("Outside Sales Rep").
_IN_PERSON_TITLE = _rx(r"\b(?:outside|field|route|door[- ]to[- ]door|in[- ]home|in[- ]store|store|showroom|dealership|counter|branch|retail|territory|brand ambassador|promoter|merchandis\w*)\b")
#: In-person selling signals on a qualified sales / account role. Mostly-virtual "remote field" roles exist, so
#: this is a QA flag, never a rejection (a rejection needs majority travel or an explicit field-based role).
_FIELD_SALES_SIGNALS = _rx(r"\bremote field (?:position|role)\b|\bregular(?:ly)? (?:joint )?(?:customer|client|account|dealer|office) visits\b"
                           r"|\btravel(?:ing|ling)? to (?:dealership|customer|client|store|retail|dealer) locations\b"
                           r"|\bface[- ]to[- ]face (?:sales|appointments|meetings|presentations)\b|\b(?:defined|assigned) (?:geographic )?territory\b")

#: Non-software engineering evidence in a description titled like software work ("Automation Engineer" + PLC).
_INDUSTRIAL = _rx(r"\b(?:plc|scada|hmi|allen[- ]bradley|rockwell|ladder logic|solidworks|autocad|revit|p&id|pcb|schematics?|wiring|machining|welding|piping|substation|mechanical design|electrical design|production line|robot(?:ic)? cells?|"
                  r"manufacturing (?:quality|operations|processes)|supplier quality|as9100|iso 9001|first article|ppap|gd&t|cmm)\b")

#: Supplementary responsibility phrases (weight 2) that the production lexicon does not cover.
SUPPLEMENT: Dict[str, Tuple[str, ...]] = {
    "gtm_revenue": (r"\bfull sales cycle\b|\bclos(?:e|ing) (?:new )?(?:business|deals)\b|\bnew business\b|\bsales pipeline\b|\bpipeline (?:generation|management)\b|\bbuild (?:a |your )?pipeline\b",
                    r"\bprospects?\b|\boutbound (?:calls|prospecting|outreach)\b|\bqualif(?:y|ying) (?:leads|prospects)\b|\bsales (?:targets|goals)\b"),
    "customer_success": (r"\b(?:account management|client relationships?|customer relationships?|customer (?:satisfaction|retention|lifecycle))\b",),
    "customer_support": (r"\binbound (?:calls|inquiries)\b|\banswer(?:ing)? (?:customer )?(?:calls|questions|inquiries)\b|\bresolv(?:e|ing) (?:customer )?(?:issues|inquiries|problems|complaints)\b|\bcustomer (?:inquiries|issues|complaints)\b",),
    "marketing": (r"\bmarketing campaigns?\b|\b(?:brand|marketing|creative|social) (?:assets|content|materials|strategy)\b|\badobe (?:creative (?:suite|cloud)|photoshop|illustrator|indesign)\b|\b(?:photoshop|illustrator|indesign|after effects|premiere pro)\b",),
    "engineering": (r"\bwrit(?:e|ing) (?:clean |production |high[- ]quality |maintainable )?code\b|\bcodebase\b|\bsoftware development\b|\bgit(?:hub|lab)?\b|\bsql\b|\brest(?:ful)? apis?\b|\bdata models?\b",),
}
_SUPPLEMENT = {fn: [_rx(p) for p in ps] for fn, ps in SUPPLEMENT.items()}
#: Product-design responsibilities corroborate a designer title for the Marketing & Creative group.
_DESIGN_WORK = _rx(r"\b(?:wireframes?|prototypes?|mockups?|design systems?|user flows|figma|sketch|visual design|interaction design|user interface|ui design|ux design)\b")


_ABBREVIATIONS = ((r"\bmgr\b", "manager"), (r"\bdir\b", "director"), (r"\bspec\b", "specialist"),
                  (r"\bcoord\b", "coordinator"), (r"\bassoc\b", "associate"), (r"\bexec\b", "executive"),
                  (r"\bengr?\b", "engineer"), (r"\bsvcs?\b", "services"), (r"\bmktg\b", "marketing"),
                  (r"\bdev\b", "developer"))


def _normalize_title(title: str) -> str:
    t = " ".join(str(title or "").replace("/", " / ").split())
    for pattern, replacement in _ABBREVIATIONS:
        t = re.sub(pattern, replacement, t, flags=re.I)
    return t


def title_families(title: str) -> Tuple[List[Tuple[str, str]], str]:
    """(every in-group (group, family) the title names, first out-of-group family or '')."""
    t = _normalize_title(title)
    in_group = [(g, fam) for g, fam, rx in _IN_GROUP if rx.search(t)]
    out = next((fam for fam, rx in _OUT_GROUP if rx.search(t)), "")
    return in_group, out


def classify_title(title: str) -> Tuple[str, str, str]:
    """(kind, group, family) of the strongest title evidence: kind in {'in_group', 'out_of_group', 'neutral'}."""
    in_group, out = title_families(title)
    if in_group:
        return "in_group", in_group[0][0], in_group[0][1]
    if out:
        return "out_of_group", "", out
    return "neutral", "", ""


def group_scores(description: str) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, List[str]]]:
    """(four-group scores, out-of-group function scores, evidence phrases per function)."""
    scores, hits = score_functions(description)
    sents = sentences(description)
    evidence: Dict[str, List[str]] = {fn: [sig.phrase for sig, _ in h] for fn, h in hits.items()}
    extra: Dict[str, int] = {}
    for fn, patterns in _SUPPLEMENT.items():
        for rx in patterns:
            hit = _first(sents, rx)
            if hit:
                extra[fn] = extra.get(fn, 0) + 2
                evidence.setdefault(fn, []).append(f"supplement:{rx.pattern[:40]}")
    total = {fn: scores.get(fn, 0) + extra.get(fn, 0) for fn in set(scores) | set(extra)}
    groups = {g: sum(total.get(fn, 0) for fn in fns) for g, fns in GROUPS.items()}
    out = {fn: total.get(fn, 0) for fn in OUT_OF_GROUP_FUNCTIONS}
    return groups, out, evidence


#: Selling responsibilities (the job carries a number) vs GTM operations responsibilities (the job runs the
#: revenue machine: RevOps, Sales Ops, GTM systems, automation, CRM, analytics, enablement).
_SELLING: Tuple[Tuple[str, int, str], ...] = (
    (r"\bfull sales cycle\b|\bown(?:ing)? the (?:entire |full |end-to-end )?sales (?:cycle|process)\b", 3, "full sales cycle"),
    (r"\bclos(?:e|es|ing) (?:new )?(?:business|deals|sales)\b|\bdeal clos\w*\b", 3, "closing deals"),
    (r"\b(?:meet|meeting|meets|exceed|exceeding|exceeds|achieve|achieving|hit|hitting|carry|carrying)\b[^.]{0,40}\b(?:quotas?|sales (?:targets|goals)|revenue targets)\b|\bquota[- ](?:carrying|attainment)\b", 3, "quota attainment"),
    (r"\b(?:prospect|prospecting|cold call\w*|cold (?:email|outreach)|outbound (?:calls|prospecting|outreach))\b", 2, "prospecting"),
    (r"\bnew (?:business|logos?)\b|\bhunter mentality\b", 2, "new business"),
    (r"\b(?:book|booking|set|setting|schedule|scheduling) (?:qualified )?(?:meetings|appointments|demos)\b", 2, "booking meetings"),
    (r"\bsales presentations\b|\bproduct demonstrations?\b|\bdemos? to (?:prospects|customers)\b", 2, "sales presentations"),
    (r"\b(?:commission|on[- ]target earnings|ote|uncapped)\b", 2, "commission compensation"),
    (r"\bqualif(?:y|ying) (?:leads|prospects|opportunities)\b", 2, "qualifying leads"),
)
_GTM_OPS: Tuple[Tuple[str, int, str], ...] = (
    (r"\b(?:salesforce|hubspot|crm|dynamics 365)\b[^.]{0,40}\b(?:administration|admin|administer|configur\w*|workflows?|automation|architecture|hygiene|data quality|integrations?)\b", 3, "CRM administration"),
    (r"\b(?:revenue|sales|gtm|go-to-market|marketing) operations\b|\brevops\b|\bsales ops\b|\bdeal desk\b", 3, "revenue / sales operations"),
    (r"\b(?:pipeline|sales|revenue|bookings) (?:reporting|analytics|dashboards|forecast(?:ing|s)?|analysis)\b|\bforecast accuracy\b", 3, "revenue reporting and forecasting"),
    (r"\blead (?:routing|scoring|enrichment|assignment)\b|\bround[- ]robin\b", 3, "lead routing"),
    (r"\bterritory (?:design|planning|carving|alignment)\b|\b(?:commission|compensation|comp plan) (?:calculations?|administration|processing|design|modeling)\b|\bquota (?:setting|planning|design)\b", 3, "territory / compensation operations"),
    (r"\bsales enablement\b|\benablement (?:programs?|content|strategy)\b|\b(?:onboard|onboarding|train|training)\b[^.]{0,30}\b(?:sales )?reps\b|\bsales (?:training|playbooks?)\b", 3, "sales enablement"),
    (r"\b(?:cpq|quote[- ]to[- ]cash)\b", 2, "quote-to-cash systems"),
    (r"\b(?:gtm|revenue|sales) (?:systems|tech stack|tooling|technology)\b|\b(?:outbound|sales) (?:infrastructure|tooling|automation)\b|\bdeliverability\b|\bworkflow automation\b|\b(?:zapier|n8n)\b", 3, "GTM systems and automation"),
    (r"\b(?:sales|revenue|gtm) (?:analyst|analytics|insights)\b|\bdata hygiene\b", 2, "sales analytics"),
)
_SELLING_RX = [(_rx(p), w, name) for p, w, name in _SELLING]
_GTM_OPS_RX = [(_rx(p), w, name) for p, w, name in _GTM_OPS]
OPS_TITLE_FAMILIES = frozenset({"revenue_operations", "crm_systems"})


def sales_scope(sents: Sequence[str]) -> Tuple[int, int, List[str], List[str]]:
    """(selling score, GTM-operations score, selling phrases, operations phrases)."""
    def score(table):
        total, names = 0, []
        for rx, weight, name in table:
            if _first(sents, rx):
                total += weight
                names.append(name)
        return total, names
    selling, sell_names = score(_SELLING_RX)
    ops, ops_names = score(_GTM_OPS_RX)
    return selling, ops, sell_names, ops_names


def _group_function(group: str, fn_scores: Mapping[str, int], dominant: str) -> str:
    fns = GROUPS[group]
    if dominant in fns:
        return dominant
    return max(fns, key=lambda f: (fn_scores.get(f, 0), -fns.index(f)))


#: Generic business title families: when the responsibilities are clearly in a four-group function,
#: the responsibilities decide (a "Technical Project Manager" whose duties are engineering). A named
#: profession (nurse, electrician, accountant ...) is never overridden this way; it goes to review.
GENERIC_BUSINESS_FAMILIES = frozenset({"operations_admin_pm"})
_IN_PERSON_FAMILIES = frozenset({"sales_execution", "sales_development", "customer_support", "customer_success", "account_management"})


def _decide_role(title: str, desc: str, sents: Sequence[str], *, quota_rule: bool = False) -> Dict[str, Any]:
    decided = _decide_role_v1(title, desc, sents)
    if not quota_rule:
        return decided
    families = set(decided.get("title_families") or [])
    sales_family = bool(families & {"sales_execution", "sales_development", "account_management"}) and not families & OPS_TITLE_FAMILIES
    lands_in_gtm = decided.get("outcome") == "qualified" and decided.get("group") == "gtm_revops_salesops"
    held_as_sales = decided.get("outcome") == "review" and decided.get("reason") in {"in_person_title_qualifier", "title_without_responsibility_evidence"}
    if not (lands_in_gtm or (sales_family and (held_as_sales or decided.get("outcome") == "qualified"))):
        return decided
    if lands_in_gtm and families & OPS_TITLE_FAMILIES:
        return decided                                   # an operations title with GTM responsibilities
    if decided.get("outcome") == "qualified" and decided.get("group") != "gtm_revops_salesops":
        return decided                                   # responsibilities already placed it in another group
    selling, ops, sell_names, ops_names = sales_scope(sents)
    groups = decided.get("group_scores") or {}
    scope = {"selling_score": selling, "gtm_ops_score": ops, "selling_evidence": sell_names, "gtm_ops_evidence": ops_names}
    # A sales title is quota-carrying by default; it needs clearly dominant operations responsibilities.
    sales_titled = bool(families & {"sales_execution", "sales_development"})
    floor, ratio_floor = (9, 2 * selling + 3) if sales_titled else (6, 2 * selling)
    if ops >= floor and ops >= ratio_floor:
        return {**decided, **scope, "outcome": "qualified", "group": "gtm_revops_salesops", "function": "gtm_revenue",
                "basis": "gtm_operations_primary" if not sales_titled else "sales_title_gtm_operations_primary"}
    if "account_management" in families and groups.get("customer_success_support", 0) >= 3 and \
            groups.get("customer_success_support", 0) >= selling:
        return {**decided, **scope, "outcome": "qualified", "group": "customer_success_support",
                "function": "customer_success", "basis": "account_management_success_primary"}
    if selling >= 3 or (sales_titled and ops < 3):
        return {**decided, **scope, "outcome": "rejected", "reason": "outside_four_groups:quota_carrying_sales",
                "evidence": (f"sales title family {sorted(families & {'sales_execution', 'sales_development'})}; "
                             f"selling {', '.join(sell_names) or 'none listed'} (score {selling}); GTM operations score {ops}")}
    return {**decided, **scope, "outcome": "review", "reason": "sales_scope_unresolved",
            "candidate_groups": ["gtm_revops_salesops"]}


def _decide_role_v1(title: str, desc: str, sents: Sequence[str]) -> Dict[str, Any]:
    in_group, out_family = title_families(title)
    base_scores, base_hits = score_functions(desc)
    dominant = _dominant(base_scores, base_hits) or ""
    groups, out_scores, evidence = group_scores(desc)
    fn_scores = {fn: base_scores.get(fn, 0) for fn in LEXICON}
    for fn, pats in _SUPPLEMENT.items():
        fn_scores[fn] += 2 * sum(1 for rx in pats if _first(sents, rx))
    best_out = max(out_scores.values()) if out_scores else 0
    best_group = max(groups, key=lambda g: groups[g])
    dom_group = GROUP_BY_FUNCTION.get(dominant, "")
    families = [fam for _, fam in in_group]
    decision: Dict[str, Any] = {
        "title_kind": "in_group" if in_group else ("out_of_group" if out_family else "neutral"),
        "title_family": families[0] if families else out_family, "title_families": families,
        "title_out_family": out_family, "group_scores": groups, "out_of_group_scores": out_scores,
        "dominant_function": dominant, "evidence_phrases": evidence}

    def qualified(g: str, basis: str, **extra: Any) -> Dict[str, Any]:
        return {**decision, "outcome": "qualified", "group": g, "function": _group_function(g, fn_scores, dominant),
                "basis": basis, **extra}

    def review(reason: str, candidates: Sequence[str]) -> Dict[str, Any]:
        return {**decision, "outcome": "review", "reason": reason,
                "candidate_groups": list(dict.fromkeys(c for c in candidates if c))}

    if _FOREIGN_TITLE.search(title) and not _US_SCOPE_TITLE.search(title):
        return {**decision, "outcome": "rejected", "reason": "market:foreign_territory_title",
                "evidence": f"title names a non-US territory: {title[:120]}"}
    if in_group:
        candidates: List[str] = []
        for g, fam in in_group:
            candidates += ["gtm_revops_salesops", "customer_success_support"] if fam == "account_management" else [g]
        candidates = list(dict.fromkeys(candidates))
        if set(families) <= _IN_PERSON_FAMILIES and _IN_PERSON_TITLE.search(title):
            return review("in_person_title_qualifier", candidates)
        if dom_group in candidates:
            return qualified(dom_group, "title_family+dominant_responsibilities")
        ranked = sorted(candidates, key=lambda g: -groups[g])
        g = ranked[0]
        margin_ok = len(ranked) == 1 or groups[g] - groups[ranked[1]] >= 2
        corroborated = groups[g] >= 3 and groups[g] >= best_out and margin_ok
        if not corroborated and "creative_design" in families and _first(sents, _DESIGN_WORK):
            g, corroborated = "marketing_creative", True
        if g == "ai_engineering_automation" and _first(sents, _INDUSTRIAL) and groups[g] < 6:
            return review("industrial_engineering_evidence", [g])
        if corroborated:
            return qualified(g, "title_family+responsibilities")
        if len(candidates) > 1 and groups[g] >= 3 and groups[g] >= best_out:
            return qualified(g, "title_family+responsibilities", group_tie=ranked[1])
        if len(candidates) > 1 and groups[g] >= 3:
            return review("title_names_several_groups", ranked)
        if dominant:
            return review("title_description_conflict", candidates + [dom_group])
        return review("title_without_responsibility_evidence", candidates)
    if out_family:
        if dom_group and out_family in GENERIC_BUSINESS_FAMILIES:
            return qualified(dom_group, "dominant_responsibilities_over_generic_title", title_conflict=out_family)
        if out_family in GENERIC_BUSINESS_FAMILIES:
            strong = groups[best_group] >= 3
        else:
            # generic service wording ("provide excellent customer service") must not hold a nurse or a paralegal
            strong = groups[best_group] >= 6 and groups[best_group] >= best_out + 3
        if dom_group or strong:
            return review("title_description_conflict", [g for g in GROUPS if groups[g] >= 3] + [dom_group])
        return {**decision, "outcome": "rejected", "reason": "outside_four_groups",
                "evidence": (f"title family {out_family}; four-group responsibility score {groups[best_group]}"
                             f" vs {best_out} for out-of-group functions")}
    if dom_group:
        return qualified(dom_group, "dominant_responsibilities")
    if dominant:
        return {**decision, "outcome": "rejected", "reason": "outside_four_groups",
                "evidence": f"responsibilities dominated by {dominant}: {', '.join(evidence.get(dominant, [])[:3])}"}
    if groups[best_group] == 0 and best_out >= 3:
        top = max(out_scores, key=lambda f: out_scores[f])
        return {**decision, "outcome": "rejected", "reason": "outside_four_groups",
                "evidence": f"no four-group responsibility; {top} responsibilities: {', '.join(evidence.get(top, [])[:3])}"}
    strength = ("no_group_evidence" if groups[best_group] == 0 else
                "weak_group_evidence" if groups[best_group] < 3 else "group_evidence_without_title")
    return review(f"role_unresolved:{strength}", [g for g in GROUPS if groups[g] >= 3])


# --- the candidate qualifier --------------------------------------------------------------------

def _org_block(row: Mapping[str, Any]) -> Dict[str, Any]:
    keys = ("organization", "organization_url", "org_linkedin_name", "org_linkedin_slug", "org_linkedin_website",
            "org_linkedin_headcount", "org_linkedin_size", "org_linkedin_industry",
            "org_linkedin_recruitment_agency_derived", "domain_derived", "linkedin_url")
    return {k: row.get(k) for k in keys if row.get(k) not in (None, "", [])}


def crm_keys(names: Iterable[str]) -> FrozenSet[str]:
    return frozenset(k for k in (name_key(n) for n in names) if len(k) >= 4)


def qualify_candidate(row: Mapping[str, Any], *, now: datetime, steps: Iterable[str] = ALL_STEPS,
                      crm: FrozenSet[str] = frozenset()) -> Dict[str, Any]:
    """One job, one decision: ``qualified_pre_contact`` (enters contact discovery), ``review:*``
    (preserved, flagged) or ``rejected:*`` (an approved hard exclusion, with evidence)."""
    on = frozenset(steps)
    unknown = on - set(ALL_STEPS)
    if unknown:
        raise ValueError(f"unknown candidate steps: {sorted(unknown)}")
    org = _org_block(row)
    employer_name = str(row.get("organization") or row.get("org_linkedin_name") or "")
    domain, slug, nk = employer_anchors({**org, "organization": org.get("organization") or employer_name})
    ekey = employer_key(domain, slug, nk)
    title = str(row.get("title") or "").strip()
    desc = str(row.get("description_text") or "")
    locations = row.get("locations_derived") or []
    location_text = ", ".join(str(x) for x in locations[:3]) if isinstance(locations, list) else str(locations or "")
    out: Dict[str, Any] = {"policy_version": POLICY_VERSION, "steps": sorted(on), "employer_key": ekey,
                           "function": "", "group": "", "flags": [], "evidence": [], "stage": ""}
    flags: List[str] = out["flags"]

    def done(outcome: str, stage: str, evidence: str = "") -> Dict[str, Any]:
        out.update(outcome=outcome, stage=stage)
        if evidence:
            out["evidence"].append({"rule": outcome, "excerpt": str(evidence)[:300]})
        return out

    # 1) not a live posting / not the true hiring company / no employer identity (unchanged)
    vt = _parse_dt(row.get("date_valid_through"))
    if vt and vt < now:
        return done("rejected:posting_expired", "posting", str(row.get("date_valid_through")))
    conflict = employer_attribution_conflict(desc, employer_name=employer_name, employer_domain=domain)
    if conflict:
        return done("rejected:employer_attribution_conflict", "company", conflict.get("excerpt", ""))
    if not ekey:
        return done("rejected:employer_identity_unresolved", "company")
    if CRM_EXCLUSION in on and crm and name_key(employer_name) in crm:
        return done("rejected:crm_existing_company", "company", employer_name)
    # company-level exclusions are independent of the role, so the candidate applies them first
    if THIRD_PARTY_REPOST in on:
        repost = third_party_repost(employer_name, desc)
        if repost:
            return done("rejected:not_true_hiring_company:third_party_repost", "company", f"{employer_name}: {repost}")
    if GOVERNMENT_EMPLOYER in on and government_employer(employer_name):
        return done("rejected:employer_is_government", "company", employer_name)
    if INDUSTRY_LABELS in on:
        category, provenance = approved_industry_exclusion(row.get("org_linkedin_industry"))
        if category:
            return done(f"rejected:employer_excluded_industry:{category}", "company",
                        f"{row.get('org_linkedin_industry')} ({provenance})")
        if _bool(row.get("org_linkedin_recruitment_agency_derived")) is True:
            return done("rejected:employer_is_agency", "company", "org_linkedin_recruitment_agency_derived=true")

    # 2) job facts: production extraction, then each enabled step re-decides only its own rule
    facts = extract_job_facts(
        title=title, description=desc, employment_type=row.get("employment_type"),
        ai_employment_type=row.get("ai_employment_type"), location_type=row.get("location_type"),
        countries=[str(c) for c in (row.get("countries_derived") or []) if c], location_text=location_text,
        employer_name=employer_name, agency_flag=_bool(row.get("org_linkedin_recruitment_agency_derived")),
        org_industry=row.get("org_linkedin_industry"),
    )
    sents = sentences(f"{title}. {location_text}. {desc}")
    body_sents = sentences(desc)
    arrangement = facts.get("work_arrangement").value
    if not arrangement and str(row.get("location_type") or "").upper() == "TELECOMMUTE":
        arrangement = "remote"
    out["work_arrangement"] = arrangement or "unknown"
    hard: List[Tuple[str, str]] = []
    for e in facts.exclusions:
        reason = e.reason
        if reason.startswith("seniority:") or reason == "people_management":
            if SENIORITY_MANAGEMENT in on:
                flags.append(reason)
                continue
        elif reason.startswith(("employment:", "program:", "active:")):
            if EMPLOYMENT_PRECISION in on:
                continue   # re-decided below
        elif reason in {"agency:staffing_text", "agency:outsourcing_text"}:
            if INTERMEDIARY_PRECISION in on:
                continue
        elif reason == "deliverability:field_work":
            if FIELD_PRECISION in on:
                continue
        elif reason == "deliverability:travel":
            if FIELD_PRECISION in on or TRAVEL_PRECISION in on:
                continue   # re-decided below (production records only the first arrangement it finds)
        elif reason == "deliverability:physical_facility":
            if PHYSICAL_EVIDENCE in on:
                continue
        elif reason in {"deliverability:security_clearance", "deliverability:professional_license", "government:public_sector_title"}:
            if ELIGIBILITY_FLAGS in on:
                if reason != "deliverability:security_clearance":
                    flags.append(reason.split(":", 1)[-1] + "_required" if reason.startswith("deliverability") else reason)
                continue   # clearance is re-decided below: required federal clearance rejects, a mention does not
        hard.append((reason, e.excerpt))
    if EMPLOYMENT_PRECISION in on:
        reason, excerpt, emp_flags = _employment_decision(title, sents, row)
        flags.extend(emp_flags)
        if reason:
            hard.insert(0, (reason, excerpt))
    if ELIGIBILITY_FLAGS in on:
        required, mentioned = federal_clearance(title, sents)
        if required:
            hard.append(("eligibility:federal_clearance_required", required))
        elif mentioned:
            flags.append("federal_clearance_mentioned_not_required")
    if INTERMEDIARY_PRECISION in on:
        reason, excerpt = _intermediary(sents, True)
        if reason:
            hard.append((reason, excerpt))
    if FIELD_PRECISION in on:
        hit = _any_first(sents, FIELD_ROLE, skip=_FIELD_INCIDENTAL)
        if hit:
            hard.append(("deliverability:field_work", hit))
    if ESSENTIAL_FIELD in on:
        hit = essential_field_travel(body_sents)
        if hit:
            hard.append(("deliverability:essential_field_travel", hit))
    if TRAVEL_PRECISION in on:
        hit, travel_flags = _travel(sents)
        flags.extend(travel_flags)
        if hit:
            hard.append(("deliverability:majority_travel", hit))
    elif FIELD_PRECISION in on:
        hit = _any_first(sents, TRAVEL_HARD)
        if hit:
            hard.append(("deliverability:travel", hit))
    if PHYSICAL_EVIDENCE in on:
        hit, phys_flags = _physical_facility(body_sents, True)
        flags.extend(phys_flags)
        if hit:
            hard.append(("deliverability:location_bound_duty", hit))
    if hard:
        order = [r for r, _ in hard]
        # keep production precedence among the production reasons; precision steps append theirs
        return done(f"rejected:{order[0]}", "job_facts", hard[0][1])

    # 3) role / campaign fit
    if len(desc.strip()) < 120:
        if ROLE_MAPPING in on:
            kind, group, family = classify_title(title)
            out.update(title_kind=kind, title_family=family)
            return done("review:description_missing", "role", f"{len(desc.strip())} chars; title family {family or 'none'}")
        return done("rejected:insufficient_evidence:description_too_short", "role", f"{len(desc.strip())} chars")
    if ROLE_MAPPING in on:
        role = _decide_role(title, desc, body_sents, quota_rule=QUOTA_SALES in on)
        out["role"] = {k: v for k, v in role.items() if k != "evidence_phrases"}
        out["role_evidence"] = role.get("evidence_phrases", {})
        if role["outcome"] == "rejected":
            return done(f"rejected:{role['reason']}", "role", role.get("evidence", ""))
        if role["outcome"] == "review":
            out["candidate_groups"] = role.get("candidate_groups", [])
            return done(f"review:{role['reason']}", "role", f"title family {role.get('title_family') or 'none'}")
        out["function"], out["group"] = role["function"], role["group"]
        if role.get("title_conflict"):
            flags.append(f"title_family_conflict:{role['title_conflict']}")
        if role.get("group_tie"):
            flags.append(f"group_assignment_tie:{role['group_tie']}")
        if set(role.get("title_families") or []) & _IN_PERSON_FAMILIES and _first(body_sents, _FIELD_SALES_SIGNALS):
            flags.append("field_sales_signals")
        _coverage_flags(row, facts, flags, now)
        if PHYSICAL_EVIDENCE in on:
            pass  # already enforced above for every job
        else:
            loc = location_bound_evidence(body_sents) if set(role.get("title_families") or []) & _IN_PERSON_FAMILIES else ""
            if loc:
                return done("review:location_bound_evidence", "role", loc)
    else:
        scores, hits = score_functions(desc)
        dominant = _dominant(scores, hits)
        if not dominant:
            return done("ambiguous:needs_semantic_classifier", "role")
        out["function"] = dominant
        out["group"] = GROUP_BY_FUNCTION.get(dominant, "")

    # 4) company gates (known facts reject; unknown facts are flags that proceed to contact discovery)
    anchor = min(d for d in (_parse_dt(row.get("date_posted")), _parse_dt(row.get("date_created")), now) if d is not None)
    if (now - anchor).days > int(rule("approval_max_age_days")):
        return done("rejected:posting_too_old", "company", str(row.get("date_posted")))
    if _bool(row.get("org_linkedin_recruitment_agency_derived")) is True:
        return done("rejected:employer_is_agency", "company", "org_linkedin_recruitment_agency_derived=true")
    if INDUSTRY_LABELS in on:
        if excluded_industry(str(row.get("org_linkedin_industry") or "")):
            flags.append(f"industry_outside_approved_list:{row.get('org_linkedin_industry')}")
    else:
        industry = excluded_industry(str(row.get("org_linkedin_industry") or ""))
        if industry:
            return done(f"rejected:employer_excluded_industry:{industry}", "company", str(row.get("org_linkedin_industry")))
    headcount = _int(row.get("org_linkedin_headcount"))
    if headcount is None:
        if ROLE_MAPPING in on:
            flags.append("company_size_unknown")
        else:
            return done("ambiguous:company_size_unknown", "company")
    elif headcount < int(rule("min_employees")):
        return done("rejected:employer_too_small", "company", str(headcount))
    elif headcount > int(rule("max_employees")):
        return done("rejected:employer_too_large", "company", str(headcount))
    return done("qualified_pre_contact", "qualified")


_PROFILE_FIELDS = ("org_linkedin_slug", "org_linkedin_headcount", "org_linkedin_industry", "org_linkedin_website")
#: Sources the provider never re-checks for expiry (developer.fantastic.jobs recommended strategy).
SOURCES_WITHOUT_EXPIRY_FEED = frozenset({"wellfound", "ycombinator"})


def _coverage_flags(row: Mapping[str, Any], facts: Any, flags: List[str], now: datetime) -> None:
    """Unknown or missing data is carried forward as data, never as a rejection."""
    if facts.get("employment_type").status == "unknown":
        flags.append("employment_type_unknown")
    if all(row.get(k) in (None, "", []) for k in _PROFILE_FIELDS):
        flags.append("company_profile_missing")
    if str(row.get("source") or "").lower() in SOURCES_WITHOUT_EXPIRY_FEED:
        flags.append("source_not_in_expired_feed:needs_freshness_rule")
    posted = _parse_dt(row.get("date_posted"))
    if posted and (now - posted).days > 30:
        flags.append(f"old_posting:{(now - posted).days}d")


def posting_identity(row: Mapping[str, Any]) -> Tuple[str, str]:
    """(posting canonical key, employer key); same construction as identity resolution."""
    org = _org_block(row)
    domain, slug, nk = employer_anchors({**org, "organization": org.get("organization") or row.get("org_linkedin_name") or ""})
    ekey = employer_key(domain, slug, nk)
    return (posting_canonical_key(ekey, row.get("title"), row.get("description_text")) if ekey else ""), ekey


__all__ = [
    "FLAG_ENV", "POLICY_VERSION", "GROUPS", "ALL_STEPS", "SENIORITY_MANAGEMENT", "ROLE_MAPPING", "PHYSICAL_EVIDENCE",
    "EMPLOYMENT_PRECISION", "INTERMEDIARY_PRECISION", "FIELD_PRECISION", "TRAVEL_PRECISION", "ELIGIBILITY_FLAGS",
    "ELIGIBILITY", "INDUSTRY_LABELS", "GOVERNMENT_EMPLOYER", "QUOTA_SALES", "ESSENTIAL_FIELD",
    "approved_industry_query_labels", "federal_clearance", "government_employer", "sales_scope", "essential_field_travel",
    "THIRD_PARTY_REPOST", "third_party_repost",
    "CRM_EXCLUSION", "candidate_enabled", "classify_title", "title_families", "group_scores", "location_bound_evidence",
    "approved_industry_exclusion", "crm_keys", "qualify_candidate", "posting_identity",
]
