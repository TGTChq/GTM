"""Regression tests: the ``part_time`` and ``internship`` employment-negative
rules must require role-defining context.

Both previously matched a bare token against any sentence in a job description,
so a single incidental mention anywhere in a 5,000-character posting rejected an
otherwise valid full-time job. Measured over the 192 JobGate rejections that
Fix C made reachable: ``internship`` 8% precision (4/50), ``part_time`` 3%
(1/32). The seven sibling rules that already require context
(contract / fractional / unpaid / ...) scored 100%.

These tests pin the narrowed behaviour: incidental mentions are ignored,
genuinely role-defining statements are still rejected, and no other employment
restriction is weakened.
"""
from __future__ import annotations

import re

import pytest

from job_fact_extractor import EMPLOYMENT_NEGATIVES

RULES = dict(EMPLOYMENT_NEGATIVES)


def fires(rule: str, text: str) -> bool:
    """Mirror _matching(): a rule fires when any pattern matches the sentence."""
    return any(re.search(pattern, text, re.I) for pattern in RULES[rule])


# ---------------------------------------------------------------------------
# internship — incidental mentions must NOT reject (all seen in the live audit)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "3+ years of professional software development experience (non-internship)",
    "At least one internship in the landscape industry OR equivalent industry work experience",
    "0-2 years of experience as a copywriting intern, junior copywriter, or similar role",
    "Hands-on experience managing campaigns in a professional or internship setting",
    "Our physicians have advanced fellowship training in various subspecialties",
    "taught by industry experts in an apprenticeship model that builds real skills",
    "View top content Human Resources Intern jobs Junior Developer jobs",
    "You will mentor our interns and junior engineers",
    "We run a summer internship programme for local students",
    "Previous internship experience is a plus",
])
def test_internship_incidental_mentions_do_not_fire(text):
    assert not fires("internship", text), f"false positive on: {text!r}"


@pytest.mark.parametrize("text", [
    "This is an internship based in our Austin office",
    "The position is an apprenticeship lasting six months",
    "Internship position supporting the marketing team",
    "We are seeking an intern to join the data team",
    "Apply for an internship with our engineering group",
    "As an intern you will shadow senior engineers",
    "This role is a returnship for professionals re-entering the workforce",
    "Externship opportunity for final-year students",
])
def test_genuine_internships_still_rejected(text):
    assert fires("internship", text), f"false negative on: {text!r}"


# ---------------------------------------------------------------------------
# part_time — incidental mentions must NOT reject
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Part-time employees and interns are not eligible to participate",
    "Premier Truck Rental hires full-time, part-time, contractor, and intern positions",
    "Thousand Palms, California, USA Full-Time/Part-Time Full-Time Job Description",
    "Build trusted relationships with employees across both the full-time and part-time workforce",
    "All Full-Time Colleagues qualify and Part-Time Colleagues qualify for most benefits",
    "You will manage a team of part-time associates",
    "Benefits are available to full-time and part-time staff after 90 days",
])
def test_part_time_incidental_mentions_do_not_fire(text):
    assert not fires("part_time", text), f"false positive on: {text!r}"


@pytest.mark.parametrize("text", [
    "This is a part-time position based in Denver",
    "The role is part time, approximately three days a week",
    "Part-time role supporting the finance team",
    "We are hiring a part-time bookkeeper",
    "This is a part time opportunity",
    "Part-time hours with flexible scheduling",
    "up to 20 hours per week",
    "approximately 25 hrs/week",
])
def test_genuine_part_time_still_rejected(text):
    assert fires("part_time", text), f"false negative on: {text!r}"


# ---------------------------------------------------------------------------
# nothing else may change
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule,text", [
    ("contract", "This is a contract role for six months"),
    ("contract", "You will be engaged as an independent contractor"),
    ("contract", "This is a contract-to-hire position"),
    ("fixed_term", "This is a fixed-term appointment"),
    ("fixed_term", "12-month contract covering parental leave"),
    ("temporary", "This is a temporary assignment through year end"),
    ("temporary", "temp-to-hire opportunity"),
    ("freelance", "We are seeking a freelance designer"),
    ("seasonal", "This is a seasonal position for the holiday period"),
    ("fractional", "This is a fractional CFO engagement"),
    ("unpaid", "This role is unpaid and offers academic credit"),
    ("unpaid", "compensation is equity-only"),
])
def test_sibling_rules_are_unchanged(rule, text):
    assert fires(rule, text), f"{rule} regressed on: {text!r}"


@pytest.mark.parametrize("rule", [name for name, _ in EMPLOYMENT_NEGATIVES])
def test_every_rule_still_present(rule):
    assert RULES[rule], f"{rule} lost its patterns"


def test_rule_order_is_unchanged():
    """extract_job_facts breaks on the first matching rule; order is behaviour."""
    assert [name for name, _ in EMPLOYMENT_NEGATIVES] == [
        "part_time", "fixed_term", "fractional", "contract", "temporary",
        "freelance", "seasonal", "internship", "unpaid",
    ]


def test_bare_fellowship_no_longer_fires_alone():
    """Clinical 'fellowship training' is a credential, not an employment type.
    Genuine restricted fellowships remain covered by assess_restricted_work."""
    assert not fires("internship", "advanced fellowship training in orthopaedics")
    assert fires("internship", "This is a fellowship for early-career researchers")


def test_restricted_work_still_catches_genuine_programmes():
    """The contextual restricted-work layer must be untouched by this change."""
    from job_quality import assess_restricted_work
    for title in ("Marketing Intern", "Software Engineering Apprentice",
                  "Research Fellowship"):
        result = assess_restricted_work({"job_title": title, "job_description": "x"})
        assert not result.eligible, f"{title} should still be restricted"
