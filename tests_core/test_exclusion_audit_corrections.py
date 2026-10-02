"""Two evidence-backed corrections from the uncertain-exclusion audit (2026-09-21).

Sampled from the first full production run's classifications:

* role:quota_carrying_sales, 14 postings -- all sales roles (Inside Sales
  Representative, Sales Executive, Senior Director - Strategic Accounts). The
  exhaustive scope routes quota-carrying sales to GTM Systems, but the flag had
  only reached the DETERMINISTIC path; the semantic path still excluded them.
* deliverability:professional_license, 95 postings -- 89 are "Life Insurance
  Agent" from ONE employer (a state insurance-producer licence on commission
  sales, correctly excluded), the rest clinical or construction (correct), and
  exactly two are false negatives: "Corporate Tax Manager" and "Family Office Tax
  Manager". A CPA is a professional credential for remotely executable finance
  work, not a physical or jurisdiction-bound duty.

The licence carve-out is deliberately narrow: only when the licence is the SOLE
exclusion and the title routes to Finance. Nothing else about the licence rule
changes, and the insurance agents stay out.
"""
from __future__ import annotations

from tgtc_core.domain.classification import classify_posting
from tgtc_core.domain.exhaustive_routing import FLAG_ENV
from tgtc_core.domain.inference import InferenceResponse, ResponsibilityItem

ON = {FLAG_ENV: "1"}
LICENCE_DESC = (
    "You will lead the corporate tax provision, federal and state filings, and tax planning for the "
    "group. An active CPA license is required for this position. You will partner with the "
    "controller and outside advisors on quarterly estimates and audits."
)


def test_a_tax_manager_whose_only_exclusion_is_a_cpa_licence_routes_to_finance():
    result = classify_posting(title="Corporate Tax Manager", description=LICENCE_DESC, inference=None, env=ON)
    assert not result.excluded, result.exclusion_reason
    assert result.compatible_functions == ["finance"]
    assert result.facts.get("exclusion_waived", {}).get("reason") == "deliverability:professional_license"


def test_the_licence_carve_out_is_off_with_the_flag_off():
    result = classify_posting(title="Corporate Tax Manager", description=LICENCE_DESC, inference=None, env={})
    assert result.excluded and result.exclusion_reason == "deliverability:professional_license"


def test_an_insurance_producer_licence_stays_excluded():
    desc = ("Sell life insurance products to families in your territory. An active state life insurance "
            "license is required. Commission-based compensation with uncapped earning potential for agents.")
    result = classify_posting(title="Life Insurance Agent", description=desc, inference=None, env=ON)
    assert result.excluded


def test_a_clinical_licence_stays_excluded():
    desc = ("Provide bedside patient care responsibilities on the medical-surgical unit. An active RN license "
            "is required. Charge nurse duties on the night shift across the unit.")
    result = classify_posting(title="Charge RN - Med Surg, Nights", description=desc, inference=None, env=ON)
    assert result.excluded


def test_a_licence_beside_another_exclusion_stays_excluded():
    desc = LICENCE_DESC + " This is a part-time position, twenty hours per week."
    result = classify_posting(title="Corporate Tax Manager", description=desc, inference=None, env=ON)
    assert result.excluded


# --- quota-carrying sales on the SEMANTIC path --------------------------------


class _SalesModel:
    model_version = "fake"

    def classify(self, request):
        excerpt = "carry a quota and close new business"
        return InferenceResponse(
            available=True, model_version="fake",
            compatible_functions=["gtm_revenue"], confidence=0.9,
            responsibilities=[ResponsibilityItem(phrase="closing new business", excerpt=excerpt)],
        )


SALES_DESC = (
    "Join our team as we grow. You will carry a quota and close new business with enterprise accounts, "
    "prospect into new logos, run discovery calls and manage the full sales cycle for our platform."
)


def test_quota_carrying_sales_on_the_semantic_path_routes_to_gtm_with_the_flag_on():
    result = classify_posting(title="Senior Director - Strategic Accounts", description=SALES_DESC,
                              inference=_SalesModel(), env=ON)
    assert not result.excluded, result.exclusion_reason
    assert result.compatible_functions == ["gtm_revenue"]


def test_quota_carrying_sales_on_the_semantic_path_is_unchanged_with_the_flag_off():
    result = classify_posting(title="Senior Director - Strategic Accounts", description=SALES_DESC,
                              inference=_SalesModel(), env={})
    assert "gtm_revenue" not in result.compatible_functions
