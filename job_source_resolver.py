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
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

import config
from ats_public_adapters import discover_public_ats_urls
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
    try:
        parsed = urlparse(value)
    except (TypeError, ValueError):
        return ""
    if parsed.netloc.lower().endswith("google.com"):
        query = parse_qs(parsed.query)
        for key in ("q", "url", "u"):
            for candidate in query.get(key, []):
                candidate = unquote(candidate)
                if candidate.startswith(("http://", "https://")):
                    return candidate
    return value


def _safe_join_url(base_url: str, raw_url: str) -> str:
    """Join and normalize untrusted HTML URL values without aborting a run."""
    try:
        return _clean_url(urljoin(str(base_url or ""), str(raw_url or "")))
    except (TypeError, ValueError):
        return ""


def candidate_urls(job: Dict) -> List[str]:
    values: List[str] = []
    for option in job.get("apply_options") or []:
        if isinstance(option, dict):
            values.append(option.get("apply_link") or "")
    values.extend(
        [
            job.get("official_job_url") or "",
            job.get("canonical_source_url") or "",
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


def _jobpostings(body: str) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for obj in _json_ld_objects(body):
        kinds = obj.get("@type")
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if any(str(kind).lower() == "jobposting" for kind in kinds):
            output.append(obj)
    return output


def _jobposting(body: str) -> Dict[str, Any]:
    """Backward-compatible first JobPosting helper used by older tests."""
    postings = _jobpostings(body)
    return postings[0] if postings else {}


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


def _parse_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _date_in_past(value: str) -> bool:
    parsed = _parse_date(value)
    return bool(parsed and parsed < datetime.now(timezone.utc))


def _official_for(
    url: str,
    source_type: str,
    company_domain: str,
    *,
    discovered_from_company: bool = False,
) -> bool:
    """Return whether URL provenance itself is sufficient to call it official.

    A shared ATS hostname is not proof of employer identity.  An ATS URL is
    considered official only when it was discovered from the employer's own
    domain. Provider-supplied ATS links can still become official later when
    their JobPosting explicitly names a compatible hiringOrganization.
    """
    host = normalize_company_domain(url)
    if source_type == "company":
        return bool(company_domain and host == company_domain)
    if source_type == "ats":
        return bool(discovered_from_company)
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
    return _title_match_score(discovery_title, page_text[:40_000]) >= 0.65




def _page_heading_texts(body: str) -> List[str]:
    values: List[str] = []
    for pattern in (r"<title[^>]*>(.*?)</title>", r"<h1[^>]*>(.*?)</h1>", r"<h2[^>]*>(.*?)</h2>"):
        for raw in re.findall(pattern, body, re.I | re.S):
            text = _strip_html(raw)
            if text:
                values.append(text[:500])
    return values[:30]


def _has_apply_action(body: str, text: str) -> bool:
    """Require an enabled, actionable application control.

    Bare prose such as "click Apply" or a disabled archived button is not
    positive evidence that applications are currently being accepted.
    """
    for match in re.finditer(
        r"<(a|button)([^>]*)>(.*?)</\1>|<input([^>]*)>",
        body,
        re.I | re.S,
    ):
        tag = (match.group(1) or "input").lower()
        attrs = str(match.group(2) or match.group(4) or "")
        inner = _strip_html(match.group(3) or "")
        combined = f"{attrs} {inner}"
        if not re.search(
            r"\b(?:apply(?: now| for this (?:job|position))?|submit (?:your )?application|job-apply|application-form)\b",
            combined,
            re.I,
        ):
            continue
        if re.search(r"\bdisabled\b|aria-disabled\s*=\s*[\"']?true", attrs, re.I):
            continue
        if tag == "a":
            href = re.search(r"href\s*=\s*[\"']([^\"']*)", attrs, re.I)
            if not href or href.group(1).strip().lower() in {"", "#", "javascript:void(0)"}:
                continue
        return True
    return False


def _looks_like_individual_job_path(url: str) -> bool:
    path = unquote(urlparse(url).path).strip("/").lower()
    if not path or path in {"careers", "jobs", "careers/jobs", "open-positions", "opportunities"}:
        return False
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 and not re.search(r"(?:job|position|opening)[-_]?[a-z0-9]{4,}", path):
        return False
    return bool(
        re.search(r"/(?:job|jobs|position|positions|opening|openings|p|apply)/[^/]{3,}", f"/{path}")
        or re.search(r"\b[a-z]+[-_][a-z]+", path)
        or re.search(r"\d{4,}", path)
    )


def _select_jobposting(
    body: str, discovery_title: str, company_name: str
) -> Tuple[Dict[str, Any], bool]:
    postings = _jobpostings(body)
    if not postings:
        return {}, False
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for obj in postings:
        title = str(obj.get("title") or "")
        employer = _employer_from_json_ld(obj)
        title_score = _title_match_score(discovery_title, title)
        employer_ok = not employer or company_names_compatible(company_name, employer)
        ranked.append((title_score + (1.0 if employer_ok else -2.0), obj))
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = ranked[0][1]
    return selected, len(postings) > 1


def _activity_signal(
    obj: Dict[str, Any], body: str, text: str
) -> Tuple[Optional[bool], List[str]]:
    valid_through = str(obj.get("validThrough") or "") if obj else ""
    if valid_through:
        return (not _date_in_past(valid_through)), ["validThrough"]
    if any(re.search(pattern, text[:12_000], re.I) for pattern in CLOSED_PATTERNS):
        return False, ["closed_text"]
    date_posted = _parse_date(str(obj.get("datePosted") or "")) if obj else None
    if date_posted and date_posted >= datetime.now(timezone.utc) - timedelta(days=max(1, config.JOB_SOURCE_MAX_ACTIVE_AGE_DAYS)):
        return True, ["recent_official_datePosted"]
    if _has_apply_action(body, text):
        return True, ["official_apply_action"]
    return None, ["no_positive_activity_signal"]


def _extract_links(body: str, base_url: str) -> List[Tuple[str, str]]:
    parser = _LinkExtractor()
    try:
        parser.feed(body)
    except Exception:
        return []
    output: List[Tuple[str, str]] = []
    for href, text in parser.links:
        url = _safe_join_url(base_url, href)
        if url:
            output.append((url, text))
    return output


def _extract_embedded_urls(body: str, base_url: str) -> List[Tuple[str, str]]:
    """Extract job/career URLs embedded in scripts and data attributes.

    Many modern careers pages render the actual ATS board client-side.  The
    first implementation inspected only ``<a href>`` elements, which meant a
    healthy 200 careers page could still produce zero candidates.  This helper
    intentionally extracts only URL-looking values with job/career semantics;
    the normal official-source and title checks remain authoritative later.
    """
    decoded = html.unescape(str(body or "")).replace(r"\/", "/")
    candidates: List[Tuple[str, str]] = []
    absolute = re.findall(r"https?://[^\s\"'<>\\]+", decoded, re.I)
    relative = re.findall(
        r"[\"']((?:/|\.\./|\./)[^\"']{0,500}(?:job|jobs|career|careers|position|opening|opportunit)[^\"']{0,500})[\"']",
        decoded,
        re.I,
    )
    for raw in [*absolute, *relative]:
        raw = raw.rstrip("),.;]}")
        url = _safe_join_url(base_url, raw)
        if not url:
            continue
        text = unquote(urlparse(url).path).replace("-", " ").replace("_", " ")
        if (
            classify_url_source(url, "") != "ats"
            and not re.search(r"\b(?:job|jobs|career|careers|position|opening|opportunit)", f"{url} {text}", re.I)
        ):
            continue
        candidates.append((url, text))
    return list(dict.fromkeys(candidates))


def _looks_like_board_url(url: str, source_type: str, anchor_text: str = "") -> bool:
    path = unquote(urlparse(url).path).strip("/").lower()
    text = f"{path} {anchor_text}".lower()
    if source_type == "ats":
        return not bool(re.search(r"/(?:job|jobs|position|positions|opening|openings|p|apply)/[^/]{4,}", f"/{path}"))
    return bool(re.search(r"\b(?:careers?|jobs?|open positions?|opportunities)\b", text)) and len(path.split("/")) <= 5


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
            base,
            urljoin(base, "careers"),
            urljoin(base, "jobs"),
            urljoin(base, "career"),
            urljoin(base, "about/careers"),
            urljoin(base, "company/careers"),
            urljoin(base, "join-us"),
            urljoin(base, "opportunities"),
        ]
        title = str(job.get("job_title") or "")
        job_id = str(job.get("job_id") or "").lower()
        scored: List[Tuple[float, str]] = []
        board_candidates: List[str] = []
        attempts: List[SourceAttempt] = []
        visited_pages: set[str] = set()
        for page_url in discovery_pages[: max(1, config.JOB_SOURCE_DISCOVERY_MAX_PAGES)]:
            payload = self._fetch(page_url)
            status = payload.get("status_code")
            final_url = payload.get("final_url") or page_url
            if final_url in visited_pages:
                continue
            visited_pages.add(final_url)
            if not status or status >= 400:
                attempts.append(SourceAttempt(page_url, "company", "discovery_unavailable", status, final_url, payload.get("error") or ""))
                continue
            attempts.append(SourceAttempt(page_url, "company", "discovery_page", status, final_url))
            page_links = [
                *_extract_links(payload.get("text") or "", final_url),
                *_extract_embedded_urls(payload.get("text") or "", final_url),
            ]
            for link, anchor_text in page_links:
                source_type = classify_url_source(link, company_domain)
                if source_type not in {"company", "ats"}:
                    continue
                score = _title_match_score(title, f"{anchor_text} {unquote(urlparse(link).path)}")
                if job_id and job_id in link.lower():
                    score += 2.0
                if score >= 0.55:
                    scored.append((score, link))
                elif _looks_like_board_url(link, source_type, anchor_text):
                    board_candidates.append(link)

        # Follow a few ATS/company board pages.  This remains bounded, while
        # supporting Workday/Greenhouse/Lever/Ashby-style client-rendered boards.
        for board_url in list(dict.fromkeys(board_candidates))[: max(1, config.JOB_SOURCE_DISCOVERY_MAX_BOARD_PAGES)]:
            payload = self._fetch(board_url)
            status = payload.get("status_code")
            final_url = payload.get("final_url") or board_url
            source_type = classify_url_source(final_url, company_domain)
            if not status or status >= 400:
                attempts.append(SourceAttempt(board_url, source_type, "board_discovery_unavailable", status, final_url, payload.get("error") or ""))
                continue
            attempts.append(SourceAttempt(board_url, source_type, "board_discovery_page", status, final_url))
            body = payload.get("text") or ""

            # Public ATS APIs provide stable discovery for client-rendered
            # Greenhouse and Lever boards. Employer identity still comes from
            # this board URL having been discovered on the employer website.
            adapter_result = discover_public_ats_urls(final_url, title, self._fetch)
            for item in adapter_result.attempts:
                attempts.append(SourceAttempt(
                    item.get("url") or final_url,
                    "ats",
                    f"public_ats_{item.get('provider')}_{item.get('status')}",
                    item.get("http_status"),
                    item.get("final_url") or "",
                    item.get("error") or "",
                ))
            for adapter_url in adapter_result.urls:
                score = _title_match_score(title, unquote(urlparse(adapter_url).path))
                # The API already performed a title match. Keep a minimum
                # score so the exact posting is validated by resolve().
                scored.append((max(score, 0.75), adapter_url))

            # Some ATS URLs already point at the exact posting but were hidden
            # in script data. Keep the board itself as a candidate only when the
            # requested title is strongly present; resolve() performs the final
            # JobPosting/identity validation.
            board_score = _title_match_score(title, _strip_html(body)[:40_000])
            if board_score >= 0.8:
                scored.append((board_score, final_url))
            for link, anchor_text in [*_extract_links(body, final_url), *_extract_embedded_urls(body, final_url)]:
                link_source = classify_url_source(link, company_domain)
                if link_source not in {"company", "ats"}:
                    continue
                score = _title_match_score(title, f"{anchor_text} {unquote(urlparse(link).path)}")
                if score >= 0.55:
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
        discovered_urls: set[str] = set()
        if fetch and company_domain:
            discovered, discovery_attempts = self._discover_company_job_urls(job, company_domain)
            attempts.extend(discovery_attempts)
            discovered_urls.update(discovered)
            urls = [*discovered, *urls]
        urls = list(dict.fromkeys(urls))
        urls.sort(
            key=lambda url: {
                "company": 0,
                "ats": 1,
                "linkedin": 3,
                "indeed": 3,
                "other": 4,
                "aggregator": 5,
                "google": 6,
            }.get(classify_url_source(url, company_domain), 7)
        )
        urls = urls[: max(1, config.JOB_SOURCE_MAX_CANDIDATES)]
        if not urls:
            return ResolvedJobSource(
                state="SOURCE_UNRESOLVED", retryable=False, attempts=attempts, notes=["no_candidate_urls"]
            )

        transient_official = any(
            attempt.http_status in TRANSIENT_STATUS_CODES
            for attempt in attempts
            if attempt.source_type in {"company", "ats"}
        )
        inactive_candidates: List[ResolvedJobSource] = []
        activity_unknown = False

        for url in urls:
            source_type = classify_url_source(url, company_domain)
            discovered_from_company = url in discovered_urls
            official_candidate = _official_for(
                url, source_type, company_domain, discovered_from_company=discovered_from_company
            )
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
            provenance_official = _official_for(
                final_url,
                final_source_type,
                company_domain,
                discovered_from_company=(discovered_from_company or final_url in discovered_urls),
            )

            if status in TRANSIENT_STATUS_CODES or status is None:
                transient_official = transient_official or official_candidate or provenance_official
                attempts.append(SourceAttempt(
                    url, source_type, "temporary_failure", status, final_url, payload.get("error") or ""
                ))
                continue

            if status in {404, 410}:
                attempts.append(SourceAttempt(url, source_type, "inactive", status, final_url))
                if official_candidate or provenance_official:
                    inactive_candidates.append(ResolvedJobSource(
                        state="INACTIVE_VERIFIED", source_url=final_url,
                        source_type=final_source_type, http_status=status, active=False,
                        official=True, attempts=list(attempts), notes=["inactive_http_status"],
                    ))
                # Never let one stale official URL hide a second active ATS/company URL.
                continue
            if not status or status >= 400:
                attempts.append(SourceAttempt(url, source_type, "http_error", status, final_url))
                continue

            text = _strip_html(body)
            obj, multiple_postings = _select_jobposting(
                body, str(job.get("job_title") or ""), company_name
            )
            canonical_title = str(obj.get("title") or "").strip()
            canonical_employer = _employer_from_json_ld(obj)
            description = _strip_html(obj.get("description")) if obj else ""
            if not description:
                description = text
            employment_type = obj.get("employmentType") if obj else ""
            if isinstance(employment_type, list):
                employment_type = ", ".join(str(value) for value in employment_type)
            valid_through = str(obj.get("validThrough") or "") if obj else ""

            title_matches = bool(canonical_title) and _titles_compatible(
                str(job.get("job_title") or ""), canonical_title
            )
            heading_scores = [
                _title_match_score(str(job.get("job_title") or ""), value)
                for value in _page_heading_texts(body)
            ]
            heading_match = max(heading_scores, default=0.0) >= 0.80
            employer_matches = bool(canonical_employer) and company_names_compatible(
                company_name, canonical_employer
            )
            if canonical_employer and not employer_matches:
                attempts.append(SourceAttempt(
                    url, source_type, "employer_identity_mismatch", status, final_url
                ))
                continue

            # Company-domain provenance proves employer identity. Shared ATS
            # domains require either employer discovery provenance or an explicit
            # compatible hiringOrganization in the posting itself.
            if final_source_type == "company":
                identity_official = provenance_official
            elif final_source_type == "ats":
                identity_official = provenance_official or employer_matches
            else:
                identity_official = provenance_official and employer_matches

            jsonld_evidence = bool(obj and title_matches and identity_official)
            listing_markers = len(re.findall(
                r"(?:job-card|job-listing|opening-item|position-item|data-job-id)", body, re.I
            ))
            heuristic_evidence = bool(
                not obj
                and identity_official
                and heading_match
                and _looks_like_individual_job_path(final_url)
                and _has_apply_action(body, text)
                and len(description) >= 400
                and listing_markers <= 3
            )
            job_page_evidence = jsonld_evidence or heuristic_evidence

            if multiple_postings and not jsonld_evidence:
                attempts.append(SourceAttempt(url, source_type, "multi_job_listing", status, final_url))
                continue
            if not identity_official:
                attempts.append(SourceAttempt(url, source_type, "employer_identity_unverified", status, final_url))
                continue
            if not job_page_evidence:
                attempts.append(SourceAttempt(url, source_type, "job_identity_unverified", status, final_url))
                continue

            closed = any(re.search(pattern, text[:12_000], re.I) for pattern in CLOSED_PATTERNS)
            active, active_notes = _activity_signal(obj, body, text)
            if closed:
                active = False
                active_notes = ["closed_text"]
            if active is False:
                attempts.append(SourceAttempt(url, source_type, "inactive_verified", status, final_url))
                inactive_candidates.append(ResolvedJobSource(
                    state="INACTIVE_VERIFIED", source_url=final_url,
                    source_type=final_source_type, http_status=status, active=False,
                    canonical_title=canonical_title or str(job.get("job_title") or ""),
                    canonical_employer=canonical_employer or company_name,
                    description=description, location_text=_location_from_json_ld(obj),
                    employment_type=str(employment_type or ""),
                    date_posted=str(obj.get("datePosted") or "") if obj else "",
                    valid_through=valid_through,
                    job_id=str(obj.get("identifier") or job.get("job_id") or ""),
                    official=True, corroborated=True, attempts=list(attempts), notes=active_notes,
                ))
                continue
            if active is None:
                activity_unknown = True
                attempts.append(SourceAttempt(url, source_type, "activity_unconfirmed", status, final_url))
                continue

            attempts.append(SourceAttempt(url, source_type, "resolved", status, final_url))
            return ResolvedJobSource(
                state="ACTIVE_VERIFIED", source_url=final_url,
                source_type=final_source_type, http_status=status, active=True,
                canonical_title=canonical_title or str(job.get("job_title") or ""),
                canonical_employer=canonical_employer or company_name,
                description=description, location_text=_location_from_json_ld(obj),
                employment_type=str(employment_type or ""),
                date_posted=str(obj.get("datePosted") or "") if obj else "",
                valid_through=valid_through,
                job_id=str(obj.get("identifier") or job.get("job_id") or ""),
                official=True, corroborated=True, attempts=attempts, notes=active_notes,
            )

        # A potentially active transient source outranks an older inactive URL.
        if transient_official:
            return ResolvedJobSource(
                state="SOURCE_TEMPORARILY_UNAVAILABLE", temporarily_unavailable=True,
                retryable=True, attempts=attempts, notes=["official_source_temporarily_unavailable"],
            )
        if activity_unknown:
            return ResolvedJobSource(
                state="SOURCE_UNRESOLVED", retryable=True, attempts=attempts,
                notes=["activity_unconfirmed"],
            )
        if inactive_candidates:
            result = inactive_candidates[-1]
            result.attempts = attempts
            return result
        return ResolvedJobSource(
            state="SOURCE_UNRESOLVED", retryable=False, attempts=attempts, notes=[],
        )


def _title_tokens(value: str) -> List[str]:
    value = re.sub(r"\bjobs?\s+in\s+[^|–—-]+$", " ", str(value or ""), flags=re.I)
    value = re.sub(
        r"\b(?:hiring now|entry level|work from home|full[- ]time|100% remote)\b",
        " ",
        value,
        flags=re.I,
    )
    stop = {
        "remote", "usa", "us", "united", "states", "job", "jobs",
        "position", "hiring", "now", "entry", "level", "in", "at",
    }
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
