"""A stale classification must not turn into a lead.

The first production canary classified ~1,600 postings before the physical-title
exclusion existed. Those rows carry a receipt for the SAME policy version, so
they are never re-classified, and their opportunities stayed open pointing at
OPERATIONS -- Residential Plumber, Soup Packer, Oil Delivery Driver.

The qualify stage therefore re-checks the title itself, before any paid
enrichment, exactly where the other known disqualifiers already sit. Defence in
depth: the classifier catches new work, this catches everything already in
flight, and neither spends a credit to rediscover a rejection.
"""
from __future__ import annotations

from tgtc_core.services.opportunity import physical_posting_block

ON = {"TGTC_EXHAUSTIVE_NINE_CAMPAIGNS": "1"}


def test_a_physical_title_is_blocked_with_a_named_reason():
    assert physical_posting_block({"title": "Residential Plumber"}, ON) == "deliverability:physical_title"
    assert physical_posting_block({"title": "Soup Packer"}, ON) == "deliverability:physical_title"
    assert physical_posting_block({"title": "Oil Delivery Driver"}, ON) == "deliverability:physical_title"


def test_knowledge_work_is_not_blocked():
    for title in ("Senior Software Engineer", "Financial Analyst", "Business Operations Manager",
                  "Chief of Staff", "Technical Recruiter", "Revenue Operations Manager"):
        assert physical_posting_block({"title": title}, ON) is None, title


def test_nothing_is_blocked_with_the_flag_off():
    assert physical_posting_block({"title": "Residential Plumber"}, {}) is None


def test_a_missing_posting_or_title_is_not_blocked():
    assert physical_posting_block(None, ON) is None
    assert physical_posting_block({}, ON) is None
    assert physical_posting_block({"title": ""}, ON) is None
