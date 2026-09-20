"""Phase 2 audit (2026-09-19): job-qualification rules that wrongly rejected valid jobs.

Authority (Luis, 2026-09-19): jobs with direct reports and C-level/VP/Head/Director/
Lead/Senior seniority CAN qualify. Never reject a job solely because its title contains
Director, VP, Head, Chief, Lead or Senior; because it manages people; or because it owns
a department or budget. Seniority affects decision-maker mapping, not job eligibility.

Three hypotheses, three commits: seniority (Task 1), people management (Task 2),
physical-duty boilerplate (Task 3). See .superpowers/sdd/phase2/task-1-3-brief.md.
"""
from __future__ import annotations

from tgtc_core.domain.facts import extract_job_facts


# --- Task 1: seniority never rejects on its own -----------------------------

def test_director_title_alone_does_not_exclude():
    facts = extract_job_facts(title="Director of Finance", description="Own monthly close and FP&A for a 300-person company. Remote.")
    assert not any(e.reason.startswith("seniority") for e in facts.exclusions)


def test_leadership_and_ic_titles_not_excluded_by_seniority():
    for title in ("Chief of Staff", "Art Director", "EA to the CEO", "Senior Lead Engineer", "VP of Marketing", "Head of People"):
        facts = extract_job_facts(title=title, description="Standard office role, remote friendly.")
        assert not any(e.reason.startswith("seniority") for e in facts.exclusions), title


# --- Task 2: people management never rejects on its own ---------------------

def test_people_management_is_not_an_exclusion():
    facts = extract_job_facts(title="Marketing Manager",
                              description="Supervisory Responsibilities\nLead a team of three marketers. Own the budget.")
    assert not any("people_management" in e.reason for e in facts.exclusions)


def test_negated_direct_reports_is_not_people_management():
    facts = extract_job_facts(title="Analyst", description="This role has no direct reports.")
    assert not any("people_management" in e.reason for e in facts.exclusions)


# --- Task 3: physical-duty boilerplate ---------------------------------------

def test_lifting_boilerplate_does_not_exclude_office_role():
    facts = extract_job_facts(title="Staff Accountant",
                              description="Remote. Ability to lift up to 25 pounds occasionally. Prepare journal entries.")
    assert not any("physical" in e.reason for e in facts.exclusions)


def test_real_physical_core_duty_still_excludes():
    facts = extract_job_facts(title="Warehouse Associate",
                              description="Operate a forklift for the duration of the shift and palletize orders on the floor.")
    assert any("physical" in e.reason for e in facts.exclusions)
