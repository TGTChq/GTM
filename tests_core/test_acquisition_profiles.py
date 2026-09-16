"""No network/DB: query contracts, recall counterexamples and scheduler bounds."""
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from tgtc_core.config import Settings
from tgtc_core.domain.acquisition_query import (
    PRIORITY_PROFILE as P, DISCOVERY_PROFILE as D, LEGACY_PROFILE as L,
    balanced_slots, count_query, frozen_page_query, profile_filters, provider_list,
)
from tgtc_core.providers.fantastic import FantasticClient, read_quota
from tgtc_core.providers.http import Response
from tgtc_core.services.acquisition import SOURCE_ATS, SOURCE_JOB_BOARDS
from tgtc_core.testing.acquisition_scope import matches_filters, values
from tgtc_core.testing.fakes import FakeFantastic, make_posting_row
from tests_core.helpers import acquisition


def row(**over):
    result = dict(title="Unusual invented job title", org_linkedin_headcount=100,
                  ai_employment_type=["FULL_TIME"], ai_taxonomies_a=["Software"],
                  org_linkedin_industry="Software Development", location_type="On-site")
    result.update(over)
    return result


@pytest.mark.parametrize("profile", [P, D, L])
def test_no_closed_title_or_remote_or_seniority_gate(profile):
    p = profile_filters(profile)
    assert not {"title", "title_advanced", "description", "description_advanced", "seniority",
                "ai_work_arrangement", "ai_taxonomies_a_primary", "exclude_ai_taxonomies_a"} & p.keys()


@pytest.mark.parametrize("title", ["Staff Accountant", "Customer Happiness Hero", "Digital Enablement Partner",
                                     "Revenue Systems Architect", "E-commerce Operations Specialist", "Invented XYZ"])
def test_arbitrary_titles_and_onsite_office_are_not_lost(title):
    assert matches_filters(row(title=title), profile_filters(P))


def test_secondary_function_still_retrievable():
    assert matches_filters(row(ai_taxonomies_a=["Manufacturing", "Finance & Accounting"]), profile_filters(P))


@pytest.mark.parametrize("count,expected", [(24, False), (25, True), (1000, True), (1001, False), (None, False)])
def test_headcount_bounds_are_explicit_and_discovery_recovers_missing_or_mistagged(count, expected):
    candidate = row(org_linkedin_headcount=count)
    assert matches_filters(candidate, profile_filters(P)) is expected
    assert matches_filters(candidate, profile_filters(D))


@pytest.mark.parametrize("types,priority", [(["FULL_TIME"], True), (["PART_TIME"], False), (None, False),
                                           (["FULL_TIME", "CONTRACTOR"], True)])
def test_employment_overlap_not_a_permanent_contract_guarantee(types, priority):
    candidate = row(ai_employment_type=types)
    assert matches_filters(candidate, profile_filters(P)) is priority
    assert matches_filters(candidate, profile_filters(D))


@pytest.mark.parametrize("categories", [None, [], ["Manufacturing"], ["Healthcare"], ["Other Not Yet Known"]])
def test_discovery_has_no_taxonomy_gate(categories):
    assert not matches_filters(row(ai_taxonomies_a=categories), profile_filters(P))
    assert matches_filters(row(ai_taxonomies_a=categories), profile_filters(D))


def test_physical_work_is_not_solved_by_categories():
    candidate = row(title="Warehouse picker", ai_taxonomies_a=["Logistics"])
    assert matches_filters(candidate, profile_filters(P))  # downstream evidence is STILL required


@pytest.mark.parametrize("profile", [P, D])
def test_agency_and_disallowed_employer_sector_stay_excluded(profile):
    assert not matches_filters(row(org_linkedin_industry="Hospitals and Health Care"), profile_filters(profile))
    assert not matches_filters(row(org_linkedin_recruitment_agency_derived=True), profile_filters(profile))


def test_comma_values_encoded_as_individual_csv_values():
    v = ["Health, Wellness & Fitness", "Software Development"]
    assert values(provider_list(v)) == set(v)


@pytest.mark.parametrize("profile", [P, D])
@pytest.mark.parametrize("source", [SOURCE_ATS, SOURCE_JOB_BOARDS])
def test_both_feeds_keep_endpoint_specific_contract(source, profile):
    t = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    svc = acquisition(MagicMock(), FakeFantastic(), lambda: t)
    endpoint, p = svc.request_params(source, lower=t.replace(hour=7), upper=t.replace(hour=8), offset=0, profile=profile)
    if source == SOURCE_ATS:
        assert p["include_basic_organization_details"] == "true" and "exclude_ats_duplicate" not in p
    else:
        assert p["exclude_ats_duplicate"] == "true" and "include_basic_organization_details" not in p
    endpoint2, counted = count_query(endpoint, p)
    assert endpoint2 == endpoint + "-count"
    assert not {"limit", "offset", "description_format", "include_basic_organization_details"} & counted.keys()
    assert all(counted[k] == v for k, v in profile_filters(profile).items())


def test_jb_only_keeps_ats_flagged_jobs():
    t = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    svc = acquisition(MagicMock(), FakeFantastic(), lambda: t, sources=[SOURCE_JOB_BOARDS])
    _, p = svc.request_params(SOURCE_JOB_BOARDS, lower=t.replace(hour=7), upper=t.replace(hour=8), offset=0, profile=P)
    assert "exclude_ats_duplicate" not in p


def test_old_queries_are_not_narrowed_at_existing_offsets():
    old = {"endpoint": "/v1/active-jb", "time_frame": "7d", "location": "United States"}
    new = {**old, **profile_filters(P)}
    assert frozen_page_query(new, old, offset=100, limit=20) == {**old, "offset": 100, "limit": 20}


def test_query_versions_fail_closed_and_configuration_is_opt_in():
    with pytest.raises(ValueError):
        profile_filters("typo")
    assert Settings.from_env({}).acquisition_strategy == L
    for value in ["all", "balanced_v2", "balanced"]:
        with pytest.raises(ValueError):
            Settings.from_env({"TGTC_ACQUISITION_STRATEGY": value})
    with pytest.raises(ValueError):
        Settings.from_env({"TGTC_FANTASTIC_CYCLE_PAGE_SLOTS": "nonsense"})


def test_balanced_slots_have_both_feeds_and_eighty_twenty_allocation():
    slots = list(balanced_slots((SOURCE_JOB_BOARDS, SOURCE_ATS), 10))
    assert len(slots) == 10
    assert Counter(p for s, p in slots) == {P: 8, D: 2}
    assert Counter(s for s, p in slots) == {SOURCE_JOB_BOARDS: 5, SOURCE_ATS: 5}
    assert {s for s, p in slots if p == D} == {SOURCE_JOB_BOARDS, SOURCE_ATS}
    assert slots[2][1] == D


@pytest.mark.parametrize("size", [0, 4, 101])
def test_slots_fail_closed_on_invalid_limits(size):
    with pytest.raises(ValueError):
        list(balanced_slots((SOURCE_JOB_BOARDS,), size))


def test_two_feeds_require_enough_slots_for_both_exploration_paths():
    with pytest.raises(ValueError):
        list(balanced_slots((SOURCE_JOB_BOARDS, SOURCE_ATS), 5))


def test_cohort_audit_is_explicitly_not_an_eligibility_test():
    from rebuild.audit_acquisition_profiles import audit
    data = {"postings": [{"id": 1, "source": SOURCE_JOB_BOARDS, "provider_job_id": "a",
                          "title": "Marketing Analyst", "org_json": {"org_linkedin_headcount": None},
                          "structured_json": {"ai_taxonomies_a": ["Marketing"], "ai_employment_type": ["FULL_TIME"]}}]}
    result = audit(data)
    assert result["priority_candidates"] == 0
    assert result["discovery_only_candidates"] == 1
    assert "NOT qualified" in result["warning"]


def test_provider_spend_header_is_not_inferred_from_cached_balance():
    response = Response(200, headers={"x-api-jobs-remaining": "20000"}, text="[]")
    assert read_quota(response).jobs_this_request is None
    response.headers["x-api-jobs-this-request"] = "7"
    assert read_quota(response).jobs_this_request == 7


def test_fake_provider_recovers_unknowns_without_requiring_titles():
    t = datetime(2026, 9, 16, 7, 30, tzinfo=timezone.utc)
    candidates = [make_posting_row(id=str(i), organization="Example", domain="example.com",
                                 description="Full-time knowledge work.", date_created=t, **facts)
                  for i, facts in enumerate([row(), row(org_linkedin_headcount=None),
                                             row(ai_taxonomies_a=None), row(ai_employment_type=["PART_TIME"])])]
    fake = FakeFantastic(rows=candidates)
    client = FantasticClient(fake, api_key="sim", base_url="https://data.fantastic.jobs")
    common = dict(date_created_gte="2026-09-16T07:00:00Z", date_created_lt="2026-09-16T08:00:00Z")
    assert len(client.fetch_page("/v1/active-jb", {**common, **profile_filters(P)}).rows) == 1
    assert len(client.fetch_page("/v1/active-jb", {**common, **profile_filters(D)}).rows) == 4


@pytest.mark.parametrize("failure", ["request_error:http_400", "timeout_uncertain", "auth_refused",
                                      "partition_outside_provider_time_frame", "lease_lost"])
def test_new_profile_error_cannot_hide_behind_another_successful_page(monkeypatch, failure):
    from types import SimpleNamespace
    from tgtc_core.__main__ import _bounded_acceptance_failure
    monkeypatch.setenv("TGTC_ACCEPTANCE_MODE", "bounded")
    report = SimpleNamespace(acquisition=[{"query_profile": P, "pages": 1, "stop_reason": "complete"},
                                          {"query_profile": D, "pages": 0, "stop_reason": failure}], stages={})
    assert _bounded_acceptance_failure(report, acquire=True) == failure


@pytest.mark.parametrize("raw", ["-1", "garbage", "", "1.5"])
def test_invalid_spend_header_is_unknown_not_a_confirmed_charge(raw):
    assert read_quota(Response(200, headers={"x-api-jobs-this-request": raw})).jobs_this_request is None
