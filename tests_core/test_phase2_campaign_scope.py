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

from tgtc_core.domain.classification import classify_posting, score_functions
from tgtc_core.domain.facts import RULE_VERSION
from tgtc_core.domain.inference import InferenceResponse, ResponsibilityItem


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


# ---------------------------------------------------------------------------
# Final whole-branch review, C1 (CRITICAL): the scope exclusion existed ONLY on
# the deterministic path. ``_apply_semantic`` assigned ``gtm_revenue`` straight
# from the model answer with no scope check at all -- and tasks 1-2 measurably
# move rows INTO that path (ledger row 1: 192 of 397 changed rows go
# rejected -> semantic_pending). Inference is live in production
# (``runner.py:113-116``), so a thin "Account Executive, carry a quota, close
# new business" posting reached the model, came back ``gtm_revenue``, and was
# approved into GTM Systems -- the exact defect the deterministic fix above
# closes. Descriptions here are deliberately BELOW the deterministic dominance
# bar (score < MIN_DOMINANT_SCORE) so the semantic path is what decides.
# ---------------------------------------------------------------------------

class _FixedAnswer:
    """A semantic port that returns one fixed, grounded answer (no network, no model)."""

    model_version = "test-model/1"

    def __init__(self, functions, excerpt: str):
        self._functions, self._excerpt = list(functions), excerpt

    def classify(self, request):
        return InferenceResponse(
            available=True, compatible_functions=list(self._functions),
            responsibilities=[ResponsibilityItem("owning the sales quota", self._excerpt)],
            confidence=0.9, model_version=self.model_version,
        )


_THIN_SELLING = (
    "We are hiring an Account Executive who will carry a quota and close new business "
    "for our growing team. You will report to the head of sales and work from our New "
    "York office three days a week. Full-time position with benefits and a comp plan."
)
_THIN_OPS = (
    "You will own our Salesforce administration and CRM hygiene for the go-to-market "
    "team, keeping records clean and automations working. You will report to the head of "
    "operations and work from our New York office three days a week. Full-time position."
)


def test_thin_selling_posting_is_not_gtm_systems_on_the_semantic_path():
    """The whole point of C1: the model says gtm_revenue, the posting's own
    evidence is selling, and the assignment must be refused exactly as the
    deterministic path refuses it."""
    result = classify_posting(title="Account Executive", description=_THIN_SELLING,
                              inference=_FixedAnswer(["gtm_revenue"], _THIN_SELLING[:80]))
    assert result.method == "semantic"
    assert "gtm_revenue" not in result.compatible_functions
    assert result.campaign_keys == []
    assert result.excluded and result.exclusion_reason == "role:quota_carrying_sales"
    assert result.rule_version == RULE_VERSION
    assert result.facts["role_exclusion"]["code"] == "quota_carrying_sales"


def test_thin_ops_posting_still_reaches_gtm_systems_on_the_semantic_path():
    """The exclusion must not swallow the campaign: a posting whose own
    evidence is operations, not selling, still routes to GTM Systems when the
    model says so."""
    result = classify_posting(title="Revenue Operations Associate", description=_THIN_OPS,
                              inference=_FixedAnswer(["gtm_revenue"], _THIN_OPS[:80]))
    assert result.method == "semantic"
    assert result.compatible_functions == ["gtm_revenue"]
    assert result.campaign_keys == ["gtm_systems"]
    assert not result.excluded


def test_a_second_semantic_function_survives_the_gtm_scope_exclusion():
    """The scope rule is about GTM Systems, and a job counts once under its
    primary: when the model returns a second, unrelated function alongside
    gtm_revenue, only gtm_revenue is dropped -- the posting is not thrown away."""
    result = classify_posting(title="Account Executive", description=_THIN_SELLING,
                              inference=_FixedAnswer(["gtm_revenue", "marketing"], _THIN_SELLING[:80]))
    assert result.compatible_functions == ["marketing"]
    assert "gtm_systems" not in result.campaign_keys
    assert not result.excluded
    assert result.rule_version == RULE_VERSION


# ---------------------------------------------------------------------------
# Scoped re-review, IMPORTANT 2: C1 left a residual escape. The shared scope
# predicate is EVIDENCE-CONDITIONAL -- `hits.get("gtm_revenue", ())` on an empty
# hit list gives selling=0, so it returns None and nothing is excluded. Only the
# DESCRIPTION is scored (classify_posting passes `desc` alone), so an "Account
# Executive" title contributes nothing either. The semantic path exists
# precisely for postings the lexicon cannot read, which makes that the one
# population where the scope check can never fire: a quota-carrying AE whose
# wording matches no gtm_revenue signal was model-assigned gtm_revenue and
# routed to GTM Systems with excluded=False.
#
# A model assignment of gtm_revenue with ZERO deterministic gtm evidence is an
# unqualified guess, and "unknown is never approved" is a fixed rule. It is
# routed to review (no function, reopenable), NOT rejected -- and the lexicon is
# deliberately NOT widened to paper over it.
# ---------------------------------------------------------------------------

_THIN_NO_GTM_EVIDENCE = (
    "You will own a book of new logos in your patch and be measured on the number you bring in "
    "each quarter. You will negotiate pricing with economic buyers, run discovery conversations, "
    "and partner with our solutions team to win competitive evaluations against incumbents."
)


def test_the_escape_posting_really_has_no_deterministic_gtm_evidence():
    """Precondition, so the test below cannot silently stop testing what it says:
    this wording matches no gtm_revenue lexicon signal at all."""
    scores, hits = score_functions(_THIN_NO_GTM_EVIDENCE)
    assert scores.get("gtm_revenue", 0) == 0 and not hits.get("gtm_revenue")


def test_a_model_gtm_assignment_with_no_deterministic_evidence_is_not_gtm_systems():
    result = classify_posting(title="Account Executive", description=_THIN_NO_GTM_EVIDENCE,
                              inference=_FixedAnswer(["gtm_revenue"], _THIN_NO_GTM_EVIDENCE[:80]))
    assert result.method == "semantic"
    assert "gtm_revenue" not in result.compatible_functions
    assert result.campaign_keys == []
    assert not result.excluded, "an unqualified guess is review, never a reject"
    assert any(n.startswith("insufficient_evidence:gtm_revenue") for n in result.notes), result.notes


def test_a_second_function_survives_when_only_gtm_revenue_is_unsupported():
    """The rule is scoped to GTM Systems: it removes the unsupported assignment,
    it does not throw the posting away."""
    result = classify_posting(title="Account Executive", description=_THIN_NO_GTM_EVIDENCE,
                              inference=_FixedAnswer(["gtm_revenue", "marketing"], _THIN_NO_GTM_EVIDENCE[:80]))
    assert result.compatible_functions == ["marketing"]
    assert "gtm_systems" not in result.campaign_keys
    assert not result.excluded
