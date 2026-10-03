"""A rate limit must stop Apollo for the run, not be rediscovered per opportunity.

Measured on budget `prod-scheduled-20261002`: of 7,407 Apollo requests, **1,351
were `people_search` marked `refused`** and they bought nothing. `refused` maps
from `CREDIT_EXHAUSTED`, `UNAUTHORIZED` and `RATE_LIMITED`, and no credits were
spent, so they were rate limits. `budget_status` counts requests as `count(*)`
over all reservations regardless of status, so each one consumed a request from
the day's 10,000 allowance — 18% of it, for nothing.

The cause: `_handle_global` raised for the opportunity in hand on RATE_LIMITED but
recorded NOTHING, unlike credit exhaustion and 401 which both call
`provider_state.record_refusal`. So the next opportunity's `_guard_provider` saw
`serving`, searched, and hit the same limit again.

`Outcome.RATE_LIMITED.global_stop` was already True and was never read anywhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tgtc_core.providers.apollo import ApolloResult, Outcome
from tgtc_core.services.opportunity import ProviderWait


def _service(conn, clock):
    from tests_core.helpers import opportunity_service
    from tests_core.seed import apollo_for
    return opportunity_service(conn, apollo_for("acme.com", "Acme"), clock)


def test_the_latch_is_clear_before_anything_happens(conn, clock):
    svc = _service(conn, clock)
    assert svc._apollo_throttled_until is None
    svc._guard_provider(chargeable=False)          # does not raise


def test_a_rate_limit_latches_the_provider_off_for_the_retry_after(conn, clock):
    svc = _service(conn, clock)
    result = ApolloResult(Outcome.RATE_LIMITED, status=429, retry_after=120.0)
    with pytest.raises(ProviderWait):
        svc._handle_global(result, chargeable=False)
    assert svc._apollo_throttled_until == clock() + timedelta(seconds=120)


def test_a_second_opportunity_does_not_re_discover_the_limit(conn, clock):
    """The 1,351 calls, in one assertion: the next attempt must be refused
    locally, without a provider call and without burning a request."""
    svc = _service(conn, clock)
    with pytest.raises(ProviderWait):
        svc._handle_global(ApolloResult(Outcome.RATE_LIMITED, status=429, retry_after=120.0),
                           chargeable=False)
    for chargeable in (False, True):
        with pytest.raises(ProviderWait, match="apollo_rate_limited_this_run"):
            svc._guard_provider(chargeable=chargeable)


def test_the_latch_releases_exactly_when_the_provider_said(conn, clock):
    svc = _service(conn, clock)
    with pytest.raises(ProviderWait):
        svc._handle_global(ApolloResult(Outcome.RATE_LIMITED, status=429, retry_after=60.0),
                           chargeable=False)
    clock.advance(seconds=61)
    svc._guard_provider(chargeable=False)          # released, no raise
    assert svc._apollo_throttled_until is None


def test_the_wait_is_bounded_and_never_becomes_an_outage(conn, clock):
    """A provider asking for a day must not take Apollo out for a day; and a
    60-second throttle must not become the six-hour block record_refusal would
    impose."""
    svc = _service(conn, clock)
    with pytest.raises(ProviderWait):
        svc._handle_global(ApolloResult(Outcome.RATE_LIMITED, status=429, retry_after=86400.0),
                           chargeable=False)
    assert svc._apollo_throttled_until == clock() + timedelta(seconds=900)
    assert svc._apollo_throttled_until < clock() + timedelta(hours=1)


def test_credit_exhaustion_and_unauthorized_still_record_provider_state(conn, clock):
    """The latch is additional, not a replacement: those two must still persist,
    because they outlive one run."""
    from tgtc_core.services import provider_state

    svc = _service(conn, clock)
    with pytest.raises(ProviderWait):
        svc._handle_global(ApolloResult(Outcome.CREDIT_EXHAUSTED, status=200,
                                        error_code="credit_exhausted"), chargeable=True)
    assert provider_state.load(conn, "apollo")["state"] in ("refusing", "unauthorized")


def test_a_served_search_leaves_the_latch_alone(conn, clock):
    svc = _service(conn, clock)
    svc._handle_global(ApolloResult(Outcome.SERVED, status=200), chargeable=False)
    assert svc._apollo_throttled_until is None
    svc._guard_provider(chargeable=False)
