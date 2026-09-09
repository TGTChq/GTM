"""Small, explicit runtime configuration for the core.

Deliberately NOT a copy of the legacy ``config.py`` (2,200+ lines, dozens of
historical switches). Every setting here has a meaning and a provenance, and
``Settings.describe()`` prints names and presence only -- never a secret value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Mapping, Optional

from .policy.campaigns import CAMPAIGN_ENV_BY_FUNCTION, POLICY_VERSION

SECRET_NAMES = (
    "TGTC_DATABASE_URL", "FANTASTIC_JOBS_API_KEY", "APOLLO_API_KEY", "AIRTABLE_TOKEN",
    "INSTANTLY_API_KEY", "ANTHROPIC_API_KEY", "TGTC_CORE_SIGNING_KEY",
)


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = str(env.get(name, "") or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(env.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- storage -----------------------------------------------------------
    database_url: str = ""
    # --- providers (presence only is ever reported) ------------------------
    fantastic_api_key: str = ""
    fantastic_base_url: str = "https://data.fantastic.jobs"
    apollo_api_key: str = ""
    apollo_base_url: str = "https://api.apollo.io/api/v1"
    airtable_token: str = ""
    airtable_base_id: str = ""
    airtable_table_name: str = "Leads"
    airtable_base_url: str = "https://api.airtable.com/v0"
    instantly_api_key: str = ""
    instantly_base_url: str = "https://api.instantly.ai/api/v2"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    inference_model: str = "claude-opus-5"
    signing_key: str = ""
    # --- campaign ids: env NAME -> id, read at runtime ---------------------
    campaign_env: Dict[str, str] = field(default_factory=dict)
    # --- limits (meaning + provenance in describe()) ------------------------
    fantastic_page_limit: int = 100
    fantastic_fresh_window_minutes: int = 60
    fantastic_fresh_lag_minutes: int = 180
    fantastic_backfill_window_hours: int = 24
    fantastic_time_frame: str = "7d"
    fantastic_max_pages_per_partition: int = 50
    fantastic_min_jobs_quota_remaining: int = 90
    fantastic_min_requests_quota_remaining: int = 20
    apollo_people_search_max_pages: int = 2
    apollo_availability_retry_hours: float = 6.0
    lease_seconds: int = 300
    retry_backoff_seconds: int = 120
    fresh_share_pct: int = 80
    payload_retention_days: int = 30
    person_employer_uniqueness: bool = True
    instantly_verify_on_import: bool = False
    inference_enabled: bool = True

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        campaign_env: Dict[str, str] = {}
        for name in list(CAMPAIGN_ENV_BY_FUNCTION.values()) + ["INSTANTLY_CAMPAIGN_ID"]:
            for suffix in ("", "_SMALL", "_MID", "_LARGE"):
                key = f"{name}{suffix}"
                value = str(env.get(key, "") or "").strip()
                if value:
                    campaign_env[key] = value
        return cls(
            database_url=str(env.get("TGTC_DATABASE_URL", "") or ""),
            fantastic_api_key=str(env.get("FANTASTIC_JOBS_API_KEY", "") or ""),
            fantastic_base_url=str(env.get("FANTASTIC_JOBS_BASE_URL", "") or "https://data.fantastic.jobs"),
            apollo_api_key=str(env.get("APOLLO_API_KEY", "") or ""),
            airtable_token=str(env.get("AIRTABLE_TOKEN", "") or ""),
            airtable_base_id=str(env.get("AIRTABLE_BASE_ID", "") or ""),
            airtable_table_name=str(env.get("AIRTABLE_TABLE_NAME", "") or "Leads"),
            instantly_api_key=str(env.get("INSTANTLY_API_KEY", "") or ""),
            instantly_base_url=str(env.get("INSTANTLY_BASE_URL", "") or "https://api.instantly.ai/api/v2"),
            anthropic_api_key=str(env.get("ANTHROPIC_API_KEY", "") or ""),
            anthropic_base_url=str(env.get("TGTC_ANTHROPIC_BASE_URL", "") or "https://api.anthropic.com"),
            inference_model=str(env.get("TGTC_INFERENCE_MODEL", "") or "claude-opus-5"),
            signing_key=str(env.get("TGTC_CORE_SIGNING_KEY", "") or ""),
            campaign_env=campaign_env,
            fantastic_page_limit=_int(env, "TGTC_FANTASTIC_PAGE_LIMIT", 100),
            fantastic_fresh_window_minutes=_int(env, "TGTC_FRESH_WINDOW_MINUTES", 60),
            fantastic_fresh_lag_minutes=_int(env, "TGTC_FRESH_LAG_MINUTES", 180),
            fantastic_backfill_window_hours=_int(env, "TGTC_BACKFILL_WINDOW_HOURS", 24),
            fantastic_time_frame=str(env.get("TGTC_FANTASTIC_TIME_FRAME", "") or "7d"),
            fantastic_max_pages_per_partition=_int(env, "TGTC_FANTASTIC_MAX_PAGES_PER_PARTITION", 50),
            fantastic_min_jobs_quota_remaining=_int(env, "TGTC_FANTASTIC_MIN_JOBS_QUOTA_REMAINING", 90),
            fantastic_min_requests_quota_remaining=_int(env, "TGTC_FANTASTIC_MIN_REQUESTS_QUOTA_REMAINING", 20),
            apollo_people_search_max_pages=_int(env, "TGTC_APOLLO_PEOPLE_SEARCH_MAX_PAGES", 2),
            apollo_availability_retry_hours=float(_int(env, "TGTC_APOLLO_AVAILABILITY_RETRY_HOURS", 6)),
            lease_seconds=_int(env, "TGTC_LEASE_SECONDS", 300),
            retry_backoff_seconds=_int(env, "TGTC_RETRY_BACKOFF_SECONDS", 120),
            fresh_share_pct=_int(env, "TGTC_FRESH_SHARE_PCT", 80),
            payload_retention_days=_int(env, "TGTC_PAYLOAD_RETENTION_DAYS", 30),
            person_employer_uniqueness=_bool(env, "TGTC_PERSON_EMPLOYER_UNIQUENESS", True),
            instantly_verify_on_import=_bool(env, "INSTANTLY_VERIFY_ON_IMPORT", False),
            inference_enabled=_bool(env, "TGTC_INFERENCE_ENABLED", True),
        )

    def describe(self) -> Dict[str, Any]:
        """Names, presence and limits. Never a value of a secret."""
        out: Dict[str, Any] = {"policy_version": POLICY_VERSION, "secrets_present": {}, "limits": {}}
        for name, attr in (
            ("TGTC_DATABASE_URL", "database_url"), ("FANTASTIC_JOBS_API_KEY", "fantastic_api_key"),
            ("APOLLO_API_KEY", "apollo_api_key"), ("AIRTABLE_TOKEN", "airtable_token"),
            ("INSTANTLY_API_KEY", "instantly_api_key"), ("ANTHROPIC_API_KEY", "anthropic_api_key"),
            ("TGTC_CORE_SIGNING_KEY", "signing_key"),
        ):
            out["secrets_present"][name] = bool(getattr(self, attr))
        out["campaign_env_present"] = sorted(self.campaign_env.keys())
        for f in fields(self):
            if f.type in ("int", "float", "bool") or isinstance(getattr(self, f.name), (int, float, bool)):
                if f.name not in {"campaign_env"}:
                    out["limits"][f.name] = getattr(self, f.name)
        out["limits"]["airtable_base_id_present"] = bool(self.airtable_base_id)
        out["limits"]["airtable_table_name"] = self.airtable_table_name
        out["limits"]["inference_model"] = self.inference_model
        return out
