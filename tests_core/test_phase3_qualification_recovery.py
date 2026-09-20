"""Phase 3 audit (2026-09-20): qualification recovery — valid jobs the rules still reject,
and campaign assignments the classifier still makes without evidence.

Every change here comes from a ruling Luis has already made (OPEN_DECISIONS.md, resolution pass
2026-09-20) or from the frozen rubric's own "not sufficient" list, measured on the CALIBRATION
stratum and the 411-item purchased-corpus set. The holdout is not read.

One hypothesis per commit:
  A  industry mapping      — online/digital media is not an approved excluded industry (Q6, D3)
  B  substring exclusion   — a Public Trust determination is not a security clearance (D5)
  C  location/travel       — the numeric travel line is withdrawn; "essential" carries the test (D4)
  D  classifier evidence   — a campaign assignment needs deterministic support, per campaign
"""
from __future__ import annotations

from tgtc_core.domain.facts import RULE_VERSION, extract_job_facts
from tgtc_core.policy.requirements import EXCLUDED_INDUSTRIES, excluded_industry

#: Enough ordinary prose that the postings under test are judged on the clause being tested,
#: not on being too short for the classifier to read.
_FILLER = ("You will build reports and dashboards for the analytics team, work with stakeholders "
           "across the business, and document your work. This role is remote.")


# --- A: industry mapping ----------------------------------------------------
# Luis, OPEN_DECISIONS.md: "Online news, digital media and media production stay allowed —
# not among the fixed exclusions" (Q6, restated in the 2026-09-20 resolution pass), and D3
# keeps the APPROVED list as-is. The rubric's own §4.K agrees: "online news, digital media,
# internet publishing and media production are NOT on the approved list ... do not exclude".
# Broadcast media, newspapers and book publishing ARE on the approved list and stay.

MEDIA_LABELS_LUIS_ALLOWS = (
    "online media", "internet news", "news media", "media production",
    "digital news", "financial news",
)
APPROVED_MEDIA_EXCLUSIONS = ("broadcast media", "newspapers", "book publishing")


def test_online_and_digital_media_industries_are_not_excluded():
    for label in MEDIA_LABELS_LUIS_ALLOWS:
        assert excluded_industry(label) == "", label
        assert label not in EXCLUDED_INDUSTRIES, label


def test_the_approved_media_exclusions_are_untouched():
    for label in APPROVED_MEDIA_EXCLUSIONS:
        assert excluded_industry(label) == label, label


def test_the_rest_of_the_approved_industry_list_is_untouched():
    for label in ("staffing and recruiting", "government administration", "mental health care",
                  "hospitals and health care", "human resources services", "outsourcing/offshoring",
                  "events services", "chemicals", "medical practice"):
        assert excluded_industry(label) == label, label


def test_an_apollo_qualifier_suffix_still_matches_an_approved_label():
    assert excluded_industry("Broadcast Media / Television") == "broadcast media"


# --- B: a Public Trust determination is not a security clearance ------------
# Luis, OPEN_DECISIONS.md D5 (2026-09-20 resolution pass): "Only an actual security
# clearance (Secret/TS/SCI) or an explicitly stated federal clearance requirement excludes.
# A Public Trust determination, a Tier 1 investigation, an HSPD-12 PIV card and ordinary
# background checks do NOT." The approved exclusion is "required federal clearance", and a
# Public Trust determination is not a clearance.
#
# The frozen rubric §4.H agrees on the substring half: "the phrase 'public trust' used in a
# non-clearance sense ('building public trust')" is explicitly NOT sufficient. Measured on
# the two frozen corpora (7,349 rows): 46 rows are excluded on the bare "public trust"
# substring with no other clearance evidence in the posting; one of them is a Public
# Information Officer whose text reads "building public trust and community engagement".

_PUBLIC_TRUST_ONLY = (
    "Ability to hold a position of public trust with the US government.",
    "Clearance Requirement: NACI (Public Trust) to be obtained after Government acceptance.",
    "Must meet eligibility for a Public Trust background investigation for IT access.",
    "The PIO serves as the media liaison, building public trust and community engagement.",
    "Candidates must pass a standard background check and an HSPD-12 PIV credentialing process.",
)

_REAL_CLEARANCE = (
    "Clearance Requirement: Active TS/SCI clearance required. Active CI Polygraph required.",
    "Must be able to obtain and maintain a Top Secret security clearance based on a T5 investigation.",
    "Candidate must have the ability to hold and maintain a Secret level security clearance.",
    "Requires Top Secret/SCI with Full Scope Poly.",
)


def test_public_trust_alone_is_not_a_clearance_exclusion():
    for text in _PUBLIC_TRUST_ONLY:
        facts = extract_job_facts(title="Data Analyst", description=text + " " + _FILLER)
        assert not any(e.reason == "deliverability:security_clearance" for e in facts.exclusions), text
        assert facts.facts["security_clearance"].value is None, text


def test_a_real_clearance_requirement_still_excludes():
    for text in _REAL_CLEARANCE:
        facts = extract_job_facts(title="Software Engineer", description=text + " " + _FILLER)
        assert any(e.reason == "deliverability:security_clearance" for e in facts.exclusions), text


def test_public_trust_beside_a_real_clearance_still_excludes():
    facts = extract_job_facts(
        title="Principal Systems Analyst",
        description="Clearance Level Must Currently Possess: Top Secret/SCI. Public Trust/Other Required: None. " + _FILLER)
    assert any(e.reason == "deliverability:security_clearance" for e in facts.exclusions)


def test_the_clearance_decision_path_carries_the_audit_rule_version():
    facts = extract_job_facts(title="Software Engineer", description=_REAL_CLEARANCE[0] + " " + _FILLER)
    exclusion = next(e for e in facts.exclusions if e.reason == "deliverability:security_clearance")
    assert exclusion.rule_version == RULE_VERSION
    assert facts.facts["security_clearance"].rule_version == RULE_VERSION
