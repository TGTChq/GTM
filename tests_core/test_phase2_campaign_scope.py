"""Phase 2 audit (2026-09-19): GTM Systems campaign scope, task 6.

Authority (approved exclusion, already signed off): quota-carrying sales is
excluded from GTM Systems unless RevOps, Sales Ops or GTM-systems work is
primary. Phase 1's independent holdout measured GTM Systems precision at
8.7% (2 of 23): the lexicon's own selling-verb signals ("quota", "sales
cycle", "prospecting", "closing deals" ...) let a plain Account Executive or
SDR posting reach the deterministic ``gtm_revenue`` dominance bar with no
RevOps/Sales Ops/GTM-systems evidence at all (`classification.py:154-156`).

Descriptions below are deliberately >=120 chars (``classify_posting``'s own
"too short" floor) so the real deterministic path -- not the length gate --
is what's under test.
"""
from __future__ import annotations

from tgtc_core.domain.classification import classify_posting
from tgtc_core.domain.facts import RULE_VERSION


def _classify(title: str, description: str):
    return classify_posting(title=title, description=description, inference=None)


# --- selling-only postings must NOT land in GTM Systems ----------------------

def test_account_executive_is_not_gtm_systems():
    result = _classify(
        "Account Executive",
        "You will own a full sales quota, run point on the entire sales cycle from "
        "qualification to close, and prospect into new accounts through outbound cold "
        "calls to build pipeline and hit your numbers every quarter.",
    )
    assert "gtm_revenue" not in result.compatible_functions
    assert result.excluded and result.exclusion_reason == "role:quota_carrying_sales"


def test_sdr_is_not_gtm_systems():
    result = _classify(
        "Sales Development Representative",
        "Book qualified meetings for account executives against a monthly quota by "
        "running cold outreach and cold calls, qualifying leads and prospecting into new "
        "accounts every single day. You will support the sales cycle from first touch to "
        "demos to prospects, building a healthy pipeline of net-new opportunities.",
    )
    assert "gtm_revenue" not in result.compatible_functions
    assert result.excluded and result.exclusion_reason == "role:quota_carrying_sales"


# --- RevOps / Sales Ops postings must STILL land in GTM Systems --------------

def test_revenue_operations_manager_stays_gtm_systems():
    result = _classify(
        "Revenue Operations Manager",
        "You will own the revenue operations function for our go-to-market team: "
        "administer Salesforce administration and CRM hygiene, build pipeline reporting "
        "and forecasting dashboards for the leadership team, and manage lead routing and "
        "territory assignment across the sales organization.",
    )
    assert result.compatible_functions == ["gtm_revenue"]
    assert result.campaign_keys == ["gtm_systems"]
    assert not result.excluded


def test_sales_operations_analyst_stays_gtm_systems():
    result = _classify(
        "Sales Operations Analyst",
        "This sales operations role owns CRM hygiene across our Salesforce instance, "
        "builds pipeline reporting and forecasting analytics for revenue leadership, and "
        "runs lead routing and enrichment for the go-to-market team. You are not "
        "quota-carrying and will not own individual accounts or deals.",
    )
    assert result.compatible_functions == ["gtm_revenue"]
    assert result.campaign_keys == ["gtm_systems"]
    assert not result.excluded


# --- fix round 1, I3 (IMPORTANT, independent review): the exclusion's rule_version
# was buried inside an ad-hoc result.facts["role_exclusion"] dict rather than a
# discoverable, queryable top-level field -- an auditor querying exclusions by
# rule version would not see it. ClassificationResult now carries its own
# top-level rule_version, set whenever an exclusion changed this batch fires,
# and serialized by to_dict() the same way every other top-level field is.

def test_quota_carrying_sales_exclusion_carries_a_top_level_rule_version():
    result = _classify(
        "Account Executive",
        "You will own a full sales quota, run point on the entire sales cycle from "
        "qualification to close, and prospect into new accounts through outbound cold "
        "calls to build pipeline and hit your numbers every quarter.",
    )
    assert result.excluded and result.rule_version == RULE_VERSION
    assert result.to_dict()["rule_version"] == RULE_VERSION


# --- mixed evidence: the LARGER share decides, not mere presence -------------

def test_ops_primary_with_some_selling_language_still_stays():
    """A posting that mentions quota/compensation in passing, but whose dominant,
    multi-signal evidence is running revenue operations, is not swept up by the
    quota-carrying-sales exclusion (ops evidence, not selling evidence, is primary)."""
    result = _classify(
        "Sales Operations Manager",
        "Own the revenue operations function end to end: administer Salesforce CRM "
        "automation and hygiene, build territory and quota models for the sales "
        "organization, run lead routing and enrichment, and automate pipeline reporting "
        "and forecasting analytics for the executive team.",
    )
    assert result.compatible_functions == ["gtm_revenue"]
    assert not result.excluded
