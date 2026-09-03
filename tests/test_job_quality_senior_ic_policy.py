"""ROLE_ALLOW_SENIOR_IC must relax Senior/Sr ICs and nothing else.

`role_gate.py` has always honoured this flag; `job_quality.py` runs first and did
not, so a flag set to admit Senior ICs still lost them. Two protections stand in
front of the flag, because a naive gate leaks both: policy-excluded title levels
(a "Senior Staff Accountant" is still Staff) and genuine people authority.

The distinction the detector draws is the OBJECT of the verb. Managing projects,
accounts, campaigns, vendors, priorities, deadlines or SSRS reports is not
authority over people. Neither is "performance management" of infrastructure, nor
an HR programme the role administers, nor hiring language in employer boilerplate.

Cases are written as generic shapes, never copied from any corpus record.
"""

import pytest

import config
from job_quality import has_people_authority, senior_ic_permitted


def job(title, description="", role="Accountant"):
    return {"job_title": title, "job_description": description, "_matched_role": role}


@pytest.fixture
def senior_ic_on(monkeypatch):
    monkeypatch.setattr(config, "ROLE_ALLOW_SENIOR_IC", True)


@pytest.fixture
def senior_ic_off(monkeypatch):
    monkeypatch.setattr(config, "ROLE_ALLOW_SENIOR_IC", False)


class TestSeniorICTitlesAreAllowed:
    @pytest.mark.parametrize("title", [
        "Senior Accountant", "Sr. Accountant", "Sr Accountant",
        "Senior Software Engineer", "Senior Copywriter", "Senior Data Engineer",
        "Senior Graphic Designer", "Senior Financial Analyst",
        "Senior Accountant - Corporate Accounting", "Senior Account Executive",
    ])
    def test_plain_senior_ic_title_passes(self, senior_ic_on, title):
        assert senior_ic_permitted(job(title, "You will prepare monthly journal entries."))


class TestNonPeopleManagementIsNotAuthority:
    @pytest.mark.parametrize("text", [
        "Ability to manage multiple priorities and meet deadlines.",
        "Manage multiple client engagements and deadlines during the year.",
        "Manage projects from kickoff through delivery.",
        "Manage programs across the portfolio.",
        "Manage a book of accounts and grow revenue.",
        "Manage marketing campaigns end to end.",
        "Manage vendors and third-party relationships.",
        "Create and manage SSRS reports for multiple customers and subject areas.",
        "Prepare and manage monthly reports for the finance function.",
        "Experience with headcount planning and labor cost forecasting.",
        "Maintain system monitoring, capacity planning, and performance management "
        "across business-critical infrastructure.",
        "Experience facilitating the full employee lifecycle: recruiting, onboarding, "
        "performance management, off-boarding.",
        "Our teams utilize a continuous performance management and development "
        "structure for feedback.",
        "They may have a technical leadership role on the platform.",
        "Lead initiatives that improve the close process.",
        "Mentor junior colleagues informally.",
        "Strong stakeholder management skills.",
        "We move fast, value teamwork, and hire people who want to make an impact.",
        "We are constantly building our team to achieve our goals.",
        "Prepare weekly recruiting reports and hiring status updates.",
    ])
    def test_not_treated_as_people_authority(self, text):
        assert not has_people_authority(text), text

    def test_senior_ic_still_permitted_with_such_text(self, senior_ic_on):
        assert senior_ic_permitted(job(
            "Senior Accountant",
            "Ability to manage multiple priorities and deadlines. Create and manage "
            "SSRS reports. Mentor junior colleagues."))


class TestGenuinePeopleAuthorityBlocks:
    @pytest.mark.parametrize("text", [
        "Ability to successfully oversee direct reports.",
        "This role will have three direct reports.",
        "You will supervise our AR/AP specialists.",
        "Train, coach, and supervise staff Accountants and junior team members.",
        "Supervisory Responsibilities:",
        "SUPERVISORY SCOPE:",
        "You will own technical direction while managing and mentoring a team of "
        "frontend engineers.",
        "They will have a role mentoring and managing teams of junior and mid-level "
        "software engineers.",
        "Responsible for leading a high-performing team of direct reports.",
        "Conduct performance reviews for your reports.",
        "Develop and communicate clear performance expectations and measures.",
        "Hire and manage the analytics function.",
    ])
    def test_detected_as_people_authority(self, text):
        assert has_people_authority(text), text

    def test_senior_title_with_authority_is_blocked(self, senior_ic_on):
        assert not senior_ic_permitted(job(
            "Senior Accountant", "You will supervise our AR/AP specialists."))


class TestPassiveSupervisionIsNotAuthority:
    @pytest.mark.parametrize("text", [
        "A self-starter who requires limited supervision.",
        "With guidance and supervision, work on client deliverables.",
        "Under the supervision of the Controller, prepare the monthly close.",
        "This position is generally subject to moderate direct supervision.",
        "The position is supervised by the Controller.",
    ])
    def test_being_supervised_is_not_authority(self, text):
        assert not has_people_authority(text), text


class TestExcludedTitleLevelsAreNeverRelaxed:
    @pytest.mark.parametrize("title", [
        "Senior Staff Accountant", "Sr Staff Accountant", "Staff Accountant",
        "Principal Account Executive", "Senior Principal Engineer",
        "Lead Data Engineer", "Head of Finance", "Director of Operations",
        "Senior Director, Finance", "VP of Marketing", "Vice President, Sales",
        "Chief of Staff",
    ])
    def test_excluded_level_blocked_even_with_senior(self, senior_ic_on, title):
        assert not senior_ic_permitted(job(title, "You will prepare journal entries."))

    @pytest.mark.parametrize("title", [
        "Senior Lead Generation Specialist",
        "Senior Demand Gen Analyst",
    ])
    def test_lead_generation_is_not_a_seniority_level(self, senior_ic_on, title):
        assert senior_ic_permitted(job(title, "You will build campaigns."))


class TestAmbiguousFailsClosed:
    def test_dual_level_posting_is_blocked(self, senior_ic_on):
        assert not senior_ic_permitted(job(
            "FP&A Manager / Senior FP&A Analyst", "You will own the forecast model."))

    def test_bare_supervisory_heading_fails_closed(self, senior_ic_on):
        assert not senior_ic_permitted(job(
            "Senior FP&A Analyst",
            "Supervisory Responsibilities:\n\nDrives enterprise-wide planning."))

    def test_missing_description_does_not_auto_allow_authority(self, senior_ic_on):
        # No description is not evidence of authority, so a plain Senior title
        # still passes -- absence of text must not become a rejection.
        assert senior_ic_permitted(job("Senior Accountant", ""))


class TestFlagOff:
    @pytest.mark.parametrize("title", [
        "Senior Accountant", "Sr. Accountant", "Senior Software Engineer"])
    def test_flag_off_retains_existing_rejection(self, senior_ic_off, title):
        assert not senior_ic_permitted(job(title, "You will prepare journal entries."))
