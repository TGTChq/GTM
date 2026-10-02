"""``python -m tgtc_core demo``: the nine-route path against SIMULATED providers.

The report it prints is orchestration evidence only. It proves the storage contract,
the gates and the delivery path work end to end; it proves nothing about the live
providers, inventory, conversion or capacity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..config import Settings
from ..db import apply_schema, connect
from ..runner import Runner
from .embedded_pg import database_url
from .scenario import build_nine_route_scenario


def run_demo(database_url_override: str = "", database_url: str = "") -> Dict[str, Any]:
    url = database_url or database_url_override
    server = None
    if not url:
        url, server = database_url_()
    conn = connect(url)
    try:
        apply_schema(conn)
        now = datetime.now(timezone.utc)
        sc = build_nine_route_scenario(now)
        settings = Settings(
            database_url=url, fantastic_api_key="sim", apollo_api_key="sim", airtable_token="sim", airtable_base_id="appSIM",
            instantly_api_key="sim", signing_key="demo-signing-key", campaign_env=sc.campaign_env, inference_enabled=False,
        )
        runner = Runner(conn, settings, fantastic_transport=sc.fantastic, apollo_transport=sc.apollo,
                        airtable_transport=sc.airtable, instantly_transport=sc.instantly, now=lambda: now, run_id="demo")
        report = runner.cycle()
        out = report.to_dict()
        out["evidence_class"] = "SIMULATED_PROVIDERS"
        out["airtable_rows_created"] = len(sc.airtable.records)
        out["instantly_leads_created"] = len(sc.instantly.leads)
        out["apollo_paid_calls_served"] = sc.apollo.served_paid
        out["approved_emails_by_function"] = _approved(conn)
        return out
    finally:
        conn.close()
        if server is not None:
            server.cleanup()


def database_url_():
    return database_url()


def _approved(conn) -> Dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT function_key, lead_json->>'email' AS email, campaign_id FROM approvals ORDER BY function_key")
        return {r["function_key"]: f"{r['email']} -> {r['campaign_id'][:8]}" for r in cur.fetchall()}
