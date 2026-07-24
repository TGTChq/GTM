"""Persistent auto-discovered ATS board registry and direct board acquisition.

The registry is populated from URLs already present in source jobs and historical
raw artifacts. It never requires a user-maintained company list. Supported
public boards: Greenhouse, Lever (global/EU), Ashby, Recruitee, Workable, Personio, SmartRecruiters, and Workday.
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


_WORKABLE_RESERVED_IDENTIFIERS = {
    "api",
    "apply",
    "blog",
    "careers",
    "help",
    "jobs",
    "resources",
    "support",
    "www",
}


def _valid_workable_identifier(value: Any) -> bool:
    identifier = str(value or "").strip().lower()
    return bool(
        re.fullmatch(r"[a-z0-9][a-z0-9-]{1,99}", identifier)
        and identifier not in _WORKABLE_RESERVED_IDENTIFIERS
    )


def _valid_registry_entry(item: Mapping[str, Any]) -> bool:
    provider = str(item.get("provider") or "").strip().lower()
    if provider == "workable":
        return _valid_workable_identifier(item.get("identifier"))
    return True


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
        if _valid_workable_identifier(slug):
            return BoardRef("workable", slug, "https://www.workable.com", f"https://apply.workable.com/{slug}")
    match = re.fullmatch(r"([a-z0-9-]+)\.workable\.com", host)
    if match and _valid_workable_identifier(match.group(1)):
        slug = match.group(1)
        return BoardRef("workable", slug, "https://www.workable.com", f"https://apply.workable.com/{slug}")
    if host == "workable.com":
        try:
            slug = parts[parts.index("accounts") + 1]
        except (ValueError, IndexError):
            slug = ""
        if _valid_workable_identifier(slug):
            return BoardRef("workable", slug, "https://www.workable.com", f"https://apply.workable.com/{slug}")

    match = re.fullmatch(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", host)
    if match:
        slug = match.group(1)
        suffix = "com" if host.endswith(".com") else "de"
        base = f"https://{slug}.jobs.personio.{suffix}"
        return BoardRef("personio", slug, base, base)

    smartrecruiters_hosts = {
        "careers.smartrecruiters.com",
        "jobs.smartrecruiters.com",
        "smartrecruiters.com",
    }
    if host in smartrecruiters_hosts and parts:
        identifier = parts[0]
        if identifier not in {"external-referrals", "candidate", "jobs"}:
            return BoardRef(
                "smartrecruiters",
                identifier,
                "https://api.smartrecruiters.com/v1",
                f"https://careers.smartrecruiters.com/{identifier}",
            )
    if host == "api.smartrecruiters.com":
        try:
            identifier = parts[parts.index("companies") + 1]
        except (ValueError, IndexError):
            identifier = ""
        if identifier:
            return BoardRef(
                "smartrecruiters",
                identifier,
                "https://api.smartrecruiters.com/v1",
                f"https://careers.smartrecruiters.com/{identifier}",
            )

    workday = re.fullmatch(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", host)
    if workday:
        path_parts = list(parts)
        if path_parts and re.fullmatch(r"[a-z]{2}-[a-z]{2}", path_parts[0], re.I):
            path_parts = path_parts[1:]
        if path_parts:
            tenant = workday.group(1)
            site = path_parts[0]
            identifier = f"{tenant}|{site}"
            base = f"https://{host}"
            return BoardRef("workday", identifier, base, f"{base}/{site}")
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
        self.invalid_entries_pruned = 0
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
            loaded = {
                str(key): dict(value)
                for key, value in entries.items()
                if isinstance(value, dict)
            }
            self.entries = {
                key: value for key, value in loaded.items() if _valid_registry_entry(value)
            }
            self.invalid_entries_pruned = len(loaded) - len(self.entries)
            if self.invalid_entries_pruned:
                logger.warning(
                    "Pruned %d invalid ATS registry entr%s",
                    self.invalid_entries_pruned,
                    "y" if self.invalid_entries_pruned == 1 else "ies",
                )
                self.save()

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
            if not _valid_registry_entry(item):
                continue
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
    raw_identifier = str(identifier or "")
    if "|" in raw_identifier:
        raw_identifier = raw_identifier.split("|", 1)[0]
    board_name = re.sub(r"[-_]+", " ", raw_identifier).strip()
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
    explicit_in_person = workplace in {
        "hybrid", "onsite", "on site", "on-site", "in person", "in-person",
    }
    provider_remote = bool(extra and extra.get("_provider_is_remote") is True)
    # A structured Hybrid/OnSite workplace type is more specific than a broad
    # provider flag indicating that some remote work is possible. Keep the
    # provider flag for auditability, but never let it turn a mandatory office
    # role into a fully remote posting.
    remote = False if explicit_in_person else (
        workplace == "remote"
        or provider_remote
        or bool(re.search(r"\bremote\b", f"{location_text}\n{description_text[:3000]}", re.I))
    )
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
        "work_arrangement": str(workplace_type or "").strip(),
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
    workday_detail_budget: Optional[int] = None,
    smartrecruiters_detail_budget: Optional[int] = None,
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
            workplace_type = row.get("workplaceType") or (
                "Remote" if row.get("isRemote") is True else ""
            )
            output.append(_direct_job(
                provider=provider, board=board, job_id=row.get("id") or row.get("jobUrl"), title=row.get("title"),
                description=row.get("descriptionPlain") or row.get("descriptionHtml"), url=row.get("jobUrl") or row.get("applyUrl"),
                location=row.get("location") or secondary_location, employment_type=row.get("employmentType"),
                posted_at=row.get("publishedAt") or row.get("updatedAt"), workplace_type=workplace_type,
                extra={
                    "_provider_is_remote": row.get("isRemote"),
                    "_provider_workplace_type": row.get("workplaceType"),
                    "_provider_secondary_locations": secondary,
                },
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

    if provider == "smartrecruiters":
        api_base = str(
            board.get("api_base") or "https://api.smartrecruiters.com/v1"
        ).rstrip("/")
        endpoint = f"{api_base}/companies/{identifier}/postings"
        page_limit = max(
            1, int(getattr(config, "ATS_SMARTRECRUITERS_MAX_PAGES_PER_BOARD", 3))
        )
        per_board_budget = max(
            0,
            int(
                getattr(
                    config,
                    "ATS_SMARTRECRUITERS_DETAIL_MAX_REQUESTS_PER_BOARD",
                    25,
                )
            ),
        )
        detail_limit = (
            per_board_budget
            if smartrecruiters_detail_budget is None
            else min(per_board_budget, max(0, int(smartrecruiters_detail_budget)))
        )

        rows: List[Dict[str, Any]] = []
        offset = 0
        page_size = 100
        for _page in range(page_limit):
            data, error = _fetch_json(
                fetcher,
                endpoint,
                params={
                    "limit": page_size,
                    "offset": offset,
                    "destination": "PUBLIC",
                },
            )
            if error:
                return [], error
            if not isinstance(data, dict):
                return [], "invalid_smartrecruiters_list"
            page_rows = data.get("content", data.get("postings", []))
            if not isinstance(page_rows, list) or not page_rows:
                break
            rows.extend(row for row in page_rows if isinstance(row, dict))
            offset += len(page_rows)
            try:
                total = int(data.get("totalFound", data.get("total", 0)) or 0)
            except (TypeError, ValueError):
                total = 0
            if (
                len(page_rows) < page_size
                or (total and offset >= total)
                or len(rows) >= max_jobs
            ):
                break

        def label(value: Any) -> str:
            if isinstance(value, Mapping):
                return str(value.get("label") or value.get("name") or "").strip()
            return str(value or "").strip()

        def location_text(value: Any) -> str:
            if not isinstance(value, Mapping):
                return str(value or "").strip()
            country = str(
                value.get("country") or value.get("countryCode") or ""
            ).strip()
            if country.lower() in {"us", "usa"}:
                country = "United States"
            return ", ".join(
                part
                for part in (
                    str(value.get("city") or "").strip(),
                    str(value.get("region") or value.get("regionCode") or "").strip(),
                    country,
                )
                if part
            )

        def description_text(value: Mapping[str, Any]) -> str:
            job_ad = value.get("jobAd") or {}
            sections = job_ad.get("sections") if isinstance(job_ad, Mapping) else {}
            if not isinstance(sections, Mapping):
                return ""
            parts: List[str] = []
            for key in (
                "companyDescription",
                "jobDescription",
                "qualifications",
                "additionalInformation",
            ):
                section = sections.get(key)
                if not isinstance(section, Mapping):
                    continue
                title = str(section.get("title") or "").strip()
                text = str(section.get("text") or "").strip()
                if title or text:
                    parts.append("\n".join(item for item in (title, text) if item))
            return "\n\n".join(parts)

        output: List[Dict[str, Any]] = []
        detail_calls = 0
        for row in rows[:max_jobs]:
            title = row.get("name") or row.get("title")
            posting_id = row.get("id") or row.get("uuid")
            detail: Mapping[str, Any] = {}
            detail_error = ""
            detail_requested = False
            if (
                posting_id
                and detail_calls < detail_limit
                and _greenhouse_title_may_match(title)
            ):
                detail_calls += 1
                detail_requested = True
                fetched, detail_error = _fetch_json(
                    fetcher, f"{endpoint}/{posting_id}"
                )
                if isinstance(fetched, Mapping):
                    detail = fetched
            effective: Mapping[str, Any] = detail or row
            if effective.get("active") is False:
                continue
            company_data = effective.get("company") or row.get("company") or {}
            company_name = (
                str(company_data.get("name") or "").strip()
                if isinstance(company_data, Mapping)
                else ""
            )
            row_board = dict(board)
            if company_name:
                row_board["company_name"] = company_name
            location_data = effective.get("location") or row.get("location") or {}
            remote = bool(
                isinstance(location_data, Mapping) and location_data.get("remote") is True
            )
            employment = label(
                effective.get("typeOfEmployment") or row.get("typeOfEmployment")
            )
            description = description_text(effective)
            url = (
                effective.get("postingUrl")
                or effective.get("applyUrl")
                or row.get("postingUrl")
                or row.get("ref")
                or f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}"
            )
            output.append(
                _direct_job(
                    provider=provider,
                    board=row_board,
                    job_id=posting_id,
                    title=effective.get("name") or title,
                    description=description,
                    url=url,
                    location=location_text(location_data),
                    employment_type=employment,
                    posted_at=(
                        effective.get("releasedDate") or row.get("releasedDate")
                    ),
                    workplace_type="remote" if remote else "",
                    extra={
                        "_provider_is_remote": remote,
                        "_smartrecruiters_detail_request_made": detail_requested,
                        "_smartrecruiters_detail_error": detail_error,
                    },
                )
            )
        return output, ""

    if provider == "workday":
        if "|" not in identifier:
            return [], "invalid_workday_identifier"
        tenant, site = identifier.split("|", 1)
        api_base = str(board.get("api_base") or "").rstrip("/")
        if not api_base:
            return [], "missing_workday_api_base"
        cxs_base = f"{api_base}/wday/cxs/{tenant}/{site}"
        page_limit = max(1, int(getattr(config, "ATS_WORKDAY_MAX_PAGES_PER_BOARD", 5)))
        per_board_budget = max(0, int(getattr(config, "ATS_WORKDAY_DETAIL_MAX_REQUESTS_PER_BOARD", 25)))
        detail_limit = per_board_budget if workday_detail_budget is None else min(
            per_board_budget, max(0, int(workday_detail_budget))
        )
        rows: List[Dict[str, Any]] = []
        offset = 0
        for _page in range(page_limit):
            payload = fetcher(
                f"{cxs_base}/jobs",
                method="POST",
                json_body={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if payload.status_code != 200:
                return [], f"HTTP {payload.status_code or 'error'}: {payload.error or payload.text[:200]}"
            try:
                data = json.loads(payload.text)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                return [], f"invalid_json:{exc}"
            page_rows = data.get("jobPostings", []) if isinstance(data, dict) else []
            if not isinstance(page_rows, list) or not page_rows:
                break
            rows.extend(row for row in page_rows if isinstance(row, dict))
            offset += len(page_rows)
            total = int(data.get("total", 0) or 0) if isinstance(data, dict) else 0
            if len(page_rows) < 20 or (total and offset >= total) or len(rows) >= max_jobs:
                break

        output: List[Dict[str, Any]] = []
        detail_calls = 0
        for row in rows[:max_jobs]:
            title = row.get("title")
            external_path = str(row.get("externalPath") or row.get("externalUrl") or "")
            posting_path = external_path.split("/job/", 1)[1] if "/job/" in external_path else ""
            detail_info: Dict[str, Any] = {}
            detail_error = ""
            detail_requested = False
            if posting_path and detail_calls < detail_limit and _greenhouse_title_may_match(title):
                detail_calls += 1
                detail_requested = True
                detail, detail_error = _fetch_json(fetcher, f"{cxs_base}/job/{posting_path}")
                if isinstance(detail, dict) and isinstance(detail.get("jobPostingInfo"), dict):
                    detail_info = detail["jobPostingInfo"]
            description = detail_info.get("jobDescription") or ""
            location = detail_info.get("location") or detail_info.get("primaryLocation") or row.get("locationsText") or ""
            url = detail_info.get("externalUrl") or external_path
            if url and str(url).startswith("/"):
                url = f"{api_base}{url}"
            job_id = detail_info.get("jobReqId") or (
                row.get("bulletFields", [""])[0]
                if isinstance(row.get("bulletFields"), list) and row.get("bulletFields")
                else external_path
            )
            output.append(_direct_job(
                provider=provider,
                board=board,
                job_id=job_id,
                title=detail_info.get("title") or title,
                description=description,
                url=url or board.get("board_url"),
                location=location,
                employment_type=detail_info.get("timeType") or detail_info.get("workerType") or "",
                posted_at=detail_info.get("startDate") or detail_info.get("postedOn") or row.get("postedOn") or "",
                extra={
                    "_workday_detail_request_made": detail_requested,
                    "_workday_detail_error": detail_error,
                },
            ))
        return output, ""

    return [], f"unsupported_provider:{provider}"
