"""Shared builders for the integrated tests (SIMULATED providers)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from tgtc_core.config import Settings
from tgtc_core.providers.airtable import AirtableClient
from tgtc_core.providers.apollo import ApolloClient
from tgtc_core.providers.fantastic import FantasticClient
from tgtc_core.runner import Runner
from tgtc_core.services.acquisition import SOURCE_JOB_BOARDS, AcquisitionService
from tgtc_core.services.delivery import DeliveryService
from tgtc_core.services.opportunity import OpportunityService
from tgtc_core.testing.fakes import FakeAirtable, FakeApollo, FakeFantastic, FakeInstantly, make_person, make_posting_row
from tgtc_core.testing.scenario import campaign_env

SIGNING_KEY = "test-signing-key"


def settings(campaigns: Optional[Dict[str, str]] = None, **over: Any) -> Settings:
    base = dict(fantastic_api_key="sim", apollo_api_key="sim", airtable_token="sim", airtable_base_id="appSIM",
                instantly_api_key="sim", signing_key=SIGNING_KEY, campaign_env=campaigns or campaign_env(),
                inference_enabled=False, fantastic_page_limit=100)
    base.update(over)
    return Settings(**base)


def acquisition(conn, fake: FakeFantastic, clock, *, page_limit: int = 100, max_pages: int = 50, **over) -> AcquisitionService:
    client = FantasticClient(fake, base_url="https://data.fantastic.jobs", api_key="sim", sleep=lambda s: None)
    kwargs = dict(page_limit=page_limit, time_frame="7d", fresh_window_minutes=60, fresh_lag_minutes=180,
                  backfill_window_hours=24, max_pages_per_partition=max_pages, min_jobs_quota_remaining=90,
                  min_requests_quota_remaining=20, now=clock)
    kwargs.update(over)
    return AcquisitionService(conn, client, **kwargs)


def apollo_client(fake: FakeApollo) -> ApolloClient:
    return ApolloClient(fake, base_url="https://api.apollo.io/api/v1", api_key="sim")


def opportunity_service(conn, fake: FakeApollo, clock, **over) -> OpportunityService:
    kwargs = dict(campaign_env=campaign_env(), signing_key=SIGNING_KEY, retry_hours=6.0, people_search_max_pages=2, now=clock)
    kwargs.update(over)
    return OpportunityService(conn, apollo_client(fake), **kwargs)


def delivery_service(conn, airtable: Optional[FakeAirtable], instantly: Optional[FakeInstantly], clock, **over) -> DeliveryService:
    at = AirtableClient(airtable, base_url="https://api.airtable.com/v0", token="sim", base_id="appSIM", table="Leads",
                        sleep=lambda s: None) if airtable is not None else None
    from tgtc_core.providers.instantly import InstantlyClient

    ic = InstantlyClient(instantly, base_url="https://api.instantly.ai/api/v2", api_key="sim") if instantly is not None else None
    kwargs = dict(lease_seconds=300, backoff_seconds=120, now=clock)
    kwargs.update(over)
    return DeliveryService(conn, airtable=at, instantly=ic, **kwargs)


def runner(conn, sc, clock, **over) -> Runner:
    s = settings(sc.campaign_env, **over)
    return Runner(conn, s, fantastic_transport=sc.fantastic, apollo_transport=sc.apollo, airtable_transport=sc.airtable,
                  instantly_transport=sc.instantly, now=clock, run_id="test")


def make_fresh_partition(conn, clock, *, source: str = SOURCE_JOB_BOARDS, lane: str = "fresh", hours_ago_start: float = 5.0,
                         hours: float = 1.0) -> int:
    start = clock() - timedelta(hours=hours_ago_start)
    end = start + timedelta(hours=hours)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO source_partitions (source, lane, window_start, window_end) VALUES (%s, %s, %s, %s) RETURNING id",
                    (source, lane, start, end))
        pid = int(cur.fetchone()["id"])
    conn.commit()
    return pid


def rows_in_window(clock, n: int, *, hours_ago_start: float = 5.0, prefix: str = "job", domain_prefix: str = "emp",
                   description: str = "", title: str = "Customer Success Manager") -> List[Dict[str, Any]]:
    start = clock() - timedelta(hours=hours_ago_start)
    desc = description or (
        "You will own customer onboarding for new accounts and drive product adoption across your book of business. "
        "Run quarterly business reviews, track health scores and lead renewals and expansion. Full-time, remote within the US.")
    return [make_posting_row(id=f"{prefix}-{i}", title=title, organization=f"Employer {i}", domain=f"{domain_prefix}{i}.com",
                             description=desc, date_created=start + timedelta(minutes=(i % 55) + 1)) for i in range(n)]


def sql1(conn, sql: str, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.commit()
    return None if row is None else list(row.values())[0]


def sqlall(conn, sql: str, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows
