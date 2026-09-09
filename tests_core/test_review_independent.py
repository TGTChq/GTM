"""Independent contract regressions originally run against TGTChq/GTM a090a2b.
Storage fixtures adapted for checkpoint 56f333e; behavior assertions unchanged.

Run from the repository root:
  PYTHONPATH=. python -m pytest /path/to/test_review_portable.py -q --tb=short

No network and no PostgreSQL. Domain/provider functions run unchanged. Service
unit tests replace storage with explicit fixtures; they do NOT certify database
transactions, persistence, leases, or end-to-end delivery. Assertions describe
the intended contract, so defects FAIL on the reviewed commit.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from tgtc_core.domain.approval import ApprovalRefusal, ApprovedLead, build_approved_lead
from tgtc_core.domain.gates import evaluate_contact
from tgtc_core.domain.inference import InferenceResponse
from tgtc_core.policy.campaigns import FUNCTION_KEYS, buyer_titles
from tgtc_core.providers.airtable import AirtableClient
from tgtc_core.providers.apollo import ApolloClient
from tgtc_core.providers.http import Response, TransportError, TransportTimeout
from tgtc_core.providers.fantastic import FantasticClient, ENDPOINT_ATS, ENDPOINT_JOB_BOARDS
from tgtc_core.services.acquisition import AcquisitionService, SOURCE_ATS, SOURCE_JOB_BOARDS, posting_content_hash
from tgtc_core.services.classification_service import classify_one
from tgtc_core.services.opportunity import OpportunityService, QualifyOutcome
from tgtc_core.testing.fakes import FakeApollo, make_person
from tgtc_core.testing.scenario import campaign_env
from tests_core.test_approval_gate import _inputs

NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.connection.statements.append((sql, params))
        self.rows = self.connection.answer(sql, params)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows


class FixtureConnection:
    """Explicit query results only; never pretends to implement PostgreSQL."""
    def __init__(self, answer):
        self.answer = answer
        self.statements = []

    def cursor(self):
        return Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class DocumentedSearchApollo(FakeApollo):
    """Synthetic identities using Apollo's documented sparse search shape.

    Source: https://docs.apollo.io/docs/retrieve-linkedin-profiles-for-prospects
    Search has id/title/obfuscated surname/availability flags/org name. LinkedIn
    and complete person details arrive after enrichment. No copied real people.
    """
    def request(self, method, url, **kwargs):
        result = super().request(method, url, **kwargs)
        if url.endswith('/mixed_people/api_search') and result.status == 200:
            body = result.json()
            people = [{
                'id': p['id'], 'first_name': p['first_name'],
                'last_name_obfuscated': 'Ex***e', 'title': p['title'],
                'has_email': True, 'organization': {'name': p['organization']['name'],
                                                    'has_employee_count': True},
            } for p in body['people']]
            return Response(200, text=json.dumps({'total_entries': len(people), 'people': people}))
        return result


class OpportunityWithFixtureStorage(OpportunityService):
    """Keep process/search/rank/approval gates; replace only persistence/state I/O."""
    def __init__(self, fake, function_key):
        def answer(sql, params):
            if 'FROM suppressions' in sql:
                return []
            if 'count(*) AS n FROM candidate_attempts' in sql:
                return [{'n': 0}]
            if 'SELECT * FROM people' in sql:
                return []
            raise AssertionError('Unexpected fixture query: ' + sql)
        super().__init__(FixtureConnection(answer),
                         ApolloClient(fake, base_url='https://api.apollo.io/api/v1', api_key='sim'),
                         campaign_env=campaign_env(), signing_key='test-key', now=lambda: NOW)
        data = _inputs()
        self.opp = {'id': 1, 'state': 'open', 'function_key': function_key, 'employer_id': 1}
        self.emp = data['employer']
        self.emp['apollo_org_id'] = 'org-acme'
        self.posting = data['posting']
        self.classification = data['classification']
        self.classification['compatible_functions'] = [function_key]
        self.person = None
        self.attempts = []
        self.approvals = []

    def _load(self, opportunity_id):
        return self.opp, self.emp, self.posting, self.classification

    def _ensure_employer_facts(self, opp, emp):
        return emp  # complete employer facts are an explicit precondition

    def _alias_domains(self, employer_id):
        return set()

    def _excluded_refs(self, opportunity_id):
        return set(), set(), set()

    def _guard_provider(self, **kwargs):
        pass  # this unit scenario starts with a healthy provider

    def _intent(self, *args, **kwargs):
        return 1

    def _finish(self, *args, **kwargs):
        pass

    def _handle_global(self, result, **kwargs):
        assert result.served, result

    def _record_attempt(self, *args, **kwargs):
        self.attempts.append((args, kwargs))

    def _upsert_person(self, enriched, emp, **kwargs):
        self.person = dict(enriched, id=1, apollo_person_id=enriched['id'])
        return 1

    def _person_row(self, person_id):
        return dict(self.person)

    def _commit_approval(self, opp, person_id, approved):
        self.approvals.append(approved)
        return 1

    def _close(self, opportunity_id, reason):
        return QualifyOutcome(opportunity_id, 'closed', reason)


@pytest.mark.parametrize('function_key', sorted(FUNCTION_KEYS))
@pytest.mark.parametrize('documented_shape', [False, True], ids=['rich_fake_control', 'documented_sparse_search'])
def test_enrichable_search_id_can_reach_full_approval(function_key, documented_shape):
    title = buyer_titles(function_key, founder_allowed=False)[0]
    person = make_person(id='synthetic-person', first='Pat', last='Example', title=title,
                         org_name='Acme', org_domain='acme.com', email='pat@acme.com', email_status='verified')
    fake_type = DocumentedSearchApollo if documented_shape else FakeApollo
    fake = fake_type(people_by_domain={'acme.com': [person]})
    svc = OpportunityWithFixtureStorage(fake, function_key)
    result = svc.process(1)
    assert result.outcome == 'approved', {
        'outcome': result.outcome, 'reason': result.reason,
        'paid_calls': fake.served_paid, 'decisions': svc.attempts,
    }
    assert len(svc.approvals) == 1


class AcceptedThenLost:
    def __init__(self, failure):
        self.failure = failure
        self.records = []

    def request(self, method, url, **kwargs):
        assert method == 'POST'
        self.records.extend(copy.deepcopy(kwargs['json_body']['records']))
        if len(self.records) == 1:
            if self.failure == 'connection_reset':
                raise TransportError('ConnectionError: peer reset after create')
            if self.failure == 'timeout':
                raise TransportTimeout('read timeout after create')
            return Response(500, text='{"error":"response generation failed after create"}')
        return Response(200, text=json.dumps({'records': [{'id': 'rec-second', **self.records[-1]}]}))


@pytest.mark.parametrize('failure', ['timeout', 'connection_reset', 'http_500'])
def test_uncertain_create_never_blindly_reposts_before_reconciliation(failure):
    transport = AcceptedThenLost(failure)
    client = AirtableClient(transport, base_url='https://api.airtable.com/v0', token='sim',
                            base_id='appSIM', table='Leads', sleep=lambda _: None)
    result = client.create_records([{'Lead Key': 'acme.com|pat@acme.com|product', 'Status': 'Approved'}])
    assert len(transport.records) == 1, {'created': len(transport.records), 'client_reported_ok': result.ok}
    assert result.uncertain


def test_explicitly_expired_posting_cannot_be_approved():
    inputs = _inputs()
    inputs['posting']['date_valid_through'] = inputs['now'] - timedelta(hours=1)
    decision = build_approved_lead(**inputs)
    assert isinstance(decision, ApprovalRefusal), 'Expired posting produced Approved'


@pytest.mark.parametrize('field,before,after', [
    ('org_linkedin_recruitment_agency_derived', False, True),
    ('countries_derived', ['US'], ['GB']),
    ('ai_employment_type', 'FULL_TIME', 'PART_TIME'),
    ('org_linkedin_headcount', 120, 5000),
])
def test_changed_eligibility_facts_invalidate_posting_evidence(field, before, after):
    row = {'id': 'same-job', 'title': 'Specialist', 'description_text': 'unchanged responsibilities',
           'organization': 'Acme', 'organization_url': 'https://acme.com', field: before}
    changed = dict(row, **{field: after})
    assert posting_content_hash(row) != posting_content_hash(changed), field


def test_transient_inference_failure_does_not_close_posting():
    posting = {
        'id': 1, 'state': 'identity_resolved', 'employer_id': 1, 'close_reason': None, 'org_json': {}, 'structured_json': {},
        'description_text': 'You will turn requests from colleagues into clear written plans, keep work moving between teams, '
                            'and prepare concise weekly summaries. This is a full-time role supporting our United States business.',
        'title': '', 'employment_type': 'FULL_TIME', 'location_type': 'remote', 'countries': ['US'],
        'location_text': 'United States', 'employer_name': 'Acme', 'content_hash': 'test-hash',
    }
    def answer(sql, params):
        if sql.startswith('SELECT * FROM postings'):
            return [posting]
        if 'INSERT INTO classifications' in sql:
            return [{'id': 1}]
        if sql.startswith('UPDATE postings'):
            return []
        raise AssertionError('Unexpected fixture query: ' + sql)
    conn = FixtureConnection(answer)
    port = SimpleNamespace(model_version='test-model', classify=lambda request:
                           InferenceResponse(available=False, unavailable_reason='rate_limited_429', unavailable_kind='transient'))
    result = classify_one(conn, 1, inference=port, now=NOW)
    assert result.outcome != 'closed', {'outcome': result.outcome, 'reason': result.reason}


def test_cached_person_does_not_bypass_territory_gate():
    person = {'id': 1, 'apollo_person_id': 'cached-person', 'first_name': 'Pat', 'last_name': 'Example',
              'title': 'VP Product EMEA', 'linkedin_url': 'https://linkedin.com/in/synthetic-pat',
              'email': 'pat@acme.com', 'email_status': 'verified', 'email_verified_at': NOW,
              'organization_name': 'Acme', 'organization_domain': 'acme.com', 'employer_id': 1}
    full_check = evaluate_contact(person=person, employer_name='Acme', employer_domains={'acme.com'},
                                  buyer_titles=['VP Product'], founder_allowed=False)
    assert full_check.reason == 'contact:territory_mismatch'
    def answer(sql, params):
        if 'INSERT INTO candidate_attempts' in sql:
            return []
        assert sql.startswith('SELECT * FROM people'), sql
        return [person]
    svc = OpportunityService(FixtureConnection(answer), None, campaign_env=campaign_env(),
                             signing_key='test-key', now=lambda: NOW)
    reused = svc._reusable_person({'id': 1, 'canonical_name': 'Acme'}, ['VP Product'], False, set(), set(), {'acme.com'}, 1, 1)
    assert reused is None, {'prior_rejection': full_check.reason, 'reused_contact_gate': reused.get('contact_gate_passed')}


@pytest.mark.parametrize('source,expected_endpoint', [
    (SOURCE_JOB_BOARDS, ENDPOINT_JOB_BOARDS), (SOURCE_ATS, ENDPOINT_ATS),
])
def test_partition_uses_its_actual_provider_feed(source, expected_endpoint, monkeypatch):
    monkeypatch.setattr("tgtc_core.services.provider_state.reserve_probe", lambda *a, **kw: {"allowed": True})
    class CapturedRequest(Exception):
        pass
    class CaptureTransport:
        path = None
        def request(self, method, url, **kwargs):
            self.path = urlsplit(url).path
            raise CapturedRequest()  # stop at HTTP boundary, before receipt writes
    def answer(sql, params):
        assert 'INSERT INTO request_attempts' in sql, sql
        return [{'id': 1}]
    transport = CaptureTransport()
    client = FantasticClient(transport, base_url='https://data.fantastic.jobs', api_key='sim')
    svc = AcquisitionService(FixtureConnection(answer), client, now=lambda: NOW,
                             page_limit=100, time_frame='7d', fresh_window_minutes=60,
                             fresh_lag_minutes=180, backfill_window_hours=24,
                             max_pages_per_partition=1, min_jobs_quota_remaining=0,
                             min_requests_quota_remaining=0)
    svc._load_partition = lambda pid: {
        'id': pid, 'lease_token': 'fixture-token', 'lease_expires_at': NOW+timedelta(hours=1), 'source': source, 'lane': 'fresh', 'state': 'open', 'next_offset': 0,
        'window_start': NOW-timedelta(hours=5), 'window_end': NOW-timedelta(hours=4),
    }
    svc.claim_partition = lambda pid: 'fixture-token'
    svc.release_partition = lambda pid, token: None
    svc._latest_quota = lambda source: {}
    svc._last_receipt_fingerprint = lambda pid: ('', None)
    with pytest.raises(CapturedRequest):
        svc.run_partition(1, max_pages=1)
    assert transport.path == expected_endpoint, {'partition_source': source, 'requested': transport.path}


def test_default_runner_schedules_both_fantastic_feeds(monkeypatch):
    from tgtc_core.runner import Runner
    from tests_core.helpers import settings
    planned = []
    class CapturePartitions:
        sources = (SOURCE_JOB_BOARDS, SOURCE_ATS)
        def recover_partitions(self, source):
            return {}
        def __init__(self, *args, **kwargs):
            pass
        def plan_fresh_partitions(self, source, **kwargs):
            planned.append(source)
        def plan_backfill_partition(self, source):
            pass
        def open_partitions(self, *args, **kwargs):
            return []
    monkeypatch.setattr('tgtc_core.runner.AcquisitionService', CapturePartitions)
    runner = Runner(None, settings(), fantastic_transport=SimpleNamespace(),
                    apollo_transport=None, airtable_transport=None, instantly_transport=None)
    runner.acquisition_gate = lambda: {'allowed': True}
    runner.acquire()
    assert set(planned) == {SOURCE_JOB_BOARDS, SOURCE_ATS}, planned


def test_unusable_domain_results_do_not_hide_a_valid_org_id_candidate():
    class BroadSearch(FakeApollo):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith("/mixed_people/api_search") and "q_organization_domains_list[]" in dict(kwargs.get("params") or {}):
                return Response(200, text=json.dumps({"people": self.people_by_domain["acme.com"]}))
            return response
    fake = BroadSearch()
    fake.people_by_domain["acme.com"] = [make_person(
        id="wrong-role", first="Wrong", last="Role", title="Software Engineer",
        org_name="Acme", org_domain="acme.com", email="wrong@acme.com", email_status="verified")]
    fake.people_by_org_id["org-acme"] = [make_person(
        id="right-role", first="Right", last="Buyer", title="VP Product",
        org_name="Acme", org_domain="acme.com", email="right@acme.com", email_status="verified")]
    svc = OpportunityWithFixtureStorage(fake, "product")
    out = svc.process(1)
    assert out.outcome == "approved", out
    assert any("organization_ids[]" in r["params"] for r in fake.requests)
    assert svc.person["apollo_person_id"] == "right-role"


@pytest.mark.parametrize("scenario, reason", [
    ("empty", "no_candidates_found"),
    ("unusable", "no_usable_candidates_in_search"),
    ("judged", "no_unjudged_candidates_remaining"),
])
def test_empty_search_outcome_distinguishes_precheck_rejection_from_paid_history(scenario, reason):
    """Same production search/process code, explicit storage fixture. An unsuitable
    search result is not proof that we already paid to evaluate that person."""
    person = make_person(
        id="search-person", first="Search", last="Person",
        title="Software Engineer" if scenario == "unusable" else "VP Product",
        org_name="Acme", org_domain="acme.com", email="search@acme.com", email_status="verified")

    class BroadSearch(FakeApollo):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith("/mixed_people/api_search"):
                from tgtc_core.testing.fakes import search_projection
                people = [] if scenario == "empty" else [search_projection(person)]
                return Response(200, text=json.dumps({"people": people}))
            return response

    fake = BroadSearch()
    svc = OpportunityWithFixtureStorage(fake, "product")
    if scenario == "judged":
        svc._excluded_refs = lambda oid: ({"pid:search-person"}, set(), set())
    out = svc.process(1)
    assert (out.outcome, out.reason) == ("closed", reason)
    assert fake.served_paid == 0 and svc.approvals == []
    assert not any(r["path"].endswith("/people/match") for r in fake.requests)
    if scenario == "unusable":
        assert len(svc.attempts) == 1
        assert svc.attempts[0][0][1:4] == ("pid:search-person", "gate", "skipped_pre_enrichment")
    else:
        assert svc.attempts == []
