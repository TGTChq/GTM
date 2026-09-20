"""Observing jurisdiction from provider shapes, so the policy never has to guess.

``policy/compliance.py`` decides; this module is the only place that reads a
provider's fields to produce the jurisdiction facts it decides on. The split is
deliberate: the policy module has no provider vocabulary at all, so it cannot
quietly acquire a fallback from one country field to another, and every
observation here answers exactly one question about exactly one subject.

Three subjects, three observations, never substituted for one another:

* the JOB's country, from the provider's derived country list;
* the EMPLOYER's country and legal form, from the provider's organization block;
* the PERSON's country, from the enriched person record.

Every observation returns ``""`` when the evidence does not determine an answer
-- including when it determines two different answers. Ambiguity is unknown, and
unknown fails closed for sending while the record stays counted for capacity.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..policy.compliance import (
    CORPORATE, ENTITY_UNKNOWN, NOT_CORPORATE, classify_corporate_subscriber, normalize_country,
)

#: Longest legal-form suffix worth testing at the end of a registered name
#: ("Community Interest Company" is three words, "company limited by guarantee"
#: four).
_MAX_SUFFIX_WORDS = 4


def _single(values: Sequence[str]) -> str:
    """The one matrix country in ``values``, or "" for none and for more than one."""
    found = {normalize_country(v) for v in values}
    found.discard("")
    return found.pop() if len(found) == 1 else ""


def observe_job_country(countries: Any) -> str:
    """The JOB's country from the provider's ``countries_derived``.

    A posting listed under two different matrix countries determines neither,
    so it is unknown rather than whichever happened to be first.
    """
    if isinstance(countries, (str, bytes)) or not isinstance(countries, (list, tuple, set)):
        countries = [countries] if countries else []
    return _single([str(c) for c in countries if c])


def observe_company_country(org: Mapping[str, Any]) -> str:
    """The EMPLOYER's own country, from the trailing country token of its
    published addresses. Not the job's country, and not the contact's.

    ``org_linkedin_locations`` entries end in a country code
    ("Silverdale Road, Earley, Reading, RG6 7HS, GB"). An employer publishing
    addresses in two matrix countries determines neither.
    """
    tails = []
    locations = (org or {}).get("org_linkedin_locations") or []
    if isinstance(locations, (str, bytes)):
        locations = [locations]
    for entry in locations:
        parts = [p.strip() for p in str(entry or "").split(",") if p.strip()]
        if parts:
            tails.append(parts[-1])
    found = _single(tails)
    if found:
        return found
    # A headquarters field is usually a city, so it resolves only when it
    # happens to name a country outright.
    return normalize_country((org or {}).get("org_linkedin_headquarters"))


def observe_legal_entity_type(org: Mapping[str, Any]) -> str:
    """The employer's LEGAL FORM, or "" when nothing establishes one.

    Two independent sources, and an exclusion from either one wins:

    * the registered name's own suffix ("Acme Ltd", "Acme LLP", "Acme Trust") --
      a legal form is part of a registered company name, so this is real
      evidence rather than a label someone typed;
    * the provider's declared company type, which is usable ONLY when it names a
      form the matrix excludes ("Sole Proprietorship", "Self-Employed",
      "Partnership"). Its other values are ownership descriptors ("Privately
      Held", "Public Company") that say who owns the body, not whether it has
      separate legal personality -- ``classify_corporate_subscriber`` returns
      ENTITY_UNKNOWN for those, and an unknown entity type is never corporate.

    Fail closed on disagreement: a declared sole proprietorship is not made
    corporate by a name that ends in "Ltd".
    """
    org = org or {}
    candidates = [_name_suffix_form(org.get("org_linkedin_name") or org.get("organization")),
                  str(org.get("org_linkedin_type") or "").strip()]
    classified = [(value, classify_corporate_subscriber(value)) for value in candidates if value]
    for value, verdict in classified:
        if verdict == NOT_CORPORATE:
            return value
    for value, verdict in classified:
        if verdict == CORPORATE:
            return value
    return ""


def _name_suffix_form(name: Any) -> str:
    """The legal form named at the END of a registered company name, or ""."""
    words = str(name or "").replace(",", " ").split()
    for size in range(min(_MAX_SUFFIX_WORDS, len(words)), 0, -1):
        suffix = " ".join(words[-size:])
        if classify_corporate_subscriber(suffix) != ENTITY_UNKNOWN:
            return suffix
    return ""


def observe_contact_country(person: Mapping[str, Any]) -> str:
    """The PERSON's own country, from the enriched person record.

    The only country field a person gate may decide on. A job's location does
    not determine it, and neither does the employer's.
    """
    return normalize_country((person or {}).get("country"))


def corporate_subscriber_status(entity_type: Any) -> str:
    """The stored verdict for a stored legal form. One call, so the column and
    the gate can never be computed by two different rules."""
    return classify_corporate_subscriber(entity_type)
