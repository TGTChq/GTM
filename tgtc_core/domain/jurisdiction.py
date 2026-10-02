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


#: US states, DC and the inhabited territories, by full name and USPS code.
#: Read ONLY from the person's own `state` field. "Georgia" is deliberately
#: absent: it is also a country, so the bare name is ambiguous and ambiguity is
#: unknown. Its postal code "GA" in a state field is unambiguous and kept.
_US_STATE_NAMES = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana",
    "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia", "washington dc", "washington d.c.", "puerto rico",
})
_US_STATE_CODES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND",
    "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR",
})


def _us_state(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    if raw.upper() in _US_STATE_CODES and len(raw) == 2:
        return True
    return raw.lower() in _US_STATE_NAMES


def observe_contact_country_with_provenance(person: Mapping[str, Any]) -> tuple:
    """The PERSON's own country and the field that established it.

    Returns ``(country, provenance)``, both ``""`` when the person's own record
    does not determine it. Only the person's OWN fields are read: an explicit
    ``country`` first, then a US ``state``. A city alone is never enough
    (Paris TX, London ON). The organization block -- the employer's location --
    is never read: a US company employs people in many countries, and employer
    location is not proof of a contact's residence or jurisdiction.

    Measured need, production 2026-09-20: 100 otherwise-eligible contacts were
    blocked `compliance:unknown_jurisdiction:absent`, and the person's own
    `state` was being discarded at enrichment time.
    """
    person = person or {}
    declared = str(person.get("country") or "").strip()
    if declared:
        # A declared country is decisive even when the matrix does not cover it
        # (Canada normalises to ""): it must never fall through to a state
        # guess, or a Canadian record with state "CA" would read as California.
        country = normalize_country(declared)
        return (country, "person.country") if country else ("", "")
    if _us_state(person.get("state")):
        return "US", "person.state"
    return "", ""


def resolve_person_contact_country(person: Mapping[str, Any]) -> tuple:
    """A stored person row's country and where it came from.

    ``people.contact_country`` when set. Otherwise the person's OWN Apollo
    evidence already stored at ``facts_json.enriched`` -- the same record, read
    again, with zero new paid enrichment.

    Measured need, production 2026-09-20: of 100 contacts blocked
    ``compliance:unknown_jurisdiction:absent``, 58 carried "United States" in
    their own stored evidence. The column arrived with migration 011, nullable
    and with no backfill, and the reuse path reuses a stored record without
    recomputing it.

    Only the person's own fields are ever read; the stored organization block
    is the employer and is ignored.
    """
    import json

    person = person or {}
    column = str(person.get("contact_country") or "").strip()
    if column:
        return column, "people.contact_country"
    facts = person.get("facts_json")
    if isinstance(facts, str):
        try:
            facts = json.loads(facts)
        except ValueError:
            facts = None
    enriched = (facts or {}).get("enriched") if isinstance(facts, Mapping) else None
    if not isinstance(enriched, Mapping):
        return "", ""
    country, provenance = observe_contact_country_with_provenance(enriched)
    return (country, f"stored:{provenance}") if country else ("", "")


def observe_contact_country(person: Mapping[str, Any]) -> str:
    """The PERSON's own country, from the enriched person record.

    The only country a person gate may decide on. A job's location does not
    determine it, and neither does the employer's.
    """
    return observe_contact_country_with_provenance(person)[0]


def corporate_subscriber_status(entity_type: Any) -> str:
    """The stored verdict for a stored legal form. One call, so the column and
    the gate can never be computed by two different rules."""
    return classify_corporate_subscriber(entity_type)
