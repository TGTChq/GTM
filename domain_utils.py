"""Utilities for validating and normalizing company domains.

The pipeline receives domains from several noisy sources: employer websites,
career pages, investor-relations pages, ATS redirects, and Apollo.  This module
reduces safe company subdomains to a registrable-looking root domain without
adding a new runtime dependency.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)

# Common public suffixes where the registrable domain needs three labels.
# This is intentionally conservative; it covers the markets most likely in the
# current US-targeted pipeline while avoiding a heavy public-suffix dependency.
_MULTI_LABEL_PUBLIC_SUFFIXES = {
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "net.nz", "org.nz", "govt.nz",
    "co.za", "org.za", "net.za",
    "com.br", "com.mx", "com.ar", "com.co", "com.pe", "com.cl",
    "com.sg", "com.hk", "com.tw", "com.my", "com.ph",
    "co.jp", "co.kr", "co.in", "firm.in", "net.in", "org.in",
    "com.cn", "com.tr", "com.sa", "com.eg", "com.ng",
    "com.de", "com.fr", "com.es", "com.it", "com.nl",
}

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
    suffix2 = ".".join(labels[-2:])
    if suffix2 in _MULTI_LABEL_PUBLIC_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])
