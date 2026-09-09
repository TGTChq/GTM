"""A nine-route SIMULATED scenario shared by the demo and the integrated tests.

Ten employers (one per function key; CUSTOMER EXPERIENCE serves two), each with a
compatible posting from the synthetic corpus, an Apollo organization, and a buyer
list that includes at least one WRONG candidate first (wrong company, no email, or
an unverified email) so the recovery path is exercised on every route.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

from ..policy.campaigns import CAMPAIGN_BY_FUNCTION, CAMPAIGN_ENV_BY_FUNCTION, FUNCTION_KEYS, KNOWN_CONTROL_CAMPAIGN_IDS
from .corpus import CORPUS, Example
from .fakes import FakeAirtable, FakeApollo, FakeFantastic, FakeInstantly, make_person, make_posting_row

CONTROL_ID_BY_CAMPAIGN_KEY: Dict[str, str] = {
    "product": "45ac1e03-67e7-4bdd-b372-808042104e4c",
    "operations": "4effab2f-9073-46a9-b7ae-986ccc8f49c6",
    "finance": "1db88bbe-b2cf-4574-a5b7-1cb948151a86",
    "people_hr": "cf01e56b-e5ad-489e-a02c-c35c93cf3b53",
    "ecommerce": "0f0f57d5-fab1-436d-b0d8-8cb43b031f03",
    "customer_experience": "1747c87e-12e9-4477-bc4d-048223d39513",
    "marketing_creative": "165c9e87-c3e7-4e9c-9ccb-a8dbf5779726",
    "gtm_systems": "917973f3-c282-4a84-8da4-525a7a91819b",
    "ai_technical": "04670c6a-828b-42cd-9dad-904592a63d9b",
}
assert set(CONTROL_ID_BY_CAMPAIGN_KEY.values()) == set(KNOWN_CONTROL_CAMPAIGN_IDS)


def campaign_env() -> Dict[str, str]:
    """Env-name -> Control id, exactly as production configures it (ten names, nine ids)."""
    return {CAMPAIGN_ENV_BY_FUNCTION[f]: CONTROL_ID_BY_CAMPAIGN_KEY[CAMPAIGN_BY_FUNCTION[f].key] for f in FUNCTION_KEYS}


BUYER_TITLE_BY_FUNCTION = {
    "product": "Director of Product", "operations": "VP of Operations", "finance": "Controller",
    "people_hr": "VP People", "ecommerce": "Head of Ecommerce", "customer_success": "VP Customer Success",
    "customer_support": "Head of Customer Support", "marketing": "VP Marketing", "gtm_revenue": "Head of Revenue Operations",
    "engineering": "VP Engineering",
}

FIRST_POSITIVE: Dict[str, str] = {
    "customer_success": "cs1", "customer_support": "sup1", "engineering": "eng1", "finance": "fin2",
    "people_hr": "hr1", "marketing": "mkt1", "operations": "ops1", "product": "prod1",
    "gtm_revenue": "gtm1", "ecommerce": "ecom1",
}


@dataclass
class Scenario:
    fantastic: FakeFantastic
    apollo: FakeApollo
    airtable: FakeAirtable
    instantly: FakeInstantly
    campaign_env: Dict[str, str]
    employers: Dict[str, str]        # function -> domain
    expected_emails: Dict[str, str]  # function -> the email that must be approved
    now: datetime


def example(key: str) -> Example:
    return next(e for e in CORPUS if e.key == key)


def build_nine_route_scenario(now: datetime | None = None) -> Scenario:
    now = now or datetime.now(timezone.utc)
    fantastic = FakeFantastic()
    apollo = FakeApollo()
    employers: Dict[str, str] = {}
    expected: Dict[str, str] = {}
    created = now - timedelta(hours=4)  # inside the fresh window (lag 3h)
    for i, fn in enumerate(FUNCTION_KEYS):
        ex = example(FIRST_POSITIVE[fn])
        domain = f"{fn.replace('_', '')}co.com"
        org_name = f"{fn.replace('_', ' ').title()} Co"
        employers[fn] = domain
        fantastic.rows.append(make_posting_row(
            id=f"job-{fn}", title=ex.title, organization=org_name, domain=domain, description=ex.description,
            date_created=created - timedelta(minutes=i), headcount=120 + i,
        ))
        apollo.organizations[domain] = {"id": f"org-{domain}", "name": org_name, "primary_domain": domain,
                                        "estimated_num_employees": 120 + i, "industry": "Software Development",
                                        "linkedin_url": f"https://www.linkedin.com/company/{fn}co"}
        buyer = BUYER_TITLE_BY_FUNCTION[fn]
        good_email = f"buyer.{fn}@{domain}"
        expected[fn] = good_email
        # Candidate 1: right title, wrong company (Apollo returns them under our domain search but org differs)
        wrong = make_person(id=f"p-{fn}-wrong", first="Wrong", last="Company", title=buyer, org_name="Other Corp",
                            org_domain="othercorp.com", email=f"wrong@othercorp.com", email_status="verified")
        wrong["organization"] = {"id": "org-other", "name": "Other Corp", "primary_domain": domain}  # slips past the domain guard
        # Candidate 2: right company, Apollo will not verify the email
        unverified = make_person(id=f"p-{fn}-unv", first="Un", last="Verified", title=buyer, org_name=org_name,
                                 org_domain=domain, email=f"un.verified@{domain}", email_status="extrapolated")
        # Candidate 3: the good one
        good = make_person(id=f"p-{fn}-good", first="Good", last=fn.replace("_", "").title(), title=buyer, org_name=org_name,
                           org_domain=domain, email=good_email, email_status="verified")
        apollo.people_by_domain[domain] = [wrong, unverified, good]
    return Scenario(fantastic=fantastic, apollo=apollo, airtable=FakeAirtable(),
                    instantly=FakeInstantly(campaign_status={cid: 1 for cid in KNOWN_CONTROL_CAMPAIGN_IDS}),
                    campaign_env=campaign_env(), employers=employers, expected_emails=expected, now=now)
