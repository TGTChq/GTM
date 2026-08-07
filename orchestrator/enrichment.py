"""Company / contact enrichment as injected interfaces.

The existing domain logic (company identity, ICP qualification, CRM +
suppression, company resolution, hiring-manager matching, contact discovery,
email verification, final gates) is integrated here **behind a Protocol**, not
imported for its side effects. Production supplies real adapters; offline supplies
``FakeEnrichmentAdapter``. Either way the engine, its stage reconciliation and its
loss instrumentation are identical.

Every material loss is counted with a reason code:
company_unresolved, company_size_rejected, not_icp, in_crm,
hiring_manager_not_found, contact_not_found, email_unverified,
duplicate_contact, no_active_hiring_signal, not_same_day_eligible, suppressed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

from orchestrator.reasons import Disposition, ReasonCode, StageOutcome
from orchestrator.waterfall import StageResult, reconcile_stage


class EnrichmentAdapter(Protocol):
    def resolve_company(self, posting: Dict[str, Any]) -> Optional[Dict[str, Any]]: ...
    def qualify_icp(self, company: Dict[str, Any]) -> Tuple[bool, Optional[ReasonCode]]: ...
    def crm_or_suppressed(self, company: Dict[str, Any]) -> Optional[ReasonCode]: ...
    def match_hiring_manager(self, posting: Dict[str, Any], company: Dict[str, Any]) -> Optional[Dict[str, Any]]: ...
    def discover_contact(self, manager: Dict[str, Any], company: Dict[str, Any]) -> Optional[Dict[str, Any]]: ...
    def verify_email(self, contact: Dict[str, Any]) -> Tuple[bool, str]: ...
    def has_active_hiring_signal(self, posting: Dict[str, Any]) -> bool: ...
    def same_day_eligible(self, posting: Dict[str, Any]) -> bool: ...


#: Dispositions that are SAFE-TERMINAL for cross-run posting suppression: a
#: delivered/qualified FINAL_PASS or a genuine business REJECT (ICP/size/founded/
#: industry/CRM/duplicate). Provider-deferred outcomes (NEEDS_CHECK, UNVERIFIED,
#: REROUTE) are deliberately excluded so an Apollo/Hunter outage never permanently
#: suppresses a posting that was never really processed (Defect B).
TERMINAL_DISPOSITIONS = frozenset({Disposition.FINAL_PASS, Disposition.REJECT})


@dataclass
class Lead:
    posting_id: str
    company: Dict[str, Any]
    contact: Dict[str, Any]
    disposition: Disposition
    primary_reason: ReasonCode
    email_status: str = ""
    contact_key: str = ""
    #: Other postings folded into this lead (company+bucket dedup) -- carried so
    #: cross-run suppression can mark every genuinely-processed posting, not just
    #: the primary one, without over-suppressing deferred work.
    related_posting_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "posting_id": self.posting_id,
            "company": self.company.get("name", ""),
            "contact": self.contact.get("email", ""),
            "disposition": self.disposition.value,
            "primary_reason": self.primary_reason.value,
            "email_status": self.email_status,
            "contact_key": self.contact_key,
        }


@dataclass
class EnrichmentReport:
    leads: List[Lead] = field(default_factory=list)
    stages: List[StageResult] = field(default_factory=list)
    loss_census: Dict[str, int] = field(default_factory=dict)

    def dispositions(self) -> List[Disposition]:
        return [lead.disposition for lead in self.leads]

    def final_pass(self) -> List[Lead]:
        return [l for l in self.leads if l.disposition is Disposition.FINAL_PASS]

    def terminal_posting_ids(self) -> set:
        """Postings that reached a SAFE-TERMINAL outcome and may be committed to
        cross-run suppression. Deferred outcomes are excluded so a provider outage
        never permanently suppresses an unprocessed posting (Defect B)."""
        out: set = set()
        for lead in self.leads:
            if lead.disposition not in TERMINAL_DISPOSITIONS:
                continue
            if lead.posting_id:
                out.add(str(lead.posting_id))
            for pid in lead.related_posting_ids:
                if pid:
                    out.add(str(pid))
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "leads": len(self.leads),
            "final_pass": len(self.final_pass()),
            "loss_census": dict(sorted(self.loss_census.items())),
            "stages": [s.to_dict() for s in self.stages],
            "sample": [l.to_dict() for l in self.leads[:10]],
        }


class EnrichmentEngine:
    """Runs postings through the enrichment adapter, reconciling every boundary
    and never weakening a quality rule to reach volume."""

    def __init__(self, adapter: EnrichmentAdapter) -> None:
        self.adapter = adapter

    def run(self, opportunities: List[Dict[str, Any]]) -> EnrichmentReport:
        loss: Counter = Counter()
        leads: List[Lead] = []
        seen_contacts: set = set()

        company_dispo: List[Tuple[StageOutcome, ReasonCode, Optional[ReasonCode]]] = []
        contact_dispo: List[Tuple[StageOutcome, ReasonCode, Optional[ReasonCode]]] = []
        gate_dispo: List[Tuple[StageOutcome, ReasonCode, Optional[ReasonCode]]] = []

        for opp in opportunities:
            pid = str(opp.get("posting_id") or opp.get("job_id") or "")
            company = self.adapter.resolve_company(opp)
            if company is None:
                loss[ReasonCode.COMPANY_UNRESOLVED.value] += 1
                company_dispo.append((StageOutcome.REJECTED, ReasonCode.COMPANY_UNRESOLVED, None))
                leads.append(Lead(pid, {}, {}, Disposition.REROUTE, ReasonCode.COMPANY_UNRESOLVED))
                continue
            icp_ok, icp_reason = self.adapter.qualify_icp(company)
            if not icp_ok:
                reason = icp_reason or ReasonCode.NOT_ICP
                loss[reason.value] += 1
                company_dispo.append((StageOutcome.REJECTED, reason, None))
                leads.append(Lead(pid, company, {}, Disposition.REJECT, reason))
                continue
            supp = self.adapter.crm_or_suppressed(company)
            if supp is not None:
                loss[supp.value] += 1
                company_dispo.append((StageOutcome.REJECTED, supp, None))
                leads.append(Lead(pid, company, {}, Disposition.REJECT, supp))
                continue
            company_dispo.append((StageOutcome.PASSED, ReasonCode.OK, None))

            manager = self.adapter.match_hiring_manager(opp, company)
            if manager is None:
                loss[ReasonCode.HIRING_MANAGER_NOT_FOUND.value] += 1
                contact_dispo.append((StageOutcome.DEFERRED, ReasonCode.HIRING_MANAGER_NOT_FOUND, None))
                leads.append(Lead(pid, company, {}, Disposition.NEEDS_CHECK, ReasonCode.HIRING_MANAGER_NOT_FOUND))
                continue
            contact = self.adapter.discover_contact(manager, company)
            if contact is None:
                loss[ReasonCode.CONTACT_NOT_FOUND.value] += 1
                contact_dispo.append((StageOutcome.DEFERRED, ReasonCode.CONTACT_NOT_FOUND, None))
                leads.append(Lead(pid, company, {}, Disposition.NEEDS_CHECK, ReasonCode.CONTACT_NOT_FOUND))
                continue
            contact_key = str(contact.get("email") or contact.get("id") or "").lower()
            if contact_key in seen_contacts:
                loss[ReasonCode.DUPLICATE_CONTACT.value] += 1
                contact_dispo.append((StageOutcome.REJECTED, ReasonCode.DUPLICATE_CONTACT, None))
                leads.append(Lead(pid, company, contact, Disposition.REJECT,
                                  ReasonCode.DUPLICATE_CONTACT, contact_key=contact_key))
                continue
            email_ok, email_status = self.adapter.verify_email(contact)
            if not email_ok:
                loss[ReasonCode.EMAIL_UNVERIFIED.value] += 1
                contact_dispo.append((StageOutcome.DEFERRED, ReasonCode.EMAIL_UNVERIFIED, None))
                leads.append(Lead(pid, company, contact, Disposition.UNVERIFIED,
                                  ReasonCode.EMAIL_UNVERIFIED, email_status=email_status,
                                  contact_key=contact_key))
                continue
            contact_dispo.append((StageOutcome.PASSED, ReasonCode.OK, None))

            # Final gates -- all must hold for FINAL_PASS.
            if not self.adapter.has_active_hiring_signal(opp):
                loss[ReasonCode.NO_ACTIVE_HIRING_SIGNAL.value] += 1
                gate_dispo.append((StageOutcome.REJECTED, ReasonCode.NO_ACTIVE_HIRING_SIGNAL, None))
                leads.append(Lead(pid, company, contact, Disposition.REJECT,
                                  ReasonCode.NO_ACTIVE_HIRING_SIGNAL, email_status=email_status,
                                  contact_key=contact_key))
                continue
            if not self.adapter.same_day_eligible(opp):
                loss[ReasonCode.NOT_SAME_DAY_ELIGIBLE.value] += 1
                gate_dispo.append((StageOutcome.DEFERRED, ReasonCode.NOT_SAME_DAY_ELIGIBLE, None))
                leads.append(Lead(pid, company, contact, Disposition.NEEDS_CHECK,
                                  ReasonCode.NOT_SAME_DAY_ELIGIBLE, email_status=email_status,
                                  contact_key=contact_key))
                continue

            seen_contacts.add(contact_key)
            gate_dispo.append((StageOutcome.PASSED, ReasonCode.OK, None))
            leads.append(Lead(pid, company, contact, Disposition.FINAL_PASS, ReasonCode.OK,
                              email_status=email_status, contact_key=contact_key))

        stages = [
            reconcile_stage("company_qualification", "opportunity", company_dispo),
            reconcile_stage("contact_discovery", "contact", contact_dispo),
            reconcile_stage("final_gates", "contact", gate_dispo),
        ]
        return EnrichmentReport(leads=leads, stages=stages, loss_census=dict(loss))


# --------------------------------------------------------------------------
# Deterministic fake for offline modes and tests
# --------------------------------------------------------------------------


@dataclass
class FakeEnrichmentAdapter:
    """A deterministic adapter with tunable yield, so tests can drive any mix of
    dispositions without touching the network. Keyed off posting fields, never
    randomised."""

    icp_min_size: int = 10
    icp_max_size: int = 5000
    suppress_domains: frozenset = frozenset()
    unverifiable_domains: frozenset = frozenset()
    no_manager_companies: frozenset = frozenset()

    def resolve_company(self, posting: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        name = str(posting.get("employer_name") or posting.get("company_name") or "").strip()
        if not name:
            return None
        domain = str(posting.get("company_domain") or (name.lower().replace(" ", "") + ".com"))
        return {"name": name, "domain": domain, "size": int(posting.get("company_size") or 100)}

    def qualify_icp(self, company: Dict[str, Any]) -> Tuple[bool, Optional[ReasonCode]]:
        size = int(company.get("size") or 0)
        if size < self.icp_min_size or size > self.icp_max_size:
            return False, ReasonCode.COMPANY_SIZE_REJECTED
        if company.get("not_icp"):
            return False, ReasonCode.NOT_ICP
        return True, None

    def crm_or_suppressed(self, company: Dict[str, Any]) -> Optional[ReasonCode]:
        if company.get("domain") in self.suppress_domains:
            return ReasonCode.SUPPRESSED
        if company.get("in_crm"):
            return ReasonCode.IN_CRM
        return None

    def match_hiring_manager(self, posting: Dict[str, Any], company: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if company.get("name") in self.no_manager_companies:
            return None
        return {"name": f"HM {company['name']}", "title": "VP Engineering"}

    def discover_contact(self, manager: Dict[str, Any], company: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        first = manager["name"].split()[0].lower()
        return {"id": f"{first}@{company['domain']}", "email": f"{first}@{company['domain']}"}

    def verify_email(self, contact: Dict[str, Any]) -> Tuple[bool, str]:
        domain = str(contact.get("email", "")).split("@")[-1]
        if domain in self.unverifiable_domains:
            return False, "unverifiable"
        return True, "verified"

    def has_active_hiring_signal(self, posting: Dict[str, Any]) -> bool:
        return bool(posting.get("active_hiring", True))

    def same_day_eligible(self, posting: Dict[str, Any]) -> bool:
        return bool(posting.get("same_day", True))
