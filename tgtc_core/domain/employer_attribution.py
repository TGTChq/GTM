"""Detect corroborated publisher/employer conflicts; never merge on job prose.

Two independent textual anchors are required: a named equal-opportunity
employer AND its name-consistent application email domain. This is a safety
refusal, not authority to transfer a vacancy, a buyer, or organization facts.
"""
from __future__ import annotations

import re
from typing import Optional

from .identity import company_names_compatible, domain_name_consistent, safe_employer_domain

_EOE = re.compile(
    r"(?:^|[\n(])\s*([A-Z][A-Za-z0-9 &'’.,-]{2,100}?)\s+is\s+an?\s+"
    r"equal\s+opportunity\s+employer\b", re.I)
_APPLICATION = re.compile(
    r"\b(?:email|send|submit)\b[^\n.!?]{0,160}\b(?:resume|résumé|application|cv)\b"
    r"[^\n!?]{0,120}?\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.I)


def employer_attribution_conflict(description: Optional[str], *, employer_name: str,
                                  employer_domain: str) -> Optional[dict]:
    text = str(description or "")
    domain = safe_employer_domain(employer_domain)
    for match in _EOE.finditer(text):
        named = match.group(1).strip(" .,")
        # Acronyms corroborate the two textual anchors only; they never merge
        # with an existing employer or establish employment for a person.
        words = re.findall(r"[a-z0-9]+", named.lower())
        acronym = "".join(w[0] for w in words if w not in {"the", "and", "of", "inc", "llc"})
        for application in _APPLICATION.finditer(text):
            observed = safe_employer_domain(application.group(1))
            if not observed or observed == domain:
                continue
            if not (domain_name_consistent(named, observed)
                    or (len(acronym) >= 3 and observed.split('.')[0] == acronym)):
                continue
            if company_names_compatible(named, employer_name):
                continue  # alternate domain of the same named employer is not this conflict
            return {"reason": "employer_attribution_conflict", "named_employer": named,
                    "application_domain": observed, "provider_employer": employer_name[:150],
                    "provider_domain": domain, "excerpt": match.group(0)[:200]}
    return None
