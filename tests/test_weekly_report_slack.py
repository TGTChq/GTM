"""Tests for Slack delivery of the weekly report.

The contract is narrow and the failure modes matter more than the happy path:

* the webhook is a secret and must never reach a log, an artifact or a receipt;
* delivery must never raise, whatever Slack does;
* the receipt is written only after Slack accepts, so a crash before it retries
  rather than silently swallowing the week's report;
* a Slack failure must not change the reporter's exit status, because the
  reporter is chained ahead of the production acquisition run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import run_weekly_report
from test_weekly_report import write_run  # shared fixture helper
from weekly_report import slack

UTC = timezone.utc
WEBHOOK = "https://hooks.slack.com/services/T00000000/B11111111/ZZZZsupersecretZZZZ"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text


def recording_poster(responses: List[Any]):
    """A poster that returns/raises the queued items and records every call."""
    calls: List[Dict[str, Any]] = []
    queue = list(responses)

    def poster(url: str, payload: Dict[str, str], timeout: float) -> Any:
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        item = queue.pop(0) if queue else responses[-1]
        if isinstance(item, Exception):
            raise item
        return item

    poster.calls = calls  # type: ignore[attr-defined]
    return poster


@pytest.fixture()
def report_files(tmp_path: Path):
    """A weekly report already written to disk, as the reporter leaves it."""
    out = tmp_path / "weekly_reports"
    out.mkdir(parents=True)
    json_path = out / "weekly_report_2026-W36.json"
    summary_path = out / "weekly_report_2026-W36.txt"
    json_path.write_text(json.dumps({"schema": "tgtc-weekly-report/1"}), encoding="utf-8")
    summary_path.write_text("TGTC WEEKLY PIPELINE REPORT\nJobs captured 100\n", encoding="utf-8")
    return json_path, summary_path


def _no_sleep(_seconds: float) -> None:
    return None


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_a_successful_post_sends_the_summary_and_writes_a_receipt(report_files):
    json_path, summary_path = report_files
    poster = recording_poster([FakeResponse(200, "ok")])

    result = slack.deliver(
        summary_path.read_text(encoding="utf-8"),
        report_id="2026-W36",
        report_path=json_path,
        window={
            "reporting_window_start": "2026-08-28T07:00:00Z",
            "reporting_window_end": "2026-09-04T07:00:00Z",
        },
        webhook=WEBHOOK,
        poster=poster,
        sleeper=_no_sleep,
        now=datetime(2026, 9, 4, 13, 0, 5, tzinfo=UTC),
    )

    assert result.status == slack.STATUS_SENT
    assert result.delivered and result.http_status == 200 and result.attempts == 1
    call = poster.calls[0]
    assert call["url"] == WEBHOOK
    assert call["payload"] == {"text": summary_path.read_text(encoding="utf-8")}
    assert list(call["payload"]) == ["text"], "the payload shape stays {'text': ...}"

    receipt = slack.receipt_path_for(json_path)
    assert receipt.exists() and receipt.name == "weekly_report_2026-W36.slack_sent.json"
    body = json.loads(receipt.read_text(encoding="utf-8"))
    assert body["report_id"] == "2026-W36"
    assert body["http_status"] == 200
    assert body["sent_at"] == "2026-09-04T13:00:05Z"
    assert body["delivery_status"] == "sent"
    assert body["reporting_window_start"] == "2026-08-28T07:00:00Z"


def test_slack_receives_the_human_summary_not_the_json(report_files):
    json_path, summary_path = report_files
    poster = recording_poster([FakeResponse(200)])
    slack.deliver(
        summary_path.read_text(encoding="utf-8"),
        report_id="2026-W36",
        report_path=json_path,
        webhook=WEBHOOK,
        poster=poster,
        sleeper=_no_sleep,
    )
    sent = poster.calls[0]["payload"]["text"]
    assert "TGTC WEEKLY PIPELINE REPORT" in sent
    assert "tgtc-weekly-report/1" not in sent, "the machine document must not be sent"


def test_an_over_long_summary_is_truncated_rather_than_rejected():
    payload = slack.build_payload("x" * (slack.MAX_MESSAGE_CHARS + 5000))
    assert len(payload["text"]) <= slack.MAX_MESSAGE_CHARS + 20
    assert payload["text"].endswith("... (truncated)")


# --------------------------------------------------------------------------
# failure isolation
# --------------------------------------------------------------------------


def test_a_missing_webhook_is_a_declared_gap_not_a_crash(report_files):
    json_path, _ = report_files
    result = slack.deliver(
        "summary", report_id="2026-W36", report_path=json_path, webhook="", env={}
    )
    assert result.status == slack.STATUS_NOT_CONFIGURED
    assert not result.attempted and not result.delivered
    assert slack.ENV_WEBHOOK in result.describe()
    assert not slack.receipt_path_for(json_path).exists()


@pytest.mark.parametrize("status", [400, 403, 404, 410])
def test_a_client_error_fails_without_retrying(report_files, status):
    json_path, _ = report_files
    poster = recording_poster([FakeResponse(status, "invalid_token")])
    result = slack.deliver(
        "summary",
        report_id="2026-W36",
        report_path=json_path,
        webhook=WEBHOOK,
        poster=poster,
        sleeper=_no_sleep,
    )
    assert result.status == slack.STATUS_FAILED
    assert result.http_status == status
    assert result.attempts == 1, "a revoked or wrong webhook will not fix itself"
    assert not slack.receipt_path_for(json_path).exists()


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_server_error_retries_then_gives_up(report_files, status):
    json_path, _ = report_files
    poster = recording_poster([FakeResponse(status, "server error")] * 5)
    result = slack.deliver(
        "summary",
        report_id="2026-W36",
        report_path=json_path,
        webhook=WEBHOOK,
        poster=poster,
        sleeper=_no_sleep,
        max_attempts=3,
    )
    assert result.status == slack.STATUS_FAILED
    assert result.attempts == 3 and len(poster.calls) == 3
    assert not slack.receipt_path_for(json_path).exists()


def test_a_transient_server_error_that_then_succeeds_is_delivered(report_files):
    json_path, _ = report_files
    poster = recording_poster([FakeResponse(503), FakeResponse(200, "ok")])
    result = slack.deliver(
        "summary",
        report_id="2026-W36",
        report_path=json_path,
        webhook=WEBHOOK,
        poster=poster,
        sleeper=_no_sleep,
    )
    assert result.status == slack.STATUS_SENT and result.attempts == 2
    assert slack.receipt_path_for(json_path).exists()


def test_a_network_error_or_timeout_is_absorbed(report_files):
    json_path, _ = report_files
    poster = recording_poster([TimeoutError("connection timed out"), OSError("dns failure")])
    result = slack.deliver(
        "summary",
        report_id="2026-W36",
        report_path=json_path,
        webhook=WEBHOOK,
        poster=poster,
        sleeper=_no_sleep,
        max_attempts=2,
    )
    assert result.status == slack.STATUS_FAILED
    assert "TimeoutError" in result.error or "OSError" in result.error
    assert not slack.receipt_path_for(json_path).exists()


def test_a_malformed_response_object_does_not_crash(report_files):
    json_path, _ = report_files

    class Weird:
        status_code = "not-a-number"
        text = None

    result = slack.deliver(
        "summary",
        report_id="2026-W36",
        report_path=json_path,
        webhook=WEBHOOK,
        poster=recording_poster([Weird()]),
        sleeper=_no_sleep,
    )
    assert result.status == slack.STATUS_FAILED
    assert not slack.receipt_path_for(json_path).exists()


def test_a_2xx_with_an_unexpected_body_still_counts_as_delivered(report_files):
    json_path, _ = report_files
    result = slack.deliver(
        "summary",
        report_id="2026-W36",
        report_path=json_path,
        webhook=WEBHOOK,
        poster=recording_poster([FakeResponse(200, "something else")]),
        sleeper=_no_sleep,
    )
    assert result.status == slack.STATUS_SENT
    assert "unexpected 2xx body" in result.error
    assert slack.receipt_path_for(json_path).exists()


def test_the_time_budget_is_small_and_separate_from_the_reporter_budget():
    """Slack must not be able to spend the 480s reporting window."""
    worst_case = slack.DEFAULT_MAX_ATTEMPTS * slack.DEFAULT_TIMEOUT_SECONDS + sum(
        slack.DEFAULT_BACKOFF_SECONDS * n for n in range(1, slack.DEFAULT_MAX_ATTEMPTS)
    )
    assert worst_case <= 60, f"worst case {worst_case}s is too large a share of the budget"


# --------------------------------------------------------------------------
# the webhook is a secret
# --------------------------------------------------------------------------


def test_the_webhook_never_appears_in_an_error_or_a_receipt(report_files, capsys):
    json_path, _ = report_files
    # requests puts the request URL into its exception text; simulate exactly that.
    poster = recording_poster(
        [RuntimeError(f"HTTPSConnectionPool: Max retries exceeded with url: {WEBHOOK}")]
    )
    result = slack.deliver(
        "summary",
        report_id="2026-W36",
        report_path=json_path,
        webhook=WEBHOOK,
        poster=poster,
        sleeper=_no_sleep,
        max_attempts=1,
    )
    assert result.status == slack.STATUS_FAILED
    assert WEBHOOK not in result.error
    assert "hooks.slack.com" not in result.error
    assert "<redacted>" in result.error

    printed = result.describe()
    assert WEBHOOK not in printed and "ZZZZsupersecret" not in printed
    assert WEBHOOK not in json.dumps(result.to_dict())


def test_the_webhook_never_appears_in_a_success_receipt(report_files):
    json_path, _ = report_files
    slack.deliver(
        "summary",
        report_id="2026-W36",
        report_path=json_path,
        webhook=WEBHOOK,
        poster=recording_poster([FakeResponse(200)]),
        sleeper=_no_sleep,
    )
    raw = slack.receipt_path_for(json_path).read_text(encoding="utf-8")
    assert WEBHOOK not in raw
    assert "hooks.slack.com" not in raw
    assert "ZZZZsupersecret" not in raw


def test_redact_scrubs_any_slack_webhook_not_only_the_configured_one():
    other = "https://hooks.slack.com/services/T9/B9/othersecret"
    assert "othersecret" not in slack.redact(f"failed calling {other}")
    assert WEBHOOK not in slack.redact(f"failed calling {WEBHOOK}", WEBHOOK)


def test_the_webhook_is_read_from_the_environment_and_not_echoed(capsys):
    assert slack.webhook_from_env({slack.ENV_WEBHOOK: "  " + WEBHOOK + "  "}) == WEBHOOK
    assert slack.webhook_from_env({}) == ""
    assert WEBHOOK not in capsys.readouterr().out


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


def test_an_existing_receipt_suppresses_a_second_send(report_files):
    json_path, _ = report_files
    poster = recording_poster([FakeResponse(200)])
    first = slack.deliver(
        "summary", report_id="2026-W36", report_path=json_path,
        webhook=WEBHOOK, poster=poster, sleeper=_no_sleep,
    )
    second = slack.deliver(
        "summary", report_id="2026-W36", report_path=json_path,
        webhook=WEBHOOK, poster=poster, sleeper=_no_sleep,
    )
    assert first.status == slack.STATUS_SENT
    assert second.status == slack.STATUS_ALREADY_SENT
    assert second.attempted is False
    assert len(poster.calls) == 1, "exactly one POST for one window"


def test_a_receipt_is_never_written_before_success(report_files):
    """The receipt is the record of delivery, so it must not exist on failure."""
    json_path, _ = report_files
    receipt = slack.receipt_path_for(json_path)

    def poster(url, payload, timeout):
        assert not receipt.exists(), "the receipt must not exist while the POST is in flight"
        return FakeResponse(500)

    result = slack.deliver(
        "summary", report_id="2026-W36", report_path=json_path,
        webhook=WEBHOOK, poster=poster, sleeper=_no_sleep, max_attempts=2,
    )
    assert result.status == slack.STATUS_FAILED
    assert not receipt.exists()


def test_a_delivered_message_whose_receipt_cannot_be_written_is_reported(report_files, monkeypatch):
    json_path, _ = report_files

    def boom(_path, _payload):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(slack, "_write_receipt_atomically", boom)
    result = slack.deliver(
        "summary", report_id="2026-W36", report_path=json_path,
        webhook=WEBHOOK, poster=recording_poster([FakeResponse(200)]), sleeper=_no_sleep,
    )
    assert result.status == slack.STATUS_SENT
    assert "will send again" in result.error, "an unrecorded send must be declared"


def test_the_receipt_is_written_atomically(report_files):
    json_path, _ = report_files
    slack.deliver(
        "summary", report_id="2026-W36", report_path=json_path,
        webhook=WEBHOOK, poster=recording_poster([FakeResponse(200)]), sleeper=_no_sleep,
    )
    directory = json_path.parent
    assert list(directory.glob("*.tmp")) == [], "no temp file may survive a completed write"
    json.loads(slack.receipt_path_for(json_path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# CLI integration
# --------------------------------------------------------------------------


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    """A run directory plus a captured Slack poster wired into the CLI."""
    root = tmp_path / "orchestrator_v2"
    write_run(root, "a", finished="2026-08-31T13:00:00Z")
    out = tmp_path / "weekly_reports"
    calls: List[Dict[str, Any]] = []
    outcome: Dict[str, Any] = {"response": FakeResponse(200)}

    def poster(url, payload, timeout):
        calls.append({"url": url, "payload": payload})
        item = outcome["response"]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setenv(slack.ENV_WEBHOOK, WEBHOOK)
    monkeypatch.setattr(slack, "_default_poster", poster)
    monkeypatch.setattr(slack, "DEFAULT_BACKOFF_SECONDS", 0)
    return {"root": root, "out": out, "calls": calls, "outcome": outcome}


def _argv(cli_env, *extra: str) -> List[str]:
    return [
        "--artifact-root", str(cli_env["root"]),
        "--start", "2026-08-28", "--end", "2026-09-04",
        "--out-dir", str(cli_env["out"]),
        *extra,
    ]


def test_cli_slack_flag_delivers_after_the_files_are_written(cli_env, capsys):
    code = run_weekly_report.main(_argv(cli_env, "--slack", "--quiet"))
    out = capsys.readouterr().out
    assert code == 0
    assert len(cli_env["calls"]) == 1
    json_path = cli_env["out"] / "weekly_report_2026-W36.json"
    assert json_path.exists()
    assert slack.receipt_path_for(json_path).exists()
    assert WEBHOOK not in out


def test_cli_without_the_flag_never_contacts_slack(cli_env):
    assert run_weekly_report.main(_argv(cli_env, "--quiet")) == 0
    assert cli_env["calls"] == []
    assert not slack.receipt_path_for(cli_env["out"] / "weekly_report_2026-W36.json").exists()


def test_cli_slack_failure_keeps_the_artifacts_and_the_exit_code(cli_env, capsys):
    """The reporter is chained ahead of acquisition; Slack must not change its exit."""
    cli_env["outcome"]["response"] = FakeResponse(500, "oh no")
    code = run_weekly_report.main(_argv(cli_env, "--slack", "--quiet"))
    out = capsys.readouterr().out

    assert code == 0, "a Slack failure must not change the reporter's exit status"
    json_path = cli_env["out"] / "weekly_report_2026-W36.json"
    assert json_path.exists() and (cli_env["out"] / "weekly_report_2026-W36.txt").exists()
    assert not slack.receipt_path_for(json_path).exists()
    assert "slack: NOT delivered" in out
    assert WEBHOOK not in out


def test_cli_slack_network_failure_also_preserves_exit_semantics(cli_env, capsys):
    cli_env["outcome"]["response"] = TimeoutError(f"timed out talking to {WEBHOOK}")
    code = run_weekly_report.main(_argv(cli_env, "--slack", "--quiet"))
    out = capsys.readouterr().out
    assert code == 0
    assert "slack: NOT delivered" in out
    assert WEBHOOK not in out and "hooks.slack.com" not in out


def test_cli_missing_webhook_reports_the_gap_without_crashing(cli_env, capsys, monkeypatch):
    monkeypatch.delenv(slack.ENV_WEBHOOK, raising=False)
    code = run_weekly_report.main(_argv(cli_env, "--slack", "--quiet"))
    out = capsys.readouterr().out
    assert code == 0
    assert cli_env["calls"] == []
    assert slack.ENV_WEBHOOK in out
    assert (cli_env["out"] / "weekly_report_2026-W36.json").exists()


def test_cli_second_invocation_does_not_duplicate_send(cli_env):
    argv = _argv(cli_env, "--slack", "--once-per-window", "--quiet")
    assert run_weekly_report.main(argv) == 0
    assert run_weekly_report.main(argv) == 0
    assert len(cli_env["calls"]) == 1, "one window, one Slack message"


def test_cli_retries_slack_when_the_report_exists_but_the_receipt_does_not(cli_env, capsys):
    """The crash-between-write-and-send case: deliver without recomputing anything."""
    cli_env["outcome"]["response"] = FakeResponse(500)
    argv = _argv(cli_env, "--slack", "--once-per-window", "--quiet")
    assert run_weekly_report.main(argv) == 0
    json_path = cli_env["out"] / "weekly_report_2026-W36.json"
    assert json_path.exists()
    assert not slack.receipt_path_for(json_path).exists()
    first_written = json_path.read_text(encoding="utf-8")
    failed_attempts = len(cli_env["calls"])
    assert failed_attempts >= 1

    # Second run: Slack is healthy now. The report must NOT be rebuilt.
    cli_env["outcome"]["response"] = FakeResponse(200)

    def explode(*args, **kwargs):
        raise AssertionError("a delivery retry must not re-read Instantly")

    original = run_weekly_report.collect_instantly
    run_weekly_report.collect_instantly = explode
    try:
        assert run_weekly_report.main(argv + ["--instantly"]) == 0
    finally:
        run_weekly_report.collect_instantly = original

    assert slack.receipt_path_for(json_path).exists()
    assert json_path.read_text(encoding="utf-8") == first_written, "the report was not regenerated"
    assert len(cli_env["calls"]) == failed_attempts + 1, "exactly one further POST, which succeeded"
    assert cli_env["calls"][-1]["payload"]["text"] == (
        cli_env["out"] / "weekly_report_2026-W36.txt"
    ).read_text(encoding="utf-8"), "it delivers the summary already on disk"


def test_cli_retry_path_is_a_no_op_once_the_receipt_exists(cli_env):
    argv = _argv(cli_env, "--slack", "--once-per-window", "--quiet")
    assert run_weekly_report.main(argv) == 0
    assert run_weekly_report.main(argv) == 0
    assert run_weekly_report.main(argv) == 0
    assert len(cli_env["calls"]) == 1


def test_cli_no_write_does_not_attempt_delivery(cli_env, capsys):
    code = run_weekly_report.main(_argv(cli_env, "--slack", "--no-write", "--quiet"))
    assert code == 0
    assert cli_env["calls"] == []
    assert "slack: skipped" in capsys.readouterr().out


def test_cli_strict_exit_code_is_unaffected_by_a_healthy_slack(cli_env):
    """--strict still reflects metric coverage only, never delivery."""
    code = run_weekly_report.main(_argv(cli_env, "--slack", "--quiet", "--strict"))
    assert code == 2, "sent_to_instantly is unavailable here, so strict still fails"
    assert len(cli_env["calls"]) == 1
