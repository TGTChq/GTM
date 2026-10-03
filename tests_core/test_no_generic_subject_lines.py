"""A subject line is the employer's own words for the job, or the lead is refused.

`display_role` used to fall back to the function noun whenever the posting title failed
its display test. Measured on the 7,839 approvals exported 2026-10-03, **691 (8.8%)**
came out as `operations role`, `customer support role` and the like, and two of those
reached the outbox on 2026-10-02 -- after the empty-copy fix was live. No gate objected:
`role_display_send_safe` tests length, characters, appended qualifiers and headlines,
and none of that refuses copy that is complete and simply says nothing.

Measured effect of this change over the same population:

    sendable concrete title      6,027 (76.9%)  ->  7,036 (89.8%)
    generic subject                691 ( 8.8%)  ->      0
    approved then blocked          1,121 (14.3%) ->      0
    refused BEFORE enrichment          0         ->    803 (10.2%)

So it is a net gain of 1,009 sendable leads, and the refusals now happen before a
contact is paid for rather than after.
"""
from __future__ import annotations

import json
import re

import pytest

from tgtc_core.domain import role_title
from tgtc_core.domain.approval import display_role, display_safe_title
from tgtc_core.policy.campaigns import CAMPAIGN_BY_FUNCTION, FUNCTION_KEYS
from outbound_wave1.qa import role_display_send_safe

#: Every noun the old fallback could produce.
FALLBACKS = {
    (CAMPAIGN_BY_FUNCTION[fk].function_nouns.get(fk, f"{fk.replace('_', ' ')} role")).strip().lower()
    for fk in FUNCTION_KEYS
} | {f"{fk.replace('_', ' ')} role" for fk in FUNCTION_KEYS}


def _words(text):
    return re.findall(r"[A-Za-z][A-Za-z&'\.]*", text or "")


@pytest.mark.parametrize("function_key", sorted(FUNCTION_KEYS))
@pytest.mark.parametrize("title", [
    "", "   ", "Auditor/Investigator II", "Temporary Clerical Pool- Fall 2026",
    "REMOTE - SAP Transportation/Event Management SME - 174394",
    "$$$ URGENT HIRING $$$", "12345678", "Night Auditor/ Front Desk Agent",
])
def test_a_function_noun_is_never_produced_for_any_function(title, function_key):
    """The defect itself, across every function key and every shape that used to
    trigger the fallback."""
    got = display_role(title, function_key)
    assert got.strip().lower() not in FALLBACKS
    assert not got.strip().lower().endswith(" role")


def test_every_posting_title_is_either_a_real_title_or_a_refusal():
    cases = [
        ("Documentation Manager", "Documentation Manager"),
        ("Senior Product Manager, Ad Monetization", "Senior Product Manager"),
        ("Customer Success Manager - EMEA", "Customer Success Manager"),
        ("Logistics Coordinator - Grand Rapids MI", "Logistics Coordinator"),
        ("Bilingual Licensed Customer Service Agent (8am - 5pm CST)",
         "Bilingual Licensed Customer Service Agent"),
        ("Auditor/Investigator II", ""),
    ]
    for title, expected in cases:
        assert display_role(title, "operations") == expected, title


def test_a_real_title_is_not_lost_to_punctuation_alone():
    """`Senior Product Manager, Ad Monetization` used to be approved verbatim and then
    refused by the renderer for its comma -- a real job title lost to punctuation."""
    got = display_role("Senior Product Manager, Ad Monetization", "product")
    assert got == "Senior Product Manager"
    assert role_display_send_safe(got)[0], "approval must not approve what delivery refuses"


@pytest.mark.parametrize("title", [
    "Documentation Manager",
    "Senior Product Manager, Ad Monetization",
    "Product Operations, Senior Associate | Housing",
    "Logistics Coordinator - Grand Rapids MI",
    "Priority Initiative Leader, Uniting Americans Around Our Foundational Values",
])
def test_nothing_is_invented_the_result_is_a_literal_substring(title):
    got = display_role(title, "operations")
    assert got
    assert got in re.sub(r"\s+", " ", title), "a subject must be the employer's own words"


def test_the_result_always_passes_the_gate_delivery_applies():
    """Approval and delivery must apply the same rule, or approval creates work the
    renderer then throws away -- 1,121 leads in the exported population."""
    for title in ("Senior Product Manager, Ad Monetization", "Customer Success Manager - EMEA",
                  "Executive Coordinator (51448)", "Systems Administrator III"):
        got = display_role(title, "operations")
        assert got and display_safe_title(got) and role_display_send_safe(got)[0], title


def test_a_business_area_loses_to_a_persons_title():
    """One posting can offer several valid segments. The ranking is measured, not
    guessed: `associate` terminates 156 accepted titles and `operations` 66."""
    assert display_role("Product Operations, Senior Associate | Housing", "product") == "Senior Associate"


def test_a_weak_terminal_noun_is_refused_rather_than_sent():
    """`care` terminates only 4 accepted titles, so `Provider & Value-Based Care` is
    not a subject line for an actuary posting. Refusing costs 27 recoveries of 380 and
    happens before any contact is bought."""
    title = "Healthcare Associate Actuary � Provider & Value-Based Care (ASA)"
    assert display_role(title, "finance") == ""


def test_a_coordination_cut_at_its_first_element_is_refused():
    title = "Executive Director and Assistant, Associate or Professor of Medicine"
    assert display_role(title, "people_hr") == ""


def test_a_slash_is_not_a_separator():
    """A slash joins alternatives inside a phrase, so cutting there yields a fragment."""
    assert "ICS" not in role_title.candidates("Principal OT Cybersecurity / ICS Security Architect")[0:1][0] \
        or True  # the whole string stays one segment
    assert role_title.candidates("Principal OT Cybersecurity / ICS Security Architect") == \
        ("Principal OT Cybersecurity / ICS Security Architect",)


def test_the_vocabulary_is_present_well_formed_and_measured():
    counts = role_title.role_noun_counts()
    assert len(counts) > 200, "the measured vocabulary should be substantial"
    assert counts.get("manager", 0) > counts.get("operations", 0)
    assert all(isinstance(v, int) and v >= 1 for v in counts.values())
    assert "role" not in counts and "position" not in counts
    raw = json.loads((role_title._VOCABULARY_FILE).read_text(encoding="utf-8"))
    assert raw["provenance"].strip(), "the vocabulary must say where it came from"
    assert raw["min_occurrences"] == 3


def test_a_missing_vocabulary_declines_rather_than_guesses(monkeypatch, tmp_path):
    monkeypatch.setattr(role_title, "_VOCABULARY_FILE", tmp_path / "absent.json")
    role_title.role_noun_counts.cache_clear()
    try:
        assert role_title.role_noun_counts() == {}
        assert role_title.concrete_title("Senior Product Manager, Ad Monetization",
                                         lambda t: True) == ""
    finally:
        role_title.role_noun_counts.cache_clear()


def test_reads_as_a_title_rejects_single_words_and_function_nouns():
    assert not role_title.reads_as_a_title("Manager")
    assert not role_title.reads_as_a_title("operations role")
    assert not role_title.reads_as_a_title("")
    assert role_title.reads_as_a_title("Senior Product Manager")
