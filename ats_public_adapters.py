"""Bounded public ATS adapters used by :mod:`job_source_resolver`.

The adapter layer has one responsibility: turn an employer-discovered ATS board
or direct ATS URL into one or more exact posting URLs.  It does not decide
whether the employer or job is valid; the resolver keeps that authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse


FetchPayload = Callable[..., Dict[str, Any]]


@dataclass
class AtsDiscoveryResult:
    urls: List[str] = field(default_factory=list)
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    records: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    authoritative_absence: bool = False


def _tokens(value: str) -> set[str]:
    stop = {
        "remote", "job", "jobs", "the", "and", "for", "at", "in", "usa",
        "us", "united", "states", "hiring", "now", "full", "time",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 1 and token not in stop
    }


def _title_score(expected: str, actual: str) -> float:
    left = _tokens(expected)
    right = _tokens(actual)
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    # The shorter title often omits location or department suffixes.  Score
    # against the smaller token set, then mildly reward symmetric overlap.
    containment = intersection / max(1, min(len(left), len(right)))
    jaccard = intersection / max(1, len(left | right))
    return min(1.0, containment * 0.8 + jaccard * 0.2)


def _fetch_payload(
    fetch: FetchPayload,
    url: str,
    *,
    method: str = "GET",
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call new or legacy fetch callbacks without breaking existing tests."""
    try:
        return fetch(url, method=method, json_body=json_body)
    except TypeError:
        if method != "GET" or json_body is not None:
            return {
                "status_code": None,
                "final_url": url,
                "text": "",
                "error": "fetch_callback_does_not_support_method",
            }
        return fetch(url)


def _json(payload: Dict[str, Any]) -> Any:
    try:
        return json.loads(str(payload.get("text") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _greenhouse_ref(url: str) -> Optional[Tuple[str, str]]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [part for part in parsed.path.split("/") if part]
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        if not parts:
            return None
        token = parts[0]
        job_id = parts[2] if len(parts) >= 3 and parts[1] == "jobs" else ""
        return token, job_id
    if host == "boards-api.greenhouse.io":
        try:
            index = parts.index("boards")
            token = parts[index + 1]
        except (ValueError, IndexError):
            return None
        job_id = parts[index + 3] if len(parts) > index + 3 and parts[index + 2] == "jobs" else ""
        return token, job_id
    return None


def _lever_ref(url: str) -> Optional[Tuple[str, str]]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [part for part in parsed.path.split("/") if part]
    if host == "jobs.lever.co" and parts:
        return parts[0], parts[1] if len(parts) > 1 else ""
    if host == "api.lever.co":
        try:
            index = parts.index("postings")
            site = parts[index + 1]
        except (ValueError, IndexError):
            return None
        return site, parts[index + 2] if len(parts) > index + 2 else ""
    return None


def _ashby_ref(url: str) -> Optional[Tuple[str, str]]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [part for part in parsed.path.split("/") if part]
    if host == "jobs.ashbyhq.com" and parts:
        return parts[0], parts[1] if len(parts) > 1 else ""
    if host == "api.ashbyhq.com":
        try:
            index = parts.index("job-board")
            board = parts[index + 1]
        except (ValueError, IndexError):
            return None
        return board, ""
    return None


def _workday_ref(url: str) -> Optional[Dict[str, str]]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    match = re.match(r"(?P<tenant>[^.]+)\.wd\d+\.myworkdayjobs\.com$", host)
    if not match:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    if not parts:
        return None
    site = parts[0]
    job_index = parts.index("job") if "job" in parts else -1
    posting_path = "/".join(parts[job_index + 1 :]) if job_index >= 0 else ""
    return {
        "host": host,
        "tenant": match.group("tenant"),
        "site": site,
        "posting_path": posting_path,
        "original_url": url,
    }


def _attempt(url: str, provider: str, status: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "url": url,
        "provider": provider,
        "status": status,
        "http_status": payload.get("status_code"),
        "final_url": payload.get("final_url") or url,
        "error": payload.get("error") or "",
    }


def _rank_urls(rows: Iterable[Tuple[float, str]]) -> List[str]:
    output: List[str] = []
    for score, url in sorted(rows, key=lambda item: item[0], reverse=True):
        if score < 0.55 or not url or url in output:
            continue
        output.append(url)
        if len(output) >= 10:
            break
    return output


def _greenhouse(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    ref = _greenhouse_ref(url)
    if not ref:
        return AtsDiscoveryResult()
    token, job_id = ref
    endpoint = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    endpoint = f"{endpoint}/{job_id}" if job_id else f"{endpoint}?content=true"
    payload = _fetch_payload(fetch, endpoint)
    status = payload.get("status_code")
    result = AtsDiscoveryResult(attempts=[_attempt(endpoint, "greenhouse", "api_unavailable" if not status or status >= 400 else "api_ok", payload)])
    if job_id and status in {404, 410}:
        result.authoritative_absence = True
    if not status or status >= 400:
        return result
    data = _json(payload)
    jobs = [data] if isinstance(data, dict) and data.get("id") else (data or {}).get("jobs", []) if isinstance(data, dict) else []
    ranked: List[Tuple[float, str]] = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        score = _title_score(title, str(job.get("title") or ""))
        if job_id and str(job.get("id") or "") == job_id:
            score += 2.0
        candidate = str(job.get("absolute_url") or "")
        if not candidate and job.get("id"):
            candidate = f"https://boards.greenhouse.io/{token}/jobs/{job['id']}"
        ranked.append((score, candidate))
        if candidate:
            result.records[candidate] = {
                "title": str(job.get("title") or ""),
                "description": str(job.get("content") or ""),
                "location": str(((job.get("location") or {}).get("name") if isinstance(job.get("location"), dict) else job.get("location")) or ""),
                "job_id": str(job.get("id") or ""),
                "active": True,
                "provider": "greenhouse",
            }
    result.urls = _rank_urls(ranked)
    result.authoritative_absence = bool(
        (job_id and status in {404, 410})
        or (status == 200 and not result.urls)
    )
    return result


def _lever(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    ref = _lever_ref(url)
    if not ref:
        return AtsDiscoveryResult()
    site, posting_id = ref
    endpoint = f"https://api.lever.co/v0/postings/{site}"
    endpoint = f"{endpoint}/{posting_id}" if posting_id else f"{endpoint}?mode=json"
    payload = _fetch_payload(fetch, endpoint)
    status = payload.get("status_code")
    result = AtsDiscoveryResult(attempts=[_attempt(endpoint, "lever", "api_unavailable" if not status or status >= 400 else "api_ok", payload)])
    if posting_id and status in {404, 410}:
        result.authoritative_absence = True
    if not status or status >= 400:
        return result
    data = _json(payload)
    jobs = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
    ranked: List[Tuple[float, str]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        score = _title_score(title, str(job.get("text") or job.get("title") or ""))
        candidate_id = str(job.get("id") or "")
        if posting_id and candidate_id == posting_id:
            score += 2.0
        candidate = str(job.get("hostedUrl") or job.get("applyUrl") or "")
        if not candidate and candidate_id:
            candidate = f"https://jobs.lever.co/{site}/{candidate_id}"
        ranked.append((score, candidate))
        if candidate:
            categories = job.get("categories") or {}
            result.records[candidate] = {
                "title": str(job.get("text") or job.get("title") or ""),
                "description": str(job.get("descriptionPlain") or job.get("description") or ""),
                "location": str(categories.get("location") or "") if isinstance(categories, dict) else "",
                "employment_type": str(categories.get("commitment") or "") if isinstance(categories, dict) else "",
                "job_id": candidate_id,
                "active": True,
                "provider": "lever",
            }
    result.urls = _rank_urls(ranked)
    result.authoritative_absence = bool(
        (posting_id and status in {404, 410})
        or (status == 200 and not result.urls)
    )
    return result


def _ashby(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    ref = _ashby_ref(url)
    if not ref:
        return AtsDiscoveryResult()
    board, posting_hint = ref
    endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false"
    payload = _fetch_payload(fetch, endpoint)
    status = payload.get("status_code")
    result = AtsDiscoveryResult(attempts=[_attempt(endpoint, "ashby", "api_unavailable" if not status or status >= 400 else "api_ok", payload)])
    if not status or status >= 400:
        return result
    data = _json(payload)
    jobs = (data or {}).get("jobs", []) if isinstance(data, dict) else []
    ranked: List[Tuple[float, str]] = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or job.get("isListed") is False:
            continue
        score = _title_score(title, str(job.get("title") or ""))
        candidate = str(job.get("jobUrl") or "")
        if posting_hint and posting_hint.lower() in candidate.lower():
            score += 2.0
        ranked.append((score, candidate))
        if candidate:
            result.records[candidate] = {
                "title": str(job.get("title") or ""),
                "description": str(job.get("descriptionPlain") or job.get("descriptionHtml") or ""),
                "location": str(job.get("location") or ""),
                "employment_type": str(job.get("employmentType") or ""),
                "job_id": str(job.get("id") or ""),
                "active": True,
                "provider": "ashby",
            }
    result.urls = _rank_urls(ranked)
    result.authoritative_absence = bool(status == 200 and not result.urls)
    return result


def _workday(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    ref = _workday_ref(url)
    if not ref:
        return AtsDiscoveryResult()
    base = f"https://{ref['host']}/wday/cxs/{ref['tenant']}/{ref['site']}"
    result = AtsDiscoveryResult()
    ranked: List[Tuple[float, str]] = []
    direct_job_missing = False

    if ref["posting_path"]:
        endpoint = f"{base}/job/{quote(ref['posting_path'], safe='/_-')}"
        payload = _fetch_payload(fetch, endpoint)
        status = payload.get("status_code")
        result.attempts.append(_attempt(endpoint, "workday", "job_api_unavailable" if not status or status >= 400 else "job_api_ok", payload))
        if status in {404, 410}:
            direct_job_missing = True
        data = _json(payload)
        info = (data or {}).get("jobPostingInfo") if isinstance(data, dict) else None
        if isinstance(info, dict):
            actual = str(info.get("title") or info.get("jobTitle") or "")
            external = str(info.get("externalUrl") or info.get("externalPath") or "")
            candidate = urljoin(f"https://{ref['host']}", external) if external else ref["original_url"]
            ranked.append((_title_score(title, actual) + 1.5, candidate))
            result.records[candidate] = {
                "title": actual,
                "description": str(info.get("jobDescription") or ""),
                "location": str(info.get("location") or info.get("primaryLocation") or ""),
                "employment_type": str(info.get("timeType") or info.get("workerType") or ""),
                "date_posted": str(info.get("startDate") or info.get("postedOn") or ""),
                "job_id": str(info.get("jobReqId") or ""),
                "active": True,
                "provider": "workday",
            }

    # Board/search support. Workday's CXS endpoint is a stable JSON surface even
    # when the public board is fully client rendered.
    endpoint = f"{base}/jobs"
    payload = _fetch_payload(
        fetch,
        endpoint,
        method="POST",
        json_body={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": title},
    )
    status = payload.get("status_code")
    result.attempts.append(_attempt(endpoint, "workday", "search_api_unavailable" if not status or status >= 400 else "search_api_ok", payload))
    data = _json(payload)
    postings = (data or {}).get("jobPostings", []) if isinstance(data, dict) else []
    for item in postings if isinstance(postings, list) else []:
        if not isinstance(item, dict):
            continue
        actual = str(item.get("title") or "")
        external = str(item.get("externalPath") or item.get("externalUrl") or "")
        candidate = urljoin(f"https://{ref['host']}", external)
        ranked.append((_title_score(title, actual), candidate))
        result.records[candidate] = {
            "title": actual,
            "description": "",
            "location": str(item.get("locationsText") or item.get("location") or ""),
            "date_posted": str(item.get("postedOn") or ""),
            "job_id": str(item.get("bulletFields", [""])[0] if isinstance(item.get("bulletFields"), list) and item.get("bulletFields") else ""),
            "active": True,
            "provider": "workday",
        }
    result.urls = _rank_urls(ranked)
    # Search results are bounded and therefore not authoritative. Only an exact
    # Workday posting endpoint returning 404/410 proves the supplied posting is gone.
    result.authoritative_absence = direct_job_missing
    return result


def _walk_json(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_json(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_json(nested)


def _generic_json(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    """Best-effort adapter for JSON-backed Oracle/ADP/Paylocity/Taleo boards."""
    payload = _fetch_payload(fetch, url)
    status = payload.get("status_code")
    result = AtsDiscoveryResult(attempts=[_attempt(url, "generic", "payload_unavailable" if not status or status >= 400 else "payload_ok", payload)])
    if not status or status >= 400:
        return result
    data = _json(payload)
    if data is None:
        return result
    ranked: List[Tuple[float, str]] = []
    for item in _walk_json(data):
        actual = str(item.get("title") or item.get("jobTitle") or item.get("name") or "")
        candidate = str(
            item.get("url")
            or item.get("jobUrl")
            or item.get("externalUrl")
            or item.get("externalPath")
            or item.get("applyUrl")
            or ""
        )
        if not actual or not candidate:
            continue
        absolute = urljoin(url, candidate)
        ranked.append((_title_score(title, actual), absolute))
        result.records[absolute] = {
            "title": actual,
            "description": str(item.get("description") or item.get("jobDescription") or ""),
            "location": str(item.get("location") or item.get("locationName") or ""),
            "employment_type": str(item.get("employmentType") or item.get("jobType") or ""),
            "job_id": str(item.get("id") or item.get("jobId") or item.get("requisitionId") or ""),
            "active": item.get("active", True) is not False,
            "provider": "generic",
        }
    result.urls = _rank_urls(ranked)
    return result


def discover_public_ats_urls(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    """Return exact posting URLs from supported public ATS surfaces."""
    if _greenhouse_ref(url):
        return _greenhouse(url, title, fetch)
    if _lever_ref(url):
        return _lever(url, title, fetch)
    if _ashby_ref(url):
        return _ashby(url, title, fetch)
    if _workday_ref(url):
        return _workday(url, title, fetch)
    return _generic_json(url, title, fetch)
