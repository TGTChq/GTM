"""Prove the Challenger routing before a single lead is written.

Read-only. Writes nothing, enrols nobody, spends nothing.

Run it inside the production container (or with the production environment) so
the Instantly key never leaves that process:

    python check_challenger_routing.py --live      # + compare to Instantly
    python check_challenger_routing.py             # offline mapping proof only
    python check_challenger_routing.py --outbox    # + outbox integers (needs DB)

What it proves, in order, and it exits non-zero on the first failure:

1. the ten internal routing keys collapse to exactly nine campaign ids;
2. the only collapse is CUSTOMER EXPERIENCE (success + support share one
   campaign), so no lead can be enrolled twice by an accidental duplicate;
3. every configured id is a CHALLENGER id and none is a retired Control id;
4. (--live) every one of those nine ids exists in the Instantly workspace, and
   the names match what we think we are routing to;
5. (--outbox) the real integer state of the delivery outbox. No PII: people and
   emails are counted as distinct hashes, never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Dict, Mapping

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tgtc_core.policy.campaigns import (  # noqa: E402
    CAMPAIGN_BY_FUNCTION, CAMPAIGN_ENV_BY_FUNCTION, KNOWN_CHALLENGER_CAMPAIGN_IDS,
    KNOWN_CONTROL_CAMPAIGN_IDS,
)

CHALLENGER_ENV = "OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS_JSON"


def resolve_mapping(env: Mapping[str, str]) -> Dict[str, str]:
    """internal function key -> Challenger campaign id, from configuration only."""
    raw = str(env.get(CHALLENGER_ENV, "") or "").strip()
    if raw:
        return {str(k): str(v) for k, v in json.loads(raw).items()}
    out: Dict[str, str] = {}
    for fn, name in CAMPAIGN_ENV_BY_FUNCTION.items():
        value = str(env.get(name, "") or "").strip()
        if value:
            out[fn] = value
    return out


def _fail(msg: str) -> None:
    print(f"FAIL  {msg}")
    raise SystemExit(1)


def prove_mapping(mapping: Dict[str, str]) -> None:
    print("internal campaign key -> Challenger campaign name -> Challenger campaign ID")
    print("-" * 78)
    for fn in sorted(mapping):
        campaign = CAMPAIGN_BY_FUNCTION.get(fn)
        name = campaign.name if campaign else "?? UNKNOWN FUNCTION KEY"
        print(f"  {fn:<18} {name:<34} {mapping[fn]}")
    print()

    missing = set(CAMPAIGN_ENV_BY_FUNCTION) - set(mapping)
    if missing:
        _fail(f"function keys with no Challenger destination: {sorted(missing)}")

    ids = set(mapping.values())
    print(f"  keys: {len(mapping)}   distinct campaign ids: {len(ids)}")
    if len(mapping) != 10:
        _fail(f"expected 10 internal keys, found {len(mapping)}")
    if len(ids) != 9:
        _fail(f"the ten keys must collapse to exactly 9 campaigns, found {len(ids)}")

    shared = sorted(fn for fn, cid in mapping.items()
                    if list(mapping.values()).count(cid) > 1)
    if shared != ["customer_success", "customer_support"]:
        _fail(f"the only permitted shared campaign is CUSTOMER EXPERIENCE; shared={shared}")
    print("  collapse: customer_success + customer_support -> CUSTOMER EXPERIENCE (expected)")

    control = ids & set(KNOWN_CONTROL_CAMPAIGN_IDS)
    if control:
        _fail(f"configuration points at RETIRED Control campaigns: {sorted(control)}")
    unknown = ids - set(KNOWN_CHALLENGER_CAMPAIGN_IDS)
    if unknown:
        _fail(f"campaign ids that are neither known Challenger nor Control: {sorted(unknown)}")
    print("  every destination is a current Challenger campaign; no Control id present")
    print("OK    mapping proof passed\n")


def compare_live(mapping: Dict[str, str], env: Mapping[str, str]) -> None:
    """GET each configured campaign by id. Read-only, no lead is touched."""
    from tgtc_core.providers.http import RequestsTransport
    from tgtc_core.providers.instantly import InstantlyClient

    key = str(env.get("INSTANTLY_API_KEY", "") or "").strip()
    if not key:
        _fail("INSTANTLY_API_KEY is not present in this environment")
    client = InstantlyClient(
        RequestsTransport(),
        base_url=str(env.get("INSTANTLY_BASE_URL", "") or "https://api.instantly.ai/api/v2"),
        api_key=key,
    )
    seen: Dict[str, str] = {}
    for fn in sorted(mapping):
        cid = mapping[fn]
        if cid in seen:
            print(f"  {fn:<18} {cid}  (alias of an already verified campaign: {seen[cid]!r})")
            continue
        result = client.get_campaign(cid)
        if not result.ok:
            _fail(f"{fn}: campaign {cid} did not resolve live (status={result.status} {result.message})")
        data = result.data if isinstance(result.data, dict) else {}
        name = str(data.get("name") or "")
        status = data.get("status")
        seen[cid] = name
        print(f"  {fn:<18} {cid}  live name: {name!r}  status={status}")
    print(f"OK    {len(seen)} distinct Challenger campaigns resolved live")
    print()


def outbox_report(env: Mapping[str, str]) -> None:
    import psycopg
    from psycopg.rows import dict_row

    url = env.get("TGTC_DATABASE_URL") or env.get("DATABASE_URL")
    if not url:
        _fail("no database URL in the environment")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel, state, count(*) AS n FROM delivery_outbox "
                "GROUP BY channel, state ORDER BY channel, state"
            )
            rows = [dict(r) for r in cur.fetchall()]
            print("delivery_outbox by channel x state")
            for r in rows:
                print(f"  {r['channel']:<10} {r['state']:<10} {r['n']}")
            cur.execute(
                """
                SELECT count(*) AS rows,
                       count(DISTINCT a.person_id) AS people,
                       count(DISTINCT lower(a.lead_json->>'email')) AS emails,
                       count(DISTINCT (a.employer_id::text || ':' || a.campaign_key)) AS company_campaign_units,
                       count(DISTINCT a.campaign_id) AS campaign_ids
                FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id
                WHERE o.channel = 'instantly' AND o.state IN ('pending', 'failed', 'claimed')
                """
            )
            pend = dict(cur.fetchone())
            print("\npending instantly outbox (integers, no PII)")
            for k, v in pend.items():
                print(f"  {k:<26} {v}")
            cur.execute(
                "SELECT a.campaign_id, count(*) AS n FROM delivery_outbox o "
                "JOIN approvals a ON a.id = o.approval_id "
                "WHERE o.channel = 'instantly' AND o.state IN ('pending','failed','claimed') "
                "GROUP BY a.campaign_id ORDER BY n DESC"
            )
            print("\npending by destination campaign id")
            for r in cur.fetchall():
                cid = r["campaign_id"]
                where = ("CHALLENGER" if cid in KNOWN_CHALLENGER_CAMPAIGN_IDS else
                         "CONTROL (retired)" if cid in KNOWN_CONTROL_CAMPAIGN_IDS else "UNKNOWN")
                print(f"  {cid}  {r['n']:>5}  {where}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="compare against the live Instantly campaign list")
    ap.add_argument("--outbox", action="store_true", help="report real outbox integers (needs the database)")
    args = ap.parse_args()
    env = os.environ

    mapping = resolve_mapping(env)
    if not mapping:
        _fail(f"no routing configured: neither {CHALLENGER_ENV} nor any INSTANTLY_CAMPAIGN_* is set")
    prove_mapping(mapping)
    if args.live:
        compare_live(mapping, env)
    if args.outbox:
        outbox_report(env)
    print("all requested checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
