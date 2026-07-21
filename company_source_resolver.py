"""Bounded first-party company-content resolver."""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

import config
from source_cache import JsonTtlCache


class _CompanyTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "svg", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "svg", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


@dataclass
class CompanyPage:
    url: str
    status: str
    http_status: Optional[int] = None
    text: str = ""
    error: str = ""


@dataclass
class CompanySource:
    state: str
    domain: str
    text: str = ""
    pages: List[CompanyPage] = field(default_factory=list)
    retryable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _html_text(body: str) -> str:
    parser = _CompanyTextParser()
    try:
        parser.feed(html.unescape(body))
        return parser.text()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()


class CompanySourceResolver:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.cache = JsonTtlCache(
            config.ORGANIZATION_CACHE_DIR, config.COMPANY_SOURCE_CACHE_TTL_HOURS
        )

    def _fetch(self, url: str) -> Dict[str, Any]:
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        payload: Dict[str, Any] = {
            "status_code": None, "url": url, "text": "", "error": "not_attempted"
        }
        transient_codes = {403, 408, 425, 429, 500, 502, 503, 504}
        for _attempt in range(max(1, min(config.MAX_HTTP_RETRIES, 2))):
            try:
                response = self.session.get(
                    url,
                    timeout=config.COMPANY_SOURCE_TIMEOUT_SECONDS,
                    allow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; TGTCCompanyVerifier/1.0)",
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
                payload = {
                    "status_code": response.status_code,
                    "url": response.url,
                    "text": _html_text(response.text[:2_000_000])[:120_000],
                }
            except requests.RequestException as exc:
                payload = {"status_code": None, "url": url, "text": "", "error": str(exc)}
            if payload.get("status_code") not in transient_codes and payload.get("status_code") is not None:
                break
        if payload.get("status_code") not in transient_codes and payload.get("status_code") is not None:
            self.cache.set(url, payload)
        return payload

    def resolve(self, domain: str, *, fetch: Optional[bool] = None) -> CompanySource:
        domain = str(domain or "").strip().lower()
        if not domain:
            return CompanySource("UNRESOLVED", "")
        fetch = config.COMPANY_SOURCE_FETCH_ENABLED if fetch is None else fetch
        if not fetch:
            return CompanySource("FETCH_DISABLED", domain)
        base = f"https://{domain}/"
        candidates = [base, urljoin(base, "about"), urljoin(base, "services")]
        candidates = candidates[: max(1, config.COMPANY_SOURCE_MAX_PAGES)]
        pages: List[CompanyPage] = []
        texts: List[str] = []
        transient = False
        for url in candidates:
            payload = self._fetch(url)
            status = payload.get("status_code")
            if status in {403, 408, 425, 429, 500, 502, 503, 504} or status is None:
                transient = True
                pages.append(CompanyPage(url, "temporary_failure", status, error=payload.get("error") or ""))
                continue
            if status and 200 <= status < 400 and payload.get("text"):
                text = str(payload["text"])
                pages.append(CompanyPage(payload.get("url") or url, "resolved", status, text[:3000]))
                texts.append(text)
            else:
                pages.append(CompanyPage(url, "not_found", status))
        combined = re.sub(r"\s+", " ", " ".join(texts)).strip()
        if combined:
            return CompanySource("RESOLVED", domain, combined, pages, False)
        return CompanySource("TEMPORARILY_UNAVAILABLE" if transient else "UNRESOLVED", domain, "", pages, transient)
