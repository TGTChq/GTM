"""A company-side hold is a category, not its underlying identity diagnosis."""
import json

import config
from run_maintenance import send_safe_forensics
from tests.test_fantastic_send_safe_auto_approval import _fantastic_job


def _run(tmp_path, job):
    source = tmp_path / 'run_artifacts/r1/enrichment/batch/jobs_enriched_1.json'
    source.parent.mkdir(parents=True)
    payload = json.dumps({'jobs': [job]})
    source.write_text(payload)
    result = send_safe_forensics(tmp_path, ['r1'])
    assert source.read_text() == payload
    return result, result['runs'][0]['verified_withheld'][0]


def test_retained_company_conflict_is_followable_without_exposing_contact(tmp_path):
    job = _fantastic_job(
        _final_state='NEEDS_CHECK', _outbound_company_hold=True,
        outbound_company_confidence='low',
        _validation_version=config.VALIDATION_VERSION,
        _outbound_company_evidence={
            'identity_keys': ['linkedin:globex', 'domain:acme.com'],
            'identity_conflict': True,
            'reasons': ['linkedin_slug_domain_disagreement'],
            'bridging_brand': '',
            'candidates': [{'source': 'organization', 'raw': 'Acme',
                            'identity_matches': {'linkedin': '', 'domain': 'exact'},
                            'contact_email': 'do-not-copy@example.test'}],
            'arbitrary_payload': {'secret': 'do-not-copy'},
        })
    result, entry = _run(tmp_path, job)
    assert result['runs'][0]['reasons'] == {'outbound_company_held_for_review': 1}
    assert entry['artifact'] == 'batch/jobs_enriched_1.json'
    assert entry['job_index'] == 0
    facts = entry['company_facts']
    assert facts['evidence_status'] == 'retained'
    assert facts['resolver_evidence']['reasons'] == ['linkedin_slug_domain_disagreement']
    assert facts['resolver_evidence']['identity_keys'] == ['linkedin:globex', 'domain:acme.com']
    assert 'do-not-copy' not in json.dumps(result)
    assert job['hiring_manager_email'] not in json.dumps(result)
    assert 'Validation Fingerprint' not in json.dumps(result)


def test_missing_company_evidence_does_not_inherit_a_historical_reason(tmp_path):
    job = _fantastic_job(_final_state='NEEDS_CHECK', _outbound_company_hold=True,
                         outbound_company_confidence='low')
    _, entry = _run(tmp_path, job)
    assert entry['reason'] == 'outbound_company_held_for_review'
    assert entry['company_facts']['evidence_status'] == 'unavailable'
    assert entry['company_facts']['resolver_evidence'] is None
    assert 'linkedin_slug_domain_disagreement' not in json.dumps(entry)


def test_rebuilt_signature_and_default_version_are_not_historical_proof(tmp_path):
    job = _fantastic_job(_final_state='NEEDS_CHECK', _outbound_company_hold=True,
                         outbound_company_confidence='low')
    job.pop('_validation_version', None)
    result, entry = _run(tmp_path, job)
    assert entry['reason'] == 'outbound_company_held_for_review'
    assert entry['has_fingerprint'] is True
    assert entry['fingerprint_source'] == 'rebuilt_by_current_mapper'
    assert entry['validation_version_source'] == 'current_configuration_default'
    assert result['historical_fingerprint_verified'] is False
    assert entry['checks_after_first_failure'] == 'not_evaluated'
