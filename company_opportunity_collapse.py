"""Collapse many postings from one employer into ONE downstream opportunity.

A single employer that posts eight roles must not become eight Apollo
enrichments and eight outbound leads. This module groups postings by *trusted*
company identity and elects one deterministic representative per employer; the
rest are retained as provenance and never reach paid person enrichment.

Identity is grouped by union-find over trusted anchors ONLY:

  ``slug:``  the LinkedIn organization slug (a provider-assigned identifier)
  ``dom:``   the defensible employer domain resolved by the canonical mapper
  ``ck:``    ``hiring_manager.company_key_for_job`` -- the grouping key the
             enrichment stage itself uses

Two companies are never merged because their names merely look similar: the
only name-derived anchor is ``ck:``, and it is included precisely so this
collapse can never be finer-grained than the downstream grouping it bounds.
Without it, two postings sharing a domain but differing in slug would elect two
representatives that enrichment would then re-merge, breaking the invariant.

A posting with neither a trusted slug nor a trusted domain has genuinely
unresolved company identity. Those FAIL CLOSED: they are withheld, never
treated as one distinct company each.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Tuple

import config

#: Reason recorded for postings withheld because identity could not be resolved.
UNRESOLVED_IDENTITY = "company_identity_unresolved"
#: Reason recorded for the non-elected postings of a resolved employer.
COLLAPSED_SIBLING = "collapsed_into_company_representative"
#: Reason recorded when EVERY posting of an employer is already covered by an
#: active lead for that same company+function.
ALREADY_ACTIVE = "company_function_already_active"


class _Union:
    """Minimal union-find over string anchors."""

    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:      # path compression
            self.parent[key], key = root, self.parent[key]
        return root

    def union(self, *keys: str) -> None:
        present = [k for k in keys if k]
        if not present:
            return
        root = self.find(present[0])
        for other in present[1:]:
            other_root = self.find(other)
            if other_root != root:
                self.parent[other_root] = root


def _slug(job: Dict[str, Any]) -> str:
    return str(job.get("org_linkedin_slug") or "").strip().lower()


def _domain(job: Dict[str, Any]) -> str:
    """The trusted employer domain, re-checked through the canonical resolver.

    ``employer_website`` is already the mapper's defensible-domain output; it is
    re-validated against the intermediary list so an ATS or aggregator host can
    never become a company identity anchor.
    """
    from company_identity import safe_company_domain
    return safe_company_domain(str(job.get("employer_website") or ""),
                               config.INTERMEDIARY_JOB_DOMAINS) or ""


def identity_anchors(job: Dict[str, Any]) -> Tuple[str, str, str]:
    """Return ``(slug_anchor, domain_anchor, downstream_key_anchor)``."""
    from hiring_manager import company_key_for_job
    slug, domain = _slug(job), _domain(job)
    downstream = str(company_key_for_job(job) or "").strip().lower()
    return (f"slug:{slug}" if slug else "",
            f"dom:{domain}" if domain else "",
            f"ck:{downstream}" if downstream else "")


def identity_resolved(job: Dict[str, Any]) -> bool:
    """True when a TRUSTED identifier exists. A bare employer name is not one."""
    return bool(_slug(job) or _domain(job))


def _posted_at(job: Dict[str, Any]) -> str:
    for key in ("_fantastic_date_posted", "job_posted_at_datetime_utc",
                "_fantastic_date_created"):
        value = str(job.get(key) or "").strip()
        if value:
            return value
    return ""


_ROLE_GATE_RANK = {"PASS": 2, "NEEDS_CHECK": 1, "UNVERIFIED": 1}
_RELEVANCE_RANK = {"accept": 2, "review": 1, "reject": 0}


def representative_strength(job: Dict[str, Any]) -> Tuple:
    """Deterministic strength ordering; higher is stronger. Never random.

    Ordered exactly as the downstream value chain is ordered: trusted identity,
    verified role match, an actual buyer-title mapping, a resolved campaign
    bucket, then recency. The provider job id is NOT part of this tuple -- ties
    are broken by the caller, which elects the lowest id.
    """
    from role_mapping import get_target_titles_for_job, get_bucket_name_for_job

    has_domain = 1 if _domain(job) else 0
    has_slug = 1 if _slug(job) else 0
    gate = _ROLE_GATE_RANK.get(str(job.get("_role_gate_state") or "").upper(), 0)
    relevance_status = _RELEVANCE_RANK.get(
        str(job.get("_role_relevance_status") or "").lower(), 0)
    try:
        relevance = int(job.get("_role_relevance_score") or 0)
    except (TypeError, ValueError):
        relevance = 0
    try:
        has_titles = 1 if get_target_titles_for_job(job, None) else 0
    except Exception:  # noqa: BLE001 - a mapping error only lowers this candidate
        has_titles = 0
    try:
        has_bucket = 1 if str(get_bucket_name_for_job(job) or "").strip() else 0
    except Exception:  # noqa: BLE001
        has_bucket = 0
    return (
        has_domain, has_slug,                # 1. clean / trusted company identity
        gate, relevance_status, relevance,   # 2. strongest supported role match
        has_titles,                          # 3. valid existing HM target mapping
        has_bucket,                          # 4. resolved production role bucket
        _posted_at(job),                     # 5. newest posting
    )


def elect_representative(members: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Elect one posting for an employer. Deterministic and order-independent.

    Candidates are first ordered by provider job id, then the strongest is taken
    with ``max``, which returns the FIRST maximal element -- so an exact tie is
    always resolved to the lowest stable job id regardless of input order.
    """
    ordered = sorted(members, key=lambda j: str(j.get("job_id") or ""))
    return max(ordered, key=representative_strength)


@dataclass
class CollapseResult:
    representatives: List[Dict[str, Any]] = field(default_factory=list)
    #: (job, reason) for every posting that did not become an opportunity.
    withheld: List[Tuple[Dict[str, Any], str]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


def collapse_company_opportunities(
        jobs: Iterable[Dict[str, Any]], *,
        suppressed_function_keys: Any = None,
        function_keys_for_job: Any = None) -> CollapseResult:
    """Elect at most one opportunity per trusted employer identity.

    The elected representative carries ``_company_collapse`` provenance naming
    every sibling posting it stands for, so the suppressed openings survive as
    evidence without triggering a second enrichment or a second lead.

    ``suppressed_function_keys`` is the set of company+function keys already
    covered by an active lead. Postings matching one are excluded from the
    election BEFORE the strongest is chosen -- electing a candidate that the
    existing-state dedupe will immediately drop would waste the whole employer.
    This never creates a lead that the dedupe would otherwise have blocked; if
    every posting of an employer is already covered, the employer is withheld.
    """
    jobs = list(jobs)
    covered = {str(k) for k in (suppressed_function_keys or ())}
    union = _Union()
    resolved: List[Dict[str, Any]] = []
    result = CollapseResult()

    for job in jobs:
        if not identity_resolved(job):
            result.withheld.append((job, UNRESOLVED_IDENTITY))
            continue
        union.union(*[a for a in identity_anchors(job) if a])
        resolved.append(job)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for job in resolved:
        anchors = [a for a in identity_anchors(job) if a]
        groups.setdefault(union.find(anchors[0]), []).append(job)

    def _already_active(job: Dict[str, Any]) -> bool:
        if not covered or function_keys_for_job is None:
            return False
        try:
            keys = {str(k) for k in (function_keys_for_job(job) or ())}
        except Exception:  # noqa: BLE001 - an unresolvable key never suppresses
            return False
        return bool(keys and keys <= covered)

    multi_job_companies = 0
    already_active_companies = 0
    for _root, members in sorted(groups.items()):
        if len(members) > 1:
            multi_job_companies += 1
        eligible = [j for j in members if not _already_active(j)]
        if not eligible:
            already_active_companies += 1
            result.withheld.extend((j, ALREADY_ACTIVE) for j in members)
            continue
        elected = elect_representative(eligible)
        # Provenance still names EVERY other opening at this employer; the
        # already-covered ones are booked under their own reason so the
        # per-posting accounting stays exact.
        siblings = [j for j in members if j is not elected]
        result.withheld.extend(
            (j, ALREADY_ACTIVE if _already_active(j) else COLLAPSED_SIBLING)
            for j in siblings)
        winner = dict(elected)
        winner["_company_collapse"] = {
            "represented_postings": len(members),
            "suppressed_postings": len(siblings),
            "related_job_ids": sorted(str(j.get("job_id") or "") for j in siblings),
            "related_job_titles": sorted({str(j.get("job_title") or "") for j in siblings}),
            "identity_anchors": [a for a in identity_anchors(winner) if a],
        }
        # ``related_job_ids`` is the field the lead layer already reads for
        # multi-opening provenance; extend it rather than replace it.
        existing_related = [str(x) for x in (winner.get("related_job_ids") or [])]
        winner["related_job_ids"] = sorted(
            set(existing_related) | set(winner["_company_collapse"]["related_job_ids"]))
        result.representatives.append(winner)

    result.representatives.sort(key=lambda j: str(j.get("job_id") or ""))
    unresolved = sum(1 for _j, r in result.withheld if r == UNRESOLVED_IDENTITY)
    result.metrics = {
        "input_postings": len(jobs),
        "identity_unresolved_withheld": unresolved,
        # Unresolved postings cannot be grouped, so each is counted as its own
        # pre-collapse company -- that is exactly the over-count this fails closed
        # on, and reporting it any other way would hide the loss.
        "unique_companies_before_collapse": len(groups) + unresolved,
        "company_opportunities_after_collapse": len(result.representatives),
        "multi_job_companies": multi_job_companies,
        "jobs_suppressed_by_company_collapse": sum(
            1 for _j, r in result.withheld if r == COLLAPSED_SIBLING),
        # Employers every one of whose openings is already covered by an active
        # lead for that same function -- withheld, not re-enriched.
        "companies_already_active": already_active_companies,
        "postings_already_active": sum(
            1 for _j, r in result.withheld if r == ALREADY_ACTIVE),
    }
    return result
