"""The daily controller: 1,000 fresh Instantly creations is a MINIMUM, not a ceiling.

A small simulated world stands in for the database and providers so the ORDER and
the GUARDS are proven directly: backlog first, small blocks only while below
target, never a purchase behind a guard, delivery of everything produced.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import tgtc_core.daily as daily
from tgtc_core.daily import DailyController, YieldEstimate
from tgtc_core.domain.acquisition_query import DISCOVERY_PROFILE, PRIORITY_PROFILE, balanced_slot
from tgtc_core.runner import CycleReport, Runner


class World:
    """Backlog to deliver, retry work available now, and a market that turns each
    bought record into ``yield_per_record`` fresh leads once processed."""

    def __init__(self, *, backlog=0, retry_leads=0, yield_per_record=0.263, credits_per_lead=1.48,
                 fantastic_limit=4000, apollo_limit=1600, apollo_refusing=False, market=10 ** 9):
        self.backlog_pending, self.backlog_created = backlog, 0
        self.retry_pending, self.fresh_retries = retry_leads, 0
        self.fresh_new, self.new_pending = 0, 0.0
        self.records, self.apollo = 0.0, 0.0
        self.ypr, self.cpl = yield_per_record, credits_per_lead
        self.fantastic_limit, self.apollo_limit = fantastic_limit, apollo_limit
        self.apollo_refusing, self.market = apollo_refusing, market
        self.calls = []
        self.unprocessed = 0


class FakeRunner:
    def __init__(self, world: World, *, instantly=True, airtable=True):
        self.w = world
        self.run_id = "run-1"
        self.s = SimpleNamespace(campaign_env={"INSTANTLY_CAMPAIGN_OPS": "c1"}, fantastic_page_limit=100)
        self.instantly = object() if instantly else None
        self.airtable = object() if airtable else None
        self.conn = None
        self.logs = []

    def now(self):
        from datetime import datetime, timezone
        return datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)

    def _log(self, stage, event, details=None):
        self.logs.append((stage, event, details))

    def acquisition_gate(self):
        return {"allowed": not self.w.apollo_refusing, "state": "refusing" if self.w.apollo_refusing else "serving"}

    def cycle(self, *, acquire, deliver, max_items, delivery_channels):
        w = self.w
        w.calls.append(("cycle", acquire))
        assert acquire is False, "the daily controller never buys inside a cycle"
        moved = 0
        if w.unprocessed:
            moved += w.unprocessed
            w.unprocessed = 0
        if w.backlog_pending:
            moved += w.backlog_pending
            w.backlog_created += w.backlog_pending
            w.backlog_pending = 0
        if w.retry_pending:
            moved += w.retry_pending
            w.fresh_retries += w.retry_pending
            w.apollo += w.retry_pending * w.cpl
            w.retry_pending = 0
        if w.new_pending >= 1:
            leads = int(w.new_pending)
            moved += leads
            w.fresh_new += leads
            w.apollo += leads * w.cpl
            w.new_pending -= leads
        return CycleReport(run_id="run-1", stages={"qualify_opportunity": {"approved": moved}},
                           delivery={"instantly": {"delivered": moved}} if moved else {"instantly": {}})

    def deliver(self, *, max_items, channels):
        self.w.calls.append(("deliver",))
        return {"airtable": {}, "instantly": {}}

    def acquire_block(self, pages):
        w = self.w
        records = min(pages * 100, w.market, w.fantastic_limit - w.records)
        w.calls.append(("acquire", pages))
        w.market -= records
        w.records += records
        w.new_pending += records * w.ypr
        return [{"new_postings": records, "stop_reason": "page_budget", "rows": records}]

    _processing_budget_stopped = staticmethod(Runner._processing_budget_stopped)
    _round_activity = staticmethod(Runner._round_activity)


def controller(world, monkeypatch, **kw):
    runner = FakeRunner(world, **{k: kw.pop(k) for k in ("instantly", "airtable") if k in kw})
    c = DailyController(runner, budget_id="prod-scheduled-20260923", env={}, **kw)
    w = world

    def measure():
        return {"fresh": w.fresh_new + w.fresh_retries, "fresh_new_units": w.fresh_new,
                "fresh_retries": w.fresh_retries, "backlog": w.backlog_created, "airtable_fresh": 0,
                "airtable_backlog": 0, "fantastic_records": w.records, "fantastic_requests": int(w.records / 100),
                "apollo_credits": w.apollo, "apollo_requests": int(w.apollo * 2.2), "pending_delivery": 0,
                "available_work": w.unprocessed}
    c.measure = measure
    monkeypatch.setattr(daily, "budget_status", lambda conn, bid: {
        "limits": {"fantastic_credits": w.fantastic_limit, "apollo_credits": w.apollo_limit, "apollo_requests": 6000},
        "used": {"fantastic": {"credits": w.records}, "apollo": {"credits": w.apollo, "requests": int(w.apollo * 2.2)}}})
    return c


def purchases(world):
    return [c for c in world.calls if c[0] == "acquire"]


# --- order and target -------------------------------------------------------------------
def test_backlog_is_drained_before_any_purchase_and_never_counts_toward_the_target(monkeypatch):
    w = World(backlog=1500)
    rep = controller(w, monkeypatch).run()
    assert w.calls[0] == ("cycle", False)
    assert rep.backlog_created == 1500
    assert rep.fresh_created >= 1000           # the target was met by FRESH leads, not by the 1,500 backlog
    assert purchases(w)                        # backlog alone never satisfied it


def test_blocks_are_small_and_stop_once_the_fresh_target_is_met(monkeypatch):
    w = World()
    rep = controller(w, monkeypatch).run()
    assert rep.stop_reason == "target_reached" and rep.fresh_created >= 1000
    assert all(pages <= 2 for _, pages in purchases(w))            # <= 200 records per block
    # Bought only what the measured yield required: ~1,000 / 0.263 ~ 3,800 records, not the 4,000 ceiling.
    assert 3700 <= w.records <= 3900
    last_buy = max(i for i, c in enumerate(w.calls) if c[0] == "acquire")
    assert not any(c[0] == "acquire" for c in w.calls[last_buy + 1:])


def test_one_thousand_is_a_minimum_everything_produced_is_delivered(monkeypatch):
    w = World(retry_leads=1300)                 # already-paid retries alone exceed the target
    rep = controller(w, monkeypatch).run()
    assert rep.fresh_created == 1300            # nothing capped at 1,000
    assert purchases(w) == []                   # and nothing bought: the target was met from free inventory
    assert ("deliver",) in w.calls              # final delivery pass always runs


def test_free_retries_reduce_what_is_bought(monkeypatch):
    w = World(retry_leads=600)
    controller(w, monkeypatch).run()
    assert w.records <= 1600                    # ~400 / 0.263, not 3,800


# --- guards: never buy when ... -------------------------------------------------------------
def test_no_purchase_while_apollo_refuses(monkeypatch):
    w = World(apollo_refusing=True)
    rep = controller(w, monkeypatch).run()
    assert purchases(w) == [] and rep.acquisition_stop == "apollo_refusing"


def test_no_purchase_when_the_apollo_allowance_cannot_process_it(monkeypatch):
    w = World(apollo_limit=10)
    rep = controller(w, monkeypatch).run()
    assert purchases(w) == [] and rep.acquisition_stop == "apollo_daily_allowance_insufficient"


def test_no_purchase_when_delivery_is_unavailable(monkeypatch):
    w = World()
    rep = controller(w, monkeypatch, instantly=False).run()
    assert purchases(w) == [] and rep.acquisition_stop == "delivery_unavailable"


def test_unprocessed_inventory_is_drained_instead_of_buying_more(monkeypatch):
    w = World()
    c = controller(w, monkeypatch)
    real_gate = c.block_gate
    seen = []

    def gate(m, records):
        if not seen:
            seen.append(1)
            w.unprocessed = 5          # something became available: drain it first
            return real_gate({**m, "available_work": 5}, records)
        return real_gate(m, records)
    c.block_gate = gate
    c.run()
    first_buy = next(i for i, x in enumerate(w.calls) if x[0] == "acquire")
    assert w.calls[first_buy - 1] == ("cycle", False)


def test_the_fantastic_ceiling_is_a_limit_not_a_target(monkeypatch):
    w = World(yield_per_record=0.1, fantastic_limit=1000)   # poor market: the ceiling binds first
    rep = controller(w, monkeypatch).run()
    assert w.records <= 1000
    assert rep.stop_reason.startswith("target_not_reached") and rep.fresh_created < 1000
    assert rep.acquisition_stop in ("fantastic_ceiling", "apollo_daily_allowance_insufficient")


def test_an_empty_market_stops_buying(monkeypatch):
    w = World(market=0)
    rep = controller(w, monkeypatch).run()
    assert rep.acquisition_stop == "no_new_inventory" and len(purchases(w)) == 2


def test_estimates_update_from_this_runs_results(monkeypatch):
    w = World(yield_per_record=0.5)             # a better market than the 0.263 prior
    rep = controller(w, monkeypatch).run()
    ests = [b["est_leads_per_record"] for b in rep.blocks]
    assert ests[-1] > ests[0] > 0.263
    assert w.records < 2600                     # it bought less because the market was better


# --- block acquisition keeps 4 priority : 1 discovery -----------------------------------------
def test_small_blocks_keep_the_four_to_one_allocation():
    sources = ("fantastic:active-jb", "fantastic:active-ats")
    slots = [balanced_slot(sources, i) for i in range(50)]
    assert sum(1 for _, p in slots if p == DISCOVERY_PROFILE) == 10
    assert sum(1 for _, p in slots if p == PRIORITY_PROFILE) == 40


def test_block_size_is_bounded():
    with pytest.raises(ValueError):
        DailyController(FakeRunner(World()), budget_id="x", block_pages=3, env={})
    assert YieldEstimate().leads_per_record(0, 0) == pytest.approx(0.263)


def test_inventory_that_never_clears_cannot_loop_the_run(monkeypatch):
    w = World()
    c = controller(w, monkeypatch)
    base = c.measure
    c.measure = lambda: {**base(), "available_work": 3}      # always "available", never processed
    rep = c.run()
    assert purchases(w) and rep.fresh_created >= 1000
    assert any(e == "stuck_inventory_ignored" for _, e, _ in c.r.logs)
