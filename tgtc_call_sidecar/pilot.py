"""The manual 100/100 pilot: from read-only inputs to completed, callable records.

A record counts only when it has a returned direct phone (E.164), current-employer
confirmation, the right cohort and decision-maker persona, and no duplicate person
or phone and no prior wrong-number / do-not-call / call history. Pending reveals,
people without numbers, switchboards and failed lookups never count.
"""
from __future__ import annotations

import csv
import time
from typing import Any, Callable, Dict, List, Optional

from .config import CALL_FIRST, CHALLENGER_CAMPAIGNS, EMAIL_FOLLOW_UP, PilotConfig
from .identity import name_company_key, norm_domain, norm_email, norm_linkedin, person_key
from .personas import FUNCTIONAL, TALENT, classify, search_titles, EXECUTIVE
from .providers import ApolloPhones, AirtableReadOnly, InstantlyReadOnly, request_id_of
from .selection import (Exclusions, best_callable_phone, call_first_units, company_phones_of,
                        current_employer_confirmed, direct_phone_flag, follow_up_candidates, hiring_signal_text,
                        pick_call_first, suggested_opener)
from .store import CreditCapExceeded, SidecarStore


# --- read-only inputs -----------------------------------------------------------------
def load_core_snapshot(inputs_dir: str):
    def rows(name):
        with open(f"{inputs_dir}/{name}.csv", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    units = rows("units")
    units_by_opp = {int(u["opportunity_id"]): u for u in units}
    approved = rows("approved")
    core = Exclusions()
    for r in rows("people_keys"):
        if r["kind"] == "person":
            if r.get("apollo_person_id"):
                core.apollo_ids.add(r["apollo_person_id"])
            if norm_linkedin(r.get("linkedin_url")):
                core.linkedins.add(norm_linkedin(r["linkedin_url"]))
        if norm_email(r.get("email")):
            core.emails.add(norm_email(r["email"]))
    return units, units_by_opp, approved, core


def load_instantly(instantly: InstantlyReadOnly):
    leads_by_email, replied, bounced, unsub = {}, set(), set(), set()
    for cid in CHALLENGER_CAMPAIGNS.values():
        for lead in instantly.leads(cid):
            e = norm_email(lead.get("email"))
            if e:
                leads_by_email[e] = lead
        replied |= {norm_email(x.get("email")) for x in instantly.leads(cid, "FILTER_VAL_REPLIED")}
        bounced |= {norm_email(x.get("email")) for x in instantly.leads(cid, "FILTER_VAL_BOUNCED")}
        unsub |= {norm_email(x.get("email")) for x in instantly.leads(cid, "FILTER_VAL_UNSUBSCRIBED")}
    return leads_by_email, replied, bounced, unsub


def load_crm(airtable: Optional[AirtableReadOnly]) -> Exclusions:
    ex = Exclusions()
    if airtable is None:
        return ex
    for rec in airtable.records(["Email", "LinkedIn"]):
        f = rec.get("fields") or {}
        if norm_email(f.get("Email")):
            ex.emails.add(norm_email(f["Email"]))
        if norm_linkedin(f.get("LinkedIn")):
            ex.linkedins.add(norm_linkedin(f["LinkedIn"]))
    return ex


def instantly_history(instantly: InstantlyReadOnly, *, email: str, first: str, last: str, domain: str) -> bool:
    """True when this person already exists anywhere in the Instantly workspace."""
    if email and any(norm_email(x.get("email")) == email for x in instantly.workspace_search(email)):
        return True
    if first and last:
        for x in instantly.workspace_search(f"{first} {last}"):
            same_name = str(x.get("first_name") or "").lower() == first.lower() and \
                str(x.get("last_name") or "").lower() == last.lower()
            lead_domain = norm_domain(x.get("company_domain") or x.get("website") or str(x.get("email") or "").split("@")[-1])
            if same_name and (not domain or lead_domain == domain):
                return True
    return False


# --- the pilot ------------------------------------------------------------------------
BREAKER = 5


class Pilot:
    def __init__(self, cfg: PilotConfig, store: SidecarStore, apollo: ApolloPhones, instantly: InstantlyReadOnly,
                 *, units, units_by_opp, approved, core: Exclusions, crm: Exclusions, william: Exclusions,
                 william_flagged: set, leads_by_email, replied, bounced, unsubscribed,
                 log: Callable[[str], None] = print, clock=time.monotonic, sleep=time.sleep):
        self.cfg, self.store, self.apollo, self.instantly = cfg, store, apollo, instantly
        self.units, self.units_by_opp, self.approved = units, units_by_opp, approved
        self.core, self.crm, self.william, self.william_flagged = core, crm, william, william_flagged
        self.leads_by_email, self.replied, self.bounced, self.unsub = leads_by_email, replied, bounced, unsubscribed
        self.log, self.clock, self.sleep = log, clock, sleep
        self.stats: Dict[str, Any] = {"rejected_after_reveal": {}, "call_first_companies_searched": 0,
                                      "call_first_searches_empty": 0, "surplus_callable_not_used": 0}
        self._in_flight_keys: set = set()
        #: Consecutive request/poll failures; at BREAKER in a row the pilot stops spending.
        self._fail_streak = 0
        for s in william_flagged:
            store.suppress(f"phone:{s}", "william_list_dnc_or_wrong_number", "william_list")

    def _reject(self, reason: str):
        d = self.stats["rejected_after_reveal"]
        d[reason] = d.get(reason, 0) + 1

    # candidates ----------------------------------------------------------------
    def follow_up_queue(self):
        taken = self.store.active_person_keys() | self.store.called_person_keys()
        cands, why = follow_up_candidates(
            self.approved, self.units_by_opp, self.leads_by_email, replied=self.replied, bounced=self.bounced,
            unsubscribed=self.unsub, william=self.william, suppressed=self.store.suppressed(), taken=taken,
            closed_signals=self.store.closed_signals(), founder_max_employees=self.cfg.founder_max_employees)
        per_company: Dict[int, int] = {}
        out = []
        for c in cands:
            eid = int(c["employer_id"])
            if per_company.get(eid, 0) >= self.cfg.follow_up_per_company:
                why["per_company_limit"] = why.get("per_company_limit", 0) + 1
                continue
            per_company[eid] = per_company.get(eid, 0) + 1
            out.append(c)
        self.stats["follow_up_eligible_before_phone"] = len(out)
        self.stats["follow_up_excluded"] = why
        return self._direct_phones_first(out)

    def _direct_phones_first(self, cands):
        """A free search per person finds Apollo's has_direct_phone flag: 'yes' first,
        'maybe'/'unknown' after, 'no' never (a reveal would buy nothing)."""
        later, flags = [], {}
        self.stats["follow_up_direct_phone_flags"] = flags
        for c in cands:
            domain = norm_domain(c.get("organization_domain") or c["unit"].get("employer_domain"))
            found = [p for p in self.apollo.search(domain=domain, titles=[c.get("title") or ""], similar_titles=False)
                     if str(p.get("id")) == str(c["apollo_person_id"])]
            flag = direct_phone_flag(found[0]) if found else "unknown"
            flags[flag] = flags.get(flag, 0) + 1
            if flag == "yes":
                yield c
            elif flag != "no":
                later.append(c)
        yield from later

    def call_first_queue(self):
        units = call_first_units(self.units, self.store.closed_signals())
        self.stats["call_first_companies_available"] = len(units)
        self.stats["call_first_companies_unexposed"] = sum(
            1 for u in units if str(u.get("email_exposed")).lower() not in ("t", "true", "1"))
        email_companies = {int(a["employer_id"]) for a in self.approved}
        later = []
        for u in units:
            persona_counts = self._persona_counts(CALL_FIRST)
            want = TALENT if persona_counts.get(TALENT, 0) < persona_counts.get(FUNCTIONAL, 0) else FUNCTIONAL
            titles = search_titles(u["function_key"], FUNCTIONAL) + search_titles(u["function_key"], TALENT)
            emp = _int(u.get("employee_count"))
            if emp is not None and emp <= self.cfg.founder_max_employees:
                titles += search_titles(u["function_key"], EXECUTIVE)
            self.stats["call_first_companies_searched"] += 1
            people = self.apollo.search(domain=norm_domain(u.get("employer_domain")),
                                        organization_id=u.get("apollo_org_id") or "", titles=titles)
            if not people:
                self.stats["call_first_searches_empty"] += 1
                continue
            taken = self.store.active_person_keys() | self.store.called_person_keys()
            pick = pick_call_first(people, u, core=self.core, crm=self.crm, william=self.william,
                                   suppressed=self.store.suppressed(), taken=taken | self._in_flight_keys,
                                   want_persona=want, founder_max_employees=self.cfg.founder_max_employees)
            if pick:
                pick["email_exposed_account"] = int(u["employer_id"]) in email_companies or \
                    str(u.get("email_exposed")).lower() in ("t", "true", "1")
                yield pick

    def _persona_counts(self, cohort):
        out: Dict[str, int] = {}
        for m in self.store.members():
            if m["cohort"] == cohort and m["status"] == "active":
                out[m["persona"]] = out.get(m["persona"], 0) + 1
        return out

    # reveal loop ----------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        cfg = self.cfg
        queues = {EMAIL_FOLLOW_UP: self.follow_up_queue(), CALL_FIRST: self.call_first_queue()}
        exhausted = {EMAIL_FOLLOW_UP: False, CALL_FIRST: False}
        in_flight: List[Dict[str, Any]] = []
        self._in_flight_keys: set = set()
        cap_hit = False
        started = self.clock()
        while True:
            need = {c: cfg.target_per_cohort - self.store.count_active(c) for c in queues}
            flying = {c: sum(1 for f in in_flight if f["cohort"] == c) for c in queues}
            # Submit: never more in flight for a cohort than it still needs.
            submitted = False
            for cohort in (EMAIL_FOLLOW_UP, CALL_FIRST):
                if self._fail_streak >= BREAKER and not cap_hit:
                    cap_hit = True   # stop submitting; drain what is in flight
                    self.stats["stopped_by_breaker"] = True
                    self.log(f"{BREAKER} consecutive reveal/poll failures: no further reveals")
                if cap_hit or exhausted[cohort] or need[cohort] - flying[cohort] <= 0 or len(in_flight) >= cfg.max_in_flight:
                    continue
                cand = next(queues[cohort], None)
                if cand is None:
                    exhausted[cohort] = True
                    continue
                try:
                    f = self._submit(cohort, cand)
                except CreditCapExceeded:
                    cap_hit = True
                    self.log("credit cap reached: no further reveals")
                    continue
                if f:
                    in_flight.append(f)
                    self._in_flight_keys.add(f["cand"]["person_key"])
                    flying[cohort] += 1
                    submitted = True
            # Poll what is due.
            now = self.clock()
            for f in list(in_flight):
                if f["next_poll"] > now:
                    continue
                done = self._poll(f)
                if done:
                    in_flight.remove(f)
                    self._in_flight_keys.discard(f["cand"]["person_key"])
            need = {c: cfg.target_per_cohort - self.store.count_active(c) for c in queues}
            filled = all(n <= 0 for n in need.values())
            nothing_left = all(exhausted[c] or need[c] <= 0 for c in queues) or cap_hit
            if self._fail_streak >= BREAKER:
                cap_hit = True
                self.stats["stopped_by_breaker"] = True
            if not in_flight and (filled or nothing_left):
                break
            if not submitted:
                self.sleep(2)
            if self.clock() - started > 6 * 3600:
                self.log("pilot wall-clock limit reached")
                break
        return self.summary(exhausted, cap_hit)

    def _submit(self, cohort: str, cand: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pid = cand["apollo_person_id"]
        ledger_id = self.store.reserve_reveal(person_key=cand["person_key"], apollo_person_id=pid, cohort=cohort,
                                              credits=self.cfg.max_credits_per_reveal, cap=self.cfg.apollo_credit_cap)
        res = self.apollo.reveal(pid)
        body = res["body"] if isinstance(res.get("body"), dict) else {}
        person = body.get("person") if isinstance(body.get("person"), dict) else None
        rid = request_id_of(body)
        if res["status"] != 200 or not rid:
            charged = 1 if person else 0
            self.store.settle_reveal(ledger_id, state="failed", charged=charged, reported=None, phone_returned=False,
                                     callable_=False, outcome=f"reveal_http_{res['status']}" + ("" if rid else "_no_request_id"))
            self._reject("reveal_request_failed")
            self._fail_streak += 1
            return None
        self.store.mark_pending(ledger_id, rid)
        return {"cohort": cohort, "cand": cand, "ledger_id": ledger_id, "request_id": rid, "person": person or {},
                "next_poll": self.clock() + 5, "deadline": self.clock() + self.cfg.poll_timeout_seconds}

    def _poll(self, f: Dict[str, Any]) -> bool:
        r = self.apollo.poll(f["request_id"])
        if r.state == "pending":
            if self.clock() > f["deadline"]:
                # Still pending at the deadline: keep the worst case reserved (it may yet be billed).
                self.store.settle_reveal(f["ledger_id"], state="failed", charged=self.cfg.max_credits_per_reveal,
                                         reported=None, phone_returned=False, callable_=False, outcome="poll_timeout")
                self._reject("poll_timeout")
                return True
            f["next_poll"] = self.clock() + max(2.0, min(r.retry_after, 30.0))
            return False
        if r.state == "failed":
            self.store.settle_reveal(f["ledger_id"], state="failed", charged=1 if f["person"] else 0, reported=None,
                                     phone_returned=False, callable_=False, outcome=f"poll_failed_{r.payload.get('status')}")
            self._reject("poll_failed")
            self._fail_streak += 1
            return True
        self._fail_streak = 0
        result = (r.payload.get("webhook_result") or {}) if isinstance(r.payload, dict) else {}
        people = [p for p in (result.get("people") or []) if isinstance(p, dict)]
        reported = _reported_credits(r.payload)
        company_phones = company_phones_of(f["person"])
        blocked = self.store.used_phones() | self.william.phones | {k[6:] for k in self.store.suppressed() if k.startswith("phone:")}
        phone, reason, any_number = best_callable_phone(people, company_phones=company_phones, blocked_phones=blocked)
        mobile = any(n.get("type_cd") == "mobile" for p in people for n in (p.get("phone_numbers") or []))
        charged = reported if reported is not None else (1 if f["person"] else 0) + (8 if mobile else 0)
        accepted, why = False, reason
        if phone:
            accepted, why = self._validate(f, phone)
        self.store.settle_reveal(f["ledger_id"], state="done", charged=charged, reported=reported,
                                 phone_returned=any_number, callable_=accepted, outcome=why or "accepted")
        if not accepted:
            self._reject(why)
        return True

    def _validate(self, f: Dict[str, Any], phone: Dict[str, Any]):
        cohort, cand, person = f["cohort"], f["cand"], f["person"]
        unit = cand["unit"]
        if self.store.count_active(cohort) >= self.cfg.target_per_cohort:
            self.stats["surplus_callable_not_used"] += 1
            return False, "cohort_already_full"
        employer_domains = {norm_domain(unit.get("employer_domain")), norm_domain(cand.get("organization_domain"))} - {""}
        confirm_unit = dict(unit)
        ok_employer = current_employer_confirmed(person, confirm_unit)
        if not ok_employer and cohort == EMAIL_FOLLOW_UP:
            # The core accepted this person's corroborated organisation domain at approval.
            for d in employer_domains:
                if current_employer_confirmed(person, {**unit, "employer_domain": d}):
                    ok_employer = True
                    break
        if not ok_employer:
            return False, "current_employer_not_confirmed"
        if str(person.get("country") or "United States") not in ("United States", "US"):
            return False, "not_us_person"
        title = person.get("title") or cand.get("title") or cand.get("search_title")
        persona = classify(title, unit["function_key"], _int(unit.get("employee_count")),
                           founder_max_employees=self.cfg.founder_max_employees)
        if cohort == CALL_FIRST and persona is None:
            return False, "not_a_decision_maker"
        persona = persona or cand.get("persona") or FUNCTIONAL
        if cohort == EMAIL_FOLLOW_UP:
            # The core's verified identity is the one in Instantly; the enrichment only adds the phone.
            email = norm_email(cand.get("email"))
            linkedin = cand.get("linkedin_url") or person.get("linkedin_url")
            first, last = cand.get("first_name") or person.get("first_name"), cand.get("last_name") or person.get("last_name")
        else:
            email = norm_email(person.get("email"))
            linkedin = person.get("linkedin_url")
            first, last = person.get("first_name"), person.get("last_name")
        nck = name_company_key(first, last, unit.get("employer_name"))
        if self.william.hit(email=email, linkedin=linkedin, phone=phone["phone_e164"], name_company=nck):
            return False, "william_list"
        pk = cand["person_key"]
        keys = [f"person:{pk}", f"phone:{phone['phone_e164']}", f"email:{email}" if email else "",
                f"li:{norm_linkedin(linkedin)}" if linkedin else ""]
        if self.store.is_suppressed(*keys):
            return False, "suppressed"
        if cohort == CALL_FIRST:
            if self.core.hit(email=email, linkedin=linkedin, apollo_id=cand["apollo_person_id"]):
                return False, "already_in_email_pipeline"
            if self.crm.hit(email=email, linkedin=linkedin):
                return False, "already_in_crm"
            if instantly_history(self.instantly, email=email, first=str(first or ""), last=str(last or ""),
                                 domain=norm_domain(unit.get("employer_domain"))):
                return False, "instantly_history"
        m = {"cohort": cohort, "opportunity_id": int(unit["opportunity_id"]), "employer_id": int(unit["employer_id"]),
             "campaign_key": unit.get("campaign_key"), "persona": persona,
             "account_email_exposed": True if cohort == EMAIL_FOLLOW_UP else bool(cand.get("email_exposed_account")),
             "prior_email_status": cand.get("prior_email_status") if cohort == EMAIL_FOLLOW_UP else "never_emailed",
             "last_email_at": cand.get("last_email_at"), "suggested_opener": suggested_opener(cohort, persona),
             "job_title": unit.get("job_title"), "job_url": unit.get("job_url"), "hiring_signal": hiring_signal_text(unit)}
        p = {"person_key": pk, "apollo_person_id": cand["apollo_person_id"], "linkedin": norm_linkedin(linkedin),
             "email": email, "first_name": first, "last_name": last, "title": title,
             "employer_id": int(unit["employer_id"]), "employer_name": unit.get("employer_name"),
             "employer_domain": norm_domain(unit.get("employer_domain"))}
        try:
            self.store.add_member(person=p, membership=m, phone={**phone, "source": "apollo_reveal"})
        except ValueError as exc:
            return False, f"duplicate:{exc}"
        return True, ""

    def summary(self, exhausted, cap_hit) -> Dict[str, Any]:
        members = [m for m in self.store.members() if m["status"] == "active"]
        return {"completed": {c: sum(1 for m in members if m["cohort"] == c) for c in (EMAIL_FOLLOW_UP, CALL_FIRST)},
                "queues_exhausted": exhausted, "credit_cap_hit": cap_hit, "ledger": self.store.ledger_summary(),
                **self.stats}


def _reported_credits(payload: Dict[str, Any]) -> Optional[int]:
    for holder in (payload, payload.get("webhook_result") or {}):
        for k in ("credits_consumed", "credits_used", "credit_count"):
            v = holder.get(k) if isinstance(holder, dict) else None
            if isinstance(v, (int, float)):
                return int(v)
    return None


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
