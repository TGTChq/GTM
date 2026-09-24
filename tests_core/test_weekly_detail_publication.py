"""Publishing the week's lead file: once, into a private folder, proved, never faked.

A link in a report is a promise that a file exists and the right people can read it.
These tests hold each half of that promise: the upload happens once per week, the access
is read back rather than assumed, and every refusal is a named state instead of a URL.

The file carries prospect data, so it is NOT shared with anyone who has the link. It
inherits the private folder's access, named readers are granted explicitly, and a file
that turns out to be world-readable is refused rather than recorded.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tgtc_core.reporting import detail, drive, pipeline, slack, store
from tests_core.test_weekly_report import FRIDAY_MORNING, WEEK_START, seed_lead


class FakeResponse:
    def __init__(self, payload=None, status=200, text="", url=""):
        self._payload, self.status_code, self.text, self.url = payload or {}, status, text, url

    def json(self):
        return self._payload


class FakeDrive:
    """The Drive API, remembering only what these tests need to judge."""

    def __init__(self, *, share_status=200, files=None):
        self.files = files or {}
        self.permissions = {}
        self.uploads = []
        self.shares = []
        self.share_status = share_status

    # requests-like surface
    def get(self, url, params=None, timeout=None):
        if url.endswith("/permissions"):
            file_id = url.rsplit("/", 2)[-2]
            return FakeResponse({"permissions": self.permissions.get(file_id, [])})
        if "files/" in url:
            file_id = url.rsplit("/", 1)[-1]
            return FakeResponse({"webViewLink": self.files[file_id]["webViewLink"]})
        name = (params or {}).get("q", "")
        found = [f for f in self.files.values() if f"name = '{f['name']}'" in name]
        return FakeResponse({"files": found})

    def post(self, url, params=None, data=None, json=None, headers=None, timeout=None):
        if url.endswith("/permissions"):
            file_id = url.rsplit("/", 2)[-2]
            if self.share_status >= 300:
                return FakeResponse(status=self.share_status)
            self.shares.append((file_id, json))
            self.permissions.setdefault(file_id, []).append({"id": "p1", **json})
            return FakeResponse({"id": "p1", **json})
        file_id = f"file-{len(self.files) + 1}"
        self.uploads.append(("create", file_id, data))
        self.files[file_id] = {"id": file_id, "name": "TGTC_weekly_leads_weekly-2026-09-11.csv",
                               "webViewLink": f"https://drive.example/{file_id}/view"}
        return FakeResponse(self.files[file_id])

    def patch(self, url, params=None, data=None, headers=None, timeout=None):
        file_id = url.rsplit("/", 1)[-1]
        self.uploads.append(("update", file_id, data))
        return FakeResponse(self.files[file_id])


def opener_ok(url, timeout=None):
    return FakeResponse(status=200, text="lead_key,campaign,email", url=url)


def folder(session, **kw):
    return drive.DriveFolder(session, "folder-1", opener=kw.pop("opener", opener_ok))


# --------------------------------------------------------------------------------
# the mechanics
# --------------------------------------------------------------------------------

def test_a_week_is_uploaded_once_and_replaced_on_a_retry():
    api = FakeDrive()
    first = drive.publish(folder(api), report_id="weekly-2026-09-11", content=b"a,b\n1,2\n")
    second = drive.publish(folder(api), report_id="weekly-2026-09-11", content=b"a,b\n1,3\n")
    assert first["created"] is True and second["created"] is False
    assert first["file_id"] == second["file_id"]          # the same file, the same link
    assert [kind for kind, _, _ in api.uploads] == ["create", "update"]
    assert len(api.files) == 1


def test_nothing_is_shared_with_anyone_who_has_the_link():
    """The folder is the access control. A public link to prospect data is not."""
    api = FakeDrive()
    out = drive.publish(folder(api), report_id="weekly-2026-09-11", content=b"x")
    assert api.shares == [], "no permission was granted at all, so none can be public"
    assert all(p.get("type") != "anyone" for p in out["permissions"])
    assert out["named_readers_granted"] == []


def test_named_readers_are_granted_once_and_never_again():
    api = FakeDrive()
    out = drive.publish(folder(api), report_id="weekly-2026-09-11", content=b"x",
                        readers=["brett@example.com", "roman@example.com"])
    assert out["named_readers_granted"] == ["brett@example.com", "roman@example.com"]
    assert [j["role"] for _, j in api.shares] == ["reader", "reader"]
    assert all(j["type"] == "user" for _, j in api.shares)

    drive.publish(folder(api), report_id="weekly-2026-09-11", content=b"x",
                  readers=["brett@example.com", "roman@example.com"])
    assert len(api.shares) == 2, "a retry re-granted access that was already there"


def test_a_file_that_turns_out_to_be_world_readable_is_refused():
    """Whatever put it there -- an inherited setting, a hand-made share -- the report
    does not record a link to prospect data that anyone can open."""
    api = FakeDrive()
    api.permissions["file-1"] = [{"id": "p0", "type": "anyone", "role": "reader"}]
    with pytest.raises(drive.DriveError, match="readable by anyone"):
        drive.publish(folder(api), report_id="weekly-2026-09-11", content=b"x")


def test_a_workspace_that_refuses_a_named_reader_is_reported_as_such():
    api = FakeDrive(share_status=403)
    with pytest.raises(drive.DriveError, match="refused reader access"):
        drive.publish(folder(api), report_id="weekly-2026-09-11", content=b"x", readers=["brett@example.com"])


def test_no_credential_is_a_state_not_a_guess():
    assert drive.credentials_from_env({}) is None
    with pytest.raises(drive.DriveError, match="neither JSON nor base64"):
        drive.credentials_from_env({"TGTC_DRIVE_SERVICE_ACCOUNT_JSON": "not-json-not-base64-!!"})


# --------------------------------------------------------------------------------
# the weekly job
# --------------------------------------------------------------------------------

def _week(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    store.ensure_schema(conn)
    return pipeline.generate_and_store(conn, now=FRIDAY_MORNING, compare_previous=False)["report"]


def test_without_drive_configured_the_detail_stays_pending_and_slack_says_so(conn):
    report = _week(conn)
    out = pipeline.publish_detail(conn, report, env={})
    assert out["published"] is False
    assert out["reason"] == "Drive is not configured on this service"
    assert set(out["missing"]) == {"TGTC_DRIVE_SERVICE_ACCOUNT_JSON", "TGTC_REPORT_DRIVE_FOLDER_ID"}
    assert detail.load(conn, "weekly-2026-09-11")["published_url"] is None
    text = "\n".join(b["text"]["text"] for b in slack.blocks_for(report, detail_rows=1) if b["type"] == "section")
    assert "_pending_" in text and "https://" not in text


def test_a_published_week_is_recorded_with_its_readers_and_never_uploaded_twice(conn, monkeypatch):
    report = _week(conn)
    api = FakeDrive()
    monkeypatch.setattr(drive, "credentials_from_env", lambda env=None: object())
    monkeypatch.setattr(drive, "authorised_session", lambda credentials: api)
    real_folder = drive.DriveFolder          # captured before patching, or this recurses
    monkeypatch.setattr(drive, "DriveFolder",
                        lambda session, folder_id, **kw: real_folder(session, folder_id, opener=opener_ok))
    env = {"TGTC_DRIVE_SERVICE_ACCOUNT_JSON": "{}", "TGTC_REPORT_DRIVE_FOLDER_ID": "folder-1"}

    env = {**env, "TGTC_REPORT_DETAIL_READERS": "brett@example.com, roman@example.com"}
    first = pipeline.publish_detail(conn, report, env=env)
    assert first["published"] is True and first["rows"] == 1
    assert all(p["type"] != "anyone" for p in first["permissions"])

    stored = detail.load(conn, "weekly-2026-09-11")
    assert stored["published_url"] == first["url"]
    assert stored["published_to"] == ["drive-folder:folder-1", "reader:brett@example.com",
                                      "reader:roman@example.com"]
    assert store.get(conn, "weekly-2026-09-11")["detail_url"] == first["url"]

    # The 06:20 retry: nothing is uploaded, nothing is re-shared, the link does not move.
    again = pipeline.publish_detail(conn, report, env=env)
    assert again == {"published": False, "reason": "already_published", "url": first["url"], "rows": 1}
    assert len(api.uploads) == 1 and len(api.shares) == 2

    # ... and the message now carries that link instead of the pending line.
    report["detail"] = {**report["detail"], "published_url": first["url"], "state": "published"}
    text = "\n".join(b["text"]["text"] for b in slack.blocks_for(report, detail_url=first["url"], detail_rows=1)
                     if b["type"] == "section")
    assert f"<{first['url']}|this week's file>" in text and "_pending_" not in text


def test_a_partial_week_is_never_published(conn):
    store.ensure_schema(conn)
    report = pipeline.build(conn, now=FRIDAY_MORNING, kind="partial", compare_previous=False)
    assert pipeline.publish_detail(conn, report, env={})["reason"] == "only a closed week is published"
