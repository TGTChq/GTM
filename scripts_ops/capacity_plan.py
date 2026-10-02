"""Instantly capacity plan from a measured run. Pure computation, no network.

Model (every term is measured or configured):

* Instantly sends only on the campaigns' schedule days (Mon-Fri), while the
  pipeline enrolls every calendar day. A campaign's new-lead limit N (per sending
  day) stops its backlog from growing only when
      N >= demand_per_calendar_day * 7 / send_days  (+ a drain of today's backlog).
* Follow-ups count: steady-state emails per sending day = EMAILS_PER_LEAD * N.
* Instantly skips a sender that reached its daily limit and uses another of the
  campaign's senders, so load goes where capacity is: a max-flow from campaigns
  to the sender accounts each may use. Every account is capped at
  (1 - HEADROOM) x its own limit (never above 20), which also keeps at least
  HEADROOM of the aggregate free.
* Senders are only ADDED to OPERATIONS (shared, add-only), from pools with the most
  spare capacity; nothing is removed from any campaign.
* If the targets do not fit, capacity is allocated in proportion to measured
  demand (progressive filling) and the remaining shortfall is reported exactly.

Usage:
  python capacity_plan.py <senders_snapshot.json> <demand.json> <plan_out.json>
demand.json: {"<campaign_id>": {"demand_per_day": int, "backlog": int}, ...,
              "_control_active": {"<control_campaign_id>": int}}   (optional)
"""
from __future__ import annotations

import json
import math
import sys
from typing import Dict, List, Tuple

OPERATIONS = "69def27c-7799-41a2-9ba8-205e54ab071b"

EMAILS_PER_LEAD = 4          # 4 steps; replies and bounces only lower it
HEADROOM = 0.10              # the user's minimum, applied per account (stricter than aggregate)
SEND_DAYS = 5                # campaign schedule: Mon-Fri, 08:00-18:00 America/Chicago
DRAIN_SEND_DAYS = 10         # clear today's backlog within two sending weeks
MAX_OPS_ADDED = 23           # the approved "approximately 23" additional shared senders
MAX_SENDER_LIMIT = 20


def targets(demand: Dict[str, dict]) -> Dict[str, float]:
    """New leads per sending day each campaign needs for its backlog to shrink."""
    return {cid: d["demand_per_day"] * 7.0 / SEND_DAYS + d.get("backlog", 0) / DRAIN_SEND_DAYS
            for cid, d in demand.items() if not cid.startswith("_")}


def _groups(members: Dict[str, List[str]], caps: Dict[str, float]) -> Dict[frozenset, float]:
    """Accounts with the same campaign memberships are interchangeable: one node each."""
    sig: Dict[str, set] = {}
    for cid, senders in members.items():
        for a in senders:
            sig.setdefault(a, set()).add(cid)
    groups: Dict[frozenset, float] = {}
    for a, cs in sig.items():
        key = frozenset(cs)
        groups[key] = groups.get(key, 0.0) + caps[a]
    return groups


def _max_flow(load: Dict[str, float], groups: Dict[frozenset, float]) -> Tuple[float, Dict[frozenset, float]]:
    """Source -> campaign (emails/day) -> account group it may use -> sink (capacity)."""
    camps = [c for c, d in load.items() if d > 1e-9]
    gl = list(groups)
    n = 2 + len(camps) + len(gl)
    S, T = 0, n - 1
    cap = [[0.0] * n for _ in range(n)]
    for i, c in enumerate(camps):
        cap[S][1 + i] = load[c]
        for j, g in enumerate(gl):
            if c in g:
                cap[1 + i][1 + len(camps) + j] = float("inf")
    for j, g in enumerate(gl):
        cap[1 + len(camps) + j][T] = groups[g]
    flow = 0.0
    while True:  # Edmonds-Karp over a few dozen nodes
        parent = [-1] * n
        parent[S] = S
        queue = [S]
        while queue and parent[T] == -1:
            u = queue.pop(0)
            for v in range(n):
                if parent[v] == -1 and cap[u][v] > 1e-9:
                    parent[v] = u
                    queue.append(v)
        if parent[T] == -1:
            break
        push, v = float("inf"), T
        while v != S:
            push = min(push, cap[parent[v]][v])
            v = parent[v]
        v = T
        while v != S:
            u = parent[v]
            cap[u][v] -= push
            cap[v][u] += push
            v = u
        flow += push
    used = {g: groups[g] - cap[1 + len(camps) + j][T] for j, g in enumerate(gl)}
    return flow, used


def _loads(n: Dict[str, float], residual: Dict[str, float]) -> Dict[str, float]:
    out = {c: EMAILS_PER_LEAD * v for c, v in n.items()}
    for c, r in residual.items():
        out[c] = out.get(c, 0.0) + r
    return out


def feasible(members, caps, n, residual) -> bool:
    load = _loads(n, residual)
    flow, _ = _max_flow(load, _groups(members, caps))
    return flow >= sum(load.values()) - 1e-6


def allocate(members: Dict[str, List[str]], caps: Dict[str, float], want: Dict[str, float],
             residual: Dict[str, float]) -> Dict[str, float]:
    """Progressive filling in proportion to measured demand: every unfrozen campaign
    rises at the same fraction of its target; a campaign freezes when it can rise no
    further without putting a sender above its capped limit."""
    n = {c: 0.0 for c in want}
    frozen = {c for c, w in want.items() if w <= 0}
    level = 0.0
    while len(frozen) < len(want) and level < 1.0 - 1e-9:
        active = [c for c in want if c not in frozen]
        lo, hi = level, 1.0
        for _ in range(30):
            mid = (lo + hi) / 2
            if feasible(members, caps, {**n, **{c: mid * want[c] for c in active}}, residual):
                lo = mid
            else:
                hi = mid
        level = lo
        for c in active:
            n[c] = level * want[c]
        if level >= 1.0 - 1e-6:
            break
        for c in active:
            if not feasible(members, caps, {**n, c: n[c] + max(0.5, 0.01 * want[c])}, residual):
                frozen.add(c)
    return n


def plan(snapshot: dict, demand: Dict[str, dict], max_added: int = MAX_OPS_ADDED) -> dict:
    accounts = {a["email"]: a for a in snapshot["accounts"]}
    camps = snapshot["campaigns"]
    challenger = {cid for cid, v in camps.items() if v["group"] == "challenger"}
    control = {cid for cid, v in camps.items() if v["group"] == "control"}
    members = {cid: list(v["email_list"]) for cid, v in camps.items()}
    healthy = {e for e, a in accounts.items() if a.get("status") == 1 and a.get("warmup_status") == 1
               and (a.get("stat_warmup_score") or 0) >= 90}
    caps = {e: (1.0 - HEADROOM) * min(int(accounts[e]["daily_limit"]), MAX_SENDER_LIMIT) for e in accounts}
    # Control is never written; its leftover follow-ups (<= 1 email/lead/day) still load shared senders.
    residual = {cid: float(n) for cid, n in (demand.get("_control_active") or {}).items() if cid in control}
    want = {cid: 0.0 for cid in challenger}
    want.update({cid: t for cid, t in targets(demand).items() if cid in challenger})

    def scoped(m):
        return {c: m[c] for c in m if c in challenger or c in residual}

    def run(extra: List[str]) -> Tuple[Dict[str, float], float]:
        m = scoped({**members, OPERATIONS: members[OPERATIONS] + extra})
        alloc = allocate(m, caps, want, residual)
        return alloc, sum(min(alloc[c], want[c]) for c in want)

    added: List[str] = []
    alloc, served = run(added)
    while len(added) < max_added and alloc[OPERATIONS] < want[OPERATIONS] - 1e-6:
        m_now = scoped({**members, OPERATIONS: members[OPERATIONS] + added})
        groups = _groups(m_now, caps)
        _, used = _max_flow(_loads(alloc, residual), groups)
        spare = {g: (c_cap - used.get(g, 0.0)) / c_cap for g, c_cap in groups.items() if c_cap}
        sig: Dict[str, set] = {}
        for c, senders in m_now.items():
            for a in senders:
                sig.setdefault(a, set()).add(c)
        used_domains = {a.split("@")[1] for a in added}
        pool = [a for a in healthy if a in sig and OPERATIONS not in sig[a]]
        if not pool:
            break
        pool.sort(key=lambda a: (-spare.get(frozenset(sig[a]), 0.0), a.split("@")[1] in used_domains, a))
        trial_alloc, trial_served = run(added + [pool[0]])
        if trial_served <= served + 1e-6:
            break
        added.append(pool[0])
        alloc, served = trial_alloc, trial_served

    final_members = scoped({**members, OPERATIONS: members[OPERATIONS] + added})
    # Whole leads: a campaign that reached its target gets the target rounded up when
    # that still fits; otherwise every allocation is rounded down.
    floored = {c: float(math.floor(v + 1e-6)) for c, v in alloc.items()}
    for c in sorted(want, key=lambda c: want[c]):
        if want[c] > 0 and alloc[c] >= want[c] - 1e-6:
            trial = {**floored, c: float(math.ceil(want[c]))}
            if feasible(final_members, caps, trial, residual):
                floored = trial
    if not feasible(final_members, caps, floored, residual):
        raise AssertionError("plan would put a sender above its capped limit")
    groups = _groups(final_members, caps)
    _, used = _max_flow(_loads(floored, residual), groups)
    total_cap = sum(min(int(accounts[e]["daily_limit"]), MAX_SENDER_LIMIT) for e in accounts)
    total_load = sum(_loads(floored, residual).values())
    per_campaign = {}
    for cid in sorted(challenger, key=lambda c: camps[c]["name"]):
        n = int(floored[cid])
        d = demand.get(cid, {"demand_per_day": 0, "backlog": 0})
        contactable = n * SEND_DAYS / 7.0
        per_campaign[cid] = {
            "name": camps[cid]["name"], "demand_per_day": d["demand_per_day"], "backlog": d.get("backlog", 0),
            "target_new_per_send_day": round(want[cid], 1), "daily_max_leads": n,
            "senders": len(final_members[cid]), "emails_per_send_day": EMAILS_PER_LEAD * n,
            "contactable_per_day": round(contactable, 1),
            "pipeline_cap_per_day": math.floor(contactable),
            "shortfall_per_day": max(0, math.ceil(d["demand_per_day"] - contactable)),
        }
    donors: Dict[str, int] = {}
    for a in added:
        for cid in challenger:
            if a in members[cid]:
                donors[camps[cid]["name"]] = donors.get(camps[cid]["name"], 0) + 1
    return {
        "model": {"emails_per_lead": EMAILS_PER_LEAD, "headroom_per_sender": HEADROOM,
                  "send_days_per_week": SEND_DAYS, "drain_send_days": DRAIN_SEND_DAYS, "max_ops_added": max_added},
        "operations_added_senders": added, "operations_added_by_donor": donors,
        "operations_senders": len(final_members[OPERATIONS]),
        "operations_daily_limit": len(final_members[OPERATIONS]) * MAX_SENDER_LIMIT,
        "campaigns": per_campaign,
        "aggregate": {
            "sender_capacity_per_send_day": total_cap,
            "planned_emails_per_send_day": round(total_load),
            "aggregate_headroom": round(1 - total_load / total_cap, 3),
            "max_sender_group_utilisation": round(max(used[g] / (groups[g] / (1 - HEADROOM)) for g in groups), 3),
            "demand_per_day": sum(c["demand_per_day"] for c in per_campaign.values()),
            "contactable_per_send_day": sum(c["daily_max_leads"] for c in per_campaign.values()),
            "contactable_per_day": round(sum(c["daily_max_leads"] for c in per_campaign.values()) * SEND_DAYS / 7.0, 1),
            "shortfall_per_day": sum(c["shortfall_per_day"] for c in per_campaign.values()),
        },
    }


def senders_needed_for(demand_per_day: float, emails_per_lead: int = EMAILS_PER_LEAD) -> int:
    """Senders at the capped limit needed to contact demand_per_day every calendar day."""
    per_send_day = demand_per_day * 7.0 / SEND_DAYS * emails_per_lead
    return math.ceil(per_send_day / ((1 - HEADROOM) * MAX_SENDER_LIMIT))


if __name__ == "__main__":
    snap = json.load(open(sys.argv[1], encoding="utf-8"))
    dem = json.load(open(sys.argv[2], encoding="utf-8"))
    out = plan(snap, dem)
    json.dump(out, open(sys.argv[3], "w", encoding="utf-8"), indent=1)
    print(f"{'campaign':<22}{'demand/d':>9}{'backlog':>8}{'target/sd':>10}{'max_new':>8}{'senders':>8}"
          f"{'emails/sd':>10}{'contact/d':>10}{'short/d':>8}")
    for c in out["campaigns"].values():
        print(f"{c['name']:<22}{c['demand_per_day']:>9}{c['backlog']:>8}{c['target_new_per_send_day']:>10}"
              f"{c['daily_max_leads']:>8}{c['senders']:>8}{c['emails_per_send_day']:>10}"
              f"{c['contactable_per_day']:>10}{c['shortfall_per_day']:>8}")
    print("OPERATIONS +", len(out["operations_added_senders"]), "shared senders", out["operations_added_by_donor"],
          "-> senders", out["operations_senders"], "daily_limit", out["operations_daily_limit"])
    print("aggregate", out["aggregate"])
    total = out["aggregate"]["demand_per_day"]
    print(f"senders needed to contact {total}/calendar day at the capped limit: {senders_needed_for(total)} (have "
          f"{len(snap['accounts'])})")
