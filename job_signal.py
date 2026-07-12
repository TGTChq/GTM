"""Freshness and source-quality checks for job-intent signals.

Job freshness and URL quality are informational: older/date-unknown openings
and missing/unreliable source links are retained. Relevance plus the explicit
Airtable approval remain the human decision point for enrollment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests

from domain_utils import normalize_company_domain

FRESHNESS_BLOCKING: set[str] = set()  # Informational only; never blocks enrollment.
URL_BLOCKING: set[str] = set()  # Informational only; Relevance + human approval drive enrollment.

# ATS links are acceptable canonical evidence even though the domain differs
# from the employer's own website.
ATS_DOMAINS = {
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "workdayjobs.com", "icims.com", "smartrecruiters.com", "jobvite.com",
    "breezy.hr", "workable.com", "recruitee.com", "applytojob.com",
    "adp.com", "oraclecloud.com", "successfactors.com", "bamboohr.com",
    "personio.com", "rippling.com", "eightfold.ai", "phenompeople.com",
}

AGGREGATOR_DOMAINS = {
    "trabajo.org", "jobleads.com", "jobright.ai", "jobgether.com",
    "lensa.com", "bebee.com", "jooble.org", "talent.com", "jora.com",
    "whatjobs.com", "grabjobs.co", "adzuna.com", "careerbuilder.com",
    "ziprecruiter.com", "glassdoor.com", "simplyhired.com", "dice.com",
    "builtin.com", "remoteok.com", "remoterocketship.com",
}

# These sites can return HTTP 200 while still being unreliable review links in
# a real browser (geo/session gating, dead partner mirrors, or non-canonical
# copies). They may remain useful as source metadata, but are never saved as the
# primary Airtable Job URL.
UNRELIABLE_REVIEW_URL_DOMAINS = {
    "trabajo.org",
}

STABLE_JOB_BOARD_DOMAINS = {
    "linkedin.com": "linkedin",
    "indeed.com": "indeed",
}

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}


@dataclass(frozen=True)
class JobSignalAssessment:
    freshness: str
    age_days: Optional[int]
    freshness_reason: str
    job_url: str
    url_status: str
    url_source: str
    url_reason: str

    @property
    def review_required(self) -> bool:
        return self.freshness in FRESHNESS_BLOCKING or self.url_status in URL_BLOCKING

    def notes(self) -> str:
        bits = [
            f"freshness={self.freshness}",
            f"freshness_reason={self.freshness_reason}",
            f"url_status={self.url_status}",
            f"url_source={self.url_source}",
            f"url_reason={self.url_reason}",
        ]
        if self.age_days is not None:
            bits.insert(1, f"age_days={self.age_days}")
        return " | ".join(bits)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object, *, relative_to: Optional[datetime] = None) -> Optional[datetime]:
    if value in (None, ""):
        return None

    base_now = _as_utc(relative_to or datetime.now(timezone.utc))

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:  # milliseconds
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d{10,13}", text):
        return _parse_datetime(int(text), relative_to=base_now)

    lowered = text.lower().strip()
    if lowered in {"today", "just posted", "new"}:
        return base_now
    if lowered == "yesterday":
        from datetime import timedelta
        return base_now - timedelta(days=1)

    # Human-readable ages are common in syndicated job feeds.  Parse the
    # major units so an old listing cannot masquerade as recent merely because
    # the API also exposes a newer re-syndication timestamp. Months and years
    # are intentionally conservative approximations for freshness filtering.
    word_number = {"a": 1, "an": 1, "one": 1}
    relative = re.search(
        r"(\d+|a|an|one)\s*\+?\s*(minute|hour|day|week|month|year)s?\s+ago",
        lowered,
    )
    if relative:
        from datetime import timedelta
        raw_amount, unit = relative.groups()
        amount = int(raw_amount) if raw_amount.isdigit() else word_number[raw_amount]
        if unit == "minute":
            return base_now - timedelta(minutes=amount)
        if unit == "hour":
            return base_now - timedelta(hours=amount)
        if unit == "day":
            return base_now - timedelta(days=amount)
        if unit == "week":
            return base_now - timedelta(days=amount * 7)
        if unit == "month":
            return base_now - timedelta(days=amount * 30)
        if unit == "year":
            return base_now - timedelta(days=amount * 365)

    normalized = text.replace("Z", "+00:00")
    try:
        return _as_utc(datetime.fromisoformat(normalized))
    except ValueError:
        return None


POSTED_AT_KEYS = (
    "job_posted_at_datetime_utc",
    "job_posted_at_timestamp",
    "job_posted_at",
    "posted_at",
    "date_posted",
    "job_posted_at_date",
)


def posted_datetime_candidates(job: Dict, *, now: Optional[datetime] = None) -> List[Tuple[str, datetime]]:
    """Return every parseable posted-date signal supplied by the source.

    Aggregators sometimes re-syndicate an old opening and expose a recent API
    timestamp while also retaining an older human-readable age.  We keep all
    signals and use the oldest one conservatively instead of trusting the first
    field blindly.
    """
    candidates: List[Tuple[str, datetime]] = []
    for key in POSTED_AT_KEYS:
        parsed = _parse_datetime(job.get(key), relative_to=now)
        if parsed:
            candidates.append((key, parsed))
    return candidates


def posted_datetime(job: Dict) -> Optional[datetime]:
    candidates = posted_datetime_candidates(job)
    return min((value for _key, value in candidates), default=None)


def expiration_datetime(job: Dict) -> Optional[datetime]:
    for key in (
        "job_offer_expiration_datetime_utc",
        "job_offer_expiration_timestamp",
        "job_expiration_datetime_utc",
        "job_expiration_timestamp",
    ):
        parsed = _parse_datetime(job.get(key))
        if parsed:
            return parsed
    return None


def classify_freshness(
    job: Dict,
    *,
    now: Optional[datetime] = None,
    extra_posted_dates: Optional[List[Tuple[str, datetime]]] = None,
) -> Tuple[str, Optional[int], str]:
    current = _as_utc(now or datetime.now(timezone.utc))
    candidates = posted_datetime_candidates(job, now=current)
    candidates.extend(extra_posted_dates or [])
    posted = min((value for _key, value in candidates), default=None)
    expires = expiration_datetime(job)

    if expires and expires < current:
        age = max(0, (current - posted).days) if posted else None
        return "stale_review", age, "explicit_expiration_is_in_the_past"

    if not posted:
        return "unknown_review", None, "missing_or_unparseable_posted_at"

    age_days = max(0, (current - posted).days)
    conflict = False
    if len(candidates) > 1:
        values = [value for _key, value in candidates]
        conflict = (max(values) - min(values)).days >= 2

    if age_days <= 7:
        reason = "posted_within_7_days"
    elif age_days < 30:
        reason = "posted_8_to_29_days_ago"
    else:
        reason = "posted_30_or_more_days_ago"

    if conflict:
        reason += ";conflicting_posted_dates_used_oldest"

    if age_days <= 7:
        return "fresh", age_days, reason
    if age_days < 30:
        return "aging", age_days, reason
    return "stale_review", age_days, reason


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""


def _domain_matches(host: str, domain: str) -> bool:
    host = (host or "").lower().strip(".")
    domain = (domain or "").lower().strip(".")
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def _domain_in(host: str, domains: Iterable[str]) -> Optional[str]:
    for domain in domains:
        if _domain_matches(host, domain):
            return domain
    return None


def classify_url_source(url: str, company_domain: str = "") -> str:
    host = _host(url)
    if not host:
        return "invalid"
    if company_domain and _domain_matches(host, company_domain):
        return "company"
    if _domain_in(host, ATS_DOMAINS):
        return "ats"
    for domain, label in STABLE_JOB_BOARD_DOMAINS.items():
        if _domain_matches(host, domain):
            return label
    if _domain_matches(host, "google.com"):
        return "google"
    if _domain_in(host, AGGREGATOR_DOMAINS):
        return "aggregator"
    return "other"


def _is_google_jobs_url(url: str) -> bool:
    """Return True for fragile Google Jobs detail-viewer URLs.

    JSearch exposes ``job_google_link`` as a Google for Jobs viewer URL, but
    those links are session/locale dependent and often reopen a generic Google
    page instead of the original vacancy. They are useful as source metadata,
    not as the review URL saved in Airtable.
    """
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False

    host = (parsed.hostname or "").lower().strip(".")
    if not (host == "google.com" or host.endswith(".google.com")):
        return False

    query = parsed.query.lower()
    fragment = parsed.fragment.lower()
    path = parsed.path.lower()
    markers = (
        "htidocid=",
        "ibp=htl%3bjobs",
        "ibp=htl;jobs",
        "udm=8",
        "vssid=jobs-detail-viewer",
        "fpstate=tldetail",
        "jobs-detail-viewer",
    )
    return path == "/search" and any(marker in query or marker in fragment for marker in markers)


def _is_unreliable_review_url(url: str) -> bool:
    """Return True for mirror domains that should not be used for human review."""
    host = _host(str(url or ""))
    return bool(_domain_in(host, UNRELIABLE_REVIEW_URL_DOMAINS))


def _clean_candidate_url(url: str) -> str:
    """Remove only known tracking parameters without touching functional ones."""
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    if not parsed.scheme or not parsed.netloc:
        return str(url or "").strip()

    tracking_keys = {
        "utm_campaign", "utm_source", "utm_medium", "utm_term", "utm_content",
        "gclid", "gbraid", "wbraid",
    }
    filtered = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in tracking_keys
    ]
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(filtered, doseq=True),
        parsed.fragment,
    ))


def _candidate_urls(job: Dict) -> List[Tuple[str, str, bool]]:
    """Collect non-Google application URLs from the JSearch payload.

    The API provides ``job_apply_link`` plus ``apply_options`` containing
    publisher-specific links. Google Jobs viewer URLs and explicitly unreliable
    partner-mirror domains are intentionally excluded from Airtable review URLs.
    """
    candidates: List[Tuple[str, str, bool]] = []
    indexes: Dict[str, int] = {}

    def add(value: object, label: str, is_direct: bool = False) -> None:
        raw_url = str(value or "").strip()
        if not raw_url or not re.match(r"^https?://", raw_url, re.I):
            return
        if _is_google_jobs_url(raw_url) or _is_unreliable_review_url(raw_url):
            return
        url = _clean_candidate_url(raw_url)
        existing_index = indexes.get(url)
        if existing_index is not None:
            old_url, old_label, old_direct = candidates[existing_index]
            if is_direct and not old_direct:
                candidates[existing_index] = (old_url, old_label, True)
            return
        indexes[url] = len(candidates)
        candidates.append((url, label, bool(is_direct)))

    add(
        job.get("job_apply_link"),
        "job_apply_link",
        bool(job.get("job_apply_is_direct")),
    )

    options = job.get("apply_options") or job.get("job_apply_options") or []
    if isinstance(options, dict):
        options = [options]
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            label = str(option.get("publisher") or option.get("source") or "apply_option")
            is_direct = bool(option.get("is_direct") or option.get("job_apply_is_direct"))
            for key in ("apply_link", "job_apply_link", "link", "url"):
                add(option.get(key), label, is_direct)

    # Some response variants use these generic names. They are accepted only
    # when they are ordinary HTTP URLs and not a Google Jobs detail viewer.
    for key in ("apply_link", "job_url", "job_link"):
        add(job.get(key), key, False)

    return candidates


def _source_rank(source: str) -> int:
    return {
        "company": 0,
        "ats": 1,
        "linkedin": 2,
        "indeed": 2,
        "other": 3,
        "aggregator": 4,
        "google": 9,
        "invalid": 10,
    }.get(source, 8)

def _probe_url(url: str, timeout: float = 8.0) -> Tuple[str, str]:
    """Return (probe_state, reason).

    ``verified`` means the server returned a normal 2xx/3xx response.
    ``broken`` is reserved for strong evidence such as 404/410 or an invalid
    URL.  Bot blocks and transient server failures are not mislabeled as dead;
    they are sent to human review instead.
    """
    if not re.match(r"^https?://", str(url or ""), re.I):
        return "broken", "invalid_or_missing_http_url"

    try:
        response = requests.get(
            url,
            headers=_HTTP_HEADERS,
            allow_redirects=True,
            timeout=timeout,
            stream=True,
        )
        status = response.status_code
        final_url = str(response.url or url)
        response.close()
    except requests.RequestException as exc:
        return "unverified", f"request_error:{exc.__class__.__name__}"

    if 200 <= status < 400:
        if _is_google_jobs_url(final_url):
            return "unverified", f"http_{status};redirected_to_google_jobs"
        return "verified", f"http_{status}"
    if status in {404, 410}:
        return "broken", f"http_{status}"
    if status in {401, 403, 405, 408, 409, 423, 425, 429, 451}:
        return "unverified", f"http_{status}_may_be_bot_or_access_block"
    if 500 <= status < 600:
        return "unverified", f"http_{status}_server_error"
    return "unverified", f"http_{status}"


def select_job_url(
    job: Dict,
    *,
    company_domain: str = "",
    probe: bool = True,
) -> Tuple[str, str, str, str]:
    """Return the best stable application URL.

    Priority: employer careers page, known ATS, LinkedIn/Indeed, other direct
    source, then a working aggregator. Google Jobs viewer URLs and known
    unreliable partner mirrors are never saved as the primary ``Job URL``.
    """
    raw_primary = str(job.get("job_apply_link") or "").strip()
    original_reference = _clean_candidate_url(raw_primary) if re.match(r"^https?://", raw_primary, re.I) else ""
    candidates = _candidate_urls(job)
    if not candidates:
        original_apply = raw_primary
        if job.get("job_google_link") or _is_google_jobs_url(original_apply):
            return "", "unverified_review", "missing", "only_google_jobs_viewer_url_available"
        if _is_unreliable_review_url(original_apply):
            return "", "unverified_review", "missing", "only_unreliable_partner_mirror_url_available"
        return "", "unverified_review", "missing", "no_stable_candidate_job_url"

    company_domain = normalize_company_domain(company_domain) or normalize_company_domain(
        job.get("employer_website") or ""
    )
    # Compare the selected URL against JSearch's original primary apply link,
    # even when that primary link was excluded as an unreliable mirror.
    original = original_reference or candidates[0][0]

    ranked = sorted(
        candidates,
        key=lambda item: (
            _source_rank(classify_url_source(item[0], company_domain)),
            0 if item[2] else 1,  # direct application links win within a source tier
            0 if item[0] == original else 1,
        ),
    )

    if not probe:
        chosen = ranked[0][0]
        source = classify_url_source(chosen, company_domain)
        status = "fallback_used" if chosen != original else "unverified_review"
        return chosen, status, source, "url_not_probed"

    broken_reasons: List[str] = []
    unverified_candidates: List[Tuple[str, str, str]] = []
    aggregator_candidates: List[Tuple[str, str, str]] = []

    for url, _label, _is_direct in ranked:
        source = classify_url_source(url, company_domain)
        probe_state, probe_reason = _probe_url(url)

        if source == "aggregator":
            if probe_state == "verified":
                if url != original:
                    return url, "fallback_used", source, f"{probe_reason};aggregator_source"
                return url, "verified", source, f"{probe_reason};aggregator_source"
            if probe_state == "unverified":
                aggregator_candidates.append((url, source, probe_reason))
            else:
                broken_reasons.append(f"{url}:{probe_reason}")
            continue

        if probe_state == "verified":
            if url != original:
                return url, "fallback_used", source, f"{probe_reason};better_or_working_fallback_selected"
            return url, "verified", source, probe_reason

        if probe_state == "unverified":
            unverified_candidates.append((url, source, probe_reason))
        else:
            broken_reasons.append(f"{url}:{probe_reason}")

    # Prefer a direct/ATS URL that could not be machine-verified over a known
    # aggregator. Human review can often open links that block scripted probes.
    if unverified_candidates:
        url, source, reason = unverified_candidates[0]
        return url, "unverified_review", source, reason

    if aggregator_candidates:
        url, source, reason = aggregator_candidates[0]
        return url, "unverified_review", source, f"{reason};aggregator_source"

    return original, "broken", classify_url_source(original, company_domain), ";".join(broken_reasons)[:1000]

def assess_job_signal(
    job: Dict,
    *,
    now: Optional[datetime] = None,
    probe_url: bool = True,
) -> JobSignalAssessment:
    freshness, age_days, freshness_reason = classify_freshness(job, now=now)
    selected_url, url_status, url_source, url_reason = select_job_url(
        job,
        company_domain=job.get("company_domain") or job.get("employer_website") or "",
        probe=probe_url,
    )
    return JobSignalAssessment(
        freshness=freshness,
        age_days=age_days,
        freshness_reason=freshness_reason,
        job_url=selected_url,
        url_status=url_status,
        url_source=url_source,
        url_reason=url_reason,
    )


def annotate_job(job: Dict, *, probe_url: bool = True) -> Dict:
    """Return a copy of ``job`` with stable signal fields attached."""
    assessment = assess_job_signal(job, probe_url=probe_url)
    enriched = dict(job)
    enriched.update(
        {
            "job_freshness": assessment.freshness,
            "job_age_days": assessment.age_days,
            "job_freshness_reason": assessment.freshness_reason,
            "job_url_selected": assessment.job_url,
            "job_url_status": assessment.url_status,
            "job_url_source": assessment.url_source,
            "job_url_reason": assessment.url_reason,
            "job_signal_review_required": assessment.review_required,
            "job_signal_notes": assessment.notes(),
        }
    )
    return enriched


def enrollment_block_reason(fields: Dict, *, probe_missing: bool = True) -> str:
    """Return a human-readable reason when an approved Airtable row is unsafe.

    ``Job Freshness`` and ``Job URL Status`` are deliberately informational.
    A stale opening or missing source URL may still be valuable, and the explicit
    Airtable approval plus Relevance are the human decision points.
    """
    url_status = str(fields.get("Job URL Status") or "").strip().lower()

    if not url_status and probe_missing:
        pseudo_job = {
            "job_apply_link": fields.get("Job URL"),
            "employer_website": fields.get("Website"),
        }
        _url, url_status, _source, _reason = select_job_url(pseudo_job, probe=True)

    if url_status in URL_BLOCKING:
        return (
            f"Job signal requires review: Job URL Status={url_status}. "
            "Replace/verify the Job URL, then change Job URL Status to verified or fallback_used."
        )
    return ""
