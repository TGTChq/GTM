"""Bounded official-source resolver for job postings.

JSearch remains discovery only.  This module follows a small candidate set,
parses first-party/ATS content and returns a canonical source snapshot with
explicit retryability.  It never crawls a whole site and never loops forever.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

import config
from company_identity import company_names_compatible, safe_company_domain
from domain_utils import normalize_company_domain
from job_signal import ATS_DOMAINS, AGGREGATOR_DOMAINS, classify_url_source
from source_cache import JsonTtlCache

logger = logging.getLogger(__name__)


CLOSED_PATTERNS = (
    r"job (?:is )?no longer available",
    r"position (?:has been|is) filled",
    r"this job has expired",
    r"posting (?:has been )?closed",
    r"no longer accepting applications",
    r"job not found",
)
TRANSIENT_STATUS_CODES = {403, 408, 425, 429, 500, 502, 503, 504}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[Tuple[str, str]] = []
        self._href = ""
        self._text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        self._href = next((str(value or "") for key, value in attrs if key.lower() == "href"), "")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href and data.strip():
            self._text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, re.sub(r"\s+", " ", " ".join(self._text)).strip()))
            self._href = ""
            self._text = []


@dataclass
class SourceAttempt:
    url: str
    source_type: str
    status: str
    http_status: Optional[int] = None
    final_url: str = ""
    error: str = ""


@dataclass
class ResolvedJobSource:
    state: str
    source_url: str = ""
    source_type: str = ""
    http_status: Optional[int] = None
    active: Optional[bool] = None
    canonical_title: str = ""
    canonical_employer: str = ""
    description: str = ""
    location_text: str = ""
    employment_type: str = ""
    date_posted: str = ""
    valid_through: str = ""
    job_id: str = ""
    official: bool = False
    temporarily_unavailable: bool = False
    retryable: bool = False
    corroborated: bool = False
    attempts: List[SourceAttempt] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["attempts"] = [asdict(item) for item in self.attempts]
        return payload


def _clean_url(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    if not value.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(value)
    if parsed.netloc.lower().endswith("google.com"):
        query = parse_qs(parsed.query)
        for key in ("q", "url", "u"):
            for candidate in query.get(key, []):
                candidate = unquote(candidate)
                if candidate.startswith(("http://", "https://")):
                    return candidate
    return value


def candidate_urls(job: Dict) -> List[str]:
    values: List[str] = []
    for option in job.get("apply_options") or []:
        if isinstance(option, dict):
            values.append(option.get("apply_link") or "")
    values.extend(
        [
            job.get("job_apply_link") or "",
            job.get("job_google_link") or "",
            job.get("job_url_selected") or "",
        ]
    )
    output: List[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_url(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            output.append(cleaned)
    company_domain = normalize_company_domain(job.get("employer_website") or "")
    output.sort(
        key=lambda url: {
            "company": 0,
            "ats": 1,
            "linkedin": 2,
            "aggregator": 4,
            "other": 3,
        }.get(classify_url_source(url, company_domain), 5)
    )
    return output[: max(1, config.JOB_SOURCE_MAX_CANDIDATES)]


def _strip_html(value: Any) -> str:
    raw = html.unescape(str(value or ""))
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        text = parser.text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).strip()


def _json_ld_objects(body: str) -> Iterable[Dict[str, Any]]:
    scripts = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        body,
        re.I | re.S,
    )
    for script in scripts:
        cleaned = html.unescape(script).strip().replace("<!--", "").replace("-->", "")
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            continue
        queue = data if isinstance(data, list) else [data]
        while queue:
            item = queue.pop(0)
            if isinstance(item, dict):
                graph = item.get("@graph")
                if isinstance(graph, list):
                    queue.extend(graph)
                yield item


def _jobposting(body: str) -> Dict[str, Any]:
    for obj in _json_ld_objects(body):
        kinds = obj.get("@type")
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if any(str(kind).lower() == "jobposting" for kind in kinds):
            return obj
    return {}


def _location_from_json_ld(obj: Dict[str, Any]) -> str:
    parts: List[str] = []
    locations = obj.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    for location in locations:
        address = (location or {}).get("address") or {}
        if isinstance(address, str):
            parts.append(address)
        elif isinstance(address, dict):
            parts.append(", ".join(
                str(address.get(key) or "").strip()
                for key in ("addressLocality", "addressRegion", "addressCountry")
                if address.get(key)
            ))
    if obj.get("jobLocationType"):
        parts.insert(0, str(obj.get("jobLocationType")))
    return " | ".join(value for value in parts if value)


def _date_in_past(value: str) -> bool:
    if not value:
        return False
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < datetime.now(timezone.utc)


def _official_for(url: str, source_type: str, company_domain: str) -> bool:
    host = normalize_company_domain(url)
    if source_type == "company":
        return True
    if source_type == "ats":
        return True
    return bool(company_domain and host == company_domain)


def _employer_from_json_ld(obj: Dict[str, Any]) -> str:
    org = obj.get("hiringOrganization") or {}
    return str(org.get("name") or "") if isinstance(org, dict) else ""


def _title_match_score(title: str, text: str) -> float:
    left = set(_title_tokens(title))
    right = set(_title_tokens(text))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


def _job_page_has_title(discovery_title: str, page_text: str) -> bool:
    return _title_match_score(discovery_title, page_text[:40_000]) >= 0.8


def _extract_links(body: str, base_url: str) -> List[Tuple[str, str]]:
    parser = _LinkExtractor()
    try:
        parser.feed(body)
    except Exception:
        return []
    output: List[Tuple[str, str]] = []
    for href, text in parser.links:
        url = _clean_url(urljoin(base_url, href))
        if url:
            output.append((url, text))
    return output


class JobSourceResolver:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        if hasattr(self.session, "max_redirects"):
            self.session.max_redirects = max(1, config.JOB_SOURCE_MAX_REDIRECTS)
        self.cache = JsonTtlCache(config.SOURCE_CACHE_DIR, config.JOB_SOURCE_CACHE_TTL_HOURS)

    def _fetch(self, url: str) -> Dict[str, Any]:
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        result: Dict[str, Any] = {
            "status_code": None, "final_url": url, "text": "", "error": "not_attempted"
        }
        for _attempt in range(max(1, config.JOB_SOURCE_ATTEMPTS_PER_URL)):
            try:
                response = self.session.get(
                    url,
                    timeout=config.JOB_SOURCE_TIMEOUT_SECONDS,
                    allow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; TGTCJobVerifier/1.0)",
                        "Accept": "text/html,application/xhtml+xml,application/json",
                    },
                )
                result = {
                    "status_code": response.status_code,
                    "final_url": response.url,
                    "text": response.text[:2_000_000],
                    "content_type": response.headers.get("content-type", ""),
                }
            except requests.RequestException as exc:
                result = {
                    "status_code": None, "final_url": url, "text": "", "error": str(exc)
                }
            if result.get("status_code") not in TRANSIENT_STATUS_CODES and result.get("status_code") is not None:
                break
        # Do not turn a transient outage into a 24-hour cached fact.
        if result.get("status_code") not in TRANSIENT_STATUS_CODES and result.get("status_code") is not None:
            self.cache.set(url, result)
        return result

    def _discover_company_job_urls(
        self, job: Dict, company_domain: str
    ) -> Tuple[List[str], List[SourceAttempt]]:
        if not company_domain:
            return [], []
        website = _clean_url(str(job.get("employer_website") or "")) or f"https://{company_domain}/"
        base = f"{urlparse(website).scheme or 'https'}://{urlparse(website).netloc or company_domain}/"
        discovery_pages = [
            urljoin(base, "careers"),
            urljoin(base, "jobs"),
            urljoin(base, "careers/jobs"),
        ]
        title = str(job.get("job_title") or "")
        job_id = str(job.get("job_id") or "").lower()
        scored: List[Tuple[float, str]] = []
        attempts: List[SourceAttempt] = []
        for page_url in discovery_pages[:3]:
            payload = self._fetch(page_url)
            status = payload.get("status_code")
            final_url = payload.get("final_url") or page_url
            if not status or status >= 400:
                attempts.append(SourceAttempt(page_url, "company", "discovery_unavailable", status, final_url, payload.get("error") or ""))
                continue
            attempts.append(SourceAttempt(page_url, "company", "discovery_page", status, final_url))
            for link, anchor_text in _extract_links(payload.get("text") or "", final_url):
                source_type = classify_url_source(link, company_domain)
                if source_type not in {"company", "ats"}:
                    continue
                score = _title_match_score(title, f"{anchor_text} {unquote(urlparse(link).path)}")
                if job_id and job_id in link.lower():
                    score += 2.0
                if score >= 0.65:
                    scored.append((score, link))
        output: List[str] = []
        for _score, link in sorted(scored, reverse=True):
            if link not in output:
                output.append(link)
            if len(output) >= max(1, config.JOB_SOURCE_MAX_CANDIDATES):
                break
        return output, attempts

    def resolve(self, job: Dict, *, fetch: Optional[bool] = None) -> ResolvedJobSource:
        fetch = config.JOB_SOURCE_FETCH_ENABLED if fetch is None else fetch
        company_name = str(job.get("employer_name") or "").strip()
        company_domain = safe_company_domain(
            job.get("employer_website") or job.get("_employer_domain_input") or "",
            config.INTERMEDIARY_JOB_DOMAINS,
        )
        urls = candidate_urls(job)
        attempts: List[SourceAttempt] = []
        # A direct company/ATS link is cheaper and more authoritative than
        # crawling generic careers pages. Discovery is a bounded fallback only.
        has_direct_official_candidate = any(
            classify_url_source(url, company_domain) in {"company", "ats"}
            for url in urls
        )
        if fetch and company_domain and not has_direct_official_candidate:
            discovered, discovery_attempts = self._discover_company_job_urls(job, company_domain)
            attempts.extend(discovery_attempts)
            for discovered_url in discovered:
                if discovered_url not in urls:
                    urls.append(discovered_url)
        urls = urls[: max(1, config.JOB_SOURCE_MAX_CANDIDATES)]
        if not urls:
            return ResolvedJobSource(
                state="SOURCE_UNRESOLVED", retryable=False, attempts=attempts, notes=["no_candidate_urls"]
            )

        transient_official = False
        for url in urls:
            source_type = classify_url_source(url, company_domain)
            official_candidate = _official_for(url, source_type, company_domain)
            if source_type in {"aggregator", "google", "indeed", "linkedin"} and not official_candidate:
                attempts.append(SourceAttempt(url, source_type, f"skipped_{source_type}"))
                continue
            if not fetch:
                attempts.append(SourceAttempt(url, source_type, "fetch_disabled"))
                continue
            payload = self._fetch(url)
            status = payload.get("status_code")
            final_url = payload.get("final_url") or url
            body = payload.get("text") or ""
            final_source_type = classify_url_source(final_url, company_domain)
            official = _official_for(final_url, final_source_type, company_domain)
            if status in TRANSIENT_STATUS_CODES or status is None:
                transient_official = transient_official or official_candidate
                attempts.append(
                    SourceAttempt(
                        url,
                        source_type,
                        "temporary_failure",
                        status,
                        final_url,
                        payload.get("error") or "",
                    )
                )
                continue
            if status in {404, 410}:
                attempts.append(SourceAttempt(url, source_type, "inactive", status, final_url))
                if official_candidate:
                    return ResolvedJobSource(
                        state="INACTIVE_VERIFIED",
                        source_url=final_url,
                        source_type=final_source_type,
                        http_status=status,
                        active=False,
                        official=True,
                        attempts=attempts,
                    )
                continue
            if not status or status >= 400:
                attempts.append(SourceAttempt(url, source_type, "http_error", status, final_url))
                continue

            text = _strip_html(body)
            if any(re.search(pattern, text[:8000], re.I) for pattern in CLOSED_PATTERNS):
                attempts.append(SourceAttempt(url, source_type, "inactive_text", status, final_url))
                return ResolvedJobSource(
                    state="INACTIVE_VERIFIED",
                    source_url=final_url,
                    source_type=final_source_type,
                    http_status=status,
                    active=False,
                    official=official,
                    attempts=attempts,
                )

            obj = _jobposting(body)
            canonical_title = str(obj.get("title") or "").strip()
            canonical_employer = _employer_from_json_ld(obj)
            description = _strip_html(obj.get("description")) if obj else ""
            if not description:
                description = text
            employment_type = obj.get("employmentType") if obj else ""
            if isinstance(employment_type, list):
                employment_type = ", ".join(str(value) for value in employment_type)
            valid_through = str(obj.get("validThrough") or "") if obj else ""
            active = not _date_in_past(valid_through)
            employer_matches = not canonical_employer or company_names_compatible(
                company_name, canonical_employer
            )
            title_matches = not canonical_title or _titles_compatible(
                str(job.get("job_title") or ""), canonical_title
            )
            page_title_match = _job_page_has_title(str(job.get("job_title") or ""), text)
            job_page_evidence = bool(obj) or (
                page_title_match
                and len(description) >= 500
                and bool(re.search(r"\b(?:apply|responsibilities|qualifications|requirements|employment type|job description)\b", text, re.I))
                and urlparse(final_url).path not in {"", "/", "/careers", "/jobs", "/careers/", "/jobs/"}
            )
            corroborated = employer_matches and title_matches and job_page_evidence
            if official and employer_matches and job_page_evidence:
                attempts.append(SourceAttempt(url, source_type, "resolved", status, final_url))
                return ResolvedJobSource(
                    state="ACTIVE_VERIFIED" if active else "INACTIVE_VERIFIED",
                    source_url=final_url,
                    source_type=final_source_type,
                    http_status=status,
                    active=active,
                    canonical_title=canonical_title or str(job.get("job_title") or ""),
                    canonical_employer=canonical_employer or company_name,
                    description=description,
                    location_text=_location_from_json_ld(obj),
                    employment_type=str(employment_type or ""),
                    date_posted=str(obj.get("datePosted") or "") if obj else "",
                    valid_through=valid_through,
                    job_id=str(obj.get("identifier") or job.get("job_id") or ""),
                    official=True,
                    corroborated=corroborated,
                    attempts=attempts,
                    notes=[] if title_matches else ["material_title_mismatch"],
                )
            attempts.append(
                SourceAttempt(url, source_type, "identity_not_corroborated", status, final_url)
            )

        if transient_official:
            return ResolvedJobSource(
                state="SOURCE_TEMPORARILY_UNAVAILABLE",
                temporarily_unavailable=True,
                retryable=True,
                attempts=attempts,
            )
        return ResolvedJobSource(
            state="SOURCE_UNRESOLVED", retryable=False, attempts=attempts
        )


def _title_tokens(value: str) -> List[str]:
    stop = {"remote", "usa", "us", "united", "states", "job", "position"}
    return [
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if token not in stop
    ]


def _titles_compatible(discovery: str, official: str) -> bool:
    left = set(_title_tokens(discovery))
    right = set(_title_tokens(official))
    if not left or not right:
        return False
    return len(left & right) / max(1, min(len(left), len(right))) >= 0.65


def title_materially_differs(discovery: str, official: str) -> bool:
    if not discovery or not official:
        return False
    left = set(_title_tokens(discovery))
    right = set(_title_tokens(official))
    material_markers = {
        "intern", "junior", "associate", "analyst", "specialist", "engineer",
        "developer", "manager", "lead", "senior", "director", "head", "vp",
        "president", "chief", "contract", "contractor", "temporary", "fractional",
    }
    # A changed seniority/function marker is material even when most title tokens
    # overlap (Corporate FP&A Analyst vs Corporate FP&A Lead).
    if (left & material_markers) != (right & material_markers):
        return True
    return not _titles_compatible(discovery, official)
