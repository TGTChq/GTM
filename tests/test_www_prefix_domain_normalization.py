"""Regression tests for exact ``www.`` prefix removal in host normalization.

Both production paths previously used ``str.lstrip("www.")``, which strips any
leading run of the CHARACTERS 'w' and '.', not the prefix "www.". Every company
domain beginning with 'w' was silently mangled:

    warnerpacific.com  ->  arnerpacific.com
    wiremasters.com    ->  iremasters.com
    wwworld.com        ->  orld.com

Those companies were then enriched against a non-existent domain, which cost
hiring-manager matches across every acquisition source. The fix is exact prefix
removal via ``str.removeprefix``, the idiom already used in ats_board_registry
and multi_source_acquisition.

No network. No provider call.
"""
import pathlib
import unittest

import fantastic_jobs_adapter as fja
import job_quality

#: The seven company domains that exposed the defect in Fantastic scale Batch 10.
BATCH10_AFFECTED = [
    "warnerpacific.com",
    "webdataguru.com",
    "weberassoc.com",
    "whitestoneassoc.com",
    "wiremasters.com",
    "withclutch.com",
    "wurthbaersupply.com",
]


class WwwPrefixRemovalTests(unittest.TestCase):
    """The required cases, asserted against BOTH affected production paths."""

    #: (input host, expected host)
    CASES = [
        ("www.warnerpacific.com", "warnerpacific.com"),
        ("warnerpacific.com", "warnerpacific.com"),
        ("webdataguru.com", "webdataguru.com"),
        ("wwworld.com", "wwworld.com"),
        ("www.acme.com", "acme.com"),
        ("acme.com", "acme.com"),
        # non-'w' domains must be untouched
        ("example.co.uk", "example.co.uk"),
        ("sub.example.com", "sub.example.com"),
        ("zdfirm.com", "zdfirm.com"),
        # only ONE leading "www." is a prefix; the rest is the real name
        ("www.wwworld.com", "wwworld.com"),
        ("www.withclutch.com", "withclutch.com"),
    ]

    def test_fantastic_jobs_adapter_host(self):
        for host, expected in self.CASES:
            with self.subTest(path="fantastic_jobs_adapter._host", host=host):
                self.assertEqual(fja._host(f"https://{host}/jobs/123"), expected)

    def test_job_quality_url_host(self):
        for host, expected in self.CASES:
            with self.subTest(path="job_quality._url_host", host=host):
                self.assertEqual(job_quality._url_host(f"https://{host}/careers"),
                                 expected)

    def test_job_quality_url_host_accepts_bare_domain(self):
        """_url_host adds the scheme itself, so bare domains must also survive."""
        for host, expected in self.CASES:
            with self.subTest(host=host):
                self.assertEqual(job_quality._url_host(host), expected)

    def test_batch10_affected_domains_round_trip_unchanged(self):
        """The exact seven companies the defect corrupted in production."""
        for domain in BATCH10_AFFECTED:
            with self.subTest(domain=domain):
                self.assertEqual(fja._host(f"https://{domain}"), domain)
                self.assertEqual(job_quality._url_host(domain), domain)
                self.assertEqual(fja._host(f"https://www.{domain}"), domain)
                self.assertEqual(job_quality._url_host(f"www.{domain}"), domain)

    def test_leading_w_is_never_stripped(self):
        """Direct guard against the lstrip character-class behaviour returning."""
        for domain in ("w.com", "we.com", "wwe.com", "wwwx.com", "ww.com"):
            with self.subTest(domain=domain):
                self.assertEqual(fja._host(f"https://{domain}"), domain)
                self.assertEqual(job_quality._url_host(domain), domain)

    def test_uppercase_www_prefix_is_removed(self):
        self.assertEqual(fja._host("https://WWW.Warnerpacific.com"), "warnerpacific.com")
        self.assertEqual(job_quality._url_host("WWW.Warnerpacific.com"), "warnerpacific.com")

    def test_empty_and_invalid_input_still_returns_empty(self):
        for value in ("", None, "   ", "not a host"):
            with self.subTest(value=value):
                self.assertEqual(fja._host(value), "")
                self.assertEqual(job_quality._url_host(value), "")


class NoCharacterClassStripRemainsTests(unittest.TestCase):
    """lstrip("www.") must not reappear anywhere in the production tree."""

    def test_no_lstrip_www_in_repository(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if 'lstrip("www.")' in text or "lstrip('www.')" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [],
                         f"character-class strip reintroduced in: {offenders}")


if __name__ == "__main__":
    unittest.main()
