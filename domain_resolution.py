"""Deterministic, evidence-based employer-domain resolution + classification.

Motivation: in run 20260807T095512Z-8b8355aa, 353/452 (78%) hiring-manager
failures were ``no_search_domain`` -- Apollo was never queried because no employer
domain could be resolved. Offline analysis of that run proved this is a SOURCE
QUALITY problem, not a resolver gap: 0/353 carried a usable direct employer host
and ~92% were single-aggregator (Himalayas) postings with the employer identity
stripped. So this module does NOT guess domains. It:

  1. adds two SAFE, deterministic recovery steps the enrichment path did not use
     (a direct ``apply_options`` employer host, and a curated
     ``COMPANY_DOMAIN_ALIASES`` lookup for well-known acronym employers), and
  2. CLASSIFIES every unresolved company explicitly, so a staffing/aggregator
     poster is never conflated with a genuine resolver failure.

Every accepted domain passes the intermediary denylist first, so a job-board or
staffing host can never masquerade as the employer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

import config
from company_identity import (
    safe_company_domain, is_intermediary_domain, domain_name_consistent,
)
from domain_utils import normalize_company_domain

# Resolution confidence tiers.
VERIFIED = "verified"        # a first-party employer host
SEARCH_ONLY = "search_only"  # good enough to search Apollo, not a delivery identity
NONE = "none"

# Classification of a posting's EMPLOYER identity vs its SOURCE platform. The
# source (Himalayas, a job board, a staffing agency) is never confused with the
# employer.
DIRECT_EMPLOYER = "direct_employer"                       # employer domain resolved
ATS_EMPLOYER_KNOWN = "ats_employer_known"                 # first-party name known (ATS), domain unresolved
KNOWN_EMPLOYER_UNRESOLVED_DOMAIN = "known_employer_unresolved_domain"  # acronym/short-brand
AGGREGATOR_EMPLOYER_UNRESOLVED = "aggregator_employer_unresolved"      # employer not visible in aggregator payload
INTERMEDIARY_UNKNOWN_CLIENT = "intermediary_unknown_client"           # staffing/recruiting, hidden client
INTERMEDIARY_KNOWN_CLIENT = "intermediary_known_client"               # staffing with a named client (reserved)
UNRESOLVED_NO_EVIDENCE = "unresolved_no_evidence"
# Back-compat alias (pre-existing name kept so older callers/tests still resolve).
INTERMEDIARY_UNRESOLVED = INTERMEDIARY_UNKNOWN_CLIENT


@dataclass
class DomainResolution:
    resolved_domain: str = ""
    resolution_method: str = "none"
    resolution_confidence: str = NONE
    unresolved_reason: str = ""
    classification: str = UNRESOLVED_NO_EVIDENCE

    def as_dict(self) -> Dict[str, Any]:
        return {
            "resolved_domain": self.resolved_domain,
            "resolution_method": self.resolution_method,
            "resolution_confidence": self.resolution_confidence,
            "unresolved_reason": self.unresolved_reason,
            "classification": self.classification,
        }


def _denylist() -> Sequence[str]:
    return getattr(config, "INTERMEDIARY_JOB_DOMAINS", ()) or ()


def _host(url: str) -> str:
    try:
        return urlparse(url if "//" in url else f"//{url}", scheme="").netloc.lower().split(":", 1)[0]
    except Exception:
        return ""


def _safe(domain_or_url: Optional[str]) -> str:
    """Reduce to a registrable employer domain, or '' if intermediary/empty."""
    if not domain_or_url:
        return ""
    return safe_company_domain(str(domain_or_url), _denylist()) or ""


def _staffing_poster(employer_name: str) -> bool:
    name = (employer_name or "").strip().lower()
    if not name:
        return False
    for kw in (getattr(config, "STAFFING_EMPLOYER_KEYWORDS", []) or []):
        if str(kw).strip().lower() in name:
            return True
    for known in (getattr(config, "KNOWN_STAFFING_EMPLOYERS", []) or []):
        if str(known).strip().lower() == name:
            return True
    return False


def _alias_domain(employer_name: str) -> str:
    aliases = {str(k).strip().lower(): str(v).strip()
               for k, v in (getattr(config, "COMPANY_DOMAIN_ALIASES", {}) or {}).items()}
    dom = aliases.get((employer_name or "").strip().lower(), "")
    return _safe(dom) if dom else ""


def _apply_options_direct_host(job: Mapping[str, Any]) -> str:
    """A direct-employer host from apply_options (the enrichment path only looked at
    the primary apply link). Only ``is_direct`` entries, denylist-checked."""
    for opt in (job.get("apply_options") or []):
        if not isinstance(opt, Mapping):
            continue
        if opt.get("is_direct") is not True:
            continue
        dom = _safe(opt.get("apply_link") or opt.get("url") or "")
        if dom:
            return dom
    return ""


def _name_consistent_first_party_host(job: Mapping[str, Any], employer_name: str) -> str:
    """Return a first-party employer domain drawn from ANY apply/source URL on the
    posting whose registrable host (a) clears the intermediary denylist AND (b) is
    NAME-CONSISTENT with the employer (`domain_name_consistent`). This is the safe
    way to use the ``applicationLink``/``canonical_source_url`` that free feeds
    preserve but mark ``is_direct=False``: a company host that matches the employer
    name is accepted; a job-board host (e.g. governmentjobs.com) that does not match
    is rejected -- so we never misattribute a board as the employer. Evidence-backed,
    never a guess."""
    if not employer_name:
        return ""
    urls: List[str] = []
    for key in ("employer_website", "job_apply_link", "official_job_url",
                "canonical_source_url"):
        v = job.get(key)
        if v:
            urls.append(str(v))
    for opt in (job.get("apply_options") or []):
        if isinstance(opt, Mapping) and (opt.get("apply_link") or opt.get("url")):
            urls.append(str(opt.get("apply_link") or opt.get("url")))
    for u in urls:
        dom = _safe(u)
        if dom and domain_name_consistent(employer_name, dom):
            return dom
    return ""


def _any_non_intermediary_host_present(job: Mapping[str, Any]) -> bool:
    """True iff ANY host anywhere on the posting is non-intermediary (used only to
    distinguish 'only aggregator hosts' from 'no host evidence at all')."""
    cands: List[str] = []
    for key in ("employer_website", "job_apply_link", "official_job_url",
                "canonical_source_url", "job_google_link"):
        v = job.get(key)
        if v:
            cands.append(str(v))
    for opt in (job.get("apply_options") or []):
        if isinstance(opt, Mapping) and (opt.get("apply_link") or opt.get("url")):
            cands.append(str(opt.get("apply_link") or opt.get("url")))
    for c in cands:
        h = _host(c)
        if h and h not in ("google.com", "www.google.com") and not is_intermediary_domain(h, _denylist()):
            return True
    return False


def recover_search_domain(existing_search_domain: str, primary_job: Mapping[str, Any],
                          employer_name: str = "") -> DomainResolution:
    """Return a resolution bundle. If ``existing_search_domain`` is already set the
    enrichment path resolved it -- we only classify it ``direct_employer``. When it
    is empty we try the two SAFE additive recovery steps and, failing those,
    classify WHY it is unresolved (staffing / known-acronym / aggregator-only /
    no-evidence). This never overrides an existing domain and never accepts an
    intermediary host."""
    employer_name = employer_name or str(primary_job.get("employer_name") or "")

    if existing_search_domain:
        return DomainResolution(
            resolved_domain=existing_search_domain,
            resolution_method="enrichment_resolved",
            resolution_confidence=VERIFIED,
            classification=DIRECT_EMPLOYER,
        )

    # Additive recovery step 1: a direct apply_options employer host.
    dom = _apply_options_direct_host(primary_job)
    if dom:
        return DomainResolution(resolved_domain=dom, resolution_method="apply_options_direct_host",
                                resolution_confidence=VERIFIED, classification=DIRECT_EMPLOYER)

    # Additive recovery step 2: a name-consistent first-party host from any
    # apply/source URL (the applicationLink free feeds preserve but flag is_direct=
    # False). Accepted only when the host matches the employer name -- never a guess.
    dom = _name_consistent_first_party_host(primary_job, employer_name)
    if dom:
        return DomainResolution(resolved_domain=dom, resolution_method="name_consistent_first_party_host",
                                resolution_confidence=SEARCH_ONLY, classification=DIRECT_EMPLOYER)

    # Additive recovery step 3: curated deterministic alias for a known employer.
    dom = _alias_domain(employer_name)
    if dom:
        return DomainResolution(resolved_domain=dom, resolution_method="employer_alias_map",
                                resolution_confidence=SEARCH_ONLY, classification=DIRECT_EMPLOYER)

    # Unresolved -> classify WHY (never a false employer domain), source-aware so
    # a hidden-client/aggregator case is not counted as a technical resolver failure.
    source = str(primary_job.get("_acquisition_source") or "").strip().lower()
    if _staffing_poster(employer_name):
        # A staffing/recruiting poster: the row is an intermediary. We cannot tell
        # from the payload whether it is hiring internally or for a hidden client,
        # so classify as unknown-client and do NOT waste Apollo on a wrong org.
        return DomainResolution(unresolved_reason="staffing_or_hidden_client",
                                classification=INTERMEDIARY_UNKNOWN_CLIENT)
    if source.startswith("ats_"):
        # ATS board -> the employer NAME is first-party/deterministic (the company
        # hosts the board). Domain unresolved, but Apollo can still search by name.
        return DomainResolution(unresolved_reason="ats_employer_known_domain_unresolved",
                                classification=ATS_EMPLOYER_KNOWN)
    if _any_non_intermediary_host_present(primary_job):
        # A real non-intermediary host exists but is not name-consistent (acronym/
        # short brand): a known employer whose domain we won't assert without an alias.
        return DomainResolution(unresolved_reason="known_employer_acronym_domain",
                                classification=KNOWN_EMPLOYER_UNRESOLVED_DOMAIN)
    if _has_any_host(primary_job):
        # Only intermediary/aggregator hosts -> the employer is not visible in the
        # aggregator payload.
        return DomainResolution(unresolved_reason="aggregator_employer_unresolved",
                                classification=AGGREGATOR_EMPLOYER_UNRESOLVED)
    return DomainResolution(unresolved_reason="no_host_evidence",
                            classification=UNRESOLVED_NO_EVIDENCE)


def _has_any_host(job: Mapping[str, Any]) -> bool:
    for key in ("employer_website", "job_apply_link", "official_job_url",
                "canonical_source_url"):
        if job.get(key):
            return True
    for opt in (job.get("apply_options") or []):
        if isinstance(opt, Mapping) and (opt.get("apply_link") or opt.get("url")):
            return True
    return False


def summarize(resolutions: Sequence[DomainResolution]) -> Dict[str, Any]:
    """Aggregate per-run domain-resolution metrics (non-PII counts only)."""
    by_method: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    by_class: Dict[str, int] = {}
    resolved = 0
    for r in resolutions:
        by_class[r.classification] = by_class.get(r.classification, 0) + 1
        if r.resolved_domain:
            resolved += 1
            by_method[r.resolution_method] = by_method.get(r.resolution_method, 0) + 1
        else:
            by_reason[r.unresolved_reason or "unknown"] = \
                by_reason.get(r.unresolved_reason or "unknown", 0) + 1
    total = len(resolutions)
    return {
        "total_companies": total,
        "resolved": resolved,
        "unresolved": total - resolved,
        "resolution_rate": round(resolved / total, 4) if total else 0.0,
        "resolved_by_method": dict(sorted(by_method.items())),
        "unresolved_by_reason": dict(sorted(by_reason.items())),
        "classification": dict(sorted(by_class.items())),
    }
