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


def test_a_long_retry_after_is_honoured_in_full_and_not_shortened(conn, clock):
    """The provider's wait is never cut short. An earlier version capped it at 900s,
    which called Apollo again BEFORE it said to -- the behaviour that burned the
    1,351 requests in the first place."""
    svc = _service(conn, clock)
    with pytest.raises(ProviderWait):
        svc._handle_global(ApolloResult(Outcome.RATE_LIMITED, status=429, retry_after=86400.0),
                           chargeable=False)
    assert svc._apollo_throttled_until == clock() + timedelta(seconds=86400)
    # 900s was the old cap; prove we do NOT come back then.
    clock.advance(seconds=901)
    with pytest.raises(ProviderWait, match="apollo_rate_limited_this_run"):
        svc._guard_provider(chargeable=False)


def test_a_long_wait_stays_in_this_run_and_is_never_persisted(conn, clock):
    """Honouring a long wait must not become a six-hour cross-run outage the way
    record_refusal would: the latch lives on the service, not in provider_state."""
    from tgtc_core.services import provider_state

    svc = _service(conn, clock)
    before = provider_state.load(conn, "apollo")
    with pytest.raises(ProviderWait):
        svc._handle_global(ApolloResult(Outcome.RATE_LIMITED, status=429, retry_after=86400.0),
                           chargeable=False)
    assert provider_state.load(conn, "apollo") == before
    fresh = _service(conn, clock)
    assert fresh._apollo_throttled_until is None
    fresh._guard_provider(chargeable=False)          # a new run is not throttled


def test_a_missing_or_absurd_retry_after_falls_back_without_inventing_an_outage(conn, clock):
    svc = _service(conn, clock)
    with pytest.raises(ProviderWait):
        svc._handle_global(ApolloResult(Outcome.RATE_LIMITED, status=429), chargeable=False)
    assert svc._apollo_throttled_until == clock() + timedelta(seconds=60)
    svc._apollo_throttled_until = None
    with pytest.raises(ProviderWait):
        svc._handle_global(ApolloResult(Outcome.RATE_LIMITED, status=429, retry_after=-5.0),
                           chargeable=False)
    # Never in the past: a negative value must not read as "retry immediately".
    assert svc._apollo_throttled_until >= clock()


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
