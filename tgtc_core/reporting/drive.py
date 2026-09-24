"""Publishing the week's lead file to the TGTC Drive folder, once per week.

Three properties, because a link in a report is a promise:

1. **One file per week.** The name carries the ``report_id``; a retry finds that file and
   replaces its content, so the link in Slack keeps pointing at the same place and a
   second copy can never appear.
2. **The permission is read back, never assumed.** After sharing, the permission list is
   fetched and checked for ``{"type": "anyone", "role": "reader"}``. If it is not there,
   nothing is recorded as published.
3. **The link is opened without credentials** before it is published. An unauthenticated
   GET that lands on a sign-in page is a link nobody outside the account can use, and
   that is a failure, not a detail.

Authentication is a Google **service account** (``TGTC_DRIVE_SERVICE_ACCOUNT_JSON``,
raw or base64), because the job runs in a container with no browser and no user session.
A connector authorised in a chat client is not a credential this process can use.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, List, Optional, Tuple

DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPES = ("https://www.googleapis.com/auth/drive",)

#: What a published weekly file must be shared as, and what is read back to prove it.
ANYONE_READER = {"type": "anyone", "role": "reader"}


class DriveError(RuntimeError):
    """Drive refused, or the state it reported back is not the state we require."""


def file_name(report_id: str) -> str:
    return f"TGTC_weekly_leads_{report_id}.csv"


def credentials_from_env(env: Optional[Dict[str, str]] = None):
    """Build service-account credentials, or return None when none are configured.

    Returning None is a first-class answer: the caller then leaves the detail pending
    instead of inventing a link.
    """
    env = os.environ if env is None else env
    raw = (env.get("TGTC_DRIVE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    if not raw.lstrip().startswith("{"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise DriveError(f"TGTC_DRIVE_SERVICE_ACCOUNT_JSON is neither JSON nor base64: {exc}") from None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DriveError(f"TGTC_DRIVE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from None
    try:
        from google.oauth2 import service_account  # imported here: only a publish needs it
    except ImportError as exc:  # pragma: no cover - the dependency is declared
        raise DriveError(f"google-auth is not installed in this image: {exc}") from None
    return service_account.Credentials.from_service_account_info(info, scopes=list(SCOPES))


def authorised_session(credentials):
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(credentials)


class DriveFolder:
    """The one folder the weekly files live in. ``session`` is any object with
    ``get``/``post``/``patch`` returning a requests-like response, so the whole flow is
    testable without a network."""

    def __init__(self, session, folder_id: str, *, opener=None):
        if not folder_id:
            raise DriveError("no Drive folder id configured")
        self._s = session
        self._folder = folder_id
        self._open = opener

    # --- reads ------------------------------------------------------------------
    def find(self, name: str) -> Optional[Dict[str, Any]]:
        query = f"name = '{name}' and '{self._folder}' in parents and trashed = false"
        response = self._s.get(DRIVE_FILES, params={"q": query, "fields": "files(id,name,webViewLink,size)",
                                                    "supportsAllDrives": "true"}, timeout=60)
        if response.status_code >= 300:
            raise DriveError(f"Drive refused the lookup: HTTP {response.status_code}")
        files = (response.json() or {}).get("files") or []
        return files[0] if files else None

    def permissions(self, file_id: str) -> List[Dict[str, Any]]:
        response = self._s.get(f"{DRIVE_FILES}/{file_id}/permissions",
                               params={"fields": "permissions(id,type,role)", "supportsAllDrives": "true"}, timeout=60)
        if response.status_code >= 300:
            raise DriveError(f"Drive refused the permission list: HTTP {response.status_code}")
        return (response.json() or {}).get("permissions") or []

    def link(self, file_id: str) -> str:
        response = self._s.get(f"{DRIVE_FILES}/{file_id}", params={"fields": "webViewLink",
                                                                   "supportsAllDrives": "true"}, timeout=60)
        if response.status_code >= 300:
            raise DriveError(f"Drive refused the file: HTTP {response.status_code}")
        return str((response.json() or {}).get("webViewLink") or "")

    # --- writes -----------------------------------------------------------------
    def upload(self, name: str, content: bytes, *, mime: str = "text/csv") -> Dict[str, Any]:
        """Create the week's file, or replace the content of the one already there."""
        existing = self.find(name)
        if existing:
            response = self._s.patch(f"{DRIVE_UPLOAD}/{existing['id']}",
                                     params={"uploadType": "media", "fields": "id,name,webViewLink",
                                             "supportsAllDrives": "true"},
                                     data=content, headers={"Content-Type": mime}, timeout=180)
            if response.status_code >= 300:
                raise DriveError(f"Drive refused the update: HTTP {response.status_code}")
            return {**(response.json() or {}), "created": False}
        metadata = {"name": name, "parents": [self._folder], "mimeType": mime}
        boundary = "tgtc-weekly-detail-boundary"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() \
            + content + f"\r\n--{boundary}--".encode()
        response = self._s.post(DRIVE_UPLOAD, params={"uploadType": "multipart", "fields": "id,name,webViewLink",
                                                      "supportsAllDrives": "true"},
                                data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                                timeout=180)
        if response.status_code >= 300:
            raise DriveError(f"Drive refused the upload: HTTP {response.status_code}")
        return {**(response.json() or {}), "created": True}

    def share_anyone_reader(self, file_id: str) -> List[Dict[str, Any]]:
        """Make the file readable by anyone with the link -- and prove it afterwards.

        Idempotent: an existing ``anyone`` permission is left exactly as it is rather
        than re-created, so a retry never changes what is already granted.
        """
        current = self.permissions(file_id)
        if not any(p.get("type") == "anyone" and p.get("role") == "reader" for p in current):
            response = self._s.post(f"{DRIVE_FILES}/{file_id}/permissions",
                                    params={"fields": "id,type,role", "supportsAllDrives": "true"},
                                    json=dict(ANYONE_READER), timeout=60)
            if response.status_code >= 300:
                raise DriveError(f"Drive refused the sharing change: HTTP {response.status_code}. "
                                 "If the workspace forbids link sharing this is an administrator setting, "
                                 "not something the job can grant itself")
            current = self.permissions(file_id)
        if not any(p.get("type") == "anyone" and p.get("role") == "reader" for p in current):
            raise DriveError("the file is not readable by anyone with the link after sharing; "
                             "nothing was recorded as published")
        return current

    def opens_without_credentials(self, url: str) -> Tuple[bool, str]:
        """Fetch the link with no authentication at all. A sign-in page is a failure."""
        if self._open is None:
            import requests

            opener = requests.Session().get
        else:
            opener = self._open
        try:
            response = opener(url, timeout=60)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        status = getattr(response, "status_code", 0)
        body = (getattr(response, "text", "") or "")[:4000].lower()
        final = str(getattr(response, "url", url))
        if status >= 400:
            return False, f"HTTP {status}"
        if "accounts.google.com" in final or "sign in" in body and "signin" in final:
            return False, f"redirected to a sign-in page ({final[:80]})"
        if "accounts.google.com/v3/signin" in body or "you need access" in body:
            return False, "Drive answered 'you need access'"
        return True, f"HTTP {status}"


def publish(folder: DriveFolder, *, report_id: str, content: bytes) -> Dict[str, Any]:
    """Upload, share, read the permission back, and open the link unauthenticated.

    Returns everything the caller needs to record -- and raises rather than returning a
    URL it could not prove is readable.
    """
    uploaded = folder.upload(file_name(report_id), content)
    file_id = str(uploaded.get("id") or "")
    if not file_id:
        raise DriveError("Drive returned no file id")
    permissions = folder.share_anyone_reader(file_id)
    url = uploaded.get("webViewLink") or folder.link(file_id)
    opened, detail = folder.opens_without_credentials(url)
    if not opened:
        raise DriveError(f"the published link does not open without credentials: {detail}")
    return {"file_id": file_id, "url": url, "created": bool(uploaded.get("created")),
            "permissions": [{"type": p.get("type"), "role": p.get("role")} for p in permissions],
            "link_opens_unauthenticated": detail, "bytes": len(content)}
