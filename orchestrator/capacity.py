"""Observed per-run outcomes; sustainable capacity requires comparable cohorts.

A lead counts toward the business target only when it is FINAL_PASS -- i.e. it
carries a verified email, a relevant active hiring signal, an ICP-qualified
company, a unique contact identity, passed CRM + suppression checks, and is
same-day outbound-eligible. Those are exactly the gates ``EnrichmentEngine``
enforces for FINAL_PASS, so the capacity report reads them off the reconciled
result rather than re-deriving them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from orchestrator.enrichment import EnrichmentReport

TARGET_FINAL_PASS_PER_DAY = 1000


@dataclass
class CapacityReport:
    raw_postings: int
    opportunities: int
    new_eligible_companies: Optional[int]
    final_pass_leads: int
    delivered_final_pass: Optional[int]
    contacts_per_company: Optional[float]
    raw_to_final_pass_yield: Optional[float]
    acquisition_requests: int
    enrichment_calls: Optional[int]
    runtime_seconds: float
    quota_consumed: int
    inventory_remaining: int
    projected_sustainable_per_day: Optional[int]
    target: int = TARGET_FINAL_PASS_PER_DAY
    eligible_companies_observed: int = 0
    unknown_company_identities: int = 0
    unknown_contact_identities: int = 0

    def meets_target(self) -> bool:
        return self.delivered_final_pass is not None and self.delivered_final_pass >= self.target

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_final_pass_per_day": self.target,
            "raw_postings": self.raw_postings,
            "opportunities": self.opportunities,
            "new_eligible_companies": self.new_eligible_companies,
            "final_pass_leads": self.final_pass_leads,
            "delivered_final_pass": self.delivered_final_pass,
            "contacts_per_company": (round(self.contacts_per_company, 4)
                                     if self.contacts_per_company is not None else None),
            "eligible_companies_observed": self.eligible_companies_observed,
            "unknown_company_identities": self.unknown_company_identities,
            "unknown_contact_identities": self.unknown_contact_identities,
            "opportunity_unit": "distinct employer x function among selected inputs",
            "final_pass_unit": "distinct identified contacts",
            "raw_to_final_pass_yield": (round(self.raw_to_final_pass_yield, 6)
                                        if self.raw_to_final_pass_yield is not None else None),
            "acquisition_requests": self.acquisition_requests,
            "enrichment_calls": self.enrichment_calls,
            "runtime_seconds": round(self.runtime_seconds, 3),
            "quota_consumed": self.quota_consumed,
            "inventory_remaining": self.inventory_remaining,
            "projected_sustainable_final_pass_per_day": self.projected_sustainable_per_day,
            "projection_status": "unknown_requires_comparable_daily_cohorts",
            "meets_target": self.meets_target(),
        }


def build_capacity_report(
    *,
    raw_postings: int,
    opportunities: int,
    enrichment: EnrichmentReport,
    delivered_final_pass: Optional[int],
    acquisition_requests: int,
    enrichment_calls: Optional[int],
    runtime_seconds: float,
    quota_consumed: int,
    inventory_remaining: int,
    runs_per_day: int = 1,
    target: int = TARGET_FINAL_PASS_PER_DAY,
    cohort_comparable: bool = False,
) -> CapacityReport:
    from hiring_manager import company_key_for_job
    contacts, company_keys = set(), set()
    unknown_company = unknown_contact = 0
    for lead in enrichment.final_pass():
        contact = lead.contact or {}
        row = contact.get("_airtable_row") or {}
        pid = str(row.get("hiring_manager_person_id") or contact.get("person_id") or "").strip()
        email = str(contact.get("email") or row.get("hiring_manager_email") or "").strip().lower()
        identity = f"person:{pid}" if pid else (f"email:{email}" if email else "")
        if identity:
            contacts.add(identity)
        else:
            unknown_contact += 1
        evidence = row or {"employer_name": lead.company.get("name"),
                           "employer_website": lead.company.get("domain") or lead.company.get("website")}
        company = company_key_for_job(evidence)
        if company and company != "unknown":
            company_keys.add(company)
        else:
            unknown_company += 1
    fp, companies = len(contacts), len(company_keys)
    contacts_per_company = (fp / companies if companies and not unknown_company and not unknown_contact
                            else None)
    # Recovered work is not newly acquired input. A truncated or mixed cohort
    # cannot establish conversion by dividing these two totals.
    yield_rate = fp / raw_postings if cohort_comparable and raw_postings else None
    # Multiplying one run's creations by a schedule proves neither replenishing
    # inventory nor repeatable conversion. Missing evidence remains unknown.
    projected = None
    return CapacityReport(
        raw_postings=raw_postings,
        opportunities=opportunities,
        new_eligible_companies=None,  # requires a prior approved-company identity baseline
        eligible_companies_observed=companies,
        unknown_company_identities=unknown_company,
        unknown_contact_identities=unknown_contact,
        final_pass_leads=fp,
        delivered_final_pass=delivered_final_pass,
        contacts_per_company=contacts_per_company,
        raw_to_final_pass_yield=yield_rate,
        acquisition_requests=acquisition_requests,
        enrichment_calls=enrichment_calls,
        runtime_seconds=runtime_seconds,
        quota_consumed=quota_consumed,
        inventory_remaining=inventory_remaining,
        projected_sustainable_per_day=projected,
        target=target,
    )
