"""A title can be affirmative evidence that the essential duties are physical.

Measured in the first production canary (2026-09-20): with the universe widened
and the nine campaigns exhaustive, 834 of 1,084 assignments fell through to the
OPERATIONS fallback, and sampling them showed what they actually were --
Residential Plumber, Soup Packer, Oil Delivery Driver, Manual Machinist,
Refrigeration Mechanic, Physical Therapist, Material Handler, Day Porter,
General Laborer.

`facts.py` excludes inherently physical duties from the DESCRIPTION text, and
those postings frequently never state it in the patterns it matches. The
fallback then dressed them up as Operations, which is precisely what the policy
forbids: "Do not use Operations to hide malformed or incomprehensible records."

This is a HARD EXCLUSION, not a routing preference, and it is exactly the list
the policy already names: patient care, warehouse labour, driving and delivery,
construction, equipment operation, manufacturing-floor production, laboratory
bench work, retail-floor service, food preparation, cleaning and facilities
maintenance, physical security, field installation and repair.

The rule is affirmative-evidence only. An ambiguous title still passes, because
unknown is not evidence of exclusion.
"""
from __future__ import annotations

import pytest

from tgtc_core.domain.classification import classify_posting
from tgtc_core.domain.exhaustive_routing import FLAG_ENV, physical_title_reason

ON = {FLAG_ENV: "1"}
DESC = (
    "Join our growing team. We offer competitive compensation, medical, dental and "
    "vision insurance, a 401(k) match and generous paid time off. We are an equal "
    "opportunity employer and consider all qualified applicants."
)


@pytest.mark.parametrize("title", [
    # patient care
    "Physical Therapist", "Medical Assistant II-Family Practice", "Pharmacy Technician",
    "Informatics Nurse", "Patient Service Representative", "Optician",
    "Dunedin Compassionate Caregiver Flexible Shifts - Full Time",
    # warehouse / fulfilment
    "Soup Packer", "Material Handler I (3rd Shift)", "Parts Staging Employee",
    # driving and delivery
    "Oil Delivery Driver Regular", "CO2 Delivery Driver", "Driver II (Non CDL)",
    # construction and trades
    "Residential Plumber", "Electrician 2", "Refrigeration Mechanic",
    "Skilled Laborer Waterproofing", "General Laborer",
    # equipment operation and manufacturing floor
    "Slitter Operator - Day Shift", "Manual Machinist (Day Shift)", "Supervisor, Manufacturing",
    # cleaning and facilities
    "1st Shift | Day Porter | Schaumburg, IL", "Entry Level Automotive Lot Porter",
    "Maintenance Technician", "Maintenance Tech",
    # field installation and repair
    "Field Service Technician", "Fayetteville Residential Upgrade Technician",
    "Calibration Technician",
    # classroom
    "Math Educator - Haven Middle School", "Lead Twos Teacher",
    "Special Education Personal Aide",
])
def test_physical_titles_are_excluded(title):
    assert physical_title_reason(title) is not None, f"{title!r} was not caught"
    result = classify_posting(title=title, description=DESC, inference=None, env=ON)
    assert result.excluded, f"{title!r} reached a campaign"
    assert result.compatible_functions == []


@pytest.mark.parametrize("title", [
    # the nine campaigns' own roles must never be caught by this rule
    "Senior Software Engineer", "Financial Analyst", "Product Manager",
    "Technical Recruiter", "Customer Success Manager", "Growth Marketing Manager",
    "Revenue Operations Manager", "Business Operations Manager", "Ecommerce Manager",
    "Account Executive", "Chief of Staff", "Data Engineer", "DevOps Engineer",
    "Technical Support Engineer", "Implementation Consultant", "Brand Designer",
    "Corporate Controller", "People Operations Manager", "Program Manager",
    # ambiguous, and ambiguous passes
    "Assistant Manager", "Project Manager", "Operations Analyst",
    "IT Systems Administrator", "Security Engineer", "Solutions Engineer",
])
def test_knowledge_work_titles_are_never_excluded(title):
    assert physical_title_reason(title) is None, f"{title!r} was wrongly caught"
    result = classify_posting(title=title, description=DESC, inference=None, env=ON)
    assert not result.excluded, f"{title!r} was wrongly rejected"
    assert result.compatible_functions


def test_the_exclusion_is_off_with_the_flag_off():
    """Rollback path stays byte-identical."""
    result = classify_posting(title="Residential Plumber", description=DESC, inference=None, env={})
    assert not result.excluded


def test_the_reason_is_named_and_stable():
    assert physical_title_reason("Residential Plumber") == "deliverability:physical_title"


def test_an_empty_or_unknown_title_is_not_excluded():
    assert physical_title_reason("") is None
    assert physical_title_reason(None) is None
    assert physical_title_reason("Coordinator") is None
