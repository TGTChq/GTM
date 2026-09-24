"""A CSV file is shared once to the intended channel, only for a due final report."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tgtc_core.reporting import detail, pipeline, slack, slack_file


class Response:
    def __init__(self, body=None, code=200):
        self.status_code, self.body = code, body or {}

    def json(self):
        return self.body


class SlackAPI:
    def __init__(self, *, reject_share=False):
        self.calls = []
        self.reject_share = reject_share

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/files.getUploadURLExternal"):
            return Response({"ok": True, "upload_url": "https://upload.slack.test/ticket", "file_id": "F123"})
        if url.endswith("/files.completeUploadExternal"):
            return Response({"ok": not self.reject_share, "error": "not_in_channel"})
        assert url == "https://upload.slack.test/ticket"
        return Response()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert url.endswith("/files.info")
        return Response({"ok": True, "file": {"id": "F123", "permalink": "https://tgtc.slack.com/files/F123"}})


def _week():
    return {"window": {"kind": "weekly", "report_id": "weekly-2026-09-18"},
            "detail": {"reconciles": True}}


def test_upload_uses_verified_channel_and_entire_csv(monkeypatch):
    api = SlackAPI()
    monkeypatch.setattr("tgtc_core.reporting.slack.resolve_channel",
                        lambda token, name: {"id": "C123", "is_member": True})
    out = slack_file.publish("token", "#gtm-engineering", "weekly.csv", b"email\na@b.com\n",
                             report_id="weekly-2026-09-18", rows=1, session=api)
    assert out["url"] == "https://tgtc.slack.com/files/F123"
    assert out["channel_id"] == "C123"
    ticket, upload, complete, info = api.calls
    assert ticket[1]["data"]["length"] == str(len(b"email\na@b.com\n"))
    assert upload[1]["data"] == b"email\na@b.com\n"
    assert complete[1]["json"]["channel_id"] == "C123"
    assert complete[1]["json"]["files"] == [{"id": "F123", "title": "weekly.csv"}]
    assert info[1]["params"]["file"] == "F123"


@pytest.mark.parametrize("member,reject", [(False, False), (True, True)])
def test_no_published_link_when_channel_or_share_fails(monkeypatch, member, reject):
    api = SlackAPI(reject_share=reject)
    monkeypatch.setattr("tgtc_core.reporting.slack.resolve_channel",
                        lambda token, name: {"id": "C123", "is_member": member})
    with pytest.raises(slack_file.SlackFileError):
        slack_file.publish("token", "#gtm-engineering", "weekly.csv", b"a,b\n",
                           report_id="weekly-2026-09-18", rows=1, session=api)
    if not member:
        assert api.calls == []


def test_publish_records_slack_file_and_retry_does_not_upload(monkeypatch):
    report = _week()
    uploads = []
    saved = {"csv": b"email\na@b.com\n", "row_count": 1, "sha256": "sha", "published_url": None}
    publications = []
    monkeypatch.setattr(detail, "load", lambda conn, report_id: saved)

    def record(conn, report_id, *, url, viewers):
        saved["published_url"] = url
        publications.append((report_id, url, viewers))

    monkeypatch.setattr(detail, "record_publication", record)

    def fake_publish(token, channel, filename, content, **kwargs):
        uploads.append((token, channel, filename, content))
        return {"url": "https://tgtc.slack.com/files/F123", "file_id": "F123",
                "channel_id": "C123", "bytes": len(content)}

    monkeypatch.setattr(slack_file, "publish", fake_publish)
    env = {"TGTC_REPORT_DETAIL_DESTINATION": "slack", "SLACK_BOT_TOKEN": "token"}
    first = pipeline.publish_detail(None, report, env=env, channel="#gtm-engineering")
    assert first["published"] and first["rows"] == 1
    assert uploads[0][3] == saved["csv"]
    assert publications[0][2] == ["slack-channel:#gtm-engineering:C123"]
    again = pipeline.publish_detail(None, report, env=env, channel="#gtm-engineering")
    assert again["reason"] == "already_published" and len(uploads) == 1


def test_slack_mode_requires_token_and_reconciled_detail(monkeypatch):
    report = _week()
    monkeypatch.setattr(detail, "load", lambda conn, report_id: {
        "csv": b"email\na@b.com\n", "row_count": 1, "sha256": "sha", "published_url": None})
    env = {"TGTC_REPORT_DETAIL_DESTINATION": "slack"}
    assert pipeline.publish_detail(None, report, env=env, channel="#gtm-engineering")["missing"] == [
        "SLACK_BOT_TOKEN"]
    monkeypatch.setattr(slack_file, "publish", lambda *a, **k: pytest.fail("must not upload"))
    report["detail"]["reconciles"] = False
    assert "does not reconcile" in pipeline.publish_detail(
        None, report, env={**env, "SLACK_BOT_TOKEN": "token"}, channel="#gtm-engineering")["reason"]


def test_slack_file_only_publishes_before_an_actual_final_report(monkeypatch):
    from tgtc_core import __main__ as cli
    from tgtc_core.reporting import schedule, slack, store

    report = _week()
    report["detail"].update({"rows": 1, "published_url": None})
    args = SimpleNamespace(slack_channel="#gtm-engineering", detail_url="", dry_run_send=False,
                           delivery_weekday="friday", due_hour=6, retry_until_hour=7,
                           timezone="America/Los_Angeles", resend=False,
                           slack_token_env="SLACK_BOT_TOKEN", destination_basis="webhook-declared")
    publications = []

    def fake_publish(*a, **kw):
        publications.append(kw["channel"])
        return {"published": True, "url": "https://tgtc.slack.com/files/F123"}

    monkeypatch.setattr(pipeline, "publish_detail", fake_publish)
    monkeypatch.setattr(cli, "_slack_destination", lambda *a: (lambda *a: {}, "slack_api:C123"))
    monkeypatch.setattr(schedule, "readiness", lambda *a: SimpleNamespace(ready=True, to_dict=lambda: {}))
    monkeypatch.setattr(slack, "blocks_for", lambda r, **kw: [{"detail_url": kw.get("detail_url")}])
    monkeypatch.setattr(slack, "text_for", lambda r: "report")
    messages = []
    monkeypatch.setattr(store, "deliver_once", lambda *a, **kw: messages.append(kw["message"]) or
                        {"sent": True})
    monkeypatch.setenv("TGTC_REPORT_DETAIL_DESTINATION", "slack")
    thursday = datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc)
    assert not cli._weekly_report_send(None, args, report, None, thursday)["sent"]
    args.dry_run_send = True
    friday = datetime(2026, 9, 25, 13, 0, tzinfo=timezone.utc)
    assert not cli._weekly_report_send(None, args, report, None, friday)["sent"]
    assert publications == [] and messages == []
    args.dry_run_send = False
    assert cli._weekly_report_send(None, args, report, None, friday)["sent"]
    assert publications == ["#gtm-engineering"]
    assert messages[0]["blocks"][0]["detail_url"] == "https://tgtc.slack.com/files/F123"
    # A failed file share still delivers the four-number summary with no invented link.
    monkeypatch.setattr(pipeline, "publish_detail", lambda *a, **kw: {
        "published": False, "reason": "Slack file share refused"})
    report["detail"]["published_url"] = None
    failed = cli._weekly_report_send(None, args, report, None, friday)
    assert failed["sent"] and failed["detail_publication"]["published"] is False
    assert messages[-1]["blocks"][0]["detail_url"] is None


def test_slack_summary_describes_actual_file_audience():
    report = {"window": {"window_label": "Sep 18–24", "window_start_local": "2026-09-18T00:00",
                         "window_end_local": "2026-09-25T00:00", "timezone": "America/Los_Angeles",
                         "data_cutoff_utc": "2026-09-25T07:00Z", "report_id": "weekly-2026-09-18"},
              "headline": {"jobs_captured": 1, "jobs_reviewed": 1, "jobs_review_rate": 100,
                           "qualified_opportunities": 1, "contacts_found": 1,
                           "added_to_instantly": 1, "added_from_earlier_approvals": 0},
              "detail": {"destination": "slack"}}
    blocks = slack.blocks_for(report, detail_url="https://tgtc.slack.com/files/F123", detail_rows=1)
    body = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    assert "visible to members of this channel" in body
    assert "Access is limited to the authorised team" not in body


def test_a_delay_notice_never_uploads_the_csv(monkeypatch):
    """The 07:00 notice says the week has not closed. It carries no personal data, so
    it must not put a file full of it into the channel first."""
    from tgtc_core import __main__ as cli
    from tgtc_core.reporting import schedule, slack, store

    report = _week()
    report["detail"].update({"rows": 1, "published_url": None})
    report["window"].setdefault("window_label", "Sep 18-24, 2026")
    args = SimpleNamespace(slack_channel="#gtm-engineering", detail_url="", dry_run_send=False,
                           delivery_weekday="friday", due_hour=6, retry_until_hour=7,
                           timezone="America/Los_Angeles", resend=False,
                           slack_token_env="SLACK_BOT_TOKEN", destination_basis="webhook-declared")
    publications = []
    monkeypatch.setattr(pipeline, "publish_detail",
                        lambda *a, **kw: publications.append(kw.get("channel")) or {"published": True})
    monkeypatch.setattr(cli, "_slack_destination", lambda *a: (lambda *a: {}, "slack_api:C123"))
    # The week has NOT closed, and the retry window is over: this is the notice path.
    monkeypatch.setattr(schedule, "readiness",
                        lambda *a: SimpleNamespace(ready=False, to_dict=lambda: {"blockers": ["a run has no end"]}))
    monkeypatch.setattr(slack, "status_notice_blocks", lambda *a, **kw: [{"notice": True}])
    messages = []
    monkeypatch.setattr(store, "deliver_once", lambda *a, **kw: messages.append(kw["message"]) or {"sent": True})
    monkeypatch.setenv("TGTC_REPORT_DETAIL_DESTINATION", "slack")

    after_retry_window = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)   # 08:00 Pacific
    out = cli._weekly_report_send(None, args, report, None, after_retry_window)

    assert out["sent"] is True, out
    assert publications == [], "a delay notice uploaded the lead CSV"
    assert messages and messages[0]["blocks"] == [{"notice": True}]


def test_a_week_that_is_not_closed_is_never_uploaded_in_slack_mode(conn, monkeypatch):
    """`partial` is a day-by-day view, generated every run. Uploading it would put
    personal data in the channel on a Tuesday."""
    from tgtc_core.reporting import store
    from tests_core.test_weekly_report import FRIDAY_MORNING, WEEK_START, seed_lead

    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    store.ensure_schema(conn)
    partial = pipeline.build(conn, now=FRIDAY_MORNING, kind="partial", compare_previous=False)
    uploaded = []
    monkeypatch.setattr("tgtc_core.reporting.slack_file.publish",
                        lambda *a, **kw: uploaded.append(True) or {"url": "x", "channel_id": "C1"})

    out = pipeline.publish_detail(conn, partial, channel="#gtm-engineering",
                                  env={"TGTC_REPORT_DETAIL_DESTINATION": "slack", "SLACK_BOT_TOKEN": "xoxb-t"})

    assert out == {"published": False, "reason": "only a closed week is published"}
    assert uploaded == [], "a partial week uploaded the lead CSV"
