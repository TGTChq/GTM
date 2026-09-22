"""GTM Call List Sidecar: isolation, eligibility and completion rules.

Every provider is a fake here; nothing touches a network or a production table.
"""
from __future__ import annotations

import copy
import os
import pathlib
import sqlite3

import pytest

from tgtc_call_sidecar import __main__ as cli
from tgtc_call_sidecar.config import CALL_FIRST, CHALLENGER_CAMPAIGNS, EMAIL_FOLLOW_UP, PilotConfig
from tgtc_call_sidecar.pilot import Pilot
from tgtc_call_sidecar.providers import ForbiddenCall, PollResult, check_allowed
from tgtc_call_sidecar.selection import (Exclusions, best_callable_phone, follow_up_candidates, follow_up_status,
                                         william_exclusions)
from tgtc_call_sidecar.store import CreditCapExceeded, SidecarStore

OPS = CHALLENGER_CAMPAIGNS["operations"]
CONTROL_OPS = "4effab2f-9073-46a9-b7ae-986ccc8f49c6"
ROOT = pathlib.Path(__file__).resolve().parents[1]


# --- a small world ---------------------------------------------------------------
def unit(opp, emp, *, domain, exposed="f", fk="operations", title="Operations Manager", employees=200):
    return {"opportunity_id": str(opp), "function_key": fk, "campaign_key": "operations", "opp_state": "open",
            "employer_id": str(emp), "employer_name": f"Co{emp}", "employer_domain": domain, "linkedin_slug": "",
            "apollo_org_id": "", "employee_count": str(employees), "company_country": "US", "job_title": title,
            "job_url": f"https://jobs.test/{opp}", "date_posted": "2026-09-20", "source": "fantastic",
            "active_postings": "1", "email_exposed": exposed, "unit_approvals": "1", "waiting_on": ""}


def approval(i, opp, emp, *, campaign=OPS, title="Director of Operations", country="US"):
    return {"approval_id": str(i), "opportunity_id": str(opp), "approval_state": "delivered", "campaign_key": "operations",
            "campaign_id": campaign, "outreach_eligible": "t", "contact_country": country,
            "approved_at": "2026-09-21", "employer_id": str(emp), "apollo_person_id": f"ap{i}", "first_name": f"F{i}",
            "last_name": f"L{i}", "title": title, "linkedin_url": f"https://www.linkedin.com/in/p{i}",
            "email": f"p{i}@co{emp}.com", "email_status": "verified", "organization_domain": f"co{emp}.com",
            "seniority": "director", "instantly_campaign": campaign, "instantly_receipt": "created",
            "instantly_at": "2026-09-21"}


def lead(email, campaign=OPS, *, contacted=True, replies=0, status=1, interest=None):
    return {"email": email, "campaign": campaign, "timestamp_last_contact": "2026-09-21T15:00:00Z" if contacted else None,
            "email_reply_count": replies, "status": status, "lt_interest_status": interest}


class FakeApollo:
    """Search returns people per domain; reveal+poll return a phone per person id."""

    def __init__(self, people_by_domain, phones, employer_of, pending_rounds=1):
        self.people_by_domain, self.phones, self.employer_of = people_by_domain, phones, employer_of
        self.pending = {}
        self.pending_rounds = pending_rounds
        self.reveals = []

    def search(self, *, domain="", organization_id="", titles, per_page=25, page=1, similar_titles=True):
        return list(self.people_by_domain.get(domain, []))

    def reveal(self, person_id):
        self.reveals.append(person_id)
        dom = self.employer_of[person_id]
        self.pending[person_id] = self.pending_rounds
        return {"status": 200, "body": {"request_id": person_id, "person": {
            "id": person_id, "first_name": "N" + person_id, "last_name": "M" + person_id,
            "title": self.people_title(person_id), "email": f"{person_id}@{dom}", "country": "United States",
            "linkedin_url": f"https://linkedin.com/in/{person_id}",
            "organization": {"primary_domain": dom, "sanitized_phone": "+12125550000"},
            "employment_history": [{"current": True}]}}}

    def people_title(self, pid):
        for ps in self.people_by_domain.values():
            for p in ps:
                if p["id"] == pid:
                    return p["title"]
        return "Director of Operations"

    def poll(self, request_id):
        if self.pending.get(request_id, 0) > 0:
            self.pending[request_id] -= 1
            return PollResult("pending", {"error_code": "result_pending"}, 0)
        nums = self.phones.get(request_id, [])
        return PollResult("ready", {"webhook_result": {"people": [{"id": request_id, "phone_numbers": nums}]}})


class FakeInstantly:
    def __init__(self, existing_emails=()):
        self.existing = set(existing_emails)
        self.writes = 0

    def workspace_search(self, text):
        return [{"email": text}] if text in self.existing else []


def mobile(n):
    return [{"sanitized_number": f"+1415555{n:04d}", "type_cd": "mobile", "status_cd": "valid_number",
             "dnc_status_cd": "not_found", "confidence_cd": "high"}]


def world(tmp_path, *, n_follow=3, n_first=3, william=None, instantly_existing=(), phones=None, cap=2000, target=3):
    units, approved, leads = [], [], {}
    for i in range(n_follow):
        units.append(unit(100 + i, 10 + i, domain=f"co{10 + i}.com", exposed="t"))
        a = approval(i, 100 + i, 10 + i)
        approved.append(a)
        leads[a["email"]] = lead(a["email"])
    people_by_domain, employer_of = {}, {}
    for j in range(n_first):
        d = f"new{j}.com"
        units.append(unit(200 + j, 50 + j, domain=d))
        pid = f"cf{j}"
        people_by_domain[d] = [{"id": pid, "title": "Head of Talent Acquisition" if j % 2 else "Operations Director",
                                "has_direct_phone": "Yes"}]
        employer_of[pid] = d
    for a in approved:
        employer_of[a["apollo_person_id"]] = a["organization_domain"]
    if phones is None:
        phones = {pid: mobile(k) for k, pid in enumerate(employer_of)}
    store = SidecarStore(str(tmp_path / "state.sqlite"))
    apollo = FakeApollo(people_by_domain, phones, employer_of)
    inst = FakeInstantly(instantly_existing)
    core = Exclusions(apollo_ids={a["apollo_person_id"] for a in approved}, emails={a["email"] for a in approved})
    pilot = Pilot(PilotConfig(target_per_cohort=target, apollo_credit_cap=cap, max_in_flight=4), store, apollo, inst,
                  units=units, units_by_opp={int(u["opportunity_id"]): u for u in units}, approved=approved, core=core,
                  crm=Exclusions(), william=william or Exclusions(), william_flagged=set(), leads_by_email=leads,
                  replied=set(), bounced=set(), unsubscribed=set(), log=lambda m: None, clock=_Clock(), sleep=lambda s: None)
    return pilot, store, apollo, approved


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 10.0
        return self.t


# --- isolation -------------------------------------------------------------------
def test_core_never_imports_the_sidecar():
    for path in (ROOT / "tgtc_core").rglob("*.py"):
        assert "tgtc_call_sidecar" not in path.read_text(encoding="utf-8"), path


def test_sidecar_off_by_default_does_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("TGTC_CALL_LIST_SIDECAR", raising=False)
    state = tmp_path / "state.sqlite"
    rc = cli.main(["pilot", "--inputs", str(tmp_path), "--out", str(tmp_path), "--state", str(state),
                   "--william-dir", str(tmp_path)])
    assert rc == 0 and not state.exists()          # no store, no provider client, no network
    monkeypatch.setenv("TGTC_CALL_LIST_SIDECAR", "0")
    assert cli.main(["disposition", "--state", str(state), "--person-key", "x", "--disposition", "no_answer"]) == 0
    assert not state.exists()


def test_running_the_sidecar_leaves_core_policy_byte_identical(tmp_path):
    from tgtc_core.policy import campaigns
    before = copy.deepcopy((campaigns.DIRECT_BUYER_TITLES, campaigns.EXECUTIVE_BUYER_TITLES,
                            campaigns.TALENT_PEOPLE_BUYER_TITLES))
    pilot, *_ = world(tmp_path)
    pilot.run()
    assert (campaigns.DIRECT_BUYER_TITLES, campaigns.EXECUTIVE_BUYER_TITLES,
            campaigns.TALENT_PEOPLE_BUYER_TITLES) == before


def test_sidecar_failure_cannot_fail_or_block_the_core(monkeypatch):
    monkeypatch.setenv("TGTC_CALL_LIST_SIDECAR", "1")

    def boom():
        raise RuntimeError("sidecar exploded")
    assert cli.after_core(boom) == 0
    monkeypatch.setenv("TGTC_CALL_LIST_SIDECAR", "0")
    assert cli.after_core(boom) == 0


# --- no writes to Airtable or Instantly ---------------------------------------------
@pytest.mark.parametrize("method,url", [
    ("POST", "https://api.instantly.ai/api/v2/leads"),
    ("DELETE", "https://api.instantly.ai/api/v2/leads/abc"),
    ("PATCH", "https://api.instantly.ai/api/v2/campaigns/abc"),
    ("POST", "https://api.instantly.ai/api/v2/leads/move"),
    ("POST", "https://api.airtable.com/v0/base/table"),
    ("PATCH", "https://api.airtable.com/v0/base/table"),
    ("DELETE", "https://api.airtable.com/v0/base/table/rec1"),
    ("POST", "https://api.apollo.io/api/v1/contacts"),
])
def test_no_write_call_can_leave_the_sidecar(method, url):
    with pytest.raises(ForbiddenCall):
        check_allowed(method, url)


def test_reads_are_allowed():
    check_allowed("POST", "https://api.instantly.ai/api/v2/leads/list")
    check_allowed("GET", "https://api.airtable.com/v0/base/table")
    check_allowed("GET", "https://api.apollo.io/api/v1/webhook_result/123")


def test_no_contact_is_removed_from_email_delivery(tmp_path):
    pilot, store, apollo, approved = world(tmp_path)
    snapshot = copy.deepcopy(approved)
    pilot.run()
    assert approved == snapshot            # the core's delivery input is untouched


# --- cohorts ---------------------------------------------------------------------------
def test_the_pilot_fills_both_cohorts_with_completed_records(tmp_path):
    pilot, store, apollo, _ = world(tmp_path)
    s = pilot.run()
    assert s["completed"] == {EMAIL_FOLLOW_UP: 3, CALL_FIRST: 3}
    for m in store.members():
        assert m["phone_e164"].startswith("+1") and m["phone_type"] == "mobile"


def test_no_person_enters_both_cohorts(tmp_path):
    pilot, store, *_ = world(tmp_path)
    pilot.run()
    keys = [m["person_key"] for m in store.members()]
    assert len(keys) == len(set(keys))
    m = store.members()[0]
    with pytest.raises(ValueError):
        store.add_member(person={"person_key": m["person_key"]},
                         membership={"cohort": CALL_FIRST if m["cohort"] == EMAIL_FOLLOW_UP else EMAIL_FOLLOW_UP},
                         phone={"phone_e164": "+14155559999"})


def test_control_campaigns_are_excluded():
    a = approval(1, 100, 10, campaign=CONTROL_OPS)
    u = {100: unit(100, 10, domain="co10.com")}
    out, why = follow_up_candidates([a], u, {a["email"]: lead(a["email"], CONTROL_OPS)}, replied=set(), bounced=set(),
                                    unsubscribed=set(), william=Exclusions(), suppressed=set(), taken=set(),
                                    closed_signals=set())
    assert out == [] and why == {"control_or_unknown_campaign": 1}
    assert follow_up_status(lead("x@y.com", CONTROL_OPS), replied_emails=set(), bounced_emails=set(),
                            unsubscribed_emails=set()) == "not_challenger"


@pytest.mark.parametrize("kw,sets,reason", [
    ({"replies": 1}, {}, "replied"),
    ({}, {"replied_emails": {"x@y.com"}}, "replied"),
    ({"status": -1}, {}, "bounced"),
    ({}, {"unsubscribed_emails": {"x@y.com"}}, "unsubscribed_or_skipped"),
    ({"interest": -1}, {}, "interest_status_set"),
    ({"contacted": False}, {}, "never_emailed"),
])
def test_follow_up_requires_a_sent_email_and_no_reply_bounce_or_unsubscribe(kw, sets, reason):
    base = {"replied_emails": set(), "bounced_emails": set(), "unsubscribed_emails": set()}
    assert follow_up_status(lead("x@y.com", **kw), **{**base, **sets}) == reason
    assert follow_up_status(lead("x@y.com"), **base) == ""


def test_follow_up_members_have_a_sent_email_and_no_reply(tmp_path):
    pilot, store, _, approved = world(tmp_path)
    pilot.leads_by_email[approved[0]["email"]]["email_reply_count"] = 1     # replied -> excluded
    pilot.leads_by_email[approved[1]["email"]]["timestamp_last_contact"] = None  # never emailed -> excluded
    pilot.run()
    fu = [m for m in store.members() if m["cohort"] == EMAIL_FOLLOW_UP]
    assert [m["email"] for m in fu] == [approved[2]["email"]]
    assert fu[0]["prior_email_status"].startswith("emailed_no_reply")


def test_do_not_call_and_former_employee_suppress_future_lists(tmp_path):
    pilot, store, *_ = world(tmp_path)
    pilot.run()
    a, b = store.members()[:2]
    store.record_disposition(a["person_key"], "do_not_call")
    store.record_disposition(b["person_key"], "former_employee")
    sup = store.suppressed()
    assert f"person:{a['person_key']}" in sup and f"phone:{a['phone_e164']}" in sup
    assert f"person:{b['person_key']}" in sup
    # a second pilot never re-selects them
    pilot2, store2, *_ = world(tmp_path)
    pilot2.run()
    active = {m["person_key"] for m in store2.members() if m["status"] == "active"}
    assert a["person_key"] not in active and b["person_key"] not in active


def test_wrong_number_invalidates_only_that_phone(tmp_path):
    pilot, store, *_ = world(tmp_path)
    pilot.run()
    m = store.members()[0]
    store.record_disposition(m["person_key"], "wrong_number")
    after = {x["person_key"]: x for x in store.members()}[m["person_key"]]
    assert after["phone_status"] == "invalid_wrong_number" and after["status"] == "needs_phone"
    assert f"person:{m['person_key']}" not in store.suppressed()


def test_role_filled_and_referral_touch_only_sidecar_state(tmp_path):
    pilot, store, *_ = world(tmp_path)
    pilot.run()
    m = store.members()[0]
    store.record_disposition(m["person_key"], "role_filled")
    store.record_disposition(m["person_key"], "referral", referral={"name": "R", "title": "VP Ops"})
    assert m["opportunity_id"] in store.closed_signals()
    assert store.db.execute("SELECT count(*) FROM referrals WHERE status='pending_validation'").fetchone()[0] == 1


def test_previous_william_list_contacts_are_excluded(tmp_path):
    wl = tmp_path / "TGTC_William_x.csv"
    wl.write_text("first_name,last_name,work_email,linkedin_url,company,phone\n"
                  "F0,L0,p0@co10.com,,Co10,\nX,Y,,https://linkedin.com/in/cf0,New0,\n", encoding="utf-8")
    william, _ = william_exclusions([str(wl)])
    pilot, store, *_ = world(tmp_path, william=william)
    pilot.run()
    emails = {m["email"] for m in store.members()}
    lis = {m["linkedin"] for m in store.members()}
    assert "p0@co10.com" not in emails and "linkedin.com/in/cf0" not in lis


def test_call_first_contacts_have_no_instantly_history(tmp_path):
    pilot, store, *_ = world(tmp_path, instantly_existing={"cf1@new1.com"})
    s = pilot.run()
    cf = {m["apollo_person_id"] for m in store.members() if m["cohort"] == CALL_FIRST}
    assert "cf1" not in cf
    assert s["rejected_after_reveal"].get("instantly_history") == 1


# --- what counts as completed ----------------------------------------------------------
def test_switchboards_and_missing_numbers_never_count(tmp_path):
    hq = [{"sanitized_number": "+12125550000", "type_cd": "work_hq", "status_cd": "valid_number"}]
    as_direct = [{"sanitized_number": "+12125550000", "type_cd": "work_direct", "status_cd": "valid_number"}]
    assert best_callable_phone([{"phone_numbers": hq}], company_phones=set(), blocked_phones=set())[1] == \
        "switchboard_or_other_type"
    assert best_callable_phone([{"phone_numbers": as_direct}], company_phones={"+12125550000"},
                               blocked_phones=set())[1] == "company_switchboard"
    assert best_callable_phone([], company_phones=set(), blocked_phones=set())[1] == "no_phone_returned"
    dnc = [{"sanitized_number": "+14155550001", "type_cd": "mobile", "dnc_status_cd": "dnc"}]
    assert best_callable_phone([{"phone_numbers": dnc}], company_phones=set(), blocked_phones=set())[1] == "dnc_flagged"


def test_people_without_a_callable_phone_are_not_counted(tmp_path):
    pilot, store, apollo, approved = world(tmp_path, phones={})   # every reveal returns no number
    s = pilot.run()
    assert s["completed"] == {EMAIL_FOLLOW_UP: 0, CALL_FIRST: 0}
    assert s["ledger"]["phone_returned"] == 0 and s["ledger"]["callable"] == 0


def test_the_pilot_credit_cap_is_hard(tmp_path):
    store = SidecarStore(str(tmp_path / "s.sqlite"))
    store.reserve_reveal(person_key="a", apollo_person_id="a", cohort=CALL_FIRST, credits=9, cap=10)
    with pytest.raises(CreditCapExceeded):
        store.reserve_reveal(person_key="b", apollo_person_id="b", cohort=CALL_FIRST, credits=9, cap=10)
    os.makedirs(tmp_path / "w", exist_ok=True)
    pilot, store2, apollo, _ = world(tmp_path / "w", cap=27)       # three worst-case reveals only
    s = pilot.run()
    assert len(apollo.reveals) <= 3 and s["credit_cap_hit"] and store2.credits_committed() <= 27


def test_the_store_is_isolated_sqlite_not_a_production_table(tmp_path):
    store = SidecarStore(str(tmp_path / "state.sqlite"))
    names = {r[0] for r in sqlite3.connect(str(tmp_path / "state.sqlite")).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"memberships", "phones", "reveal_ledger", "call_attempts", "referrals", "suppressions"} <= names
    assert not names & {"approvals", "delivery_outbox", "delivery_receipts", "opportunities"}
    store.close()


def test_repeated_reveal_failures_stop_the_pilot(tmp_path):
    pilot, store, apollo, _ = world(tmp_path, n_follow=3, n_first=6)
    apollo.reveal = lambda pid: (apollo.reveals.append(pid), {"status": 422, "body": {}})[1]
    s = pilot.run()
    assert s.get("stopped_by_breaker") and len(apollo.reveals) == 5
    assert s["ledger"]["credits_charged"] == 0


def test_people_apollo_says_have_no_direct_phone_are_never_revealed(tmp_path):
    pilot, store, apollo, _ = world(tmp_path, n_follow=0, n_first=2)
    for ps in apollo.people_by_domain.values():
        for p in ps:
            p["has_direct_phone"] = "No"
    pilot.run()
    assert apollo.reveals == []
