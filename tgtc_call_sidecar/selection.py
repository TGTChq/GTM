"""Cohort eligibility, ranking and phone validation. Pure: no I/O, inputs never mutated."""
from __future__ import annotations

import csv
import glob
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import CALL_FIRST, CHALLENGER_CAMPAIGNS, CHALLENGER_IDS, CONTROL_IDS, EMAIL_FOLLOW_UP
from .identity import name_company_key, norm_domain, norm_email, norm_linkedin, norm_phone_us, person_key
from .personas import EXECUTIVE, FUNCTIONAL, PRIORITY, TALENT, classify


# --- exclusions ------------------------------------------------------------------
@dataclass
class Exclusions:
    emails: set = field(default_factory=set)
    linkedins: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    apollo_ids: set = field(default_factory=set)
    name_company: set = field(default_factory=set)

    def hit(self, *, email="", linkedin="", phone="", apollo_id="", name_company="") -> str:
        if email and norm_email(email) in self.emails:
            return "email"
        if linkedin and norm_linkedin(linkedin) in self.linkedins:
            return "linkedin"
        if phone and phone in self.phones:
            return "phone"
        if apollo_id and apollo_id in self.apollo_ids:
            return "apollo_id"
        if name_company and name_company in self.name_company:
            return "name_company"
        return ""


_DNC_NOTE = re.compile(r"do\s*not\s*call|\bdnc\b|remove me|stop calling|wrong\s*(?:number|person)", re.I)


def william_exclusions(paths: Iterable[str]) -> Tuple[Exclusions, set]:
    """Everyone on William's previous lists, by every identity key they carry.
    Returns (exclusions, phones flagged do-not-call / wrong-number in the call notes)."""
    ex, flagged = Exclusions(), set()
    for path in paths:
        for row in _rows(path):
            low = {str(k or "").strip().lower(): v for k, v in row.items()}
            email = low.get("work_email") or low.get("work email")
            li = low.get("linkedin_url") or low.get("linkedin")
            first, last = low.get("first_name") or low.get("first name"), low.get("last_name") or low.get("last name")
            company = low.get("company")
            if norm_email(email):
                ex.emails.add(norm_email(email))
            if norm_linkedin(li):
                ex.linkedins.add(norm_linkedin(li))
            key = name_company_key(first, last, company)
            if key:
                ex.name_company.add(key)
            notes = " ".join(str(low.get(k) or "") for k in ("call status", "call notes"))
            for col in ("phone", "alternate phone", "consumer mobile phone", "consumer alt mobile phone 1"):
                p = norm_phone_us(low.get(col))
                if p:
                    ex.phones.add(p)
                    if _DNC_NOTE.search(notes):
                        flagged.add(p)
    return ex, flagged


def _rows(path: str) -> List[Dict[str, Any]]:
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    out = []
    for ws in wb.worksheets:
        it = ws.iter_rows(values_only=True)
        hdr = [str(h or "") for h in next(it, ())]
        out.extend({hdr[i]: v for i, v in enumerate(r) if i < len(hdr)} for r in it)
    return out


def default_william_paths(downloads: str) -> List[str]:
    return sorted(glob.glob(os.path.join(downloads, "TGTC_William_*.csv")) +
                  glob.glob(os.path.join(downloads, "TGTC_William_*.xlsx")))


# --- the hiring signal -----------------------------------------------------------
def signal_valid(unit: Optional[Dict[str, Any]], closed_signals: set) -> bool:
    return bool(unit) and unit.get("opp_state") in ("open", "approved") and int(unit.get("active_postings") or 0) > 0 \
        and int(unit["opportunity_id"]) not in closed_signals and str(unit.get("company_country") or "US") in ("US", "")


def hiring_signal_text(unit: Dict[str, Any]) -> str:
    posted = str(unit.get("date_posted") or "")[:10]
    n = int(unit.get("active_postings") or 0)
    return f"{unit.get('job_title') or 'open role'}" + (f" (posted {posted})" if posted else "") + \
        (f"; {n} active postings" if n > 1 else "")


# --- EMAIL_FOLLOW_UP ---------------------------------------------------------------
def follow_up_status(lead: Optional[Dict[str, Any]], *, replied_emails: set, bounced_emails: set,
                     unsubscribed_emails: set) -> str:
    """'' when an Instantly lead is a valid follow-up (emailed, no reply, no bounce/unsubscribe), else why not."""
    if not lead:
        return "not_in_instantly"
    email = norm_email(lead.get("email"))
    if lead.get("campaign") not in CHALLENGER_IDS or lead.get("campaign") in CONTROL_IDS:
        return "not_challenger"
    if not lead.get("timestamp_last_contact"):
        return "never_emailed"
    if int(lead.get("email_reply_count") or 0) > 0 or email in replied_emails:
        return "replied"
    if email in bounced_emails or lead.get("status") == -1:
        return "bounced"
    if email in unsubscribed_emails or lead.get("status") in (-2, -3):
        return "unsubscribed_or_skipped"
    interest = lead.get("lt_interest_status")
    if interest not in (None, 0):          # 0 = out-of-office auto-reply; any human status is a reply
        return "interest_status_set"
    return ""


def follow_up_candidates(approved: List[Dict[str, Any]], units: Dict[int, Dict[str, Any]],
                         leads_by_email: Dict[str, Dict[str, Any]], *, replied: set, bounced: set,
                         unsubscribed: set, william: Exclusions, suppressed: set, taken: set,
                         closed_signals: set, founder_max_employees: int = 99
                         ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    out, why = [], {}

    def drop(reason):
        why[reason] = why.get(reason, 0) + 1

    for a in approved:
        email = norm_email(a.get("email"))
        pk = person_key(apollo_person_id=a.get("apollo_person_id") or "", linkedin=a.get("linkedin_url"), email=email)
        if a.get("instantly_receipt") not in ("created", "existing", "reconciled"):
            drop("not_in_instantly"); continue
        if a.get("instantly_campaign") in CONTROL_IDS or a.get("instantly_campaign") not in CHALLENGER_IDS:
            drop("control_or_unknown_campaign"); continue
        if str(a.get("contact_country") or "") != "US":
            drop("not_us"); continue
        if not a.get("apollo_person_id"):
            drop("no_apollo_id"); continue
        reason = follow_up_status(leads_by_email.get(email), replied_emails=replied, bounced_emails=bounced,
                                  unsubscribed_emails=unsubscribed)
        if reason:
            drop(reason); continue
        unit = units.get(int(a["opportunity_id"]))
        if not signal_valid(unit, closed_signals):
            drop("hiring_signal_invalid"); continue
        if william.hit(email=email, linkedin=a.get("linkedin_url"),
                       name_company=name_company_key(a.get("first_name"), a.get("last_name"), unit.get("employer_name"))):
            drop("william_list"); continue
        if pk in taken or f"person:{pk}" in suppressed or f"email:{email}" in suppressed:
            drop("already_in_sidecar_or_suppressed"); continue
        persona = classify(a.get("title"), unit["function_key"], _int(unit.get("employee_count")),
                           founder_max_employees=founder_max_employees) or FUNCTIONAL
        lead = leads_by_email[email]
        out.append({**a, "person_key": pk, "email": email, "unit": unit, "persona": persona,
                    "last_email_at": lead.get("timestamp_last_contact"),
                    "prior_email_status": "emailed_no_reply" + ("_sequence_completed" if lead.get("status") == 3 else "")})
    # Functional owners first; within a persona, the most recently emailed first (still warm).
    out.sort(key=lambda c: str(c["last_email_at"] or ""), reverse=True)
    out.sort(key=lambda c: PRIORITY.get(c["persona"], 9))
    return out, why


# --- CALL_FIRST ------------------------------------------------------------------
def call_first_units(units: List[Dict[str, Any]], closed_signals: set) -> List[Dict[str, Any]]:
    """One unit per employer (its newest posting); employers with NO email exposure first."""
    valid = [u for u in units if signal_valid(u, closed_signals) and (u.get("employer_domain") or u.get("apollo_org_id"))]
    valid.sort(key=lambda u: str(u.get("date_posted") or ""), reverse=True)
    valid.sort(key=_exposed)
    seen, out = set(), []
    for u in valid:
        if int(u["employer_id"]) not in seen:
            seen.add(int(u["employer_id"]))
            out.append(u)
    return out


def _exposed(u) -> int:
    return 1 if str(u.get("email_exposed")).lower() in ("t", "true", "1") else 0


def pick_call_first(people: List[Dict[str, Any]], unit: Dict[str, Any], *, core: Exclusions, crm: Exclusions,
                    william: Exclusions, suppressed: set, taken: set, want_persona: Optional[str],
                    founder_max_employees: int = 99) -> Optional[Dict[str, Any]]:
    """The best never-contacted decision-maker at this company from free search results."""
    ranked = []
    for p in people:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        pk = person_key(apollo_person_id=pid)
        if pk in taken or f"person:{pk}" in suppressed:
            continue
        if core.hit(apollo_id=pid, linkedin=p.get("linkedin_url")) or crm.hit(linkedin=p.get("linkedin_url")) \
                or william.hit(linkedin=p.get("linkedin_url")):
            continue
        persona = classify(p.get("title"), unit["function_key"], _int(unit.get("employee_count")),
                           founder_max_employees=founder_max_employees)
        if persona is None:
            continue
        direct = direct_phone_flag(p)
        if direct == "no":
            continue          # Apollo says it has no direct number: a reveal would buy nothing
        ranked.append(((0 if want_persona and persona == want_persona else 1), PRIORITY[persona],
                       {"yes": 0, "maybe": 1}.get(direct, 2), pid, p, persona))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[:4])
    _, _, _, _, p, persona = ranked[0]
    return {"apollo_person_id": str(p["id"]), "person_key": person_key(apollo_person_id=str(p["id"])),
            "persona": persona, "search_title": p.get("title"), "unit": unit, "direct_phone": direct_phone_flag(p)}


def direct_phone_flag(search_person: Dict[str, Any]) -> str:
    """Apollo search's has_direct_phone: 'yes' | 'maybe' | 'no' | 'unknown'."""
    v = str(search_person.get("has_direct_phone") or "").strip().lower()
    if v.startswith("yes") or v == "true":
        return "yes"
    if v.startswith("maybe"):
        return "maybe"
    if v in ("no", "false"):
        return "no"
    return "unknown"


# --- after the reveal ---------------------------------------------------------------
CALLABLE_TYPES = ("mobile", "work_direct")


def best_callable_phone(people_payload: List[Dict[str, Any]], *, company_phones: set, blocked_phones: set
                        ) -> Tuple[Optional[Dict[str, Any]], str, bool]:
    """(phone, reason, any_number_returned). Only a direct line counts: never a switchboard,
    never an invalid or do-not-call number, never a phone already used or blocked."""
    numbers = []
    for person in people_payload or []:
        numbers.extend(person.get("phone_numbers") or [])
    if not numbers:
        return None, "no_phone_returned", False
    last = "no_callable_type"
    for n in sorted(numbers, key=lambda n: (CALLABLE_TYPES.index(n.get("type_cd")) if n.get("type_cd") in CALLABLE_TYPES
                                            else 9, 0 if str(n.get("confidence_cd") or "").lower() == "high" else 1)):
        if n.get("type_cd") not in CALLABLE_TYPES:
            last = "switchboard_or_other_type"; continue
        e164 = norm_phone_us(n.get("sanitized_number") or n.get("raw_number"))
        if not e164:
            last = "not_us_e164"; continue
        if "invalid" in str(n.get("status_cd") or "").lower():
            last = "invalid_status"; continue
        dnc = str(n.get("dnc_status_cd") or "").lower()
        if dnc and "not" not in dnc and dnc not in ("clean", "unknown", "none"):
            last = "dnc_flagged"; continue
        if e164 in company_phones:
            last = "company_switchboard"; continue
        if e164 in blocked_phones:
            last = "phone_already_used_or_blocked"; continue
        return {"phone_e164": e164, "type": n.get("type_cd"), "status": n.get("status_cd"),
                "dnc_status": n.get("dnc_status_cd"), "confidence": n.get("confidence_cd")}, "", True
    return None, last, True


def current_employer_confirmed(person: Dict[str, Any], unit: Dict[str, Any]) -> bool:
    """The enriched person's CURRENT organisation is the employer (domain or Apollo org id)."""
    org = person.get("organization") or {}
    want_domain = norm_domain(unit.get("employer_domain"))
    want_org = str(unit.get("apollo_org_id") or "")
    domains = {norm_domain(org.get(k)) for k in ("primary_domain", "website_url", "domain")} - {""}
    if want_org and str(org.get("id") or person.get("organization_id") or "") == want_org:
        return _current_role_at(person, org)
    if want_domain and want_domain in domains:
        return _current_role_at(person, org)
    return False


def _current_role_at(person: Dict[str, Any], org: Dict[str, Any]) -> bool:
    hist = person.get("employment_history") or []
    if not hist:
        return True   # Apollo's current organisation itself is the evidence
    oid = str(org.get("id") or "")
    return any(h.get("current") and (not oid or str(h.get("organization_id") or "") == oid) for h in hist)


def company_phones_of(person: Dict[str, Any]) -> set:
    org = person.get("organization") or {}
    out = set()
    for v in (org.get("sanitized_phone"), org.get("phone"), (org.get("primary_phone") or {}).get("sanitized_number")
              if isinstance(org.get("primary_phone"), dict) else None):
        p = norm_phone_us(v)
        if p:
            out.add(p)
    return out


def suggested_opener(cohort: str, persona: str) -> str:
    if cohort == EMAIL_FOLLOW_UP:
        return "email_follow_up_on_open_role"
    return {FUNCTIONAL: "hiring_signal_team_growth", TALENT: "hiring_signal_search_support",
            EXECUTIVE: "hiring_signal_founder_capacity"}.get(persona, "hiring_signal_team_growth")


def campaign_of(unit: Dict[str, Any]) -> str:
    return unit.get("campaign_key") if unit.get("campaign_key") in CHALLENGER_CAMPAIGNS else ""


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = ["Exclusions", "william_exclusions", "follow_up_candidates", "call_first_units", "pick_call_first",
           "best_callable_phone", "current_employer_confirmed", "company_phones_of", "suggested_opener",
           "signal_valid", "hiring_signal_text", "follow_up_status", "campaign_of", "EMAIL_FOLLOW_UP", "CALL_FIRST",
           "TALENT", "FUNCTIONAL", "EXECUTIVE"]
