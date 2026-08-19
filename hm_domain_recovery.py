"""Mechanism B -- employer-domain corroboration for HM recovery.

Pure, deterministic decision: may an ALTERNATE domain (the one Apollo resolved, or
one surfaced by domain-resolution evidence) be used for a hiring-manager search in
place of the normal source/search domain?

The answer is YES only when strong structured evidence proves the alternate is the
SAME employer / a legitimate rebrand / parent / first-party identity -- never a
generic "accept whatever domain Apollo returned". It is fail-closed: any name
conflict, staffing/client ambiguity, or merely-speculative relationship is rejected.

This module holds NO network or Airtable I/O and never mutates canonical identity;
the caller keeps source/canonical/recovered domains as separate provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict

import company_identity as ci
import config

#: Tokens that signal a staffing/agency/client-of-record relationship -- an
#: alternate whose name carries these (while the source does not) is ambiguous and
#: must not be corroborated as the same first-party employer.
_STAFFING_TOKENS = (
    "staffing", "recruiting", "recruitment", "talent solutions", "talent acquisition",
    "staffing agency", "employment agency", "personnel", "temp agency", "temp staffing",
    "workforce solutions", "search firm", "headhunter", "rpo", "peo",
    "professional employer", "employer of record",
)


@dataclass
class RecoveryDecision:
    accepted: bool
    recovered_domain: str = ""
    reason: str = ""
    evidence: Dict[str, object] = field(default_factory=dict)


def _linkedin_key(url_or_slug) -> str:
    """Normalize a LinkedIn company URL or slug to a bare comparable key."""
    s = str(url_or_slug or "").strip().lower()
    if not s:
        return ""
    m = re.search(r"/company/([^/?#]+)", s)
    key = (m.group(1) if m else s)
    return key.strip().strip("/")


def _has_staffing_token(name: str) -> bool:
    norm = ci.normalize_company_name(name) or ""
    return any(tok in norm for tok in _STAFFING_TOKENS)


def corroborate_recovery_domain(
    *,
    source_domain: str,
    source_name: str,
    candidate_domain: str,
    candidate_name: str,
    source_linkedin: str = "",
    candidate_linkedin: str = "",
) -> RecoveryDecision:
    """Decide whether ``candidate_domain`` may be used as a recovery search domain
    for the employer identified by ``source_name``/``source_domain``.

    Acceptance tiers (strongest first); anything else is rejected fail-closed:
      * ``linkedin_identity_match`` -- source & candidate share a LinkedIn org id;
      * ``exact_name_and_domain_consistent`` -- identical normalized company name AND
        the candidate domain is name-consistent (first-party) with it;
      * ``name_compatible_domain_consistent`` -- compatible names AND the candidate
        domain is name-consistent with BOTH names (rebrand/alternate first-party).
    """
    denylist = getattr(config, "INTERMEDIARY_JOB_DOMAINS", frozenset())
    ev: Dict[str, object] = {
        "source_domain": source_domain, "source_name": source_name,
        "candidate_domain": candidate_domain, "candidate_name": candidate_name,
    }
    cand = ci.safe_company_domain(candidate_domain, denylist) if candidate_domain else ""
    if not cand:
        return RecoveryDecision(False, reason="no_candidate_domain", evidence=ev)

    src = ci.safe_company_domain(source_domain, denylist) if source_domain else ""
    if src and (cand == src or ci.domains_equivalent(cand, src)):
        return RecoveryDecision(False, reason="no_alternate_domain", evidence=ev)

    # Staffing/client ambiguity: reject when the alternate looks like an agency and
    # the source does not (a genuinely staffing source is left to normal handling).
    if _has_staffing_token(candidate_name) and not _has_staffing_token(source_name):
        return RecoveryDecision(False, reason="staffing_ambiguity", evidence=ev)

    # Names must be at least compatible; a material conflict is an unrelated company.
    names_compatible = ci.company_names_compatible(source_name, candidate_name)

    lk_s, lk_c = _linkedin_key(source_linkedin), _linkedin_key(candidate_linkedin)
    if lk_s and lk_c and lk_s == lk_c:
        # Same LinkedIn org is definitive same-employer proof -- but still refuse if
        # the names blatantly conflict (guards against bad LinkedIn data).
        if names_compatible or ci.normalize_company_name(source_name) == "" \
                or ci.normalize_company_name(candidate_name) == "":
            return RecoveryDecision(True, cand, "linkedin_identity_match",
                                    {**ev, "linkedin": lk_s})
        return RecoveryDecision(False, reason="names_conflict_despite_linkedin", evidence=ev)

    if not names_compatible:
        return RecoveryDecision(False, reason="names_conflict", evidence=ev)

    src_norm = ci.normalize_company_name(source_name)
    cand_norm = ci.normalize_company_name(candidate_name)
    if src_norm and src_norm == cand_norm and ci.domain_name_consistent(source_name, cand):
        return RecoveryDecision(True, cand, "exact_name_and_domain_consistent", ev)

    # Compatible (not identical) names: require the alternate domain to be first-party
    # consistent with BOTH the source and candidate names -- rejects "similar names
    # but different identity" where the domain does not actually belong to the name.
    if ci.domain_name_consistent(source_name, cand) and ci.domain_name_consistent(candidate_name, cand):
        return RecoveryDecision(True, cand, "name_compatible_domain_consistent", ev)

    return RecoveryDecision(False, reason="insufficient_evidence", evidence=ev)
