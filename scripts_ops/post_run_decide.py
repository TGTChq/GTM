"""From one reconciled production day to a capacity plan and pipeline caps. No network.

  decide: python post_run_decide.py plan <reconcile.txt> <istate_prerun.json> <senders.json> <out_dir>
    * demand  = eligible approvals that day, per campaign (post_run_reconcile.sql section 8)
    * backlog = leads enrolled in Instantly but not yet emailed before the run
                + pipeline rows carried from earlier days (not the day's own deferrals)
    * Control residual = Control leads still active (load on shared senders, never written)
    Writes demand.json and plan.json, prints which approved increases the measurement
    confirms (the plan needs more than the current limit) and which it does not.

  caps:   python post_run_decide.py caps <plan.json> <apply_state_after.json>
    Pipeline caps per CALENDAR day from the VERIFIED Instantly limits:
      cap = floor(new leads per sending day x 5 / 7)
    so a week of enrolment equals a week of first emails. Capacity-bound campaigns
    (OPERATIONS, FINANCE, AI_TECHNICAL) use the
    plan's allocation when it is lower than the Instantly limit. Prints the variables.
"""
from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capacity_plan as cp  # noqa: E402

CH = {
    "PRODUCT": "7b9aa5f3-fe46-49fa-b2ac-fee1da346ed0", "OPERATIONS": "69def27c-7799-41a2-9ba8-205e54ab071b",
    "FINANCE": "7b319c7a-cc55-4e08-8a47-7058c345d8ae", "PEOPLE_HR": "d2326028-e312-405b-9e16-526bd309d4dd",
    "ECOMMERCE": "c3e81c21-db44-40f5-addc-d9945a78394b", "CUSTOMER_EXPERIENCE": "269cd138-00b1-48c3-9093-16c36120a20e",
    "MARKETING_CREATIVE": "1feb6344-6065-49d7-9764-d125985fb9c9", "GTM_SYSTEMS": "8f25abd5-568a-4e88-b310-9acf85161c6c",
    "AI_TECHNICAL": "8bfa0769-4b9a-4346-8e93-17ac8b726dce",
}
APPROVED = ("OPERATIONS", "FINANCE", "AI_TECHNICAL")
CAPACITY_BOUND = {CH["OPERATIONS"], CH["FINANCE"], CH["AI_TECHNICAL"]}


def parse_demand(reconcile_txt: str) -> dict:
    out = {}
    for line in open(reconcile_txt, encoding="utf-8"):
        if line.startswith("DEMAND|"):
            _, cid, eligible, pending_total, carried = line.strip().split("|")
            out[cid] = {"eligible": int(eligible), "pending_total": int(pending_total), "carried": int(carried)}
    if set(out) != set(CH.values()):
        raise SystemExit("reconcile output does not cover exactly the nine Challenger campaigns")
    return out


def decide(reconcile_txt, istate_path, senders_path, out_dir):
    measured = parse_demand(reconcile_txt)
    istate = json.load(open(istate_path, encoding="utf-8"))["campaigns"]
    senders = json.load(open(senders_path, encoding="utf-8"))
    demand = {}
    for cid, m in measured.items():
        waiting = int(istate[cid]["not_contacted"])
        demand[cid] = {"demand_per_day": m["eligible"], "backlog": waiting + m["carried"],
                       "instantly_waiting_before_run": waiting, "pipeline_carried": m["carried"]}
    demand["_control_active"] = {cid: int(r.get("active", 0)) for cid, r in istate.items() if r["group"] == "control"}
    json.dump(demand, open(os.path.join(out_dir, "demand.json"), "w"), indent=1)
    plan = cp.plan(senders, demand)
    json.dump(plan, open(os.path.join(out_dir, "plan.json"), "w"), indent=1)
    print(f"{'campaign':<22}{'eligible':>9}{'waiting':>8}{'carried':>8}{'need/sd':>8}{'current':>8}{'plan':>6}{'senders':>8}{'short/d':>8}")
    for name, cid in sorted(CH.items()):
        p, d = plan["campaigns"][cid], demand[cid]
        current = istate[cid]["daily_max_leads"]
        print(f"{name:<22}{d['demand_per_day']:>9}{d['instantly_waiting_before_run']:>8}{d['pipeline_carried']:>8}"
              f"{p['target_new_per_send_day']:>8}{current:>8}{p['daily_max_leads']:>6}{p['senders']:>8}{p['shortfall_per_day']:>8}")
    print("OPERATIONS added senders:", len(plan["operations_added_senders"]), plan["operations_added_by_donor"])
    print("aggregate:", plan["aggregate"])
    print("confirmation (raise only where the measured need exceeds the current limit):")
    for name in APPROVED:
        cid = CH[name]
        cur, new = int(istate[cid]["daily_max_leads"] or 0), plan["campaigns"][cid]["daily_max_leads"]
        need = plan["campaigns"][cid]["target_new_per_send_day"]
        print(f"  {name}: need {need}/send day, current {cur}, plan {new} -> "
              f"{'CONFIRMED, raise' if new > cur and need > cur else 'NOT confirmed, keep'}")


def caps(plan_path, after_path):
    plan = json.load(open(plan_path, encoding="utf-8"))
    after = json.load(open(after_path, encoding="utf-8"))
    by = {}
    for name, cid in CH.items():
        verified = int(after[cid]["daily_max_leads"])
        per_send_day = min(verified, plan["campaigns"][cid]["daily_max_leads"]) if cid in CAPACITY_BOUND else verified
        by[cid] = math.floor(per_send_day * cp.SEND_DAYS / 7)
        print(f"{name:<22} instantly {verified:>4}/send day -> pipeline cap {by[cid]:>4}/day")
    total = sum(by.values())
    print("TGTC_DELIVERY_MAX_BY_CAMPAIGN", json.dumps(by, separators=(",", ":")))
    print("TGTC_DELIVERY_MAX_TOTAL", total)
    return by, total


if __name__ == "__main__":
    if sys.argv[1] == "plan":
        decide(*sys.argv[2:6])
    elif sys.argv[1] == "caps":
        caps(*sys.argv[2:4])
