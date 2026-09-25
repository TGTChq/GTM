"""The daily production controller: 1,000 is a MINIMUM of fresh Instantly creations.

KPI: at least ``target`` unique, net-new, verified, compliant leads CREATED in the
configured (Challenger) Instantly campaigns from approvals produced BY THIS RUN.
Backlog delivered during the run is delivered and reported, never counted toward
the target. Approvals, Airtable rows and emails sent are not the KPI.

Order (2026-09-22):
  A. drain free and already-paid inventory first (delivery backlog, bought jobs,
     cached resolutions, stored people, available 2nd/3rd contacts);
  B. count fresh Instantly creations;
  C. while below target, buy Fantastic in small blocks (<= ``block_pages`` pages),
     each fully processed and delivered before the next is considered;
  D. after each block, re-estimate leads per record and Apollo credits per lead
     from this run's actual results (seeded with the 2026-09-21 measurement);
  E. stop buying when the target is met, a ceiling or guard says no, or the market
     returns nothing. Delivery of everything already produced always finishes.

The budget ceilings are safety limits, not spending targets. Nothing here changes
a qualification, compliance, employer, email, suppression or dedupe rule.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .services import followups, instantly_capacity, replacements
from .services.spend_budget import APOLLO_ROLLING_CAP_ENV, ROLLING_WINDOW, budget_status

STAGE_KINDS = ("resolve_identity", "classify", "qualify_opportunity")


@dataclass
class YieldEstimate:
    """Seeded with the measured 2026-09-21 run, updated with this run's results."""

    prior_leads_per_record: float = 0.263
    prior_records: float = 500.0
    prior_credits_per_lead: float = 1.48
    prior_leads: float = 200.0
    prior_requests_per_record: float = 0.87

    def leads_per_record(self, new_unit_leads: int, records: float) -> float:
        return (self.prior_leads_per_record * self.prior_records + new_unit_leads) / (self.prior_records + records)

    def credits_per_lead(self, credits: float, leads: int) -> float:
        return (self.prior_credits_per_lead * self.prior_leads + credits) / (self.prior_leads + leads)

    def requests_per_record(self, requests: int, records: float) -> float:
        return (self.prior_requests_per_record * self.prior_records + requests) / (self.prior_records + records)


@dataclass
class DailyReport:
    run_id: str
    budget_id: str
    target: int
    stop_reason: str = ""
    acquisition_stop: str = ""
    fresh_created: int = 0
    fresh_from_new_units: int = 0
    fresh_from_retries: int = 0
    backlog_created: int = 0
    airtable_fresh: int = 0
    airtable_backlog: int = 0
    fantastic_records: float = 0.0
    fantastic_requests: int = 0
    apollo_credits: float = 0.0
    apollo_requests: int = 0
    capacity_block: Dict[str, Any] = field(default_factory=dict)
    replacements: Dict[str, int] = field(default_factory=dict)
    followups: Dict[str, int] = field(default_factory=dict)
    rotation: Dict[str, Any] = field(default_factory=dict)
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    drains: List[Dict[str, Any]] = field(default_factory=list)
    rounds: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def target_met(self) -> bool:
        return self.fresh_created >= self.target

    def to_dict(self) -> Dict[str, Any]:
        cost = (self.apollo_credits / self.fresh_created) if self.fresh_created else None
        return {
            "run_id": self.run_id, "budget_id": self.budget_id, "target": self.target,
            "target_met": self.target_met, "stop_reason": self.stop_reason, "acquisition_stop": self.acquisition_stop,
            "fresh_instantly_created": self.fresh_created, "fresh_from_new_units": self.fresh_from_new_units,
            "fresh_from_retries": self.fresh_from_retries, "backlog_instantly_created": self.backlog_created,
            "total_instantly_created": self.fresh_created + self.backlog_created,
            "airtable_fresh": self.airtable_fresh, "airtable_backlog": self.airtable_backlog,
            "shortfall": max(0, self.target - self.fresh_created),
            "fantastic_records": self.fantastic_records, "fantastic_requests": self.fantastic_requests,
            "apollo_credits": self.apollo_credits, "apollo_requests": self.apollo_requests,
            "apollo_credits_per_fresh_lead": round(cost, 3) if cost else None,
            "capacity_block": self.capacity_block, "replacements": self.replacements,
            "followups": self.followups,
            "rotation": {k: v for k, v in (self.rotation or {}).items() if k != "rotation"},
            "blocks": self.blocks, "drains": self.drains, "rounds": len(self.rounds),
        }


class DailyController:
    def __init__(self, runner, *, budget_id: str, target: int = 1000, block_pages: int = 2,
                 max_rounds: int = 300, max_items: int = 10000, stall_rounds: int = 2,
                 estimate: Optional[YieldEstimate] = None, env=None):
        if target < 1 or not 1 <= block_pages <= 2 or max_rounds < 1:
            raise ValueError("target >= 1, 1 <= block_pages <= 2 (<= 250 records per block), max_rounds >= 1")
        self.r = runner
        self.budget_id = budget_id
        self.target = target
        self.block_pages = block_pages
        self.max_rounds = max_rounds
        self.max_items = max_items
        self.stall_rounds = stall_rounds
        self.est = estimate or YieldEstimate()
        self.env = os.environ if env is None else env
        self.started = self.r.now()
        self.report = DailyReport(run_id=self.r.run_id, budget_id=budget_id, target=target)
        self._last_delivery: Dict[str, Dict[str, int]] = {}

    # --- measurement ----------------------------------------------------------
    def campaign_ids(self) -> List[str]:
        return sorted(set((self.r.s.campaign_env or {}).values()))

    def measure(self) -> Dict[str, Any]:
        conn, t0, ids = self.r.conn, self.started, self.campaign_ids()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT (a.run_id = %(run)s) AS fresh, (op.created_at >= %(t0)s) AS new_unit,
                       count(DISTINCT lower(o.payload_json->>'email')) AS n
                FROM delivery_receipts r JOIN delivery_outbox o ON o.id = r.outbox_id
                JOIN approvals a ON a.id = o.approval_id JOIN opportunities op ON op.id = a.opportunity_id
                WHERE r.channel = 'instantly' AND r.receipt_kind = 'created' AND r.received_at >= %(t0)s
                  AND r.external_campaign = ANY(%(ids)s)
                GROUP BY 1, 2
                """, {"run": self.r.run_id, "t0": t0, "ids": ids})
            inst = [dict(x) for x in cur.fetchall()]
            cur.execute(
                """
                SELECT (a.run_id = %(run)s) AS fresh, count(DISTINCT a.id) AS n
                FROM delivery_receipts r JOIN delivery_outbox o ON o.id = r.outbox_id JOIN approvals a ON a.id = o.approval_id
                WHERE r.channel = 'airtable' AND r.receipt_kind IN ('created', 'reconciled') AND r.received_at >= %(t0)s
                GROUP BY 1
                """, {"run": self.r.run_id, "t0": t0})
            air = {bool(x["fresh"]): int(x["n"]) for x in cur.fetchall()}
            cur.execute(
                "SELECT provider, count(*) AS requests, COALESCE(sum(estimated_credits), 0) AS credits "
                "FROM spend_reservations WHERE budget_id = %s AND created_at >= %s GROUP BY provider",
                (self.budget_id, t0))
            spend = {x["provider"]: {"requests": int(x["requests"]), "credits": float(x["credits"])} for x in cur.fetchall()}
            cur.execute(
                "SELECT count(*) AS n FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id "
                "WHERE o.channel = 'instantly' AND o.state IN ('pending', 'failed', 'claimed', 'in_flight') AND a.run_id = %s",
                (self.r.run_id,))
            pending = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT count(*) AS n FROM work_items WHERE kind = ANY(%s) AND state IN ('ready', 'retry') AND available_at <= %s",
                (list(STAGE_KINDS), self.r.now()))
            available = int(cur.fetchone()["n"])
        conn.commit()
        fresh_new = sum(int(x["n"]) for x in inst if x["fresh"] and x["new_unit"])
        fresh_old = sum(int(x["n"]) for x in inst if x["fresh"] and not x["new_unit"])
        return {
            "fresh": fresh_new + fresh_old, "fresh_new_units": fresh_new, "fresh_retries": fresh_old,
            "backlog": sum(int(x["n"]) for x in inst if not x["fresh"]),
            "airtable_fresh": air.get(True, 0), "airtable_backlog": air.get(False, 0),
            "fantastic_records": spend.get("fantastic", {}).get("credits", 0.0),
            "fantastic_requests": spend.get("fantastic", {}).get("requests", 0),
            "apollo_credits": spend.get("apollo", {}).get("credits", 0.0),
            "apollo_requests": spend.get("apollo", {}).get("requests", 0),
            "pending_delivery": pending, "available_work": available,
        }

    def _apply(self, m: Dict[str, Any]) -> None:
        rp = self.report
        rp.fresh_created, rp.fresh_from_new_units, rp.fresh_from_retries = m["fresh"], m["fresh_new_units"], m["fresh_retries"]
        rp.backlog_created, rp.airtable_fresh, rp.airtable_backlog = m["backlog"], m["airtable_fresh"], m["airtable_backlog"]
        rp.fantastic_records, rp.fantastic_requests = m["fantastic_records"], m["fantastic_requests"]
        rp.apollo_credits, rp.apollo_requests = m["apollo_credits"], m["apollo_requests"]

    # --- phases -------------------------------------------------------------------
    def drain(self, label: str) -> str:
        """Process and deliver everything available now, with NO acquisition. Stops
        paid processing as soon as the fresh target is met (delivery still runs)."""
        stalled, cycles = 0, 0
        while len(self.report.rounds) < self.max_rounds:
            rep = self.r.cycle(acquire=False, deliver=True, max_items=self.max_items,
                               delivery_channels=("instantly", "airtable"))
            cycles += 1
            self._last_delivery = rep.delivery or {}
            self.report.rounds.append({"phase": label, "stages": rep.stages, "delivery": rep.delivery,
                                       "acquisition_new_postings": 0, "acquisition_stops": []})
            if self.r._processing_budget_stopped(rep):
                outcome = "processing_budget_exhausted"
                break
            m = self.measure()
            self._apply(m)
            if m["fresh"] >= self.target:
                outcome = "target_reached"
                break
            stalled = stalled + 1 if self.r._round_activity(rep) == 0 else 0
            if stalled >= self.stall_rounds:
                outcome = "drained"
                break
        else:
            outcome = "max_rounds"
        self.report.drains.append({"phase": label, "cycles": cycles, "outcome": outcome,
                                   "fresh_after": self.report.fresh_created})
        return outcome

    def deliver_everything(self) -> None:
        """Finish delivery of every eligible contact already produced. No enrichment."""
        for _ in range(20):
            out = self.r.deliver(max_items=self.max_items, channels=("instantly", "airtable"))
            moved = sum(int(v or 0) for counts in out.values() for k, v in counts.items() if k not in ("deferred",))
            if not moved:
                break

    def capacity_check(self) -> Dict[str, Any]:
        """Is there room in the destination? One delivery pass answers it, and that pass
        is free and owed anyway: it hands over contacts that are already paid for. It
        runs BEFORE any processing, because enrichment spends Apollo credits and a full
        destination makes that spend worthless (measured 2026-09-24).

        Then, and only if the workspace would not hold what this run intends to create,
        room is made by removing contacts whose sequence finished and who never replied.
        That decision reads the occupancy first, so a workspace with room is left alone.
        """
        self.r.deliver(max_items=self.max_items, channels=("instantly", "airtable"))
        self.report.rotation = self.r.make_instantly_room(target=self.target)
        return self.r.instantly_capacity_gate()

    def block_gate(self, m: Dict[str, Any], records: int) -> str:
        """'' when buying ``records`` more is justified and safe, else why not."""
        if self.r.instantly is None or self.r.airtable is None:
            return "delivery_unavailable"
        if self.r.instantly_capacity_gate().get("blocked"):
            return instantly_capacity.STOP_REASON
        inst = self._last_delivery.get("instantly") or {}
        ok = sum(int(inst.get(k, 0)) for k in ("delivered", "reconciled", "blocked", "deferred"))
        if int(inst.get("failed", 0)) + int(inst.get("uncertain", 0)) > 0 and ok == 0:
            return "delivery_unavailable"
        gate = self.r.acquisition_gate()
        if not gate.get("allowed", False):
            return f"apollo_{gate.get('state')}"
        if m["available_work"] > 0:
            return "inventory_unprocessed"
        status = budget_status(self.r.conn, self.budget_id)
        limits, used = status["limits"], status["used"]
        f_left = limits.get("fantastic_credits", 0) - used.get("fantastic", {}).get("credits", 0)
        if f_left < records:
            return "fantastic_ceiling"
        leads = self.est.leads_per_record(m["fresh_new_units"], m["fantastic_records"])
        cpl = self.est.credits_per_lead(m["apollo_credits"], m["fresh"])
        rpr = self.est.requests_per_record(m["apollo_requests"], m["fantastic_records"])
        need_credits = records * leads * cpl
        a_left = limits.get("apollo_credits", 0) - used.get("apollo", {}).get("credits", 0)
        if a_left < need_credits:
            return "apollo_daily_allowance_insufficient"
        r_left = limits.get("apollo_requests", 0) - used.get("apollo", {}).get("requests", 0)
        if r_left < records * rpr:
            return "apollo_request_allowance_insufficient"
        rolling = str(self.env.get(APOLLO_ROLLING_CAP_ENV, "") or "").strip()
        if rolling:
            with self.r.conn.cursor() as cur:
                cur.execute("SELECT COALESCE(sum(estimated_credits), 0) AS c FROM spend_reservations "
                            "WHERE provider = 'apollo' AND status <> 'refused' AND created_at > %s",
                            (self.r.now() - ROLLING_WINDOW,))
                used_30d = float(cur.fetchone()["c"])
            self.r.conn.commit()
            if float(rolling) - used_30d < need_credits:
                return "apollo_rolling_allowance_insufficient"
        return ""

    def run(self) -> DailyReport:
        rp = self.report
        self.r._log("daily", "start", {"budget_id": self.budget_id, "target": self.target,
                                       "block_pages": self.block_pages, "campaign_ids": len(self.campaign_ids())})
        capacity = self.capacity_check()                      # 0: is there anywhere to put leads?
        short = int((rp.rotation or {}).get("deficit") or 0)
        if short > 0 and not capacity.get("blocked"):
            # There is room for some of this run but not for the whole of it, and
            # rotation could not make up the difference. Buying the shortfall would
            # produce contacts with nowhere to go, so acquisition stops here and says
            # by exactly how many slots it is short. Delivery already ran, so what was
            # already paid for still went.
            rp.acquisition_stop = f"instantly_slots_short_by:{short}"
            rp.stop_reason = f"target_not_reached:instantly_slots_short_by:{short}"
            self.r._log("daily", "instantly_slots_short", {
                "deficit": short, "free": (rp.rotation or {}).get("free_after")
                or (rp.rotation or {}).get("free_before"),
                "reason": (rp.rotation or {}).get("reason", "")})
            self._apply(self.measure())
            self.r._log("daily", "end", rp.to_dict())
            return rp
        if capacity.get("blocked"):
            # Every contact this run would produce would land nowhere. Stop before the
            # first purchase and the first enrichment; the approved contacts already
            # waiting keep their identity, verified email and suppressions and go first
            # when there is room again.
            rp.capacity_block = {"blocked": True, "since": str(capacity.get("since") or ""),
                                 "remaining_uploads": capacity.get("remaining_uploads"),
                                 "alerted": bool(self.r.instantly_capacity_alert())}
            rp.acquisition_stop = instantly_capacity.STOP_REASON
            rp.stop_reason = f"target_not_reached:{instantly_capacity.STOP_REASON}"
            self._apply(self.measure())
            self.r._log("daily", "end", rp.to_dict())
            return rp
        # A departure left a vacancy; putting it back on the ordinary queue BEFORE the
        # backlog drain means the same run fills it, with every gate it always had.
        rp.replacements = self.r.fill_departed_units(limit=replacements.per_run(self.env))
        # An out-of-office that said "back on the 6th" is decided when the 6th arrives:
        # suppressed addresses and gone vacancies are closed, the rest are verified.
        rp.followups = self.r.decide_due_followups(limit=followups.per_run(self.env))
        outcome = self.drain("backlog")                       # A: free and already-paid inventory first
        empty_blocks = 0
        unprocessed_streak = 0
        page = int(self.r.s.fantastic_page_limit)
        while outcome != "target_reached" and len(rp.rounds) < self.max_rounds:
            if outcome == "processing_budget_exhausted":
                rp.acquisition_stop = "processing_budget_exhausted"
                break
            m = self.measure()
            self._apply(m)
            deficit = self.target - m["fresh"] - m["pending_delivery"]
            if deficit <= 0:
                rp.acquisition_stop = "credible_pending_covers_target"
                break
            leads = self.est.leads_per_record(m["fresh_new_units"], m["fantastic_records"])
            pages = max(1, min(self.block_pages, math.ceil(deficit / max(leads, 1e-6) / page)))
            if unprocessed_streak >= 2:
                # The same items keep reappearing (e.g. failing and retrying): draining again
                # would loop without progress. Decide on the market instead, visibly.
                self.r._log("daily", "stuck_inventory_ignored", {"available_work": m["available_work"]})
                m = {**m, "available_work": 0}
            why = self.block_gate(m, pages * page)
            if why == "inventory_unprocessed":
                before_fresh = m["fresh"]
                outcome = self.drain("unprocessed")
                unprocessed_streak = unprocessed_streak + 1 if self.report.fresh_created == before_fresh else 0
                continue
            unprocessed_streak = 0
            if why:
                rp.acquisition_stop = why
                break
            before = m
            acq = self.r.acquire_block(pages)                 # C: one block, <= 2 pages
            new_postings = sum(int(x.get("new_postings") or 0) for x in acq)
            stops = [str(x.get("stop_reason") or "") for x in acq]
            rp.rounds.append({"phase": "acquire", "stages": {}, "delivery": {},
                              "acquisition_new_postings": new_postings, "acquisition_stops": stops})
            outcome = self.drain(f"block{len(rp.blocks) + 1}")  # D: process and deliver it fully
            after = self.measure()
            self._apply(after)
            records = after["fantastic_records"] - before["fantastic_records"]
            gained = after["fresh_new_units"] - before["fresh_new_units"]
            rp.blocks.append({
                "block": len(rp.blocks) + 1, "pages": pages, "records": records, "new_postings": new_postings,
                "fresh_new_unit_leads": gained, "marginal_leads_per_record": round(gained / records, 4) if records else None,
                "fresh_total": after["fresh"], "est_leads_per_record": round(
                    self.est.leads_per_record(after["fresh_new_units"], after["fantastic_records"]), 4),
                "est_credits_per_lead": round(self.est.credits_per_lead(after["apollo_credits"], after["fresh"]), 3),
                "acquisition_stops": stops,
            })
            if any(s in ("auth_refused", "quota_refused") or s.startswith(("spend_budget_exhausted:", "quota_reserve:"))
                   for s in stops):
                rp.acquisition_stop = next(s for s in stops if s)
                break
            empty_blocks = empty_blocks + 1 if records == 0 and new_postings == 0 else 0
            if empty_blocks >= 2:
                rp.acquisition_stop = "no_new_inventory"
                break
        if rp.acquisition_stop == instantly_capacity.STOP_REASON and not rp.capacity_block:
            gate = self.r.instantly_capacity_gate()           # it filled up DURING the run
            rp.capacity_block = {"blocked": True, "since": str(gate.get("since") or ""),
                                 "remaining_uploads": gate.get("remaining_uploads"),
                                 "alerted": bool(self.r.instantly_capacity_alert())}
        self.deliver_everything()                            # finish delivery of what exists
        final = self.measure()
        self._apply(final)
        if rp.fresh_created >= self.target:
            rp.stop_reason = "target_reached"
        elif rp.acquisition_stop:
            rp.stop_reason = f"target_not_reached:{rp.acquisition_stop}"
        else:
            rp.stop_reason = "target_not_reached:max_rounds" if len(rp.rounds) >= self.max_rounds else "target_not_reached"
        self.r._log("daily", "end", rp.to_dict())
        return rp


__all__ = ["DailyController", "DailyReport", "YieldEstimate"]
