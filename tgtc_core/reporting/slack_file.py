"""Upload one reconciled weekly CSV into a verified Slack channel.

The existing incoming webhook can send the headline, but cannot upload files.
This transport needs a bot token with files:write, files:read and channels:read.
It never places the CSV in a webhook payload or in a job log.
"""

from __future__ import annotations

from typing import Any, Dict

from . import slack


class SlackFileError(RuntimeError):
    """The CSV could not be uploaded and verified in the requested channel."""


def _api(response, stage: str) -> Dict[str, Any]:
    if response.status_code >= 300:
        raise SlackFileError(f"Slack {stage} returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise SlackFileError(f"Slack {stage} did not return JSON") from None
    if not body.get("ok"):
        raise SlackFileError(f"Slack {stage} refused: {body.get('error', 'unknown_error')}")
    return body


def publish(token: str, channel: str, filename: str, content: bytes,
            *, report_id: str, rows: int, session=None) -> Dict[str, Any]:
    """Upload and share a CSV; return a Slack permalink after verifying its channel.

    The caller records the permalink in the report ledger. An unsuccessful upload
    never turns into a published detail URL.
    """
    if not token or not content or not channel.startswith("#"):
        raise SlackFileError("bot token, channel name and CSV content are required")
    if session is None:
        import requests
        session = requests
    try:
        resolved = slack.resolve_channel(token, channel)
    except slack.SlackError as exc:
        raise SlackFileError(str(exc)) from exc
    if not resolved.get("is_member"):
        raise SlackFileError(f"the app is not a member of {channel}; no CSV was uploaded")
    channel_id = resolved["id"]
    headers = {"Authorization": f"Bearer {token}"}
    ticket = _api(session.post(f"{slack.SLACK_API}/files.getUploadURLExternal", headers=headers,
                               data={"filename": filename, "length": str(len(content))}, timeout=30),
                  "upload ticket")
    upload_url, file_id = ticket.get("upload_url"), ticket.get("file_id")
    if not upload_url or not file_id:
        raise SlackFileError("Slack returned an incomplete upload ticket")
    received = session.post(upload_url, data=content, headers={"Content-Type": "text/csv"}, timeout=180)
    if received.status_code >= 300:
        raise SlackFileError(f"Slack file upload returned HTTP {received.status_code}")
    _api(session.post(f"{slack.SLACK_API}/files.completeUploadExternal", headers=headers,
                      json={"files": [{"id": file_id, "title": filename}], "channel_id": channel_id,
                            "initial_comment": f"TGTC weekly lead detail · {report_id} · {rows:,} rows"},
                      timeout=60), "file share")
    info = _api(session.get(f"{slack.SLACK_API}/files.info", headers=headers,
                            params={"file": file_id}, timeout=30), "file lookup")
    file = info.get("file") or {}
    url = file.get("permalink")
    if file.get("id") != file_id or not url or not url.startswith("https://"):
        raise SlackFileError("Slack did not confirm the uploaded file's private permalink")
    return {"url": url, "file_id": file_id, "channel_id": channel_id, "rows": rows,
            "bytes": len(content)}
