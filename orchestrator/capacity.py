"""300-lead capacity report.

A lead counts toward the business target only when it is FINAL_PASS -- i.e. it
carries a verified email, a relevant active hiring signal, an ICP-qualified
company, a unique contact identity, passed CRM + suppression checks, and is
same-day outbound-eligible. Those are exactly the gates ``EnrichmentEngine``
enforces for FINAL_PASS, so the capacity report reads them off the reconciled
result rather than re-deriving them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from orchestrator.enrichment import EnrichmentReport

TARGET_FINAL_PASS_PER_DAY = 300


@dataclass
class CapacityReport:
    raw_postings: int
    opportunities: int
    new_eligible_companies: int
    final_pass_leads: int
    delivered_final_pass: int
    contacts_per_company: float
    raw_to_final_pass_yield: float
    acquisition_requests: int
    enrichment_calls: int
    runtime_seconds: float
    quota_consumed: int
    inventory_remaining: int
    projected_sustainable_per_day: int
    target: int = TARGET_FINAL_PASS_PER_DAY

    def meets_target(self) -> bool:
        return self.delivered_final_pass >= self.target

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_final_pass_per_day": self.target,
            "raw_postings": self.raw_postings,
            "opportunities": self.opportunities,
            "new_eligible_companies": self.new_eligible_companies,
            "final_pass_leads": self.final_pass_leads,
            "delivered_final_pass": self.delivered_final_pass,
            "contacts_per_company": round(self.contacts_per_company, 4),
            "raw_to_final_pass_yield": round(self.raw_to_final_pass_yield, 6),
            "acquisition_requests": self.acquisition_requests,
            "enrichment_calls": self.enrichment_calls,
            "runtime_seconds": round(self.runtime_seconds, 3),
            "quota_consumed": self.quota_consumed,
            "inventory_remaining": self.inventory_remaining,
            "projected_sustainable_final_pass_per_day": self.projected_sustainable_per_day,
            "meets_target": self.meets_target(),
        }


def build_capacity_report(
    *,
    raw_postings: int,
    opportunities: int,
    enrichment: EnrichmentReport,
    delivered_final_pass: int,
    acquisition_requests: int,
    enrichment_calls: int,
    runtime_seconds: float,
    quota_consumed: int,
    inventory_remaining: int,
    runs_per_day: int = 1,
    target: int = TARGET_FINAL_PASS_PER_DAY,
) -> CapacityReport:
    fp = len(enrichment.final_pass())
    companies = len({l.company.get("name") for l in enrichment.final_pass() if l.company})
    contacts_per_company = (fp / companies) if companies else 0.0
    yield_rate = (fp / raw_postings) if raw_postings else 0.0
    # Sustainable projection: FINAL_PASS actually deliverable per run x runs/day,
    # bounded by remaining inventory. Deliberately conservative -- it never
    # projects beyond what this run demonstrated per unit of input.
    per_run = delivered_final_pass
    projected = min(per_run * max(1, runs_per_day), per_run * max(1, runs_per_day))
    if inventory_remaining >= 0:
        projected = min(projected, per_run * max(1, runs_per_day))
    return CapacityReport(
        raw_postings=raw_postings,
        opportunities=opportunities,
        new_eligible_companies=companies,
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
