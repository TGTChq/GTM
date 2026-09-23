"""The Slack message, and the two ways it can reach a channel.

**The destination is verified, never inferred.** An incoming webhook URL is opaque:
nothing in it says which channel it posts to, and no API will tell you. So the
preferred path is a bot token, where the channel is resolved BY NAME against the
workspace (``conversations.list``) and the message is addressed to that channel id --
the destination is then a fact, and a receipt records it. The webhook path still
exists, because it is the credential this project has, but it refuses to run unless an
operator states in the command which channel it targets, and the receipt records that
the destination was *declared* rather than verified. The URL is never printed, logged
or put in a receipt.

**The message is built for a reader, not for a log.** Every figure carries its
denominator, each stage keeps its own unit, exceptions are short and explicit, and no
name, email or phone number appears anywhere in it -- the lead-level detail is a
separate, private export.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

SLACK_API = "https://slack.com/api"

#: Slack refuses a section longer than this; we cut at a line boundary well before it.
_SECTION_LIMIT = 2900


class SlackError(RuntimeError):
    """Slack refused, or the destination could not be established."""


# --------------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------------

def _n(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return f"{value:,}" if isinstance(value, int) else str(value)


def _pct(part: Optional[float], whole: Optional[float]) -> str:
    """A percentage is never printed without the denominator it came from."""
    if not whole or part is None:
        return "n/a"
    return f"{100.0 * part / whole:.1f}%"


def _section(text: str) -> Dict[str, Any]:
    if len(text) > _SECTION_LIMIT:
        kept: List[str] = []
        used = 0
        for line in text.splitlines():
            if used + len(line) + 1 > _SECTION_LIMIT - 40:
                break
            kept.append(line)
            used += len(line) + 1
        text = "\n".join(kept) + "\n_(truncated)_"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _code(lines: List[str]) -> str:
    return "```\n" + "\n".join(lines) + "\n```"


# --------------------------------------------------------------------------------
# the report message
# --------------------------------------------------------------------------------

def blocks_for(report: Dict[str, Any], *, detail_url: Optional[str] = None,
               detail_hint: Optional[str] = None) -> List[Dict[str, Any]]:
    """The weekly report as Slack blocks, in the seven sections the readers asked for."""
    w, d, j = report["window"], report["delivery"], report["jobs"]
    a, c, u, b = report["acquisition"], report["contacts"], report["units"], report["backlog"]
    runs, recon, spend = report["runs"], report["reconciliation"], report["spend"]
    alerts, notes = report.get("alerts", []), report.get("notes", [])
    integrity = report.get("integrity_alerts", [])
    status = ("🟥 integrity: " + str(len(integrity)) + " to check" if integrity
              else f"⚠️ {len(alerts)} exception(s)" if alerts else "✅ reconciled")

    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"TGTC weekly pipeline — {w['window_label']}"}},
        # 1. window, cutoff, status
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"*Window* {w['window_start_local'][:16]} → {w['window_end_local'][:16]} "
            f"{w['timezone']} (end exclusive)  •  *Data cutoff* {w['data_cutoff_utc']}  •  "
            f"*Status* {status}  •  {w['iso_week']}")}]},
        _section(
            f"*Net-new leads created in the nine Challenger campaigns: {_n(d['instantly_created_unique_people'])}*\n"
            f"• {_n(b['created_from_this_weeks_approvals'])} from approvals made this week, "
            f"{_n(b['created_from_earlier_approvals_backlog'])} from earlier approvals (backlog)\n"
            f"• Production runs in the window: {_n(runs['run_count'])} "
            f"({_n(runs['completed_runs'])} completed, {_n(runs['refused_runs'])} refused)"),
        {"type": "divider"},
        # 2. capture and review, with the denominators spelled out
        _section(
            "*Jobs captured and reviewed*\n"
            f"• Records captured from the provider: *{_n(a['fantastic_records_returned'])}* "
            f"({_n(a['fantastic_records_unique_ids'])} unique ids, {_n(a['fantastic_requests'])} requests)\n"
            f"• New unique jobs: *{_n(j['new_jobs_unique'])}* — "
            f"{_pct(j['new_jobs_unique'], a['fantastic_records_returned'])} of the "
            f"{_n(a['fantastic_records_returned'])} records captured\n"
            f"• Jobs reviewed: *{_n(j['jobs_reviewed'])}* classifications — "
            f"{_pct(j['jobs_reviewed'], j['new_jobs_unique'])} of this week's "
            f"{_n(j['new_jobs_unique'])} new jobs (reviews also cover jobs first seen earlier)\n"
            f"• Still pending review: *{_n(j['jobs_pending_review'])}* — "
            f"{_pct(j['jobs_pending_review'], j['new_jobs_unique'])} of this week's new jobs"),
        # 3. qualified, employers, units
        _section(
            "*Qualified work*\n"
            f"• Qualified jobs: *{_n(j['jobs_qualified'])}* — {_pct(j['jobs_qualified'], j['jobs_reviewed'])} "
            f"of the {_n(j['jobs_reviewed'])} reviewed "
            f"(rejected {_n(j['jobs_rejected_excluded'])}, no campaign fit {_n(j['jobs_no_campaign_fit'])})\n"
            f"• Unique employers: *{_n(u['new_employers'])}*\n"
            f"• Company × campaign units: *{_n(u['new_units'])}* "
            f"({_n(u['units_worked_for_contacts'])} worked for contacts)"),
        # 4. contacts, Airtable and Instantly -- three separate measurements
        _section(
            "*Contacts and delivery* _(three different things, never added together)_\n"
            f"• Verified contacts: *{_n(c['emails_verified'])}* verified emails "
            f"from {_n(c['candidates_found'])} candidates found; "
            f"*{_n(c['contacts_approved'])}* approved, {_n(c['compliance_blocked'])} compliance-blocked\n"
            f"• Airtable records created: *{_n(d['airtable_records_created'])}*\n"
            f"• Genuine new Instantly creations: *{_n(d['instantly_created_unique_people'])}* unique people "
            f"(already existing {_n(d['instantly_already_existing'])}, rejected {_n(d['instantly_rejected'])})"),
        {"type": "divider"},
    ]

    # 5. daily trend, previous week, nine campaigns
    trend = [f"{'day':<12}{'created':>9}{'approved':>10}{'new jobs':>10}{'Apollo cr':>11}"]
    for day in report["daily"]:
        if day.get("unavailable"):
            trend.append(f"{day['weekday']} {day['date'][5:]:<8}{'unavailable — before this database begins':>40}")
            continue
        trend.append(f"{day['weekday']} {day['date'][5:]:<8}{day['instantly_created_unique_people']:>9,}"
                     f"{day['contacts_approved']:>10,}{day['new_jobs']:>10,}{_n(day['apollo_credits']):>11}")
    blocks.append(_section("*Daily trend* (local days, " + w["timezone"] + ")\n" + _code(trend)))

    previous = report.get("previous_week")
    if previous:
        rows = [f"{'metric':<34}{'this week':>12}{'previous':>12}{'change':>10}"]
        for metric, row in previous["metrics"].items():
            change = row["change"]
            rows.append(f"{metric[:34]:<34}{_n(row['this_week']):>12}{_n(row['previous_week']):>12}"
                        f"{('' if change is None else f'{change:+,}'):>10}")
        blocks.append(_section(f"*Against the previous week* ({previous['window']})\n" + _code(rows)))

    table = [f"{'campaign':<24}{'units':>7}{'approved':>10}{'created':>9}{'airtable':>10}{'blocked':>9}"]
    for row in report["by_campaign"]["campaigns"].values():
        table.append(f"{row['name'][:24]:<24}{row['new_units']:>7,}{row['contacts_approved']:>10,}"
                     f"{row['instantly_created_unique_people']:>9,}{row['airtable_records_created']:>10,}"
                     f"{row['compliance_blocked']:>9,}")
    blocks.append(_section("*All nine Challenger campaigns*\n" + _code(table)))

    # 6. provider consumption and cost per final new lead
    spend_lines = []
    for provider, row in spend["providers"].items():
        credits = row["credits_confirmed"] if row["credits_confirmed"] is not None else row["credits_estimated"]
        money = f"${_n(row['spend_usd'])}" if row["spend_usd"] is not None else "no verified unit price"
        spend_lines.append(f"• *{provider}*: {_n(row['requests'])} requests, {_n(credits)} credits — "
                           f"{_n(row['credits_per_final_lead'])} per final new lead ({money})")
    cost = spend["cost_per_final_lead_usd"]
    spend_lines.append(f"• *Cost per final new lead*: "
                       f"{('$' + _n(cost)) if cost is not None else 'not reported — a provider unit price is not verified'}")
    blocks.append(_section("*Provider consumption*\n" + "\n".join(spend_lines)))

    # 7. exceptions, short and explicit, then where the detail lives
    legacy = report["legacy_airtable_review"]
    exceptions = [f"• 🟥 {line}" for line in integrity[:4]]
    exceptions += [f"• ⚠️ {line}" for line in alerts if line not in integrity][:4]
    exceptions.append(
        f"• 🗂️ *Legacy exception (not delivered leads):* {_n(legacy['records'])} historical Airtable records "
        "with no genuine Instantly creation behind them — a review population, never counted as delivered, "
        "never archived or removed here")
    if report["coverage"]["local_days_unavailable"]:
        exceptions.append("• ◻️ *Unavailable:* " + ", ".join(report["coverage"]["local_days_unavailable"]) +
                          " — before this database's first record, reported as unavailable, not as zero")
    exceptions.append(
        f"• 🔎 *Reconciliation:* {_n(recon['airtable_records_created'])} Airtable records = "
        f"{_n(recon['genuine_instantly_creations_approvals'])} genuine creations + "
        f"{_n(recon['airtable_records_without_a_genuine_creation'])} with a named reason "
        f"(unexplained: {_n(recon['unexplained_difference'])})")
    for line in notes[:2]:
        exceptions.append(f"• ℹ️ {line}")
    blocks.append(_section("*Exceptions*\n" + "\n".join(exceptions)))

    detail = (f"<{detail_url}|Secure lead-level detail>" if detail_url
              else (detail_hint or "Lead-level detail is not published: it holds personal data and is generated "
                                   "on request as a private, deduplicated export (one row per lead, traceable to "
                                   "job, employer, person, campaign and both delivery receipts)"))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": (
        f"{detail} • no personal data appears in this summary • the 200-person phone sidecar is excluded from "
        f"every figure • report `{w['report_id']}` generated {report['generated_at']} from the core database")}]})
    return blocks


def text_for(report: Dict[str, Any]) -> str:
    """The notification line, which is all a phone shows."""
    w, d = report["window"], report["delivery"]
    alerts = report.get("alerts", [])
    state = "reconciled" if not alerts else f"{len(alerts)} exception(s)"
    return (f"TGTC weekly pipeline {w['window_label']}: "
            f"{d['instantly_created_unique_people']:,} net-new Challenger leads created ({state})")


def status_notice_blocks(report: Dict[str, Any], readiness: Dict[str, Any], *,
                         retry_until: str) -> List[Dict[str, Any]]:
    """The message that goes out when the week has NOT closed by the end of the retry
    window. It carries no pipeline numbers at all: partial figures presented next to the
    words "weekly report" become the number people remember."""
    w = report["window"]
    blockers = readiness.get("blockers") or ["the data has not finished closing"]
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "TGTC weekly report — delayed, not yet final"}},
        _section(
            f"*The report for {w['window_label']} is not being posted yet.* The week's production data had not "
            f"finished closing by {retry_until}, so there are no final numbers to publish. "
            "Partial figures are deliberately not shown here.\n\n*What is still open*\n"
            + "\n".join(f"• {item}" for item in blockers)),
        _section("*What happens next*\nThe job keeps retrying on its schedule and posts the complete, reconciled "
                 "report to this channel as soon as the data closes. If it does not close today, this notice is "
                 "the signal to look at the production run rather than at the report."),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"window {w['window_start_local'][:16]} → {w['window_end_local'][:16]} {w['timezone']} • "
            f"checked {report['generated_at']} • report `{w['report_id']}`")}]},
    ]


# --------------------------------------------------------------------------------
# destinations
# --------------------------------------------------------------------------------

def _post(url: str, *, token: Optional[str] = None, payload: Dict[str, Any]) -> Dict[str, Any]:
    import requests  # local import: a report that is not sent needs no HTTP stack

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.post(url, json=payload, headers=headers, timeout=30)
    if response.status_code >= 300:
        raise SlackError(f"Slack returned HTTP {response.status_code}")
    if response.headers.get("content-type", "").startswith("application/json"):
        body = response.json()
        if not body.get("ok", False):
            raise SlackError(f"Slack refused: {body.get('error', 'unknown_error')}")
        return body
    if response.text.strip().lower() != "ok":
        raise SlackError(f"Slack refused: {response.text[:120]}")
    return {"ok": True}


def resolve_channel(token: str, name: str) -> Dict[str, Any]:
    """Find a channel BY NAME in the workspace and return its id.

    This is the verification: the report is addressed to an id that was looked up from
    the name the operator asked for, so it cannot silently land somewhere else.
    """
    import requests

    wanted = name.lstrip("#").strip().lower()
    cursor, seen = "", 0
    while True:
        response = requests.get(
            f"{SLACK_API}/conversations.list",
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": 1000, "exclude_archived": "true", "cursor": cursor,
                    "types": "public_channel,private_channel"}, timeout=30)
        body = response.json()
        if not body.get("ok", False):
            raise SlackError(f"Slack refused conversations.list: {body.get('error', 'unknown_error')}")
        for channel in body.get("channels", []):
            seen += 1
            if str(channel.get("name", "")).lower() == wanted:
                return {"id": channel["id"], "name": channel["name"],
                        "is_private": bool(channel.get("is_private")),
                        "is_member": bool(channel.get("is_member"))}
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    raise SlackError(f"#{wanted} was not found among the {seen} channels this token can see; "
                     "the destination could not be verified, so nothing was sent")


def api_sender(token: str, channel_id: str) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    """Post as the app to a RESOLVED channel id."""

    def send(channel: str, message: Dict[str, Any]) -> Dict[str, Any]:
        body = _post(f"{SLACK_API}/chat.postMessage", token=token,
                     payload={"channel": channel_id, "blocks": message["blocks"],
                              "text": message["text"], "unfurl_links": False})
        return {"transport": "slack_api", "channel": channel, "channel_id": channel_id,
                "message_ts": body.get("ts"), "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    return send


def webhook_sender(url: str) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    """Post through an incoming webhook. The URL never leaves this closure."""

    def send(channel: str, message: Dict[str, Any]) -> Dict[str, Any]:
        _post(url, payload={"blocks": message["blocks"], "text": message["text"], "unfurl_links": False})
        return {"transport": "incoming_webhook", "channel": channel,
                "destination_basis": "declared by the operator; an incoming webhook cannot be introspected",
                "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    return send
