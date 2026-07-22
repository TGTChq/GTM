"""Small, bounded adapters for public ATS job-board endpoints.

The adapters improve recall for client-rendered Greenhouse and Lever boards.
They never establish employer identity by themselves: callers must preserve the
fact that the board URL was discovered from the employer's own website or
otherwise corroborate the hiring organization.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse


FetchPayload = Callable[[str], Dict[str, Any]]


@dataclass
class AtsDiscoveryResult:
    urls: List[str] = field(default_factory=list)
    attempts: List[Dict[str, Any]] = field(default_factory=list)


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 1 and token not in {"remote", "job", "jobs", "the", "and", "for"}
    }


def _title_score(expected: str, actual: str) -> float:
    left = _tokens(expected)
    right = _tokens(actual)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


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
        job_id = ""
        if len(parts) >= 3 and parts[1] == "jobs":
            job_id = parts[2]
        return token, job_id
    if host == "boards-api.greenhouse.io":
        try:
            board_index = parts.index("boards")
            token = parts[board_index + 1]
        except (ValueError, IndexError):
            return None
        job_id = ""
        if len(parts) > board_index + 3 and parts[board_index + 2] == "jobs":
            job_id = parts[board_index + 3]
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

def _attempt(url: str, provider: str, status: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "url": url,
        "provider": provider,
        "status": status,
        "http_status": payload.get("status_code"),
        "final_url": payload.get("final_url") or url,
        "error": payload.get("error") or "",
    }


def _greenhouse(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    ref = _greenhouse_ref(url)
    if not ref:
        return AtsDiscoveryResult()
    token, job_id = ref
    endpoint = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    if job_id:
        endpoint = f"{endpoint}/{job_id}"
    else:
        endpoint = f"{endpoint}?content=true"
    payload = fetch(endpoint)
    status = payload.get("status_code")
    result = AtsDiscoveryResult(attempts=[_attempt(endpoint, "greenhouse", "api_unavailable" if not status or status >= 400 else "api_ok", payload)])
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
        absolute_url = str(job.get("absolute_url") or "")
        if not absolute_url and job.get("id"):
            absolute_url = f"https://boards.greenhouse.io/{token}/jobs/{job['id']}"
        if absolute_url and score >= 0.55:
            ranked.append((score, absolute_url))
    result.urls = [candidate for _score, candidate in sorted(ranked, reverse=True)[:10]]
    return result


def _lever(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    ref = _lever_ref(url)
    if not ref:
        return AtsDiscoveryResult()
    site, posting_id = ref
    endpoint = f"https://api.lever.co/v0/postings/{site}"
    if posting_id:
        endpoint = f"{endpoint}/{posting_id}"
    else:
        endpoint = f"{endpoint}?mode=json"
    payload = fetch(endpoint)
    status = payload.get("status_code")
    result = AtsDiscoveryResult(attempts=[_attempt(endpoint, "lever", "api_unavailable" if not status or status >= 400 else "api_ok", payload)])
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
        hosted_url = str(job.get("hostedUrl") or job.get("applyUrl") or "")
        if not hosted_url and candidate_id:
            hosted_url = f"https://jobs.lever.co/{site}/{candidate_id}"
        if hosted_url and score >= 0.55:
            ranked.append((score, hosted_url))
    result.urls = [candidate for _score, candidate in sorted(ranked, reverse=True)[:10]]
    return result




def _ashby(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    ref = _ashby_ref(url)
    if not ref:
        return AtsDiscoveryResult()
    board, posting_hint = ref
    endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false"
    payload = fetch(endpoint)
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
        job_url = str(job.get("jobUrl") or "")
        if posting_hint and posting_hint.lower() in job_url.lower():
            score += 2.0
        if job_url and score >= 0.55:
            ranked.append((score, job_url))
    result.urls = [candidate for _score, candidate in sorted(ranked, reverse=True)[:10]]
    return result

def discover_public_ats_urls(url: str, title: str, fetch: FetchPayload) -> AtsDiscoveryResult:
    """Return exact posting URLs from supported public ATS endpoints.

    Unknown ATS providers return an empty result and remain on the generic,
    bounded HTML discovery path.
    """
    if _greenhouse_ref(url):
        return _greenhouse(url, title, fetch)
    if _lever_ref(url):
        return _lever(url, title, fetch)
    if _ashby_ref(url):
        return _ashby(url, title, fetch)
    return AtsDiscoveryResult()
