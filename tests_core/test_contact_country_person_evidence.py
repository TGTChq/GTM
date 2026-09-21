"""A person's country from THEIR OWN location evidence, never the employer's.

Measured in production (2026-09-20): 100 otherwise-eligible contacts blocked as
`compliance:unknown_jurisdiction:absent`. `observe_contact_country` read only
the person record's `country` field and ignored the person's own `state` and
`city`, which Apollo returns separately. A person whose own record says
"state: California" is located in the United States; that is person-level
evidence, deterministic, and has provenance.

What this must NEVER do: infer the contact's country from the EMPLOYER. A US
company employs people in many countries. Employer location is not proof of a
contact's residence or jurisdiction, and a record with only employer location
stays blocked.
"""
from __future__ import annotations

import pytest

from tgtc_core.domain.jurisdiction import observe_contact_country, observe_contact_country_with_provenance


def test_an_explicit_country_still_decides():
    assert observe_contact_country({"country": "United States"}) == "US"
    assert observe_contact_country({"country": "Germany"}) == "DE"


@pytest.mark.parametrize("state", ["California", "CA", "New York", "NY", "Texas", "District of Columbia", "DC"])
def test_a_us_state_on_the_persons_own_record_is_us(state):
    assert observe_contact_country({"state": state}) == "US"


def test_provenance_is_recorded_for_a_state_inference():
    country, provenance = observe_contact_country_with_provenance({"state": "California", "city": "San Francisco"})
    assert country == "US"
    assert provenance == "person.state"


def test_provenance_for_an_explicit_country():
    assert observe_contact_country_with_provenance({"country": "United States"}) == ("US", "person.country")


def test_employer_location_is_never_used():
    """The organization block is not the person."""
    person = {"organization": {"country": "United States", "state": "California"},
              "organization_country": "United States"}
    assert observe_contact_country(person) == ""
    assert observe_contact_country_with_provenance(person) == ("", "")


def test_a_city_alone_is_not_enough():
    """Cities repeat across countries (Paris TX / Paris FR, London ON / London UK)."""
    assert observe_contact_country({"city": "Paris"}) == ""
    assert observe_contact_country({"city": "San Francisco"}) == ""


def test_an_ambiguous_state_value_is_not_evidence():
    """'Georgia' is a US state and a country; alone it is unknown."""
    assert observe_contact_country({"state": "Georgia"}) == ""


def test_a_non_us_region_is_not_mistaken_for_a_state():
    assert observe_contact_country({"state": "Ontario"}) == ""
    assert observe_contact_country({"state": "Bavaria"}) == ""


def test_a_declared_country_is_never_overridden_by_a_state_guess():
    """Canada is not in the compliance matrix and normalises to "". It must stay
    unknown -- blocked -- and never fall through to `state: CA` read as
    California. Found by this test, not by review."""
    assert observe_contact_country({"country": "Canada", "state": "CA"}) == ""
    assert observe_contact_country_with_provenance({"country": "Canada", "state": "CA"}) == ("", "")


def test_nothing_known_is_unknown():
    assert observe_contact_country({}) == ""
    assert observe_contact_country(None) == ""


# --- resolving a stored person row -------------------------------------------
# Measured, production 2026-09-20: of 100 contacts blocked
# `compliance:unknown_jurisdiction:absent`, 58 carried country "United States"
# in their OWN stored Apollo evidence (facts_json.enriched). The column
# `people.contact_country` arrived with migration 011, nullable and with no
# backfill, and the reuse path ("never pay twice") reuses the stored record
# without recomputing it. The approval read only the column.

from tgtc_core.domain.jurisdiction import resolve_person_contact_country  # noqa: E402


def test_the_column_wins_when_present():
    person = {"contact_country": "US", "facts_json": {"enriched": {"country": "Germany"}}}
    assert resolve_person_contact_country(person) == ("US", "people.contact_country")


def test_falls_back_to_the_persons_own_stored_evidence():
    person = {"contact_country": None, "facts_json": {"enriched": {"country": "United States"}}}
    assert resolve_person_contact_country(person) == ("US", "stored:person.country")


def test_falls_back_to_the_persons_own_stored_state():
    person = {"contact_country": "", "facts_json": {"enriched": {"state": "Texas"}}}
    assert resolve_person_contact_country(person) == ("US", "stored:person.state")


def test_stored_evidence_as_a_json_string_is_read():
    person = {"contact_country": None, "facts_json": '{"enriched": {"country": "United Kingdom"}}'}
    assert resolve_person_contact_country(person) == ("UK", "stored:person.country")


def test_a_non_matrix_country_in_stored_evidence_stays_unknown():
    person = {"contact_country": None, "facts_json": {"enriched": {"country": "India", "state": "CA"}}}
    assert resolve_person_contact_country(person) == ("", "")


def test_stored_employer_location_is_never_used():
    person = {"contact_country": None,
              "facts_json": {"enriched": {"organization": {"country": "United States"}}}}
    assert resolve_person_contact_country(person) == ("", "")


def test_nothing_stored_is_unknown():
    assert resolve_person_contact_country({"contact_country": None}) == ("", "")
    assert resolve_person_contact_country({}) == ("", "")
