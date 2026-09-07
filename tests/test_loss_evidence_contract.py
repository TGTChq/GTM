"""Regressions for conclusions that aggregate counters cannot establish."""

import json

import pytest

from orchestrator import loss_units as lu


def test_missing_delivery_is_unknown_in_serialized_report():
    report = json.loads(json.dumps(lu.decompose({})))
    assert report['delivery']['submitted'] is None
    assert report['delivery']['reconciles'] is None
    assert report['reach']['opportunities_without_outcome'] is None


def test_an_explicit_empty_delivery_can_reconcile():
    report = lu.delivery_reconciles(0, 0, {})
    assert report['reconciles'] is True
    assert report['unexplained'] == 0


@pytest.mark.parametrize('bad', [None, '', 'not a count', -1, 1.5, True])
def test_invalid_delivery_counts_do_not_turn_into_zero(bad):
    assert lu.delivery_reconciles(bad, 0, {})['reconciles'] is None


def test_unknown_skip_value_preserves_unknown_delivery():
    assert lu.delivery_reconciles(199, 28, {'no_contact': None})['reconciles'] is None


def test_missing_skip_map_is_not_an_explicit_empty_map():
    assert lu.delivery_reconciles(0, 0, None)['reconciles'] is None


def test_aliases_count_once_when_they_agree():
    result = lu.classify({'unverified': 155, 'email_unverified': 155})
    assert result['by_unit'][lu.DISPOSITION]['sum_within_unit'] == 155


def test_disagreeing_aliases_are_not_resolved_by_picking_the_larger():
    result = lu.classify({'unverified': 155, 'email_unverified': 160})
    assert result['by_unit'][lu.DISPOSITION]['sum_within_unit'] is None


def test_a_category_cannot_add_postings_and_opportunities():
    result = lu.classify({'REJECT_ROLE_MISMATCH': 7, 'not_icp': 3})
    assert result['by_category'][lu.INTERNAL_FILTER]['sum_within_category'] is None


def test_same_unit_does_not_establish_disjoint_populations():
    result = lu.classify({'not_icp': 3, 'company_unresolved': 3})
    assert result['by_unit'][lu.OPPORTUNITY]['sum_within_unit'] is None


def test_unknown_reason_count_remains_unknown():
    assert lu.classify({'future_reason': None})['unclassified_labels']['future_reason'] is None


def test_delivery_rows_are_not_opportunity_outcomes():
    report = lu.decompose({'metrics': {'qualified_opportunities': 1,
                                      'airtable_candidates': 2},
                           'stop_reason': 'topup:apollo_circuit_open'})
    assert report['reach']['opportunities_with_outcome'] is None
    assert report['reach']['opportunities_without_outcome'] is None


def test_stop_reason_does_not_assign_a_cause_to_missing_outcomes():
    report = lu.opportunity_reach(qualified_postings=20, opportunities_formed=10,
                                 opportunities_with_outcome=6,
                                 stop_reason='topup:apollo_circuit_open')
    assert report['opportunities_without_outcome'] == 4
    assert report['category_for_those_without_outcome'] == lu.UNATTRIBUTED


def test_outcomes_greater_than_population_are_not_clamped_to_zero():
    report = lu.opportunity_reach(qualified_postings=2, opportunities_formed=1,
                                 opportunities_with_outcome=2)
    assert report['opportunities_without_outcome'] is None


def test_no_search_cannot_be_classified_as_searched_without_result():
    report = lu.contact_discovery({'eligible_company_buckets': 1, 'hm_searches': 0,
                                  'hm_found': 0, 'hm_not_found': 1}, {}, {})
    assert report[lu.SEARCHED_NO_RESULT] is None
    assert report['partition_closes'] is None


def test_mixed_search_and_contact_marginals_cannot_prove_an_intersection():
    # These counters fit both a cached contact + failed search, and a successful
    # search + an unsearched row. They must not pick either explanation.
    report = lu.contact_discovery({'eligible_company_buckets': 2, 'hm_searches': 1,
                                  'hm_found': 1, 'hm_not_found': 1}, {}, {})
    assert report['searched_and_found'] is None
    assert report[lu.SEARCHED_NO_RESULT] is None


def test_absent_counter_is_not_a_disagreement_with_a_measured_zero():
    report = lu.contact_discovery({'hm_not_found': 0}, {}, {})
    assert report['counter_disagreement']['agree'] is None


def test_two_reasons_do_not_prove_containment_or_email_failure():
    report = lu.overlaps({'unverified': 155, 'no_contact': 144})
    assert report['containments'][0]['outer_excluding_inner'] is None


def test_absence_of_contact_does_not_prove_a_search_was_made():
    result = lu.classify({'hiring_manager_not_found': 1, 'no_contact': 1})
    assert lu.SEARCHED_NO_RESULT not in result['by_category']
