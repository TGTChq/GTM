"""Employer-correctness check for alternate mail domains accepted by rule B.

For each DISTINCT (employer, email domain) pair among B's acceptances -- a pair,
not a person, because the domain is the claim being tested -- fetch the email
domain's public website and record:

* redirects_to_employer: the final URL's host is the employer domain (or a
  subdomain of it);
* names_employer: the page title or first 20 KB contains a distinctive token of
  the employer's name;
* verdict: CONFIRMED if either holds, UNCONFIRMED if the site answers but shows
  neither, UNREACHABLE if no site answers (common for mail-only domains --
  NOT evidence against).

Company-level only; no person data is read or printed.
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

import requests

STOP = {"inc", "llc", "ltd", "corp", "co", "company", "group", "the", "and", "of", "holdings", "services",
        "international", "partners", "management", "solutions", "systems", "technologies", "a", "s"}


def tokens(name: str):
    return [t for t in re.findall(r"[a-z0-9]{3,}", name.lower()) if t not in STOP]


def fetch(domain: str):
    for scheme in ("https://", "http://"):
        try:
            r = requests.get(scheme + domain, timeout=8, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; tgtc-domain-check)"})
            return r.url, r.text[:20000]
        except requests.RequestException:
            continue
    return None, ""


def main():
    rows = {json.loads(l)["k"]: json.loads(l) for l in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if l.strip()}
    accepted = [int(x) for x in Path(sys.argv[2]).read_text(encoding="utf-8").split() if x.strip()]
    sample_n = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    pairs = sorted({(rows[k]["employer_name"], rows[k]["employer_domain"], rows[k]["email_domain"]) for k in accepted})
    random.seed(20260922)
    if sample_n and len(pairs) > sample_n:
        pairs = random.sample(pairs, sample_n)
    counts = {"CONFIRMED": 0, "UNCONFIRMED": 0, "UNREACHABLE": 0}
    for employer_name, employer_domain, mail_domain in pairs:
        final_url, body = fetch(mail_domain)
        if final_url is None:
            verdict, detail = "UNREACHABLE", ""
        else:
            host = re.sub(r"^www\.", "", (requests.utils.urlparse(final_url).hostname or "").lower())
            redirects = host == employer_domain or host.endswith("." + employer_domain)
            title = " ".join(re.findall(r"<title[^>]*>(.*?)</title>", body, re.I | re.S))[:200]
            names = any(t in (title + " " + body).lower() for t in tokens(employer_name))
            verdict = "CONFIRMED" if (redirects or names) else "UNCONFIRMED"
            detail = f"final_host={host} redirects={redirects} names_employer={names} title={title.strip()[:60]!r}"
        counts[verdict] += 1
        print(f"{verdict:<11} {employer_name[:34]:<34} {employer_domain:<28} {mail_domain:<26} {detail}")
    print("summary:", counts, "pairs:", len(pairs))


if __name__ == "__main__":
    main()
