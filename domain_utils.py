"""Utilities for validating and normalizing company domains.

The pipeline receives domains from several noisy sources: employer websites,
career pages, investor-relations pages, ATS redirects, and Apollo.  This module
reduces safe company subdomains to a registrable root using a bundled suffix list.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

import tldextract

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)

# No HTTP or writable cache is needed, including on a fresh container. Private
# hosting suffixes stay at the platform root for the existing intermediary gate.
DOMAIN_NORMALIZATION_VERSION = "public-suffix/1"
_PUBLIC_SUFFIX = tldextract.TLDExtract(
    suffix_list_urls=(), cache_dir=None, include_psl_private_domains=False)

# Subdomains that commonly appear in company-owned career, recruiting, or
# investor URLs.  The final root-domain reduction handles any additional nested
# labels, but keeping this list makes intent explicit and helps readability.
_COMPANY_SUBDOMAIN_PREFIXES = {
    "www", "jobs", "job", "careers", "career", "apply", "recruiting",
    "recruitment", "talent", "people", "work", "join", "joinus",
    "investor", "investors", "ir", "about", "corporate", "corp",
}


def _extract_host(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).strip()
    if not raw or any(ch.isspace() for ch in raw):
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").strip(".").lower()
    except ValueError:
        return ""
    return host


def _is_valid_hostname(host: str) -> bool:
    if not host or "." not in host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    labels = host.split(".")
    return all(_LABEL_RE.fullmatch(label or "") for label in labels)


def normalize_company_domain(value: str | None) -> str:
    """Return a stable root company domain or ``""`` for invalid input.

    Examples:
        ``https://investor.capitalone.com/news`` -> ``capitalone.com``
        ``https://careers.acme.co.uk/jobs`` -> ``acme.co.uk``
        ``google`` -> ``""``
        ``the mitre`` -> ``""``
    """
    host = _extract_host(value)
    if not _is_valid_hostname(host):
        return ""

    labels = host.split(".")
    while len(labels) > 2 and labels[0] in _COMPANY_SUBDOMAIN_PREFIXES:
        labels.pop(0)

    host = ".".join(labels)
    labels = host.split(".")
    extracted = _PUBLIC_SUFFIX(host)
    if extracted.suffix:
        # A public suffix alone is not an employer, e.g. gov.in or edu.ph.
        return extracted.top_domain_under_public_suffix
    # Preserve the established behavior for internal/unlisted test namespaces.
    return ".".join(labels[-2:])
