"""Loss reasons carry UNITS, and several of them count the same population twice.

A run reports a flat ``loss_reasons`` map. Read as a list it invites exactly the
wrong arithmetic -- the 2026-09-07 run's map sums to well over its own opportunity
count, because it mixes four units and repeats two populations under different
labels:

* ``REJECT_*`` are **postings**, rejected before any opportunity is formed.
* ``hiring_manager_not_found`` is a **company x function** opportunity.
* ``no_contact`` / ``send_safe_withheld`` are **delivery rows** -- opportunities that
  reached the writer.
* ``unverified`` / ``needs_check`` are **dispositions** over the same opportunities.

And the repeats: ``email_unverified`` and ``unverified`` are one population under two
names, as are ``not_icp`` and ``rejected``. ``unverified`` also CONTAINS
``no_contact`` -- an opportunity with no email at all is not verified either -- so
adding the two counts the same opportunities twice and then calls the sum a loss.

WHAT THIS MODULE REFUSES TO DO. It does not infer a cause from a counter. "Nothing
was found" and "we never looked" produce the same absence in a funnel, and only
stage-entry evidence separates them, so a population the artifacts cannot classify is
returned as ``unattributed`` rather than assigned to the provider or to the budget.
The five categories below exist precisely so that "Apollo found nobody" cannot absorb
work that was never attempted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

#: unit -> what one increment counts.
POSTING = "posting"
OPPORTUNITY = "company_x_function_opportunity"
DELIVERY_ROW = "delivery_row"
DISPOSITION = "opportunity_disposition"

#: The five outcomes the user of this data has to be able to tell apart. Each is a
#: statement about what HAPPENED, not about who is to blame for it.
SEARCHED_NO_RESULT = "searched_and_found_nothing"
NEVER_SEARCHED = "never_searched"
INTERRUPTED = "interrupted_before_completion"
CACHE_NEGATIVE = "known_negative_from_cache"
INTERNAL_FILTER = "rejected_by_our_own_rules"
FOUND_BUT_WITHHELD = "contact_found_then_withheld"
UNATTRIBUTED = "unattributed"

#: reason label -> (unit, stage, category). Anything absent is reported as unknown
#: rather than guessed into a category.
REASON_UNITS: Dict[str, tuple] = {
    "REJECT_QUALITY_GUARD_OTHER": (POSTING, "qualification", INTERNAL_FILTER),
    "REJECT_ROLE_MISMATCH": (POSTING, "qualification", INTERNAL_FILTER),
    "REJECT_ROLE_NOT_GLOBALLY_COVERABLE": (POSTING, "qualification", INTERNAL_FILTER),
    "REJECT_SECURITY_CLEARANCE_REQUIRED": (POSTING, "qualification", INTERNAL_FILTER),
    "REJECT_EXCLUDED_SENIORITY": (POSTING, "qualification", INTERNAL_FILTER),
    "REJECT_INTERNSHIP": (POSTING, "qualification", INTERNAL_FILTER),
    "REJECT_UNRESOLVABLE_POSTING": (POSTING, "qualification", INTERNAL_FILTER),
    "not_icp": (OPPORTUNITY, "account_gate", INTERNAL_FILTER),
    "rejected": (OPPORTUNITY, "account_gate", INTERNAL_FILTER),
    "company_unresolved": (OPPORTUNITY, "identity", INTERNAL_FILTER),
    "hiring_manager_not_found": (OPPORTUNITY, "contact_discovery", SEARCHED_NO_RESULT),
    "unverified": (DISPOSITION, "email_validation", UNATTRIBUTED),
    "email_unverified": (DISPOSITION, "email_validation", UNATTRIBUTED),
    "needs_check": (DISPOSITION, "contact_gate", UNATTRIBUTED),
    "reroute": (DISPOSITION, "contact_gate", UNATTRIBUTED),
    "no_contact": (DELIVERY_ROW, "delivery", SEARCHED_NO_RESULT),
    "send_safe_withheld": (DELIVERY_ROW, "delivery", FOUND_BUT_WITHHELD),
    "company_function_suppressed": (DELIVERY_ROW, "delivery", INTERNAL_FILTER),
    "account_suppressed": (DELIVERY_ROW, "delivery", INTERNAL_FILTER),
    "skipped_existing": (DELIVERY_ROW, "delivery", INTERNAL_FILTER),
    "updated_existing": (DELIVERY_ROW, "delivery", INTERNAL_FILTER),
    "delivery_unreconciled": (DELIVERY_ROW, "delivery", UNATTRIBUTED),
}

#: Labels the pipeline emits from ONE population. Listing them is not a guess: each
#: group is a single decision point that writes more than one key.
ALIAS_GROUPS: List[tuple] = [
    ("email_validation_failures", ("unverified", "email_unverified")),
    ("account_gate_rejections", ("not_icp", "rejected")),
]

#: Containment, not aliasing: the left label's population INCLUDES the right one.
#: An opportunity with no email at all is also an unverified one.
CONTAINMENTS: List[tuple] = [
    ("unverified", "no_contact",
     "an opportunity with no contact has no verified email either"),
]


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def classify(reasons: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Group one run's loss reasons by unit, stage and category.

    Sums are reported PER UNIT and never across units, because a posting, an
    opportunity and a delivery row are not addable.
    """
    by_unit: Dict[str, Dict[str, int]] = {}
    by_category: Dict[str, Dict[str, int]] = {}
    unknown: Dict[str, int] = {}
    for label, raw in (reasons or {}).items():
        count = _int(raw)
        entry = REASON_UNITS.get(str(label))
        if entry is None:
            unknown[str(label)] = count
            continue
        unit, _stage, category = entry
        by_unit.setdefault(unit, {})[str(label)] = count
        by_category.setdefault(category, {})[str(label)] = count
    return {
        "by_unit": {unit: {"labels": labels, "sum_within_unit": sum(labels.values())}
                    for unit, labels in sorted(by_unit.items())},
        "by_category": {c: {"labels": l, "sum_within_category": sum(l.values())}
                        for c, l in sorted(by_category.items())},
        "unclassified_labels": unknown,
        "note": ("counts are summed only WITHIN a unit; across units they are "
                 "different populations and adding them is meaningless"),
    }


def overlaps(reasons: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Which labels repeat one population, and which contains which."""
    present = {str(k): _int(v) for k, v in (reasons or {}).items()}
    aliases = []
    for name, labels in ALIAS_GROUPS:
        found = {label: present[label] for label in labels if label in present}
        if len(found) > 1:
            aliases.append({
                "group": name, "labels": found,
                "counts_agree": len(set(found.values())) == 1,
                "double_counted_if_summed": sum(found.values()) - max(found.values()),
            })
    contained = []
    for outer, inner, why in CONTAINMENTS:
        if outer in present and inner in present:
            contained.append({
                "outer": outer, "outer_count": present[outer],
                "inner": inner, "inner_count": present[inner], "why": why,
                "outer_excluding_inner": present[outer] - present[inner],
                "consistent": present[outer] >= present[inner],
            })
    return {"alias_groups": aliases, "containments": contained}


def delivery_reconciles(submitted: Any, created: Any,
                        skips: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Does the writer's own arithmetic close?

    ``submitted = created + every skip``. When it does, the delivery-row unit is
    fully explained and nothing there needs attributing to a provider. When it does
    not, the remainder is named and left unattributed.
    """
    total_skips = sum(_int(v) for v in (skips or {}).values())
    submitted_n, created_n = _int(submitted), _int(created)
    remainder = submitted_n - created_n - total_skips
    return {"submitted": submitted_n, "created": created_n, "skips": total_skips,
            "unexplained": remainder, "reconciles": remainder == 0,
            "unit": DELIVERY_ROW}


def opportunity_reach(*, qualified_postings: Any, opportunities_formed: Any,
                      opportunities_with_outcome: Any,
                      stop_reason: str = "") -> Dict[str, Any]:
    """How much of the qualified inventory the run actually reached.

    The distinction the funnel cannot make on its own. Postings that qualified but
    whose opportunity was never formed, and opportunities formed but never given an
    outcome, are WORK NOT ATTEMPTED -- they are not evidence about contact coverage,
    and attributing them to the provider or to a spent budget would be inventing a
    cause for an absence.
    """
    qualified = _int(qualified_postings)
    formed = _int(opportunities_formed)
    with_outcome = _int(opportunities_with_outcome)
    interrupted = bool(stop_reason) and "target_met" not in str(stop_reason)
    return {
        "qualified_postings": qualified,
        "opportunities_formed": formed,
        "opportunities_with_outcome": with_outcome,
        "opportunities_without_outcome": max(0, formed - with_outcome),
        "category_for_those_without_outcome": (INTERRUPTED if interrupted
                                               else NEVER_SEARCHED),
        "stop_reason": str(stop_reason or ""),
        "qualified_postings_per_formed_opportunity": (
            round(qualified / formed, 2) if formed else None),
        "caution": ("qualified POSTINGS collapse into far fewer opportunities, so "
                    "the ratio is a collapse factor and not a loss"),
    }


def decompose(record: Optional[Mapping[str, Any]],
              hm: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The whole decomposition for one reporting-ledger record.

    ``hm`` is the observability layer's hiring-manager summary when it is available.
    Without it the contact stage cannot say whether a search was issued, so that
    section is omitted rather than guessed at from subtractions.
    """
    record = dict(record or {})
    metrics = dict(record.get("metrics") or {})
    reasons = dict(record.get("loss_reasons") or {})
    skips = dict(record.get("delivery_skip_breakdown") or {})
    return {
        "run_id": record.get("run_id", ""),
        "stop_reason": record.get("stop_reason", ""),
        "state": record.get("state", ""),
        "classified": classify(reasons),
        "overlaps": overlaps(reasons),
        "delivery": delivery_reconciles(metrics.get("airtable_candidates"),
                                        metrics.get("sent_to_airtable"), skips),
        **({"contact_discovery": contact_discovery(
            hm, record.get("loss_reasons"), record.get("delivery_skip_breakdown"))}
           if hm else {"contact_discovery": {
               "unavailable": "no hiring-manager summary for this run; whether a "
                              "search was issued cannot be established"}}),
        "reach": opportunity_reach(
            qualified_postings=metrics.get("role_qualified_postings"),
            opportunities_formed=metrics.get("qualified_opportunities"),
            opportunities_with_outcome=metrics.get("airtable_candidates"),
            stop_reason=record.get("stop_reason", "")),
    }


def contact_discovery(hm: Optional[Mapping[str, Any]],
                      reasons: Optional[Mapping[str, Any]],
                      skips: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Split contact-stage absence into the things it can actually be.

    A funnel shows the same hole whether nobody was found, nobody was looked for, or
    the run stopped first. ``hm_searches`` is stamped per company x bucket when a
    people-search call is genuinely issued, so it is the one field that separates
    "searched" from "never searched" -- everything here is built on it rather than
    on a subtraction.

    THE CROSS-CHECK. ``loss_reasons["hiring_manager_not_found"]`` and the
    observability layer's ``hm_not_found`` are computed from the same rule (a bucket
    with no ``hiring_manager_name``) over sets that are meant to be the same. On the
    2026-09-07 run they disagree, 169 against 147. One of them is wrong and the
    artifacts do not say which, so the disagreement is REPORTED rather than resolved
    by preferring one: a consumer that picks the larger silently books 22 buckets as
    "nobody found" that the other counter says had somebody.
    """
    hm = dict(hm or {})
    reasons = dict(reasons or {})
    skips = dict(skips or {})
    eligible = _int(hm.get("eligible_company_buckets"))
    searched = _int(hm.get("hm_searches"))
    found = _int(hm.get("hm_found"))
    observed_not_found = _int(hm.get("hm_not_found"))
    reported_not_found = _int(reasons.get("hiring_manager_not_found"))
    disagreement = reported_not_found - observed_not_found
    return {
        "unit": hm.get("hm_not_found_unit") or OPPORTUNITY,
        NEVER_SEARCHED: max(0, eligible - searched),
        SEARCHED_NO_RESULT: observed_not_found,
        "searched_and_found": found,
        FOUND_BUT_WITHHELD: _int(skips.get("send_safe_withheld")),
        "eligible_buckets": eligible,
        "searches_issued": searched,
        "partition_closes": (found + observed_not_found == eligible),
        "counter_disagreement": {
            "loss_reasons.hiring_manager_not_found": reported_not_found,
            "hm_summary.hm_not_found": observed_not_found,
            "difference": disagreement,
            "agree": disagreement == 0,
            "consequence": ("none" if disagreement == 0 else
                            f"{abs(disagreement)} buckets are booked as 'nobody found' "
                            "by one counter and not by the other; neither artifact "
                            "says which is right, so this stays unresolved"),
        },
        "not_established": [
            "whether a search that returned nobody had a searchable domain",
            "whether a negative came from a cache rather than a call",
        ],
    }
