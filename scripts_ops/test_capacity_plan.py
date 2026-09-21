"""Tests for the Instantly capacity plan (pure; run: python -m pytest scripts_ops/test_capacity_plan.py)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capacity_plan as cp  # noqa: E402

OPS = cp.OPERATIONS


def _snapshot(pools):
    """pools: {campaign_id: (group, name, n_exclusive_accounts)}; each campaign its own senders."""
    accounts, campaigns = [], {}
    for cid, (group, name, n) in pools.items():
        emails = [f"s{i}@{name.lower()}{i % 3}.test" for i in range(n)]
        accounts += [{"email": e, "daily_limit": 20, "status": 1, "warmup_status": 1, "stat_warmup_score": 100}
                     for e in emails]
        campaigns[cid] = {"group": group, "name": name, "email_list": emails}
    return {"accounts": accounts, "campaigns": campaigns}


def test_weekday_schedule_raises_the_per_send_day_target():
    t = cp.targets({"c": {"demand_per_day": 100, "backlog": 50}})
    assert t["c"] == 100 * 7 / 5 + 50 / cp.DRAIN_SEND_DAYS


def test_demand_that_fits_is_met_with_headroom_and_no_added_senders():
    snap = _snapshot({OPS: ("challenger", "OPS", 33), "fin": ("challenger", "FIN", 33)})
    out = cp.plan(snap, {OPS: {"demand_per_day": 50, "backlog": 0}, "fin": {"demand_per_day": 50, "backlog": 0}})
    assert out["operations_added_senders"] == []
    assert out["campaigns"][OPS]["daily_max_leads"] == 70
    assert out["aggregate"]["shortfall_per_day"] == 0
    assert out["aggregate"]["max_sender_group_utilisation"] <= 0.9 + 1e-9


def test_operations_borrows_only_spare_senders_and_never_overloads():
    snap = _snapshot({OPS: ("challenger", "OPS", 33), "prod": ("challenger", "PROD", 33),
                      "fin": ("challenger", "FIN", 33)})
    demand = {OPS: {"demand_per_day": 300, "backlog": 0}, "prod": {"demand_per_day": 20, "backlog": 0},
              "fin": {"demand_per_day": 100, "backlog": 0}}
    out = cp.plan(snap, demand)
    assert 0 < len(out["operations_added_senders"]) <= cp.MAX_OPS_ADDED
    # the small campaign keeps its full demand; Finance too
    assert out["campaigns"]["prod"]["shortfall_per_day"] == 0
    assert out["campaigns"]["fin"]["shortfall_per_day"] == 0
    assert out["aggregate"]["max_sender_group_utilisation"] <= 0.9 + 1e-9
    # added senders come from existing campaigns, never invented
    known = {a["email"] for a in snap["accounts"]}
    assert set(out["operations_added_senders"]) <= known


def test_insufficient_capacity_is_allocated_by_demand_and_the_shortfall_reported():
    snap = _snapshot({OPS: ("challenger", "OPS", 10), "fin": ("challenger", "FIN", 10)})
    demand = {OPS: {"demand_per_day": 400, "backlog": 0}, "fin": {"demand_per_day": 100, "backlog": 0}}
    out = cp.plan(snap, demand, max_added=0)
    ops, fin = out["campaigns"][OPS], out["campaigns"]["fin"]
    # each pool: 10 x 18 = 180 emails/send day -> 45 new leads/send day, whatever the demand
    assert ops["daily_max_leads"] == 45 and fin["daily_max_leads"] == 45
    assert out["aggregate"]["shortfall_per_day"] == ops["shortfall_per_day"] + fin["shortfall_per_day"] > 0
    assert out["aggregate"]["max_sender_group_utilisation"] <= 0.9 + 1e-9


def test_control_residual_load_is_counted_but_control_never_gets_a_limit():
    snap = _snapshot({OPS: ("challenger", "OPS", 10), "ctl": ("control", "CTL", 0)})
    snap["campaigns"]["ctl"]["email_list"] = list(snap["campaigns"][OPS]["email_list"])
    out = cp.plan(snap, {OPS: {"demand_per_day": 100, "backlog": 0}, "_control_active": {"ctl": 80}}, max_added=0)
    assert "ctl" not in out["campaigns"]
    # 180 capacity - 80 residual = 100 emails -> 25 new leads/send day
    assert out["campaigns"][OPS]["daily_max_leads"] == 25


def test_senders_needed_for_a_calendar_day_target():
    assert cp.senders_needed_for(1000) == 312  # 1,000 x 7/5 x 4 = 5,600 emails per send day / 18
