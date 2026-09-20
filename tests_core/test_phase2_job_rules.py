"""Phase 2 audit (2026-09-19): job-qualification rules that wrongly rejected valid jobs.

Authority (Luis, 2026-09-19): jobs with direct reports and C-level/VP/Head/Director/
Lead/Senior seniority CAN qualify. Never reject a job solely because its title contains
Director, VP, Head, Chief, Lead or Senior; because it manages people; or because it owns
a department or budget. Seniority affects decision-maker mapping, not job eligibility.

Three hypotheses, three commits: seniority (Task 1), people management (Task 2),
physical-duty boilerplate (Task 3). See .superpowers/sdd/phase2/task-1-3-brief.md.
"""
from __future__ import annotations

from tgtc_core.domain.facts import extract_job_facts, size_state


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


# --- Task 3 boundary pins, fix round 1 (2026-09-19): IMPORTANT finding --------
# test_real_physical_core_duty_still_excludes above never touches the lift/lifting
# pattern or FACILITY_LIFT_INCIDENTAL (it only exercises the untouched forklift
# span), so it does not by itself prove the narrowing is scoped correctly. These
# two pin the actual boundary the review asked for.

def test_lift_with_ada_qualifier_and_genuine_physical_evidence_still_excludes():
    """A lift clause carrying the ADA qualifier ("occasionally") that ALSO shares
    its sentence with other physical evidence (forklift) must still exclude -- the
    narrowing only clears a lift clause that is the SOLE physical evidence."""
    facts = extract_job_facts(title="Warehouse Associate",
                              description="Occasionally lift up to 25 pounds while operating a forklift on the floor.")
    assert any("physical" in e.reason for e in facts.exclusions)


def test_lift_with_only_the_ada_qualifier_as_evidence_does_not_exclude():
    """Mirrors RULE_AUDIT.md's `facility_lift_25` probe verbatim ("ADA boilerplate
    on an office job"): when the lift clause's ONLY physical evidence is the
    qualifier itself, it must not exclude."""
    facts = extract_job_facts(title="Customer Success Manager",
                              description="Physical demands: must be able to lift up to 25 pounds occasionally.")
    assert not any("physical" in e.reason for e in facts.exclusions)


# --- Task 4: employment type, three states (2026-09-19, Luis) ----------------
# Decision 1: keep excluding part-time/contractor/temporary/freelance/internship/
# unpaid/volunteer/commission-only/equity-only; full-time stays in scope. An
# explicit employer statement beats a provider tag; a provider tag that
# CONTRADICTS an explicit statement produces unknown + a review reason, never an
# automatic rejection. An explicitly excluded type still excludes. An incidental
# occurrence of an excluded word in unrelated prose never decides employment
# type. Brief's test bodies use `provider_employment_type=`, which is not a real
# parameter of `extract_job_facts`; adapted to `ai_employment_type=`, the real
# structured provider-tag parameter this rule already reads (facts.py:277). The
# brief's pseudocode also treats `e` (an `Exclusion`) as a string; adapted to
# `e.reason`, following the same adaptation tasks 1-3 made (task-1-3-report.md).

def test_incidental_contract_word_keeps_full_time():
    facts = extract_job_facts(title="Customer Success Manager",
                              description="Full-time. You will own contract renewals for existing accounts.",
                              ai_employment_type="FULL_TIME")
    assert facts.employment == "full_time"
    assert not any(e.reason.startswith("employment") for e in facts.exclusions)


def test_provider_tag_conflicting_with_explicit_statement_is_unknown_not_rejected():
    facts = extract_job_facts(title="Finance Analyst",
                              description="This is a full-time, permanent position.",
                              ai_employment_type="PART_TIME")
    assert facts.employment == "unknown"
    assert not any(e.reason.startswith("employment") for e in facts.exclusions)
    assert any("employment" in r for r in facts.review_reasons)


def test_explicit_part_time_still_excludes():
    facts = extract_job_facts(title="Bookkeeper", description="Part-time, 20 hours per week.",
                              ai_employment_type="PART_TIME")
    assert any(e.reason.startswith("employment") for e in facts.exclusions)


def test_commission_only_and_equity_only_still_exclude():
    for text in ("This is a commission-only position.", "Equity-only compensation, no salary."):
        facts = extract_job_facts(title="Sales Associate", description=text, ai_employment_type="FULL_TIME")
        assert any(e.reason.startswith("employment") for e in facts.exclusions), text


def test_incidental_fixed_term_clause_keeps_full_time():
    """Not in the brief's four test bodies, but required by its own 'Measured defect'
    text verbatim: "an at-will 'fixed term' clause... trips the employment rule
    today." An at-will/introductory-period qualifier on a bare 'fixed term' mention
    is incidental (mirrors task 3's FACILITY_LIFT_INCIDENTAL narrowing); the explicit
    N-month contract/term phrasing is untouched and still excludes on its own."""
    facts = extract_job_facts(
        title="Customer Success Manager",
        description=("This is a full-time position. Note: an at-will fixed term "
                      "introductory period applies to new hires per company policy."),
        ai_employment_type="FULL_TIME")
    assert facts.employment == "full_time"
    assert not any(e.reason.startswith("employment") for e in facts.exclusions)


def test_genuine_fixed_term_contract_still_excludes():
    facts = extract_job_facts(title="Program Manager",
                              description="This is a 6-month fixed term contract with possible extension.",
                              ai_employment_type="FULL_TIME")
    assert any(e.reason.startswith("employment:fixed_term") for e in facts.exclusions)


# --- Task 5: company size, three states (2026-09-19, Luis) -------------------
# Decision 2: target 25-1,000 employees. All reliable populated sources agree
# inside -> accept; all agree outside -> reject; sources conflict across the
# boundary -> firmographic_conflict/unknown_firmographics, preserved, never a
# reject and never counted as confirmed in-range. `size_state` is a new,
# standalone, source-agnostic function on facts.py -- it does not depend on a
# job posting at all, so the brief's test bodies are used verbatim.

def test_sources_agree_inside_range_accepts():
    assert size_state(headcount=400, size_band="201-500") == "in_range"


def test_sources_agree_outside_range_rejects():
    assert size_state(headcount=5000, size_band="1,001-5,000") == "out_of_range"


def test_sources_conflicting_across_the_boundary_is_a_conflict():
    assert size_state(headcount=400, size_band="1,001-5,000") == "firmographic_conflict"


def test_single_populated_source_inside_range_accepts():
    assert size_state(headcount=400, size_band=None) == "in_range"


def test_no_populated_source_is_unknown():
    assert size_state(headcount=None, size_band=None) == "unknown_firmographics"


def test_a_band_straddling_the_boundary_is_not_a_conflict_by_itself():
    # 501-1,000 lies inside; 1,001-5,000 lies outside; a band that spans 1,000 is indeterminate
    assert size_state(headcount=None, size_band="501-2,000") == "unknown_firmographics"


def test_conflict_is_never_an_exclusion():
    facts = extract_job_facts(title="Controller", description="Remote finance role.",
                              company={"headcount": 400, "size_band": "1,001-5,000"})
    assert not any(e.reason.startswith("company:size") for e in facts.exclusions)
    assert any("firmographic_conflict" in r for r in facts.review_reasons)


def test_company_size_out_of_range_excludes():
    facts = extract_job_facts(title="Controller", description="Remote finance role.",
                              company={"headcount": 5000, "size_band": "1,001-5,000"})
    assert any(e.reason.startswith("company:size") for e in facts.exclusions)
    assert not facts.review_reasons


def test_company_size_conflict_resolved_by_description_stated_headcount():
    """Task 5's 'cheapest resolution first' step 1: an employee count stated in the
    description text. Here it agrees with the size_band (both outside), resolving
    the headcount-vs-band conflict without a paid call."""
    facts = extract_job_facts(
        title="Controller",
        description="Remote finance role at a company of about 3,000 employees worldwide.",
        company={"headcount": 400, "size_band": "1,001-5,000"})
    assert any(e.reason == "company:size:out_of_range" for e in facts.exclusions)
    assert not facts.review_reasons


def test_no_company_argument_adds_no_size_fact_or_exclusion():
    """Backward compatibility: every caller before this task omits `company`."""
    facts = extract_job_facts(title="Controller", description="Remote finance role.")
    assert not any(e.reason.startswith("company:size") for e in facts.exclusions)
    assert not any(r.startswith("company:") for r in facts.review_reasons)
