"""The single HTTP seam every provider client goes through.

``RequestsTransport`` is production. Tests inject a stateful fake implementing the
same protocol, so the REAL client code runs against SIMULATED responses. A timeout
is surfaced as ``TransportTimeout`` and treated by callers as an uncertain outcome:
it proves neither that the provider acted nor that it did not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol


class TransportError(Exception):
    """Network-level failure with no HTTP response."""


class TransportTimeout(TransportError):
    """The request may or may not have reached the provider."""


@dataclass
class Response:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    text: str = ""
    url: str = ""

    def json(self) -> Any:
        try:
            return json.loads(self.text) if self.text else None
        except ValueError:
            return None

    def header(self, name: str) -> Optional[str]:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: Optional[Mapping[str, str]] = None,
                params: Optional[Any] = None, json_body: Optional[Any] = None,
                timeout: float = 30.0) -> Response: ...


class RequestsTransport:
    """Production transport. Never logs headers (they carry credentials)."""

    def request(self, method: str, url: str, *, headers: Optional[Mapping[str, str]] = None,
                params: Optional[Any] = None, json_body: Optional[Any] = None,
                timeout: float = 30.0) -> Response:
        import requests

        try:
            resp = requests.request(method, url, headers=dict(headers or {}), params=params,
                                    json=json_body, timeout=timeout)
        except requests.Timeout as exc:
            raise TransportTimeout(type(exc).__name__) from None
        except requests.RequestException as exc:
            raise TransportError(type(exc).__name__) from None
        return Response(status=resp.status_code, headers=dict(resp.headers), text=resp.text or "", url=str(resp.url))
