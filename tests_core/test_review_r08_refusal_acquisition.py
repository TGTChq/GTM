"""R08 -- a persisted Apollo refusal withholds paid inventory acquisition while available
data keeps being processed; the retry interval permits one controlled probe reserved
atomically across workers; only a served chargeable response lifts the refusal."""

from __future__ import annotations

from tgtc_core.services import provider_state
from tgtc_core.testing.scenario import build_nine_route_scenario
from tests_core.helpers import runner, sql1, sqlall


def test_persisted_refusal_withholds_acquisition_but_processing_continues(conn, clock):
    sc = build_nine_route_scenario(clock())
    sc.apollo.credits = 0                                            # Apollo refuses every paid call
    r = runner(conn, sc, clock)
    first = r.cycle()
    assert len(first.acquisition) > 0 and sql1(conn, "SELECT count(*) FROM postings") == 10
    assert first.stages["qualify_opportunity"] == {"wait": 10}
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'apollo'") == "refusing"
    fantastic_calls = len(sc.fantastic.requests)
    # next cycle: NO paid inventory is bought while the refusal stands; identity/classification still run
    clock.advance(hours=1)
    second = r.cycle()
    assert second.acquisition == [] and second.acquisition_withheld["reason"] == "apollo_refusing"
    assert len(sc.fantastic.requests) == fantastic_calls
    assert second.stages["qualify_opportunity"] == {}                # inside the interval: no probe either
    # after the interval: exactly ONE controlled paid attempt across the whole cycle, still refused
    paid = lambda: len([q for q in sc.apollo.requests if q["path"].endswith("/people/match") or q["path"].endswith("/organizations/enrich")])
    before = paid()
    clock.advance(hours=5, seconds=1)
    third = r.cycle()
    assert third.acquisition == [] and paid() == before + 1
    assert third.stages["qualify_opportunity"].get("wait") == 10
    # recovery: Apollo serves -> the probe succeeds, refusal lifted, acquisition resumes next cycle
    sc.apollo.credits = None
    clock.advance(hours=6, seconds=1)
    fourth = r.cycle()
    assert fourth.stages["qualify_opportunity"].get("approved", 0) >= 1
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'apollo'") == "serving"
    clock.advance(hours=1)
    fifth = r.cycle()
    assert fifth.acquisition_withheld == {} and len(sc.fantastic.requests) > fantastic_calls


def test_probe_reservation_is_atomic_across_two_connections(conn, conn2, clock):
    provider_state.record_refusal(conn, "apollo", "BILLING.LIMIT.CREDITS_EXHAUSTED", now=clock())
    clock.advance(hours=6, seconds=1)
    a = provider_state.reserve_probe(conn, "apollo", retry_hours=6.0, now=clock())
    b = provider_state.reserve_probe(conn2, "apollo", retry_hours=6.0, now=clock())
    assert (a["allowed"], a["reserved"]) == (True, True)
    assert b["allowed"] is False and b["next_attempt_after"] > clock()
    # the reservation itself never lifts the refusal
    assert provider_state.acquisition_allowed(conn, "apollo")["allowed"] is False
    provider_state.record_served(conn, "apollo", now=clock())
    assert provider_state.acquisition_allowed(conn, "apollo")["allowed"] is True


def test_rate_limit_and_transient_errors_do_not_withhold_acquisition(conn, clock):
    sc = build_nine_route_scenario(clock())
    sc.apollo.fail_next = [429]
    r = runner(conn, sc, clock)
    r.cycle()
    assert provider_state.acquisition_allowed(conn, "apollo")["allowed"] is True
