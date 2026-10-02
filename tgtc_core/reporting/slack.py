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

def headline_lines(report: Dict[str, Any]) -> List[str]:
    """The four figures, in the order they were asked for.

    The review percentage is only printed when both counts come from the same cohort;
    where there is no cohort it is omitted with its reason, because a ratio of two
    populations that do not contain each other is not a percentage of anything.
    """
    h = report["headline"]
    rate = h["jobs_review_rate"]
    jobs = f"*Jobs:* {_n(h['jobs_captured'])} captured / {_n(h['jobs_reviewed'])} reviewed"
    jobs += f" ({rate:.1f}%)" if rate is not None else f" (% omitted — {h['jobs_review_rate_omitted_because']})"
    added = f"*Added to Instantly:* {_n(h['added_to_instantly'])}"
    if h["added_from_earlier_approvals"]:
        added += f" _(includes {_n(h['added_from_earlier_approvals'])} from approvals made before this week)_"
    lines = [
        jobs,
        f"*Qualified opportunities:* {_n(h['qualified_opportunities'])}",
        f"*Contacts found:* {_n(h['contacts_found'])}",
        added,
    ]
    blocked = h.get("verified_contacts_blocked_for_outreach") or 0
    if blocked:
        # Reported beside the four figures, never inside them: a contact a compliance
        # rule forbids sending to is real capacity and is not a lead that went out.
        lines.append(f"_Verified but blocked for outreach: {_n(blocked)} — counted capacity, never sent._")
    return lines


def _integrity_line(report: Dict[str, Any]) -> Optional[str]:
    """Only a failure that puts the FIGURES in doubt earns a place beside them.

    An operational exception -- a day whose run never happened, a run the container
    replacement killed -- is real and is kept, but it belongs at the end: it does not
    make the numbers above wrong, and a warning sitting under the headline reads as if
    it does.
    """
    integrity = report.get("integrity_alerts") or []
    if not integrity:
        return None
    rest = len(report.get("alerts") or []) - 1
    return f"🟥 {integrity[0]}" + (f" _(+{rest} more, listed below)_" if rest > 0 else "")


def _short_run(run_id: str) -> str:
    """The tail of a run id: unique, and short enough to sit in a line people read."""
    return str(run_id).rsplit("-", 1)[-1][:12] or str(run_id)[:12]


def _operational_notes(report: Dict[str, Any]) -> List[str]:
    """What happened to the week's production, briefly, and never as a hole in the total.

    A recovered run's work IS in the figures above. Saying "interrupted" without saying
    that invites the reader to subtract something that was never missing.
    """
    runs = report.get("runs") or {}
    notes: List[str] = []
    missing = runs.get("local_days_without_a_run") or []
    if missing:
        notes.append(f"no production run on {', '.join(missing)}")
    for item in runs.get("interrupted_runs") or []:
        notes.append(
            f"the run started {str(item.get('started_at'))[:16].replace('T', ' ')}Z (`{_short_run(item.get('run_id'))}`) "
            f"was interrupted and could not close itself — its work is included in the figures above, "
            f"completed by `{_short_run(item.get('superseded_by'))}`")
    unavailable = (report.get("coverage") or {}).get("local_days_unavailable") or []
    if unavailable:
        notes.append(f"no data available for {', '.join(unavailable)} (before the database's first record)")
    counted = len(missing) + len(runs.get("interrupted_runs") or []) + (1 if unavailable else 0)
    others = len(report.get("alerts") or []) - len(report.get("integrity_alerts") or []) - counted
    if others > 0:
        notes.append(f"{others} further note(s) in the run ledger")
    return notes


def _footer(report: Dict[str, Any]) -> str:
    """Definitions and operational notes, at the end, in the small type."""
    w = report["window"]
    parts = [f"Week {w['window_start_local'][:10]} → {w['window_end_local'][:10]} {w['timezone']} "
             f"(end exclusive) • data cutoff {w['data_cutoff_utc']} • report `{w['report_id']}`"]
    notes = _operational_notes(report)
    if notes:
        parts.append("*Runs:* " + "; ".join(notes))
    parts.append("*Definitions:* jobs captured/reviewed are the same cohort • 'contacts found' = current "
                 "employer confirmed and work email verified • 'added' counts receipt-confirmed creations "
                 "only, never existing contacts, Control campaigns or the phone sidecar")
    return "\n".join(parts)


def blocks_for(report: Dict[str, Any], *, detail_url: Optional[str] = None,
               detail_rows: Optional[int] = None, detail_hint: Optional[str] = None) -> List[Dict[str, Any]]:
    """The weekly message, in the order a reader needs it: which week, the four figures,
    where the full CSV is, and then the small print.

    **Exactly one visual reference to the CSV.** When the file lives in this channel it
    already has its own card, and repeating its permalink here does not merely unfurl --
    posting a Slack file link SHARES the file again, which `unfurl_links: false` does not
    prevent. Measured 2026-09-25: two share entries 0.146s apart, so the same CSV was
    rendered twice. The link is therefore named, not linked, when Slack itself holds the
    file; a detail hosted anywhere else is still linked, because nothing there duplicates.
    """
    w = report["window"]
    in_slack = (report.get("detail") or {}).get("destination") == "slack"
    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"TGTC weekly pipeline — {w['window_label']} ({w['timezone']})"}},
        _section("\n".join(headline_lines(report))),
    ]
    integrity = _integrity_line(report)
    if integrity:
        blocks.append(_section(integrity))

    rows = f"{_n(detail_rows)} rows, one per genuine creation" if detail_rows is not None else "one row per genuine creation"
    if in_slack:
        # The file's own card in this channel is the single visual reference.
        blocks.append(_section(
            f"*Full CSV:* {rows}, reconciled against *Added to Instantly*."
            + (" Posted with this report, visible to members of this channel."
               if detail_url else
               " _The upload to this channel has not been confirmed; the file is generated and stored._")))
    elif detail_url:
        blocks.append(_section(
            f"*Lead-level detail (private):* <{detail_url}|this week's file> — {rows}, reconciled against "
            "*Added to Instantly*. Access is limited to the authorised team."))
    else:
        blocks.append(_section(
            f"*Lead-level detail:* _pending_ — {rows}, reconciled against *Added to Instantly*, but not "
            "published: no private destination and reader list has been verified yet. "
            + (detail_hint or "It is deliberately not posted here, because it contains personal data.")))

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _footer(report)}]})
    return blocks


def text_for(report: Dict[str, Any]) -> str:
    """The notification line, which is all a phone shows."""
    w, h = report["window"], report["headline"]
    return (f"TGTC weekly pipeline {w['window_label']}: {h['added_to_instantly']:,} added to Instantly, "
            f"{h['qualified_opportunities']:,} qualified opportunities, "
            f"{h['jobs_captured']:,} jobs captured")


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


def connectivity_test_blocks(*, channel: str, schedule: str, window_rule: str) -> List[Dict[str, Any]]:
    """One short message that proves the credential reaches the channel.

    It carries no pipeline data at all: a test that looks like a report teaches people
    to read a test as a report. It says what it is, what will arrive here later, and
    nothing else.
    """
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "TGTC reporting — connectivity test"}},
        _section(
            f"This is a one-off test that the weekly reporting job can reach *{channel}*. "
            "It carries no pipeline numbers.\n\n"
            f"From now on the TGTC weekly pipeline report is posted here *{schedule}*, covering "
            f"{window_rule}. If a production run or its delivery receipts are still closing at that "
            "moment, the job retries quietly for an hour and posts a short, labelled status notice "
            "instead of partial numbers — then the full report as soon as the data closes.\n\n"
            "Nothing else is ever posted here by this job, and no personal data appears in it."),
    ]


def connectivity_test_text(channel: str) -> str:
    return f"TGTC reporting — connectivity test for {channel} (no pipeline data)"


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
    # Asking for private channels as well requires groups:read. A token that only needs
    # to find a PUBLIC channel should not be made to carry that scope, so a missing_scope
    # refusal falls back to public channels once instead of failing the whole report.
    types = "public_channel,private_channel"
    cursor, seen = "", 0
    while True:
        response = requests.get(
            f"{SLACK_API}/conversations.list",
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": 1000, "exclude_archived": "true", "cursor": cursor,
                    "types": types}, timeout=30)
        body = response.json()
        if not body.get("ok", False):
            if body.get("error") == "missing_scope" and types != "public_channel":
                types, cursor, seen = "public_channel", "", 0
                continue
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
