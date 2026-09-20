"""Phase 3 audit (2026-09-20): qualification recovery — valid jobs the rules still reject,
and campaign assignments the classifier still makes without evidence.

Every change here comes from a ruling Luis has already made (OPEN_DECISIONS.md, resolution pass
2026-09-20) or from the frozen rubric's own "not sufficient" list, measured on the CALIBRATION
stratum and the 411-item purchased-corpus set. The holdout is not read.

One hypothesis per commit:
  A  industry mapping      — online/digital media is not an approved excluded industry (Q6, D3)
  B  substring exclusion   — a Public Trust determination is not a security clearance (D5)
  C  location/travel       — the numeric travel line is withdrawn; "essential" carries the test (D4)
  D  classifier evidence   — a campaign assignment needs deterministic support, per campaign
"""
from __future__ import annotations

from tgtc_core.domain.facts import RULE_VERSION, extract_job_facts
from tgtc_core.policy.requirements import EXCLUDED_INDUSTRIES, excluded_industry

#: Enough ordinary prose that the postings under test are judged on the clause being tested,
#: not on being too short for the classifier to read.
_FILLER = ("You will build reports and dashboards for the analytics team, work with stakeholders "
           "across the business, and document your work. This role is remote.")


# --- A: industry mapping ----------------------------------------------------
# Luis, OPEN_DECISIONS.md: "Online news, digital media and media production stay allowed —
# not among the fixed exclusions" (Q6, restated in the 2026-09-20 resolution pass), and D3
# keeps the APPROVED list as-is. The rubric's own §4.K agrees: "online news, digital media,
# internet publishing and media production are NOT on the approved list ... do not exclude".
# Broadcast media, newspapers and book publishing ARE on the approved list and stay.

MEDIA_LABELS_LUIS_ALLOWS = (
    "online media", "internet news", "news media", "media production",
    "digital news", "financial news",
)
APPROVED_MEDIA_EXCLUSIONS = ("broadcast media", "newspapers", "book publishing")


def test_online_and_digital_media_industries_are_not_excluded():
    for label in MEDIA_LABELS_LUIS_ALLOWS:
        assert excluded_industry(label) == "", label
        assert label not in EXCLUDED_INDUSTRIES, label


def test_the_approved_media_exclusions_are_untouched():
    for label in APPROVED_MEDIA_EXCLUSIONS:
        assert excluded_industry(label) == label, label


def test_the_rest_of_the_approved_industry_list_is_untouched():
    for label in ("staffing and recruiting", "government administration", "mental health care",
                  "hospitals and health care", "human resources services", "outsourcing/offshoring",
                  "events services", "chemicals", "medical practice"):
        assert excluded_industry(label) == label, label


def test_an_apollo_qualifier_suffix_still_matches_an_approved_label():
    assert excluded_industry("Broadcast Media / Television") == "broadcast media"
