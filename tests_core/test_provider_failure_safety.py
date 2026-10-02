"""Offline boundary tests: errors never leak bodies or become business absence."""

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tgtc_core.providers.apollo import ApolloClient, Outcome, classify
from tgtc_core.providers.http import Response, TransportError
from tgtc_core.runner import Runner
from tgtc_core.testing.fakes import CREDIT_BODY


def test_apollo_errors_keep_only_safe_allowlisted_diagnostics():
    secret = 'quoted-secret with spaces'
    body = json.loads(json.dumps(CREDIT_BODY))
    body['error'] += ' "api_key": "' + secret + '"'
    body['error_details']['message'] = secret
    body['error_details']['context'].update({
        'api_key': {'value': secret}, 'huge': {'value': 'x' * 10000},
        'credit_type': {'value': secret}, 'next_billing_date': {'value': secret},
    })
    result = classify(Response(status=422, text=json.dumps(body)))
    assert result.outcome is Outcome.CREDIT_EXHAUSTED
    assert result.error_code == 'BILLING.LIMIT.CREDITS_EXHAUSTED'
    assert result.context == {'credit_balance': 0}
    assert secret not in json.dumps(result.summary())
    assert len(json.dumps(result.summary())) < 300


def test_apollo_transport_error_never_echoes_exception():
    class Broken:
        def request(self, *args, **kwargs):
            raise TransportError('https://private/?api_key=secret')

    result = ApolloClient(Broken(), base_url='https://offline', api_key='unused').search_people(titles=[])
    assert result.outcome is Outcome.SERVER and result.message == 'transport_error'
    assert 'secret' not in json.dumps(result.summary())


@pytest.mark.parametrize('body', ['not JSON', '[]', '{}', '{"people":null}', '{"people":"bad"}', '{"people":["bad"]}'])
def test_search_invalid_success_body_is_technical_failure(body):
    transport = SimpleNamespace(request=lambda *a, **kw: Response(status=200, text=body))
    result = ApolloClient(transport, base_url='https://offline', api_key='unused').search_people(titles=[])
    assert result.outcome is Outcome.SERVER and result.error_code == 'invalid_response'


def test_search_valid_empty_list_remains_business_absence():
    transport = SimpleNamespace(request=lambda *a, **kw: Response(status=200, text='{"people":[]}'))
    result = ApolloClient(transport, base_url='https://offline', api_key='unused').search_people(titles=[])
    assert result.served and result.data == {'people': []}


def test_people_search_requests_full_page_and_verified_email_profiles():
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs["params"])
        return Response(status=200, text='{"people":[]}')

    client = ApolloClient(SimpleNamespace(request=request), base_url='https://offline', api_key='unused')
    assert client.search_people(titles=['VP Operations'], email_statuses=['verified']).served
    assert ('per_page', '100') in calls[0]
    assert ('contact_email_status[]', 'verified') in calls[0]


@pytest.mark.parametrize('value,expected', [('nan', None), ('inf', None), ('-1', None), ('1e50', 900.0), ('30', 30.0)])
def test_retry_after_is_finite_nonnegative_and_bounded(value, expected):
    result = classify(Response(status=429, text='{}', headers={'Retry-After': value}))
    assert result.retry_after == expected


@pytest.mark.parametrize('path,key', [('/organizations/enrich', 'organization'), ('/people/match', 'person')])
@pytest.mark.parametrize('value', [None, {}])
def test_documented_explicit_no_match_shape_remains_served(path, key, value):
    transport = SimpleNamespace(request=lambda *a, **kw: Response(status=200, text=json.dumps({key: value})))
    result = ApolloClient(transport, base_url='https://offline', api_key='unused')._call('POST', path, {})
    assert result.served


def _runner(monkeypatch):
    from tgtc_core import runner as module

    r = object.__new__(Runner)
    r.conn = SimpleNamespace(rollback=lambda: None)
    r.s = SimpleNamespace(lease_seconds=60, inference_retry_minutes=15, retry_backoff_seconds=30)
    r.now = lambda: datetime(2026, 9, 16, tzinfo=timezone.utc)
    r.inference = None
    r.apollo = None
    r._log = lambda *a, **kw: None
    items = iter([SimpleNamespace(id=123, subject_id=456), None])
    r.scheduler = SimpleNamespace(claim=lambda *a, **kw: next(items))
    monkeypatch.setattr(module, 'transaction', lambda *a, **kw: nullcontext())
    for name in ('wait', 'close', 'complete'):
        monkeypatch.setattr(module.work_queue, name, lambda *a, **kw: None)
    return r, module


@pytest.mark.parametrize('reason,technical', [
    ('inference_transient:inference_error:RateLimitError', True),
    ('inference_config:inference_error:BadRequestError', True),
    ('inference_transient:spend_budget_exhausted', False),
])
def test_runner_counts_technical_inference_failures_but_not_budget(monkeypatch, reason, technical):
    r, module = _runner(monkeypatch)
    monkeypatch.setattr(module, 'classify_one', lambda *a, **kw: SimpleNamespace(outcome='wait', reason=reason, retry_after=None))
    counts = r.work('classify')
    assert counts.get('technical_failure', 0) == int(technical)
    assert counts.get('budget_exhausted', 0) == int(not technical)


def test_runner_reports_missing_apollo_as_technical_failure(monkeypatch):
    r, _ = _runner(monkeypatch)
    assert r.work('qualify_opportunity') == {'wait': 1, 'technical_failure': 1}


def test_runner_exception_logs_and_retry_reason_do_not_echo_secrets(monkeypatch, caplog):
    r, module = _runner(monkeypatch)
    errors = []

    def fail(*args, **kwargs):
        raise RuntimeError('secret-credential and private database record')

    monkeypatch.setattr(module, 'classify_one', fail)
    monkeypatch.setattr(module.work_queue, 'retry', lambda conn, item, error, **kw: errors.append(error))
    assert r.work('classify') == {'error_retry': 1, 'technical_failure': 1}
    assert errors == ['RuntimeError']
    assert 'secret-credential' not in caplog.text and 'private database' not in caplog.text
