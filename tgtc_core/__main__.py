"""``python -m tgtc_core`` -- the core's own entry point.

Subcommands:
  migrate            apply the schema to TGTC_DATABASE_URL (idempotent)
  describe           print settings presence/limits and the policy manifest (no secrets)
  check-db           authenticated PostgreSQL SELECTs only; does not install the schema
  cycle              acquisition + stages + delivery against the REAL providers configured
  run-target         repeat cycles until this run creates the Airtable target
  work --kind K      drain one stage
  deliver            drain the outbox
  ledger             print the reconciled ledger
  weekly-report      measure a Friday-to-Friday reporting week from the database (no provider call)
  slack-test         send one labelled connectivity test to a channel (no pipeline data)
  report-export      write a stored week's private lead-level file out (personal data)
  report-detail-link record where a week's detail was published, and to whom
  replies-poll       ingest Instantly replies (out of office / departed / opt-out); never sends
  import-airtable    import existing Airtable rows as suppressions (reads only)
  prune              null compressed page payloads older than TGTC_PAYLOAD_RETENTION_DAYS (receipts kept)
  demo               end-to-end run against SIMULATED providers on an embedded PostgreSQL

Nothing here deploys, merges or changes live configuration. ``cycle``/``work``/
``run-target``/``deliver`` will spend provider credits when given real credentials -- they are the
production path, and they refuse to run without an explicit ``--i-understand-spend``.
With ``TGTC_ACCEPTANCE_MODE=read_only``, only ``describe`` and ``check-db`` are
allowed; the spend acknowledgement does not override that restriction.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

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


def cmd_check_db(args) -> int:
    from .db.check import check_database

    # No provider settings or runner are needed for an infrastructure check.
    result = check_database(args.database_url or os.environ.get("TGTC_DATABASE_URL", ""))
    print(json.dumps(result, sort_keys=True))
    if result["status"] == "missing_database_url":
        return 2
    return 0 if result["status"] == "database_reachable" else 1


def _runner(conn, s: Settings, *, allow_spend: bool):
    _require_spend_acknowledgement(allow_spend)

    from .domain.inference import AnthropicAdapter, NullAdapter
    from .providers.http import RequestsTransport
    from .runner import Runner

    t = RequestsTransport()
    inference = AnthropicAdapter(api_key=s.anthropic_api_key, model=s.inference_model, base_url=s.anthropic_base_url) if s.anthropic_api_key else NullAdapter()
    return Runner(conn, s, fantastic_transport=t if s.fantastic_api_key else None, apollo_transport=t if s.apollo_api_key else None,
                  airtable_transport=t if s.airtable_token else None, instantly_transport=t if s.instantly_api_key else None,
                  inference=inference)


def _budget_id(args, settings: Settings) -> str:
    return str(getattr(args, "budget_id", "") or settings.spend_budget_id or "").strip()


def _require_persistent_budget(args, settings: Settings) -> None:
    settings.spend_budget_id = _budget_id(args, settings)
    if not settings.spend_budget_id:
        raise SystemExit("refusing provider execution without TGTC_SPEND_BUDGET_ID or --budget-id")


def _require_spend_acknowledgement(allow_spend: bool) -> None:
    # Refuse before opening storage or applying a schema. This acknowledgement is
    # not a credit budget and does not make a live acceptance cycle bounded.
    if not allow_spend:
        raise SystemExit("refusing to run against real providers without --i-understand-spend")


def _require_acceptance_command(args) -> None:
    """The initial acceptance service may only inspect config or SELECT from DB.

    The ordinary spend acknowledgement cannot override this deployment setting.
    This is a zero-provider-spend CLI mode, not a budget for later paid trials.
    """
    mode = os.environ.get("TGTC_ACCEPTANCE_MODE", "").strip()
    if mode not in ("", "read_only", "bounded"):
        raise SystemExit("invalid TGTC_ACCEPTANCE_MODE; expected read_only, bounded or unset")
    if mode == "read_only" and args.cmd not in ("describe", "check-db"):
        raise SystemExit("TGTC_ACCEPTANCE_MODE=read_only allows only describe and check-db")
    if mode == "bounded":
        allowed = ("describe", "check-db", "migrate", "budget", "ledger", "cycle", "run-target", "work",
                   "weekly-report")
        if args.cmd not in allowed:
            raise SystemExit("TGTC_ACCEPTANCE_MODE=bounded blocks delivery and unbounded maintenance commands")
        if args.cmd in ("cycle", "run-target") and not args.no_deliver:
            raise SystemExit("TGTC_ACCEPTANCE_MODE=bounded requires --no-deliver")


def cmd_cycle(args) -> int:
    _require_spend_acknowledgement(args.i_understand_spend)
    s = _settings()
    _require_persistent_budget(args, s)
    conn = connect(args.database_url or s.database_url)
    apply_schema(conn)
    r = _runner(conn, s, allow_spend=args.i_understand_spend)
    report = r.cycle(acquire=not args.no_acquire, deliver=not args.no_deliver,
                     max_items=args.max_items)
    print(json.dumps(report.to_dict(), indent=2, default=str))
    failure = _bounded_acceptance_failure(report, acquire=not args.no_acquire)
    if failure:
        print(f"bounded acceptance failed: {failure}", file=sys.stderr)
        return 1
    return 0


def cmd_run_target(args) -> int:
    """Target-seeking production controller; delivers Airtable and Instantly."""
    _require_spend_acknowledgement(args.i_understand_spend)
    s = _settings()
    _require_persistent_budget(args, s)
    from .db.connection import acquire_run_lock
    run_lock = acquire_run_lock(args.database_url or s.database_url, connector=connect)
    if run_lock is None:
        # Another run (cron or manual) is active: this execution must not become a
        # duplicate. Exit 0 so the platform does not report a crash.
        print("run-target: another production run holds the run lock; this execution did nothing")
        return 0
    try:
        return _run_target_locked(args, s)
    finally:
        run_lock.close()


def _run_target_locked(args, s) -> int:
    conn = connect(args.database_url or s.database_url)
    apply_schema(conn)
    r = _runner(conn, s, allow_spend=args.i_understand_spend)
    report = r.run_to_target(
        target=args.target,
        max_rounds=args.max_rounds,
        max_items=args.max_items,
        acquire=not args.no_acquire,
        deliver=not args.no_deliver,
    )
    print(json.dumps(report.to_dict(), indent=2, default=str))
    # A completed run below target is a successful process (exit 0, result
    # target_not_reached in the ledger). Railway marks any non-zero exit
    # CRASHED, so only a genuinely broken system may return non-zero.
    from .runner import run_exit_code
    code = run_exit_code(report)
    summary = (f"target run {report.result}: {report.airtable_created}/{report.target} Airtable leads; "
               f"stop_reason={report.stop_reason}")
    if code:
        summary += f"; technical_failures={report.technical_failures}"
    print(summary, file=sys.stderr if code else sys.stdout)
    return code


def cmd_budget_id(args) -> int:
    """Print the namespaced budget id for a kind of run (no database, no spend)."""
    from .services.budget_policy import budget_id_for

    print(budget_id_for(args.kind))
    return 0


def cmd_run_daily(args) -> int:
    """The daily production controller: 1,000 fresh Instantly creations is a MINIMUM;
    drain first, buy in small blocks, deliver everything produced (``tgtc_core.daily``)."""
    _require_spend_acknowledgement(args.i_understand_spend)
    s = _settings()
    _require_persistent_budget(args, s)
    from .services.budget_policy import BudgetPolicyError, claim, validate
    try:
        validate(args.budget_kind, s.spend_budget_id)
    except BudgetPolicyError as exc:
        print(f"run-daily refused: {exc}", file=sys.stderr)
        return EXIT_BUDGET_REFUSED
    from .db.connection import acquire_run_lock
    run_lock = acquire_run_lock(args.database_url or s.database_url, connector=connect)
    if run_lock is None:
        print("run-daily: another production run holds the run lock; this execution did nothing")
        return 0
    try:
        conn = connect(args.database_url or s.database_url)
        apply_schema(conn)
        r = _runner(conn, s, allow_spend=args.i_understand_spend)
        try:
            claimed = claim(conn, budget_id=s.spend_budget_id, kind=args.budget_kind, run_id=r.run_id)
        except BudgetPolicyError as exc:
            # Visible, non-zero: a scheduled run must never go on silently with a budget
            # another kind of run consumed.
            r._log("daily", "refused", {"budget_id": s.spend_budget_id, "reason": str(exc)})
            print(f"run-daily refused: {exc}", file=sys.stderr)
            return EXIT_BUDGET_REFUSED
        r._log("daily", "budget_claim", claimed)
        from .daily import DailyController
        report = DailyController(r, budget_id=s.spend_budget_id, target=args.target, block_pages=args.block_pages,
                                 max_rounds=args.max_rounds, max_items=args.max_items).run()
        out = report.to_dict()
        from .runner import TargetRunReport, run_exit_code
        classified = TargetRunReport(run_id=r.run_id, target=args.target, rounds=report.rounds)
        out["technical_failures"] = classified.technical_failures
        print(json.dumps(out, indent=2, default=str))
        print(f"daily run {report.stop_reason}: fresh Instantly {out['fresh_instantly_created']}/{args.target}, "
              f"backlog {out['backlog_instantly_created']}")
        # A business shortfall exits 0 (visible in the ledger); only a broken system is non-zero.
        return run_exit_code(classified)
    finally:
        run_lock.close()


EXIT_BUDGET_REFUSED = 3


def _bounded_acceptance_failure(report, *, acquire: bool) -> str:
    """Expose technical failures in every stage of a bounded acceptance run.

    The ordinary production CLI keeps its historical best-effort exit behaviour.
    A paid acceptance run must not look green when every provider page failed at
    request time, or when inference/enrichment failed without acquisition. A valid
    empty page, business rejection and normal budget stop are not failures.
    """
    if os.environ.get("TGTC_ACCEPTANCE_MODE", "").strip() != "bounded":
        return ""
    if acquire:
        # A healthy priority page must not hide a broken discovery query (or the
        # reverse). Legacy acceptance semantics remain backward-compatible.
        for item in report.acquisition:
            stop = str(item.get("stop_reason") or "")
            if item.get("query_profile") in {"priority_v1", "discovery_v1"} and (
                stop.startswith(("request_error:", "provider_")) or stop in {
                    "auth_refused", "quota_refused", "timeout_uncertain", "lease_lost",
                    "partition_outside_provider_time_frame", "duplicate_page_loop",
                }
            ):
                return stop
        pages = sum(int(item.get("pages") or 0) for item in report.acquisition)
        if pages == 0:
            for item in report.acquisition:
                stop = str(item.get("stop_reason") or "")
                if stop.startswith("request_error:"):
                    return stop
    failures = [failure for stage, counts in getattr(report, "stages", {}).items()
                if (failure := _bounded_work_failure(stage, counts))]
    return "; ".join(failures)


def _bounded_work_failure(kind: str, counts: dict) -> str:
    """Use per-run diagnostics, not historical ledger totals or approval yield.

    ``technical_failure`` includes handled provider failures that would otherwise
    be hidden in wait/closed totals. Generic waits and closes can be legitimate.
    """
    if os.environ.get("TGTC_ACCEPTANCE_MODE", "").strip() != "bounded":
        return ""
    failures = [f"{key}={counts[key]}" for key in ("technical_failure", "error_retry", "retry", "lease_lost")
                if int(counts.get(key) or 0) > 0]
    return f"{kind}: {', '.join(failures)}" if failures else ""


def cmd_work(args) -> int:
    _require_spend_acknowledgement(args.i_understand_spend)
    s = _settings()
    if args.kind in ("classify", "qualify_opportunity"):
        _require_persistent_budget(args, s)
    conn = connect(args.database_url or s.database_url)
    r = _runner(conn, s, allow_spend=args.i_understand_spend)
    counts = r.work(args.kind, max_items=args.max_items)
    print(json.dumps(counts, indent=2))
    failure = _bounded_work_failure(args.kind, counts)
    if failure:
        print(f"bounded acceptance failed: {failure}", file=sys.stderr)
        return 1
    return 0


def cmd_deliver(args) -> int:
    _require_spend_acknowledgement(args.i_understand_spend)
    s = _settings()
    conn = connect(args.database_url or s.database_url)
    r = _runner(conn, s, allow_spend=args.i_understand_spend)
    print(json.dumps(r.deliver(max_items=args.max_items), indent=2))
    return 0


def cmd_budget(args) -> int:
    from .services.spend_budget import BudgetLimits, create_budget

    s = _settings()
    budget_id = _budget_id(args, s)
    if not budget_id:
        raise SystemExit("--budget-id is required")
    if getattr(args, "budget_kind", ""):
        from .services.budget_policy import BudgetPolicyError, validate
        try:
            validate(args.budget_kind, budget_id)
        except BudgetPolicyError as exc:
            raise SystemExit(f"budget refused: {exc}")
    if args.expires_hours <= 0:
        raise SystemExit("--expires-hours must be positive")
    expires_at = datetime.now(timezone.utc) + timedelta(hours=args.expires_hours)
    conn = connect(args.database_url or s.database_url)
    apply_schema(conn)
    result = create_budget(
        conn, budget_id,
        BudgetLimits(
            fantastic_requests=args.fantastic_requests,
            fantastic_credits=args.fantastic_credits,
            apollo_requests=args.apollo_requests,
            apollo_credits=args.apollo_credits,
            anthropic_requests=args.anthropic_requests,
            anthropic_input_tokens=args.anthropic_input_tokens,
            anthropic_output_tokens=args.anthropic_output_tokens,
        ),
        expires_at=expires_at,
    )
    print(json.dumps(result, indent=2, default=str))
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


def cmd_canary_24h(args) -> int:
    """Daily 24h acquisition canary. Off unless FANTASTIC_DAILY_24H_CANARY=1. No database,
    no enrichment, no delivery: Fantastic pages in, a new evidence directory out."""
    from .services.daily_24h_canary import cli

    return cli(args, os.environ)


def cmd_canary_24h_strategy(args) -> int:
    """Provider-confirmed 24h strategy canary: zero-credit count matrix first, then bounded records.
    Off unless FANTASTIC_DAILY_24H_CANARY=1. No database, no enrichment, no delivery."""
    from .services.daily_24h_canary import strategy_cli

    return strategy_cli(args, os.environ)


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
EXIT_REPORT_ATTENTION = 4
EXIT_REPORT_NOT_SENT = 5


def cmd_report_export(args) -> int:
    """Write a stored week's lead-level file out, for handing to a verified destination.

    It carries personal data, so it is written where the lead export always is -- outside
    the repository -- and its checksum is printed, so the file that reaches the readers
    can be matched to the one the report reconciled against.
    """
    from pathlib import Path

    from .reporting import detail as lead_detail, export as lead_export

    conn = connect(args.database_url or _settings().database_url)
    stored = lead_detail.load(conn, args.report_id)
    if stored is None:
        print(f"no lead detail is stored for {args.report_id}", file=sys.stderr)
        return 1
    target = Path(args.out).resolve()
    try:
        target.relative_to(lead_export.REPO_ROOT)
    except ValueError:
        pass
    else:
        print("the lead detail carries personal data and must not be written inside the repository",
              file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(stored["csv"])
    print(json.dumps({"report_id": args.report_id, "rows": stored["row_count"], "sha256": stored["sha256"],
                      "path": str(target), "published_url": stored.get("published_url")}, indent=2, default=str))
    return 0


def cmd_report_detail_link(args) -> int:
    """Record where a week's detail was published and exactly who was given access."""
    from .reporting import detail as lead_detail

    viewers = [v.strip() for v in args.viewers.split(",") if v.strip()]
    if not viewers:
        print("--viewers lists the accounts that were granted access; 'anyone with the link' is not a reader list",
              file=sys.stderr)
        return 1
    conn = connect(args.database_url or _settings().database_url)
    try:
        row = lead_detail.record_publication(conn, args.report_id, url=args.url, viewers=viewers)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(row, indent=2, default=str))
    return 0


def cmd_replies_poll(args) -> int:
    """Read Instantly's replies for the nine campaigns and record what each one means.

    Reading never sends. An out-of-office waits, a departure stops that address and
    queues a replacement search, an opt-out is respected, and anything a person wrote
    goes to a human. ``--dry-run`` decides everything and writes nothing, which is how
    a change to the classifier is judged against production before it touches it.
    """
    from .providers.http import RequestsTransport
    from .providers.instantly import InstantlyClient
    from .services import replies as reply_service

    s = _settings()
    campaign_ids = sorted({v for v in (s.campaign_env or {}).values() if v})
    if not campaign_ids:
        print("replies-poll refused: no INSTANTLY_CAMPAIGN_* ids are configured on this service", file=sys.stderr)
        return 1
    if not s.instantly_api_key:
        print("replies-poll refused: INSTANTLY_API_KEY is not set on this service", file=sys.stderr)
        return 1
    conn = connect(args.database_url or s.database_url)
    apply_schema(conn)
    client = InstantlyClient(RequestsTransport(), base_url=s.instantly_base_url, api_key=s.instantly_api_key)
    report = reply_service.poll(conn, client, campaign_ids=campaign_ids,
                                now=datetime.now(timezone.utc), page_size=args.page_size,
                                max_pages=args.max_pages, dry_run=args.dry_run, resume=args.resume)
    print(json.dumps({"campaigns": len(campaign_ids), **report.to_dict()}, indent=2, default=str))
    return 0


def _slack_destination(args, channel):
    """Resolve a sender and say how the destination was established, or refuse.

    Used by both the weekly report and the connectivity test, so a message can never
    reach a channel by a route the other one would not accept.
    """
    from .reporting import slack

    token = os.environ.get(args.slack_token_env, "").strip()
    webhook = os.environ.get(args.webhook_env, "").strip()
    if token:
        found = slack.resolve_channel(token, channel)          # raises SlackError if unknown
        if not found.get("is_member"):
            raise slack.SlackError(f"the app is not a member of {channel} ({found['id']}); invite it first")
        return slack.api_sender(token, found["id"]), f"slack_api:{found['id']}"
    if webhook and args.destination_basis == "webhook-declared":
        # An incoming webhook cannot be introspected, so the operator states which channel
        # it posts to and the receipt records that it was DECLARED, not verified.
        return slack.webhook_sender(webhook), "webhook_declared"
    raise slack.SlackError(
        f"no verified destination. Set {args.slack_token_env} (the channel is then resolved by name and "
        f"confirmed), or pass --destination-basis webhook-declared to assert that {args.webhook_env} "
        f"posts to {channel}")


def cmd_slack_test(args) -> int:
    """Send exactly one labelled connectivity test to a channel, once per day.

    The test is recorded in the same table as the reports, under its own key, so it both
    leaves an auditable receipt and demonstrates the property the Friday retries depend
    on: a second attempt with the same key sends nothing.
    """
    from .reporting import slack, store as report_store
    from .reporting.window import resolve_timezone

    channel = args.channel.strip()
    tz, _ = resolve_timezone(args.timezone)
    now = datetime.now(timezone.utc) if not args.now else datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    key = f"connectivity-test-{now.astimezone(tz).date().isoformat()}"
    schedule = f"every {args.delivery_weekday.capitalize()} at {args.due_hour:02d}:00 {args.timezone}"
    window_rule = "Friday 00:00 to the following Friday 00:00 (end exclusive)"
    message = {"blocks": slack.connectivity_test_blocks(channel=channel, schedule=schedule,
                                                        window_rule=window_rule),
               "text": slack.connectivity_test_text(channel)}
    conn = connect(args.database_url or _settings().database_url)
    report_store.ensure_schema(conn)
    try:
        sender, basis = _slack_destination(args, channel)
    except slack.SlackError as exc:
        print(f"slack-test refused: {exc}", file=sys.stderr)
        return EXIT_REPORT_NOT_SENT
    if args.dry_run:
        print(json.dumps({"sent": False, "reason": "dry_run", "key": key, "channel": channel,
                          "destination_basis": basis, "message": message}, indent=2))
        return 0
    try:
        out = report_store.deliver_guarded(conn, key=key, channel=channel,
                                           kind=report_store.CONNECTIVITY_TEST, sender=sender,
                                           message=message, destination_basis=basis, resend=args.resend)
    except slack.SlackError as exc:
        print(f"slack-test failed: {exc}", file=sys.stderr)
        return EXIT_REPORT_NOT_SENT
    print(json.dumps(out, indent=2, default=str))
    return 0


def _weekly_report_destination_basis(args) -> str:
    """What a real send WOULD use, so a rehearsal states it without establishing it."""
    if os.environ.get(args.slack_token_env, "").strip():
        return "slack_api (channel id resolved by name at send time)"
    if args.destination_basis == "webhook-declared":
        return "webhook_declared"
    return "none: no verified destination"


def _weekly_report_send(conn, args, report, window, now):
    """Decide whether this tick may post, and post at most once if it may.

    The rule, in order: only a closed week is ever published; the clock is read in the
    REPORT's timezone so 06:00 local stays 06:00 across a daylight-saving change; the
    data must have closed; and between 06:00 and 07:00 a tick that finds the week still
    closing simply waits for the next one. After 07:00 an unclosed week gets a labelled
    status notice -- never partial numbers dressed as the final report -- and the real
    report follows as soon as the data closes.
    """
    from .reporting import slack, store as report_store
    from .reporting.schedule import (ACTION_FINAL, ACTION_NOTICE, ACTION_SKIP, BEFORE_DUE, NOT_DELIVERY_DAY,
                                     decide, delivery_state, readiness)

    channel = (args.slack_channel or "").strip()
    if not channel:
        return {"sent": False, "error": "refused to send: --slack-channel names the confirmed destination"}
    detail = report.get("detail") or {}
    detail_url = (args.detail_url or detail.get("published_url")
                  or os.environ.get("TGTC_REPORT_DETAIL_URL", "").strip())
    detail_rows = detail.get("rows")
    if args.dry_run_send:
        # Rendering is not sending: a rehearsal may look at any week, including the one
        # in progress, and touches neither Slack nor the delivery record.
        message = {"blocks": slack.blocks_for(report, detail_url=detail_url or None, detail_rows=detail_rows),
                   "text": slack.text_for(report)}
        return {"sent": False, "reason": "dry_run", "channel": channel, "blocks": len(message["blocks"]),
                "destination_basis": _weekly_report_destination_basis(args),
                "preview": message["text"], "message": message}
    # The clock first: a tick that could not send anything needs no credential, no
    # readiness query and no noise. On any day but Friday the job is measuring the week
    # in progress, which is the point of running daily -- it is not an error.
    state = delivery_state(now, weekday=WEEKDAYS.index(args.delivery_weekday), due_hour=args.due_hour,
                           retry_until_hour=args.retry_until_hour, tz_name=args.timezone)
    schedule = (f"{args.delivery_weekday} {args.due_hour:02d}:00 {args.timezone}, "
                f"retry until {args.retry_until_hour:02d}:00")
    if state in (NOT_DELIVERY_DAY, BEFORE_DUE):
        return {"sent": False, "reason": state, "state": state, "channel": channel, "schedule": schedule}
    # It IS the delivery moment: holding anything but a closed week is now a real
    # misconfiguration, and a missing destination is a real delivery failure.
    if report["window"]["kind"] != "weekly":
        return {"sent": False, "state": state, "channel": channel, "schedule": schedule,
                "error": "refused to send: only a closed week is published"}

    ready = readiness(conn, window, report)
    action, reason = decide(state, ready.ready)
    common = {"channel": channel, "state": state, "reason": reason, "schedule": schedule,
              "readiness": ready.to_dict()}
    if action == ACTION_SKIP:
        return {"sent": False, **common}

    try:
        sender, basis = _slack_destination(args, channel)
    except slack.SlackError as exc:
        return {"sent": False, "error": f"refused to send: {exc}", **common}
    common["destination_basis"] = basis
    try:
        if action == ACTION_FINAL:
            message = {"blocks": slack.blocks_for(report, detail_url=detail_url or None,
                                                  detail_rows=detail_rows),
                       "text": slack.text_for(report)}
            out = report_store.deliver_once(conn, report, sender=sender, channel=channel, message=message,
                                            kind=report_store.FINAL, destination_basis=basis, resend=args.resend)
            return {**out, **common}
        assert action == ACTION_NOTICE
        notice = {"blocks": slack.status_notice_blocks(report, ready.to_dict(),
                                                       retry_until=f"{args.retry_until_hour:02d}:00 {args.timezone}"),
                  "text": f"TGTC weekly report for {report['window']['window_label']} is delayed: the week's data "
                          "has not closed yet"}
        out = report_store.deliver_once(conn, report, sender=sender, channel=channel, message=notice,
                                        kind=report_store.STATUS_NOTICE, destination_basis=basis)
        return {**out, **common}
    except (report_store.DeliveryRefused, slack.SlackError) as exc:
        return {"sent": False, "error": str(exc), **common}
    except Exception as exc:  # noqa: BLE001 - a transport failure must be visible, never swallowed
        return {"sent": False, "error": f"delivery failed: {type(exc).__name__}: {exc}", **common}


def cmd_weekly_report(args) -> int:
    """Measure a reporting week from the database, store it, and deliver it at most once.

    Reads the pipeline's own receipts, so it needs no run artifacts and no provider
    call: generating a report spends nothing. Delivery happens only when it is asked
    for explicitly AND the destination is confirmed on the command line, because a
    report sent to the wrong place cannot be recalled.
    """
    from .reporting import pipeline, store as report_store
    from .reporting.window import is_due

    now = datetime.now(timezone.utc)
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    if args.if_due_hour is not None:
        due = is_due(now, weekday=WEEKDAYS.index(args.if_due_weekday), hour=args.if_due_hour,
                     minute=args.if_due_minute, tz_name=args.timezone)
        if not due:
            print(json.dumps({"skipped": "not_due", "now": now.isoformat(),
                              "schedule": f"{args.if_due_weekday} {args.if_due_hour:02d}:{args.if_due_minute:02d} "
                                          f"{args.timezone}"}))
            return 0
    week_start = None
    kind = "weekly"
    week = args.week
    if week == "auto":
        # On the delivery weekday the closed week is the subject; on any other day the
        # week in progress is, so a problem is visible days before Friday.
        week = "last" if is_due(now, weekday=WEEKDAYS.index(args.if_due_weekday), hour=0,
                                tz_name=args.timezone) else "current"
    if week == "current":
        kind = "partial"
    elif week not in ("last", ""):
        week_start = datetime.strptime(week, "%Y-%m-%d").date()

    prices = {}
    for provider, name in (("apollo", "TGTC_APOLLO_CREDIT_USD"), ("fantastic", "TGTC_FANTASTIC_RECORD_USD")):
        raw = os.environ.get(name, "").strip()
        if raw:
            prices[provider] = float(raw)

    s = _settings()
    conn = connect(args.database_url or s.database_url)
    if not args.no_store:
        report_store.ensure_schema(conn)
    result = pipeline.generate_and_store(
        conn, now=now, out_dir=args.out_dir or None, store_report=not args.no_store,
        lead_detail=not args.no_lead_detail,
        kind=kind, weeks_back=args.weeks_back, week_start=week_start, tz_name=args.timezone,
        compare_previous=not args.no_compare, unit_prices=prices, target_per_run=args.target)
    report = result["report"]

    window = result["window"]
    # The file goes to its private destination BEFORE the message is built, so the link
    # in Slack is one that was uploaded, shared and opened -- never a URL we hoped for.
    if not args.no_lead_detail and report["window"]["kind"] == "weekly":
        published = pipeline.publish_detail(conn, report)
        result["detail_publication"] = published
        if published.get("published") or published.get("reason") == "already_published":
            report["detail"] = {**(report.get("detail") or {}),
                                "published_url": published.get("url"), "state": "published"}
    if args.lead_export:
        from .reporting import export as lead_export
        rows = lead_export.lead_rows(conn, window)
        path = lead_export.write_csv(args.lead_export, rows)
        result["lead_export"] = {"path": str(path), "rows": len(rows)}

    if args.print_format == "text":
        from .reporting import render
        print(render.render_text(report))
    elif args.print_format == "json":
        print(json.dumps(report, indent=2, default=str))

    delivery = {"sent": False, "reason": "not requested"}
    if args.send != "none":
        delivery = _weekly_report_send(conn, args, report, window, now)
        if delivery.get("message") is not None:
            # One line, on purpose: a container's log lines can arrive out of order, and
            # a rehearsal that has to be reassembled by hand is not a rehearsal.
            print("SLACK_PREVIEW " + json.dumps(delivery.pop("message"), separators=(",", ":")))
        if delivery.get("error"):
            print(f"weekly-report: {delivery['error']}", file=sys.stderr)
            print(json.dumps({"delivery": delivery}, indent=2, default=str))
            return EXIT_REPORT_NOT_SENT

    summary = {
        "report_id": result["report_id"],
        "window": report["window"]["window_label"],
        "window_utc": [report["window"]["window_start_utc"], report["window"]["window_end_utc"]],
        "kind": report["window"]["kind"],
        "instantly_created_unique_people": report["delivery"]["instantly_created_unique_people"],
        "airtable_records_created": report["delivery"]["airtable_records_created"],
        "reconciliation_holds": report["reconciliation"]["identity_holds"],
        "status": report["status"],
        "lead_detail": report.get("detail"),
        "detail_publication": result.get("detail_publication"),
        "integrity_alerts": report["integrity_alerts"],
        "alerts": report["alerts"],
        "notes": report["notes"],
        "stored": bool(result.get("stored")),
        "artifacts": result.get("artifacts"),
        "lead_export": result.get("lead_export"),
        "delivery": delivery,
    }
    print(json.dumps(summary, indent=2, default=str))
    failing = {"integrity": report["integrity_alerts"], "alerts": report["alerts"], "never": []}[args.fail_on]
    if failing:
        return EXIT_REPORT_ATTENTION
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tgtc_core", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("migrate", cmd_migrate), ("describe", cmd_describe), ("cycle", cmd_cycle),
                     ("run-target", cmd_run_target), ("work", cmd_work),
                     ("deliver", cmd_deliver), ("ledger", cmd_ledger), ("import-airtable", cmd_import_airtable),
                     ("prune", cmd_prune), ("demo", cmd_demo), ("check-db", cmd_check_db), ("budget", cmd_budget),
                     ("run-daily", cmd_run_daily)):
        p = sub.add_parser(name)
        p.add_argument("--database-url", default="")
        p.add_argument("--max-items", type=int, default=10000 if name in ("run-target", "run-daily") else 1000)
        p.add_argument("--no-acquire", action="store_true")
        p.add_argument("--no-deliver", action="store_true")
        p.add_argument("--budget-id", default="")
        p.add_argument("--i-understand-spend", action="store_true", help="required for cycle/work/deliver against real providers")
        if name == "work":
            p.add_argument("--kind", required=True, choices=("resolve_identity", "classify", "qualify_opportunity"))
        if name == "run-target":
            p.add_argument("--target", type=int, default=None)
            p.add_argument("--max-rounds", type=int, default=None)
        if name == "run-daily":
            p.add_argument("--budget-kind", required=True, choices=("scheduled", "manual", "canary"))
            p.add_argument("--target", type=int, default=1000, help="MINIMUM fresh Instantly creations")
            p.add_argument("--block-pages", type=int, default=2, help="Fantastic pages per block (<= 250 records)")
            p.add_argument("--max-rounds", type=int, default=300)
        if name == "budget":
            p.add_argument("--budget-kind", default="", choices=("", "scheduled", "manual", "canary", "sidecar"))
            p.add_argument("--expires-hours", type=int, default=24)
            p.add_argument("--fantastic-requests", type=int, required=True)
            p.add_argument("--fantastic-credits", type=int, required=True)
            p.add_argument("--apollo-requests", type=int, required=True)
            p.add_argument("--apollo-credits", type=int, required=True)
            p.add_argument("--anthropic-requests", type=int, required=True)
            p.add_argument("--anthropic-input-tokens", type=int, required=True)
            p.add_argument("--anthropic-output-tokens", type=int, required=True)
        p.set_defaults(fn=fn)
    p = sub.add_parser("budget-id", help="print the namespaced budget id for a kind of run")
    p.add_argument("--kind", required=True, choices=("scheduled", "manual", "canary", "sidecar"))
    p.set_defaults(fn=cmd_budget_id)
    p = sub.add_parser("canary-24h", help="daily 24h acquisition canary; needs FANTASTIC_DAILY_24H_CANARY=1")
    p.add_argument("--state-dir", required=True, help="NEW evidence directory; never a production path")
    p.add_argument("--registry", default="", help="read-only JSON export of already-acquired postings")
    p.add_argument("--size-exclusion-employers", default="", help="read-only JSON employers for exclude_organization_slug")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--max-records", type=int, default=5000)
    p.add_argument("--max-requests", type=int, default=25)
    p.add_argument("--preflight-counts", action="store_true", help="one zero-job-credit count per partition first")
    p.add_argument("--dry-run", action="store_true", help="render payloads and the offset sequence; no request")
    p.add_argument("--i-understand-spend", action="store_true")
    p.set_defaults(fn=cmd_canary_24h)
    p = sub.add_parser("canary-24h-strategy",
                       help="provider-confirmed 24h strategy canary; needs FANTASTIC_DAILY_24H_CANARY=1")
    p.add_argument("--state-dir", required=True, help="NEW evidence directory; never a production path")
    p.add_argument("--phase", choices=("counts", "records"), default="counts",
                   help="counts: zero-job-credit count matrix only; records: bounded billed pages per arm")
    p.add_argument("--registry", default="", help="read-only JSON export of already-acquired postings")
    p.add_argument("--size-exclusion-employers", default="", help="read-only JSON employers for exclude_organization_slug")
    p.add_argument("--arms", default="", help="comma list of arm keys (default: all five)")
    p.add_argument("--max-records", type=int, default=5000)
    p.add_argument("--max-requests", type=int, default=25)
    p.add_argument("--max-records-per-arm", type=int, default=1000)
    for flag in ("--wf-yc-industry", "--wf-yc-agency", "--ats-missing-industry", "--ats-missing-agency"):
        p.add_argument(flag, choices=("on", "off"), default="on")
    p.add_argument("--ats-missing-full-time", choices=("on", "off"), default="off")
    p.add_argument("--dry-run", action="store_true", help="render every arm's payload; no request")
    p.add_argument("--i-understand-spend", action="store_true")
    p.set_defaults(fn=cmd_canary_24h_strategy)
    p = sub.add_parser("weekly-report", help="measure a reporting week from the database (spends nothing)")
    p.add_argument("--database-url", default="")
    p.add_argument("--week", default="auto",
                   help="auto (the closed week on the delivery weekday, the week in progress otherwise), "
                        "last, current, or a YYYY-MM-DD week start")
    p.add_argument("--weeks-back", type=int, default=0, help="with --week last: how many closed weeks to step back")
    p.add_argument("--timezone", default="America/Los_Angeles")
    p.add_argument("--out-dir", default="", help="also write <report_id>.json and .txt here")
    p.add_argument("--lead-export", default="", help="write the private lead-level CSV here (outside the repository)")
    p.add_argument("--no-store", action="store_true", help="do not write the report_runs row (pure read)")
    p.add_argument("--no-lead-detail", action="store_true",
                   help="skip generating this week's private lead-level file")
    p.add_argument("--no-compare", action="store_true", help="skip the previous-week comparison")
    p.add_argument("--target", type=int, default=1000, help="the per-run minimum a day is flagged against")
    p.add_argument("--print-format", choices=("text", "json", "none"), default="text")
    p.add_argument("--if-due-weekday", choices=WEEKDAYS, default="friday")
    p.add_argument("--if-due-hour", type=int, default=None,
                   help="only produce the report at or after this hour in --timezone on --if-due-weekday")
    p.add_argument("--if-due-minute", type=int, default=0)
    p.add_argument("--send", choices=("none", "slack"), default="none")
    p.add_argument("--slack-channel", default="", help="the confirmed destination, e.g. '#gtm-engineering'")
    p.add_argument("--slack-token-env", default="SLACK_BOT_TOKEN",
                   help="env var holding a bot token; with it the channel is resolved by name and confirmed")
    p.add_argument("--webhook-env", default="SLACK_WEEKLY_REPORT_WEBHOOK_URL")
    p.add_argument("--destination-basis", choices=("verify", "webhook-declared"), default="verify",
                   help="verify: resolve the channel id from the workspace. webhook-declared: the operator "
                        "asserts which channel the opaque webhook posts to")
    p.add_argument("--detail-url", default="", help="link to the secure lead-level detail, when one is published")
    p.add_argument("--delivery-weekday", choices=WEEKDAYS, default="friday")
    p.add_argument("--due-hour", type=int, default=6, help="delivery moment in --timezone")
    p.add_argument("--retry-until-hour", type=int, default=7,
                   help="retry silently until this local hour; after it, post a labelled status notice instead")
    p.add_argument("--dry-run-send", action="store_true", help="render the Slack message and send nothing")
    p.add_argument("--resend", action="store_true", help="send again a week already delivered (never automatic)")
    p.add_argument("--fail-on", choices=("integrity", "alerts", "never"), default="never",
                   help="exit 4 on integrity problems (an unexplained difference, a missing run on a covered "
                        "day, a Control creation, a refused run), on any alert, or never")
    p.add_argument("--fail-on-alerts", dest="fail_on", action="store_const", const="alerts",
                   help="deprecated spelling of --fail-on alerts")
    p.add_argument("--now", default="", help="evaluate as of this instant (rehearsal and tests)")
    p.set_defaults(fn=cmd_weekly_report)
    p = sub.add_parser("slack-test", help="send one labelled connectivity test to a channel (no pipeline data)")
    p.add_argument("--database-url", default="")
    p.add_argument("--channel", required=True, help="the confirmed destination, e.g. '#gtm-engineering'")
    p.add_argument("--slack-token-env", default="SLACK_BOT_TOKEN")
    p.add_argument("--webhook-env", default="SLACK_WEEKLY_REPORT_WEBHOOK_URL")
    p.add_argument("--destination-basis", choices=("verify", "webhook-declared"), default="verify")
    p.add_argument("--timezone", default="America/Los_Angeles")
    p.add_argument("--delivery-weekday", choices=WEEKDAYS, default="friday")
    p.add_argument("--due-hour", type=int, default=6)
    p.add_argument("--dry-run", action="store_true", help="render the test message and send nothing")
    p.add_argument("--resend", action="store_true", help="send today's test again (never automatic)")
    p.add_argument("--now", default="", help="evaluate as of this instant (tests)")
    p.set_defaults(fn=cmd_slack_test)
    p = sub.add_parser("report-export", help="write a stored week's private lead-level file out")
    p.add_argument("--database-url", default="")
    p.add_argument("--report-id", required=True)
    p.add_argument("--out", required=True, help="a path OUTSIDE the repository")
    p.set_defaults(fn=cmd_report_export)
    p = sub.add_parser("report-detail-link", help="record where a week's detail was published, and to whom")
    p.add_argument("--database-url", default="")
    p.add_argument("--report-id", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--viewers", required=True, help="comma-separated accounts that were granted access")
    p.set_defaults(fn=cmd_report_detail_link)
    p = sub.add_parser("replies-poll", help="ingest Instantly replies for the nine campaigns (never sends)")
    p.add_argument("--database-url", default="")
    p.add_argument("--dry-run", action="store_true", help="classify and report; write nothing, move no cursor")
    p.add_argument("--max-pages", type=int, default=10)
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--resume", action="store_true",
                   help="continue the stored cursor deeper into history instead of reading from the top")
    p.set_defaults(fn=cmd_replies_poll)
    args = parser.parse_args(argv)
    _require_acceptance_command(args)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
