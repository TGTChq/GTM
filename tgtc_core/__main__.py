"""``python -m tgtc_core`` -- the core's own entry point.

Subcommands:
  migrate            apply the schema to TGTC_DATABASE_URL (idempotent)
  describe           print settings presence/limits and the policy manifest (no secrets)
  cycle              acquisition + stages + delivery against the REAL providers configured
  work --kind K      drain one stage
  deliver            drain the outbox
  ledger             print the reconciled ledger
  import-airtable    import existing Airtable rows as suppressions (reads only)
  prune              null compressed page payloads older than TGTC_PAYLOAD_RETENTION_DAYS (receipts kept)
  demo               end-to-end run against SIMULATED providers on an embedded PostgreSQL

Nothing here deploys, merges or changes live configuration. ``cycle``/``work``/
``deliver`` will spend provider credits when given real credentials -- they are the
production path, and they refuse to run without an explicit ``--i-understand-spend``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from .config import Settings
from .db import apply_schema, connect
from .policy.requirements import describe as describe_policy


def _settings() -> Settings:
    return Settings.from_env(os.environ)


def cmd_migrate(args) -> int:
    s = _settings()
    url = args.database_url or s.database_url
    if not url:
        print("TGTC_DATABASE_URL is required", file=sys.stderr)
        return 2
    conn = connect(url)
    version = apply_schema(conn)
    print(json.dumps({"schema_version": version}))
    return 0


def cmd_describe(args) -> int:
    s = _settings()
    print(json.dumps({"settings": s.describe(), "policy": describe_policy().__dict__}, indent=2, default=str))
    return 0


def _runner(conn, s: Settings, *, allow_spend: bool):
    from .domain.inference import AnthropicAdapter, NullAdapter
    from .providers.http import RequestsTransport
    from .runner import Runner

    if not allow_spend:
        raise SystemExit("refusing to run against real providers without --i-understand-spend")
    t = RequestsTransport()
    inference = AnthropicAdapter(api_key=s.anthropic_api_key, model=s.inference_model, base_url=s.anthropic_base_url) if s.anthropic_api_key else NullAdapter()
    return Runner(conn, s, fantastic_transport=t if s.fantastic_api_key else None, apollo_transport=t if s.apollo_api_key else None,
                  airtable_transport=t if s.airtable_token else None, instantly_transport=t if s.instantly_api_key else None,
                  inference=inference)


def cmd_cycle(args) -> int:
    s = _settings()
    conn = connect(args.database_url or s.database_url)
    apply_schema(conn)
    r = _runner(conn, s, allow_spend=args.i_understand_spend)
    print(json.dumps(r.cycle(acquire=not args.no_acquire, max_items=args.max_items).to_dict(), indent=2, default=str))
    return 0


def cmd_work(args) -> int:
    s = _settings()
    conn = connect(args.database_url or s.database_url)
    r = _runner(conn, s, allow_spend=args.i_understand_spend)
    print(json.dumps(r.work(args.kind, max_items=args.max_items), indent=2))
    return 0


def cmd_deliver(args) -> int:
    s = _settings()
    conn = connect(args.database_url or s.database_url)
    r = _runner(conn, s, allow_spend=args.i_understand_spend)
    print(json.dumps(r.deliver(max_items=args.max_items), indent=2))
    return 0


def cmd_ledger(args) -> int:
    from .services.metrics import ledger

    s = _settings()
    conn = connect(args.database_url or s.database_url)
    print(json.dumps(ledger(conn), indent=2, default=str))
    return 0


def cmd_import_airtable(args) -> int:
    from .providers.airtable import AirtableClient
    from .providers.http import RequestsTransport
    from .services.suppression import import_airtable_rows

    s = _settings()
    conn = connect(args.database_url or s.database_url)
    apply_schema(conn)
    client = AirtableClient(RequestsTransport(), base_url=s.airtable_base_url, token=s.airtable_token,
                            base_id=s.airtable_base_id, table=s.airtable_table_name)
    fields = ["Lead Key", "Company", "Website", "Role Bucket", "Status", "Email", "Outbound Company Identity",
              "Outbound Company Confidence", "Outbound Hold"]
    offset = ""
    total = None
    while True:
        rows, offset = client.list_page(fields, offset=offset)
        counts = import_airtable_rows(conn, rows)
        total = counts if total is None else _merge(total, counts)
        if not offset:
            break
    print(json.dumps(total.__dict__ if total else {}, indent=2))
    return 0


def _merge(a, b):
    a.rows += b.rows
    a.inserted += b.inserted
    a.already_present += b.already_present
    a.skipped += b.skipped
    for k, v in b.by_kind.items():
        a.by_kind[k] = a.by_kind.get(k, 0) + v
    return a


def cmd_prune(args) -> int:
    from .services.retention import prune_payloads

    s = _settings()
    conn = connect(args.database_url or s.database_url)
    print(json.dumps(prune_payloads(conn, retention_days=s.payload_retention_days), indent=2, default=str))
    return 0


def cmd_demo(args) -> int:
    """SIMULATED providers, embedded PostgreSQL, nine routes. Not live evidence."""
    from .testing.demo import run_demo

    report = run_demo(database_url=args.database_url)
    print(json.dumps(report, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tgtc_core", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("migrate", cmd_migrate), ("describe", cmd_describe), ("cycle", cmd_cycle), ("work", cmd_work),
                     ("deliver", cmd_deliver), ("ledger", cmd_ledger), ("import-airtable", cmd_import_airtable),
                     ("prune", cmd_prune), ("demo", cmd_demo)):
        p = sub.add_parser(name)
        p.add_argument("--database-url", default="")
        p.add_argument("--max-items", type=int, default=1000)
        p.add_argument("--no-acquire", action="store_true")
        p.add_argument("--i-understand-spend", action="store_true", help="required for cycle/work/deliver against real providers")
        if name == "work":
            p.add_argument("--kind", required=True, choices=("resolve_identity", "classify", "qualify_opportunity"))
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
