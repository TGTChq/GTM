"""Which candidate a paid call is spent on. One total order, one place.

Phase 4 audit (2026-09-20). ``OpportunityService._rank`` keyed on the index of
the first matching buyer title and nothing else, which left three problems:

* **Not a total order.** ``sorted`` is stable, so every tie fell through to the
  order Apollo returned the people in -- provider-side, undocumented, and not
  reproducible. "Only ever spend a credit on the best-ranked candidate" is not a
  property you can hold if "best" is decided by the response order.
* **A founder could outrank the functional owner.** The founder demotion lived
  in the branch for candidates matching NO buyer title, which
  ``pre_enrichment_check`` has already rejected -- unreachable from the live
  path. "Founder & CTO" matches "CTO", inherits the CTO's index, and is bought
  ahead of the actual Director of Engineering. Policy makes the founder the
  FALLBACK buyer for a company too small to have the real one
  (``founder_fallback_max_employees``), not the preferred one.
* **Employer evidence was not a signal.** A candidate whose Apollo organization
  domain IS the employer's and one matched only by a compatible company name
  ranked identically. ``email:domain_not_employer`` was the largest
  post-enrichment loss Stage 2a measured (9 of 101 paid calls).

What is deliberately NOT in the key, because the evidence for it does not
exist before a paid call: Apollo's free people-search returns neither
``departments`` nor ``seniority``. Both are empty on all 512 candidates Stage 2a
saved (`phase5_apollo_2a/state/search.jsonl`, 0 credits). The title is the only
function evidence a free search carries, which is also why the title normaliser
carries the whole weight of this stage.

The persona tiers are not a separate term either: ``buyer_titles()`` is already
ordered functional owner, executive leader, Talent/People owner, founders last,
and a test in ``test_phase4_personas.py`` pins that. Adding a persona term would
be a second, drift-prone statement of the same policy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Set, Tuple

from .gates import _org_domain, organization_matches, person_organization
from .identity import company_names_compatible, safe_employer_domain
from .title_norm import matched_target_index
from ..policy.campaigns import is_founder_tier


def candidate_rank_key(person: Dict[str, Any], *, buyer_titles: Sequence[str],
                       employer_domains: Set[str], employer_name: str) -> Tuple[Any, ...]:
    """The total order. Smaller sorts first.

    Terms, in the order they decide:

    0. ``founder_tier`` -- a founder is the fallback buyer, never the preferred
       one, even when their title also spells a real buyer title.
    1. ``title_index`` -- the buyer list's own preference order, which is the
       policy statement of who should be contacted first. Evidence never
       re-orders the functions; it only breaks ties within one.
    2. ``organization domain is exactly the employer's`` -- the strongest
       employer evidence a search result carries.
    3. ``organization name is compatible with the employer's`` -- the next
       strongest.
    4. ``person_ref`` -- the deterministic tiebreak that makes this a TOTAL
       order. It is the candidate's own stable identity, so the same people in
       any provider order produce the same ranking.
    """
    title = str(person.get("title") or "")
    org = person_organization(person)
    org_domain = _org_domain(org) or safe_employer_domain(person.get("organization_domain"))
    org_name = str(org.get("name") or person.get("organization_name") or "")
    return (
        1 if is_founder_tier(title) else 0,
        matched_target_index(title, buyer_titles),
        0 if (org_domain and org_domain in employer_domains) else 1,
        0 if (org_name and company_names_compatible(employer_name, org_name)) else 1,
        str(person.get("_ref") or person.get("person_key") or person.get("id") or ""),
    )


def rank_candidates(candidates: List[Dict[str, Any]], *, buyer_titles: Sequence[str],
                    employer_domains: Set[str], employer_name: str) -> List[Dict[str, Any]]:
    return sorted(candidates, key=lambda p: candidate_rank_key(
        p, buyer_titles=buyer_titles, employer_domains=employer_domains, employer_name=employer_name))


def organization_evidence(person: Dict[str, Any], *, employer_domains: Set[str], employer_name: str) -> str:
    """Why this candidate is believed to be at this employer, for the artefact.

    Returned so a ranking decision can be read back from
    ``candidate_attempts.details`` instead of re-derived.
    """
    org = person_organization(person)
    org_domain = _org_domain(org) or safe_employer_domain(person.get("organization_domain"))
    org_name = str(org.get("name") or person.get("organization_name") or "")
    if org_domain and org_domain in employer_domains:
        return "employer_domain_exact"
    if organization_matches(name=org_name, domain=org_domain,
                            employer_name=employer_name, employer_domains=employer_domains):
        return "employer_name_compatible"
    return "none"
