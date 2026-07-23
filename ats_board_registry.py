"""Persistent auto-discovered ATS board registry and direct board acquisition.

The registry is populated from URLs already present in source jobs and historical
raw artifacts. It never requires a user-maintained company list. Supported
public boards: Greenhouse, Lever (global/EU), Ashby, Recruitee, Workable, and Personio.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree

import config
from company_identity import company_names_compatible
from domain_utils import normalize_company_domain
from free_job_sources import FetchPayload, Fetcher, default_fetcher, html_to_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BoardRef:
    provider: str
    identifier: str
    api_base: str
    board_url: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.identifier}:{self.api_base}"


def _host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def detect_board_ref(url: str) -> Optional[BoardRef]:
    raw = str(url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and parts:
        token = parts[0]
        return BoardRef("greenhouse", token, "https://boards-api.greenhouse.io", f"https://boards.greenhouse.io/{token}")
    if host == "boards-api.greenhouse.io":
        try:
            index = parts.index("boards")
            token = parts[index + 1]
        except (ValueError, IndexError):
            return None
        return BoardRef("greenhouse", token, "https://boards-api.greenhouse.io", f"https://boards.greenhouse.io/{token}")

    lever_hosts = {
        "jobs.lever.co": "https://api.lever.co",
        "api.lever.co": "https://api.lever.co",
        "jobs.eu.lever.co": "https://api.eu.lever.co",
        "api.eu.lever.co": "https://api.eu.lever.co",
    }
    if host in lever_hosts:
        if host.startswith("jobs.") and parts:
            site = parts[0]
        else:
            try:
                site = parts[parts.index("postings") + 1]
            except (ValueError, IndexError):
                return None
        board_host = "jobs.eu.lever.co" if ".eu." in host else "jobs.lever.co"
        return BoardRef("lever", site, lever_hosts[host], f"https://{board_host}/{site}")

    if host == "jobs.ashbyhq.com" and parts:
        board = parts[0]
        return BoardRef("ashby", board, "https://api.ashbyhq.com", f"https://jobs.ashbyhq.com/{board}")
    if host == "api.ashbyhq.com":
        try:
            board = parts[parts.index("job-board") + 1]
        except (ValueError, IndexError):
            return None
        return BoardRef("ashby", board, "https://api.ashbyhq.com", f"https://jobs.ashbyhq.com/{board}")

    match = re.fullmatch(r"([a-z0-9-]+)\.recruitee\.com", host)
    if match:
        slug = match.group(1)
        return BoardRef("recruitee", slug, f"https://{slug}.recruitee.com", f"https://{slug}.recruitee.com")

    if host == "apply.workable.com" and parts:
        slug = parts[0]
        if slug not in {"j", "jobs"}:
            return BoardRef("workable", slug, "https://www.workable.com", f"https://apply.workable.com/{slug}")
    match = re.fullmatch(r"([a-z0-9-]+)\.workable\.com", host)
    if match and match.group(1) not in {"www", "api", "apply"}:
        slug = match.group(1)
        return BoardRef("workable", slug, "https://www.workable.com", f"https://apply.workable.com/{slug}")
    if host == "workable.com":
        try:
            slug = parts[parts.index("accounts") + 1]
        except (ValueError, IndexError):
            slug = ""
        if slug:
            return BoardRef("workable", slug, "https://www.workable.com", f"https://apply.workable.com/{slug}")

    match = re.fullmatch(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", host)
    if match:
        slug = match.group(1)
        suffix = "com" if host.endswith(".com") else "de"
        base = f"https://{slug}.jobs.personio.{suffix}"
        return BoardRef("personio", slug, base, base)
    return None


def _candidate_urls(job: Mapping[str, Any]) -> Iterable[str]:
    for key in (
        "official_job_url", "canonical_source_url", "job_apply_link",
        "job_google_link", "employer_website",
    ):
        value = str(job.get(key) or "").strip()
        if value:
            yield value
    for option in job.get("apply_options") or []:
        if isinstance(option, Mapping):
            value = str(option.get("apply_link") or "").strip()
            if value:
                yield value
    description = str(job.get("job_description") or "")[:100_000]
    for match in re.finditer(r"https?://[^\s<>\"')]+", html.unescape(description), re.I):
        yield match.group(0).rstrip(".,;:")


def discover_board_refs(job: Mapping[str, Any]) -> List[BoardRef]:
    output: List[BoardRef] = []
    seen: set[str] = set()
    for url in _candidate_urls(job):
        ref = detect_board_ref(url)
        if ref and ref.key not in seen:
            seen.add(ref.key)
            output.append(ref)
    return output


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        temp = handle.name
    os.replace(temp, path)


class AtsBoardRegistry:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or config.ATS_BOARD_REGISTRY_FILE)
        self.entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = self.path.with_suffix(self.path.suffix + f".corrupt.{datetime.now():%Y%m%d%H%M%S}")
            os.replace(self.path, backup)
            return
        entries = payload.get("boards", {}) if isinstance(payload, dict) else {}
        if isinstance(entries, dict):
            self.entries = {str(key): dict(value) for key, value in entries.items() if isinstance(value, dict)}

    def save(self) -> None:
        _atomic_write(self.path, {"updated_at": _now_iso(), "boards": self.entries})

    def upsert_from_job(self, job: Mapping[str, Any]) -> int:
        refs = discover_board_refs(job)
        company = str(job.get("employer_name") or "").strip()
        website = str(job.get("employer_website") or "").strip()
        domain = normalize_company_domain(website)
        source = str(job.get("_acquisition_source") or job.get("job_publisher") or "")
        changed = 0
        for ref in refs:
            existing = self.entries.get(ref.key, {})
            existing_name = str(existing.get("company_name") or "").strip()
            existing_domain = normalize_company_domain(existing.get("company_domain") or "")
            existing_confidence = int(existing.get("identity_confidence", 0) or 0)
            if existing_confidence <= 0 and existing:
                existing_confidence = 3 if str(existing.get("discovered_from_source") or "").startswith("ats_") else 1

            incoming_confidence = 1
            if source.startswith("ats_") and job.get("_ats_board_identity_verified") is True:
                incoming_confidence = 3
            elif job.get("job_apply_is_direct") is True:
                direct_ref = detect_board_ref(str(job.get("job_apply_link") or ""))
                if direct_ref and direct_ref.key == ref.key:
                    incoming_confidence = 2
            if incoming_confidence < 2:
                for option in job.get("apply_options") or []:
                    if not isinstance(option, Mapping) or option.get("is_direct") is not True:
                        continue
                    direct_ref = detect_board_ref(str(option.get("apply_link") or ""))
                    if direct_ref and direct_ref.key == ref.key:
                        incoming_confidence = 2
                        break

            chosen_name = existing_name
            chosen_domain = existing_domain
            identity_conflict = False
            if company:
                compatible = not existing_name or company_names_compatible(existing_name, company)
                if compatible and (not existing_name or incoming_confidence > existing_confidence):
                    chosen_name = company
                elif not compatible:
                    identity_conflict = True
                    if incoming_confidence > existing_confidence:
                        chosen_name = company
            if domain:
                compatible_domain = not existing_domain or existing_domain == domain
                if compatible_domain and (not existing_domain or incoming_confidence > existing_confidence):
                    chosen_domain = domain
                elif not compatible_domain:
                    identity_conflict = True
                    if incoming_confidence > existing_confidence:
                        chosen_domain = domain

            chosen_confidence = max(existing_confidence, incoming_confidence)
            if incoming_confidence > existing_confidence and (company or domain):
                chosen_confidence = incoming_confidence
            item = {
                **existing,
                **asdict(ref),
                "key": ref.key,
                "company_name": chosen_name,
                "company_domain": chosen_domain,
                "identity_confidence": chosen_confidence,
                "discovered_from_source": source or existing.get("discovered_from_source", ""),
                "first_seen_at": existing.get("first_seen_at") or _now_iso(),
                "last_seen_at": _now_iso(),
                "last_checked_at": existing.get("last_checked_at", ""),
                "last_success_at": existing.get("last_success_at", ""),
                "last_job_count": int(existing.get("last_job_count", 0) or 0),
                "consecutive_failures": int(existing.get("consecutive_failures", 0) or 0),
                "last_error": existing.get("last_error", ""),
            }
            if identity_conflict:
                item["identity_conflicts"] = int(existing.get("identity_conflicts", 0) or 0) + 1
                item["last_conflicting_company_name"] = company
                item["last_conflicting_company_domain"] = domain
                item["last_identity_conflict_at"] = _now_iso()
            if existing != item:
                self.entries[ref.key] = item
                changed += 1
        return changed

    def upsert_from_jobs(self, jobs: Iterable[Mapping[str, Any]], *, save: bool = True) -> int:
        changed = sum(self.upsert_from_job(job) for job in jobs)
        if changed and save:
            self.save()
        return changed

    def seed_from_history(self, roots: Optional[Iterable[Path]] = None) -> Dict[str, int]:
        roots = list(roots or [Path(config.OUTPUT_DIR), Path(config.FILTERED_OUTPUT_DIR), Path(config.STEP3_OUTPUT_DIR)])
        candidates: List[Path] = []
        for root in roots:
            if not root.exists():
                continue
            candidates.extend(path for path in root.rglob("*.json") if path.is_file())
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        files_scanned = 0
        jobs_scanned = 0
        changed = 0
        for path in candidates[: max(0, config.ATS_REGISTRY_HISTORY_FILE_LIMIT)]:
            if path.stat().st_size > config.ATS_REGISTRY_MAX_HISTORY_FILE_BYTES:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rows = payload.get("jobs", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                continue
            files_scanned += 1
            for job in rows:
                if not isinstance(job, dict):
                    continue
                jobs_scanned += 1
                changed += self.upsert_from_job(job)
        if changed:
            self.save()
        return {"files_scanned": files_scanned, "jobs_scanned": jobs_scanned, "boards_added_or_updated": changed}

    def due_entries(
        self,
        limit: Optional[int] = None,
        *,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, config.ATS_BOARD_REFRESH_INTERVAL_HOURS))
        due = []
        for item in self.entries.values():
            company = str(item.get("company_name") or "").strip()
            if not company:
                continue
            checked = _parse_iso(item.get("last_checked_at"))
            if force or checked is None or checked <= cutoff:
                due.append(dict(item))
        due.sort(key=lambda item: (
            _parse_iso(item.get("last_checked_at")) or datetime.min.replace(tzinfo=timezone.utc),
            -int(item.get("last_job_count", 0) or 0),
        ))
        return due[: max(1, limit or config.ATS_MAX_BOARDS_PER_RUN)]

    def record_result(
        self,
        key: str,
        *,
        success: bool,
        job_count: int = 0,
        error: str = "",
        save: bool = True,
    ) -> None:
        item = self.entries.get(key)
        if not item:
            return
        item["last_checked_at"] = _now_iso()
        item["last_job_count"] = int(job_count)
        if success:
            item["last_success_at"] = _now_iso()
            item["consecutive_failures"] = 0
            item["last_error"] = ""
        else:
            item["consecutive_failures"] = int(item.get("consecutive_failures", 0) or 0) + 1
            item["last_error"] = str(error or "")[:1000]
        if save:
            self.save()


def _board_identity_verified(company_name: Any, identifier: Any) -> bool:
    company = str(company_name or "").strip()
    board_name = re.sub(r"[-_]+", " ", str(identifier or "")).strip()
    if len(re.sub(r"[^a-z0-9]+", "", company.lower())) < 4:
        return False
    if len(re.sub(r"[^a-z0-9]+", "", board_name.lower())) < 4:
        return False
    # Use the repository's conservative organization-name matcher rather than
    # substring containment. This accepts legal/generic suffix variants such as
    # ``Acme`` vs ``acme-inc`` while rejecting collisions such as
    # ``Meta`` vs ``metabase``.
    return company_names_compatible(company, board_name)


def _timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return str(value)


def _direct_job(
    *,
    provider: str,
    board: Mapping[str, Any],
    job_id: Any,
    title: Any,
    description: Any,
    url: Any,
    location: Any = "",
    employment_type: Any = "",
    posted_at: Any = "",
    workplace_type: Any = "",
    expires_at: Any = "",
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    company = str(board.get("company_name") or "").strip()
    domain = str(board.get("company_domain") or "").strip()
    job_url = str(url or "").strip()
    location_text = re.sub(r"\s+", " ", str(location or "")).strip()
    workplace = str(workplace_type or "").strip().lower()
    description_text = html_to_text(description)
    remote = workplace == "remote" or bool(re.search(r"\bremote\b", f"{location_text}\n{description_text[:3000]}", re.I))
    country = "US" if re.search(r"\b(?:united states|usa|u\.s\.|remote us|us only)\b", f"{location_text}\n{description_text[:4000]}", re.I) else ""
    job = {
        "job_id": f"ats:{provider}:{board.get('identifier')}:{job_id}",
        "job_title": re.sub(r"\s+", " ", str(title or "")).strip(),
        "employer_name": company,
        "employer_website": f"https://{domain}" if domain else "",
        "job_publisher": provider.title(),
        "job_description": description_text,
        "job_apply_link": job_url,
        "job_apply_is_direct": True,
        "official_job_url": job_url,
        "canonical_source_url": job_url,
        "apply_options": ([{"publisher": provider.title(), "apply_link": job_url, "is_direct": True}] if job_url else []),
        "job_location": location_text or ("Remote" if remote else ""),
        "job_country": country,
        "job_is_remote": remote,
        "job_employment_type": str(employment_type or "").strip(),
        "job_posted_at_datetime_utc": _timestamp(posted_at),
        "job_offer_expiration_datetime_utc": _timestamp(expires_at),
        "_acquisition_source": f"ats_{provider}",
        "_ats_provider": provider,
        "_ats_board_identifier": str(board.get("identifier") or ""),
        "_provider_record_structured": True,
        "_ats_board_identity_verified": _board_identity_verified(
            company, board.get("identifier")
        ),
    }
    if extra:
        job.update(dict(extra))
    return job


def _fetch_json(fetcher: Fetcher, url: str, **kwargs: Any) -> Tuple[Optional[Any], str]:
    payload = fetcher(url, **kwargs)
    if payload.status_code != 200:
        return None, f"HTTP {payload.status_code or 'error'}: {payload.error or payload.text[:200]}"
    try:
        return json.loads(payload.text), ""
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"invalid_json:{exc}"


def _xml_text(node: ElementTree.Element, name: str) -> str:
    for child in list(node):
        if child.tag.rsplit("}", 1)[-1].lower() == name.lower():
            return "".join(child.itertext()).strip()
    return ""


def _personio_description(position: ElementTree.Element) -> str:
    parts: List[str] = []
    for node in position.iter():
        if node.tag.rsplit("}", 1)[-1].lower() != "jobdescription":
            continue
        heading = _xml_text(node, "name")
        value = _xml_text(node, "value")
        if heading or value:
            parts.append("\n".join(item for item in (heading, value) if item))
    return html_to_text("\n\n".join(parts))


def _greenhouse_title_may_match(title: Any) -> bool:
    from role_catalog import DEFAULT_SEARCH_ROLES
    from role_relevance import assess_role

    job = {"job_title": str(title or ""), "job_description": ""}
    return any(
        assess_role(job, role).status in {"accept", "review"}
        for role in DEFAULT_SEARCH_ROLES
    )


def fetch_board_jobs(
    board: Mapping[str, Any],
    fetcher: Fetcher = default_fetcher,
    *,
    greenhouse_detail_budget: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    provider = str(board.get("provider") or "")
    identifier = str(board.get("identifier") or "")
    if not provider or not identifier or not str(board.get("company_name") or "").strip():
        return [], "missing_board_identity"
    max_jobs = max(1, config.ATS_MAX_JOBS_PER_BOARD)

    if provider == "greenhouse":
        board_endpoint = f"https://boards-api.greenhouse.io/v1/boards/{identifier}"
        board_data, board_error = _fetch_json(fetcher, board_endpoint)
        if not board_error and isinstance(board_data, dict):
            official_name = str(board_data.get("name") or "").strip()
            if official_name:
                board = {**dict(board), "company_name": official_name}

        endpoint = f"https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs"
        data, error = _fetch_json(fetcher, endpoint, params={"content": "true"})
        rows = data.get("jobs", []) if isinstance(data, dict) else []
        if error:
            return [], error
        output = []
        per_board_budget = max(0, int(getattr(config, "ATS_GREENHOUSE_DETAIL_MAX_REQUESTS_PER_BOARD", 25)))
        if greenhouse_detail_budget is None:
            detail_budget = per_board_budget
        else:
            detail_budget = min(per_board_budget, max(0, int(greenhouse_detail_budget)))
        detail_calls = 0
        for row in rows[:max_jobs] if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            location = row.get("location") or {}
            title = row.get("title")
            posted_at = ""
            expires_at = ""
            detail_error = ""
            detail_requested = False
            if detail_calls < detail_budget and _greenhouse_title_may_match(title):
                detail_calls += 1
                detail_requested = True
                detail, detail_error = _fetch_json(
                    fetcher,
                    f"https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs/{row.get('id')}",
                )
                if isinstance(detail, dict):
                    posted_at = detail.get("first_published") or ""
                    expires_at = detail.get("application_deadline") or ""
            output.append(_direct_job(
                provider=provider, board=board, job_id=row.get("id"), title=title,
                description=row.get("content"), url=row.get("absolute_url") or f"https://boards.greenhouse.io/{identifier}/jobs/{row.get('id')}",
                location=location.get("name") if isinstance(location, dict) else location,
                employment_type="", posted_at=posted_at, expires_at=expires_at,
                extra={
                    "_ats_source_updated_at": _timestamp(row.get("updated_at")),
                    "_greenhouse_detail_request_made": detail_requested,
                    "_greenhouse_detail_checked": bool(detail_requested),
                    "_greenhouse_first_published_verified": bool(posted_at),
                    "_greenhouse_detail_error": detail_error,
                },
            ))
        return output, ""

    if provider == "lever":
        api_base = str(board.get("api_base") or "https://api.lever.co").rstrip("/")
        endpoint = f"{api_base}/v0/postings/{identifier}"
        data, error = _fetch_json(fetcher, endpoint, params={"mode": "json", "limit": max_jobs})
        if error:
            return [], error
        rows = data if isinstance(data, list) else []
        output = []
        for row in rows[:max_jobs]:
            if not isinstance(row, dict):
                continue
            categories = row.get("categories") or {}
            description = row.get("descriptionPlain") or row.get("description") or ""
            if isinstance(row.get("lists"), list):
                description = "\n".join([description, *[html_to_text(item.get("content")) for item in row["lists"] if isinstance(item, dict)]])
            output.append(_direct_job(
                provider=provider, board=board, job_id=row.get("id"), title=row.get("text") or row.get("title"),
                description=description, url=row.get("hostedUrl") or row.get("applyUrl"),
                location=categories.get("location") if isinstance(categories, dict) else "",
                employment_type=categories.get("commitment") if isinstance(categories, dict) else "",
                posted_at=row.get("createdAt"), workplace_type=row.get("workplaceType"),
            ))
        return output, ""

    if provider == "ashby":
        endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{identifier}"
        data, error = _fetch_json(fetcher, endpoint, params={"includeCompensation": "false"})
        if error:
            return [], error
        rows = data.get("jobs", []) if isinstance(data, dict) else []
        output = []
        for row in rows[:max_jobs] if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("isListed") is False:
                continue
            secondary = row.get("secondaryLocations") or []
            secondary_location = ""
            if isinstance(secondary, list) and secondary and isinstance(secondary[0], dict):
                secondary_location = str(secondary[0].get("location") or "")
            workplace_type = "Remote" if row.get("isRemote") is True else row.get("workplaceType")
            output.append(_direct_job(
                provider=provider, board=board, job_id=row.get("id") or row.get("jobUrl"), title=row.get("title"),
                description=row.get("descriptionPlain") or row.get("descriptionHtml"), url=row.get("jobUrl") or row.get("applyUrl"),
                location=row.get("location") or secondary_location, employment_type=row.get("employmentType"),
                posted_at=row.get("publishedAt") or row.get("updatedAt"), workplace_type=workplace_type,
            ))
        return output, ""

    if provider == "recruitee":
        endpoint = f"https://{identifier}.recruitee.com/api/offers/"
        data, error = _fetch_json(fetcher, endpoint)
        if error:
            return [], error
        rows = data.get("offers", data.get("jobs", [])) if isinstance(data, dict) else data if isinstance(data, list) else []
        output = []
        for row in rows[:max_jobs] if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or row.get("state") or "published").lower()
            if status in {"closed", "archived", "draft", "unpublished"}:
                continue
            location = row.get("location") or row.get("location_name") or row.get("city") or ""
            if isinstance(location, dict):
                location = ", ".join(str(location.get(key) or "") for key in ("city", "state", "country") if location.get(key))
            output.append(_direct_job(
                provider=provider, board=board, job_id=row.get("id") or row.get("slug"), title=row.get("title") or row.get("name"),
                description=row.get("description") or row.get("description_html") or row.get("description_plain"),
                url=row.get("careers_url") or row.get("url") or f"https://{identifier}.recruitee.com/o/{row.get('slug')}",
                location=location, employment_type=row.get("employment_type") or row.get("type"),
                posted_at=row.get("published_at") or row.get("created_at") or row.get("updated_at"),
                workplace_type=row.get("remote") and "remote" or row.get("workplace_type"),
            ))
        return output, ""

    if provider == "workable":
        endpoint = f"https://www.workable.com/api/accounts/{identifier}"
        data, error = _fetch_json(fetcher, endpoint, params={"details": "true"})
        if error:
            return [], error
        rows = data.get("jobs", []) if isinstance(data, dict) else []
        company_name = str(data.get("name") or "").strip() if isinstance(data, dict) else ""
        if company_name and not str(board.get("company_name") or "").strip():
            board = {**dict(board), "company_name": company_name}
        output = []
        for row in rows[:max_jobs] if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            location = ", ".join(
                str(row.get(key) or "").strip()
                for key in ("city", "state", "country")
                if str(row.get(key) or "").strip()
            )
            output.append(_direct_job(
                provider=provider, board=board, job_id=row.get("shortcode") or row.get("code") or row.get("id"),
                title=row.get("title"), description=row.get("description") or row.get("description_html") or row.get("requirements"),
                url=row.get("url") or row.get("shortlink") or f"https://apply.workable.com/{identifier}/j/{row.get('shortcode')}",
                location=location, employment_type=row.get("employment_type") or row.get("type"),
                posted_at=row.get("published_on") or row.get("created_at") or row.get("updated_at"),
                workplace_type="remote" if row.get("telecommuting") is True else row.get("workplace_type"),
            ))
        return output, ""

    if provider == "personio":
        api_base = str(board.get("api_base") or f"https://{identifier}.jobs.personio.de").rstrip("/")
        payload = fetcher(f"{api_base}/xml", params={"language": "en"}, headers={"Accept": "application/xml,text/xml"})
        if payload.status_code != 200:
            return [], f"HTTP {payload.status_code or 'error'}: {payload.error or payload.text[:200]}"
        try:
            root = ElementTree.fromstring(payload.text)
        except ElementTree.ParseError as exc:
            return [], f"invalid_xml:{exc}"
        positions = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "position"]
        output = []
        for position in positions[:max_jobs]:
            job_id = _xml_text(position, "id")
            title = _xml_text(position, "name")
            office = _xml_text(position, "office")
            description = _personio_description(position)
            output.append(_direct_job(
                provider=provider, board=board, job_id=job_id, title=title, description=description,
                url=f"{api_base}/job/{job_id}" if job_id else api_base,
                location=office, employment_type=_xml_text(position, "employmentType"),
                posted_at=_xml_text(position, "createdAt") or _xml_text(position, "publishedAt"),
                workplace_type="remote" if re.search(r"\bremote\b", f"{office}\n{description}", re.I) else "",
            ))
        return output, ""

    return [], f"unsupported_provider:{provider}"
