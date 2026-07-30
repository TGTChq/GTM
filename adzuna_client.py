"""Official Adzuna job-search API acquisition client.

Additive, self-contained module (FINAL_30_PLUS_SYSTEM_SPEC.md section 11).
Deliberately does not import or modify ``config.py`` -- it reads its own
``ADZUNA_*`` environment variables directly so it can be reviewed and wired
into ``config.py``/``multi_source_acquisition.py`` as a single, separate
integration step. Never reads, prints, logs, or serializes credential values;
only variable names and non-secret metadata may appear in any report.

Output uses the exact same normalized job-record field names as
``free_job_sources.py``'s adapters (``employer_name``, ``job_title``,
``job_id``, ``job_posted_at_datetime_utc``, etc.) so it is directly
dedup-compatible with ``job_filter.dedup_key()`` and freshness-compatible
with ``job_signal.posted_datetime_candidates()`` without any adapter-specific
handling elsewhere in the pipeline.

Adzuna is a discovery source, like the free feeds: it never establishes
employer identity by itself (``job_apply_is_direct=False``,
``_provider_record_structured=True``) and remains subject to the normal
Job/Account/Contact validation gates downstream.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from free_job_sources import FetchPayload, Fetcher, SourceResult, default_fetcher, html_to_text

logger = logging.getLogger(__name__)

ADZUNA_ENDPOINT_TEMPLATE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

# Small fallback default, used only when no role-catalog/config portfolio is
# available (Phase 13 section 4). Production acquisition should drive the
# role-catalog-derived portfolio via build_query_portfolio().
DEFAULT_ADZUNA_QUERIES = [
    "revenue operations",
    "marketing operations",
    "gtm engineer",
    "customer success manager",
    "sales automation",
]

_QUOTA_BODY_PATTERNS = (
    "quota",
    "upgrade your plan",
    "current plan, basic",
)


@dataclass
class AdzunaQuerySpec:
    """One planned Adzuna query cell of the portfolio (Phase 13 section 4)."""
    role_family: str
    canonical_role: str
    title_variant: str          # the actual `what` search string
    remote_variant: str         # "" | "remote"
    freshness_window_days: int  # max_days_old for this cell
    max_pages: int              # pagination depth for this cell


def build_query_portfolio(
    roles,
    *,
    remote_variants=("", "remote"),
    freshness_windows=(30,),
    max_pages: int = 2,
    max_queries: int = 12,
    role_family_of=None,
) -> list["AdzunaQuerySpec"]:
    """Build a controlled, role-catalog-derived query portfolio.

    ``roles`` is an iterable of canonical role titles (e.g.
    ``role_catalog.DEFAULT_ACQUISITION_ROLES``). ``role_family_of`` maps a
    role to its function bucket (e.g. ``role_catalog.get_function_bucket``);
    when omitted the family is left blank. Title variants are the canonical
    role plus a conservative remote-prefixed variant -- deliberately NOT a
    broad keyword expansion, to avoid quality-unsafe queries. The portfolio is
    capped at ``max_queries`` cells (role x remote-variant x freshness-window),
    ordered so each distinct role is covered before deepening variants.
    """
    specs: list[AdzunaQuerySpec] = []
    role_list = [str(r).strip() for r in roles if str(r).strip()]
    for window in freshness_windows:
        for remote in remote_variants:
            for role in role_list:
                what = f"{remote} {role}".strip() if remote else role
                family = role_family_of(role) if role_family_of else ""
                specs.append(AdzunaQuerySpec(
                    role_family=str(family or ""),
                    canonical_role=role,
                    title_variant=what,
                    remote_variant=remote,
                    freshness_window_days=int(window),
                    max_pages=int(max_pages),
                ))
                if len(specs) >= max_queries:
                    return specs
    return specs


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


@dataclass
class AdzunaSettings:
    """Snapshot of the Adzuna environment configuration.

    Never carries the actual APP_ID/APP_KEY values into a report -- only
    ``app_id_configured``/``app_key_configured`` booleans should be surfaced.
    """

    enabled: bool = field(default_factory=lambda: _env_bool("ADZUNA_ENABLED", False))
    country: str = field(default_factory=lambda: (os.getenv("ADZUNA_COUNTRY") or "us").strip().lower())
    app_id: str = field(default_factory=lambda: os.getenv("ADZUNA_APP_ID") or "")
    app_key: str = field(default_factory=lambda: os.getenv("ADZUNA_APP_KEY") or "")
    results_per_page: int = field(default_factory=lambda: _env_int("ADZUNA_RESULTS_PER_PAGE", 50))
    max_pages_per_query: int = field(default_factory=lambda: _env_int("ADZUNA_MAX_PAGES_PER_QUERY", 3))
    max_requests_per_run: int = field(default_factory=lambda: _env_int("ADZUNA_MAX_REQUESTS_PER_RUN", 40))
    max_days_old: int = field(default_factory=lambda: _env_int("ADZUNA_MAX_DAYS_OLD", 30))
    timeout_seconds: int = field(default_factory=lambda: _env_int("ADZUNA_TIMEOUT_SECONDS", 20))

    @property
    def app_id_configured(self) -> bool:
        return bool(self.app_id)

    @property
    def app_key_configured(self) -> bool:
        return bool(self.app_key)

    @property
    def credentials_configured(self) -> bool:
        return self.app_id_configured and self.app_key_configured


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _iso(value: Any) -> str:
    """Adzuna's ``created`` field is already ISO-8601 UTC (e.g.
    ``2026-07-20T09:15:00Z``); pass through parseable values, drop the rest."""
    text = _clean(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _redirect_domain(url: str) -> str:
    """Adzuna's own redirect_url is a tracking link, never a company domain."""
    return ""


def normalize_adzuna_job(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one raw Adzuna result row to this pipeline's normalized job schema.

    Returns ``None`` for a row with no usable job id -- Adzuna's ``id`` field
    is the only stable identifier available; without it the record cannot be
    safely deduplicated across pagination or across runs.
    """
    job_id = _clean(row.get("id"))
    if not job_id:
        return None
    company = row.get("company") if isinstance(row.get("company"), Mapping) else {}
    location = row.get("location") if isinstance(row.get("location"), Mapping) else {}
    category = row.get("category") if isinstance(row.get("category"), Mapping) else {}

    title_text = _clean(row.get("title"))
    company_text = _clean(company.get("display_name"))
    description_text = html_to_text(row.get("description"))
    apply_url = _clean(row.get("redirect_url"))
    location_text = _clean(location.get("display_name")) or "United States"
    contract_time = _clean(row.get("contract_time"))
    contract_type = _clean(row.get("contract_type"))

    job: Dict[str, Any] = {
        "job_id": f"adzuna:{job_id}",
        "job_title": title_text,
        "employer_name": company_text,
        # Adzuna's redirect_url is a tracking link, never the employer's own
        # domain -- never populate employer_website from it.
        "employer_website": _redirect_domain(apply_url),
        "job_publisher": "Adzuna",
        "job_description": description_text,
        "job_apply_link": apply_url,
        "job_apply_is_direct": False,
        "job_google_link": "",
        "job_location": location_text,
        "job_country": "US",
        "job_is_remote": bool(re.search(r"\bremote\b", f"{title_text} {description_text}", re.I)),
        # A missing/unknown contract_time must NOT be assumed full-time
        # (Phase 13 section 4): emit "" so the downstream employment-evidence
        # gate decides, rather than silently manufacturing a FULLTIME label.
        "job_employment_type": (
            "FULLTIME" if contract_time == "full_time"
            else "PARTTIME" if contract_time == "part_time"
            else "CONTRACTOR" if contract_type == "contract"
            else "PERMANENT" if contract_type == "permanent"
            else ""
        ),
        "job_posted_at_datetime_utc": _iso(row.get("created")),
        "job_min_salary": row.get("salary_min"),
        "job_max_salary": row.get("salary_max"),
        "job_salary_currency": "USD",
        "job_required_skills": [],
        "apply_options": (
            [{"publisher": "Adzuna", "apply_link": apply_url, "is_direct": False}]
            if apply_url else []
        ),
        "canonical_source_url": apply_url,
        "_acquisition_source": "adzuna",
        "_source_home_url": "https://www.adzuna.com",
        "_provider_record_structured": True,
        "_adzuna_category": _clean(category.get("label")),
    }
    return job


class AdzunaAdapter:
    """Official Adzuna acquisition source, following the same class-based
    ``.fetch(fetcher) -> SourceResult`` adapter shape as free_job_sources.py's
    adapters (see ``multi_source_acquisition.build_adapters()``), so it can be
    registered the same way once wired in."""

    name = "adzuna"
    display_name = "Adzuna (official)"

    def __init__(
        self,
        settings: Optional[AdzunaSettings] = None,
        *,
        queries: Optional[List[str]] = None,
        portfolio: Optional[List["AdzunaQuerySpec"]] = None,
        marginal_min_new_companies: int = 1,
        max_transient_retries: int = 2,
    ):
        self.settings = settings or AdzunaSettings()
        self.marginal_min_new_companies = max(0, int(marginal_min_new_companies))
        self.max_transient_retries = max(0, int(max_transient_retries))
        if portfolio is not None:
            self.portfolio = list(portfolio)
        else:
            # Normalize a plain string list (or the small fallback default)
            # into portfolio cells using the settings' window/page depth.
            base = list(queries) if queries is not None else list(DEFAULT_ADZUNA_QUERIES)
            self.portfolio = [
                AdzunaQuerySpec(
                    role_family="", canonical_role=q, title_variant=q,
                    remote_variant="", freshness_window_days=self.settings.max_days_old,
                    max_pages=self.settings.max_pages_per_query,
                )
                for q in base
            ]

    def _fetch_page(self, fetcher, endpoint_country, spec, page):
        """One page request with bounded retry on transient failure. Returns
        (payload, classification) where classification is one of:
        ok | auth | quota | rate_limited | transient | None."""
        params = {
            "app_id": self.settings.app_id,
            "app_key": self.settings.app_key,
            "results_per_page": max(1, min(50, self.settings.results_per_page)),
            "what": spec.title_variant,
            "max_days_old": max(1, spec.freshness_window_days),
            "content-type": "application/json",
        }
        endpoint = ADZUNA_ENDPOINT_TEMPLATE.format(country=endpoint_country, page=page)
        attempts = 0
        while True:
            attempts += 1
            payload = fetcher(endpoint, params=params, timeout=self.settings.timeout_seconds)
            body_lower = (payload.text or "").lower()
            code = payload.status_code
            if code and code >= 400 and any(p in body_lower for p in _QUOTA_BODY_PATTERNS):
                return payload, "quota"
            if code in (401, 403):
                return payload, "auth"
            if code == 429:
                return payload, "rate_limited"
            if code == 200:
                return payload, "ok"
            # 5xx / network / unexpected: bounded retry before giving up.
            if attempts <= self.max_transient_retries:
                continue
            return payload, "transient"

    def fetch(self, fetcher: Fetcher = default_fetcher) -> SourceResult:
        result = SourceResult(source=self.name)
        settings = self.settings

        if not settings.enabled:
            result.success = True
            result.metadata = {"skipped_reason": "disabled_by_config", "enabled": False}
            return result
        if not settings.credentials_configured:
            missing = [
                name for name, configured in (
                    ("ADZUNA_APP_ID", settings.app_id_configured),
                    ("ADZUNA_APP_KEY", settings.app_key_configured),
                )
                if not configured
            ]
            result.success = False
            result.errors.append(f"missing_credentials:{','.join(missing)}")
            result.metadata = {"skipped_reason": "missing_credentials", "missing_variables": missing}
            return result

        endpoint_country = settings.country or "us"
        requests_budget = max(0, settings.max_requests_per_run)
        seen_job_keys: set[str] = set()
        seen_company_keys: set[str] = set()
        auth_failed = False
        quota_exhausted = False
        rate_limited_queries: List[str] = []
        transient_error_queries: List[str] = []
        queries_attempted = 0
        consecutive_low_yield = 0
        per_query: List[Dict[str, Any]] = []

        for spec in self.portfolio:
            if requests_budget <= 0 or auth_failed or quota_exhausted:
                break
            queries_attempted += 1
            q_raw = 0
            q_new_jobs = 0
            q_new_companies = 0
            for page in range(1, max(1, spec.max_pages) + 1):
                if requests_budget <= 0:
                    break
                result.requests_attempted += 1
                result.pages += 1
                requests_budget -= 1
                payload, cls = self._fetch_page(fetcher, endpoint_country, spec, page)

                if cls == "quota":
                    quota_exhausted = True
                    result.errors.append(f"quota_exhausted:{spec.title_variant}:page{page}")
                    break
                if cls == "auth":
                    auth_failed = True
                    result.errors.append(f"authentication_failed:HTTP{payload.status_code}")
                    break
                if cls == "rate_limited":
                    rate_limited_queries.append(spec.title_variant)
                    result.errors.append(f"rate_limited:{spec.title_variant}:page{page}")
                    break
                if cls == "transient":
                    transient_error_queries.append(f"{spec.title_variant}:page{page}")
                    result.errors.append(
                        f"transient_error:HTTP{payload.status_code or 'none'}:{payload.error or payload.text[:120]}"
                    )
                    break

                result.requests_succeeded += 1
                data = _json_object(payload)
                if data is None:
                    result.errors.append(f"malformed_response:{spec.title_variant}:page{page}")
                    break
                rows = data.get("results")
                if not isinstance(rows, list):
                    result.errors.append(f"malformed_response_no_results_array:{spec.title_variant}:page{page}")
                    break
                # A 200 with an empty results list is a SUCCESSFUL execution,
                # not a failure (Phase 13 section 4) -- simply nothing to add.
                result.raw_records += len(rows)
                q_raw += len(rows)

                new_this_page = 0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    job = normalize_adzuna_job(row)
                    if not job:
                        continue
                    marker = job["job_id"]
                    if marker in seen_job_keys:
                        continue
                    seen_job_keys.add(marker)
                    job["_adzuna_query"] = spec.title_variant
                    job["_adzuna_role_family"] = spec.role_family
                    job["_adzuna_page"] = page
                    result.jobs.append(job)
                    new_this_page += 1
                    q_new_jobs += 1
                    company = str(job.get("employer_name") or "").strip().lower()
                    if company and company not in seen_company_keys:
                        seen_company_keys.add(company)
                        q_new_companies += 1

                if not rows or new_this_page == 0:
                    break

            per_query.append({
                "what": spec.title_variant, "role_family": spec.role_family,
                "freshness_window_days": spec.freshness_window_days,
                "raw": q_raw, "new_jobs": q_new_jobs, "new_companies": q_new_companies,
            })
            # Marginal-yield stop: if a query adds fewer than the minimum new
            # canonical companies, count it; after two consecutive low-yield
            # queries, stop expanding the portfolio (budget-preserving).
            if q_new_companies < self.marginal_min_new_companies:
                consecutive_low_yield += 1
                if consecutive_low_yield >= 2:
                    result.errors.append("marginal_yield_stop")
                    break
            else:
                consecutive_low_yield = 0

        # Success = no auth/quota failure and every attempted query executed
        # cleanly (a clean run with zero jobs is still a successful execution).
        result.success = not auth_failed and not quota_exhausted
        result.metadata = {
            "enabled": True,
            "country": endpoint_country,
            "queries_planned": len(self.portfolio),
            "queries_attempted": queries_attempted,
            "unique_jobs": len(seen_job_keys),
            "unique_companies": len(seen_company_keys),
            "auth_failed": auth_failed,
            "quota_exhausted": quota_exhausted,
            "rate_limited_queries": rate_limited_queries,
            "transient_error_queries": transient_error_queries,
            "requests_budget_remaining": requests_budget,
            "per_query": per_query,
        }
        return result


def _json_object(payload: FetchPayload) -> Optional[Dict[str, Any]]:
    import json

    try:
        data = json.loads(payload.text or "")
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def run_adzuna_acquisition(
    fetcher: Fetcher = default_fetcher,
    *,
    queries: Optional[List[str]] = None,
    settings: Optional[AdzunaSettings] = None,
) -> SourceResult:
    """Convenience entry point mirroring free_job_sources.py's module-level
    acquisition functions; equivalent to ``AdzunaAdapter(...).fetch(fetcher)``."""
    return AdzunaAdapter(settings=settings, queries=queries).fetch(fetcher)
