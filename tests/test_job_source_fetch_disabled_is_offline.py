"""Regression tests: ``resolve(fetch=False)`` must make ZERO network calls.

Defect: ``JobSourceResolver.resolve`` called
``_corroborate_independent_publishers`` unconditionally at the end of the
resolution ladder. That helper calls ``self._fetch`` on every
aggregator/other/indeed/linkedin candidate URL, so a caller that explicitly
asked for ``fetch=False`` still issued live HTTP. Because
``adapters_real.RealEnrichmentStage`` runs
``run_precontact_qualification(fetch_sources=False)``, the orchestrator's own
"offline" enrichment path was not network-free either.

These tests pin:
  * fetch=False performs no fetch at all;
  * fetch=True still reaches corroboration (live behaviour NOT weakened);
  * the JobGate path used by qualification is network-free with fetch=False;
  * a socket-level guard sees no outbound connection on the fetch=False path.
"""
from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

import config
from job_gate import JobGate
from job_source_resolver import JobSourceResolver


def _aggregator_job() -> dict:
    """A record whose only URLs are aggregator/linkedin -- exactly the shape that
    used to trigger the unconditional corroboration fetch."""
    return {
        "job_id": "fantastic_1",
        "job_title": "Account Executive",
        "employer_name": "Acme Analytics",
        "employer_website": "acmeanalytics.com",
        "job_description": "We are hiring an Account Executive. " * 40,
        "job_apply_link": "https://www.linkedin.com/jobs/view/account-executive-at-acme-1",
        "canonical_source_url": "https://www.linkedin.com/jobs/view/account-executive-at-acme-1",
        "apply_options": [{
            "publisher": "linkedin",
            "apply_link": "https://www.linkedin.com/jobs/view/account-executive-at-acme-1",
            "is_direct": False,
        }],
        "_acquisition_source": "fantastic_jobs_linkedin",
        "_provider_record_structured": True,
    }


class _CountingResolver(JobSourceResolver):
    """Counts every call to the HTTP primitive without performing one."""

    def __init__(self):
        super().__init__()
        self.fetch_calls: list[str] = []

    def _fetch(self, url, *args, **kwargs):
        self.fetch_calls.append(url)
        return {"status_code": None, "url": url, "final_url": url,
                "text": "", "error": "stubbed"}


def test_resolve_with_fetch_false_makes_zero_fetch_calls():
    resolver = _CountingResolver()
    resolver.resolve(_aggregator_job(), fetch=False)
    assert resolver.fetch_calls == [], (
        f"fetch=False must not fetch; got {len(resolver.fetch_calls)} call(s): "
        f"{resolver.fetch_calls[:5]}"
    )


def test_resolve_with_fetch_false_still_returns_a_decision():
    """Skipping corroboration must not crash or change the contract."""
    resolver = _CountingResolver()
    resolved = resolver.resolve(_aggregator_job(), fetch=False)
    assert resolved is not None
    assert resolved.state, "resolve() must still yield a state with fetch=False"


def test_resolve_with_fetch_true_still_attempts_corroboration():
    """Live behaviour must NOT be weakened: with fetch=True the corroboration
    helper is still invoked."""
    resolver = _CountingResolver()
    with patch.object(
        JobSourceResolver,
        "_corroborate_independent_publishers",
        autospec=True,
        return_value=None,
    ) as spy:
        resolver.resolve(_aggregator_job(), fetch=True)
    assert spy.called, "fetch=True must still reach independent-publisher corroboration"


def test_corroboration_is_not_invoked_when_fetch_is_false():
    resolver = _CountingResolver()
    with patch.object(
        JobSourceResolver,
        "_corroborate_independent_publishers",
        autospec=True,
        return_value=None,
    ) as spy:
        resolver.resolve(_aggregator_job(), fetch=False)
    assert not spy.called, "fetch=False must skip corroboration entirely"


def test_job_gate_with_fetch_false_is_network_free():
    """JobGate is what qualification actually calls; it must be offline too."""
    resolver = _CountingResolver()
    gate = JobGate(resolver=resolver)
    decision = gate.evaluate(_aggregator_job(), fetch=False)
    assert decision is not None
    assert resolver.fetch_calls == [], (
        "JobGate.evaluate(fetch=False) issued HTTP via the resolver"
    )


def test_socket_level_guard_sees_no_outbound_connection_with_fetch_false():
    """The definitive check: block the socket layer and resolve with fetch=False.

    A real (unstubbed) resolver is used so nothing but the fix prevents the call.
    """
    attempts: list = []
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _blocked_create_connection(address, *a, **k):
        attempts.append(address)
        raise AssertionError(f"outbound connection attempted to {address}")

    def _blocked_getaddrinfo(host, port, *a, **k):
        attempts.append((host, port))
        raise AssertionError(f"DNS resolution attempted for {host}:{port}")

    socket.create_connection = _blocked_create_connection
    socket.getaddrinfo = _blocked_getaddrinfo
    try:
        JobSourceResolver().resolve(_aggregator_job(), fetch=False)
    finally:
        socket.create_connection = real_create_connection
        socket.getaddrinfo = real_getaddrinfo

    assert attempts == [], f"fetch=False attempted network I/O: {attempts}"


@pytest.mark.parametrize("allow_corroborated", [True, False])
def test_fetch_false_is_offline_regardless_of_corroboration_flag(allow_corroborated):
    """JOB_SOURCE_ALLOW_CORROBORATED is a live-behaviour switch; it must not be
    the thing that keeps an offline run offline."""
    resolver = _CountingResolver()
    with patch.object(config, "JOB_SOURCE_ALLOW_CORROBORATED", allow_corroborated):
        resolver.resolve(_aggregator_job(), fetch=False)
    assert resolver.fetch_calls == []
