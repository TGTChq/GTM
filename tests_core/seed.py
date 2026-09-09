"""Seed helpers: put a posting through identity + classification without HTTP."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from tgtc_core.services.acquisition import SOURCE_JOB_BOARDS, upsert_posting
from tgtc_core.services.classification_service import classify_one
from tgtc_core.services.identity_service import resolve_posting_identity
from tgtc_core.testing.corpus import CORPUS
from tgtc_core.testing.fakes import FakeApollo, make_person, make_posting_row
from tgtc_core.testing.scenario import BUYER_TITLE_BY_FUNCTION, FIRST_POSITIVE
from tests_core.helpers import sql1


def example_for(function_key: str):
    return next(e for e in CORPUS if e.key == FIRST_POSITIVE[function_key])


def seed_posting(conn, clock, row: Dict[str, Any], *, source: str = SOURCE_JOB_BOARDS, lane: str = "fresh") -> int:
    state, pid = upsert_posting(conn, source=source, row=row, lane=lane, now=clock())
    conn.commit()
    return pid


def seed_opportunity(conn, clock, *, function_key: str = "customer_success", domain: str = "acme.com",
                     org_name: str = "Acme", title: Optional[str] = None, description: Optional[str] = None,
                     headcount: Optional[int] = 120, job_id: str = "job-1", source: str = SOURCE_JOB_BOARDS,
                     lane: str = "fresh", hours_ago: float = 4.0, inference=None) -> Tuple[int, Optional[int], Optional[int]]:
    ex = example_for(function_key)
    row = make_posting_row(id=job_id, title=title if title is not None else ex.title, organization=org_name, domain=domain,
                           description=description or ex.description, date_created=clock() - timedelta(hours=hours_ago),
                           headcount=headcount)
    pid = seed_posting(conn, clock, row, source=source, lane=lane)
    out = resolve_posting_identity(conn, pid, now=clock())
    if out.employer_id is None:
        return pid, None, None
    res = classify_one(conn, pid, inference=inference, now=clock())
    oid = None
    if res.opportunity_ids:
        oid = sql1(conn, "SELECT id FROM opportunities WHERE employer_id = %s AND function_key = %s", (out.employer_id, function_key))
    return pid, out.employer_id, oid


def apollo_for(domain: str, org_name: str, *, function_key: str = "customer_success", headcount: int = 120,
               people: Optional[List[Dict[str, Any]]] = None, industry: str = "Software Development") -> FakeApollo:
    fake = FakeApollo()
    fake.organizations[domain] = {"id": f"org-{domain}", "name": org_name, "primary_domain": domain,
                                  "estimated_num_employees": headcount, "industry": industry}
    fake.people_by_domain[domain] = people if people is not None else [good_buyer(domain, org_name, function_key)]
    return fake


def good_buyer(domain: str, org_name: str, function_key: str = "customer_success", *, id: str = "p-good",
               email: Optional[str] = None, status: str = "verified") -> Dict[str, Any]:
    return make_person(id=id, first="Good", last="Buyer", title=BUYER_TITLE_BY_FUNCTION[function_key], org_name=org_name,
                       org_domain=domain, email=email or f"good.buyer@{domain}", email_status=status)
