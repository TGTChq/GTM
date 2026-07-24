"""Free, unauthenticated global job-feed adapters.

Each adapter maps its provider payload into the pipeline's existing raw-job
contract. Provider feeds are discovery evidence: they never establish employer
identity by themselves and remain subject to the normal Job/Account/Contact
validation gates.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests

import config

logger = logging.getLogger(__name__)


@dataclass
class FetchPayload:
    status_code: Optional[int]
    url: str
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    error: str = ""


@dataclass
class SourceResult:
    source: str
    jobs: List[Dict[str, Any]] = field(default_factory=list)
    requests_attempted: int = 0
    requests_succeeded: int = 0
    raw_records: int = 0
    pages: int = 0
    success: bool = True
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


Fetcher = Callable[..., FetchPayload]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        value = re.sub(r"\s+", " ", data or "").strip()
        if value:
            self.parts.append(value)


def html_to_text(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = "\n".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


def _safe_public_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except (socket.gaierror, OSError):
        return False
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(address).is_global for address in addresses)
    except ValueError:
        return False


def default_fetcher(
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Optional[int] = None,
    method: str = "GET",
    json_body: Optional[Mapping[str, Any]] = None,
) -> FetchPayload:
    request_headers = {
        "User-Agent": "TGTCJobAcquisition/1.3 (+https://tgtc.io)",
        "Accept": "application/json,application/rss+xml,application/xml,text/xml,text/html;q=0.8",
        **dict(headers or {}),
    }
    current_url = str(url or "").strip()
    current_params: Optional[Mapping[str, Any]] = params
    for _redirect in range(4):
        if not _safe_public_url(current_url):
            return FetchPayload(status_code=None, url=current_url, error="unsafe_or_unresolvable_url")
        try:
            request_method = str(method or "GET").upper()
            if request_method not in {"GET", "POST"}:
                return FetchPayload(status_code=None, url=current_url, error="unsupported_http_method")
            with requests.request(
                request_method,
                current_url,
                params=current_params,
                json=dict(json_body or {}) if json_body is not None else None,
                headers=request_headers,
                timeout=timeout or config.FREE_SOURCE_REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location", "")
                    if not location:
                        return FetchPayload(status_code=response.status_code, url=current_url, error="redirect_without_location")
                    current_url = urljoin(response.url or current_url, location)
                    current_params = None
                    continue
                limit = max(1, int(config.FREE_SOURCE_MAX_RESPONSE_CHARS))
                chunks: List[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    remaining = limit - total
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    total += min(len(chunk), remaining)
                    if total >= limit:
                        break
                encoding = response.encoding or "utf-8"
                text = b"".join(chunks).decode(encoding, errors="replace")
                return FetchPayload(
                    status_code=response.status_code,
                    url=response.url or current_url,
                    text=text,
                    headers={str(k): str(v) for k, v in response.headers.items()},
                )
        except requests.RequestException as exc:
            return FetchPayload(status_code=None, url=current_url, error=str(exc))
    return FetchPayload(status_code=None, url=current_url, error="too_many_redirects")


def _json(payload: FetchPayload) -> Any:
    try:
        return json.loads(payload.text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _iso_datetime(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    raw = str(value).strip()
    if not raw:
        return ""
    if raw.isdigit():
        return _iso_datetime(int(raw))
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _employment_type(value: Any) -> str:
    normalized = re.sub(r"[^a-z]", "", str(value or "").lower())
    mapping = {
        "fulltime": "Full Time",
        "parttime": "Part Time",
        "contract": "Contract",
        "contractor": "Contract",
        "temporary": "Temporary",
        "intern": "Internship",
        "internship": "Internship",
        "volunteer": "Volunteer",
    }
    return mapping.get(normalized, str(value or "").strip())


def _country_code_from_text(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    if not text:
        return ""
    us_markers = {
        "us", "usa", "united states", "united states of america",
        "north america us", "remote us", "us only",
    }
    if text in us_markers or re.search(r"\b(?:united states|usa|u s|us only|remote us)\b", text):
        return "US"
    return ""


def _provider_job_id(source: str, value: Any, *fallback_parts: Any) -> str:
    raw = str(value or "").strip()
    if raw:
        return f"{source}:{raw}"
    digest = hashlib.sha256(
        "|".join(str(part or "").strip().lower() for part in fallback_parts).encode("utf-8")
    ).hexdigest()[:24]
    return f"{source}:{digest}"


def _canonical_job(
    *,
    source: str,
    source_name: str,
    source_home: str,
    source_id: Any,
    title: Any,
    company: Any,
    description: Any,
    url: Any,
    location: Any = "",
    country: Any = "",
    employment_type: Any = "",
    posted_at: Any = "",
    expires_at: Any = "",
    employer_website: Any = "",
    tags: Optional[Iterable[Any]] = None,
    salary_min: Any = None,
    salary_max: Any = None,
    salary_currency: Any = "",
    salary_period: Any = "",
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()
    company_text = re.sub(r"\s+", " ", str(company or "")).strip()
    description_text = html_to_text(description)
    url_text = str(url or "").strip()
    location_text = re.sub(r"\s+", " ", str(location or "")).strip()
    country_text = str(country or "").strip().upper()
    if not country_text:
        country_text = _country_code_from_text(location_text)
    if not location_text:
        location_text = "Remote"
    job_id = _provider_job_id(source, source_id, company_text, title_text, url_text)
    job: Dict[str, Any] = {
        "job_id": job_id,
        "job_title": title_text,
        "employer_name": company_text,
        "employer_website": str(employer_website or "").strip(),
        "job_publisher": source_name,
        "job_description": description_text,
        "job_apply_link": url_text,
        "job_apply_is_direct": False,
        "job_google_link": "",
        "job_location": location_text,
        "job_country": country_text,
        "job_is_remote": True,
        "job_employment_type": _employment_type(employment_type),
        "job_posted_at_datetime_utc": _iso_datetime(posted_at),
        "job_offer_expiration_datetime_utc": _iso_datetime(expires_at),
        "job_min_salary": salary_min,
        "job_max_salary": salary_max,
        "job_salary_currency": str(salary_currency or "").strip(),
        "job_salary_period": str(salary_period or "").strip(),
        "job_required_skills": [str(item) for item in (tags or []) if str(item or "").strip()],
        "apply_options": ([{"publisher": source_name, "apply_link": url_text, "is_direct": False}] if url_text else []),
        "canonical_source_url": url_text,
        "_acquisition_source": source,
        "_source_home_url": source_home,
        "_provider_record_structured": True,
    }
    if extra:
        job.update(dict(extra))
    return job


class HimalayasAdapter:
    name = "himalayas"
    display_name = "Himalayas"
    endpoint = "https://himalayas.app/jobs/api"

    def fetch(self, fetcher: Fetcher = default_fetcher) -> SourceResult:
        result = SourceResult(source=self.name)
        offset = 0
        limit = min(20, max(1, config.HIMALAYAS_PAGE_SIZE))
        max_pages = max(1, config.HIMALAYAS_MAX_PAGES)
        max_records = max(1, config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE)
        total_count: Optional[int] = None
        for _page in range(max_pages):
            payload = fetcher(self.endpoint, params={"offset": offset, "limit": limit})
            result.requests_attempted += 1
            result.pages += 1
            if payload.status_code != 200:
                result.success = False if not result.jobs else True
                result.errors.append(
                    f"HTTP {payload.status_code or 'error'}: {payload.error or payload.text[:200]}"
                )
                break
            result.requests_succeeded += 1
            data = _json(payload)
            if not isinstance(data, dict):
                result.success = False if not result.jobs else True
                result.errors.append("invalid_json_object")
                break
            rows = data.get("jobs") or []
            if not isinstance(rows, list):
                rows = []
            result.raw_records += len(rows)
            try:
                total_count = int(data.get("totalCount"))
            except (TypeError, ValueError):
                total_count = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                restrictions = row.get("locationRestrictions") or []
                restriction_names = []
                for item in restrictions if isinstance(restrictions, list) else []:
                    if isinstance(item, dict):
                        restriction_names.append(str(item.get("name") or item.get("alpha2") or ""))
                    else:
                        restriction_names.append(str(item or ""))
                country = "US" if any(_country_code_from_text(item) == "US" for item in restriction_names) else ""
                location = (
                    "Remote - United States"
                    if country == "US"
                    else "Remote - Worldwide"
                    if not restriction_names
                    else "Remote - " + ", ".join(item for item in restriction_names if item)
                )
                result.jobs.append(_canonical_job(
                    source=self.name,
                    source_name=self.display_name,
                    source_home="https://himalayas.app",
                    source_id=row.get("guid"),
                    title=row.get("title"),
                    company=row.get("companyName"),
                    description=row.get("description") or row.get("excerpt"),
                    url=row.get("applicationLink"),
                    location=location,
                    country=country,
                    employment_type=row.get("employmentType"),
                    posted_at=row.get("pubDate"),
                    expires_at=row.get("expiryDate"),
                    tags=[*(row.get("categories") or []), *(row.get("parentCategories") or [])],
                    salary_min=row.get("minSalary"),
                    salary_max=row.get("maxSalary"),
                    salary_currency=row.get("currency"),
                    salary_period=row.get("salaryPeriod"),
                    extra={"_source_company_slug": str(row.get("companySlug") or "")},
                ))
                if len(result.jobs) >= max_records:
                    break
            if len(result.jobs) >= max_records or not rows:
                break
            offset += len(rows)
            if total_count is not None and offset >= total_count:
                break
        result.metadata = {"total_count": total_count, "endpoint": self.endpoint}
        return result


class JobicyAdapter:
    name = "jobicy"
    display_name = "Jobicy"
    endpoint = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self, fetcher: Fetcher = default_fetcher) -> SourceResult:
        result = SourceResult(source=self.name, requests_attempted=1, pages=1)
        payload = fetcher(self.endpoint, params={"count": 50, "geo": "usa"})
        if payload.status_code != 200:
            result.success = False
            result.errors.append(f"HTTP {payload.status_code or 'error'}: {payload.error or payload.text[:200]}")
            return result
        result.requests_succeeded = 1
        data = _json(payload)
        if not isinstance(data, dict):
            result.success = False
            result.errors.append("invalid_json_object")
            return result
        rows = data.get("jobs", [])
        if not isinstance(rows, list):
            result.success = False
            result.errors.append("invalid_jobs_array")
            return result
        result.raw_records = len(rows)
        for row in rows[: max(1, config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE)]:
            if not isinstance(row, dict):
                continue
            location = str(row.get("jobGeo") or "Remote - United States")
            job_type = row.get("jobType") or ""
            if isinstance(job_type, list):
                job_type = job_type[0] if job_type else ""
            industry = row.get("jobIndustry") or []
            if not isinstance(industry, list):
                industry = [industry]
            result.jobs.append(_canonical_job(
                source=self.name,
                source_name=self.display_name,
                source_home="https://jobicy.com",
                source_id=row.get("id") or row.get("jobSlug"),
                title=row.get("jobTitle"),
                company=row.get("companyName"),
                description=row.get("jobDescription") or row.get("jobExcerpt"),
                url=row.get("url"),
                location=location,
                country="US" if _country_code_from_text(location) == "US" or location.lower() == "usa" else "",
                employment_type=job_type,
                posted_at=row.get("pubDate"),
                tags=[*industry, row.get("jobLevel")],
                salary_min=row.get("annualSalaryMin") or row.get("salaryMin"),
                salary_max=row.get("annualSalaryMax") or row.get("salaryMax"),
                salary_currency=row.get("salaryCurrency"),
                salary_period=row.get("salaryPeriod") or "annual",
            ))
        return result


class RemotiveAdapter:
    name = "remotive"
    display_name = "Remotive"
    endpoint = "https://remotive.com/api/remote-jobs"

    def fetch(self, fetcher: Fetcher = default_fetcher) -> SourceResult:
        result = SourceResult(source=self.name, requests_attempted=1, pages=1)
        payload = fetcher(self.endpoint)
        if payload.status_code != 200:
            result.success = False
            result.errors.append(f"HTTP {payload.status_code or 'error'}: {payload.error or payload.text[:200]}")
            return result
        result.requests_succeeded = 1
        data = _json(payload)
        if not isinstance(data, dict):
            result.success = False
            result.errors.append("invalid_json_object")
            return result
        rows = data.get("jobs", [])
        if not isinstance(rows, list):
            result.success = False
            result.errors.append("invalid_jobs_array")
            return result
        result.raw_records = len(rows)
        for row in rows[: max(1, config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE)]:
            if not isinstance(row, dict):
                continue
            location = str(row.get("candidate_required_location") or "Remote")
            result.jobs.append(_canonical_job(
                source=self.name,
                source_name=self.display_name,
                source_home="https://remotive.com",
                source_id=row.get("id"),
                title=row.get("title"),
                company=row.get("company_name"),
                description=row.get("description"),
                url=row.get("url"),
                location=location,
                country=_country_code_from_text(location),
                employment_type=row.get("job_type"),
                posted_at=row.get("publication_date"),
                tags=[row.get("category"), *(row.get("tags") or [])],
                extra={"job_salary_text": str(row.get("salary") or "")},
            ))
        return result


class RemoteOkAdapter:
    name = "remoteok"
    display_name = "Remote OK"
    endpoint = "https://remoteok.com/api"

    def fetch(self, fetcher: Fetcher = default_fetcher) -> SourceResult:
        result = SourceResult(source=self.name, requests_attempted=1, pages=1)
        payload = fetcher(self.endpoint, headers={"Accept": "application/json"})
        if payload.status_code != 200:
            result.success = False
            result.errors.append(f"HTTP {payload.status_code or 'error'}: {payload.error or payload.text[:200]}")
            return result
        result.requests_succeeded = 1
        data = _json(payload)
        if not isinstance(data, list):
            result.success = False
            result.errors.append("invalid_json_array")
            return result
        rows = [item for item in data if isinstance(item, dict) and item.get("id")]
        result.raw_records = len(rows)
        for row in rows[: max(1, config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE)]:
            location = str(row.get("location") or "Remote")
            result.jobs.append(_canonical_job(
                source=self.name,
                source_name=self.display_name,
                source_home="https://remoteok.com",
                source_id=row.get("id") or row.get("slug"),
                title=row.get("position"),
                company=row.get("company"),
                description=row.get("description"),
                url=row.get("apply_url") or row.get("url"),
                location=location,
                country=_country_code_from_text(location),
                employment_type=(
                    "Full Time"
                    if any(re.sub(r"[^a-z]", "", str(tag).lower()) == "fulltime" for tag in (row.get("tags") or []))
                    else ""
                ),
                posted_at=row.get("date") or row.get("epoch"),
                tags=row.get("tags") or [],
                salary_min=row.get("salary_min"),
                salary_max=row.get("salary_max"),
                salary_currency="USD" if row.get("salary_min") or row.get("salary_max") else "",
            ))
        return result


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _xml_child_text(item: ElementTree.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in list(item):
        if _xml_local_name(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


class WeWorkRemotelyAdapter:
    name = "weworkremotely"
    display_name = "We Work Remotely"
    endpoint = "https://weworkremotely.com/remote-jobs.rss"

    def fetch(self, fetcher: Fetcher = default_fetcher) -> SourceResult:
        result = SourceResult(source=self.name, requests_attempted=1, pages=1)
        payload = fetcher(self.endpoint, headers={"Accept": "application/rss+xml,application/xml,text/xml"})
        if payload.status_code != 200:
            result.success = False
            result.errors.append(f"HTTP {payload.status_code or 'error'}: {payload.error or payload.text[:200]}")
            return result
        result.requests_succeeded = 1
        try:
            root = ElementTree.fromstring(payload.text)
        except ElementTree.ParseError as exc:
            result.success = False
            result.errors.append(f"invalid_xml:{exc}")
            return result
        items = [node for node in root.iter() if _xml_local_name(node.tag) == "item"]
        result.raw_records = len(items)
        for item in items[: max(1, config.FREE_SOURCE_MAX_RECORDS_PER_SOURCE)]:
            title = _xml_child_text(item, "title")
            company = _xml_child_text(item, "company", "creator", "author")
            role_title = title
            if not company and ":" in title:
                left, right = title.split(":", 1)
                if 1 <= len(left.split()) <= 10 and len(right.strip()) >= 4:
                    company, role_title = left.strip(), right.strip()
            description = _xml_child_text(item, "description", "encoded", "summary", "content")
            link = _xml_child_text(item, "link", "guid")
            location = _xml_child_text(item, "region", "location") or "Remote"
            employment = _xml_child_text(item, "type", "jobtype", "commitment")
            result.jobs.append(_canonical_job(
                source=self.name,
                source_name=self.display_name,
                source_home="https://weworkremotely.com",
                source_id=_xml_child_text(item, "guid") or link,
                title=role_title,
                company=company,
                description=description,
                url=link,
                location=location,
                country=_country_code_from_text(location),
                employment_type=employment or ("Full Time" if re.search(r"\bfull[- ]time\b", description, re.I) else ""),
                posted_at=_xml_child_text(item, "pubdate", "published", "date"),
                tags=[_xml_child_text(item, "category")],
            ))
        return result


ADAPTERS = {
    "himalayas": HimalayasAdapter,
    "jobicy": JobicyAdapter,
    "remotive": RemotiveAdapter,
    "remoteok": RemoteOkAdapter,
    "weworkremotely": WeWorkRemotelyAdapter,
}


def build_adapters(names: Iterable[str]) -> List[Any]:
    adapters = []
    for raw in names:
        name = str(raw or "").strip().lower()
        factory = ADAPTERS.get(name)
        if not factory:
            raise ValueError(f"Unsupported free job source: {raw}")
        adapters.append(factory())
    return adapters


def provider_domain(source: str) -> str:
    mapping = {
        "himalayas": "himalayas.app",
        "jobicy": "jobicy.com",
        "remotive": "remotive.com",
        "remoteok": "remoteok.com",
        "weworkremotely": "weworkremotely.com",
    }
    return mapping.get(str(source or "").lower(), urlparse(str(source or "")).hostname or "")
