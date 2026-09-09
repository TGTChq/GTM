"""Titles are data. Equal responsibilities -> equal decision; attractive title + incompatible work -> excluded."""

from __future__ import annotations

import pytest

from tgtc_core.domain.classification import classify_posting
from tgtc_core.testing.corpus import CORPUS

POSITIVES = [e for e in CORPUS if e.expected_function and not e.expected_excluded]
GENERIC_TITLES = ["", "Team Member", "Specialist", "Associate", "Role", "Open Position"]


def _decide(title, description):
    r = classify_posting(description=description, title=title, countries=["US"], employment_type="FULL_TIME")
    return (r.excluded, r.exclusion_reason if r.excluded else "", tuple(r.compatible_functions))


@pytest.mark.parametrize("example", POSITIVES, ids=[e.key for e in POSITIVES])
def test_generic_or_missing_title_keeps_the_decision(example):
    baseline = _decide(example.title, example.description)
    assert baseline == (False, "", (example.expected_function,)), f"baseline for {example.key}: {baseline}"
    for generic in GENERIC_TITLES:
        assert _decide(generic, example.description) == baseline, f"{example.key} changed under title {generic!r}"


@pytest.mark.parametrize("example", POSITIVES, ids=[e.key for e in POSITIVES])
def test_title_from_another_function_does_not_move_the_decision(example):
    other = "Software Engineer" if example.expected_function != "engineering" else "Staff Accountant"
    assert _decide(other, example.description) == _decide(example.title, example.description)


def test_attractive_title_with_physical_work_is_excluded():
    e = next(x for x in CORPUS if x.key == "neg1")
    excluded, reason, fns = _decide(e.title, e.description)
    assert excluded and reason.startswith("deliverability:") and fns == ()


def test_leadership_title_is_excluded_even_with_ic_looking_work():
    excluded, reason, _ = _decide("Director of Finance", next(x for x in CORPUS if x.key == "fin2").description)
    assert excluded and reason == "seniority:leadership_or_principal"


def test_staff_accountant_is_not_a_leadership_title():
    e = next(x for x in CORPUS if x.key == "fin1")
    assert _decide(e.title, e.description) == (False, "", ("finance",))


def test_no_evidence_is_not_approved_and_not_a_review_class():
    e = next(x for x in CORPUS if x.key == "neg7")
    r = classify_posting(description=e.description, title=e.title, countries=["US"])
    assert not r.excluded and r.compatible_functions == [] and r.method == "deterministic"
    assert any(n.startswith("insufficient_evidence") for n in r.notes)
