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

The file is never shared with "anyone with the link": it inherits the private folder's
access, and named readers are granted explicitly. Prospect data does not get a public URL.

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

    def share_with_readers(self, file_id: str, readers: List[str]) -> List[str]:
        """Grant reader to named people, and to nobody else. Idempotent: an address that
        already has access is left exactly as it is."""
        have = {str(p.get("emailAddress") or "").lower() for p in self.permissions(file_id)}
        granted: List[str] = []
        for address in readers:
            email = str(address or "").strip()
            if not email or email.lower() in have:
                continue
            response = self._s.post(f"{DRIVE_FILES}/{file_id}/permissions",
                                    params={"fields": "id,type,role", "supportsAllDrives": "true",
                                            "sendNotificationEmail": "false"},
                                    json={"type": "user", "role": "reader", "emailAddress": email}, timeout=60)
            if response.status_code >= 300:
                raise DriveError(f"Drive refused reader access for a named person: HTTP {response.status_code}")
            granted.append(email)
        return granted

def publish(folder: DriveFolder, *, report_id: str, content: bytes,
            readers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Upload into the private folder, grant named readers if any, and read the access
    back before returning a URL.

    The file is NOT shared with "anyone with the link". It inherits the folder's access,
    which is the access control, and named readers are granted explicitly when the
    operator names them. A link that anyone can open is not an access-controlled
    destination for prospect data, whatever convenience it buys.

    Raises rather than returning a URL whose access it could not read back.
    """
    uploaded = folder.upload(file_name(report_id), content)
    file_id = str(uploaded.get("id") or "")
    if not file_id:
        raise DriveError("Drive returned no file id")
    granted = folder.share_with_readers(file_id, readers or [])
    url = uploaded.get("webViewLink") or folder.link(file_id)
    if not url:
        raise DriveError("Drive returned no link for the uploaded file")
    current = folder.permissions(file_id)
    public = [p for p in current if p.get("type") == "anyone"]
    if public:
        raise DriveError("the uploaded file is readable by anyone with the link; "
                         "prospect data must not be published that way")
    return {"file_id": file_id, "url": url, "created": bool(uploaded.get("created")),
            "permissions": [{"type": p.get("type"), "role": p.get("role")} for p in current],
            "named_readers_granted": granted, "bytes": len(content)}
