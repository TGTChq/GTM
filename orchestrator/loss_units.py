"""Describe recorded loss counters without inventing population intersections.

Missing counts are unknown. Equal units do not prove disjoint populations, and a
missing contact does not prove a completed search. This module observes counters;
it never makes approval, suppression, retry, budget or run-stop decisions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

#: unit -> what one increment counts.
POSTING = "posting"
OPPORTUNITY = "company_x_function_opportunity"
DELIVERY_ROW = "delivery_row"
DISPOSITION = "lead_disposition"

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
    "hiring_manager_not_found": (OPPORTUNITY, "contact_discovery", UNATTRIBUTED),
    "unverified": (DISPOSITION, "email_validation", UNATTRIBUTED),
    "email_unverified": (DISPOSITION, "email_validation", UNATTRIBUTED),
    "needs_check": (DISPOSITION, "contact_gate", UNATTRIBUTED),
    "reroute": (DISPOSITION, "contact_gate", UNATTRIBUTED),
    "no_contact": (DELIVERY_ROW, "delivery", UNATTRIBUTED),
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

def _int(value: Any) -> Optional[int]:
    """Accept nonnegative integer counters; absence and malformed data stay unknown."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        try:
            return int(value.strip())
        except ValueError:
            pass
    return None


def _sum(values) -> Optional[int]:
    values = list(values)
    return sum(values) if all(v is not None for v in values) else None


def _population_count(labels: Mapping[str, Optional[int]]) -> Optional[int]:
    """Only a single counter or agreeing aliases establish one population count.

    Multiple non-alias labels may overlap, even when their units match. Writer
    arithmetic is handled separately using the writer's own skip partition.
    """
    groups = {label: name for name, aliases in ALIAS_GROUPS for label in aliases}
    populations: Dict[str, List[Optional[int]]] = {}
    for label, count in labels.items():
        populations.setdefault(groups.get(label, label), []).append(count)
    if len(populations) != 1:
        return None
    values = next(iter(populations.values()))
    return values[0] if None not in values and len(set(values)) == 1 else None


def classify(reasons: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Group one run's loss reasons by unit, stage and category.

    Retain individual observations. Totals require both common units and evidence
    that labels describe one population; equal units alone are insufficient.
    """
    by_unit: Dict[str, Dict[str, Optional[int]]] = {}
    by_category: Dict[str, Dict[str, Optional[int]]] = {}
    unknown: Dict[str, Optional[int]] = {}
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
        "by_unit": {unit: {"labels": labels, "sum_within_unit": _population_count(labels)}
                    for unit, labels in sorted(by_unit.items())},
        "by_category": {c: {"labels": l,
                             "units": {label: REASON_UNITS[label][0] for label in l},
                             "sum_within_category": (
                                 _population_count(l)
                                 if len({REASON_UNITS[label][0] for label in l}) == 1
                                 else None)}
                        for c, l in sorted(by_category.items())},
        "unclassified_labels": unknown,
        "note": ("null means a population total is not established; common units "
                 "do not establish disjointness, and aliases count once"),
    }


def overlaps(reasons: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Expose known aliases; do not subtract unlinked cross-stage populations."""
    present = {str(k): _int(v) for k, v in (reasons or {}).items()}
    aliases = []
    for name, labels in ALIAS_GROUPS:
        found = {label: present[label] for label in labels if label in present}
        if len(found) > 1:
            aliases.append({
                "group": name, "labels": found,
                "counts_agree": (len(set(found.values())) == 1
                                 if None not in found.values() else None),
                "double_counted_if_summed": (
                    sum(found.values()) - next(iter(found.values()))
                    if None not in found.values() and len(set(found.values())) == 1
                    else None),
            })
    contained = []
    for outer, inner in [("unverified", "no_contact")]:
        if outer in present and inner in present:
            contained.append({
                "outer": outer, "outer_count": present[outer],
                "inner": inner, "inner_count": present[inner],
                "established": False,
                "why": "different stages and units; linked identities and email outcomes required",
                "outer_excluding_inner": None,
                "consistent": None,
            })
    return {"alias_groups": aliases, "containments": contained}


def delivery_reconciles(submitted: Any, created: Any,
                        skips: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Does the writer's own arithmetic close?

    ``submitted = created + every skip`` is a numeric partition of the writer's
    receipt. It does not establish approval status or the correctness of a skip.
    """
    total_skips = _sum(_int(v) for v in skips.values()) if skips is not None else None
    submitted_n, created_n = _int(submitted), _int(created)
    remainder = (submitted_n - created_n - total_skips
                 if None not in (submitted_n, created_n, total_skips) else None)
    return {"submitted": submitted_n, "created": created_n, "skips": total_skips,
            "unexplained": remainder,
            "reconciles": remainder == 0 if remainder is not None else None,
            "unit": DELIVERY_ROW}


def opportunity_reach(*, qualified_postings: Any, opportunities_formed: Any,
                      opportunities_with_outcome: Any,
                      stop_reason: str = "") -> Dict[str, Any]:
    """How much of the qualified inventory the run actually reached.

    The two opportunity counts must describe distinct opportunities in the same
    cohort. A missing outcome alone says neither whether work was attempted nor
    why it lacks an outcome. The run's stop reason does not supply that linkage.
    """
    qualified = _int(qualified_postings)
    formed = _int(opportunities_formed)
    with_outcome = _int(opportunities_with_outcome)
    missing = (formed - with_outcome
               if None not in (formed, with_outcome) and with_outcome <= formed
               else None)
    return {
        "qualified_postings": qualified,
        "opportunities_formed": formed,
        "opportunities_with_outcome": with_outcome,
        "opportunities_without_outcome": missing,
        "category_for_those_without_outcome": UNATTRIBUTED if missing != 0 else None,
        "stop_reason": str(stop_reason or ""),
        "qualified_postings_per_formed_opportunity": (
            round(qualified / formed, 2) if qualified is not None and formed else None),
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
    skips = record.get("delivery_skip_breakdown")
    return {
        "run_id": record.get("run_id", ""),
        "stop_reason": record.get("stop_reason", ""),
        "state": record.get("state", ""),
        "classified": classify(reasons),
        "overlaps": overlaps(reasons),
        "delivery": delivery_reconciles(metrics.get("airtable_candidates"),
                                        metrics.get("sent_to_airtable"), skips),
        **({"contact_discovery": contact_discovery(
            hm, record.get("loss_reasons"), record.get("delivery_skip_breakdown"),
            (hm or {}).get("stats"))}
           if hm else {"contact_discovery": {
               "unavailable": "no hiring-manager summary for this run; whether a "
                              "search was issued cannot be established"}}),
        "reach": opportunity_reach(
            qualified_postings=metrics.get("role_qualified_postings"),
            opportunities_formed=metrics.get("qualified_opportunities"),
            # Airtable candidates are delivery rows, not distinct opportunities.
            # No linked outcome-opportunity count is persisted in this ledger.
            opportunities_with_outcome=None,
            stop_reason=record.get("stop_reason", "")),
    }


def contact_discovery(hm: Optional[Mapping[str, Any]],
                      reasons: Optional[Mapping[str, Any]],
                      skips: Optional[Mapping[str, Any]],
                      stats: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Report marginal observations without treating them as a causal partition.

    hm_observability.hm_summary counts eligible lead rows and their diagnostic
    booleans. hiring_manager stamps the search flag before calling the client and
    can replay it from a bucket checkpoint. It is not a count of physical requests
    or proof of search completion during this run. The separate marginal counts
    do not identify intersections, unique opportunities or cache-only results.
    """
    hm = dict(hm or {})
    reasons = dict(reasons or {})
    skips = dict(skips or {})
    eligible = _int(hm.get("eligible_company_buckets"))
    searched = _int(hm.get("hm_searches"))
    found = _int(hm.get("hm_found"))
    observed_not_found = _int(hm.get("hm_not_found"))
    reported_not_found = _int(reasons.get("hiring_manager_not_found"))
    disagreement = (reported_not_found - observed_not_found
                    if None not in (reported_not_found, observed_not_found) else None)
    observed_partition = (found + observed_not_found == eligible
                          if None not in (found, observed_not_found, eligible) else None)
    return {
        "unit": "lead_observation",
        "reported_unit": hm.get("hm_not_found_unit"),
        NEVER_SEARCHED: None,
        SEARCHED_NO_RESULT: None,
        "searched_and_found": None,
        FOUND_BUT_WITHHELD: {
            "count": _int(skips.get("send_safe_withheld")),
            "unit": DELIVERY_ROW, "included_in_contact_partition": False},
        "eligible_buckets": eligible,
        "searches_issued": None,
        "observed": {"eligible_rows": eligible, "rows_marked_search_called": searched,
                     "rows_with_manager_name": found,
                     "rows_without_manager_name": observed_not_found,
                     "name_partition_closes": observed_partition},
        "partition_closes": None,
        "counter_disagreement": {
            "loss_reasons.hiring_manager_not_found": reported_not_found,
            "hm_summary.hm_not_found": observed_not_found,
            "difference": disagreement,
            "agree": disagreement == 0 if disagreement is not None else None,
            "consequence": ("neither artifact links the populations here; a difference "
                            "requires scope and identity reconciliation, not choosing a counter"),
        },
        # A NEGATIVE THAT COST NOTHING is separable, and only from the run's own
        # stats. hiring_manager checks a zero-title negative cache BEFORE the client
        # call and, on a hit, sets no search flag at all -- so a cached negative is
        # neither a search nor a silent absence. It is the one part of "why is there
        # no contact" the artifacts can answer, and only when stats are supplied.
        "provider_interaction": _provider_interaction(stats),
        "not_established": [
            "distinct opportunity membership and overlap of these observations",
            "physical requests issued and completed during this run",
            "whether an absent contact came from an incomplete search or a rejection",
        ],
    }


def _provider_interaction(stats: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Counters the run stamps itself about search attempts and cached negatives.

    ``row2_people_search_calls_total`` is incremented immediately before the client
    call, inside the try -- so it counts attempts ENTERED, including any that raised.
    It is not a count of completed searches, and nothing here treats it as one.
    """
    if stats is None:
        return {"available": False,
                "why": "the run's hiring-manager stats were not supplied"}
    return {
        "available": True,
        "search_attempts_entered": _int(stats.get("row2_people_search_calls_total")),
        "negative_cache_hits": _int(stats.get("people_search_negative_cache_hit")),
        "incomplete_searches": _int(stats.get("people_search_incomplete")),
        "companies_with_a_search_attempt": _int(
            stats.get("row2_companies_with_people_search_call")),
        "caution": ("attempts are counted before the call returns; a cached negative "
                    "sets no search flag, so it is neither an attempt nor a silent "
                    "absence"),
    }
