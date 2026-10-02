"""Authoritative business-scope change (2026-09-20): the nine campaigns are
collectively exhaustive for every job that passes the hard eligibility gates.

Gated by ``TGTC_EXHAUSTIVE_NINE_CAMPAIGNS=1``. With the flag off every decision
must stay byte-identical to ``tgtc-core/2`` so the rollback path is a flag flip
and nothing else.

What the instruction changed, and what it did NOT:

* A job may no longer be rejected for "no campaign fit", an absent allowlist
  title, an imperfect role pattern, multi-function responsibilities or a
  classifier tie. Those become routing problems, never terminal rejections.
* The twelve approved hard exclusions still reject. Campaign scope never
  overrides an eligibility gate.
* Evidence discipline is unchanged: title plus actual position
  responsibilities. Employer boilerplate, benefits and EEO language are still
  not campaign evidence.
"""
from __future__ import annotations

import pytest

from tgtc_core.domain.classification import classify_posting
from tgtc_core.domain.exhaustive_routing import (
    FALLBACK_FUNCTION,
    FLAG_ENV,
    RULE_VERSION,
    exhaustive_enabled,
    route_by_title,
)
from tgtc_core.policy.campaigns import CAMPAIGN_BY_FUNCTION, FUNCTION_KEYS

ON = {FLAG_ENV: "1"}
OFF: dict = {}

# A description long enough to clear classify_posting's 120-char floor while
# carrying no functional signal of its own: benefits and EEO boilerplate only.
# Under the new scope a job like this must still be routed -- by its title --
# and the boilerplate must never be cited as the campaign evidence.
BOILERPLATE = (
    "We offer competitive compensation, medical, dental and vision insurance, a "
    "401(k) match, generous paid time off and a yearly learning stipend. We are "
    "an equal opportunity employer and all qualified applicants will receive "
    "consideration without regard to race, color, religion, sex or national origin."
)


def _classify(title: str, description: str, env=ON):
    return classify_posting(title=title, description=description, inference=None, env=env)


# --- the flag itself ---------------------------------------------------------


def test_flag_defaults_off():
    assert exhaustive_enabled(None) is False
    assert exhaustive_enabled({}) is False
    assert exhaustive_enabled({FLAG_ENV: "0"}) is False
    assert exhaustive_enabled({FLAG_ENV: "1"}) is True


def test_flag_off_leaves_an_unroutable_posting_undecided():
    """The rollback path: with the flag off nothing routes by title."""
    result = _classify("Chief of Staff", BOILERPLATE, env=OFF)
    assert result.compatible_functions == []
    assert not result.excluded


# --- every one of the nine campaigns is reachable by title -------------------


@pytest.mark.parametrize(
    "title,function_key",
    [
        # Product
        ("Senior Product Manager", "product"),
        ("Director of Product Design", "product"),
        ("Product Analytics Lead", "product"),
        # Operations
        ("Business Operations Manager", "operations"),
        ("Program Manager", "operations"),
        ("Procurement Specialist", "operations"),
        ("Legal Operations Manager", "operations"),
        # Finance
        ("Senior Accountant", "finance"),
        ("FP&A Manager", "finance"),
        ("Corporate Controller", "finance"),
        # People & HR
        ("Technical Recruiter", "people_hr"),
        ("People Operations Manager", "people_hr"),
        ("Compensation and Benefits Analyst", "people_hr"),
        # Ecommerce
        ("Ecommerce Manager", "ecommerce"),
        ("Marketplace Operations Lead", "ecommerce"),
        ("Merchandising Manager", "ecommerce"),
        # Customer Experience
        ("Customer Success Manager", "customer_success"),
        ("Implementation Consultant", "customer_success"),
        ("Technical Support Engineer", "customer_support"),
        # Marketing & Creative
        ("Growth Marketing Manager", "marketing"),
        ("Brand Designer", "marketing"),
        ("Communications Director", "marketing"),
        # GTM Systems & Revenue Automation
        ("Revenue Operations Manager", "gtm_revenue"),
        ("Sales Operations Analyst", "gtm_revenue"),
        ("Partnerships Manager", "gtm_revenue"),
        # AI & Technical Automation
        ("Senior Software Engineer", "engineering"),
        ("Machine Learning Engineer", "engineering"),
        ("DevOps Engineer", "engineering"),
        ("IT Systems Administrator", "engineering"),
        ("Security Engineer", "engineering"),
        ("QA Automation Engineer", "engineering"),
        ("Data Engineer", "engineering"),
    ],
)
def test_title_routes_to_expected_function(title, function_key):
    route = route_by_title(title)
    assert route is not None, f"{title!r} did not route"
    assert route.function_key == function_key


def test_all_nine_campaigns_are_reachable_by_title():
    """No campaign may be unreachable: that would silently starve one offer."""
    reached = set()
    for title, _ in _TITLE_SAMPLES:
        route = route_by_title(title)
        if route:
            reached.add(CAMPAIGN_BY_FUNCTION[route.function_key].key)
    assert len(reached) == 9, sorted(reached)


_TITLE_SAMPLES = [
    ("Senior Product Manager", "product"),
    ("Business Operations Manager", "operations"),
    ("Senior Accountant", "finance"),
    ("Technical Recruiter", "people_hr"),
    ("Ecommerce Manager", "ecommerce"),
    ("Customer Success Manager", "customer_success"),
    ("Growth Marketing Manager", "marketing"),
    ("Revenue Operations Manager", "gtm_revenue"),
    ("Senior Software Engineer", "engineering"),
]


def test_every_routed_function_key_is_a_real_campaign_function():
    for title, _ in _TITLE_SAMPLES:
        route = route_by_title(title)
        assert route.function_key in FUNCTION_KEYS


# --- "no campaign fit" is no longer terminal ---------------------------------


def test_title_only_posting_is_routed_not_dropped():
    result = _classify("Senior Software Engineer", BOILERPLATE)
    assert result.compatible_functions == ["engineering"]
    assert not result.excluded


def test_unmatched_corporate_role_falls_back_to_operations():
    """'other legitimate corporate roles without a better functional destination'."""
    result = _classify("Chief of Staff", BOILERPLATE)
    assert result.compatible_functions == [FALLBACK_FUNCTION]
    assert not result.excluded


def test_fallback_is_operations_not_a_rejection():
    assert FALLBACK_FUNCTION == "operations"


def test_routed_posting_always_carries_evidence():
    """approval.py refuses on `no_responsibility_evidence`; a title-routed job
    must therefore carry its title as the evidence, not arrive empty."""
    for title in ("Senior Software Engineer", "Chief of Staff", "Senior Accountant"):
        result = _classify(title, BOILERPLATE)
        assert result.responsibilities, f"{title!r} routed with no evidence"
        assert title.lower() in result.responsibilities[0].excerpt.lower()


def test_boilerplate_is_never_cited_as_campaign_evidence():
    result = _classify("Chief of Staff", BOILERPLATE)
    cited = " ".join(r.excerpt.lower() for r in result.responsibilities)
    for banned in ("401(k)", "equal opportunity", "dental", "paid time off"):
        assert banned not in cited


def test_routed_result_is_stamped_with_the_rule_version():
    result = _classify("Chief of Staff", BOILERPLATE)
    assert result.rule_version == RULE_VERSION


# --- hard exclusions still win ----------------------------------------------


def test_part_time_is_still_rejected_despite_a_routable_title():
    result = classify_posting(
        title="Senior Software Engineer",
        description=BOILERPLATE,
        employment_type="PART_TIME",
        inference=None,
        env=ON,
    )
    assert result.excluded
    assert result.compatible_functions == []


def test_non_us_is_gated_by_compliance_not_by_classification():
    """Geography is not a classification-time exclusion and must not become one:
    acquisition, enrichment and outreach are three separate permissions. A DE
    job classifies normally (job_acquisition_allowed) and is stopped later, at
    the outreach gate, which is where the US-only portfolio is enforced."""
    from tgtc_core.policy.compliance import cold_email_allowed, job_acquisition_allowed

    result = classify_posting(
        title="Senior Software Engineer",
        description=BOILERPLATE,
        countries=["DE"],
        inference=None,
        env=ON,
    )
    assert not result.excluded
    assert result.compatible_functions == ["engineering"]

    assert job_acquisition_allowed("DE").allowed is True
    assert cold_email_allowed("DE").allowed is False
    assert cold_email_allowed("US").allowed is False  # fails closed without controls


def test_staffing_agency_is_still_rejected_despite_a_routable_title():
    result = classify_posting(
        title="Senior Software Engineer",
        description=BOILERPLATE,
        employer_name="Acme Staffing Solutions",
        agency_flag=True,
        inference=None,
        env=ON,
    )
    assert result.excluded
    assert result.compatible_functions == []


# --- seniority / leadership are explicitly NOT rejection reasons -------------


@pytest.mark.parametrize(
    "title",
    [
        "VP of Engineering",
        "Chief Technology Officer",
        "Head of People",
        "Director of Finance",
        "Senior Director, Business Operations",
    ],
)
def test_seniority_and_leadership_are_routed_not_rejected(title):
    result = _classify(title, BOILERPLATE)
    assert not result.excluded
    assert result.compatible_functions, f"{title!r} was dropped"


# --- quota-carrying sales: behaviour change under the new scope --------------


def test_quota_carrying_sales_still_excluded_with_the_flag_off():
    """The Phase 2 signed-off rule is untouched on the rollback path."""
    result = _classify(
        "Account Executive",
        "You will own a full sales quota, run point on the entire sales cycle from "
        "qualification to close, and prospect into new accounts through outbound cold "
        "calls to build pipeline and hit your numbers every quarter.",
        env=OFF,
    )
    assert result.excluded and result.exclusion_reason == "role:quota_carrying_sales"


def test_quota_carrying_sales_routes_to_gtm_with_the_flag_on():
    """The new hard-exclusion list does not contain it, and GTM Systems now
    explicitly covers 'sales, business development, partnerships'."""
    result = _classify(
        "Account Executive",
        "You will own a full sales quota, run point on the entire sales cycle from "
        "qualification to close, and prospect into new accounts through outbound cold "
        "calls to build pipeline and hit your numbers every quarter.",
    )
    assert not result.excluded
    assert result.compatible_functions == ["gtm_revenue"]


# --- deterministic first, stable tie-break ----------------------------------


def test_description_dominance_still_wins_over_the_title():
    """Deterministic description evidence outranks a title route."""
    result = _classify(
        "Chief of Staff",
        "You will own the revenue operations function for our go-to-market team: "
        "administer Salesforce administration and CRM hygiene, build pipeline reporting "
        "and forecasting dashboards for the leadership team, and manage lead routing and "
        "territory assignment across the sales organization.",
    )
    assert result.compatible_functions == ["gtm_revenue"]


def test_routing_is_deterministic_and_repeatable():
    first = _classify("Chief of Staff", BOILERPLATE)
    second = _classify("Chief of Staff", BOILERPLATE)
    assert first.compatible_functions == second.compatible_functions


def test_exactly_one_campaign_is_assigned():
    for title, _ in _TITLE_SAMPLES:
        result = _classify(title, BOILERPLATE)
        assert len(result.compatible_functions) == 1
        assert len(result.campaign_keys) == 1


# --- short descriptions go to review, never to rejection --------------------


def test_short_description_with_a_routable_title_still_routes():
    result = _classify("Senior Software Engineer", "Join us.")
    assert result.compatible_functions == ["engineering"]
    assert not result.excluded


def test_no_title_and_no_description_is_review_not_rejection():
    result = _classify("", "Join us.")
    assert not result.excluded
    assert result.compatible_functions == []
    assert any("insufficient_evidence" in n for n in result.notes)
